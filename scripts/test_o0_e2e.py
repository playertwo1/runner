import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class O0RealE2ETest(unittest.TestCase):
    def test_real_fail_fix_pass_cycle_stops_for_product_authority(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "cycle-evidence.json"
            process = subprocess.run(
                [
                    sys.executable,
                    str(source / "scripts" / "o0_e2e.py"),
                    "--work-root",
                    str(root / "run"),
                    "--output",
                    str(output),
                ],
                cwd=source,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(0, process.returncode, process.stderr)
            evidence = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                ["READY_FOR_AUDIT", "FIX_REQUIRED", "READY_FOR_AUDIT", "WAITING_PRODUCT_AUTHORITY"],
                [step["machine_state"] for step in evidence["steps"]],
            )
            self.assertEqual([0, 1, 1, 2], [step["audit_round"] for step in evidence["steps"]])
            self.assertNotEqual(evidence["initial_sha"], evidence["corrected_sha"])
            self.assertEqual(40, len(evidence["initial_sha"]))
            self.assertEqual(40, len(evidence["corrected_sha"]))
            self.assertEqual(4, len(set(evidence["runner_process_ids"])))
            self.assertEqual(4, len(set(evidence["actor_process_ids"])))
            self.assertNotEqual(evidence["builder_workspace"], evidence["audit_workspace_root"])
            self.assertEqual(
                [evidence["initial_sha"], evidence["corrected_sha"]],
                [workspace["head_sha"] for workspace in evidence["audit_workspaces"]],
            )

            final = evidence["final_state"]
            self.assertEqual("WAITING_PRODUCT_AUTHORITY", final["machine_state"])
            self.assertEqual("PRODUCT_AUTHORITY", final["next_actor"])
            self.assertEqual(2, final["audit_round"])
            self.assertEqual(evidence["corrected_sha"], final["builder_head_sha"])
            self.assertEqual(evidence["corrected_sha"], final["audit_target_sha"])
            self.assertEqual(evidence["corrected_sha"], final["last_audited_sha"])
            self.assertEqual("PASS", final["last_audit_result"])
            self.assertEqual("O0", final["phase"])
            self.assertEqual("S1", final["gate"])
            self.assertIsNone(final["approval"])
            self.assertTrue(final["human_gate_required"])
            self.assertEqual(1, len({step["run_id"] for step in evidence["steps"]}))

            for reference in evidence["evidence_references"]:
                path = root / "run" / reference["path"]
                self.assertTrue(path.is_file())
                self.assertEqual(reference["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

            self.assertEqual(4, len(evidence["canonical_reports"]))
            for recorded in evidence["canonical_reports"]:
                path = root / "run" / recorded["path"]
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(recorded["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
                for item in payload["checks"] + payload.get("findings", []):
                    self.assertEqual({"evidence_id", "sha256"}, set(item["evidence"]))


if __name__ == "__main__":
    unittest.main()
