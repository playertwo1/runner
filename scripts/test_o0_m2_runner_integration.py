"""Unit tests for O0 v2 M2 runner integration, standalone bundle, and evidence artifacts."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.orchestrate_handoffs import validate_with_schema
from scripts.orchestrate_handoffs import HandoffError
from scripts.o0_runner import _terminate_actor_process


class O0V2M2Test(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.evidence_reaudit_path = self.root / "O0_V2_M2_EVIDENCE_REAUDIT.json"
        self.evidence_history_path = self.root / "O0_V2_M2_EVIDENCE.json"

    def test_m2_historical_evidence_preserved(self):
        self.assertTrue(self.evidence_history_path.is_file(), f"Historical evidence missing at {self.evidence_history_path}")
        history = json.loads(self.evidence_history_path.read_text(encoding="utf-8"))
        self.assertEqual("0.1", history.get("schema_version"))
        self.assertEqual("O0-v2-M2-runner-adapters", history.get("scenario"))

    def test_m2_reaudit_evidence_artifact_and_package(self):
        proof_path = self.root / "O0_V2_M2_EVIDENCE_PROCESS_PROOF_FINAL.json"
        self.assertTrue(proof_path.is_file(), f"Missing process proof at {proof_path}")
        evidence = json.loads(proof_path.read_text(encoding="utf-8"))

        self.assertEqual("0.1", evidence.get("schema_version"))
        self.assertEqual("O0-v2-M2-runner-adapters", evidence.get("scenario"))

        criteria = evidence["acceptance_criteria"]
        self.assertTrue(criteria["adapters_connected_to_runner"])
        self.assertTrue(criteria["explicit_workspace_and_task"])
        self.assertTrue(criteria["canonical_builder_report_accepted"])
        self.assertTrue(criteria["canonical_audit_report_accepted"])
        self.assertTrue(criteria["schema_validation_enforced"])
        self.assertTrue(criteria["sha_verification_enforced"])
        self.assertTrue(criteria["evidence_canonicalized_and_referenced"])
        self.assertTrue(criteria["audit_checkout_immutable"])
        self.assertTrue(criteria["no_human_intervention_in_cycle"])
        self.assertTrue(criteria["no_gate_approval_registered"])
        self.assertTrue(criteria["stops_at_waiting_product_authority"])
        self.assertTrue(criteria["self_sufficient_builder_bundle"])
        self.assertTrue(criteria["bundle_clonable_isolated"])
        self.assertTrue(criteria["real_cli_timeout_terminates_child"])
        self.assertTrue(criteria["real_cli_cancellation_terminates_child"])
        self.assertTrue(criteria["no_partial_report_or_state_advance"])

        runner_exec = evidence["runner_execution"]
        builder = runner_exec["builder"]
        auditor = runner_exec["auditor"]
        final_state = runner_exec["final_state"]

        self.assertEqual("antigravity-cli", builder["executor_id"])
        self.assertNotEqual(builder["base_sha"], builder["produced_sha"])

        self.assertEqual("codex-cli", auditor["executor_id"])
        self.assertEqual(builder["produced_sha"], auditor["audited_sha"])
        self.assertEqual("PASS", auditor["audit_result"])
        self.assertEqual([], auditor["findings"])

        self.assertEqual("WAITING_PRODUCT_AUTHORITY", final_state["machine_state"])
        self.assertEqual("PRODUCT_AUTHORITY", final_state["next_actor"])
        self.assertIsNone(final_state["approval"])
        self.assertTrue(final_state["human_gate_required"])

        # Package artifacts verification
        package_root = self.root / evidence["package_root"]
        self.assertTrue(package_root.is_dir(), f"Package dir missing at {package_root}")

        artifacts = evidence["artifacts"]
        for key, entry in artifacts.items():
            if key == "interruptions":
                continue
            if key in {"evidence", "operations"}:
                for item_name, item_meta in entry.items():
                    item_path = package_root / item_meta["path"]
                    self.assertTrue(item_path.is_file(), f"Missing {item_name} at {item_path}")
                    self.assertEqual(item_meta["sha256"], hashlib.sha256(item_path.read_bytes()).hexdigest())
            else:
                art_path = package_root / entry["path"]
                self.assertTrue(art_path.is_file(), f"Missing artifact {key} at {art_path}")
                self.assertEqual(entry["sha256"], hashlib.sha256(art_path.read_bytes()).hexdigest())

        # Validate canonical report schemas
        builder_report = json.loads((package_root / artifacts["builder_report"]["path"]).read_text(encoding="utf-8"))
        validate_with_schema(builder_report, "builder")

        audit_report = json.loads((package_root / artifacts["audit_report"]["path"]).read_text(encoding="utf-8"))
        validate_with_schema(audit_report, "audit")

        state_doc = json.loads((package_root / artifacts["final_state"]["path"]).read_text(encoding="utf-8"))
        validate_with_schema(state_doc, "state")

        # Verify interruption proofs
        proofs = runner_exec["interruption_proofs"]
        for name in ("builder_timeout", "builder_cancel", "auditor_timeout", "auditor_cancel"):
            proof = proofs[name]
            self.assertTrue(proof["observed_tree_pids_before"], name)
            self.assertIn(proof["child_pid"], proof["observed_tree_pids_before"])
            self.assertEqual([], proof["alive_tree_pids_after"])
            self.assertTrue(proof["pass"], f"Proof {name} did not pass: {proof}")
            self.assertEqual(proof["expected_reason"], proof["interrupted_reason"])
            self.assertTrue(proof["all_child_processes_terminated"], f"Child processes not terminated in {name}")
            self.assertFalse(proof["report_accepted"], f"Report was accepted in {name}")
            self.assertTrue(proof["canonical_state_preserved"], f"State not preserved in {name}")
            self.assertTrue(proof["journal_marked_interrupted"], f"Journal not marked interrupted in {name}")
            refs = evidence["artifacts"]["interruptions"][name]
            for key in ("state_before", "state_after", "journal", "absence_proof"):
                ref = refs[key]
                path = package_root / ref["path"]
                self.assertTrue(path.is_file(), str(path))
                self.assertEqual(ref["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            before = json.loads((package_root / refs["state_before"]["path"]).read_text())
            after = json.loads((package_root / refs["state_after"]["path"]).read_text())
            journal = json.loads((package_root / refs["journal"]["path"]).read_text())
            absence = json.loads((package_root / refs["absence_proof"]["path"]).read_text())
            self.assertEqual(before, after)
            self.assertEqual("INTERRUPTED", journal["phase"])
            self.assertEqual(proof["expected_reason"], journal["interruption_reason"])
            self.assertFalse(absence["report_exists"])
            report_dir = package_root / refs["reports_dir"]
            files = sorted(p.relative_to(report_dir).as_posix() for p in report_dir.rglob("*") if p.is_file())
            self.assertEqual(absence["reports_inventory"], files)
            self.assertNotIn(absence["report_path"], files)

    @unittest.skipUnless(sys.platform == "win32", "Windows taskkill contract")
    def test_taskkill_failure_is_not_silently_accepted(self):
        process = Mock(pid=424242)
        with patch("scripts.o0_runner.subprocess.run", return_value=Mock(returncode=1)):
            with self.assertRaises(HandoffError):
                _terminate_actor_process(process)
        process.kill.assert_called_once()

    def test_process_query_failure_cannot_count_as_termination(self):
        from scripts.o0_m2_runner_integration import _get_descendants, _is_pid_alive
        failure = subprocess.CalledProcessError(1, "tasklist")
        with patch("scripts.o0_m2_runner_integration.subprocess.check_output", side_effect=failure):
            with self.assertRaises(subprocess.CalledProcessError):
                _is_pid_alive(424242)
            with self.assertRaises(subprocess.CalledProcessError):
                _get_descendants(424242)

    def test_standalone_builder_bundle(self):
        bundle_path = self.root / "O0_V2_M2_EVIDENCE_REAUDIT_PACKAGE" / "builder.bundle"
        self.assertTrue(bundle_path.is_file(), f"Missing builder.bundle at {bundle_path}")

        # Verify bundle does not require external refs
        res_v = subprocess.run(["git", "bundle", "verify", str(bundle_path)], capture_output=True, text=True, check=True)
        self.assertNotIn("The bundle requires this ref", res_v.stdout)

        # Clone bundle in completely isolated temp directory
        with tempfile.TemporaryDirectory() as td:
            clone_dir = Path(td) / "cloned"
            subprocess.run(["git", "clone", str(bundle_path), str(clone_dir)], capture_output=True, check=True)
            head = subprocess.check_output(["git", "-C", str(clone_dir), "rev-parse", "HEAD"], text=True).strip()

            evidence = json.loads(self.evidence_reaudit_path.read_text(encoding="utf-8"))
            expected_sha = evidence["runner_execution"]["builder"]["produced_sha"]
            self.assertEqual(expected_sha, head)

            # Check unit tests pass in isolated clone
            res_test = subprocess.run([sys.executable, "-m", "unittest", "test_calc.py"], cwd=clone_dir, capture_output=True, text=True)
            self.assertEqual(0, res_test.returncode, f"Tests failed in cloned bundle: {res_test.stderr}")

    def test_adapter_binary_discovery(self):
        from scripts.o0_antigravity_adapter import _find_agy_binary
        from scripts.o0_codex_adapter import _find_codex_binary

        agy_bin = _find_agy_binary()
        self.assertTrue(agy_bin.is_file(), f"Antigravity binary not found: {agy_bin}")

        codex_bin = _find_codex_binary()
        self.assertTrue(codex_bin.is_file(), f"Codex binary not found: {codex_bin}")

        with self.assertRaises(FileNotFoundError):
            _find_agy_binary("nonexistent/agy/binary/path")

        with self.assertRaises(FileNotFoundError):
            _find_codex_binary("nonexistent/codex/binary/path")

    def test_canonical_conversion_for_audit_fail(self):
        sample_fail_report = {
            "schema_version": "0.1",
            "executor_id": "codex-cli",
            "role": "AUDITOR",
            "authority": "INDEPENDENT_AUDIT",
            "audit_result": "FAIL",
            "audited_sha": "0" * 40,
            "summary": "Audit failed due to bug in subtract function",
            "findings": [
                {
                    "id": "CODEX-FINDING-001",
                    "severity": "HIGH",
                    "blocking": True,
                    "files": ["."],
                    "evidence": {
                        "evidence_id": "r1-audit-finding-test",
                        "sha256": "0" * 64,
                    },
                    "problem": "Function returns addition instead of subtraction",
                    "violated_criterion": "Implementation correctness",
                    "resolution_condition": "Fix return value in calc.py",
                }
            ],
            "checks": [
                {
                    "id": "codex-check-1",
                    "status": "FAIL",
                    "evidence": {
                        "evidence_id": "r1-audit-check-test",
                        "sha256": "0" * 64,
                    },
                }
            ],
            "residual_risks": [],
            "gate_registration": "NOT_AUTHORIZED",
        }
        validate_with_schema(sample_fail_report, "audit")


if __name__ == "__main__":
    unittest.main()
