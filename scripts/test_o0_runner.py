import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import o0_runner as runner_module
from scripts.orchestrate_handoffs import init_state
from scripts.o0_runner import (
    HandoffError,
    audit_handoff,
    builder_handoff,
    load_config,
    next_actor,
    prepare_audit_workspace,
    prepare_builder_findings,
    run_actor,
    run_once,
    validate_auditor_boundaries,
    validate_builder_result,
    validate_workspaces,
    verify_audit_after,
)


class O0RunnerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_audit_snapshot_reuse_after_interruption_is_immutable(self):
        state = self.root / "state.json"
        state.write_bytes(b'{"run_id":"same"}')
        audit_root = self.root / "audits"
        first = runner_module.write_state_snapshot(state, audit_root, "a" * 40)
        self.assertEqual(first, runner_module.write_state_snapshot(state, audit_root, "a" * 40))
        self.assertEqual(b'{"run_id":"same"}', first.read_bytes())
        state.write_bytes(b'{"run_id":"changed"}')
        with self.assertRaises(HandoffError):
            runner_module.write_state_snapshot(state, audit_root, "a" * 40)
        self.assertEqual(b'{"run_id":"same"}', first.read_bytes())

    def _run_runner_cli(self, config: Path):
        return subprocess.run(
            [sys.executable, "-m", "scripts.o0_runner", "--config", str(config)],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_config_requires_actor_commands(self):
        path = self.root / "config.json"
        path.write_text("{}", encoding="utf-8")
        with self.assertRaises(HandoffError):
            load_config(path)

    def test_config_rejects_non_finite_lock_timeout(self):
        path = self.root / "config.json"
        payload = {
            "repository": "repo",
            "state_path": "state.json",
            "reports_dir": "reports",
            "builder_workspace": "builder",
            "audit_workspaces": "audits",
            "builder_command": ["builder"],
            "auditor_command": ["auditor"],
        }
        for timeout in (float("nan"), float("inf")):
            payload["lock_timeout_seconds"] = timeout
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(HandoffError, "non-negative finite"):
                load_config(path)

    def test_malformed_config_is_rejected_deterministically(self):
        config = self.root / "malformed-config.json"
        config.write_text("{", encoding="utf-8")

        attempts = [self._run_runner_cli(config) for _ in range(2)]

        self.assertEqual([2, 2], [attempt.returncode for attempt in attempts])
        self.assertEqual(attempts[0].stderr, attempts[1].stderr)
        self.assertTrue(attempts[0].stderr.startswith("RUNNER ERROR:"))

    def test_malformed_state_rejects_before_actor_without_mutation(self):
        (self.root / "repo").mkdir()
        (self.root / "builder").mkdir()
        state = self.root / "state.json"
        state.write_text("{", encoding="utf-8")
        before = state.read_bytes()
        marker = self.root / "actor-ran"
        actor = [
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).touch()",
        ]
        config = self.root / "runner.json"
        config.write_text(json.dumps({
            "repository": "repo",
            "state_path": "state.json",
            "reports_dir": "reports",
            "builder_workspace": "builder",
            "audit_workspaces": "audits",
            "builder_command": actor,
            "auditor_command": actor,
        }), encoding="utf-8")

        attempts = [self._run_runner_cli(config) for _ in range(2)]

        self.assertEqual([2, 2], [attempt.returncode for attempt in attempts])
        self.assertEqual(attempts[0].stderr, attempts[1].stderr)
        self.assertTrue(attempts[0].stderr.startswith("RUNNER ERROR:"))
        self.assertFalse(marker.exists())
        self.assertEqual(before, state.read_bytes())

    def test_restart_recovers_persisted_state_without_process_memory(self):
        repository = self.root / "repo"
        repository.mkdir()
        builder_workspace = self.root / "builder"
        builder_workspace.mkdir()
        policy_path = self.root / "policy.json"
        state_path = self.root / "state.json"
        report_path = self.root / "reports" / "builder-report.json"
        config_path = self.root / "runner.json"
        policy_path.write_text(json.dumps({
            "schema_version": "0.1",
            "id": "builder-auditor-loop",
            "builder_role_id": "builder",
            "auditor_role_id": "auditor",
            "product_authority_id": "owner",
            "max_audit_rounds": 3,
            "immutable_audit_target": True,
            "auditor_write_access": False,
            "human_gate_required": True,
            "auto_advance_after_audit": False,
            "persist_handoffs": True,
        }), encoding="utf-8")
        init_state(
            policy_path,
            state_path,
            project_id="sample",
            phase="O0",
            gate="S1",
            builder_branch="builder/o0-c23",
        )
        report_path.parent.mkdir()
        report_path.write_text(json.dumps({
            "schema_version": "0.1",
            "executor_id": "builder-executor",
            "role": "BUILDER",
            "authority": "IMPLEMENTATION",
            "result_sha": "a" * 40,
            "result": "DISPUTED",
            "summary": "Finding disputed.",
            "changed_paths": [],
            "checks": [],
            "limitations": [],
            "disputed_findings": ["AUD-001"],
            "escalation": None,
        }), encoding="utf-8")
        builder_handoff(state_path, report_path)
        persisted = state_path.read_bytes()
        run_id = json.loads(persisted).get("run_id")
        self.assertIsNotNone(run_id)
        config_path.write_text(json.dumps({
            "repository": "repo",
            "state_path": "state.json",
            "reports_dir": "reports",
            "builder_workspace": "builder",
            "audit_workspaces": "audits",
            "builder_command": ["must-not-run"],
            "auditor_command": ["must-not-run"],
        }), encoding="utf-8")

        restarted = self._run_runner_cli(config_path)

        self.assertEqual(0, restarted.returncode, restarted.stderr)
        recovered = json.loads(restarted.stdout)
        self.assertEqual("BLOCKED", recovered["machine_state"])
        self.assertEqual("PRODUCT_AUTHORITY", recovered["next_actor"])
        self.assertEqual(run_id, recovered["run_id"])
        self.assertEqual(persisted, state_path.read_bytes())

    def test_builder_and_auditor_workspaces_must_be_separate(self):
        with self.assertRaises(HandoffError):
            validate_workspaces(self.root / "work", self.root / "work" / "audit")

    def test_report_write_scope_cannot_include_state_or_audit_workspace(self):
        with self.assertRaises(HandoffError):
            validate_auditor_boundaries(
                self.root / "state.json", self.root / "audits", self.root
            )

    def test_run_actor_starts_configured_command_and_requires_report(self):
        report = self.root / "reports" / "builder.json"

        def complete(command, **kwargs):
            report.write_text(json.dumps({"ok": True}), encoding="utf-8")
            return type("Result", (), {"returncode": 0})()

        with patch("scripts.o0_runner.subprocess.run", side_effect=complete) as mocked:
            run_actor(["provider", "run"], self.root, report, {"X": "1"})
        self.assertEqual(["provider", "run"], mocked.call_args.args[0])
        self.assertTrue(report.is_file())

    def _repository(self):
        repository = self.root / "repo"
        repository.mkdir()
        subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
        (repository / "file.txt").write_text("one", encoding="utf-8")
        subprocess.run(["git", "add", "file.txt"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-m", "one"], cwd=repository, check=True, capture_output=True)
        first = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True
        ).stdout.strip()
        (repository / "file.txt").write_text("two", encoding="utf-8")
        subprocess.run(["git", "commit", "-am", "two"], cwd=repository, check=True, capture_output=True)
        second = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True
        ).stdout.strip()
        return repository, first, second

    def _audit_runner_inputs(self, audit_target: str, builder_head: str):
        state = self.root / "state.json"
        state.write_text(json.dumps({
            "schema_version": "0.1",
            "run_id": "run-" + "0" * 64,
            "project_id": "sample",
            "phase": "O0",
            "gate": "NONE",
            "machine_state": "READY_FOR_AUDIT",
            "next_actor": "AUDITOR",
            "blocked_reason": None,
            "builder_branch": "work",
            "builder_executor_id": "builder-executor",
            "auditor_executor_id": None,
            "product_authority_id": "owner",
            "builder_head_sha": builder_head,
            "audit_target_sha": audit_target,
            "last_audited_sha": None,
            "audit_round": 0,
            "max_audit_rounds": 3,
            "last_builder_report": None,
            "last_audit_report": None,
            "last_audit_result": None,
            "human_gate_required": True,
            "approval": None,
            "updated_at": "now",
            "message": "",
        }), encoding="utf-8")
        (self.root / "builder").mkdir(exist_ok=True)
        marker = self.root / "auditor-ran"
        actor = [
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).touch()",
        ]
        config = self.root / "runner.json"
        config.write_text(json.dumps({
            "repository": "repo",
            "state_path": "state.json",
            "reports_dir": "reports",
            "builder_workspace": "builder",
            "audit_workspaces": "audits",
            "builder_command": ["must-not-run"],
            "auditor_command": actor,
        }), encoding="utf-8")
        return state, config, marker

    def _pass_audit_report(self, audited_sha: str):
        return {
            "schema_version": "0.1",
            "executor_id": "auditor-executor",
            "role": "AUDITOR",
            "authority": "INDEPENDENT_AUDIT",
            "audit_result": "PASS",
            "audited_sha": audited_sha,
            "summary": "Audit completed.",
            "findings": [],
            "checks": [{"id": "o0-c26", "status": "PASS", "evidence": "SHA matched"}],
            "residual_risks": [],
            "gate_registration": "NOT_AUTHORIZED",
        }

    def test_runner_rejects_checkout_divergent_from_frozen_sha_before_auditor(self):
        repository, wrong_sha, target = self._repository()
        state, config, marker = self._audit_runner_inputs(target, target)
        workspace = self.root / "audits" / target
        workspace.parent.mkdir()
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(workspace), wrong_sha],
            cwd=repository,
            check=True,
            capture_output=True,
        )
        before = state.read_bytes()

        result = self._run_runner_cli(config)

        self.assertEqual(2, result.returncode)
        self.assertIn("does not match audit_target_sha", result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(before, state.read_bytes())

    def test_runner_rejects_checkout_changed_during_audit_without_state_advance(self):
        repository, wrong_sha, target = self._repository()
        state, config, _ = self._audit_runner_inputs(target, target)
        before = state.read_bytes()

        def change_checkout(command, workspace, report, env, *, write_sandbox=False):
            subprocess.run(
                ["git", "checkout", "--detach", wrong_sha],
                cwd=workspace,
                check=True,
                capture_output=True,
            )
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(json.dumps(self._pass_audit_report(target)), encoding="utf-8")

        with (
            patch("scripts.o0_runner.prepare_audit_workspace", return_value=repository),
            patch("scripts.o0_runner.run_actor", side_effect=change_checkout),
        ):
            with self.assertRaisesRegex(HandoffError, "HEAD changed"):
                run_once(config)

        self.assertEqual(before, state.read_bytes())
        unchanged = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual("READY_FOR_AUDIT", unchanged["machine_state"])
        self.assertEqual("O0", unchanged["phase"])
        self.assertEqual("NONE", unchanged["gate"])
        self.assertIsNone(unchanged["approval"])

    def test_runner_rejects_report_sha_divergent_from_frozen_sha_without_state_advance(self):
        repository, wrong_sha, target = self._repository()
        state, config, _ = self._audit_runner_inputs(target, target)
        before = state.read_bytes()

        def write_divergent_report(command, workspace, report, env, *, write_sandbox=False):
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(json.dumps(self._pass_audit_report(wrong_sha)), encoding="utf-8")

        with (
            patch("scripts.o0_runner.prepare_audit_workspace", return_value=repository),
            patch("scripts.o0_runner.run_actor", side_effect=write_divergent_report),
        ):
            with self.assertRaisesRegex(HandoffError, "Audit SHA mismatch"):
                run_once(config)

        self.assertEqual(before, state.read_bytes())
        unchanged = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual("READY_FOR_AUDIT", unchanged["machine_state"])
        self.assertEqual("O0", unchanged["phase"])
        self.assertEqual("NONE", unchanged["gate"])
        self.assertIsNone(unchanged["approval"])

    def test_runner_rejects_pass_with_blocking_finding_without_state_advance(self):
        repository, _, target = self._repository()
        state, config, _ = self._audit_runner_inputs(target, target)
        before = state.read_bytes()

        def write_invalid_pass(command, workspace, report, env, *, write_sandbox=False):
            payload = self._pass_audit_report(target)
            payload["findings"] = [{
                "id": "O0-C27-001",
                "severity": "HIGH",
                "blocking": True,
                "files": ["scripts/o0_runner.py"],
                "evidence": "Blocking finding conflicts with PASS.",
                "problem": "PASS cannot accept a blocking finding.",
                "violated_criterion": "O0-C27",
                "resolution_condition": "Reject the audit result.",
            }]
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(json.dumps(payload), encoding="utf-8")

        with (
            patch("scripts.o0_runner.prepare_audit_workspace", return_value=repository),
            patch("scripts.o0_runner.run_actor", side_effect=write_invalid_pass),
        ):
            with self.assertRaisesRegex(HandoffError, "PASS cannot contain blocking findings"):
                run_once(config)

        self.assertEqual(before, state.read_bytes())
        unchanged = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual("READY_FOR_AUDIT", unchanged["machine_state"])
        self.assertEqual(0, unchanged["audit_round"])
        self.assertIsNone(unchanged["last_audit_result"])
        self.assertIsNone(unchanged["last_audited_sha"])
        self.assertEqual("O0", unchanged["phase"])
        self.assertEqual("NONE", unchanged["gate"])
        self.assertIsNone(unchanged["approval"])

    def test_terminal_states_do_not_run_agents_or_advance_phase(self):
        repository, _, target = self._repository()
        cases = {
            "WAITING_PRODUCT_AUTHORITY": {
                "last_audit_result": "PASS",
                "last_audited_sha": target,
                "approval": None,
                "next_actor": "PRODUCT_AUTHORITY",
                "blocked_reason": None,
            },
            "BLOCKED": {
                "last_audit_result": "ESCALATE",
                "last_audited_sha": target,
                "approval": None,
                "next_actor": "PRODUCT_AUTHORITY",
                "blocked_reason": {"code": "AUDITOR_ESCALATED", "source": "AUDITOR", "evidence_ref": "last_audit_report"},
            },
            "GATE_APPROVED": {
                "last_audit_result": "PASS",
                "last_audited_sha": target,
                "approval": {
                    "executor_id": "owner",
                    "role": "PRODUCT_AUTHORITY",
                    "authority": "GATE_APPROVAL",
                    "gate": "S1",
                    "audited_sha": target,
                    "approved_at": "now",
                },
                "next_actor": "STOP",
                "blocked_reason": None,
            },
        }

        for machine_state, expected in cases.items():
            with self.subTest(machine_state=machine_state):
                state, config, marker = self._audit_runner_inputs(target, target)
                payload = json.loads(state.read_text(encoding="utf-8"))
                payload.update({
                    "phase": "O0",
                    "gate": "S1",
                    "machine_state": machine_state,
                    "next_actor": expected["next_actor"],
                    "blocked_reason": expected["blocked_reason"],
                    "last_audit_result": expected["last_audit_result"],
                    "last_audited_sha": expected["last_audited_sha"],
                    "approval": expected["approval"],
                })
                state.write_text(json.dumps(payload), encoding="utf-8")
                before = state.read_bytes()

                result = self._run_runner_cli(config)

                self.assertEqual(0, result.returncode, result.stderr)
                self.assertFalse(marker.exists())
                self.assertEqual(before, state.read_bytes())
                observed = json.loads(result.stdout)
                self.assertEqual("O0", observed["phase"])
                self.assertEqual("S1", observed["gate"])
                self.assertEqual(machine_state, observed["machine_state"])
                self.assertEqual(expected["next_actor"], observed["next_actor"])

    def test_runner_rejects_malformed_audit_sha_before_agent_or_state_mutation(self):
        _, _, head = self._repository()
        state, config, marker = self._audit_runner_inputs("not-a-sha", head)
        before = state.read_bytes()

        result = self._run_runner_cli(config)

        self.assertEqual(2, result.returncode)
        self.assertIn("state schema validation failed", result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(before, state.read_bytes())

    def test_runner_rejects_nonexistent_audit_sha_before_agent_or_state_mutation(self):
        self._repository()
        state, config, marker = self._audit_runner_inputs("f" * 40, "f" * 40)
        before = state.read_bytes()

        result = self._run_runner_cli(config)

        self.assertEqual(2, result.returncode)
        self.assertIn("Unknown result SHA", result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(before, state.read_bytes())

    def test_runner_rejects_non_commit_audit_sha_before_agent_or_state_mutation(self):
        repository, _, _ = self._repository()
        blob = subprocess.run(
            ["git", "rev-parse", "HEAD:file.txt"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        state, config, marker = self._audit_runner_inputs(blob, blob)
        before = state.read_bytes()

        result = self._run_runner_cli(config)

        self.assertEqual(2, result.returncode)
        self.assertIn("Unknown result SHA", result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(before, state.read_bytes())

    def test_auditor_cannot_restore_write_permission_or_write_target(self):
        repository, _, target = self._repository()
        workspace = prepare_audit_workspace(repository, self.root / "audits", target)
        report = self.root / "reports" / "audit.json"
        code = (
            "import json,os,pathlib;"
            "target=pathlib.Path('file.txt');blocked=False;"
            "\ntry:\n target.chmod(0o644);target.write_text('tampered')"
            "\nexcept PermissionError:\n blocked=True"
            "\npathlib.Path(os.environ['IDEAS_STANDARD_REPORT']).write_text(json.dumps({'blocked':blocked}))"
        )
        run_actor([sys.executable, "-c", code], workspace, report, {}, write_sandbox=True)
        self.assertTrue(json.loads(report.read_text())["blocked"])
        self.assertEqual("two", (workspace / "file.txt").read_text())

    def test_auditor_cannot_write_canonical_state(self):
        state = self.root / "state.json"
        state.write_text('{"audit_target_sha":"fixed"}', encoding="utf-8")
        workspace = self.root / "workspace"
        workspace.mkdir()
        report = self.root / "reports" / "audit.json"
        code = (
            "import json,os,pathlib;"
            "state=pathlib.Path(os.environ['CANONICAL']);blocked=False;"
            "\ntry:\n state.chmod(0o644);state.write_text('{}')"
            "\nexcept PermissionError:\n blocked=True"
            "\npathlib.Path(os.environ['IDEAS_STANDARD_REPORT']).write_text(json.dumps({'blocked':blocked}))"
        )
        run_actor(
            [sys.executable, "-c", code],
            workspace,
            report,
            {"CANONICAL": str(state)},
            write_sandbox=True,
        )
        self.assertTrue(json.loads(report.read_text())["blocked"])
        self.assertEqual('{"audit_target_sha":"fixed"}', state.read_text())

    def test_auditor_failsafe_when_sandbox_unavailable(self):
        repository, _, target = self._repository()
        workspace = prepare_audit_workspace(repository, self.root / "audits", target)
        report = self.root / "reports" / "audit.json"
        if os.name == "nt":
            with patch("shutil.which", return_value=None):
                with self.assertRaisesRegex(HandoffError, "Auditor write sandbox is unavailable"):
                    run_actor([sys.executable, "-c", "pass"], workspace, report, {}, write_sandbox=True)

    @unittest.skipUnless(os.name == "nt", "Windows ACL semantics")
    def test_windows_sandbox_blocks_existing_child_without_inheritance(self):
        workspace = self.root / "acl-workspace"
        child = workspace / "child"
        child.mkdir(parents=True)
        target = child / "file.txt"
        target.write_text("before", encoding="utf-8")
        subprocess.run(["icacls", str(child), "/inheritance:d"], check=True, capture_output=True)
        report = self.root / "reports" / "audit.json"

        with runner_module._windows_auditor_write_sandbox(workspace, report, {}):
            with self.assertRaises(PermissionError):
                target.write_text("blocked", encoding="utf-8")

        target.write_text("after", encoding="utf-8")

    def test_state_change_during_audit_is_rejected(self):
        repository, _, target = self._repository()
        workspace = prepare_audit_workspace(repository, self.root / "audits", target)
        state = self.root / "state.json"
        state.write_text('{"audit_target_sha":"before"}', encoding="utf-8")
        before = state.read_bytes()
        state.write_text('{"audit_target_sha":"after"}', encoding="utf-8")
        with self.assertRaisesRegex(HandoffError, "Canonical state changed"):
            verify_audit_after(workspace, state, target, before)

    def test_clean_checkout_at_wrong_head_is_rejected(self):
        repository, first, target = self._repository()
        workspace = prepare_audit_workspace(repository, self.root / "audits", target)
        for path in [workspace, *workspace.rglob("*")]:
            path.chmod(path.stat().st_mode | 0o200)
        subprocess.run(["git", "checkout", "--detach", first], cwd=workspace, check=True, capture_output=True)
        state = self.root / "state.json"
        state.write_text(
            json.dumps({
                "schema_version": "0.1",
                "run_id": "run-" + "0" * 64,
                "project_id": "sample",
                "phase": "O0",
                "gate": "NONE",
                "machine_state": "READY_FOR_AUDIT",
                "next_actor": "AUDITOR",
                "blocked_reason": None,
                "builder_branch": "work",
                "builder_executor_id": "builder",
                "auditor_executor_id": None,
                "product_authority_id": "owner",
                "builder_head_sha": target,
                "audit_target_sha": target,
                "last_audited_sha": None,
                "audit_round": 0,
                "max_audit_rounds": 3,
                "last_builder_report": None,
                "last_audit_report": None,
                "last_audit_result": None,
                "human_gate_required": True,
                "approval": None,
                "updated_at": "now",
                "message": ""
            }),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(HandoffError, "HEAD changed"):
            verify_audit_after(workspace, state, target, state.read_bytes())

    def test_runner_audit_fail_transitions_to_fix_required(self):
        target = "a" * 40
        state = self.root / "state.json"
        state.write_text(
            json.dumps({
                "schema_version": "0.1",
                "run_id": "run-" + "0" * 64,
                "project_id": "sample",
                "phase": "O0",
                "gate": "NONE",
                "machine_state": "READY_FOR_AUDIT",
                "next_actor": "AUDITOR",
                "blocked_reason": None,
                "builder_branch": "work",
                "builder_executor_id": "builder-executor",
                "auditor_executor_id": None,
                "product_authority_id": "owner",
                "builder_head_sha": target,
                "audit_target_sha": target,
                "last_audited_sha": None,
                "audit_round": 0,
                "max_audit_rounds": 3,
                "last_builder_report": None,
                "last_audit_report": None,
                "last_audit_result": None,
                "human_gate_required": True,
                "approval": None,
                "updated_at": "now",
                "message": ""
            }),
            encoding="utf-8",
        )
        (self.root / "builder").mkdir()
        config = self.root / "runner.json"
        config.write_text(
            json.dumps({
                "repository": "repo",
                "state_path": "state.json",
                "reports_dir": "reports",
                "builder_workspace": "builder",
                "audit_workspaces": "audits",
                "builder_command": ["builder"],
                "auditor_command": ["auditor"],
            }),
            encoding="utf-8",
        )

        def fail_audit(command, workspace, report, env, *, write_sandbox=False):
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(
                json.dumps({
                    "schema_version": "0.1",
                    "executor_id": "auditor-executor",
                    "role": "AUDITOR",
                    "authority": "INDEPENDENT_AUDIT",
                    "audit_result": "FAIL",
                    "audited_sha": target,
                    "summary": "Blocking finding found.",
                    "findings": [{
                        "id": "O0-TEST-001",
                        "severity": "HIGH",
                        "blocking": True,
                        "files": ["scripts/o0_runner.py"],
                        "evidence": "failure path",
                        "problem": "adversarial failure",
                        "violated_criterion": "O0-C14",
                        "resolution_condition": "return FIX_REQUIRED",
                    }],
                    "checks": [{"id": "o0-c14", "status": "FAIL", "evidence": "finding"}],
                    "residual_risks": [],
                    "gate_registration": "NOT_AUTHORIZED",
                }),
                encoding="utf-8",
            )

        workspace = self.root / "audit-workspace"
        workspace.mkdir()
        snapshot = self.root / "snapshot.json"
        with (
            patch("scripts.o0_runner.prepare_audit_workspace", return_value=workspace),
            patch("scripts.o0_runner.write_state_snapshot", return_value=snapshot),
            patch("scripts.o0_runner.verify_audit_after"),
            patch("scripts.o0_runner.run_actor", side_effect=fail_audit),
        ):
            result = run_once(config)

        self.assertEqual("FIX_REQUIRED", result["machine_state"])
        self.assertEqual("FAIL", result["last_audit_result"])
        self.assertEqual(1, result["audit_round"])

    def test_runner_audit_pass_waits_for_product_authority(self):
        target = "a" * 40
        state = self.root / "pass-state.json"
        state.write_text(
            json.dumps({
                "schema_version": "0.1",
                "run_id": "run-" + "0" * 64,
                "project_id": "sample",
                "phase": "O0",
                "gate": "NONE",
                "machine_state": "READY_FOR_AUDIT",
                "next_actor": "AUDITOR",
                "blocked_reason": None,
                "builder_branch": "work",
                "builder_executor_id": "builder-executor",
                "auditor_executor_id": None,
                "product_authority_id": "owner",
                "builder_head_sha": target,
                "audit_target_sha": target,
                "last_audited_sha": None,
                "audit_round": 0,
                "max_audit_rounds": 3,
                "last_builder_report": None,
                "last_audit_report": None,
                "last_audit_result": None,
                "human_gate_required": True,
                "approval": None,
                "updated_at": "now",
                "message": "",
            }),
            encoding="utf-8",
        )
        (self.root / "builder").mkdir()
        config = self.root / "pass-runner.json"
        config.write_text(
            json.dumps({
                "repository": "repo",
                "state_path": "pass-state.json",
                "reports_dir": "pass-reports",
                "builder_workspace": "builder",
                "audit_workspaces": "audits",
                "builder_command": ["builder"],
                "auditor_command": ["auditor"],
            }),
            encoding="utf-8",
        )

        def pass_audit(command, workspace, report, env, *, write_sandbox=False):
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(
                json.dumps({
                    "schema_version": "0.1",
                    "executor_id": "auditor-executor",
                    "role": "AUDITOR",
                    "authority": "INDEPENDENT_AUDIT",
                    "audit_result": "PASS",
                    "audited_sha": target,
                    "summary": "O0-C18 verified.",
                    "findings": [],
                    "checks": [{"id": "o0-c18", "status": "PASS", "evidence": "verified"}],
                    "residual_risks": [],
                    "gate_registration": "NOT_AUTHORIZED",
                }),
                encoding="utf-8",
            )

        workspace = self.root / "pass-audit-workspace"
        workspace.mkdir()
        snapshot = self.root / "pass-snapshot.json"
        with (
            patch("scripts.o0_runner.prepare_audit_workspace", return_value=workspace),
            patch("scripts.o0_runner.write_state_snapshot", return_value=snapshot),
            patch("scripts.o0_runner.verify_audit_after"),
            patch("scripts.o0_runner.run_actor", side_effect=pass_audit),
        ):
            result = run_once(config)

        self.assertEqual(target, result["last_audited_sha"])
        self.assertEqual("PASS", result["last_audit_result"])
        self.assertEqual("WAITING_PRODUCT_AUTHORITY", result["machine_state"])
        self.assertEqual("PRODUCT_AUTHORITY", next_actor(result))
        self.assertEqual("NONE", result["gate"])
        self.assertIsNone(result["approval"])
        self.assertEqual("O0", result["phase"])

    def test_runner_audit_escalate_blocks_for_product_authority(self):
        target = "b" * 40
        state = self.root / "escalate-state.json"
        state.write_text(
            json.dumps({
                "schema_version": "0.1",
                "run_id": "run-" + "0" * 64,
                "project_id": "sample",
                "phase": "O0",
                "gate": "NONE",
                "machine_state": "READY_FOR_AUDIT",
                "next_actor": "AUDITOR",
                "blocked_reason": None,
                "builder_branch": "work",
                "builder_executor_id": "builder-executor",
                "auditor_executor_id": None,
                "product_authority_id": "owner",
                "builder_head_sha": target,
                "audit_target_sha": target,
                "last_audited_sha": None,
                "audit_round": 0,
                "max_audit_rounds": 3,
                "last_builder_report": None,
                "last_audit_report": None,
                "last_audit_result": None,
                "human_gate_required": True,
                "approval": None,
                "updated_at": "now",
                "message": "",
            }),
            encoding="utf-8",
        )
        (self.root / "builder").mkdir()
        config = self.root / "escalate-runner.json"
        config.write_text(
            json.dumps({
                "repository": "repo",
                "state_path": "escalate-state.json",
                "reports_dir": "escalate-reports",
                "builder_workspace": "builder",
                "audit_workspaces": "audits",
                "builder_command": ["builder"],
                "auditor_command": ["auditor"],
            }),
            encoding="utf-8",
        )

        def escalate_audit(command, workspace, report, env, *, write_sandbox=False):
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(
                json.dumps({
                    "schema_version": "0.1",
                    "executor_id": "auditor-executor",
                    "role": "AUDITOR",
                    "authority": "INDEPENDENT_AUDIT",
                    "audit_result": "ESCALATE",
                    "audited_sha": target,
                    "summary": "Material decision requires Product Authority.",
                    "escalation_reason": "Canonical conflict requires a Product Authority decision.",
                    "findings": [],
                    "checks": [{
                        "id": "o0-c19",
                        "status": "NOT_APPLICABLE",
                        "evidence": "human decision required",
                    }],
                    "residual_risks": ["material decision unresolved"],
                    "gate_registration": "NOT_AUTHORIZED",
                }),
                encoding="utf-8",
            )

        workspace = self.root / "escalate-audit-workspace"
        workspace.mkdir()
        snapshot = self.root / "escalate-snapshot.json"
        with (
            patch("scripts.o0_runner.prepare_audit_workspace", return_value=workspace),
            patch("scripts.o0_runner.write_state_snapshot", return_value=snapshot),
            patch("scripts.o0_runner.verify_audit_after"),
            patch("scripts.o0_runner.run_actor", side_effect=escalate_audit),
        ):
            result = run_once(config)

        evidence = Path(result["last_audit_report"])
        self.assertEqual(target, result["audit_target_sha"])
        self.assertEqual(target, result["last_audited_sha"])
        self.assertEqual("ESCALATE", result["last_audit_result"])
        self.assertEqual("BLOCKED", result["machine_state"])
        self.assertEqual("PRODUCT_AUTHORITY", next_actor(result))
        self.assertIn("escalated", result["message"].lower())
        preserved_report = json.loads(evidence.read_text())
        self.assertEqual("Canonical conflict requires a Product Authority decision.", preserved_report["escalation_reason"])
        self.assertEqual(
            "human decision required",
            runner_module.resolve_evidence(
                evidence.parent / "evidence",
                preserved_report["checks"][0]["evidence"],
                result,
            )["content"],
        )
        self.assertEqual("material decision unresolved", preserved_report["residual_risks"][0])
        self.assertEqual(str(evidence), result["last_audit_report"])
        self.assertEqual("NONE", result["gate"])
        self.assertIsNone(result["approval"])
        self.assertEqual("O0", result["phase"])

    def test_audit_escalate_rejects_missing_or_blank_reason_without_state_change(self):
        target = "c" * 40
        base_state = {
            "schema_version": "0.1",
            "run_id": "run-" + "0" * 64,
            "project_id": "sample",
            "phase": "O0",
            "gate": "NONE",
            "machine_state": "READY_FOR_AUDIT",
            "next_actor": "AUDITOR",
            "blocked_reason": None,
            "builder_branch": "work",
            "builder_executor_id": "builder-executor",
            "auditor_executor_id": None,
            "product_authority_id": "owner",
            "builder_head_sha": target,
            "audit_target_sha": target,
            "last_audited_sha": None,
            "audit_round": 0,
            "max_audit_rounds": 3,
            "last_builder_report": None,
            "last_audit_report": None,
            "last_audit_result": None,
            "human_gate_required": True,
            "approval": None,
            "updated_at": "now",
            "message": "",
        }
        base_report = {
            "schema_version": "0.1",
            "executor_id": "auditor-executor",
            "role": "AUDITOR",
            "authority": "INDEPENDENT_AUDIT",
            "audit_result": "ESCALATE",
            "audited_sha": target,
            "summary": "Generic summary is insufficient.",
            "findings": [],
            "checks": [{
                "id": "o0-c19",
                "status": "NOT_APPLICABLE",
                "evidence": "escalation validation",
            }],
            "residual_risks": [],
            "gate_registration": "NOT_AUTHORIZED",
        }

        for label, reason in (("missing", None), ("empty", ""), ("spaces", "   ")):
            with self.subTest(reason=label):
                state = self.root / f"invalid-escalate-{label}-state.json"
                report = self.root / f"invalid-escalate-{label}-report.json"
                state.write_text(json.dumps(base_state), encoding="utf-8")
                payload = dict(base_report)
                if reason is not None:
                    payload["escalation_reason"] = reason
                report.write_text(json.dumps(payload), encoding="utf-8")
                before = state.read_bytes()

                with self.assertRaisesRegex(HandoffError, "audit schema validation failed"):
                    audit_handoff(state, report)

                self.assertEqual(before, state.read_bytes())
                self.assertTrue(report.is_file())
                unchanged = json.loads(state.read_text())
                self.assertEqual(target, unchanged["audit_target_sha"])
                self.assertEqual("READY_FOR_AUDIT", unchanged["machine_state"])
                self.assertEqual("NONE", unchanged["gate"])
                self.assertIsNone(unchanged["approval"])
                self.assertEqual("O0", unchanged["phase"])

    def _fix_required_state(self, target: str, report: Path, round_number: int = 1):
        current = {
            "schema_version": "0.1",
            "run_id": "run-" + "0" * 64,
            "project_id": "sample",
            "phase": "O0",
            "gate": "NONE",
            "machine_state": "FIX_REQUIRED",
            "next_actor": "BUILDER",
            "blocked_reason": None,
            "builder_branch": "work",
            "builder_executor_id": "builder-executor",
            "auditor_executor_id": "auditor-executor",
            "product_authority_id": "owner",
            "builder_head_sha": target,
            "audit_target_sha": target,
            "last_audited_sha": target,
            "audit_round": round_number,
            "max_audit_rounds": 3,
            "last_builder_report": None,
            "last_audit_report": str(report),
            "last_audit_result": "FAIL",
            "human_gate_required": True,
            "approval": None,
            "updated_at": "now",
            "message": "Audit failed; findings are ready for Builder correction.",
        }
        runner_module.canonicalize_report_evidence(report, "audit", current, report.parent / "evidence")
        current["last_audit_report_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()
        return current

    def _fail_report(self, target: str):
        return {
            "schema_version": "0.1",
            "executor_id": "auditor-executor",
            "role": "AUDITOR",
            "authority": "INDEPENDENT_AUDIT",
            "audit_result": "FAIL",
            "audited_sha": target,
            "summary": "Blocking findings.",
            "findings": [{
                "id": "O0-015-001",
                "severity": "HIGH",
                "blocking": True,
                "files": ["scripts/o0_runner.py"],
                "evidence": "forwarding evidence",
                "problem": "finding must reach Builder",
                "violated_criterion": "O0-C15",
                "resolution_condition": "preserve finding unchanged",
            }],
            "checks": [{"id": "o0-c15", "status": "FAIL", "evidence": "finding"}],
            "residual_risks": [],
            "gate_registration": "NOT_AUTHORIZED",
        }

    def test_fix_required_prepares_exact_findings_for_builder(self):
        target = "a" * 40
        reports = self.root / "reports"
        reports.mkdir()
        audit_report = reports / "audit-report.json"
        source = self._fail_report(target)
        second = dict(source["findings"][0])
        second.update(id="O0-015-002", evidence="second forwarding evidence", problem="second finding")
        source["findings"].append(second)
        audit_report.write_text(json.dumps(source), encoding="utf-8")

        handoff = prepare_builder_findings(
            self._fix_required_state(target, audit_report, round_number=2),
            reports,
        )
        payload = json.loads(handoff.read_text(encoding="utf-8"))

        self.assertEqual(target, payload["audit_target_sha"])
        self.assertEqual(2, payload["audit_round"])
        canonical_audit = json.loads(audit_report.read_text(encoding="utf-8"))
        self.assertEqual(canonical_audit["findings"], payload["findings"])
        self.assertEqual(2, len(payload["findings"]))
        self.assertEqual({"audit_target_sha", "audit_round", "findings"}, set(payload))
        reference = payload["findings"][0]["evidence"]
        self.assertEqual(
            source["findings"][0]["evidence"],
            runner_module.resolve_evidence(reports / "evidence", reference, self._fix_required_state(target, audit_report, round_number=2))["content"],
        )

    def test_runner_passes_failed_audit_findings_to_builder(self):
        target = "a" * 40
        reports = self.root / "reports"
        reports.mkdir()
        audit_report = reports / "audit-report.json"
        source = self._fail_report(target)
        audit_report.write_text(json.dumps(source), encoding="utf-8")
        state = self.root / "state.json"
        state.write_text(
            json.dumps(self._fix_required_state(target, audit_report)),
            encoding="utf-8",
        )
        (self.root / "builder").mkdir()
        config = self.root / "runner.json"
        config.write_text(
            json.dumps({
                "repository": "repo",
                "state_path": "state.json",
                "reports_dir": "reports",
                "builder_workspace": "builder",
                "audit_workspaces": "audits",
                "builder_command": ["builder"],
                "auditor_command": ["auditor"],
            }),
            encoding="utf-8",
        )
        captured = {}

        def capture_builder(command, workspace, report, env, *, write_sandbox=False):
            captured.update(env)
            raise HandoffError("stop before O0-C16")

        with patch("scripts.o0_runner.run_actor", side_effect=capture_builder):
            with self.assertRaisesRegex(HandoffError, "stop before O0-C16"):
                run_once(config)

        handoff = Path(captured["IDEAS_STANDARD_FINDINGS"])
        payload = json.loads(handoff.read_text(encoding="utf-8"))
        self.assertEqual(target, payload["audit_target_sha"])
        self.assertEqual(1, payload["audit_round"])
        self.assertEqual(source["findings"][0]["id"], payload["findings"][0]["id"])
        self.assertEqual({"evidence_id", "sha256"}, set(payload["findings"][0]["evidence"]))

    def test_invalid_pass_with_not_run_is_not_forwarded(self):
        target = "a" * 40
        reports = self.root / "reports"
        reports.mkdir()
        audit_report = reports / "audit-report.json"
        invalid = self._fail_report(target)
        invalid["audit_result"] = "PASS"
        invalid["checks"][0]["status"] = "NOT_RUN"
        audit_report.write_text(json.dumps(invalid), encoding="utf-8")

        with self.assertRaises(HandoffError):
            prepare_builder_findings(
                self._fix_required_state(target, audit_report),
                reports,
            )
        self.assertFalse((reports / "builder-findings.json").exists())

    def test_divergent_sha_is_not_forwarded(self):
        target = "a" * 40
        reports = self.root / "reports"
        reports.mkdir()
        audit_report = reports / "audit-report.json"
        divergent = self._fail_report("b" * 40)
        audit_report.write_text(json.dumps(divergent), encoding="utf-8")

        with self.assertRaisesRegex(HandoffError, "current failed audit target"):
            prepare_builder_findings(
                self._fix_required_state(target, audit_report),
                reports,
            )
        self.assertFalse((reports / "builder-findings.json").exists())

    def test_schema_valid_changed_finding_is_rejected_against_canonical_audit(self):
        target = "a" * 40
        reports = self.root / "reports"
        reports.mkdir()
        audit_report = reports / "audit-report.json"
        audit_report.write_text(json.dumps(self._fail_report(target)), encoding="utf-8")
        current = self._fix_required_state(target, audit_report)
        handoff = prepare_builder_findings(current, reports)
        payload = json.loads(handoff.read_text(encoding="utf-8"))
        payload["findings"][0]["problem"] = "schema-valid adulteration"
        handoff.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaisesRegex(HandoffError, "canonical audit report"):
            runner_module.validate_builder_findings_handoff(current, handoff)

    def test_changed_accepted_audit_report_is_rejected_before_builder(self):
        target = "a" * 40
        reports = self.root / "sealed-reports"
        reports.mkdir()
        audit_report = reports / "audit-report.json"
        audit_report.write_text(json.dumps(self._fail_report(target)), encoding="utf-8")
        current = self._fix_required_state(target, audit_report)
        runner_module.seal_audit_report(audit_report)
        changed = json.loads(audit_report.read_text(encoding="utf-8"))
        changed["findings"][0]["problem"] = "forged after acceptance"
        audit_report.write_text(json.dumps(changed), encoding="utf-8")

        with self.assertRaisesRegex(HandoffError, "accepted audit report"):
            prepare_builder_findings(current, reports)

    def test_removed_or_forged_snapshot_cannot_rewrite_accepted_findings(self):
        target = "a" * 40
        reports = self.root / "tampered-source-reports"
        reports.mkdir()
        report = reports / "audit-report.json"
        report.write_text(json.dumps(self._fail_report(target)), encoding="utf-8")
        current = self._fix_required_state(target, report)
        runner_module.seal_audit_report(report)
        accepted_digest = hashlib.sha256(report.read_bytes()).hexdigest()
        current["last_audit_report_sha256"] = accepted_digest
        changed = json.loads(report.read_text(encoding="utf-8"))
        changed["findings"][0]["problem"] = "forged after FAIL"
        forged = json.dumps(changed).encode()
        report.write_bytes(forged)
        snapshot = runner_module.accepted_audit_snapshot(report)
        state_before = json.dumps(current).encode()

        for snapshot_bytes in (None, forged):
            with self.subTest(snapshot_bytes=snapshot_bytes is not None):
                if snapshot_bytes is None:
                    snapshot.unlink(missing_ok=True)
                else:
                    snapshot.write_bytes(snapshot_bytes)
                with self.assertRaises(HandoffError):
                    prepare_builder_findings(current, reports)
                self.assertEqual(state_before, json.dumps(current).encode())

    def test_forged_source_after_fail_stops_runner_before_builder(self):
        target = "a" * 40
        reports = self.root / "forged-runner-reports"
        reports.mkdir()
        report = reports / "audit-report.json"
        report.write_text(json.dumps(self._fail_report(target)), encoding="utf-8")
        current = self._fix_required_state(target, report)
        state = self.root / "forged-runner-state.json"
        state.write_text(json.dumps(current), encoding="utf-8")
        changed = json.loads(report.read_text(encoding="utf-8"))
        changed["findings"][0]["problem"] = "forged"
        report.write_text(json.dumps(changed), encoding="utf-8")
        runner_module.accepted_audit_snapshot(report).write_bytes(report.read_bytes())
        (self.root / "builder").mkdir()
        config = self.root / "forged-runner.json"
        config.write_text(json.dumps({
            "repository": "repo", "state_path": str(state), "reports_dir": str(reports),
            "builder_workspace": "builder", "audit_workspaces": "audits",
            "builder_command": ["builder"], "auditor_command": ["auditor"],
        }), encoding="utf-8")
        before = state.read_bytes()
        with patch("scripts.o0_runner.run_actor") as actor:
            with self.assertRaisesRegex(HandoffError, "accepted audit report digest"):
                run_once(config)
        actor.assert_not_called()
        self.assertEqual(before, state.read_bytes())

    def test_duplicate_finding_id_is_rejected_before_handoff(self):
        target = "a" * 40
        reports = self.root / "duplicate-reports"
        reports.mkdir()
        audit_report = reports / "audit-report.json"
        source = self._fail_report(target)
        source["findings"].append(dict(source["findings"][0]))
        audit_report.write_text(json.dumps(source), encoding="utf-8")

        with self.assertRaisesRegex(HandoffError, "Duplicate audit finding ID"):
            prepare_builder_findings(self._fix_required_state(target, audit_report), reports)

    def test_runner_rejects_tampered_findings_before_builder_execution(self):
        target = "a" * 40
        reports = self.root / "reports"
        reports.mkdir()
        audit_report = reports / "audit-report.json"
        audit_report.write_text(json.dumps(self._fail_report(target)), encoding="utf-8")
        state = self.root / "state.json"
        current = self._fix_required_state(target, audit_report)
        state.write_text(json.dumps(current), encoding="utf-8")
        (self.root / "builder").mkdir()
        config = self.root / "runner.json"
        config.write_text(json.dumps({
            "repository": "repo", "state_path": "state.json", "reports_dir": "reports",
            "builder_workspace": "builder", "audit_workspaces": "audits",
            "builder_command": ["builder"], "auditor_command": ["auditor"],
        }), encoding="utf-8")
        original_prepare = runner_module.prepare_builder_findings
        actor_called = False

        def tampered_prepare(state_payload, reports_path):
            handoff = original_prepare(state_payload, reports_path)
            payload = json.loads(handoff.read_text(encoding="utf-8"))
            payload["findings"][0]["problem"] = "changed before Builder"
            handoff.write_text(json.dumps(payload), encoding="utf-8")
            return handoff

        def mark_actor(*args, **kwargs):
            nonlocal actor_called
            actor_called = True
            raise HandoffError("Builder must not execute")

        before = state.read_bytes()
        with patch("scripts.o0_runner.prepare_builder_findings", side_effect=tampered_prepare), patch(
            "scripts.o0_runner.run_actor", side_effect=mark_actor
        ):
            with self.assertRaisesRegex(HandoffError, "canonical audit report"):
                run_once(config)

        self.assertFalse(actor_called)
        self.assertEqual(before, state.read_bytes())

    def test_launch_revalidates_findings_after_initial_check(self):
        target = "a" * 40
        reports = self.root / "launch-reports"
        reports.mkdir()
        audit_report = reports / "audit-report.json"
        audit_report.write_text(json.dumps(self._fail_report(target)), encoding="utf-8")
        current = self._fix_required_state(target, audit_report)
        state = self.root / "launch-state.json"
        state.write_text(json.dumps(current), encoding="utf-8")
        handoff = prepare_builder_findings(current, reports)
        runner_module.validate_builder_findings_handoff(current, handoff)
        payload = json.loads(handoff.read_text(encoding="utf-8"))
        payload["findings"][0]["problem"] = "changed after first validation"
        handoff.write_text(json.dumps(payload), encoding="utf-8")
        marker = self.root / "builder-launched.txt"

        with self.assertRaisesRegex(HandoffError, "canonical audit report"):
            run_actor(
                [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"],
                self.root,
                reports / "builder-report.json",
                {"IDEAS_STANDARD_FINDINGS": str(handoff), "IDEAS_STANDARD_STATE": str(state)},
            )

        self.assertFalse(marker.exists())

    @unittest.skipUnless(hasattr(os, "memfd_create"), "sealed file descriptors require Linux")
    def test_handoff_swap_during_sealing_is_rejected_before_builder(self):
        target = "a" * 40
        reports = self.root / "handoff-race-reports"
        reports.mkdir()
        audit_report = reports / "audit-report.json"
        audit_report.write_text(json.dumps(self._fail_report(target)), encoding="utf-8")
        current = self._fix_required_state(target, audit_report)
        state = self.root / "handoff-race-state.json"
        state.write_text(json.dumps(current), encoding="utf-8")
        handoff = prepare_builder_findings(current, reports)
        original_memfd = os.memfd_create
        marker = self.root / "race-builder-launched"

        def swap_then_seal(*args, **kwargs):
            payload = json.loads(handoff.read_text(encoding="utf-8"))
            payload["findings"][0]["problem"] = "forged during sealing"
            handoff.write_text(json.dumps(payload), encoding="utf-8")
            return original_memfd(*args, **kwargs)

        before = state.read_bytes()
        with patch("scripts.o0_runner.os.memfd_create", side_effect=swap_then_seal):
            with self.assertRaises(HandoffError):
                run_actor(
                    [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
                    self.root,
                    reports / "builder-report.json",
                    {"IDEAS_STANDARD_FINDINGS": str(handoff), "IDEAS_STANDARD_STATE": str(state)},
                )
        self.assertFalse(marker.exists())
        self.assertEqual(before, state.read_bytes())

    @unittest.skipUnless(hasattr(os, "memfd_create"), "sealed file descriptors require Linux")
    def test_late_handoff_swap_cannot_reach_builder(self):
        target = "a" * 40
        reports = self.root / "sealed-delivery-reports"
        reports.mkdir()
        audit_report = reports / "audit-report.json"
        audit_report.write_text(json.dumps(self._fail_report(target)), encoding="utf-8")
        current = self._fix_required_state(target, audit_report)
        state = self.root / "sealed-delivery-state.json"
        state.write_text(json.dumps(current), encoding="utf-8")
        handoff = prepare_builder_findings(current, reports)
        before = state.read_bytes()
        real_run = subprocess.run

        def swap_at_spawn(*args, **kwargs):
            payload = json.loads(handoff.read_text(encoding="utf-8"))
            payload["findings"][0]["problem"] = "forged at spawn"
            handoff.write_text(json.dumps(payload), encoding="utf-8")
            return real_run(*args, **kwargs)

        report = reports / "builder-report.json"
        code = "import json,os; from pathlib import Path; p=json.load(open(os.environ['IDEAS_STANDARD_FINDINGS'])); Path(os.environ['IDEAS_STANDARD_REPORT']).write_text(json.dumps({'problem':p['findings'][0]['problem']}))"
        with patch("scripts.o0_runner.subprocess.run", side_effect=swap_at_spawn):
            run_actor(
                [sys.executable, "-c", code], self.root, report,
                {"IDEAS_STANDARD_FINDINGS": str(handoff), "IDEAS_STANDARD_STATE": str(state)},
            )
        self.assertEqual("finding must reach Builder", json.loads(report.read_text())["problem"])
        self.assertEqual(before, state.read_bytes())

    @unittest.skipUnless(os.name == "nt", "Windows file sharing semantics")
    def test_windows_late_handoff_replace_is_blocked_before_builder_read(self):
        target = "a" * 40
        reports = self.root / "windows-findings-reports"
        reports.mkdir()
        audit_report = reports / "audit-report.json"
        audit_report.write_text(json.dumps(self._fail_report(target)), encoding="utf-8")
        current = self._fix_required_state(target, audit_report)
        state = self.root / "windows-findings-state.json"
        state.write_text(json.dumps(current), encoding="utf-8")
        handoff = prepare_builder_findings(current, reports)
        before = state.read_bytes()
        real_run = subprocess.run
        rejected = False
        write_rejected = False

        def swap_at_spawn(*args, **kwargs):
            nonlocal rejected, write_rejected
            payload = json.loads(handoff.read_text(encoding="utf-8"))
            payload["findings"][0]["problem"] = "forged at spawn"
            replacement = self.root / "forged-findings.json"
            replacement.write_text(json.dumps(payload), encoding="utf-8")
            try:
                os.replace(replacement, handoff)
            except OSError:
                rejected = True
            try:
                handoff.write_text(json.dumps(payload), encoding="utf-8")
            except OSError:
                write_rejected = True
            return real_run(*args, **kwargs)

        report = reports / "builder-report.json"
        code = "import json,os; from pathlib import Path; p=json.load(open(os.environ['IDEAS_STANDARD_FINDINGS'])); Path(os.environ['IDEAS_STANDARD_REPORT']).write_text(json.dumps({'problem':p['findings'][0]['problem']}))"
        with patch("scripts.o0_runner.subprocess.run", side_effect=swap_at_spawn):
            run_actor(
                [sys.executable, "-c", code], self.root, report,
                {"IDEAS_STANDARD_FINDINGS": str(handoff), "IDEAS_STANDARD_STATE": str(state)},
            )
        self.assertTrue(rejected)
        self.assertTrue(write_rejected)
        self.assertEqual("finding must reach Builder", json.loads(report.read_text())["problem"])
        self.assertEqual(before, state.read_bytes())


    def _builder_report(self, result_sha: str):
        return {
            "schema_version": "0.1",
            "executor_id": "builder-executor",
            "role": "BUILDER",
            "authority": "IMPLEMENTATION",
            "result_sha": result_sha,
            "result": "READY_FOR_AUDIT",
            "summary": "Correction complete.",
            "changed_paths": ["file.txt"],
            "checks": [{"id": "unit", "status": "PASS", "evidence": "green"}],
            "limitations": [],
            "disputed_findings": [],
            "escalation": None,
        }

    def _canonicalize_builder_report(self, current, report_path):
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        context = {**current, "audit_target_sha": payload["result_sha"]}
        return runner_module.canonicalize_report_evidence(
            report_path, "builder", context, report_path.parent / "evidence"
        )

    def _accept_changed_audit_report(self, current, report_path):
        runner_module.canonicalize_report_evidence(
            report_path, "audit", current, report_path.parent / "evidence"
        )
        current["last_audit_report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()

    def _correction_inputs(self):
        repository, previous, corrected = self._repository()
        reports = self.root / "correction-reports"
        reports.mkdir()
        audit_report = reports / "audit-report.json"
        audit_report.write_text(json.dumps(self._fail_report(previous)), encoding="utf-8")
        current = self._fix_required_state(previous, audit_report, round_number=2)
        handoff = prepare_builder_findings(current, reports)
        return repository, previous, corrected, current, handoff, handoff.read_bytes()

    def test_fix_required_accepts_existing_new_sha_and_preserves_round_link(self):
        repository, previous, corrected, current, handoff, before = self._correction_inputs()
        validate_builder_result(
            current,
            self._builder_report(corrected),
            repository,
            handoff,
            before,
        )
        payload = json.loads(handoff.read_text(encoding="utf-8"))
        self.assertNotEqual(previous, corrected)
        self.assertEqual(previous, payload["audit_target_sha"])
        self.assertEqual(2, payload["audit_round"])

    def test_reaudit_handoff_uses_delta_when_context_is_sufficient(self):
        repository, previous, corrected, current, findings_handoff, _ = self._correction_inputs()
        audit_report = Path(current["last_audit_report"])
        audit_payload = json.loads(audit_report.read_text(encoding="utf-8"))
        audit_payload["findings"][0]["severity"] = "MEDIUM"
        audit_payload["checks"][0]["status"] = "PASS"
        audit_report.write_text(json.dumps(audit_payload), encoding="utf-8")
        self._accept_changed_audit_report(current, audit_report)
        findings_handoff = prepare_builder_findings(current, findings_handoff.parent)
        builder_payload = self._builder_report(corrected)
        reports = self.root / "reaudit-reports"

        creator = getattr(runner_module, "prepare_reaudit_handoff", None)
        self.assertIsNotNone(creator)
        handoff = creator(current, builder_payload, repository, findings_handoff, reports)
        payload = json.loads(handoff.read_text(encoding="utf-8"))

        self.assertEqual(previous, payload["previous_audited_sha"])
        self.assertEqual(corrected, payload["new_audit_target_sha"])
        self.assertEqual(2, payload["audit_round"])
        self.assertEqual("DELTA", payload["context_mode"])
        self.assertEqual([], payload["full_context_reasons"])
        self.assertEqual(["file.txt"], payload["changed_paths"])
        self.assertEqual(audit_payload["findings"][0]["id"], payload["findings"][0]["id"])
        self.assertEqual(
            "forwarding evidence",
            runner_module.resolve_evidence(
                reports / "evidence", payload["findings"][0]["evidence"], current
            )["content"],
        )
        self.assertEqual("o0-c15", payload["reusable_evidence"][0]["check_id"])
        self.assertEqual("PASS", payload["reusable_evidence"][0]["status"])

    def test_reaudit_handoff_requires_full_context_for_each_risk_condition(self):
        repository, _, corrected, current, findings_handoff, _ = self._correction_inputs()
        audit_report = Path(current["last_audit_report"])
        reports = self.root / "full-reaudit-reports"
        cases = (
            ("OUT_OF_SCOPE_CHANGE", "MEDIUM", "finding", ["undeclared.txt"]),
            ("MISSING_EVIDENCE", "MEDIUM", "", ["file.txt"]),
            ("MATERIAL_RISK", "HIGH", "finding", ["file.txt"]),
        )

        for reason, severity, check_evidence, declared_paths in cases:
            with self.subTest(reason=reason):
                audit_payload = self._fail_report(current["audit_target_sha"])
                audit_payload["findings"][0]["severity"] = severity
                audit_payload["checks"][0]["evidence"] = check_evidence
                audit_report.write_text(json.dumps(audit_payload), encoding="utf-8")
                self._accept_changed_audit_report(current, audit_report)
                findings_handoff = prepare_builder_findings(current, findings_handoff.parent)
                builder_payload = self._builder_report(corrected)
                builder_payload["changed_paths"] = declared_paths
                case_reports = reports / reason.lower()

                handoff = runner_module.prepare_reaudit_handoff(
                    current, builder_payload, repository, findings_handoff, case_reports
                )
                payload = json.loads(handoff.read_text(encoding="utf-8"))

                self.assertEqual("FULL", payload["context_mode"])
                self.assertEqual([reason], payload["full_context_reasons"])
                self.assertEqual(audit_payload["findings"][0]["id"], payload["findings"][0]["id"])
                self.assertEqual(1, len(payload["reusable_evidence"]))

    def test_runner_creates_and_supplies_reaudit_handoff(self):
        repository, _, corrected, current, _, _ = self._correction_inputs()
        state = self.root / "reaudit-state.json"
        state.write_text(json.dumps(current), encoding="utf-8")
        reports = self.root / "correction-reports"
        config = self.root / "reaudit-runner.json"
        config.write_text(json.dumps({
            "repository": "repo",
            "state_path": "reaudit-state.json",
            "reports_dir": "correction-reports",
            "builder_workspace": "repo",
            "audit_workspaces": "reaudit-workspaces",
            "builder_command": ["builder"],
            "auditor_command": ["auditor"],
        }), encoding="utf-8")

        def write_builder_report(command, workspace, report, env, *, write_sandbox=False):
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(json.dumps(self._builder_report(corrected)), encoding="utf-8")

        with patch("scripts.o0_runner.run_actor", side_effect=write_builder_report):
            run_once(config)

        handoff = reports / "reaudit-handoff.json"
        self.assertTrue(handoff.is_file())
        captured = {}
        audit_workspace = self.root / "reaudit-workspace"
        audit_workspace.mkdir()

        def capture_auditor(command, workspace, report, env, *, write_sandbox=False):
            captured.update(env)
            raise HandoffError("captured reaudit context")

        with (
            patch("scripts.o0_runner.prepare_audit_workspace", return_value=audit_workspace),
            patch("scripts.o0_runner.write_state_snapshot", return_value=self.root / "snapshot.json"),
            patch("scripts.o0_runner.run_actor", side_effect=capture_auditor),
        ):
            with self.assertRaisesRegex(HandoffError, "captured reaudit context"):
                run_once(config)

        self.assertEqual(str(handoff), captured.get("IDEAS_STANDARD_REAUDIT_HANDOFF"))

    def test_reaudit_cannot_downgrade_material_risk_to_delta(self):
        repository, _, corrected, current, findings_handoff, _ = self._correction_inputs()
        builder_payload = self._builder_report(corrected)
        reports = self.root / "tampered-reaudit-reports"
        handoff = runner_module.prepare_reaudit_handoff(
            current, builder_payload, repository, findings_handoff, reports
        )
        state = self.root / "tampered-reaudit-state.json"
        state.write_text(json.dumps(current), encoding="utf-8")
        builder_report_path = self.root / "tampered-builder-report.json"
        builder_report_path.write_text(json.dumps(builder_payload), encoding="utf-8")
        self._canonicalize_builder_report(current, builder_report_path)
        transitioned = builder_handoff(state, builder_report_path)
        tampered = json.loads(handoff.read_text(encoding="utf-8"))
        tampered["context_mode"] = "DELTA"
        tampered["full_context_reasons"] = []
        handoff.write_text(json.dumps(tampered), encoding="utf-8")
        before = state.read_bytes()

        with self.assertRaisesRegex(HandoffError, "Reaudit context decision is inconsistent"):
            runner_module.validate_reaudit_handoff(transitioned, handoff, repository)

        self.assertEqual(before, state.read_bytes())

    def test_reaudit_cannot_replace_builder_paths_to_downgrade_out_of_scope_change(self):
        repository, _, corrected, current, findings_handoff, _ = self._correction_inputs()
        audit_report = Path(current["last_audit_report"])
        audit_payload = json.loads(audit_report.read_text(encoding="utf-8"))
        audit_payload["findings"][0]["severity"] = "MEDIUM"
        audit_report.write_text(json.dumps(audit_payload), encoding="utf-8")
        self._accept_changed_audit_report(current, audit_report)
        findings_handoff = prepare_builder_findings(current, findings_handoff.parent)
        builder_payload = self._builder_report(corrected)
        builder_payload["changed_paths"] = ["undeclared.txt"]
        reports = self.root / "tampered-paths-reaudit-reports"
        handoff = runner_module.prepare_reaudit_handoff(
            current, builder_payload, repository, findings_handoff, reports
        )
        self.assertEqual("FULL", json.loads(handoff.read_text(encoding="utf-8"))["context_mode"])

        state = self.root / "tampered-paths-reaudit-state.json"
        state.write_text(json.dumps(current), encoding="utf-8")
        builder_report_path = self.root / "canonical-builder-report.json"
        builder_report_path.write_text(json.dumps(builder_payload), encoding="utf-8")
        self._canonicalize_builder_report(current, builder_report_path)
        transitioned = builder_handoff(state, builder_report_path)
        tampered = json.loads(handoff.read_text(encoding="utf-8"))
        tampered["declared_changed_paths"] = ["file.txt"]
        tampered["context_mode"] = "DELTA"
        tampered["full_context_reasons"] = []
        handoff.write_text(json.dumps(tampered), encoding="utf-8")
        before = state.read_bytes()

        with self.assertRaisesRegex(HandoffError, "canonical Builder report"):
            runner_module.validate_reaudit_handoff(transitioned, handoff, repository)

        self.assertEqual(before, state.read_bytes())

    def test_builder_findings_handoff_rejects_history_fields(self):
        _, _, _, current, handoff, _ = self._correction_inputs()
        payload = json.loads(handoff.read_text(encoding="utf-8"))
        payload["transcript"] = ["entire prior conversation"]
        handoff.write_text(json.dumps(payload), encoding="utf-8")

        validator = getattr(runner_module, "validate_builder_findings_handoff", None)
        self.assertIsNotNone(validator, "Builder findings needs a closed minimal-context contract")
        with self.assertRaises(HandoffError):
            validator(current, handoff)

    def test_reaudit_handoff_size_ignores_irrelevant_report_history(self):
        repository, _, corrected, current, findings_handoff, _ = self._correction_inputs()
        audit_report = Path(current["last_audit_report"])
        audit_payload = json.loads(audit_report.read_text(encoding="utf-8"))
        audit_payload["findings"][0]["severity"] = "MEDIUM"
        audit_payload["summary"] = "historical narrative " * 5000
        audit_report.write_text(json.dumps(audit_payload), encoding="utf-8")
        self._accept_changed_audit_report(current, audit_report)
        findings_handoff = prepare_builder_findings(current, findings_handoff.parent)
        builder_payload = self._builder_report(corrected)
        builder_payload["summary"] = "superseded builder narrative " * 5000

        handoff = runner_module.prepare_reaudit_handoff(
            current,
            builder_payload,
            repository,
            findings_handoff,
            self.root / "bounded-reaudit-reports",
        )
        payload = json.loads(handoff.read_text(encoding="utf-8"))

        self.assertEqual(
            {
                "schema_version", "previous_audited_sha", "new_audit_target_sha",
                "audit_round", "context_mode", "full_context_reasons", "findings",
                "changed_paths", "declared_changed_paths", "reusable_evidence",
            },
            set(payload),
        )
        self.assertNotIn("historical narrative", handoff.read_text(encoding="utf-8"))
        self.assertNotIn("superseded builder narrative", handoff.read_text(encoding="utf-8"))
        self.assertLess(handoff.stat().st_size, 4096)

    def test_evidence_reference_is_stable_and_verifiable(self):
        _, previous, _, current, _, _ = self._correction_inputs()
        evidence_root = self.root / "evidence"
        store = getattr(runner_module, "store_evidence", None)
        resolve = getattr(runner_module, "resolve_evidence", None)
        self.assertIsNotNone(store)
        self.assertIsNotNone(resolve)

        reference = store(
            evidence_root,
            evidence_id="finding-O0-015-001",
            content="reproducible proof",
            current=current,
        )

        self.assertEqual({"evidence_id", "sha256"}, set(reference))
        self.assertEqual(64, len(reference["sha256"]))
        envelope = resolve(evidence_root, reference, current)
        self.assertEqual("reproducible proof", envelope["content"])
        self.assertEqual(current["run_id"], envelope["run_id"])
        self.assertEqual(2, envelope["audit_round"])
        self.assertEqual(previous, envelope["audit_target_sha"])
        self.assertEqual(
            reference,
            store(
                evidence_root,
                evidence_id="finding-O0-015-001",
                content="reproducible proof",
                current=current,
            ),
        )

    def test_evidence_reference_rejects_missing_tampered_or_cross_run_content(self):
        _, _, _, current, _, _ = self._correction_inputs()
        evidence_root = self.root / "adversarial-evidence"
        store = getattr(runner_module, "store_evidence", None)
        resolve = getattr(runner_module, "resolve_evidence", None)
        self.assertIsNotNone(store)
        self.assertIsNotNone(resolve)
        reference = store(
            evidence_root,
            evidence_id="check-o0-c37",
            content="original",
            current=current,
        )

        with self.assertRaises(HandoffError):
            resolve(evidence_root, {**reference, "evidence_id": "missing"}, current)
        path = evidence_root / "check-o0-c37.json"
        original = path.read_bytes()
        path.write_bytes(original.replace(b"original", b"replaced"))
        with self.assertRaises(HandoffError):
            resolve(evidence_root, reference, current)
        path.write_bytes(original)
        with self.assertRaises(HandoffError):
            resolve(evidence_root, reference, {**current, "run_id": "run-" + "f" * 64})
        with self.assertRaises(HandoffError):
            resolve(evidence_root, reference, {**current, "audit_round": current["audit_round"] + 1})
        with self.assertRaises(HandoffError):
            resolve(evidence_root, reference, {**current, "audit_target_sha": "f" * 40})
        with self.assertRaises(HandoffError):
            resolve(evidence_root, {**reference, "sha256": "f" * 64}, current)
        with self.assertRaises(HandoffError):
            store(evidence_root, evidence_id="../escape", content="x", current=current)
        with self.assertRaises(HandoffError):
            store(evidence_root, evidence_id="check-o0-c37", content="replacement", current=current)

    def test_builder_handoff_references_evidence_without_retransmitting_content(self):
        _, _, _, current, handoff, _ = self._correction_inputs()
        payload = json.loads(handoff.read_text(encoding="utf-8"))
        reference = payload["findings"][0]["evidence"]

        self.assertEqual({"evidence_id", "sha256"}, set(reference))
        self.assertNotIn("forwarding evidence", handoff.read_text(encoding="utf-8"))
        envelope = runner_module.resolve_evidence(handoff.parent / "evidence", reference, current)
        self.assertEqual("forwarding evidence", envelope["content"])

    def test_reaudit_handoff_reuses_verifiable_references_without_content(self):
        repository, _, corrected, current, findings_handoff, _ = self._correction_inputs()
        builder_payload = self._builder_report(corrected)
        reports = self.root / "reference-reaudit-reports"
        reports.mkdir()
        target_findings = reports / "builder-findings.json"
        target_findings.write_bytes(findings_handoff.read_bytes())
        source_evidence = findings_handoff.parent / "evidence"
        target_evidence = reports / "evidence"
        target_evidence.mkdir()
        for source in source_evidence.iterdir():
            (target_evidence / source.name).write_bytes(source.read_bytes())

        handoff = runner_module.prepare_reaudit_handoff(
            current, builder_payload, repository, target_findings, reports
        )
        payload = json.loads(handoff.read_text(encoding="utf-8"))

        finding_reference = payload["findings"][0]["evidence"]
        check_reference = payload["reusable_evidence"][0]["evidence"]
        self.assertEqual({"evidence_id", "sha256"}, set(finding_reference))
        self.assertEqual({"evidence_id", "sha256"}, set(check_reference))
        serialized = handoff.read_text(encoding="utf-8")
        self.assertNotIn("forwarding evidence", serialized)
        self.assertNotIn('"evidence": "finding"', serialized)
        self.assertEqual(
            "finding",
            runner_module.resolve_evidence(target_evidence, check_reference, current)["content"],
        )

    def test_tampered_evidence_is_rejected_before_auditor_without_state_mutation(self):
        repository, _, corrected, current, findings_handoff, _ = self._correction_inputs()
        reports = findings_handoff.parent
        builder_payload = self._builder_report(corrected)
        handoff = runner_module.prepare_reaudit_handoff(
            current, builder_payload, repository, findings_handoff, reports
        )
        state = self.root / "evidence-state.json"
        state.write_text(json.dumps(current), encoding="utf-8")
        builder_report = reports / "builder-report.json"
        builder_report.write_text(json.dumps(builder_payload), encoding="utf-8")
        self._canonicalize_builder_report(current, builder_report)
        builder_handoff(state, builder_report)
        payload = json.loads(handoff.read_text(encoding="utf-8"))
        evidence_path = reports / "evidence" / f"{payload['reusable_evidence'][0]['evidence']['evidence_id']}.json"
        evidence_path.write_bytes(evidence_path.read_bytes().replace(b"finding", b"changed"))
        config = self.root / "evidence-runner.json"
        config.write_text(json.dumps({
            "repository": "repo",
            "state_path": "evidence-state.json",
            "reports_dir": "correction-reports",
            "builder_workspace": "repo",
            "audit_workspaces": "evidence-audits",
            "builder_command": ["builder"],
            "auditor_command": ["auditor"],
        }), encoding="utf-8")
        before = state.read_bytes()
        actor_started = False

        def unexpected_actor(*args, **kwargs):
            nonlocal actor_started
            actor_started = True

        with patch("scripts.o0_runner.run_actor", side_effect=unexpected_actor):
            with self.assertRaisesRegex(HandoffError, "digest mismatch"):
                run_once(config)

        self.assertFalse(actor_started)
        self.assertEqual(before, state.read_bytes())

    def test_raw_report_evidence_is_canonicalized_to_references(self):
        repository, previous, corrected = self._repository()
        reports = self.root / "canonical-reports"
        reports.mkdir()
        audit_path = reports / "audit-report.json"
        audit_path.write_text(json.dumps(self._fail_report(previous)), encoding="utf-8")
        audit_state = self._fix_required_state(previous, audit_path, round_number=2)
        builder_path = reports / "builder-report.json"
        builder_path.write_text(json.dumps(self._builder_report(corrected)), encoding="utf-8")
        canonicalize = getattr(runner_module, "canonicalize_report_evidence", None)
        self.assertIsNotNone(canonicalize)

        canonicalize(builder_path, "builder", {**audit_state, "audit_target_sha": corrected}, reports / "evidence")
        canonicalize(audit_path, "audit", audit_state, reports / "evidence")
        builder = json.loads(builder_path.read_text(encoding="utf-8"))
        audit = json.loads(audit_path.read_text(encoding="utf-8"))

        self.assertEqual({"evidence_id", "sha256"}, set(builder["checks"][0]["evidence"]))
        self.assertEqual({"evidence_id", "sha256"}, set(audit["checks"][0]["evidence"]))
        self.assertEqual({"evidence_id", "sha256"}, set(audit["findings"][0]["evidence"]))
        self.assertNotIn('"evidence": "', builder_path.read_text(encoding="utf-8"))
        self.assertNotIn('"evidence": "', audit_path.read_text(encoding="utf-8"))

    def test_builder_transition_rejects_inline_missing_tampered_or_wrong_binding_evidence(self):
        _, _, corrected, source_current, _, _ = self._correction_inputs()
        with self.assertRaises(HandoffError):
            runner_module.validate_with_schema(self._builder_report(corrected), "builder")
        for mode in ("missing", "tampered", "wrong_binding"):
            with self.subTest(mode=mode):
                current = json.loads(json.dumps(source_current))
                case_root = self.root / f"canonical-{mode}"
                case_root.mkdir()
                state = case_root / "state.json"
                report = case_root / "builder-report.json"
                report.write_text(json.dumps(self._builder_report(corrected)), encoding="utf-8")
                self._canonicalize_builder_report(current, report)
                canonical = json.loads(report.read_text(encoding="utf-8"))
                evidence_path = case_root / "evidence" / f"{canonical['checks'][0]['evidence']['evidence_id']}.json"
                if mode == "missing":
                    evidence_path.unlink()
                elif mode == "tampered":
                    evidence_path.write_bytes(evidence_path.read_bytes().replace(b"green", b"red"))
                else:
                    current = {**current, "run_id": "run-" + "f" * 64}
                state.write_text(json.dumps(current), encoding="utf-8")
                before = state.read_bytes()

                with self.assertRaises(HandoffError):
                    builder_handoff(state, report)

                self.assertEqual(before, state.read_bytes())

    def test_fix_required_rejects_missing_result_sha(self):
        repository, _, corrected, current, handoff, before = self._correction_inputs()
        report = self._builder_report(corrected)
        del report["result_sha"]
        with self.assertRaises(HandoffError):
            validate_builder_result(current, report, repository, handoff, before)

    def test_fix_required_rejects_unknown_result_sha(self):
        repository, _, _, current, handoff, before = self._correction_inputs()
        with self.assertRaisesRegex(HandoffError, "Unknown result SHA"):
            validate_builder_result(
                current,
                self._builder_report("f" * 40),
                repository,
                handoff,
                before,
            )

    def test_fix_required_rejects_previous_audit_target_sha(self):
        repository, previous, _, current, handoff, before = self._correction_inputs()
        with self.assertRaisesRegex(HandoffError, "must produce a new SHA"):
            validate_builder_result(
                current,
                self._builder_report(previous),
                repository,
                handoff,
                before,
            )

    def test_valid_correction_requires_fresh_audit(self):
        repository, previous, corrected, current, _, _ = self._correction_inputs()
        state = self.root / "correction-state.json"
        state.write_text(json.dumps(current), encoding="utf-8")
        report = self.root / "correction-builder.json"
        report.write_text(json.dumps(self._builder_report(corrected)), encoding="utf-8")
        self._canonicalize_builder_report(current, report)

        result = builder_handoff(state, report)

        self.assertEqual(corrected, result["audit_target_sha"])
        self.assertEqual("READY_FOR_AUDIT", result["machine_state"])
        self.assertEqual("AUDITOR", next_actor(result))
        self.assertIsNone(result["last_audited_sha"])
        self.assertIsNone(result["last_audit_result"])
        self.assertNotEqual(previous, result["audit_target_sha"])

    def test_previous_pass_is_not_reused_for_new_sha(self):
        repository, previous, corrected, current, _, _ = self._correction_inputs()
        current["last_audit_result"] = "PASS"
        state = self.root / "stale-pass-state.json"
        state.write_text(json.dumps(current), encoding="utf-8")
        report = self.root / "stale-pass-builder.json"
        report.write_text(json.dumps(self._builder_report(corrected)), encoding="utf-8")
        self._canonicalize_builder_report(current, report)

        result = builder_handoff(state, report)

        self.assertIsNone(result["last_audited_sha"])
        self.assertIsNone(result["last_audit_result"])
        self.assertEqual("AUDITOR", next_actor(result))

    def test_fix_required_rejects_result_sha_divergent_from_builder_head(self):
        repository, previous, corrected, current, handoff, before = self._correction_inputs()
        subprocess.run(
            ["git", "checkout", "--detach", previous],
            cwd=repository,
            check=True,
            capture_output=True,
        )
        with self.assertRaisesRegex(HandoffError, "Builder workspace HEAD"):
            validate_builder_result(
                current,
                self._builder_report(corrected),
                repository,
                handoff,
                before,
                repository,
            )

    def test_fix_required_rejects_changed_findings_link(self):
        repository, _, corrected, current, handoff, before = self._correction_inputs()
        payload = json.loads(handoff.read_text(encoding="utf-8"))
        payload["audit_round"] = 3
        handoff.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(HandoffError, "findings changed"):
            validate_builder_result(
                current,
                self._builder_report(corrected),
                repository,
                handoff,
                before,
            )


if __name__ == "__main__":
    unittest.main()
