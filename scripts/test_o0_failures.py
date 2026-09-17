"""O0-C43: persisted, redacted runner failures from real processes."""
import json
import subprocess
import sys

from scripts.test_o0_recovery import O0InterruptionRecoveryTest


class O0RunnerFailureTest(O0InterruptionRecoveryTest):
    def test_actor_nonzero_persists_exit_evidence_without_secret_or_state_advance(self):
        secret = "C43_SECRET_DO_NOT_EXPOSE_8b5d"
        self.actor.write_text(
            "import sys\nprint('C43_SECRET_DO_NOT_EXPOSE_8b5d')\n"
            "print('C43_SECRET_DO_NOT_EXPOSE_8b5d', file=sys.stderr)\n"
            "raise SystemExit(7)\n",
            encoding="utf-8",
        )
        before = self.state.read_bytes()

        failed = self._resume()

        self.assertEqual(2, failed.returncode)
        self.assertNotIn(secret, failed.stdout + failed.stderr)
        self.assertEqual(before, self.state.read_bytes())
        records = list((self.root / "reports" / "runner-failures").glob("*.json"))
        self.assertEqual(1, len(records))
        raw = records[0].read_text(encoding="utf-8")
        self.assertNotIn(secret, raw)
        evidence = json.loads(raw)
        self.assertEqual("ACTOR_EXIT_NONZERO", evidence["kind"])
        self.assertEqual(2, evidence["runner_exit_code"])
        self.assertEqual(7, evidence["actor_exit_code"])
        self.assertEqual(json.loads(before)["run_id"], evidence["run_id"])
        self.assertEqual("READY_FOR_BUILD", evidence["machine_state"])

        restarted = self._resume()
        self.assertEqual(2, restarted.returncode)
        self.assertTrue(records[0].is_file())
        self.assertEqual(raw, records[0].read_text(encoding="utf-8"))
        self.assertEqual(before, self.state.read_bytes())

    def test_invalid_actor_report_records_redacted_failure_without_transition(self):
        secret = "C43_REPORT_SECRET_1d8b"
        self.actor.write_text(
            "import os\nfrom pathlib import Path\n"
            "Path(os.environ['IDEAS_STANDARD_REPORT']).write_text("
            "'{\"secret\":\"C43_REPORT_SECRET_1d8b\"}', encoding='utf-8')\n",
            encoding="utf-8",
        )
        before = self.state.read_bytes()

        failed = self._resume()

        self.assertEqual(2, failed.returncode)
        self.assertNotIn(secret, failed.stdout + failed.stderr)
        self.assertEqual(before, self.state.read_bytes())
        records = list((self.root / "reports" / "runner-failures").glob("*.json"))
        self.assertEqual(1, len(records))
        self.assertNotIn(secret, records[0].read_text(encoding="utf-8"))
        self.assertEqual("HANDOFF_REJECTED", json.loads(records[0].read_text())["kind"])

    def test_malformed_config_persists_redacted_bootstrap_failure(self):
        secret = "C43_CONFIG_SECRET_3f0c"
        self.config.write_text('{"secret":"' + secret + '",', encoding="utf-8")
        before = self.state.read_bytes()

        failed = self._resume()

        self.assertEqual(2, failed.returncode)
        self.assertNotIn(secret, failed.stdout + failed.stderr)
        self.assertEqual(before, self.state.read_bytes())
        records = list((self.root / "runner-failures").glob("*.json"))
        self.assertEqual(1, len(records))
        self.assertNotIn(secret, records[0].read_text(encoding="utf-8"))
        evidence = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertEqual("INVALID_JSON", evidence["kind"])
        self.assertEqual(2, evidence["runner_exit_code"])

    def test_actor_exit_after_report_records_failure_then_c41_recovers_once(self):
        original = self.actor.read_text(encoding="utf-8")
        self.actor.write_text(original + "\nraise SystemExit(7)\n", encoding="utf-8")
        before = self.state.read_bytes()

        first = self._resume()
        self.assertEqual(2, first.returncode)
        self.assertEqual(before, self.state.read_bytes())
        records = list((self.root / "reports" / "runner-failures").glob("*.json"))
        self.assertEqual(1, len(records))
        self.assertEqual(7, json.loads(records[0].read_text())["actor_exit_code"])

        second = self._resume()
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual("READY_FOR_AUDIT", json.loads(self.state.read_text())["machine_state"])
        self.assertEqual(1, len(self.marker.read_text(encoding="utf-8").splitlines()))
        self.assertTrue(records[0].is_file())

    def test_failure_after_transition_links_source_operation_and_c41_recovers(self):
        wrapper = self.root / "fail-after-transition.py"
        wrapper.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(self.source)!r})\n"
            "import scripts.o0_runner as runner\n"
            "def fail(*args, **kwargs):\n"
            "    raise OSError('C43_SECRET_IO_ERROR')\n"
            "runner.persist_operation = fail\n"
            f"sys.argv = ['o0_runner', '--config', {str(self.config)!r}]\n"
            "raise SystemExit(runner.main())\n",
            encoding="utf-8",
        )
        failed = subprocess.run(
            [sys.executable, str(wrapper)], cwd=self.source,
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(2, failed.returncode)
        self.assertNotIn("C43_SECRET_IO_ERROR", failed.stderr)
        journal_path = next((self.root / "reports" / "operations").glob("*.journal.json"))
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        self.assertEqual("REPORT_READY", journal["phase"])
        record_path = next((self.root / "reports" / "runner-failures").glob("*.json"))
        evidence = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(journal["operation_id"], evidence["operation_id"])

        resumed = self._resume()
        self.assertEqual(0, resumed.returncode, resumed.stderr)
        self.assertEqual(1, len(self.marker.read_text(encoding="utf-8").splitlines()))
        self.assertTrue(record_path.exists())

    def test_invalid_journal_does_not_attribute_failure_to_next_operation(self):
        self._start_paused_runner("persist_operation")
        journal_path = next((self.root / "reports" / "operations").glob("*.journal.json"))
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["operation_id"] = "op-" + "0" * 64
        journal_path.write_text(json.dumps(journal), encoding="utf-8")

        failed = self._resume()

        self.assertEqual(2, failed.returncode)
        record_path = next((self.root / "reports" / "runner-failures").glob("*.json"))
        evidence = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertIsNone(evidence["operation_id"])

    def test_malformed_journal_persists_failure_evidence_without_secrets_or_state_advance(self):
        self._start_paused_runner("persist_operation")
        journal_path = next((self.root / "reports" / "operations").glob("*.journal.json"))
        secret = "C43_JOURNAL_SECRET_MALFORMED_9c1e"
        journal_path.write_text('{"secret":"' + secret + '",', encoding="utf-8")
        before_state = self.state.read_bytes()
        before_marker = self.marker.read_text(encoding="utf-8") if self.marker.exists() else ""

        failed = self._resume()

        self.assertEqual(2, failed.returncode)
        self.assertNotIn(secret, failed.stdout + failed.stderr)
        self.assertEqual(before_state, self.state.read_bytes())
        if self.marker.exists():
            self.assertEqual(before_marker, self.marker.read_text(encoding="utf-8"))
        record_path = next((self.root / "reports" / "runner-failures").glob("*.json"))
        raw = record_path.read_text(encoding="utf-8")
        self.assertNotIn(secret, raw)
        evidence = json.loads(raw)
        self.assertEqual("INVALID_JSON", evidence["kind"])
        self.assertEqual(2, evidence["runner_exit_code"])
        self.assertIsNone(evidence["actor_exit_code"])
        self.assertIsNone(evidence["operation_id"])
        self.assertEqual(json.loads(before_state)["run_id"], evidence["run_id"])
