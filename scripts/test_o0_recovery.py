import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from scripts.orchestrate_handoffs import init_state


class O0InterruptionRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = Path(__file__).resolve().parents[1]
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self._git("init", "-b", "builder/o0-c41")
        self._git("config", "user.name", "O0 C41")
        self._git("config", "user.email", "o0-c41@example.invalid")
        (self.repository / "artifact.txt").write_text("candidate\n", encoding="utf-8")
        self._git("add", "artifact.txt")
        self._git("commit", "-m", "controlled candidate")
        self.state = self.root / "state.json"
        init_state(
            self.source / "orchestration" / "builder-auditor-policy.json",
            self.state,
            project_id="o0-c41-recovery",
            phase="O0",
            gate="S1",
            builder_branch="builder/o0-c41",
        )
        self.marker = self.root / "actor-runs.txt"
        self.actor = self.root / "actor.py"
        self.actor.write_text(
            """import json, os, subprocess, sys
from pathlib import Path
marker = Path(sys.argv[1])
with marker.open('a', encoding='utf-8') as stream:
    stream.write(f'{os.getpid()}\\n')
sha = subprocess.run(['git','rev-parse','HEAD'], capture_output=True, text=True, check=True).stdout.strip()
payload = {'schema_version':'0.1','executor_id':'real-builder','role':'BUILDER','authority':'IMPLEMENTATION','result_sha':sha,'result':'READY_FOR_AUDIT','summary':'interruption recovery','changed_paths':['artifact.txt'],'checks':[{'id':'real-process','status':'PASS','evidence':'one actor execution'}],'limitations':[],'disputed_findings':[],'escalation':None}
Path(os.environ['IDEAS_STANDARD_REPORT']).write_text(json.dumps(payload), encoding='utf-8')
""",
            encoding="utf-8",
        )
        self.config = self.root / "runner.json"
        self.config.write_text(
            json.dumps(
                {
                    "repository": str(self.repository),
                    "state_path": str(self.state),
                    "reports_dir": str(self.root / "reports"),
                    "builder_workspace": str(self.repository),
                    "audit_workspaces": str(self.root / "audits"),
                    "builder_command": [sys.executable, str(self.actor), str(self.marker)],
                    "auditor_command": ["must-not-run"],
                    "lock_timeout_seconds": 0,
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def _git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.repository, capture_output=True, text=True, check=True
        )

    def _start_paused_runner(self, function_name: str):
        reached = self.root / f"{function_name}.reached"
        wrapper = self.root / f"pause-{function_name}.py"
        wrapper.write_text(
            f"""import sys, time
from pathlib import Path
sys.path.insert(0, {str(self.source)!r})
import scripts.o0_runner as runner
original = runner.{function_name}
def paused(*args, **kwargs):
    Path({str(reached)!r}).write_text('reached', encoding='utf-8')
    time.sleep(60)
    return original(*args, **kwargs)
runner.{function_name} = paused
sys.argv = ['o0_runner', '--config', {str(self.config)!r}]
raise SystemExit(runner.main())
""",
            encoding="utf-8",
        )
        process = subprocess.Popen(
            [sys.executable, str(wrapper)],
            cwd=self.source,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 10
        while not reached.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if not reached.exists():
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
            self.fail((stdout, stderr))
        process.kill()
        process.communicate(timeout=5)

    def _start_paused_operation_record_write(self):
        reached = self.root / "operation-record-write.reached"
        wrapper = self.root / "pause-operation-record-write.py"
        wrapper.write_text(
            f"""import sys, time
from pathlib import Path
sys.path.insert(0, {str(self.source)!r})
import scripts.o0_runner as runner
original = runner._write_canonical_atomic
def paused(path, payload):
    if path.name.startswith('op-') and path.name.endswith('.json') and '.journal.' not in path.name:
        Path({str(reached)!r}).write_text('reached', encoding='utf-8')
        time.sleep(60)
    return original(path, payload)
runner._write_canonical_atomic = paused
sys.argv = ['o0_runner', '--config', {str(self.config)!r}]
raise SystemExit(runner.main())
""",
            encoding="utf-8",
        )
        process = subprocess.Popen(
            [sys.executable, str(wrapper)], cwd=self.source,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.monotonic() + 10
        while not reached.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if not reached.exists():
            process.kill()
            self.fail(process.communicate(timeout=5))
        process.kill()
        process.communicate(timeout=5)

    def _start_interrupted_evidence_write(self):
        reached = self.root / "evidence-write.reached"
        wrapper = self.root / "pause-evidence-write.py"
        wrapper.write_text(
            f"""import os, sys, time
from pathlib import Path
sys.path.insert(0, {str(self.source)!r})
import scripts.o0_runner as runner
original = runner._write_bytes_atomic
def interrupted(path, payload):
    if path.parent.name == 'evidence':
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + f'.{{os.getpid()}}.tmp')
        temporary.write_bytes(payload[:len(payload)//2])
        Path({str(reached)!r}).write_text('reached', encoding='utf-8')
        time.sleep(60)
    return original(path, payload)
runner._write_bytes_atomic = interrupted
sys.argv = ['o0_runner', '--config', {str(self.config)!r}]
raise SystemExit(runner.main())
""",
            encoding="utf-8",
        )
        process = subprocess.Popen(
            [sys.executable, str(wrapper)], cwd=self.source,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.monotonic() + 10
        while not reached.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if not reached.exists():
            stdout, stderr = process.communicate(timeout=5)
            self.fail((stdout, stderr))
        process.kill()
        process.communicate(timeout=5)

    def _resume(self):
        return subprocess.run(
            [sys.executable, "-m", "scripts.o0_runner", "--config", str(self.config)],
            cwd=self.source,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def _assert_recovered_once(self, result):
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("READY_FOR_AUDIT", payload["machine_state"])
        self.assertTrue(payload["operation_recovered"])
        self.assertEqual(1, len(self.marker.read_text(encoding="utf-8").splitlines()))
        operation_id = payload["operation_id"]
        operation_root = self.root / "reports" / "operations"
        self.assertTrue((operation_root / f"{operation_id}.json").is_file())
        self.assertTrue((operation_root / f"{operation_id}.report.json").is_file())
        self.assertFalse((operation_root / f"{operation_id}.journal.json").exists())

    def test_resume_after_report_does_not_execute_builder_twice(self):
        # Removing recovery before builder_handoff makes this rerun the actor.
        self._start_paused_runner("builder_handoff")

        resumed = self._resume()

        self._assert_recovered_once(resumed)

    def test_resume_after_state_transition_finishes_operation_record(self):
        # Removing recovery before persist_operation selects the next actor instead.
        self._start_paused_runner("persist_operation")
        applied_state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual("READY_FOR_AUDIT", applied_state["machine_state"])

        resumed = self._resume()

        self._assert_recovered_once(resumed)

    def test_resume_after_report_snapshot_finishes_atomic_operation_record(self):
        # Removing partial-snapshot recovery makes this fail on FileExistsError.
        self._start_paused_operation_record_write()
        snapshots = list((self.root / "reports" / "operations").glob("*.report.json"))
        self.assertEqual(1, len(snapshots))

        resumed = self._resume()

        self._assert_recovered_once(resumed)

    def test_tampered_journal_result_is_rejected_without_state_mutation(self):
        self._start_paused_runner("finish_recovered_operation")
        state_before = self.state.read_bytes()
        journal_path = next((self.root / "reports" / "operations").glob("*.journal.json"))
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["result"]["phase"] = "S2"
        journal_path.write_bytes(
            json.dumps(journal, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        )

        resumed = self._resume()

        self.assertEqual(2, resumed.returncode)
        self.assertIn("journal result", resumed.stderr.lower())
        self.assertEqual(state_before, self.state.read_bytes())
        self.assertEqual(1, len(self.marker.read_text(encoding="utf-8").splitlines()))

    def test_nonzero_actor_with_report_is_recovered_without_second_execution(self):
        self.actor.write_text(self.actor.read_text(encoding="utf-8") + "\nsys.exit(7)\n", encoding="utf-8")

        failed = self._resume()
        resumed = self._resume()

        self.assertEqual(2, failed.returncode)
        self._assert_recovered_once(resumed)

    def test_partial_snapshot_is_replaced_from_validated_recovery_report(self):
        self._start_paused_operation_record_write()
        snapshot = next((self.root / "reports" / "operations").glob("*.report.json"))
        content = snapshot.read_bytes()
        snapshot.write_bytes(content[: len(content) // 2])

        resumed = self._resume()

        self._assert_recovered_once(resumed)

    def test_kill_during_evidence_write_resumes_from_raw_report(self):
        self._start_interrupted_evidence_write()
        evidence_root = self.root / "reports" / "evidence"
        self.assertFalse(list(evidence_root.glob("*.json")))

        resumed = self._resume()

        self._assert_recovered_once(resumed)
        self.assertEqual(1, len(list(evidence_root.glob("*.json"))))


if __name__ == "__main__":
    unittest.main()
