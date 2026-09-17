"""Unit test for O0 v2 M1 CLI validation."""
import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.o0_m1_cli_validation import _prepare_work_root
from scripts.o0_codex_adapter import _build_audit_prompt


class O0V2M1Test(unittest.TestCase):
    def test_reaudit_prompt_keeps_structured_context_without_inline_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True, capture_output=True)
            (repo / "a.txt").write_text("a", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
            base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
            (repo / "a.txt").write_text("b", encoding="utf-8")
            subprocess.run(["git", "commit", "-am", "change"], cwd=repo, check=True, capture_output=True)
            target = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
            payload = {
                "previous_audited_sha": base,
                "new_audit_target_sha": target,
                "audit_round": 1,
                "context_mode": "DELTA",
                "full_context_reasons": [],
                "findings": [{
                    "id": "F-1", "severity": "HIGH", "blocking": True,
                    "files": ["a.txt"], "problem": "bad value",
                    "violated_criterion": "value", "resolution_condition": "fix",
                    "evidence": {"evidence_id": "ev-1", "sha256": "a" * 64},
                }],
                "changed_paths": ["a.txt"],
                "declared_changed_paths": ["a.txt"],
                "reusable_evidence": [{
                    "check_id": "c-1", "status": "FAIL",
                    "evidence": {"evidence_id": "ev-2", "sha256": "b" * 64},
                }],
            }
            prompt = _build_audit_prompt(target, repo, "Check the fix.", payload)
            self.assertIn('"id":"F-1"', prompt)
            self.assertIn('"context_mode":"DELTA"', prompt)
            self.assertIn("a.txt", prompt)
            self.assertNotIn("secret evidence body", prompt)

    def test_existing_execution_is_never_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "accepted"
            root.mkdir()
            marker = root / "builder.bundle"
            marker.write_bytes(b"accepted evidence")
            with self.assertRaises(FileExistsError):
                _prepare_work_root(root)
            self.assertEqual(b"accepted evidence", marker.read_bytes())

    def test_m1_evidence_artifact(self):
        source = Path(__file__).resolve().parents[1]
        evidence_file = source / "O0_V2_M1_EVIDENCE_REAUDIT.json"
        self.assertTrue(evidence_file.is_file(), f"Evidence file {evidence_file} must exist")

        evidence = json.loads(evidence_file.read_text(encoding="utf-8"))
        self.assertEqual("0.1", evidence.get("schema_version"))
        self.assertEqual("O0-v2-M1-cli-validation", evidence.get("scenario"))

        criteria = evidence["acceptance_criteria"]
        self.assertTrue(criteria["version_recorded"])
        self.assertTrue(criteria["authentication_confirmed"])
        self.assertTrue(criteria["non_interactive_execution"])
        self.assertTrue(criteria["exit_codes_verified"])
        self.assertTrue(criteria["explicit_working_directories"])
        self.assertTrue(criteria["builder_file_changed"])
        self.assertTrue(criteria["builder_unit_test_executed"])
        self.assertTrue(criteria["builder_commit_created"])
        self.assertTrue(criteria["builder_verifiable_sha"])
        self.assertTrue(criteria["auditor_separate_checkout"])
        self.assertTrue(criteria["auditor_code_write_protected"])
        self.assertTrue(criteria["auditor_reports_outside_checkout"])
        self.assertTrue(criteria["structured_output_captured"])
        self.assertTrue(criteria["no_interactive_prompts"])

        builder = evidence["builder"]
        self.assertEqual("Antigravity CLI", builder["cli_name"])
        self.assertEqual(0, builder["exit_code"])
        self.assertEqual("SUCCESS", builder["status"])
        self.assertTrue(builder["unit_test_passed"])
        self.assertNotEqual(builder["base_sha"], builder["produced_sha"])

        auditor = evidence["auditor"]
        self.assertEqual("OpenAI Codex CLI", auditor["cli_name"])
        self.assertEqual(0, auditor["exit_code"])
        self.assertTrue(auditor["write_protection_verified"])
        self.assertTrue(auditor["checkout_unmodified"])
        self.assertEqual(builder["produced_sha"], auditor["audited_sha"])
        self.assertTrue(auditor["write_attempt_rejected"])
        self.assertEqual("calc.py", auditor["write_attempt_path"])
        self.assertEqual("PermissionError", auditor["write_attempt_error"])
        self.assertEqual(auditor["write_attempt_sha256_before"], auditor["write_attempt_sha256_after"])

        package = source / evidence["package_root"]
        for name in ("builder_bundle", "auditor_report", "builder_output", "auditor_events"):
            ref = evidence["artifacts"][name]
            path = package / ref["path"]
            self.assertTrue(path.is_file(), str(path))
            tracked = subprocess.run(["git", "ls-files", "--error-unmatch", "--", str(path.relative_to(source))], cwd=source, capture_output=True)
            self.assertEqual(0, tracked.returncode, f"Artifact not committed: {path}")
            self.assertEqual(ref["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

        with tempfile.TemporaryDirectory() as temporary:
            clone = Path(temporary) / "builder"
            subprocess.run(["git", "clone", str(package / evidence["artifacts"]["builder_bundle"]["path"]), str(clone)], check=True, capture_output=True)
            sha = subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            self.assertEqual(builder["produced_sha"], sha)
            result = subprocess.run([sys.executable, "-B", "-m", "unittest", "test_calc"], cwd=clone, check=False, capture_output=True)
            self.assertEqual(0, result.returncode, result.stderr.decode(errors="replace"))

        report = json.loads((package / evidence["artifacts"]["auditor_report"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(builder["produced_sha"], report["audited_sha"])
        self.assertIn(report["audit_result"], ("PASS", "FAIL"))
        self.assertTrue(report["summary"].strip())
        self.assertIsInstance(report["findings"], list)
        self.assertTrue(report["checks"])


if __name__ == "__main__":
    unittest.main()
