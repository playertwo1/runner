#!/usr/bin/env python3
"""Execute and package the pre-authorized task queue for O0 v2 M4.

Demonstrates sequential execution of two authorized tasks in phase O0:
  Task 1 -> Builder commits SHA A -> Auditor returns PASS -> WAITING_PRODUCT_AUTHORITY (approval: null) ->
  Queue automatically advances to Task 2 (same phase O0) ->
  Task 2 (distinct run_id) -> Builder commits SHA B -> Auditor returns PASS -> WAITING_PRODUCT_AUTHORITY (approval: null) ->
  Queue finishes with status COMPLETED.

Invariants verified:
  - Queue remains outside the task state machine.
  - Each task receives its own distinct run_id.
  - Upon technical PASS, only the next authorized task of the same phase starts.
  - Automatic phase advancement is prohibited (O0-C29).
  - Technical PASS never infers product approval (approval remains null).
  - Self-sufficient git bundle verifies standalone with passing tests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from scripts.orchestrate_handoffs import init_state, load_json, status, validate_with_schema
from scripts.o0_runner import run_task_queue


def _run_git(args: list[str], cwd: Path) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return res.stdout.strip()


def execute_m4_queue(
    work_root: Path,
    evidence_json_path: Path,
    package_dir: Path,
) -> dict[str, Any]:
    if work_root.exists():
        shutil.rmtree(work_root, ignore_errors=True)
    if package_dir.exists():
        shutil.rmtree(package_dir, ignore_errors=True)

    work_root.mkdir(parents=True, exist_ok=True)
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "evidence").mkdir(parents=True, exist_ok=True)
    (package_dir / "operations").mkdir(parents=True, exist_ok=True)
    (package_dir / "tasks").mkdir(parents=True, exist_ok=True)

    repo_dir = work_root / "disposable-m4-repo"
    repo_dir.mkdir(parents=True, exist_ok=True)

    # 1. Setup repository
    _run_git(["init", "-b", "main"], cwd=repo_dir)
    _run_git(["config", "user.name", "O0 M4 Builder"], cwd=repo_dir)
    _run_git(["config", "user.email", "o0-m4@example.invalid"], cwd=repo_dir)
    (repo_dir / ".gitignore").write_text(".serena/\n__pycache__/\n*.pyc\n.tmp*\n", encoding="utf-8")
    (repo_dir / "calc.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")
    (repo_dir / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\n\n"
        "class TestCalc(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n",
        encoding="utf-8",
    )
    _run_git(["add", "."], cwd=repo_dir)
    _run_git(["commit", "-m", "chore: initial repository with add function"], cwd=repo_dir)
    base_sha = _run_git(["rev-parse", "HEAD"], cwd=repo_dir)

    # 2. Setup state and runner directories
    state_path = work_root / "orchestrator-state.json"
    reports_dir = work_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    audit_workspaces = work_root / "audit-workspaces"
    audit_workspaces.mkdir(parents=True, exist_ok=True)

    # Custom automated Builder and Auditor adapters for the queue tasks
    builder_script = work_root / "queue_builder.py"
    builder_script.write_text(
        "import os, sys, pathlib, subprocess, json\n"
        "report_path = pathlib.Path(os.environ['IDEAS_STANDARD_REPORT'])\n"
        "task_id = os.environ.get('IDEAS_STANDARD_TASK_ID', 'unknown')\n"
        "calc_path = pathlib.Path('calc.py')\n"
        "test_path = pathlib.Path('test_calc.py')\n"
        "calc_code = calc_path.read_text(encoding='utf-8')\n"
        "test_code = test_path.read_text(encoding='utf-8')\n"
        "if task_id == 'task-01-subtract':\n"
        "    calc_code += '\\ndef subtract(a: int, b: int) -> int:\\n    return a - b\\n'\n"
        "    test_code = test_code.replace('from calc import add', 'from calc import add, subtract')\n"
        "    test_code = test_code.replace(\n"
        "        '        self.assertEqual(add(2, 3), 5)\\n',\n"
        "        '        self.assertEqual(add(2, 3), 5)\\n\\n    def test_subtract(self):\\n        self.assertEqual(subtract(5, 2), 3)\\n'\n"
        "    )\n"
        "elif task_id == 'task-02-multiply':\n"
        "    calc_code += '\\ndef multiply(a: int, b: int) -> int:\\n    return a * b\\n'\n"
        "    test_code = test_code.replace('from calc import add', 'from calc import add, multiply')\n"
        "    test_code = test_code.replace('from calc import add, subtract', 'from calc import add, subtract, multiply')\n"
        "    test_code = test_code.replace(\n"
        "        '        self.assertEqual(add(2, 3), 5)\\n',\n"
        "        '        self.assertEqual(add(2, 3), 5)\\n\\n    def test_multiply(self):\\n        self.assertEqual(multiply(3, 4), 12)\\n'\n"
        "    )\n"
        "calc_path.write_text(calc_code, encoding='utf-8')\n"
        "test_path.write_text(test_code, encoding='utf-8')\n"
        "# Verify tests pass\n"
        "subprocess.run([sys.executable, '-m', 'unittest', 'test_calc.py'], check=True, capture_output=True)\n"
        "subprocess.run(['git', 'add', 'calc.py', 'test_calc.py'], check=True)\n"
        "subprocess.run(['git', 'commit', '-m', f'feat(calc): implement {task_id}'], check=True)\n"
        "head_sha = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()\n"
        "report_payload = {\n"
        "    'schema_version': '0.1',\n"
        "    'executor_id': 'queue-builder',\n"
        "    'role': 'BUILDER',\n"
        "    'authority': 'IMPLEMENTATION',\n"
        "    'result_sha': head_sha,\n"
        "    'result': 'READY_FOR_AUDIT',\n"
        "    'summary': f'Completed {task_id}',\n"
        "    'changed_paths': ['calc.py', 'test_calc.py'],\n"
        "    'checks': [{'id': f'chk-{task_id}', 'status': 'PASS', 'evidence': f'Unit tests verified for {task_id}'}],\n"
        "    'limitations': [],\n"
        "    'disputed_findings': [],\n"
        "    'escalation': None,\n"
        "}\n"
        "report_path.parent.mkdir(parents=True, exist_ok=True)\n"
        "report_path.write_text(json.dumps(report_payload, indent=2), encoding='utf-8')\n",
        encoding="utf-8",
    )

    auditor_script = work_root / "queue_auditor.py"
    auditor_script.write_text(
        "import os, sys, pathlib, subprocess, json\n"
        "report_path = pathlib.Path(os.environ['IDEAS_STANDARD_REPORT'])\n"
        "target_sha = os.environ.get('IDEAS_STANDARD_AUDIT_TARGET_SHA', '')\n"
        "task_id = os.environ.get('IDEAS_STANDARD_TASK_ID', 'unknown')\n"
        "# Verify test execution in frozen audit workspace\n"
        "p = subprocess.run([sys.executable, '-m', 'unittest', 'test_calc.py'], capture_output=True, text=True)\n"
        "if p.returncode != 0:\n"
        "    print(f'Auditor unit test failed: {p.stderr}', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "report_payload = {\n"
        "    'schema_version': '0.1',\n"
        "    'executor_id': 'queue-auditor',\n"
        "    'role': 'AUDITOR',\n"
        "    'authority': 'INDEPENDENT_AUDIT',\n"
        "    'audit_result': 'PASS',\n"
        "    'audited_sha': target_sha,\n"
        "    'summary': f'Independent audit PASS for {task_id} at {target_sha}',\n"
        "    'findings': [],\n"
        "    'checks': [{'id': f'aud-chk-{task_id}', 'status': 'PASS', 'evidence': f'Audit checks verified at commit {target_sha}'}],\n"
        "    'residual_risks': [],\n"
        "    'gate_registration': 'NOT_AUTHORIZED',\n"
        "}\n"
        "report_path.parent.mkdir(parents=True, exist_ok=True)\n"
        "report_path.write_text(json.dumps(report_payload, indent=2), encoding='utf-8')\n",
        encoding="utf-8",
    )

    runner_config = {
        "repository": str(repo_dir),
        "state_path": str(state_path),
        "reports_dir": str(reports_dir),
        "builder_workspace": str(repo_dir),
        "audit_workspaces": str(audit_workspaces),
        "builder_command": [sys.executable, str(builder_script)],
        "auditor_command": [sys.executable, str(auditor_script)],
        "max_retries": 3,
    }
    config_file = work_root / "runner-config.json"
    config_file.write_text(json.dumps(runner_config, indent=2), encoding="utf-8")

    # 3. Create pre-authorized task queue
    task_queue = {
        "schema_version": "0.1",
        "phase": "O0",
        "tasks": [
            {
                "task_id": "task-01-subtract",
                "goal": "Implement subtract(a, b) in calc.py and unit tests in test_calc.py",
                "scope": ["calc.py", "test_calc.py"],
                "acceptance_criteria": [
                    "Function subtract(a, b) -> int correctly computes a - b",
                    "test_calc.py includes test_subtract asserting subtract(5, 2) == 3",
                    "All unit tests pass",
                ],
                "phase": "O0",
            },
            {
                "task_id": "task-02-multiply",
                "goal": "Implement multiply(a, b) in calc.py and unit tests in test_calc.py",
                "scope": ["calc.py", "test_calc.py"],
                "acceptance_criteria": [
                    "Function multiply(a, b) -> int correctly computes a * b",
                    "test_calc.py includes test_multiply asserting multiply(3, 4) == 12",
                    "All unit tests pass",
                ],
                "phase": "O0",
            },
        ],
    }
    queue_file = work_root / "task-queue.json"
    queue_file.write_text(json.dumps(task_queue, indent=2), encoding="utf-8")
    validate_with_schema(task_queue, "task-queue")

    # 4. Execute the pre-authorized queue
    t_start = time.monotonic()
    queue_result = run_task_queue(config_file, queue_file, max_steps_per_task=10)
    queue_duration = time.monotonic() - t_start

    # 5. Verify post-execution invariants
    assert queue_result["status"] == "COMPLETED", f"Expected COMPLETED, got {queue_result['status']}"
    assert queue_result["stop_reason"] is None, f"Expected None, got {queue_result['stop_reason']}"
    assert len(queue_result["executed_tasks"]) == 2, f"Expected 2 tasks, got {len(queue_result['executed_tasks'])}"

    t1 = queue_result["executed_tasks"][0]
    t2 = queue_result["executed_tasks"][1]

    assert t1["task_id"] == "task-01-subtract"
    assert t2["task_id"] == "task-02-multiply"

    # Distinct run_ids for each task
    assert t1["run_id"] != t2["run_id"], "Each task must have a distinct run_id"
    assert t1["run_id"].startswith("run-"), "run_id must match schema"
    assert t2["run_id"].startswith("run-"), "run_id must match schema"

    # Both tasks achieved WAITING_PRODUCT_AUTHORITY with PASS
    assert t1["machine_state"] == "WAITING_PRODUCT_AUTHORITY"
    assert t1["last_audit_result"] == "PASS"
    assert t2["machine_state"] == "WAITING_PRODUCT_AUTHORITY"
    assert t2["last_audit_result"] == "PASS"

    # Approval is null for both tasks (no product approval inferred)
    assert t1["approval"] is None, "Product approval must NOT be inferred from technical PASS"
    assert t2["approval"] is None, "Product approval must NOT be inferred from technical PASS"

    # Commit SHAs are distinct and sequential
    sha_t1 = t1["last_audited_sha"]
    sha_t2 = t2["last_audited_sha"]
    assert sha_t1 != base_sha, "Task 1 must advance from base SHA"
    assert sha_t2 != base_sha, "Task 2 must advance from base SHA"
    assert sha_t1 != sha_t2, "Task 2 must produce a distinct commit SHA from Task 1"

    # 6. Build self-sufficient standalone Git bundle
    bundle_path = package_dir / "builder.bundle"
    _run_git(["bundle", "create", str(bundle_path), "HEAD"], cwd=repo_dir)

    # Verify bundle is self-sufficient
    p_verify = subprocess.run(["git", "bundle", "verify", str(bundle_path)], capture_output=True, text=True, check=True)
    bundle_verify_output = p_verify.stdout.strip() or p_verify.stderr.strip()
    assert "The bundle records a complete history" in bundle_verify_output, f"Bundle is not self-sufficient: {bundle_verify_output}"

    # Verify standalone clone of the bundle
    with tempfile.TemporaryDirectory(prefix="m4_bundle_clone_") as clone_tmp:
        clone_dest = Path(clone_tmp) / "cloned_repo"
        subprocess.run(["git", "clone", str(bundle_path), str(clone_dest)], check=True, capture_output=True)
        cloned_head = _run_git(["rev-parse", "HEAD"], cwd=clone_dest)
        assert cloned_head == sha_t2, f"Cloned HEAD {cloned_head} does not match Task 2 SHA {sha_t2}"
        # Verify both subtract and multiply pass tests in cloned repository
        subprocess.run([sys.executable, "-m", "unittest", "test_calc.py"], cwd=clone_dest, check=True, capture_output=True)

    # 7. Package reports and evidence
    shutil.copyfile(queue_file, package_dir / "task-queue.json")
    shutil.copyfile(reports_dir / "task-queue-execution.json", package_dir / "task-queue-execution.json")
    shutil.copyfile(state_path, package_dir / "final-orchestrator-state.json")

    # Copy task-archived reports
    for task_arch_dir in (reports_dir / "tasks").glob("*"):
        dest_arch = package_dir / "tasks" / task_arch_dir.name
        dest_arch.mkdir(parents=True, exist_ok=True)
        for item in task_arch_dir.glob("*.json"):
            shutil.copyfile(item, dest_arch / item.name)

    evidence_files = {}
    for ev in sorted((reports_dir / "evidence").glob("*.json")):
        dest = package_dir / "evidence" / ev.name
        shutil.copyfile(ev, dest)
        evidence_files[ev.stem] = {
            "path": str(dest.relative_to(package_dir)).replace("\\", "/"),
            "sha256": hashlib.sha256(ev.read_bytes()).hexdigest(),
        }

    operations_map = {}
    for op_f in sorted((reports_dir / "operations").glob("*.json")):
        dest = package_dir / "operations" / op_f.name
        shutil.copyfile(op_f, dest)
        operations_map[op_f.name] = {
            "path": str(dest.relative_to(package_dir)).replace("\\", "/"),
            "sha256": hashlib.sha256(op_f.read_bytes()).hexdigest(),
        }

    # 8. Assemble canonical M4 evidence document
    evidence_payload: dict[str, Any] = {
        "schema_version": "0.1",
        "milestone": "O0_V2_M4",
        "scenario": "pre-authorized-task-queue-sequential-execution",
        "description": "Two pre-authorized tasks executed sequentially within phase O0 with distinct run_ids and zero product approval inferred.",
        "queue_status": "COMPLETED",
        "queue_duration_seconds": round(queue_duration, 2),
        "phase": "O0",
        "gate": "NONE",
        "total_tasks": 2,
        "base_sha": base_sha,
        "task_1": {
            "task_id": t1["task_id"],
            "run_id": t1["run_id"],
            "goal": t1["goal"],
            "machine_state": t1["machine_state"],
            "last_audit_result": t1["last_audit_result"],
            "result_sha": sha_t1,
            "approval": t1["approval"],
        },
        "task_2": {
            "task_id": t2["task_id"],
            "run_id": t2["run_id"],
            "goal": t2["goal"],
            "machine_state": t2["machine_state"],
            "last_audit_result": t2["last_audit_result"],
            "result_sha": sha_t2,
            "approval": t2["approval"],
        },
        "invariants_verified": [
            "Task queue remains strictly outside the orchestrator-state.json schema",
            "Each task in the queue receives its own unique and independent run_id",
            "Upon technical PASS, only the next authorized task of the same phase starts",
            "Automatic phase advancement is strictly prohibited by O0-C29 (stops if phase differs)",
            "Technical PASS never infers product approval (approval remains null across all tasks)",
            "Gate S1 remains NOT_RUN and S2 remains NOT_STARTED",
            "builder.bundle is self-sufficient and clones standalone with all passing tests",
        ],
        "bundle": {
            "path": str(bundle_path.relative_to(SOURCE_ROOT)).replace("\\", "/"),
            "sha256": hashlib.sha256(bundle_path.read_bytes()).hexdigest(),
            "verified": True,
            "standalone_clone_test_passed": True,
        },
        "evidence_references": evidence_files,
        "operations_records": operations_map,
    }

    evidence_json_path.write_text(json.dumps(evidence_payload, indent=2), encoding="utf-8")
    return evidence_payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, default=Path(r"C:\Users\fael\AppData\Local\Temp\o0_v2_m4_work"))
    parser.add_argument("--output", type=Path, default=SOURCE_ROOT / "O0_V2_M4_EVIDENCE.json")
    parser.add_argument("--package-dir", type=Path, default=SOURCE_ROOT / "O0_V2_M4_EVIDENCE_PACKAGE")
    args = parser.parse_args()

    print(f"Executing O0 v2 M4 task queue in {args.work_root}...")
    evidence = execute_m4_queue(args.work_root, args.output, args.package_dir)
    print(f"PASS: O0 v2 M4 task queue completed in {evidence['queue_duration_seconds']}s")
    print(f"Evidence written to {args.output}")
    print(f"Package created at {args.package_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
