"""Real-process timeout/cancellation boundaries for O0-C42."""
import json
import subprocess
import sys
import time
from pathlib import Path

from scripts.test_o0_recovery import O0InterruptionRecoveryTest


class O0TimeoutCancellationTest(O0InterruptionRecoveryTest):
    def _slow_first_actor(self):
        self.actor.write_text(
            """import json, os, subprocess, sys, time
from pathlib import Path
marker = Path(sys.argv[1])
with marker.open('a', encoding='utf-8') as stream:
    stream.write(f'{os.getpid()}\\n')
attempt = len(marker.read_text(encoding='utf-8').splitlines())
if attempt == 1:
    Path(os.environ['IDEAS_STANDARD_REPORT']).write_text('partial', encoding='utf-8')
    time.sleep(60)
sha = subprocess.run(['git','rev-parse','HEAD'], capture_output=True, text=True, check=True).stdout.strip()
payload = {'schema_version':'0.1','executor_id':'real-builder','role':'BUILDER','authority':'IMPLEMENTATION','result_sha':sha,'result':'READY_FOR_AUDIT','summary':'resumed','changed_paths':['artifact.txt'],'checks':[{'id':'real-process','status':'PASS','evidence':'resumed execution'}],'limitations':[],'disputed_findings':[],'escalation':None}
Path(os.environ['IDEAS_STANDARD_REPORT']).write_text(json.dumps(payload), encoding='utf-8')
""", encoding="utf-8"
        )

    def _set_config(self, **changes):
        payload = json.loads(self.config.read_text(encoding="utf-8"))
        payload.update(changes)
        self.config.write_text(json.dumps(payload), encoding="utf-8")

    def _assert_interrupted_then_resume(self, first, reason):
        self.assertEqual(2, first.returncode, first.stderr)
        self.assertIn(reason.lower(), first.stderr.lower())
        self.assertEqual(self.original_state, self.state.read_bytes())
        journals = list((self.root / "reports" / "operations").glob("*.journal.json"))
        self.assertEqual(1, len(journals))
        journal = json.loads(journals[0].read_text(encoding="utf-8"))
        self.assertEqual("INTERRUPTED", journal["phase"])
        self.assertEqual(reason, journal["interruption_reason"])
        self.assertIsNone(journal["result"])
        self.assertFalse((self.root / "reports" / "builder-report.json").exists())
        self.assertEqual(1, len(self.marker.read_text(encoding="utf-8").splitlines()))
        stopped = self._resume()
        self.assertEqual(2, stopped.returncode)
        self.assertTrue(journals[0].exists())
        self.assertFalse((self.root / "reports" / "builder-report.json").exists())
        self.assertEqual(1, len(self.marker.read_text(encoding="utf-8").splitlines()))
        self._set_config(resume_interrupted=True)
        resumed = self._resume()
        self.assertEqual(0, resumed.returncode, resumed.stderr)
        self.assertEqual("READY_FOR_AUDIT", json.loads(resumed.stdout)["machine_state"])
        self.assertEqual(2, len(self.marker.read_text(encoding="utf-8").splitlines()))
        self.assertFalse(journals[0].exists())

    def test_timeout_records_interrupted_operation_without_partial_transition(self):
        self._slow_first_actor()
        self._set_config(actor_timeout_seconds=0.25)
        self.original_state = self.state.read_bytes()
        self._assert_interrupted_then_resume(self._resume(), "TIMEOUT")

    def test_cancel_records_interrupted_operation_without_partial_transition(self):
        self._slow_first_actor()
        cancel = self.root / "cancel.request"
        self._set_config(cancel_path=str(cancel))
        self.original_state = self.state.read_bytes()
        process = subprocess.Popen(
            [sys.executable, "-m", "scripts.o0_runner", "--config", str(self.config)],
            cwd=self.source, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.monotonic() + 10
        while not self.marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.marker.exists(), process.communicate(timeout=5) if process.poll() is not None else "actor did not start")
        cancel.write_text("cancel", encoding="utf-8")
        stdout, stderr = process.communicate(timeout=10)
        first = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        cancel.unlink()
        self._assert_interrupted_then_resume(first, "CANCELLED")

    def test_preexisting_cancellation_never_starts_actor(self):
        cancel = self.root / "cancel-before-start.request"
        cancel.write_text("cancel", encoding="utf-8")
        self._set_config(cancel_path=str(cancel))
        before = self.state.read_bytes()
        first = self._resume()
        self.assertEqual(2, first.returncode, first.stderr)
        self.assertFalse(self.marker.exists())
        self.assertEqual(before, self.state.read_bytes())
        journals = list((self.root / "reports" / "operations").glob("*.journal.json"))
        self.assertEqual(1, len(journals))
        self.assertEqual("CANCELLED", json.loads(journals[0].read_text())["interruption_reason"])
