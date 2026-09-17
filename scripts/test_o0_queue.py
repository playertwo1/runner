#!/usr/bin/env python3
"""Unit and integration tests for O0 v2 M4 Task Queue runner."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.o0_runner import HandoffError, run_task_queue
from scripts.orchestrate_handoffs import status, validate_with_schema


class O0QueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir(parents=True, exist_ok=True)
        self._git(["init", "-b", "main"], cwd=self.repo)
        self._git(["config", "user.name", "Test Builder"], cwd=self.repo)
        self._git(["config", "user.email", "builder@example.invalid"], cwd=self.repo)
        (self.repo / "base.txt").write_text("initial", encoding="utf-8")
        self._git(["add", "."], cwd=self.repo)
        self._git(["commit", "-m", "chore: initial commit"], cwd=self.repo)

        self.reports_dir = self.root / "reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.audit_workspaces = self.root / "audit_workspaces"
        self.audit_workspaces.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "orchestrator-state.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _git(self, args: list[str], cwd: Path) -> str:
        res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
        return res.stdout.strip()

    def _create_actor_scripts(self) -> tuple[Path, Path]:
        """Creates dummy Builder and Auditor adapter scripts that inspect IDEAS_STANDARD_TASK_ID."""
        builder_script = self.root / "builder.py"
        builder_script.write_text(
            "import os, pathlib, subprocess, json\n"
            "report_path = pathlib.Path(os.environ['IDEAS_STANDARD_REPORT'])\n"
            "task_id = os.environ.get('IDEAS_STANDARD_TASK_ID', 'default')\n"
            "target = pathlib.Path(f'{task_id}.txt')\n"
            "target.write_text(f'content for {task_id}\\n', encoding='utf-8')\n"
            "subprocess.run(['git', 'add', '.'], check=True)\n"
            "subprocess.run(['git', 'commit', '-m', f'feat: {task_id}'], check=True)\n"
            "sha = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()\n"
            "report = {\n"
            "    'schema_version': '0.1',\n"
            "    'executor_id': 'test-builder',\n"
            "    'role': 'BUILDER',\n"
            "    'authority': 'IMPLEMENTATION',\n"
            "    'result_sha': sha,\n"
            "    'result': 'READY_FOR_AUDIT',\n"
            "    'summary': f'Completed {task_id}',\n"
            "    'changed_paths': ['.'],\n"
            "    'checks': [{'id': 'chk-01', 'status': 'PASS', 'evidence': f'Build verified for {task_id} text'}],\n"
            "    'limitations': [],\n"
            "    'disputed_findings': [],\n"
            "    'escalation': None,\n"
            "}\n"
            "report_path.parent.mkdir(parents=True, exist_ok=True)\n"
            "report_path.write_text(json.dumps(report), encoding='utf-8')\n",
            encoding="utf-8",
        )

        auditor_script = self.root / "auditor.py"
        auditor_script.write_text(
            "import os, pathlib, json\n"
            "report_path = pathlib.Path(os.environ['IDEAS_STANDARD_REPORT'])\n"
            "sha = os.environ.get('IDEAS_STANDARD_AUDIT_TARGET_SHA', '')\n"
            "report = {\n"
            "    'schema_version': '0.1',\n"
            "    'executor_id': 'test-auditor',\n"
            "    'role': 'AUDITOR',\n"
            "    'authority': 'INDEPENDENT_AUDIT',\n"
            "    'audit_result': 'PASS',\n"
            "    'audited_sha': sha,\n"
            "    'summary': 'Audit passed',\n"
            "    'findings': [],\n"
            "    'checks': [{'id': 'chk-aud-01', 'status': 'PASS', 'evidence': 'Verification passed text'}],\n"
            "    'residual_risks': [],\n"
            "    'gate_registration': 'NOT_AUTHORIZED',\n"
            "}\n"
            "report_path.parent.mkdir(parents=True, exist_ok=True)\n"
            "report_path.write_text(json.dumps(report), encoding='utf-8')\n",
            encoding="utf-8",
        )
        return builder_script, auditor_script

    def test_two_authorized_tasks_run_in_sequence_with_distinct_run_ids(self) -> None:
        builder_script, auditor_script = self._create_actor_scripts()

        config = {
            "repository": str(self.repo),
            "state_path": str(self.state_path),
            "reports_dir": str(self.reports_dir),
            "builder_workspace": str(self.repo),
            "audit_workspaces": str(self.audit_workspaces),
            "builder_command": [sys.executable, str(builder_script)],
            "auditor_command": [sys.executable, str(auditor_script)],
            "max_retries": 3,
        }
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

        queue = {
            "schema_version": "0.1",
            "phase": "O0",
            "tasks": [
                {
                    "task_id": "task-01-multiply",
                    "goal": "Implement multiply",
                    "scope": ["task-01-multiply.txt"],
                    "acceptance_criteria": ["multiply implemented", "clean checkout"],
                    "phase": "O0",
                },
                {
                    "task_id": "task-02-divide",
                    "goal": "Implement divide",
                    "scope": ["task-02-divide.txt"],
                    "acceptance_criteria": ["divide implemented", "clean checkout"],
                    "phase": "O0",
                },
            ],
        }
        queue_path = self.root / "queue.json"
        queue_path.write_text(json.dumps(queue, indent=2), encoding="utf-8")

        result = run_task_queue(config_path, queue_path, max_steps_per_task=10)

        self.assertEqual("COMPLETED", result["status"])
        self.assertIsNone(result["stop_reason"])
        self.assertEqual(2, len(result["executed_tasks"]))

        task1 = result["executed_tasks"][0]
        task2 = result["executed_tasks"][1]

        self.assertEqual("task-01-multiply", task1["task_id"])
        self.assertEqual("task-02-divide", task2["task_id"])

        # Invariant: Each task receives its own distinct run_id
        self.assertTrue(task1["run_id"].startswith("run-"))
        self.assertTrue(task2["run_id"].startswith("run-"))
        self.assertNotEqual(task1["run_id"], task2["run_id"])

        # Invariant: Both achieve technical PASS in WAITING_PRODUCT_AUTHORITY
        self.assertEqual("WAITING_PRODUCT_AUTHORITY", task1["machine_state"])
        self.assertEqual("PASS", task1["last_audit_result"])
        self.assertEqual("WAITING_PRODUCT_AUTHORITY", task2["machine_state"])
        self.assertEqual("PASS", task2["last_audit_result"])

        # Invariant: approval is None for both (no product approval inferred)
        self.assertIsNone(task1["approval"])
        self.assertIsNone(task2["approval"])

        # Invariant: Commit SHAs are distinct and sequential
        self.assertIsNotNone(task1["last_audited_sha"])
        self.assertIsNotNone(task2["last_audited_sha"])
        self.assertNotEqual(task1["last_audited_sha"], task2["last_audited_sha"])

        # Verify execution record written to reports directory
        exec_file = self.reports_dir / "task-queue-execution.json"
        self.assertTrue(exec_file.is_file())
        self.assertEqual("COMPLETED", json.loads(exec_file.read_text(encoding="utf-8"))["status"])

    def test_queue_cleanup_removes_audit_workspace_without_error(self) -> None:
        builder_script, auditor_script = self._create_actor_scripts()
        config = {
            "repository": str(self.repo),
            "state_path": str(self.state_path),
            "reports_dir": str(self.reports_dir),
            "builder_workspace": str(self.repo),
            "audit_workspaces": str(self.audit_workspaces),
            "builder_command": [sys.executable, str(builder_script)],
            "auditor_command": [sys.executable, str(auditor_script)],
            "max_retries": 3,
            "cleanup_audit_workspaces": True,
        }
        config_path = self.root / "config-cleanup.json"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        queue_path = self.root / "queue-cleanup.json"
        queue_path.write_text(json.dumps({
            "schema_version": "0.1",
            "phase": "O0",
            "tasks": [{
                "task_id": "task-cleanup",
                "goal": "cleanup",
                "scope": ["file.txt"],
                "acceptance_criteria": ["pass"],
                "phase": "O0",
            }],
        }), encoding="utf-8")

        result = run_task_queue(config_path, queue_path, max_steps_per_task=10)
        self.assertEqual("COMPLETED", result["status"])
        self.assertFalse(
            [path for path in self.audit_workspaces.iterdir() if path.name != "state-snapshots"]
        )

    def test_queue_stops_when_task_is_blocked(self) -> None:
        builder_script, _ = self._create_actor_scripts()
        # Auditor script returns ESCALATE for task-01, causing BLOCKED state
        auditor_script = self.root / "auditor_fail.py"
        auditor_script.write_text(
            "import os, pathlib, json\n"
            "report_path = pathlib.Path(os.environ['IDEAS_STANDARD_REPORT'])\n"
            "sha = os.environ.get('IDEAS_STANDARD_AUDIT_TARGET_SHA', '')\n"
            "report = {\n"
            "    'schema_version': '0.1',\n"
            "    'executor_id': 'test-auditor',\n"
            "    'role': 'AUDITOR',\n"
            "    'authority': 'INDEPENDENT_AUDIT',\n"
            "    'audit_result': 'ESCALATE',\n"
            "    'audited_sha': sha,\n"
            "    'summary': 'Escalated',\n"
            "    'escalation_reason': 'Testing escalation reason',\n"
            "    'findings': [{\n"
            "        'id': 'F-1',\n"
            "        'severity': 'CRITICAL',\n"
            "        'blocking': True,\n"
            "        'files': ['.'],\n"
            "        'evidence': 'Critical defect evidence text',\n"
            "        'problem': 'Defect',\n"
            "        'violated_criterion': 'Criteria',\n"
            "        'resolution_condition': 'Fix',\n"
            "    }],\n"
            "    'checks': [{'id': 'chk-01', 'status': 'FAIL', 'evidence': 'Escalation check evidence text'}],\n"
            "    'residual_risks': [],\n"
            "    'gate_registration': 'NOT_AUTHORIZED',\n"
            "}\n"
            "report_path.parent.mkdir(parents=True, exist_ok=True)\n"
            "report_path.write_text(json.dumps(report), encoding='utf-8')\n",
            encoding="utf-8",
        )

        config = {
            "repository": str(self.repo),
            "state_path": str(self.state_path),
            "reports_dir": str(self.reports_dir),
            "builder_workspace": str(self.repo),
            "audit_workspaces": str(self.audit_workspaces),
            "builder_command": [sys.executable, str(builder_script)],
            "auditor_command": [sys.executable, str(auditor_script)],
            "max_retries": 3,
        }
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

        queue = {
            "schema_version": "0.1",
            "phase": "O0",
            "tasks": [
                {
                    "task_id": "task-01-failing",
                    "goal": "Failing goal",
                    "scope": ["file1.txt"],
                    "acceptance_criteria": ["criteria 1"],
                    "phase": "O0",
                },
                {
                    "task_id": "task-02-never-runs",
                    "goal": "Should not run",
                    "scope": ["file2.txt"],
                    "acceptance_criteria": ["criteria 2"],
                    "phase": "O0",
                },
            ],
        }
        queue_path = self.root / "queue.json"
        queue_path.write_text(json.dumps(queue, indent=2), encoding="utf-8")

        result = run_task_queue(config_path, queue_path, max_steps_per_task=10)

        # Invariant: Queue halts on BLOCKED; task 2 is never executed
        self.assertEqual("BLOCKED", result["status"])
        self.assertIn("BLOCKED", result["stop_reason"])
        self.assertEqual(1, len(result["executed_tasks"]))
        self.assertEqual("task-01-failing", result["executed_tasks"][0]["task_id"])
        self.assertEqual("BLOCKED", result["executed_tasks"][0]["machine_state"])

    def test_queue_prohibits_automatic_phase_advancement(self) -> None:
        builder_script, auditor_script = self._create_actor_scripts()
        config = {
            "repository": str(self.repo),
            "state_path": str(self.state_path),
            "reports_dir": str(self.reports_dir),
            "builder_workspace": str(self.repo),
            "audit_workspaces": str(self.audit_workspaces),
            "builder_command": [sys.executable, str(builder_script)],
            "auditor_command": [sys.executable, str(auditor_script)],
            "max_retries": 3,
        }
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

        # Task 2 declares phase S1 in an O0 queue -> prohibited by O0-C29
        queue = {
            "schema_version": "0.1",
            "phase": "O0",
            "tasks": [
                {
                    "task_id": "task-01",
                    "goal": "Goal 1",
                    "scope": ["file1.txt"],
                    "acceptance_criteria": ["criteria 1"],
                    "phase": "O0",
                },
                {
                    "task_id": "task-02-cross-phase",
                    "goal": "Goal 2",
                    "scope": ["file2.txt"],
                    "acceptance_criteria": ["criteria 2"],
                    "phase": "S1",
                },
            ],
        }
        queue_path = self.root / "queue.json"
        queue_path.write_text(json.dumps(queue, indent=2), encoding="utf-8")

        # Must reject queue initialization because task phase violates queue phase under O0-C29
        with self.assertRaisesRegex(HandoffError, "prohibited by O0-C29"):
            run_task_queue(config_path, queue_path)

    def test_queue_rejects_malformed_queue_declaration(self) -> None:
        config = {
            "repository": str(self.repo),
            "state_path": str(self.state_path),
            "reports_dir": str(self.reports_dir),
            "builder_workspace": str(self.repo),
            "audit_workspaces": str(self.audit_workspaces),
            "builder_command": ["cmd"],
            "auditor_command": ["cmd"],
        }
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")

        # Missing goal, scope, criteria
        invalid_queue = {
            "schema_version": "0.1",
            "phase": "O0",
            "tasks": [{"task_id": "task-01", "phase": "O0"}],
        }
        queue_path = self.root / "invalid_queue.json"
        queue_path.write_text(json.dumps(invalid_queue), encoding="utf-8")

        with self.assertRaises(Exception):
            run_task_queue(config_path, queue_path)

    def test_queue_invocation_idempotency_rejects_duplicate_and_preserves_accepted_results(self) -> None:
        builder_script, auditor_script = self._create_actor_scripts()
        config = {
            "repository": str(self.repo),
            "state_path": str(self.state_path),
            "reports_dir": str(self.reports_dir),
            "builder_workspace": str(self.repo),
            "audit_workspaces": str(self.audit_workspaces),
            "builder_command": [sys.executable, str(builder_script)],
            "auditor_command": [sys.executable, str(auditor_script)],
            "max_retries": 3,
        }
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

        invocation_id = "inv-req-12345"
        queue = {
            "schema_version": "0.1",
            "invocation_id": invocation_id,
            "phase": "O0",
            "tasks": [
                {
                    "task_id": "task-01",
                    "goal": "Goal 1",
                    "scope": ["file1.txt"],
                    "acceptance_criteria": ["criteria 1"],
                    "phase": "O0",
                },
            ],
        }
        queue_path = self.root / "queue.json"
        queue_path.write_text(json.dumps(queue, indent=2), encoding="utf-8")

        # 1st Invocation - Processed
        run_task_queue(config_path, queue_path, invocation_id=invocation_id)

        # Update state to ACCEPTED with existing results
        record_file = self.reports_dir / f"task-queue-execution-{invocation_id}.json"
        record_file.write_text(
            json.dumps({"status": "ACCEPTED", "results": ["item1"], "invocation_id": invocation_id}),
            encoding="utf-8",
        )

        # 2nd Invocation - Duplicate attempt
        with self.assertRaises(HandoffError) as ctx:
            run_task_queue(config_path, queue_path, invocation_id=invocation_id)
        self.assertIn("ALREADY_EXISTS", str(ctx.exception))

        # Main invariant validation: previous accepted state and results must NOT be erased
        state = json.loads(record_file.read_text(encoding="utf-8"))
        self.assertEqual("ACCEPTED", state["status"])
        self.assertEqual(["item1"], state["results"])

    def test_interruption_before_final_record_rejects_retry_without_state_mutation(self) -> None:
        builder_script, auditor_script = self._create_actor_scripts()
        config = {
            "repository": str(self.repo),
            "state_path": str(self.state_path),
            "reports_dir": str(self.reports_dir),
            "builder_workspace": str(self.repo),
            "audit_workspaces": str(self.audit_workspaces),
            "builder_command": [sys.executable, str(builder_script)],
            "auditor_command": [sys.executable, str(auditor_script)],
            "max_retries": 3,
        }
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        invocation_id = "inv-interrupted-final-write"
        queue_path = self.root / "queue.json"
        queue_path.write_text(json.dumps({
            "schema_version": "0.1", "invocation_id": invocation_id, "phase": "O0",
            "tasks": [{
                "task_id": "task-01", "goal": "Goal", "scope": ["file1.txt"],
                "acceptance_criteria": ["criterion"], "phase": "O0",
            }],
        }), encoding="utf-8")
        record_path = self.reports_dir / f"task-queue-execution-{invocation_id}.json"

        from scripts import o0_runner
        original_write = o0_runner.write_json

        def interrupt_final_write(path, payload):
            if Path(path) == record_path and payload.get("status") == "COMPLETED":
                raise OSError("simulated interruption before final queue record")
            return original_write(path, payload)

        with patch("scripts.o0_runner.write_json", side_effect=interrupt_final_write):
            with self.assertRaisesRegex(OSError, "simulated interruption"):
                run_task_queue(config_path, queue_path, invocation_id=invocation_id)

        state_before_retry = self.state_path.read_bytes()
        record_before_retry = record_path.read_bytes()
        progress = json.loads(record_before_retry)
        self.assertEqual("IN_PROGRESS", progress["status"])
        self.assertEqual(["task-01"], progress["results"])

        with self.assertRaisesRegex(HandoffError, "INVOCATION_ALREADY_EXISTS"):
            run_task_queue(config_path, queue_path, invocation_id=invocation_id)

        self.assertEqual(state_before_retry, self.state_path.read_bytes())
        self.assertEqual(record_before_retry, record_path.read_bytes())

    def test_queue_service_and_repository_python_api(self) -> None:
        from scripts.o0_queue import QueueRepository, QueueService

        queue_repo = QueueRepository()
        queue_service = QueueService(queue_repo)
        invocation_id = "inv-req-12345"
        payload = {"data": "test-payload"}

        # 1ª Invocação - Aceita e processada (parcial ou total)
        queue_service.invoke(invocation_id, payload)
        queue_repo.update_state(invocation_id, {"status": "ACCEPTED", "results": ["item1"]})

        # 2ª Invocação - Tentativa duplicada
        try:
            queue_service.invoke(invocation_id, payload)
        except Exception as error:
            self.assertIn("ALREADY_EXISTS", str(error))  # Comportamento aceito (Rejeição)

        # Validação principal: o estado anterior NÃO pode ter sido apagado
        state = queue_repo.get_state(invocation_id)
        self.assertIsNotNone(state)
        self.assertEqual("ACCEPTED", state["status"])
        self.assertEqual(["item1"], state["results"])


if __name__ == "__main__":
    unittest.main()
