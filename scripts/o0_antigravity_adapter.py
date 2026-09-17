#!/usr/bin/env python3
"""Antigravity CLI adapter for o0_runner.py (O0 v2 M2).

Invoked by o0_runner as the configured builder_command in cwd=builder_workspace.
Reads task and environment, executes Antigravity CLI in headless print mode,
validates that changes were made and tests pass, and writes the canonical
builder-report.json required by the Ideias Standard.
"""
from __future__ import annotations

import argparse
import json
import os
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

TIER_MODELS = {
    "fast": "gemini-3.8-flash-low",
    "flash": "gemini-3.8-flash-low",
    "standard": "gemini-3.7-flash-medium",
    "medium": "gemini-3.7-flash-medium",
    "pro": "gemini-3.1-pro-high",
    "deep": "gemini-3.1-pro-high",
}


def resolve_model(cli_model: str, tier: str | None, is_fix_required: bool, audit_round: int = 0) -> str:
    if tier and tier.lower() in TIER_MODELS:
        return TIER_MODELS[tier.lower()]
    env_tier = os.environ.get("IDEAS_STANDARD_MODEL_TIER", "").strip().lower()
    if env_tier in TIER_MODELS:
        return TIER_MODELS[env_tier]
    env_model = os.environ.get("IDEAS_STANDARD_MODEL", "").strip()
    if env_model:
        return env_model
    if cli_model and cli_model != "gemini-3.7-flash-medium":
        return cli_model
    if is_fix_required and audit_round >= 2:
        return TIER_MODELS["pro"]
    return "gemini-3.7-flash-medium"


def _find_agy_binary(override: str | None = None) -> Path:
    if override:
        p = Path(override)
        if p.is_file():
            return p.resolve()
        raise FileNotFoundError(f"Specified Antigravity CLI binary not found: {override}")

    localappdata = Path(os.environ.get("LOCALAPPDATA", r"C:\Users\fael\AppData\Local"))
    candidate = localappdata / "agy" / "bin" / "agy.exe"
    if candidate.is_file():
        return candidate.resolve()

    which_agy = shutil.which("agy")
    if which_agy:
        return Path(which_agy).resolve()

    raise FileNotFoundError("Antigravity CLI (agy) binary not found")


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", default=None, help="Explicit task description for Builder")
    parser.add_argument("--model", default="gemini-3.7-flash-medium", help="Model for Antigravity CLI")
    parser.add_argument("--model-tier", choices=["fast", "flash", "standard", "medium", "pro", "deep"], default=None, help="Model tier for Builder")
    parser.add_argument("--agy-bin", default=None, help="Path to Antigravity CLI binary")
    parser.add_argument("--test-cmd", default=None, help="Optional test command to verify before report")
    args = parser.parse_args()

    report_env = os.environ.get("IDEAS_STANDARD_REPORT")
    if not report_env:
        print("ERROR: IDEAS_STANDARD_REPORT environment variable is required", file=sys.stderr)
        return 1

    report_path = Path(report_env).resolve()
    workspace = Path.cwd().resolve()

    state_env = os.environ.get("IDEAS_STANDARD_STATE")
    is_fix_required = False
    audit_round = 0
    if state_env and Path(state_env).is_file():
        try:
            state_data = json.loads(Path(state_env).read_text(encoding="utf-8"))
            is_fix_required = (state_data.get("machine_state") == "FIX_REQUIRED")
            audit_round = int(state_data.get("audit_round", 0))
        except Exception:
            pass

    selected_model = resolve_model(args.model, args.model_tier, is_fix_required, audit_round)

    # Discover binary
    agy_bin = _find_agy_binary(args.agy_bin)

    # Determine base SHA
    base_sha = _run_git(["rev-parse", "HEAD"], cwd=workspace).stdout.strip()

    # Determine task from arguments, environment or findings
    task_desc = args.task or os.environ.get("IDEAS_STANDARD_TASK")
    findings_path = os.environ.get("IDEAS_STANDARD_FINDINGS")

    if findings_path and Path(findings_path).is_file():
        findings_data = json.loads(Path(findings_path).read_text(encoding="utf-8"))
        findings_list = findings_data.get("findings", [])
        findings_summary = "; ".join(f.get("problem", "") for f in findings_list)
        prompt_task = (
            f"Fix the following audit findings: {findings_summary}. "
            f"Implement the necessary corrections in {workspace} and verify with tests."
        )
    elif task_desc:
        prompt_task = task_desc
    elif os.environ.get("IDEAS_STANDARD_TASK_GOAL"):
        goal = os.environ["IDEAS_STANDARD_TASK_GOAL"]
        scope_str = os.environ.get("IDEAS_STANDARD_TASK_SCOPE")
        criteria_str = os.environ.get("IDEAS_STANDARD_TASK_CRITERIA")
        parts = [f"Goal: {goal}"]
        if scope_str:
            try:
                scope_list = json.loads(scope_str)
                parts.append(f"Scope: {', '.join(scope_list)}")
            except Exception:
                pass
        if criteria_str:
            try:
                criteria_list = json.loads(criteria_str)
                parts.append("Criteria:\n- " + "\n- ".join(criteria_list))
            except Exception:
                pass
        prompt_task = "\n".join(parts)
    else:
        prompt_task = "Implement the requested changes in the active workspace and ensure tests pass."

    # Scope directories to avoid indexing unrelated trees (e.g. reports/, fixtures/)
    scoped_dirs: list[str] = []
    scope_env = os.environ.get("IDEAS_STANDARD_TASK_SCOPE")
    if scope_env:
        try:
            for s in json.loads(scope_env):
                p = (workspace / s).resolve()
                if p.is_dir():
                    scoped_dirs.append(str(p))
                elif p.is_file():
                    scoped_dirs.append(str(p.parent))
        except Exception:
            pass
    if findings_path and Path(findings_path).is_file():
        try:
            findings_data = json.loads(Path(findings_path).read_text(encoding="utf-8"))
            for f in findings_data.get("findings", []):
                for s in f.get("files", []):
                    p = (workspace / s).resolve()
                    if p.is_dir():
                        scoped_dirs.append(str(p))
                    elif p.is_file():
                        scoped_dirs.append(str(p.parent))
        except Exception:
            pass

    add_dir_args: list[str] = [f"--add-dir={workspace}"]
    for d in sorted(set(scoped_dirs)):
        if d != str(workspace):
            add_dir_args.append(f"--add-dir={d}")

    builder_prompt = (
        f"Target Repository Directory: {workspace}\n\n"
        f"{prompt_task}\n\n"
        f"Instructions:\n"
        f"- All code files to edit reside inside repository '{workspace}'.\n"
        f"- Be direct and concise. Edit only the necessary files in '{workspace}'. Do not output conversational explanations.\n"
        f"- Verify your edits with: python -B -m unittest -q scripts/test_check.py && python -B scripts/validate_standard.py --self-check\n"
        f"- When tests pass, stage files with 'git -C \"{workspace}\" add -A' and commit with a concise semantic commit message.\n"
        f"- Reply with only the word DONE."
    )

    cmd = [
        str(agy_bin),
        *add_dir_args,
        f"--model={selected_model}",
        "--dangerously-skip-permissions",
        "--disable-slash-commands",
        "--output-format", "json",
        f"--print={builder_prompt}",
    ]

    child_pid_file = os.environ.get("IDEAS_STANDARD_CHILD_PID_FILE")
    if child_pid_file:
        proc = subprocess.Popen(cmd, cwd=workspace, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            Path(child_pid_file).write_text(str(proc.pid), encoding="utf-8")
        except OSError:
            pass
        stdout, stderr = proc.communicate()
        p = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    else:
        p = subprocess.run(cmd, cwd=workspace, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
    if p.returncode != 0:
        print(f"ERROR: Antigravity CLI exited with code {p.returncode}: {p.stderr}", file=sys.stderr)
        return p.returncode

    try:
        output_json = json.loads(p.stdout.strip())
        status = output_json.get("status")
        if status != "SUCCESS":
            print(f"ERROR: Antigravity CLI status is not SUCCESS: {output_json}", file=sys.stderr)
            return 1
    except json.JSONDecodeError:
        print(f"ERROR: Antigravity CLI did not return valid JSON: {p.stdout[:300]}", file=sys.stderr)
        return 1

    # Verify produced SHA, or commit working tree changes if uncommitted
    result_sha = _run_git(["rev-parse", "HEAD"], cwd=workspace).stdout.strip()
    if result_sha == base_sha:
        status_proc = _run_git(["status", "--porcelain"], cwd=workspace)
        if status_proc.stdout.strip():
            _run_git(["add", "-A"], cwd=workspace)
            summary_first_line = prompt_task.strip().splitlines()[0] if prompt_task.strip() else "updates"
            if len(summary_first_line) > 72:
                summary_first_line = summary_first_line[:72]
            _run_git(["commit", "-m", f"feat: {summary_first_line}"], cwd=workspace)
            result_sha = _run_git(["rev-parse", "HEAD"], cwd=workspace).stdout.strip()

    if result_sha == base_sha:
        if is_fix_required:
            print("ERROR: Builder correction must produce a new SHA", file=sys.stderr)
        else:
            print("ERROR: Builder execution did not produce a new commit (result_sha == base_sha)", file=sys.stderr)
        return 1

    # Get changed paths
    diff_proc = _run_git(["diff", "--name-only", f"{base_sha}..{result_sha}"], cwd=workspace)
    changed_paths = [line.strip() for line in diff_proc.stdout.splitlines() if line.strip()]
    if not changed_paths:
        changed_paths = ["."]

    # Local Pre-validation (Zero Tokens): compile modified Python files to detect syntax errors
    for p in changed_paths:
        target_p = workspace / p
        if target_p.suffix == ".py" and target_p.is_file():
            try:
                py_compile.compile(str(target_p), doraise=True)
            except py_compile.PyCompileError as exc:
                print(f"ERROR: Local pre-validation failed: syntax error in {p}: {exc}", file=sys.stderr)
                return 1

    # Run required unit tests
    test_cmd = args.test_cmd or os.environ.get("IDEAS_STANDARD_TEST_CMD")
    if not test_cmd:
        # Auto-detect test suite from repository workspace
        if (workspace / "scripts" / "test_check.py").exists() and (workspace / "scripts" / "validate_standard.py").exists():
            test_cmd = f'"{sys.executable}" -m unittest -q scripts/test_check.py && "{sys.executable}" scripts/validate_standard.py --self-check'
        elif (workspace / "scripts" / "validate_standard.py").exists():
            test_cmd = f'"{sys.executable}" scripts/validate_standard.py --self-check'
        elif (workspace / "tests").is_dir():
            test_cmd = f'"{sys.executable}" -m unittest discover -q -s tests'
        else:
            print("ERROR: No test command configured or detected; tests are required for READY_FOR_AUDIT", file=sys.stderr)
            return 1

    p_test = subprocess.run(test_cmd, shell=True, cwd=workspace, capture_output=True, text=True)
    if p_test.returncode != 0:
        err_msg = p_test.stderr or p_test.stdout or "unknown test failure"
        lines = err_msg.strip().splitlines()
        truncated_err = "\n".join(lines[-25:]) if len(lines) > 25 else err_msg
        print(f"ERROR: Builder tests failed: {truncated_err}", file=sys.stderr)
        return 1

    test_check = {
        "id": "antigravity-unit-tests",
        "status": "PASS",
        "evidence": f"Test command '{test_cmd}' passed successfully",
    }

    # Assemble canonical builder-report.json
    report_payload: dict[str, Any] = {
        "schema_version": "0.1",
        "executor_id": "antigravity-cli",
        "role": "BUILDER",
        "authority": "IMPLEMENTATION",
        "result_sha": result_sha,
        "result": "READY_FOR_AUDIT",
        "summary": f"Antigravity CLI completed task: {prompt_task[:200]}",
        "changed_paths": changed_paths,
        "checks": [
            {
                "id": "antigravity-commit",
                "status": "PASS",
                "evidence": f"Builder produced commit {result_sha} modifying {len(changed_paths)} path(s)",
            },
            test_check,
        ],
        "limitations": [],
        "disputed_findings": [],
        "escalation": None,
    }

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
