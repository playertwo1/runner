"""O0-C45: Adversarial E2E cycle covering concurrency, interruption, timeout, and retry."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class O0AdversarialE2ETest(unittest.TestCase):
    def test_committed_evidence_references_are_verifiable(self):
        source = Path(__file__).resolve().parents[1]
        manifest = json.loads((source / "O0_C45_E2E_EVIDENCE.json").read_text(encoding="utf-8"))
        package = source / manifest["package_root"]
        for category in ("canonical_reports", "evidence_references", "failure_references", "journal_references"):
            self.assertTrue(manifest[category], category)
            for reference in manifest[category]:
                relative = Path(reference["path"])
                self.assertFalse(relative.is_absolute())
                self.assertNotIn("..", relative.parts)
                target = package / relative
                self.assertTrue(target.is_file(), str(target))
                tracked = subprocess.run(
                    ["git", "ls-files", "--error-unmatch", "--", str(target.relative_to(source))],
                    cwd=source, capture_output=True, check=False,
                )
                self.assertEqual(0, tracked.returncode, str(target))
                self.assertEqual(reference["sha256"], hashlib.sha256(target.read_bytes()).hexdigest())

    def test_adversarial_e2e_cycle_covers_all_dimensions(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "adversarial-evidence.json"
            work_root = root / "run"
            process = subprocess.run(
                [
                    sys.executable,
                    str(source / "scripts" / "o0_adversarial_e2e.py"),
                    "--work-root",
                    str(work_root),
                    "--output",
                    str(output),
                ],
                cwd=source,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(0, process.returncode, process.stderr)
            self.assertTrue(output.is_file(), "Adversarial evidence file must be created")
            evidence = json.loads(output.read_text(encoding="utf-8"))

            # 1. Coverage verification (every dimension must have explicit assertion)
            coverage = evidence["adversarial_coverage"]
            self.assertTrue(coverage["retry"])
            self.assertTrue(coverage["timeout"])
            self.assertTrue(coverage["interruption"])
            self.assertTrue(coverage["concurrency"])
            self.assertTrue(coverage["duplicate_rejection"])

            # 2. Retry verification
            retry = evidence["retry_evidence"]
            self.assertEqual(2, retry["transient_failure_exit_code"])
            self.assertEqual("ACTOR_EXIT_NONZERO", retry["failure_record_kind"])
            self.assertEqual(7, retry["actor_exit_code"])
            self.assertTrue(retry["state_preserved"])

            # 3. Timeout verification (partial report written before hang discarded before resumption)
            timeout = evidence["timeout_evidence"]
            self.assertEqual(0.25, timeout["timeout_seconds"])
            self.assertEqual(2, timeout["timeout_exit_code"])
            self.assertTrue(timeout["partial_report_discarded"])
            self.assertTrue(timeout["state_preserved"])
            self.assertEqual("TIMEOUT", timeout["interruption_reason"])
            self.assertTrue(timeout["resume_required"])

            # 4. Interruption verification (distinct from timeout, real cancellation and clean recovery)
            interruption = evidence["interruption_evidence"]
            self.assertEqual("CANCELLED", interruption["interruption_type"])
            self.assertEqual(2, interruption["interruption_exit_code"])
            self.assertTrue(interruption["partial_report_discarded"])
            self.assertTrue(interruption["state_preserved_without_advance"])
            self.assertTrue(interruption["cancel_cleared_required"])
            self.assertTrue(interruption["resume_flag_required"])
            self.assertEqual(0, interruption["resumed_success_exit_code"])
            self.assertEqual("READY_FOR_AUDIT", interruption["advanced_state"])

            # 5. Concurrency verification
            concurrency = evidence["concurrency_evidence"]
            self.assertEqual(2, concurrency["concurrent_rejected_exit_code"])
            self.assertIn("Runner state lock is busy", concurrency["concurrent_error_message"])
            self.assertEqual(0, concurrency["primary_completed_exit_code"])
            self.assertEqual("WAITING_PRODUCT_AUTHORITY", concurrency["machine_state_after_primary"])

            # 6. Duplicate rejection verification
            duplicate = evidence["duplicate_evidence"]
            self.assertTrue(duplicate["automation_stopped_preserved"])
            self.assertGreaterEqual(duplicate["accepted_operations_count"], 1)
            self.assertTrue(duplicate["duplicate_report_rejected"])

            # 7. Final state verification
            final = evidence["final_state"]
            self.assertEqual("WAITING_PRODUCT_AUTHORITY", final["machine_state"])
            self.assertEqual("PRODUCT_AUTHORITY", final["next_actor"])
            self.assertIsNone(final["approval"])
            self.assertTrue(final["human_gate_required"])
            self.assertEqual("O0", final["phase"])
            self.assertEqual("S1", final["gate"])

            # 8. Verifiable artifact references
            # 8.1 Canonical reports
            self.assertGreaterEqual(len(evidence["canonical_reports"]), 2)
            for r_ref in evidence["canonical_reports"]:
                target = work_root / r_ref["path"]
                self.assertTrue(target.is_file(), f"Canonical report missing: {r_ref['path']}")
                self.assertEqual(r_ref["sha256"], hashlib.sha256(target.read_bytes()).hexdigest())

            # 8.2 Evidence envelopes
            self.assertGreaterEqual(len(evidence["evidence_references"]), 2)
            for e_ref in evidence["evidence_references"]:
                target = work_root / e_ref["path"]
                self.assertTrue(target.is_file(), f"Evidence envelope missing: {e_ref['path']}")
                self.assertEqual(e_ref["sha256"], hashlib.sha256(target.read_bytes()).hexdigest())

            # 8.3 Runner failure records
            self.assertGreaterEqual(len(evidence["failure_references"]), 3)
            failure_kinds = set()
            for f_ref in evidence["failure_references"]:
                target = work_root / f_ref["path"]
                self.assertTrue(target.is_file(), f"Failure record missing: {f_ref['path']}")
                self.assertEqual(f_ref["sha256"], hashlib.sha256(target.read_bytes()).hexdigest())
                failure_kinds.add(f_ref["kind"])
            self.assertIn("ACTOR_EXIT_NONZERO", failure_kinds)
            self.assertIn("TIMEOUT", failure_kinds)
            self.assertIn("CANCELLED", failure_kinds)

            # 8.4 Interrupted journal snapshots
            self.assertGreaterEqual(len(evidence["journal_references"]), 2)
            journal_reasons = set()
            for j_ref in evidence["journal_references"]:
                target = work_root / j_ref["path"]
                self.assertTrue(target.is_file(), f"Journal snapshot missing: {j_ref['path']}")
                self.assertEqual(j_ref["sha256"], hashlib.sha256(target.read_bytes()).hexdigest())
                self.assertEqual("INTERRUPTED", j_ref["phase"])
                journal_reasons.add(j_ref["interruption_reason"])
            self.assertIn("TIMEOUT", journal_reasons)
            self.assertIn("CANCELLED", journal_reasons)


if __name__ == "__main__":
    unittest.main()
