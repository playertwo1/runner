#!/usr/bin/env python3
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class O0M5EvidenceTest(unittest.TestCase):
    def test_committed_package_references_are_complete_and_integral(self):
        evidence = json.loads((ROOT / "O0_V2_M5_EVIDENCE.json").read_text(encoding="utf-8"))
        package = ROOT / "O0_V2_M5_EVIDENCE_PACKAGE"
        self.assertEqual("COMPLETED", evidence["status"])
        self.assertEqual("COMPLETED", evidence["cycle_status"])
        self.assertEqual(2, len(evidence["queue_result"]["executed_tasks"]))
        self.assertEqual(2, evidence["queue_result"]["executed_tasks"][0]["audit_round"])
        self.assertIsNone(evidence["approval"])
        fail = json.loads((package / "reports/tasks/known-defect/audit-report.json.accepted.1").read_text(encoding="utf-8"))
        passed = json.loads((package / "reports/tasks/known-defect/audit-report.json.accepted.2").read_text(encoding="utf-8"))
        self.assertEqual(("FAIL", evidence["sha_a"]), (fail["audit_result"], fail["audited_sha"]))
        self.assertEqual(("PASS", evidence["sha_b"]), (passed["audit_result"], passed["audited_sha"]))
        self.assertNotEqual(evidence["sha_a"], evidence["sha_b"])
        fresh = evidence["interruption_proof"]["fresh_real_cli_resume"]
        self.assertEqual({("BUILDER", "TIMEOUT"), ("AUDITOR", "CANCELLED")},
                         {(item["role"], item["reason"]) for item in fresh})
        for item in fresh:
            self.assertTrue(item["state_preserved_before_resume"])
            self.assertFalse(item["partial_report_accepted"])
            self.assertEqual(1, item["operation_records_after_resume"])
            self.assertTrue(item["all_observed_processes_terminated"])
            folder = package / "fresh-resume" / (
                f"{item['role'].lower()}-{'timeout' if item['reason'] == 'TIMEOUT' else 'cancel'}"
            )
            self.assertEqual((folder / "state-before.json").read_bytes(), (folder / "state-after.json").read_bytes())
            self.assertEqual("INTERRUPTED", json.loads((folder / "interrupted-journal.json").read_text())["phase"])
            self.assertFalse(json.loads((folder / "absence-proof.json").read_text())["report_exists"])
            resumed = json.loads((folder / "resumed-state.json").read_text())
            self.assertIsNone(resumed["approval"])
            self.assertEqual("NONE", resumed["gate"])
            self.assertEqual(0, json.loads((folder / "taskkill-events.json").read_text())[0]["exit_code"])
        referenced = {item["path"] for item in evidence["references"]}
        actual = {path.relative_to(package).as_posix() for path in package.rglob("*") if path.is_file()}
        self.assertEqual(actual, referenced)
        for item in evidence["references"]:
            raw = (package / item["path"]).read_bytes()
            self.assertEqual(item["sha256"], hashlib.sha256(raw).hexdigest())

        with tempfile.TemporaryDirectory() as temporary:
            clone = Path(temporary) / "clone"
            subprocess.run(["git", "clone", str(package / "builder.bundle"), str(clone)], check=True, capture_output=True)
            for sha in (evidence["sha_a"], evidence["sha_b"], evidence["second_task_sha"]):
                subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=clone, check=True, capture_output=True)
            subprocess.run([sys.executable, "-m", "unittest", "test_calc.py"], cwd=clone, check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
