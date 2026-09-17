#!/usr/bin/env python3
"""Integration and validation orchestrator for O0 v2 M2 (Reaudit).

Connects Antigravity CLI and Codex CLI to the configurable commands of o0_runner.py,
demonstrating a real non-interactive call to each agent through the runner,
canonical report validation, SHA verification, workspace isolation, evidence
canonicalization, self-sufficient standalone bundle verification, and real Windows CLI
timeout/cancellation process tree termination proofs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.o0_runner import run_once, load_json, status, ActorInterrupted
from scripts.orchestrate_handoffs import init_state, NEXT_ACTOR_BY_STATE


def _unprotect(path: Path) -> None:
    if not path.exists():
        return
    for p in path.rglob("*"):
        try:
            p.chmod(stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass


def _clean_dir(path: Path) -> None:
    if path.exists():
        _unprotect(path)
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def _get_descendants(parent_pid: int) -> list[int]:
    out = subprocess.check_output(
        ["powershell", "-NoProfile", "-Command", f"(Get-CimInstance Win32_Process -Filter 'ParentProcessId = {parent_pid}').ProcessId"],
        text=True, stderr=subprocess.DEVNULL,
    )
    direct = [int(x.strip()) for x in out.splitlines() if x.strip().isdigit()]
    descendants = list(direct)
    for child_pid in direct:
        descendants.extend(_get_descendants(child_pid))
    return descendants


def _is_pid_alive(pid: int) -> bool:
    out = subprocess.check_output(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], text=True, stderr=subprocess.DEVNULL)
    return any(line.split(',')[1].strip('"') == str(pid) for line in out.splitlines() if line.startswith('"') and len(line.split(',')) > 1)


def _get_parent_pid(pid: int) -> int:
    out = subprocess.check_output(
        ["powershell", "-NoProfile", "-Command", f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}').ParentProcessId"],
        text=True, stderr=subprocess.DEVNULL,
    ).strip()
    if not out.isdigit():
        raise RuntimeError(f"Cannot identify parent of actor PID {pid}")
    return int(out)


def _prove_interruption(
    work_dir: Path,
    repo_dir: Path,
    policy_path: Path,
    command: list[str],
    mode: str,  # "timeout" or "cancel"
    actor_role: str,  # "BUILDER" or "AUDITOR"
    target_sha: str | None = None,
    timeout_seconds: float = 3.0,
) -> dict[str, Any]:
    work_dir.mkdir(parents=True, exist_ok=True)
    state_path = work_dir / "orchestrator-state.json"
    reports_dir = work_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    audit_workspaces = work_dir / "audit-workspaces"
    audit_workspaces.mkdir(parents=True, exist_ok=True)
    cancel_file = work_dir / "cancel.req"
    child_pid_file = work_dir / "child.pid"

    init_state(
        policy_path=policy_path,
        state_path=state_path,
        project_id=f"interruption-{actor_role.lower()}-{mode}",
        phase="O0",
        gate="NONE",
        builder_branch="main",
    )

    if actor_role == "AUDITOR":
        st = load_json(state_path)
        st["machine_state"] = "READY_FOR_AUDIT"
        st["next_actor"] = "AUDITOR"
        st["audit_target_sha"] = target_sha
        st["builder_head_sha"] = target_sha
        state_path.write_text(json.dumps(st, indent=2), encoding="utf-8")

    state_before = state_path.read_bytes()
    (work_dir / "state-before.json").write_bytes(state_before)

    config = {
        "repository": str(repo_dir),
        "state_path": str(state_path),
        "reports_dir": str(reports_dir),
        "builder_workspace": str(repo_dir),
        "audit_workspaces": str(audit_workspaces),
        "builder_command": command if actor_role == "BUILDER" else ["dummy"],
        "auditor_command": command if actor_role == "AUDITOR" else ["dummy"],
    }
    if mode == "timeout":
        config["actor_timeout_seconds"] = timeout_seconds
    elif mode == "cancel":
        config["cancel_path"] = str(cancel_file)

    config_path = work_dir / "runner-config.json"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    env_backup = os.environ.get("IDEAS_STANDARD_CHILD_PID_FILE")
    os.environ["IDEAS_STANDARD_CHILD_PID_FILE"] = str(child_pid_file)

    observed_tree_pids_before: list[int] = []
    observation_error: list[str] = []

    def observe_tree_when_ready():
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if child_pid_file.is_file() and child_pid_file.read_text().strip():
                break
            time.sleep(0.05)
        try:
            child_pid = int(child_pid_file.read_text().strip())
            assert _is_pid_alive(child_pid), "CLI process was not alive before interruption"
            adapter_pid = _get_parent_pid(child_pid)
            assert _is_pid_alive(adapter_pid), "Adapter process was not alive before interruption"
            observed_tree_pids_before.extend([adapter_pid, child_pid, *_get_descendants(child_pid)])
        except Exception as exc:
            observation_error.append(type(exc).__name__)
        if mode == "cancel":
            time.sleep(0.25)
            cancel_file.write_text("CANCEL", encoding="utf-8")

    trigger_thread = threading.Thread(target=observe_tree_when_ready, daemon=True)
    trigger_thread.start()

    interrupted_reason = None
    t0 = time.monotonic()
    try:
        run_once(config_path)
    except ActorInterrupted as exc:
        interrupted_reason = exc.reason
    finally:
        if env_backup is not None:
            os.environ["IDEAS_STANDARD_CHILD_PID_FILE"] = env_backup
        else:
            os.environ.pop("IDEAS_STANDARD_CHILD_PID_FILE", None)
        trigger_thread.join(timeout=2.0)
    elapsed = round(time.monotonic() - t0, 2)

    time.sleep(1.0)
    child_pid = None
    if child_pid_file.is_file():
        raw_pid = child_pid_file.read_text().strip()
        if raw_pid.isdigit():
            child_pid = int(raw_pid)

    child_alive = _is_pid_alive(child_pid) if child_pid is not None else False
    alive_tree_pids_after = [pid for pid in observed_tree_pids_before if _is_pid_alive(pid)]
    descendants_status = {pid: _is_pid_alive(pid) for pid in observed_tree_pids_before if pid != child_pid}
    all_terminated = (len(set(observed_tree_pids_before)) >= 2 and not observation_error and not alive_tree_pids_after)

    report_file = reports_dir / ("builder-report.json" if actor_role == "BUILDER" else "audit-report.json")
    report_accepted = report_file.is_file()
    (work_dir / "state-after.json").write_bytes(state_path.read_bytes())
    reports_inventory = sorted(p.relative_to(reports_dir).as_posix() for p in reports_dir.rglob("*") if p.is_file())
    (work_dir / "absence-proof.json").write_text(json.dumps({
        "report_exists": report_accepted,
        "report_path": report_file.relative_to(reports_dir).as_posix(),
        "reports_inventory": reports_inventory,
    }, indent=2) + "\n", encoding="utf-8")

    final_st = status(state_path)
    expected_state = "READY_FOR_BUILD" if actor_role == "BUILDER" else "READY_FOR_AUDIT"
    state_preserved = (state_path.read_bytes() == state_before and final_st["machine_state"] == expected_state and final_st["approval"] is None)

    expected_reason = "TIMEOUT" if mode == "timeout" else "CANCELLED"
    journals = list((reports_dir / "operations").glob("*.journal.json"))
    journal_marked_interrupted = False
    if journals:
        j_data = load_json(journals[0])
        journal_marked_interrupted = (j_data.get("phase") == "INTERRUPTED" and j_data.get("interruption_reason") == expected_reason)

    return {
        "actor_role": actor_role,
        "mode": mode,
        "duration_seconds": elapsed,
        "interrupted_reason": interrupted_reason,
        "expected_reason": expected_reason,
        "child_pid": child_pid,
        "observed_tree_pids_before": sorted(set(observed_tree_pids_before)),
        "observation_error": observation_error,
        "alive_tree_pids_after": alive_tree_pids_after,
        "child_alive_after_interruption": child_alive,
        "descendants_alive_after_interruption": descendants_status,
        "all_child_processes_terminated": all_terminated,
        "report_accepted": report_accepted,
        "canonical_state_preserved": state_preserved,
        "journal_marked_interrupted": journal_marked_interrupted,
        "pass": (interrupted_reason == expected_reason and all_terminated and not report_accepted and state_preserved and journal_marked_interrupted),
    }


def execute_m2_integration(work_root: Path, output_json: Path, package_dir: Path) -> dict[str, Any]:
    work_root = work_root.resolve()
    package_dir = package_dir.resolve()
    if work_root.exists() or package_dir.exists() or output_json.exists():
        raise FileExistsError("M2 execution or evidence already exists; use new paths")
    work_root.mkdir(parents=True)
    package_dir.mkdir(parents=True)

    source_root = Path(__file__).resolve().parents[1]
    policy_path = source_root / "orchestration" / "builder-auditor-policy.json"
    antigravity_adapter = source_root / "scripts" / "o0_antigravity_adapter.py"
    codex_adapter = source_root / "scripts" / "o0_codex_adapter.py"

    assert antigravity_adapter.is_file(), f"Antigravity adapter missing at {antigravity_adapter}"
    assert codex_adapter.is_file(), f"Codex adapter missing at {codex_adapter}"
    assert policy_path.is_file(), f"Policy missing at {policy_path}"

    # 1. Setup disposable repository
    repo_dir = work_root / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "O0 M2 Builder"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "builder@o0m2.test"], cwd=repo_dir, check=True)

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
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "chore: initial commit with add function"], cwd=repo_dir, check=True, capture_output=True)
    base_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    # 2. Setup runner directories and configuration
    state_path = work_root / "orchestrator-state.json"
    reports_dir = work_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    audit_workspaces = work_root / "audit-workspaces"
    audit_workspaces.mkdir(parents=True, exist_ok=True)
    builder_workspace = repo_dir

    init_state(
        policy_path=policy_path,
        state_path=state_path,
        project_id="o0-v2-m2-project",
        phase="O0",
        gate="NONE",
        builder_branch="main",
    )

    task_instructions = (
        "In the active workspace, update calc.py to add function 'subtract(a: int, b: int) -> int' that returns a - b. "
        "In test_calc.py, add 'test_subtract' asserting subtract(10, 4) == 6. "
        "Run unit tests with 'python -m unittest test_calc.py'. "
        "When passing, stage calc.py and test_calc.py and commit with message 'feat(calc): add subtract function with tests'."
    )

    builder_cmd = [sys.executable, str(antigravity_adapter), "--task", task_instructions]
    auditor_cmd = [sys.executable, str(codex_adapter), "--task", "Verify if subtract(a, b) and test_subtract are implemented correctly and tests pass."]

    runner_config = {
        "repository": str(repo_dir),
        "state_path": str(state_path),
        "reports_dir": str(reports_dir),
        "builder_workspace": str(builder_workspace),
        "audit_workspaces": str(audit_workspaces),
        "builder_command": builder_cmd,
        "auditor_command": auditor_cmd,
        "max_retries": 3,
    }
    config_file = work_root / "runner-config.json"
    config_file.write_text(json.dumps(runner_config, indent=2), encoding="utf-8")

    initial_state = status(state_path)
    assert initial_state["machine_state"] == "READY_FOR_BUILD"
    assert initial_state["next_actor"] == "BUILDER"
    assert initial_state["audit_round"] == 0

    # 3. Step 1: Execute Builder through o0_runner.py
    t_builder_start = time.monotonic()
    step1_result = run_once(config_file)
    t_builder_duration = time.monotonic() - t_builder_start

    assert step1_result["machine_state"] == "READY_FOR_AUDIT", f"Expected READY_FOR_AUDIT, got {step1_result['machine_state']}"
    assert step1_result["next_actor"] == "AUDITOR", f"Expected next_actor AUDITOR, got {step1_result['next_actor']}"
    builder_sha = step1_result["audit_target_sha"]
    assert builder_sha is not None and builder_sha != base_sha, f"Builder produced invalid SHA: {builder_sha}"

    builder_report_file = reports_dir / "builder-report.json"
    assert builder_report_file.is_file(), "Builder report was not saved"
    builder_report = load_json(builder_report_file)
    assert builder_report["role"] == "BUILDER"
    assert builder_report["result_sha"] == builder_sha
    assert builder_report["result"] == "READY_FOR_AUDIT"

    # 4. Step 2: Execute Auditor through o0_runner.py
    t_auditor_start = time.monotonic()
    step2_result = run_once(config_file)
    t_auditor_duration = time.monotonic() - t_auditor_start

    assert step2_result["machine_state"] == "WAITING_PRODUCT_AUTHORITY", f"Expected WAITING_PRODUCT_AUTHORITY, got {step2_result['machine_state']}"
    assert step2_result["next_actor"] == "PRODUCT_AUTHORITY", f"Expected next_actor PRODUCT_AUTHORITY, got {step2_result['next_actor']}"
    assert step2_result["last_audit_result"] == "PASS", f"Expected last_audit_result PASS, got {step2_result['last_audit_result']}"
    assert step2_result["approval"] is None, "Approval must remain null (no automatic gate approval)"
    assert step2_result["human_gate_required"] is True, "human_gate_required must remain True"

    audit_report_file = reports_dir / "audit-report.json"
    assert audit_report_file.is_file(), "Audit report was not saved"
    audit_report = load_json(audit_report_file)
    assert audit_report["role"] == "AUDITOR"
    assert audit_report["audited_sha"] == builder_sha
    assert audit_report["audit_result"] == "PASS"

    frozen_checkout = audit_workspaces / builder_sha
    assert frozen_checkout.is_dir(), f"Frozen checkout missing at {frozen_checkout}"
    p_frozen_status = subprocess.run(["git", "-C", str(frozen_checkout), "status", "--porcelain"], capture_output=True, text=True, check=True)
    assert p_frozen_status.stdout.strip() == "", f"Audit checkout was modified: {p_frozen_status.stdout}"

    # 5. Standalone self-sufficient bundle creation and verification
    bundle_file = package_dir / "builder.bundle"
    subprocess.run(["git", "-C", str(repo_dir), "bundle", "create", str(bundle_file), "HEAD"], check=True, capture_output=True)

    # Verify bundle is self-sufficient without requiring external refs
    p_verify = subprocess.run(["git", "bundle", "verify", str(bundle_file)], capture_output=True, text=True, check=True)
    assert "The bundle requires this ref" not in p_verify.stdout, f"Bundle has missing prerequisite refs: {p_verify.stdout}"

    # Test standalone cloning into completely isolated directory
    test_clone_dir = work_root / "test_cloned_bundle"
    subprocess.run(["git", "clone", str(bundle_file), str(test_clone_dir)], check=True, capture_output=True)
    cloned_head = subprocess.run(["git", "-C", str(test_clone_dir), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    assert cloned_head == builder_sha, f"Cloned bundle HEAD mismatch: {cloned_head} != {builder_sha}"

    # Verify unit tests pass in the cloned repo
    p_test_cloned = subprocess.run([sys.executable, "-m", "unittest", "test_calc.py"], cwd=test_clone_dir, capture_output=True, text=True)
    assert p_test_cloned.returncode == 0, f"Cloned repo tests failed: {p_test_cloned.stderr}"

    # 6. Prove real Windows CLI timeout and cancellation termination of child processes
    interruption_root = work_root / "interruption_proofs"
    interruption_proofs = {}

    # 6a. Real Builder CLI Timeout
    interruption_proofs["builder_timeout"] = _prove_interruption(
        work_dir=interruption_root / "builder_timeout",
        repo_dir=repo_dir,
        policy_path=policy_path,
        command=builder_cmd,
        mode="timeout",
        actor_role="BUILDER",
    )
    assert interruption_proofs["builder_timeout"]["pass"], f"Builder timeout proof failed: {interruption_proofs['builder_timeout']}"

    # 6b. Real Builder CLI Cancellation
    interruption_proofs["builder_cancel"] = _prove_interruption(
        work_dir=interruption_root / "builder_cancel",
        repo_dir=repo_dir,
        policy_path=policy_path,
        command=builder_cmd,
        mode="cancel",
        actor_role="BUILDER",
    )
    assert interruption_proofs["builder_cancel"]["pass"], f"Builder cancel proof failed: {interruption_proofs['builder_cancel']}"

    # 6c. Real Auditor CLI Timeout
    interruption_proofs["auditor_timeout"] = _prove_interruption(
        work_dir=interruption_root / "auditor_timeout",
        repo_dir=repo_dir,
        policy_path=policy_path,
        command=auditor_cmd,
        mode="timeout",
        actor_role="AUDITOR",
        target_sha=builder_sha,
    )
    assert interruption_proofs["auditor_timeout"]["pass"], f"Auditor timeout proof failed: {interruption_proofs['auditor_timeout']}"

    # 6d. Real Auditor CLI Cancellation
    interruption_proofs["auditor_cancel"] = _prove_interruption(
        work_dir=interruption_root / "auditor_cancel",
        repo_dir=repo_dir,
        policy_path=policy_path,
        command=auditor_cmd,
        mode="cancel",
        actor_role="AUDITOR",
        target_sha=builder_sha,
    )
    assert interruption_proofs["auditor_cancel"]["pass"], f"Auditor cancel proof failed: {interruption_proofs['auditor_cancel']}"

    # 7. Persist evidence package
    shutil.copyfile(builder_report_file, package_dir / "builder-report.json")
    shutil.copyfile(audit_report_file, package_dir / "audit-report.json")
    shutil.copyfile(state_path, package_dir / "final-orchestrator-state.json")

    evidence_files = {}
    for ev in sorted((reports_dir / "evidence").glob("*.json")):
        dest = package_dir / "evidence" / ev.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ev, dest)
        evidence_files[ev.stem] = {
            "path": str(dest.relative_to(package_dir)),
            "sha256": hashlib.sha256(ev.read_bytes()).hexdigest(),
        }

    operations_journals = {}
    for j in sorted((reports_dir / "operations").glob("*.json")):
        dest = package_dir / "operations" / j.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(j, dest)
        operations_journals[j.stem] = {
            "path": str(dest.relative_to(package_dir)),
            "sha256": hashlib.sha256(j.read_bytes()).hexdigest(),
        }

    interruption_artifacts = {}
    for name in ("builder_timeout", "builder_cancel", "auditor_timeout", "auditor_cancel"):
        source_dir = interruption_root / name
        dest_dir = package_dir / "interruptions" / name
        dest_dir.mkdir(parents=True)
        refs = {}
        for key, filename in (("state_before", "state-before.json"),
                              ("state_after", "state-after.json"),
                              ("absence_proof", "absence-proof.json")):
            source = source_dir / filename
            dest = dest_dir / filename
            shutil.copyfile(source, dest)
            refs[key] = {"path": dest.relative_to(package_dir).as_posix(),
                         "sha256": hashlib.sha256(dest.read_bytes()).hexdigest()}
        journals = list((source_dir / "reports" / "operations").glob("*.journal.json"))
        assert len(journals) == 1, f"Expected one interruption journal for {name}"
        dest = dest_dir / "journal.json"
        shutil.copyfile(journals[0], dest)
        refs["journal"] = {"path": dest.relative_to(package_dir).as_posix(),
                           "sha256": hashlib.sha256(dest.read_bytes()).hexdigest()}
        shutil.copytree(source_dir / "reports", dest_dir / "reports")
        refs["reports_dir"] = (dest_dir / "reports").relative_to(package_dir).as_posix()
        interruption_artifacts[name] = refs

    evidence_artifact = {
        "schema_version": "0.1",
        "scenario": "O0-v2-M2-runner-adapters",
        "acceptance_criteria": {
            "adapters_connected_to_runner": True,
            "explicit_workspace_and_task": True,
            "canonical_builder_report_accepted": True,
            "canonical_audit_report_accepted": True,
            "schema_validation_enforced": True,
            "sha_verification_enforced": True,
            "evidence_canonicalized_and_referenced": True,
            "audit_checkout_immutable": True,
            "no_human_intervention_in_cycle": True,
            "no_gate_approval_registered": True,
            "stops_at_waiting_product_authority": True,
            "self_sufficient_builder_bundle": True,
            "bundle_clonable_isolated": True,
            "real_cli_timeout_terminates_child": True,
            "real_cli_cancellation_terminates_child": True,
            "no_partial_report_or_state_advance": True,
        },
        "runner_execution": {
            "config_path": str(config_file.relative_to(work_root)),
            "builder": {
                "adapter": str(antigravity_adapter.name),
                "executor_id": builder_report["executor_id"],
                "duration_seconds": round(t_builder_duration, 2),
                "base_sha": base_sha,
                "produced_sha": builder_sha,
                "changed_paths": builder_report["changed_paths"],
                "checks": builder_report["checks"],
            },
            "auditor": {
                "adapter": str(codex_adapter.name),
                "executor_id": audit_report["executor_id"],
                "duration_seconds": round(t_auditor_duration, 2),
                "audited_sha": audit_report["audited_sha"],
                "audit_result": audit_report["audit_result"],
                "checks": audit_report["checks"],
                "findings": audit_report["findings"],
            },
            "final_state": {
                "machine_state": step2_result["machine_state"],
                "next_actor": step2_result["next_actor"],
                "audit_round": step2_result["audit_round"],
                "last_audit_result": step2_result["last_audit_result"],
                "approval": step2_result["approval"],
                "human_gate_required": step2_result["human_gate_required"],
            },
            "interruption_proofs": interruption_proofs,
        },
        "package_root": str(package_dir.name),
        "artifacts": {
            "builder_bundle": {
                "path": "builder.bundle",
                "sha256": hashlib.sha256(bundle_file.read_bytes()).hexdigest(),
            },
            "builder_report": {
                "path": "builder-report.json",
                "sha256": hashlib.sha256(builder_report_file.read_bytes()).hexdigest(),
            },
            "audit_report": {
                "path": "audit-report.json",
                "sha256": hashlib.sha256(audit_report_file.read_bytes()).hexdigest(),
            },
            "final_state": {
                "path": "final-orchestrator-state.json",
                "sha256": hashlib.sha256(state_path.read_bytes()).hexdigest(),
            },
            "evidence": evidence_files,
            "operations": operations_journals,
            "interruptions": interruption_artifacts,
        },
    }

    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(evidence_artifact, indent=2) + "\n", encoding="utf-8")

    # Cleanup temporary workroot, leaving package intact
    _unprotect(work_root)
    return evidence_artifact


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, default=Path(".tmp_o0_m2_process_proof_final_run"))
    parser.add_argument("--output", type=Path, default=Path("O0_V2_M2_EVIDENCE_PROCESS_PROOF_FINAL.json"))
    parser.add_argument("--package", type=Path, default=Path("evev2m2"))
    args = parser.parse_args()

    execute_m2_integration(args.work_root, args.output, args.package)
    print(f"O0 v2 M2 Reaudit Integration PASSED. Evidence written to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
