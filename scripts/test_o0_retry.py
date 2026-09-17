"""O0-C44: limited retries and rejection of duplicate operations or accepted reports."""
import json
import subprocess
import sys
from pathlib import Path

from scripts.orchestrate_handoffs import init_state
from scripts.test_o0_recovery import O0InterruptionRecoveryTest


class O0RetryTest(O0InterruptionRecoveryTest):
    def test_retry_limit_blocks_excessive_attempts_without_actor_execution(self):
        self.actor.write_text(
            "import sys\n"
            f"marker = open({str(self.marker)!r}, 'a', encoding='utf-8')\n"
            "marker.write('failed-attempt\\n')\n"
            "marker.close()\n"
            "raise SystemExit(7)\n",
            encoding="utf-8",
        )
        config_data = json.loads(self.config.read_text(encoding="utf-8"))
        config_data["max_retries"] = 2
        self.config.write_text(json.dumps(config_data), encoding="utf-8")

        before_state = self.state.read_bytes()

        first = self._resume()
        self.assertEqual(2, first.returncode)
        self.assertEqual(1, len(self.marker.read_text(encoding="utf-8").splitlines()))

        second = self._resume()
        self.assertEqual(2, second.returncode)
        self.assertEqual(2, len(self.marker.read_text(encoding="utf-8").splitlines()))

        third = self._resume()
        self.assertEqual(2, third.returncode)
        self.assertIn("Operation retry limit exceeded", third.stderr)
        self.assertEqual(2, len(self.marker.read_text(encoding="utf-8").splitlines()))
        self.assertEqual(before_state, self.state.read_bytes())

    def test_duplicate_report_across_operations_is_rejected(self):
        self.actor.write_text(
            f"""import json, os, subprocess, sys
from pathlib import Path
marker = Path(sys.argv[1])
with marker.open('a', encoding='utf-8') as stream:
    stream.write(f'{{os.getpid()}}\\n')
sha = subprocess.run(['git','rev-parse','HEAD'], capture_output=True, text=True, check=True).stdout.strip()
payload = {{'schema_version':'0.1','executor_id':'real-builder','role':'BUILDER','authority':'IMPLEMENTATION','result_sha':sha,'result':'READY_FOR_AUDIT','summary':'report test','changed_paths':['artifact.txt'],'checks':[],'limitations':[],'disputed_findings':[],'escalation':None}}
Path(os.environ['IDEAS_STANDARD_REPORT']).write_text(json.dumps(payload), encoding='utf-8')
""",
            encoding="utf-8",
        )
        first = self._resume()
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual(1, len(self.marker.read_text(encoding="utf-8").splitlines()))

        # Set up a distinct operation with a different run_id sharing reports_dir
        second_state_path = self.root / "state2.json"
        init_state(
            self.source / "orchestration" / "builder-auditor-policy.json",
            second_state_path,
            project_id="o0-c41-recovery-2",
            phase="O0",
            gate="S1",
            builder_branch="builder/o0-c41",
        )

        config2_path = self.root / "config2.json"
        config2_data = json.loads(self.config.read_text(encoding="utf-8"))
        config2_data["state_path"] = "state2.json"
        config2_path.write_text(json.dumps(config2_data), encoding="utf-8")

        second = subprocess.run(
            [sys.executable, "-m", "scripts.o0_runner", "--config", str(config2_path)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(2, second.returncode)
        self.assertIn("Report has already been accepted", second.stderr)

    def test_completed_operation_without_replay_config_is_rejected(self):
        before_state = self.state.read_bytes()
        first = self._resume()
        self.assertEqual(0, first.returncode, first.stderr)
        runs_before = len(self.marker.read_text(encoding="utf-8").splitlines())

        self.state.write_bytes(before_state)

        re_run = self._resume()
        self.assertEqual(2, re_run.returncode)
        self.assertIn("Operation has already been completed", re_run.stderr)
        self.assertEqual(runs_before, len(self.marker.read_text(encoding="utf-8").splitlines()))

    def test_successful_retry_advances_state_when_within_limit(self):
        flaky_flag = self.root / "fail_once.flag"
        flaky_flag.write_text("fail", encoding="utf-8")
        original_actor = self.actor.read_text(encoding="utf-8")
        self.actor.write_text(
            f"from pathlib import Path\n"
            f"flag = Path({str(flaky_flag)!r})\n"
            f"if flag.exists():\n"
            f"    flag.unlink()\n"
            f"    raise SystemExit(5)\n"
            + original_actor,
            encoding="utf-8",
        )
        config_data = json.loads(self.config.read_text(encoding="utf-8"))
        config_data["max_retries"] = 2
        self.config.write_text(json.dumps(config_data), encoding="utf-8")

        first = self._resume()
        self.assertEqual(2, first.returncode)

        second = self._resume()
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual("READY_FOR_AUDIT", json.loads(self.state.read_text())["machine_state"])

    def test_persist_operation_and_check_duplicate_report(self):
        import hashlib
        from scripts.o0_runner import persist_operation, check_report_not_already_accepted, operation_identity
        from scripts.orchestrate_handoffs import HandoffError, status

        reports_dir = self.root / "unit_reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        report_file = self.root / "sample_report.json"
        report_file.write_text('{"result":"test"}', encoding="utf-8")

        result_state = status(self.state)
        op_first, source_first = operation_identity(result_state, "BUILDER")
        source_second = dict(source_first)
        source_second["run_id"] = "run-" + "2" * 64
        op_second = "op-" + hashlib.sha256(json.dumps(source_second, sort_keys=True).encode("utf-8")).hexdigest()

        # First persist succeeds
        persisted = persist_operation(reports_dir, op_first, source_first, "BUILDER", report_file, result_state)
        self.assertEqual(op_first, persisted["operation_id"])
        self.assertFalse(persisted["operation_replayed"])

        # Same operation_id replays idempotently
        replayed = persist_operation(reports_dir, op_first, source_first, "BUILDER", report_file, result_state)
        self.assertEqual(op_first, replayed["operation_id"])
        self.assertTrue(replayed["operation_replayed"])

        # Different operation_id with same report is rejected
        digest = hashlib.sha256(report_file.read_bytes()).hexdigest()
        with self.assertRaises(HandoffError) as ctx:
            check_report_not_already_accepted(reports_dir, digest, op_second)
        self.assertIn("Report has already been accepted", str(ctx.exception))
        with self.assertRaises(HandoffError) as ctx:
            persist_operation(reports_dir, op_second, source_second, "BUILDER", report_file, result_state)
        self.assertIn("Report has already been accepted", str(ctx.exception))
