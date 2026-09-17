import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.orchestrate_handoffs import init_state


class O0IdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = Path(__file__).resolve().parents[1]
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self._git("init", "-b", "builder/o0-c40")
        self._git("config", "user.name", "O0 C40")
        self._git("config", "user.email", "o0-c40@example.invalid")
        (self.repository / "artifact.txt").write_text("candidate\n", encoding="utf-8")
        self._git("add", "artifact.txt")
        self._git("commit", "-m", "controlled candidate")
        self.state = self.root / "state.json"
        init_state(
            self.source / "orchestration" / "builder-auditor-policy.json",
            self.state,
            project_id="o0-c40-idempotency",
            phase="O0",
            gate="S1",
            builder_branch="builder/o0-c40",
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
payload = {'schema_version':'0.1','executor_id':'real-builder','role':'BUILDER','authority':'IMPLEMENTATION','result_sha':sha,'result':'READY_FOR_AUDIT','summary':'real idempotent operation','changed_paths':['artifact.txt'],'checks':[{'id':'real-process','status':'PASS','evidence':'one actor execution'}],'limitations':[],'disputed_findings':[],'escalation':None}
Path(os.environ['IDEAS_STANDARD_REPORT']).write_text(json.dumps(payload), encoding='utf-8')
""",
            encoding="utf-8",
        )
        self.config = self.root / "runner.json"
        self._write_config()

    def tearDown(self):
        self.temp.cleanup()

    def _git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.repository, capture_output=True, text=True, check=True
        )

    def _write_config(self, operation_id=None, auditor_command=None):
        payload = {
            "repository": str(self.repository),
            "state_path": str(self.state),
            "reports_dir": str(self.root / "reports"),
            "builder_workspace": str(self.repository),
            "audit_workspaces": str(self.root / "audits"),
            "builder_command": [sys.executable, str(self.actor), str(self.marker)],
            "auditor_command": auditor_command or ["must-not-run"],
            "lock_timeout_seconds": 0,
        }
        if operation_id is not None:
            payload["operation_id"] = operation_id
        self.config.write_text(json.dumps(payload), encoding="utf-8")

    def _run_runner(self):
        return subprocess.run(
            [sys.executable, "-m", "scripts.o0_runner", "--config", str(self.config)],
            cwd=self.source,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    @staticmethod
    def _tree_digests(root: Path):
        return {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def test_same_operation_replays_persisted_result_without_actor_or_duplicates(self):
        first = self._run_runner()
        self.assertEqual(0, first.returncode, first.stderr)
        first_result = json.loads(first.stdout)
        self.assertIn("operation_id", first_result)
        self.assertFalse(first_result["operation_replayed"])
        self.assertFalse(first_result["operation_recovered"])
        operation_id = first_result["operation_id"]
        record = json.loads(
            (self.root / "reports" / "operations" / f"{operation_id}.json").read_text(encoding="utf-8")
        )
        self.assertEqual("BUILDER", record["source"]["actor"])
        self.assertEqual("READY_FOR_BUILD", record["source"]["machine_state"])
        self.assertEqual(0, record["source"]["audit_round"])
        self.assertEqual(first_result["run_id"], record["source"]["run_id"])
        self._write_config(operation_id)
        state_before = self.state.read_bytes()
        reports_before = self._tree_digests(self.root / "reports")

        replay = self._run_runner()

        self.assertEqual(0, replay.returncode, replay.stderr)
        replay_result = json.loads(replay.stdout)
        self.assertEqual(operation_id, replay_result["operation_id"])
        self.assertTrue(replay_result["operation_replayed"])
        self.assertEqual("READY_FOR_AUDIT", replay_result["machine_state"])
        self.assertEqual(1, len(self.marker.read_text(encoding="utf-8").splitlines()))
        self.assertEqual(state_before, self.state.read_bytes())
        self.assertEqual(reports_before, self._tree_digests(self.root / "reports"))

    def test_same_identity_rejects_changed_payload_without_mutation(self):
        first = self._run_runner()
        self.assertEqual(0, first.returncode, first.stderr)
        operation_id = json.loads(first.stdout)["operation_id"]
        self._write_config(operation_id)
        report = self.root / "reports" / "builder-report.json"
        payload = json.loads(report.read_text(encoding="utf-8"))
        payload["summary"] = "different payload with reused identity"
        report.write_text(json.dumps(payload), encoding="utf-8")
        state_before = self.state.read_bytes()
        marker_before = self.marker.read_bytes()

        replay = self._run_runner()

        self.assertEqual(2, replay.returncode)
        self.assertIn("Operation payload differs", replay.stderr)
        self.assertEqual(state_before, self.state.read_bytes())
        self.assertEqual(marker_before, self.marker.read_bytes())
        self.assertEqual(1, len(list((self.root / "reports" / "operations").glob("*.report.json"))))

    def test_unknown_identity_is_rejected_before_actor(self):
        self._write_config("op-" + "f" * 64)
        state_before = self.state.read_bytes()

        result = self._run_runner()

        self.assertEqual(2, result.returncode)
        self.assertIn("does not match the current state transition", result.stderr)
        self.assertFalse(self.marker.exists())
        self.assertEqual(state_before, self.state.read_bytes())

    def test_replay_rejects_forged_source_actor_or_result(self):
        first = self._run_runner()
        self.assertEqual(0, first.returncode, first.stderr)
        operation_id = json.loads(first.stdout)["operation_id"]
        self._write_config(operation_id)
        record_path = self.root / "reports" / "operations" / f"{operation_id}.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["source"]["machine_state"] = "FIX_REQUIRED"
        record["actor"] = "AUDITOR"
        record["result"]["message"] = "forged but schema-valid result"
        record_path.write_bytes(
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        state_before = self.state.read_bytes()
        marker_before = self.marker.read_bytes()

        replay = self._run_runner()

        self.assertEqual(2, replay.returncode)
        self.assertIn("operation record identity", replay.stderr.lower())
        self.assertEqual(state_before, self.state.read_bytes())
        self.assertEqual(marker_before, self.marker.read_bytes())

    @unittest.skipIf(os.name == "nt", "Auditor write sandbox requires POSIX Landlock")
    def test_audit_replay_does_not_duplicate_round_report_or_evidence(self):
        builder = self._run_runner()
        self.assertEqual(0, builder.returncode, builder.stderr)
        audit_marker = self.root / "reports" / "auditor-runs.txt"
        auditor = self.root / "auditor.py"
        auditor.write_text(
            """import json, os, sys
from pathlib import Path
marker = Path(sys.argv[1])
with marker.open('a', encoding='utf-8') as stream:
    stream.write(f'{os.getpid()}\\n')
target = os.environ['IDEAS_STANDARD_AUDIT_TARGET_SHA']
payload = {'schema_version':'0.1','executor_id':'real-auditor','role':'AUDITOR','authority':'INDEPENDENT_AUDIT','audit_result':'PASS','audited_sha':target,'summary':'real audit operation','findings':[],'checks':[{'id':'real-audit','status':'PASS','evidence':'one auditor execution'}],'residual_risks':[],'gate_registration':'NOT_AUTHORIZED'}
Path(os.environ['IDEAS_STANDARD_REPORT']).write_text(json.dumps(payload), encoding='utf-8')
""",
            encoding="utf-8",
        )
        auditor_command = [sys.executable, str(auditor), str(audit_marker)]
        self._write_config(auditor_command=auditor_command)
        audit = self._run_runner()
        self.assertEqual(0, audit.returncode, audit.stderr)
        audit_result = json.loads(audit.stdout)
        operation_id = audit_result["operation_id"]
        self.assertEqual(1, audit_result["audit_round"])
        self._write_config(operation_id, auditor_command)
        state_before = self.state.read_bytes()
        reports_before = self._tree_digests(self.root / "reports")

        replay = self._run_runner()

        self.assertEqual(0, replay.returncode, replay.stderr)
        replay_result = json.loads(replay.stdout)
        self.assertTrue(replay_result["operation_replayed"])
        self.assertEqual(1, replay_result["audit_round"])
        self.assertEqual(1, len(audit_marker.read_text(encoding="utf-8").splitlines()))
        self.assertEqual(state_before, self.state.read_bytes())
        self.assertEqual(reports_before, self._tree_digests(self.root / "reports"))


if __name__ == "__main__":
    unittest.main()
