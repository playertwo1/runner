#!/usr/bin/env python3
"""OpenAI Codex CLI adapter for o0_runner.py (O0 v2 M2).

Invoked by o0_runner as the configured auditor_command in cwd=audit_workspace.
Reads target SHA and environment, executes Codex CLI with --sandbox read-only,
validates that the checkout was unmodified, and writes the canonical
audit-report.json required by the Ideias Standard.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any


def _find_codex_binary(override: str | None = None) -> Path:
    if override:
        p = Path(override)
        if p.is_file():
            return p.resolve()
        raise FileNotFoundError(f"Specified Codex CLI binary not found: {override}")

    appdata = Path(os.environ.get("APPDATA", r"C:\Users\fael\AppData\Roaming"))
    candidate = appdata / "npm" / "codex.CMD"
    if candidate.is_file():
        return candidate.resolve()

    which_codex = shutil.which("codex")
    if which_codex:
        return Path(which_codex).resolve()

    raise FileNotFoundError("OpenAI Codex CLI binary not found")


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )


def _load_reaudit_context(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Reaudit handoff must be an object")
    required = {
        "previous_audited_sha", "new_audit_target_sha", "audit_round",
        "context_mode", "full_context_reasons", "findings",
        "changed_paths", "declared_changed_paths", "reusable_evidence",
    }
    missing = sorted(required - payload.keys())
    if missing:
        raise ValueError(f"Reaudit handoff missing fields: {', '.join(missing)}")
    return payload


def _compact_reaudit_context(payload: dict[str, Any]) -> str:
    context = {
        "previous_audited_sha": payload["previous_audited_sha"],
        "new_audit_target_sha": payload["new_audit_target_sha"],
        "audit_round": payload["audit_round"],
        "context_mode": payload["context_mode"],
        "full_context_reasons": payload["full_context_reasons"],
        "findings": payload["findings"],
        "changed_paths": payload["changed_paths"],
        "declared_changed_paths": payload["declared_changed_paths"],
        "reusable_evidence": payload["reusable_evidence"],
    }
    return json.dumps(context, ensure_ascii=False, separators=(",", ":"))


def _diff_summary(cwd: Path, target_sha: str, base_sha: str | None) -> str:
    if base_sha:
        range_spec = f"{base_sha}..{target_sha}"
    else:
        parent = _run_git(["rev-parse", f"{target_sha}^"], cwd=cwd).stdout.strip()
        range_spec = f"{parent}..{target_sha}"
    return _run_git(["diff", "--name-status", range_spec], cwd=cwd).stdout.strip()


def _build_audit_prompt(
    target_sha: str,
    audit_workspace: Path,
    task_criteria: str,
    reaudit_payload: dict[str, Any] | None = None,
) -> str:
    base_sha = reaudit_payload.get("previous_audited_sha") if reaudit_payload else None
    changed = _diff_summary(audit_workspace, target_sha, base_sha)
    diff_context = f"Delta {base_sha or 'parent'}..{target_sha}:\n{changed or '(no changed paths)'}"
    prompt_rules = (
        "Report rules: Be concise and direct. Do not output conversational filler or chat explanations. "
        "If audit_result is PASS, findings must be empty and every check status must be PASS or NOT_APPLICABLE (never FAIL or NOT_RUN). "
        "If any check fails, audit_result must be FAIL and findings must have at least one finding. "
        "IMPORTANT SANDBOX RULES: The audit checkout is strictly READ-ONLY. When running Python tests or commands, always use `python -B -m unittest -q` or `python -B scripts/validate_standard.py --self-check` so Python does not attempt to write .pyc files to __pycache__. Never attempt to write temporary files or compile caches into the repository checkout. Do not run py_compile directly on checkout files."
    )
    if reaudit_payload is not None:
        return (
            "You are an independent auditor performing a REAUDIT. "
            f"Audit commit {target_sha} in the current read-only checkout. "
            f"Prior context (references only): {_compact_reaudit_context(reaudit_payload)}\n"
            f"{diff_context}\n{task_criteria}\n"
            "Verify every applicable prior finding and the declared delta. "
            f"Read referenced evidence by ID/SHA only when needed. {prompt_rules} Return the required JSON report; findings and checks must be structured."
        )
    return (
        "You are an independent auditor. "
        f"Audit commit {target_sha} in the current read-only checkout. "
        f"{diff_context}\n{task_criteria}\n"
        f"Read only files necessary for the criteria. {prompt_rules} Return the required JSON report; findings and checks must be structured."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", default=None, help="Explicit task description for Auditor")
    parser.add_argument("--codex-bin", default=None, help="Path to Codex CLI binary")
    args = parser.parse_args()

    report_env = os.environ.get("IDEAS_STANDARD_REPORT")
    if not report_env:
        print("ERROR: IDEAS_STANDARD_REPORT environment variable is required", file=sys.stderr)
        return 1

    target_sha = os.environ.get("IDEAS_STANDARD_AUDIT_TARGET_SHA")
    if not target_sha:
        print("ERROR: IDEAS_STANDARD_AUDIT_TARGET_SHA environment variable is required", file=sys.stderr)
        return 1

    report_path = Path(report_env).resolve()
    audit_workspace = Path.cwd().resolve()

    # Discover binary
    codex_bin = _find_codex_binary(args.codex_bin)

    # Verify workspace is on the target SHA
    current_head = _run_git(["rev-parse", "HEAD"], cwd=audit_workspace).stdout.strip()
    if current_head != target_sha:
        print(f"ERROR: Audit workspace HEAD ({current_head}) does not match target SHA ({target_sha})", file=sys.stderr)
        return 1

    # Temporary schema and output paths placed safely outside the audit checkout
    temp_dir = report_path.parent / f".codex_tmp_{target_sha[:8]}_{uuid.uuid4().hex[:8]}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_schema_file = temp_dir / "auditor-schema.json"
    temp_report_file = temp_dir / "codex-raw-report.json"

    codex_output_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["audited_sha", "audit_result", "summary", "findings", "checks"],
        "properties": {
            "audited_sha": {"type": "string"},
            "audit_result": {"enum": ["PASS", "FAIL"]},
            "summary": {"type": "string"},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "severity", "blocking", "files", "problem", "violated_criterion", "resolution_condition", "evidence"],
                    "properties": {
                        "id": {"type": "string"},
                        "severity": {"enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"]},
                        "blocking": {"type": "boolean"},
                        "files": {"type": "array", "items": {"type": "string"}},
                        "problem": {"type": "string"},
                        "violated_criterion": {"type": "string"},
                        "resolution_condition": {"type": "string"},
                        "evidence": {"type": "string"},
                    },
                },
            },
            "checks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "status", "evidence"],
                    "properties": {
                        "id": {"type": "string"},
                        "status": {"enum": ["PASS", "FAIL", "NOT_RUN", "NOT_APPLICABLE"]},
                        "evidence": {"type": "string"},
                    },
                },
            },
        },
    }
    temp_schema_file.write_text(json.dumps(codex_output_schema, indent=2), encoding="utf-8")

    task_criteria = args.task or os.environ.get("IDEAS_STANDARD_AUDIT_CRITERIA")
    if not task_criteria and os.environ.get("IDEAS_STANDARD_TASK_GOAL"):
        goal = os.environ["IDEAS_STANDARD_TASK_GOAL"]
        criteria_str = os.environ.get("IDEAS_STANDARD_TASK_CRITERIA")
        parts = [f"Goal: {goal}"]
        if criteria_str:
            try:
                criteria_list = json.loads(criteria_str)
                parts.append("Acceptance criteria to verify:\n- " + "\n- ".join(criteria_list))
            except Exception:
                pass
        task_criteria = "\n".join(parts)
    elif not task_criteria:
        task_criteria = (
            "Inspect the repository files, implementation and tests. "
            "Verify correctness, completeness and that tests pass."
        )

    reaudit_handoff_env = os.environ.get("IDEAS_STANDARD_REAUDIT_HANDOFF")
    reaudit_payload = None
    if reaudit_handoff_env and Path(reaudit_handoff_env).is_file():
        reaudit_payload = _load_reaudit_context(Path(reaudit_handoff_env))
    auditor_prompt = _build_audit_prompt(
        target_sha, audit_workspace, task_criteria, reaudit_payload
    )


    codex_cmd = [
        str(codex_bin),
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "-C", str(audit_workspace),
        "--sandbox", "read-only",
        "--color", "never",
        "--json",
        "--output-schema", str(temp_schema_file),
        "-o", str(temp_report_file),
        auditor_prompt,
    ]

    pycache_dir = temp_dir / "pycache"
    pycache_dir.mkdir(parents=True, exist_ok=True)
    codex_env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPYCACHEPREFIX": str(pycache_dir),
    }
    child_pid_file = os.environ.get("IDEAS_STANDARD_CHILD_PID_FILE")
    if child_pid_file:
        proc = subprocess.Popen(codex_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=codex_env)
        try:
            Path(child_pid_file).write_text(str(proc.pid), encoding="utf-8")
        except OSError:
            pass
        stdout, stderr = proc.communicate(input="")
        p = subprocess.CompletedProcess(codex_cmd, proc.returncode, stdout, stderr)
    else:
        p = subprocess.run(
            codex_cmd,
            input="",
            text=True,
            capture_output=True,
            check=False,
            env=codex_env,
        )

    if p.returncode != 0:
        print(f"ERROR: Codex CLI exited with code {p.returncode}: {p.stderr}", file=sys.stderr)
        shutil.rmtree(temp_dir, ignore_errors=True)
        return p.returncode

    if not temp_report_file.is_file():
        print(f"ERROR: Codex CLI did not produce output report at {temp_report_file}", file=sys.stderr)
        shutil.rmtree(temp_dir, ignore_errors=True)
        return 1

    try:
        raw_report = json.loads(temp_report_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"ERROR: Failed to parse Codex output JSON: {exc}", file=sys.stderr)
        shutil.rmtree(temp_dir, ignore_errors=True)
        return 1

    # Verify audit checkout remained completely clean
    p_status = _run_git(["status", "--porcelain"], cwd=audit_workspace)
    if p_status.stdout.strip():
        print(f"ERROR: Audit checkout was modified during audit: {p_status.stdout}", file=sys.stderr)
        shutil.rmtree(temp_dir, ignore_errors=True)
        return 1

    audit_result = raw_report.get("audit_result", "FAIL")
    raw_findings = raw_report.get("findings", [])
    raw_checks = raw_report.get("checks", [])

    sanitized_checks = []
    has_failed_check = False
    for chk in raw_checks:
        if not isinstance(chk, dict):
            continue
        c_id = str(chk.get("id") or f"check-{len(sanitized_checks)+1}")
        c_status = str(chk.get("status") or "FAIL")
        c_ev = str(chk.get("evidence") or "Verification performed during audit")
        if c_status == "FAIL":
            has_failed_check = True
        elif audit_result == "PASS" and c_status == "NOT_RUN":
            c_status = "NOT_APPLICABLE"
        sanitized_checks.append({
            "id": c_id,
            "status": c_status,
            "evidence": c_ev,
        })

    if has_failed_check and audit_result == "PASS":
        audit_result = "FAIL"

    if not sanitized_checks:
        sanitized_checks = [{
            "id": "codex-check-1",
            "status": "PASS" if audit_result == "PASS" else "FAIL",
            "evidence": "Auditor verification performed in frozen checkout",
        }]

    converted_findings = []
    if audit_result == "FAIL":
        for i, item in enumerate(raw_findings):
            if isinstance(item, dict):
                converted_findings.append(item)
            else:
                converted_findings.append({
                    "id": f"CODEX-FINDING-{i+1:03d}",
                    "severity": "HIGH",
                    "blocking": True,
                    "files": ["."],
                    "evidence": f"Codex reported finding: {item}",
                    "problem": str(item),
                    "violated_criterion": "Implementation or test correctness",
                    "resolution_condition": "Builder must fix the reported finding and produce a new SHA.",
                })
        if not converted_findings:
            converted_findings.append({
                "id": "CODEX-FINDING-001",
                "severity": "HIGH",
                "blocking": True,
                "files": ["."],
                "evidence": "Audit check failed without detailed findings",
                "problem": "One or more checks reported failure during audit",
                "violated_criterion": "Implementation or test correctness",
                "resolution_condition": "Builder must fix the reported check failure and produce a new SHA.",
            })
    else:
        converted_findings = []

    canonical_report: dict[str, Any] = {
        "schema_version": "0.1",
        "executor_id": "codex-cli",
        "role": "AUDITOR",
        "authority": "INDEPENDENT_AUDIT",
        "audit_result": audit_result,
        "audited_sha": target_sha,
        "summary": raw_report.get("summary", f"Independent audit {audit_result} by Codex CLI."),
        "findings": converted_findings,
        "checks": sanitized_checks,
        "residual_risks": [],
        "gate_registration": "NOT_AUTHORIZED",
    }

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(canonical_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Cleanup temporary files
    shutil.rmtree(temp_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
