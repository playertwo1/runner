#!/usr/bin/env python3
"""Execute and package the real Antigravity ↔ Codex correction cycle for O0 v2 M3.

Demonstrates a single-invocation cycle:
  Builder produces SHA A -> Auditor returns FAIL with findings ->
  runner forwards findings -> Builder corrects and produces distinct SHA B ->
  Auditor returns PASS -> transitions to WAITING_PRODUCT_AUTHORITY with approval=null.

Preserves run_id, SHAs, audit rounds, and evidence references.
Generates canonical reports, verifiable evidence envelopes, and standalone git bundle.
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
from scripts.o0_runner import run_loop


def _run_git(args: list[str], cwd: Path) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return res.stdout.strip()


def execute_m3_correction_cycle(
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


    repo_dir = work_root / "disposable-m3-repo"
    if repo_dir.exists():
        shutil.rmtree(repo_dir, ignore_errors=True)
    repo_dir.mkdir(parents=True, exist_ok=True)

    # 1. Setup repository
    _run_git(["init", "-b", "main"], cwd=repo_dir)
    _run_git(["config", "user.name", "O0 M3 Builder"], cwd=repo_dir)
    _run_git(["config", "user.email", "o0-m3@example.invalid"], cwd=repo_dir)
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
    policy_path = SOURCE_ROOT / "orchestration" / "builder-auditor-policy.json"
    state_path = work_root / "orchestrator-state.json"
    reports_dir = work_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    audit_workspaces = work_root / "audit-workspaces"
    audit_workspaces.mkdir(parents=True, exist_ok=True)

    init_state(
        policy_path=policy_path,
        state_path=state_path,
        project_id="o0-v2-m3-correction-cycle",
        phase="O0",
        gate="NONE",
        builder_branch="main",
    )
    initial_state = status(state_path)
    run_id = initial_state["run_id"]
    assert initial_state["machine_state"] == "READY_FOR_BUILD"
    assert initial_state["audit_round"] == 0

    antigravity_adapter = SOURCE_ROOT / "scripts" / "o0_antigravity_adapter.py"
    codex_adapter = SOURCE_ROOT / "scripts" / "o0_codex_adapter.py"

    builder_initial_task = (
        "In the active workspace, add function 'multiply(a: int, b: int) -> int' in calc.py. "
        "For this initial round, implement it as addition 'return a + b' (intentional defect for auditor detection). "
        "In test_calc.py, add 'test_multiply' asserting multiply(2, 2) == 4. "
        "Run tests with 'python -m unittest test_calc.py', stage calc.py and test_calc.py, "
        "and commit with message 'feat(calc): add multiply function with intentional defect'."
    )

    auditor_criteria = (
        "Inspect calc.py and test_calc.py. Verify that function 'multiply(a: int, b: int) -> int' "
        "correctly computes multiplication (a * b) and not addition (a + b). "
        "If multiply returns a + b or uses addition, or if test_multiply only tests 2 and 2, "
        "report a finding with problem 'multiply function implements addition (a + b) instead of multiplication (a * b)' "
        "and return audit_result FAIL. "
        "If multiply correctly uses multiplication (a * b) and tests pass, return audit_result PASS with empty findings."
    )

    runner_config = {
        "repository": str(repo_dir),
        "state_path": str(state_path),
        "reports_dir": str(reports_dir),
        "builder_workspace": str(repo_dir),
        "audit_workspaces": str(audit_workspaces),
        "builder_command": [sys.executable, str(antigravity_adapter), "--task", builder_initial_task],
        "auditor_command": [sys.executable, str(codex_adapter), "--task", auditor_criteria],
        "max_retries": 3,
    }

    config_file = work_root / "runner-config.json"
    config_file.write_text(json.dumps(runner_config, indent=2), encoding="utf-8")

    # 3. Execute the single-invocation loop with real CLIs
    t_start = time.monotonic()
    final_state = run_loop(config_file, max_steps=10)
    cycle_duration = time.monotonic() - t_start

    # 4. Verify post-cycle assertions
    assert final_state["machine_state"] == "WAITING_PRODUCT_AUTHORITY", f"Expected WAITING_PRODUCT_AUTHORITY, got {final_state['machine_state']}"
    assert final_state["next_actor"] == "PRODUCT_AUTHORITY", f"Expected PRODUCT_AUTHORITY, got {final_state['next_actor']}"
    assert final_state["last_audit_result"] == "PASS", f"Expected PASS, got {final_state['last_audit_result']}"
    assert final_state["approval"] is None, "approval must remain null (no automatic human gate registration)"
    assert final_state["human_gate_required"] is True, "human_gate_required must be True"
    assert final_state["run_id"] == run_id, "run_id must be preserved throughout cycle"
    assert final_state["audit_round"] == 2, f"Expected audit_round 2, got {final_state['audit_round']}"

    # Read the completed operations from operations directory
    raw_ops = [
        p for p in (reports_dir / "operations").glob("op-*.json")
        if not p.name.endswith(".report.json") and not p.name.endswith(".journal.json")
    ]
    operations = sorted(raw_ops, key=lambda p: load_json(p)["result"]["updated_at"])
    assert len(operations) >= 4, f"Expected at least 4 operations, found {len(operations)}"

    step_records = []
    for op_path in operations:
        rep_path = op_path.with_name(op_path.stem + ".report.json")
        op_data = load_json(op_path)
        rep_data = load_json(rep_path)
        step_records.append({
            "operation_id": op_data["operation_id"],
            "actor": op_data["actor"],
            "source_state": op_data["source"]["machine_state"],
            "result_state": op_data["result"]["machine_state"],
            "target_sha": op_data.get("source", {}).get("audit_target_sha") or op_data.get("result", {}).get("audit_target_sha"),
            "result_sha": rep_data.get("result_sha") or rep_data.get("audited_sha"),
            "status": rep_data.get("result") or rep_data.get("audit_result"),
            "updated_at": op_data["result"]["updated_at"],
        })

    # Step 0: Builder produces SHA A
    sha_a = step_records[0]["result_sha"]
    assert sha_a != base_sha, "SHA A must be different from base SHA"

    # Step 1: Auditor FAIL on SHA A
    assert step_records[1]["actor"] == "AUDITOR"
    assert step_records[1]["target_sha"] == sha_a
    assert step_records[1]["status"] == "FAIL"

    # Step 2: Builder produces SHA B
    sha_b = step_records[2]["result_sha"]
    assert sha_b not in {base_sha, sha_a}, "SHA B must be distinct from base SHA and SHA A"

    # Step 3: Auditor PASS on SHA B
    assert step_records[3]["actor"] == "AUDITOR"
    assert step_records[3]["target_sha"] == sha_b
    assert step_records[3]["status"] == "PASS"

    # Verify frozen checkouts
    checkout_a = audit_workspaces / sha_a
    checkout_b = audit_workspaces / sha_b
    assert checkout_a.is_dir(), f"Checkout for SHA A missing at {checkout_a}"
    assert checkout_b.is_dir(), f"Checkout for SHA B missing at {checkout_b}"
    assert _run_git(["status", "--porcelain"], cwd=checkout_a) == "", "Checkout A was modified"
    assert _run_git(["status", "--porcelain"], cwd=checkout_b) == "", "Checkout B was modified"

    # 5. Build self-sufficient standalone Git bundle
    bundle_path = package_dir / "builder.bundle"
    _run_git(["bundle", "create", str(bundle_path), "HEAD"], cwd=repo_dir)

    # Verify bundle is self-sufficient
    p_verify = subprocess.run(["git", "bundle", "verify", str(bundle_path)], capture_output=True, text=True, check=True)
    bundle_verify_output = p_verify.stdout.strip() or p_verify.stderr.strip()
    assert "The bundle records a complete history" in bundle_verify_output, f"Bundle is not self-sufficient: {bundle_verify_output}"

    # Verify standalone clone of the bundle
    with tempfile.TemporaryDirectory(prefix="m3_bundle_clone_") as clone_tmp:
        clone_dest = Path(clone_tmp) / "cloned_repo"
        subprocess.run(["git", "clone", str(bundle_path), str(clone_dest)], check=True, capture_output=True)
        cloned_head = _run_git(["rev-parse", "HEAD"], cwd=clone_dest)
        assert cloned_head == sha_b, f"Cloned HEAD {cloned_head} does not match SHA B {sha_b}"
        # Verify unit tests pass in cloned repository
        subprocess.run([sys.executable, "-m", "unittest", "test_calc.py"], cwd=clone_dest, check=True, capture_output=True)

    # 6. Package reports and evidence
    # History of reports
    builder_r0_report = load_json(operations[0].with_name(operations[0].stem + ".report.json"))
    audit_r1_report = load_json(operations[1].with_name(operations[1].stem + ".report.json"))
    builder_r1_report = load_json(operations[2].with_name(operations[2].stem + ".report.json"))
    audit_r2_report = load_json(operations[3].with_name(operations[3].stem + ".report.json"))

    (package_dir / "builder-report-round0.json").write_text(json.dumps(builder_r0_report, indent=2), encoding="utf-8")
    (package_dir / "audit-report-round1-fail.json").write_text(json.dumps(audit_r1_report, indent=2), encoding="utf-8")
    (package_dir / "builder-findings.json").write_text((reports_dir / "builder-findings.json").read_text(encoding="utf-8"), encoding="utf-8")
    (package_dir / "builder-report-round1-fix.json").write_text(json.dumps(builder_r1_report, indent=2), encoding="utf-8")
    (package_dir / "reaudit-handoff.json").write_text((reports_dir / "reaudit-handoff.json").read_text(encoding="utf-8"), encoding="utf-8")
    (package_dir / "audit-report-round2-pass.json").write_text(json.dumps(audit_r2_report, indent=2), encoding="utf-8")
    (package_dir / "final-orchestrator-state.json").write_text(state_path.read_text(encoding="utf-8"), encoding="utf-8")



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

    # 7. Assemble canonical M3 evidence document
    evidence_payload: dict[str, Any] = {
        "schema_version": "0.1",
        "milestone": "O0_V2_M3",
        "scenario": "real-cli-correction-cycle-single-invocation",
        "description": "Full Builder -> Auditor FAIL -> Builder fix -> Auditor PASS cycle executed in a single run_loop call with real Antigravity and Codex CLIs.",
        "cycle_status": "COMPLETED",
        "cycle_duration_seconds": round(cycle_duration, 2),
        "run_id": run_id,
        "base_sha": base_sha,
        "sha_a": sha_a,
        "sha_b": sha_b,
        "shas_distinct": sha_a != sha_b and sha_a != base_sha and sha_b != base_sha,
        "audit_rounds_total": final_state["audit_round"],
        "max_audit_rounds": final_state["max_audit_rounds"],
        "final_state": {
            "machine_state": final_state["machine_state"],
            "next_actor": final_state["next_actor"],
            "last_audit_result": final_state["last_audit_result"],
            "audit_round": final_state["audit_round"],
            "max_audit_rounds": final_state["max_audit_rounds"],
            "builder_head_sha": final_state["builder_head_sha"],
            "audit_target_sha": final_state["audit_target_sha"],
            "last_audited_sha": final_state["last_audited_sha"],
            "approval": final_state["approval"],
            "human_gate_required": final_state["human_gate_required"],
            "gate": final_state["gate"],
            "phase": final_state["phase"],
        },
        "cycle_steps": step_records,
        "bundle": {
            "path": str(bundle_path.relative_to(package_dir.parent)).replace("\\", "/"),
            "sha256": hashlib.sha256(bundle_path.read_bytes()).hexdigest(),
            "size_bytes": bundle_path.stat().st_size,
            "verification_status": "SELF_SUFFICIENT",
            "verify_output": bundle_verify_output,
        },
        "frozen_checkouts": {
            "sha_a": {
                "path": str(checkout_a.relative_to(work_root)).replace("\\", "/"),
                "head": _run_git(["rev-parse", "HEAD"], cwd=checkout_a),
                "clean": True,
            },
            "sha_b": {
                "path": str(checkout_b.relative_to(work_root)).replace("\\", "/"),
                "head": _run_git(["rev-parse", "HEAD"], cwd=checkout_b),
                "clean": True,
            },
        },
        "evidence_references": evidence_files,
        "operations_records": operations_map,
        "agents": {
            "builder": {
                "cli": "Antigravity CLI (agy)",
                "adapter": str(antigravity_adapter.relative_to(SOURCE_ROOT)).replace("\\", "/"),
                "authority": "IMPLEMENTATION",
            },
            "auditor": {
                "cli": "OpenAI Codex CLI (codex)",
                "adapter": str(codex_adapter.relative_to(SOURCE_ROOT)).replace("\\", "/"),
                "authority": "INDEPENDENT_AUDIT",
            },
        },
        "invariants_verified": [
            "Single invocation via run_loop() coordinates multi-step transitions",
            "Builder produces initial commit SHA A",
            "Auditor returns FAIL with structured blocking findings for SHA A",
            "Runner automatically forwards findings via builder-findings.json and IDEAS_STANDARD_FINDINGS",
            "Builder corrects the defect, verifies unit tests, and commits distinct SHA B",
            "Runner validates SHA B != SHA A and prepares reaudit-handoff.json",
            "Auditor re-audits SHA B in frozen checkout and returns PASS with zero findings",
            "Runner transitions to WAITING_PRODUCT_AUTHORITY with approval=null and human_gate_required=true",
            "run_id and SHAs are preserved across all operations",
            "Audit rounds are tracked and bounded by max_audit_rounds=3",
            "builder.bundle is self-sufficient and clones standalone with passing tests",
            "Zero automatic gate approval or phase advancement",
        ],
    }

    evidence_json_path.write_text(json.dumps(evidence_payload, indent=2), encoding="utf-8")
    return evidence_payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, default=Path(r"C:\Users\fael\AppData\Local\Temp\o0_v2_m3_work"))
    parser.add_argument("--output", type=Path, default=SOURCE_ROOT / "O0_V2_M3_EVIDENCE.json")
    parser.add_argument("--package-dir", type=Path, default=SOURCE_ROOT / "O0_V2_M3_EVIDENCE_PACKAGE")
    args = parser.parse_args()

    print(f"Executing O0 v2 M3 correction loop in {args.work_root}...")
    evidence = execute_m3_correction_cycle(args.work_root, args.output, args.package_dir)
    print(f"PASS: O0 v2 M3 correction cycle completed in {evidence['cycle_duration_seconds']}s")
    print(f"Evidence written to {args.output}")
    print(f"Package created at {args.package_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
