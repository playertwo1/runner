#!/usr/bin/env python3
"""Unit tests for O0 v2 M3 correction loop and runner integration."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from scripts.orchestrate_handoffs import HandoffError, init_state, load_json, status, validate_with_schema
from scripts.o0_runner import run_loop, run_once


class TestO0M3CorrectionLoop(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="test_m3_unit_")
        self.root = Path(self.temp_dir.name)
        self.policy_path = SOURCE_ROOT / "orchestration" / "builder-auditor-policy.json"
        self.state_path = self.root / "orchestrator-state.json"
        self.reports_dir = self.root / "reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.audit_workspaces = self.root / "audit-workspaces"
        self.audit_workspaces.mkdir(parents=True, exist_ok=True)
        self.repo_dir = self.root / "repo"
        self.repo_dir.mkdir(parents=True, exist_ok=True)

        # Setup minimal git repo
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test Builder"], cwd=self.repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=self.repo_dir, check=True, capture_output=True)
        (self.repo_dir / "calc.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=self.repo_dir, check=True, capture_output=True)
        self.base_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo_dir, check=True, capture_output=True, text=True).stdout.strip()

        init_state(
            policy_path=self.policy_path,
            state_path=self.state_path,
            project_id="test-m3-project",
            phase="O0",
            gate="NONE",
            builder_branch="main",
        )
        self.initial_state = status(self.state_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _make_commit(self, filename: str, content: str, msg: str) -> str:
        (self.repo_dir / filename).write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", msg], cwd=self.repo_dir, check=True, capture_output=True)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    def test_run_loop_mocked_cycle_to_waiting_product_authority(self):
        """Verify run_loop completes a 4-step cycle (Builder -> FAIL -> Builder -> PASS)."""
        sha_a = self._make_commit("calc.py", "def multiply(a, b): return a + b\n", "feat: defect")
        sha_b = self._make_commit("calc.py", "def multiply(a, b): return a * b\n", "fix: correct")
        # Reset repo to base_sha initially
        subprocess.run(["git", "checkout", self.base_sha], cwd=self.repo_dir, check=True, capture_output=True)

        actor_script = self.root / "mock_actor.py"
        actor_code = f"""
import json, os, subprocess, sys
from pathlib import Path

report_path = Path(os.environ["IDEAS_STANDARD_REPORT"])
target_sha = os.environ.get("IDEAS_STANDARD_AUDIT_TARGET_SHA")
findings_path = os.environ.get("IDEAS_STANDARD_FINDINGS")

if target_sha is None:
    # BUILDER
    if findings_path is not None:
        subprocess.run(["git", "checkout", "{sha_b}"], check=True, capture_output=True)
        payload = {{
            "schema_version": "0.1",
            "executor_id": "test-builder",
            "role": "BUILDER",
            "authority": "IMPLEMENTATION",
            "result_sha": "{sha_b}",
            "result": "READY_FOR_AUDIT",
            "summary": "Builder fixed the defect",
            "changed_paths": ["calc.py"],
            "checks": [{{"id": "c1", "status": "PASS", "evidence": "Fixed"}}],
            "limitations": [],
            "disputed_findings": [],
            "escalation": None
        }}
    else:
        subprocess.run(["git", "checkout", "{sha_a}"], check=True, capture_output=True)
        payload = {{
            "schema_version": "0.1",
            "executor_id": "test-builder",
            "role": "BUILDER",
            "authority": "IMPLEMENTATION",
            "result_sha": "{sha_a}",
            "result": "READY_FOR_AUDIT",
            "summary": "Builder initial work",
            "changed_paths": ["calc.py"],
            "checks": [{{"id": "c1", "status": "PASS", "evidence": "Initial"}}],
            "limitations": [],
            "disputed_findings": [],
            "escalation": None
        }}
else:
    # AUDITOR
    if target_sha == "{sha_a}":
        payload = {{
            "schema_version": "0.1",
            "executor_id": "test-auditor",
            "role": "AUDITOR",
            "authority": "INDEPENDENT_AUDIT",
            "audit_result": "FAIL",
            "audited_sha": "{sha_a}",
            "summary": "Audit FAIL on SHA A",
            "findings": [{{
                "id": "FINDING-001",
                "severity": "HIGH",
                "blocking": True,
                "files": ["calc.py"],
                "evidence": "Addition instead of multiplication",
                "problem": "multiply function returns a + b",
                "violated_criterion": "Correctness",
                "resolution_condition": "Fix multiply to return a * b"
            }}],
            "checks": [{{"id": "chk1", "status": "FAIL", "evidence": "Failed"}}],
            "residual_risks": [],
            "gate_registration": "NOT_AUTHORIZED"
        }}
    else:
        payload = {{
            "schema_version": "0.1",
            "executor_id": "test-auditor",
            "role": "AUDITOR",
            "authority": "INDEPENDENT_AUDIT",
            "audit_result": "PASS",
            "audited_sha": "{sha_b}",
            "summary": "Audit PASS on SHA B",
            "findings": [],
            "checks": [{{"id": "chk1", "status": "PASS", "evidence": "Passed"}}],
            "residual_risks": [],
            "gate_registration": "NOT_AUTHORIZED"
        }}

report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
"""
        actor_script.write_text(actor_code, encoding="utf-8")

        config = {
            "repository": str(self.repo_dir),
            "state_path": str(self.state_path),
            "reports_dir": str(self.reports_dir),
            "builder_workspace": str(self.repo_dir),
            "audit_workspaces": str(self.audit_workspaces),
            "builder_command": [sys.executable, str(actor_script)],
            "auditor_command": [sys.executable, str(actor_script)],
        }
        config_path = self.root / "runner-config.json"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

        final_state = run_loop(config_path, max_steps=10)

        self.assertEqual("WAITING_PRODUCT_AUTHORITY", final_state["machine_state"])
        self.assertEqual("PRODUCT_AUTHORITY", final_state["next_actor"])
        self.assertEqual("PASS", final_state["last_audit_result"])
        self.assertIsNone(final_state["approval"])
        self.assertTrue(final_state["human_gate_required"])
        self.assertEqual(2, final_state["audit_round"])
        self.assertEqual(sha_b, final_state["audit_target_sha"])
        self.assertEqual(self.initial_state["run_id"], final_state["run_id"])

    def test_run_loop_terminates_at_blocked_on_escalate(self):
        """Verify run_loop terminates at BLOCKED when auditor returns ESCALATE."""
        sha_a = self._make_commit("calc.py", "def test(): pass\n", "feat: test")
        subprocess.run(["git", "checkout", self.base_sha], cwd=self.repo_dir, check=True, capture_output=True)

        actor_script = self.root / "mock_escalate.py"
        actor_code = f"""
import json, os, subprocess
from pathlib import Path
report_path = Path(os.environ["IDEAS_STANDARD_REPORT"])
target_sha = os.environ.get("IDEAS_STANDARD_AUDIT_TARGET_SHA")
if target_sha is None:
    subprocess.run(["git", "checkout", "{sha_a}"], check=True, capture_output=True)
    payload = {{
        "schema_version": "0.1",
        "executor_id": "test-builder",
        "role": "BUILDER",
        "authority": "IMPLEMENTATION",
        "result_sha": "{sha_a}",
        "result": "READY_FOR_AUDIT",
        "summary": "Initial work",
        "changed_paths": ["calc.py"],
        "checks": [{{"id": "c1", "status": "PASS", "evidence": "ev"}}],
        "limitations": [],
        "disputed_findings": [],
        "escalation": None
    }}
else:
    payload = {{
        "schema_version": "0.1",
        "executor_id": "test-auditor",
        "role": "AUDITOR",
        "authority": "INDEPENDENT_AUDIT",
        "audit_result": "ESCALATE",
        "audited_sha": "{sha_a}",
        "summary": "Escalation requested",
        "escalation_reason": "Risk requires human decision",
        "findings": [],
        "checks": [{{"id": "chk1", "status": "PASS", "evidence": "ev"}}],
        "residual_risks": [],
        "gate_registration": "NOT_AUTHORIZED"
    }}
report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
"""
        actor_script.write_text(actor_code, encoding="utf-8")

        config = {
            "repository": str(self.repo_dir),
            "state_path": str(self.state_path),
            "reports_dir": str(self.reports_dir),
            "builder_workspace": str(self.repo_dir),
            "audit_workspaces": str(self.audit_workspaces),
            "builder_command": [sys.executable, str(actor_script)],
            "auditor_command": [sys.executable, str(actor_script)],
        }
        config_path = self.root / "runner-config-escalate.json"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

        final_state = run_loop(config_path, max_steps=10)
        self.assertEqual("BLOCKED", final_state["machine_state"])
        self.assertEqual("AUDITOR_ESCALATED", final_state["blocked_reason"]["code"])
        self.assertEqual("PRODUCT_AUTHORITY", final_state["next_actor"])

    def test_run_loop_terminates_at_blocked_when_max_rounds_reached(self):
        """Verify run_loop terminates at BLOCKED when audit_round reaches max_audit_rounds=3."""
        sha1 = self._make_commit("calc.py", "def a(): pass\n", "feat: 1")
        sha2 = self._make_commit("calc.py", "def b(): pass\n", "feat: 2")
        sha3 = self._make_commit("calc.py", "def c(): pass\n", "feat: 3")
        subprocess.run(["git", "checkout", self.base_sha], cwd=self.repo_dir, check=True, capture_output=True)

        actor_script = self.root / "mock_max_rounds.py"
        actor_code = f"""
import json, os, subprocess
from pathlib import Path
report_path = Path(os.environ["IDEAS_STANDARD_REPORT"])
target_sha = os.environ.get("IDEAS_STANDARD_AUDIT_TARGET_SHA")
findings_path = os.environ.get("IDEAS_STANDARD_FINDINGS")

round_counter_file = Path("{str(self.root / 'round.txt').replace('\\', '/')}")
if not round_counter_file.exists():
    round_num = 0
else:
    round_num = int(round_counter_file.read_text())

shas = ["{sha1}", "{sha2}", "{sha3}"]

if target_sha is None:
    # Builder
    current_sha = shas[min(round_num, len(shas) - 1)]
    subprocess.run(["git", "checkout", current_sha], check=True, capture_output=True)
    payload = {{
        "schema_version": "0.1",
        "executor_id": "test-builder",
        "role": "BUILDER",
        "authority": "IMPLEMENTATION",
        "result_sha": current_sha,
        "result": "READY_FOR_AUDIT",
        "summary": f"Round {{round_num}} attempt",
        "changed_paths": ["calc.py"],
        "checks": [{{"id": "c1", "status": "PASS", "evidence": "ev"}}],
        "limitations": [],
        "disputed_findings": [],
        "escalation": None
    }}
else:
    # Auditor fails every round
    round_counter_file.write_text(str(round_num + 1))
    payload = {{
        "schema_version": "0.1",
        "executor_id": "test-auditor",
        "role": "AUDITOR",
        "authority": "INDEPENDENT_AUDIT",
        "audit_result": "FAIL",
        "audited_sha": target_sha,
        "summary": f"Round {{round_num + 1}} fail",
        "findings": [{{
            "id": f"F{{round_num + 1}}",
            "severity": "HIGH",
            "blocking": True,
            "files": ["."],
            "evidence": "Still failing",
            "problem": "Still failing",
            "violated_criterion": "Criterion",
            "resolution_condition": "Fix"
        }}],
        "checks": [{{"id": "chk1", "status": "FAIL", "evidence": "ev"}}],
        "residual_risks": [],
        "gate_registration": "NOT_AUTHORIZED"
    }}
report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
"""
        actor_script.write_text(actor_code, encoding="utf-8")

        config = {
            "repository": str(self.repo_dir),
            "state_path": str(self.state_path),
            "reports_dir": str(self.reports_dir),
            "builder_workspace": str(self.repo_dir),
            "audit_workspaces": str(self.audit_workspaces),
            "builder_command": [sys.executable, str(actor_script)],
            "auditor_command": [sys.executable, str(actor_script)],
        }
        config_path = self.root / "runner-config-max-rounds.json"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

        final_state = run_loop(config_path, max_steps=10)
        self.assertEqual("BLOCKED", final_state["machine_state"])
        self.assertEqual("AUDIT_ROUND_LIMIT_REACHED", final_state["blocked_reason"]["code"])
        self.assertEqual(3, final_state["audit_round"])

    def test_standalone_bundle_creation_and_verification(self):
        """Verify git bundle create HEAD is self-sufficient without prerequisites."""
        bundle_file = self.root / "test.bundle"
        subprocess.run(["git", "bundle", "create", str(bundle_file), "HEAD"], cwd=self.repo_dir, check=True, capture_output=True)

        verify_proc = subprocess.run(["git", "bundle", "verify", str(bundle_file)], capture_output=True, text=True, check=True)
        output = verify_proc.stdout.strip() or verify_proc.stderr.strip()
        self.assertIn("The bundle records a complete history", output)
        self.assertNotIn("error:", output.lower())

        with tempfile.TemporaryDirectory(prefix="bundle_clone_test_") as clone_dir:
            dest = Path(clone_dir) / "cloned"
            subprocess.run(["git", "clone", str(bundle_file), str(dest)], check=True, capture_output=True)
            cloned_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=dest, check=True, capture_output=True, text=True).stdout.strip()
            self.assertEqual(self.base_sha, cloned_head)


if __name__ == "__main__":
    unittest.main()
