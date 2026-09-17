#!/usr/bin/env python3
"""O0 v2 M1: Validate Antigravity CLI and Codex CLI in disposable repository."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from uuid import uuid4
from pathlib import Path


def _run(cmd: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=check,
    )


def _protect_dir_readonly(path: Path) -> None:
    """Set checkout files read-only; caller must test that a write is denied."""
    for p in path.rglob("*"):
        if p.is_file() and not p.name.startswith(".git"):
            os.chmod(p, stat.S_IREAD)


def _prepare_work_root(work_root: Path) -> None:
    """A repeated invocation must never erase a previous execution."""
    work_root.mkdir(parents=True, exist_ok=False)


def execute_m1_validation(work_root: Path, output_json: Path) -> dict:
    work_root = work_root.resolve()
    if output_json.exists():
        raise FileExistsError(f"Evidence already exists: {output_json}")
    package_dir = output_json.resolve().parent / f"{output_json.stem}_PACKAGE"
    if package_dir.exists():
        raise FileExistsError(f"Evidence package already exists: {package_dir}")
    _prepare_work_root(work_root)

    # 1. Discover CLIs
    localappdata = Path(os.environ.get("LOCALAPPDATA", r"C:\Users\fael\AppData\Local"))
    appdata = Path(os.environ.get("APPDATA", r"C:\Users\fael\AppData\Roaming"))

    agy_bin = localappdata / "agy" / "bin" / "agy.exe"
    if not agy_bin.is_file():
        which_agy = shutil.which("agy")
        if which_agy:
            agy_bin = Path(which_agy)
        else:
            raise FileNotFoundError(f"Antigravity CLI not found at {agy_bin}")

    codex_bin = appdata / "npm" / "codex.CMD"
    if not codex_bin.is_file():
        which_codex = shutil.which("codex")
        if which_codex:
            codex_bin = Path(which_codex)
        else:
            raise FileNotFoundError(f"Codex CLI not found at {codex_bin}")

    # 2. Inspect Versions
    agy_version_proc = _run([str(agy_bin), "--version"])
    agy_version = agy_version_proc.stdout.strip()

    codex_version_proc = _run([str(codex_bin), "--version"], cwd=work_root)
    codex_version = codex_version_proc.stdout.strip()

    # 3. Setup Disposable Repositories
    builder_repo = work_root / "builder_repo"
    builder_repo.mkdir(parents=True, exist_ok=True)

    _run(["git", "init", "-b", "main"], cwd=builder_repo)
    _run(["git", "config", "user.name", "Antigravity Builder"], cwd=builder_repo)
    _run(["git", "config", "user.email", "builder@antigravity.test"], cwd=builder_repo)

    calc_file = builder_repo / "calc.py"
    calc_file.write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")

    test_file = builder_repo / "test_calc.py"
    test_file.write_text(
        """import unittest
from calc import add

class TestCalc(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)

if __name__ == '__main__':
    unittest.main()
""",
        encoding="utf-8",
    )

    gitignore_file = builder_repo / ".gitignore"
    gitignore_file.write_text(".serena/\n__pycache__/\n*.pyc\n", encoding="utf-8")

    _run(["git", "add", ".gitignore", "calc.py", "test_calc.py"], cwd=builder_repo)
    _run(["git", "commit", "-m", "chore: initial commit with add function and gitignore"], cwd=builder_repo)
    base_sha = _run(["git", "rev-parse", "HEAD"], cwd=builder_repo).stdout.strip()

    # 4. Builder: Non-interactive execution via Antigravity CLI
    builder_prompt = (
        f"In the active workspace {builder_repo}, update calc.py to add a function 'multiply(a: int, b: int) -> int' that returns a * b. "
        "In test_calc.py, add a test method 'test_multiply' that asserts multiply(3, 4) == 12. "
        "Run the tests using 'python -m unittest test_calc.py'. "
        "When tests pass, stage the files with 'git add calc.py test_calc.py' and commit with message 'feat(calc): add multiply function with tests'. "
        "Reply with only the word DONE."
    )

    builder_cmd = [
        str(agy_bin),
        f"--add-dir={builder_repo}",
        "--model=gemini-3.7-flash-medium",
        "--dangerously-skip-permissions",
        "--output-format", "json",
        f"--print={builder_prompt}",
    ]

    t_builder_start = time.monotonic()
    p_builder = _run(builder_cmd, cwd=builder_repo, check=False)
    t_builder_duration = time.monotonic() - t_builder_start

    assert p_builder.returncode == 0, f"Antigravity CLI failed with code {p_builder.returncode}: {p_builder.stderr}"

    builder_output_json = json.loads(p_builder.stdout.strip())
    assert builder_output_json.get("status") == "SUCCESS", f"Expected SUCCESS status, got: {builder_output_json}"

    # Verify builder git commit and SHA
    builder_sha = _run(["git", "rev-parse", "HEAD"], cwd=builder_repo).stdout.strip()
    assert builder_sha != base_sha, "Builder must create a new commit with distinct SHA"

    # Verify unit test passes in builder repo
    p_builder_test = _run([sys.executable, "-m", "unittest", "test_calc.py"], cwd=builder_repo, check=False)
    assert p_builder_test.returncode == 0, f"Builder tests failed: {p_builder_test.stderr}"

    commit_log = _run(["git", "log", "-1", "--stat"], cwd=builder_repo).stdout.strip()

    builder_evidence = {
        "cli_name": "Antigravity CLI",
        "binary_path": str(agy_bin),
        "version": agy_version,
        "auth_mode": "Local Antigravity / Gemini authenticated session",
        "command": builder_cmd,
        "working_directory": str(builder_repo),
        "exit_code": p_builder.returncode,
        "duration_seconds": round(t_builder_duration, 2),
        "conversation_id": builder_output_json.get("conversation_id"),
        "status": builder_output_json.get("status"),
        "usage": builder_output_json.get("usage"),
        "base_sha": base_sha,
        "produced_sha": builder_sha,
        "commit_log": commit_log,
        "unit_test_passed": True,
    }

    # 5. Auditor: Non-interactive execution via Codex CLI in isolated checkout
    audit_checkout = work_root / "audit_checkout"
    audit_reports = work_root / "audit_reports"
    audit_reports.mkdir(parents=True, exist_ok=True)
    report_file = audit_reports / "codex_audit_report.json"

    # Clone into separate audit checkout at exact builder_sha
    _run(["git", "clone", str(builder_repo), str(audit_checkout)])
    _run(["git", "checkout", builder_sha], cwd=audit_checkout)
    audit_sha = _run(["git", "rev-parse", "HEAD"], cwd=audit_checkout).stdout.strip()
    assert audit_sha == builder_sha, f"Checkout SHA {audit_sha} != builder SHA {builder_sha}"

    # Protect checkout files against modification (read-only)
    _protect_dir_readonly(audit_checkout)
    protected_file = audit_checkout / "calc.py"
    before_write_attempt = protected_file.read_bytes()
    write_attempt_rejected = False
    write_attempt_error = None
    try:
        protected_file.write_text("tampered\n", encoding="utf-8")
    except PermissionError:
        write_attempt_rejected = True
        write_attempt_error = "PermissionError"
    assert write_attempt_rejected, "Audit checkout accepted a deliberate write to calc.py"
    assert protected_file.read_bytes() == before_write_attempt

    report_schema = {
        "type": "object", "additionalProperties": False,
        "required": ["audited_sha", "audit_result", "summary", "findings", "checks"],
        "properties": {
            "audited_sha": {"type": "string"},
            "audit_result": {"enum": ["PASS", "FAIL"]},
            "summary": {"type": "string"},
            "findings": {"type": "array", "items": {"type": "string"}},
            "checks": {"type": "array", "items": {"type": "string"}},
        },
    }
    schema_file = audit_reports / "auditor-output.schema.json"
    schema_file.write_text(json.dumps(report_schema), encoding="utf-8")

    auditor_prompt = (
        f"You are an independent auditor. Audit the repository in the current directory at commit {builder_sha}. "
        "Inspect calc.py and test_calc.py. Verify if multiply(a, b) and test_multiply correctly implement and test multiplication. "
        "Confirm that the implementation is correct and complete. "
        "Return the JSON report required by the output schema. Use audit_result PASS only if the implementation and tests are correct. "
        "Use arrays of strings for findings and checks; findings must be empty for PASS."
    )

    auditor_cmd = [
        str(codex_bin),
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "-C", str(audit_checkout),
        "--sandbox", "read-only",
        "--json",
        "--output-schema", str(schema_file),
        "-o", str(report_file),
        auditor_prompt,
    ]

    t_auditor_start = time.monotonic()
    p_auditor = subprocess.run(
        auditor_cmd,
        input="",
        text=True,
        capture_output=True,
        check=False,
    )
    t_auditor_duration = time.monotonic() - t_auditor_start

    assert p_auditor.returncode == 0, f"Codex CLI failed with code {p_auditor.returncode}: {p_auditor.stderr}"
    assert report_file.is_file(), f"Auditor report file must be created at {report_file}"

    report_content = json.loads(report_file.read_text(encoding="utf-8"))
    assert set(report_content) == set(report_schema["required"]), "Auditor report keys diverge from schema"
    assert report_content["audited_sha"] == builder_sha, "Auditor reported a different SHA"
    assert report_content["audit_result"] == "PASS", "Auditor did not accept the Builder commit"
    assert isinstance(report_content["summary"], str) and report_content["summary"].strip()
    assert isinstance(report_content["findings"], list) and not report_content["findings"]
    assert isinstance(report_content["checks"], list) and report_content["checks"]
    assert all(isinstance(item, str) and item.strip() for item in report_content["checks"])
    events = [json.loads(line) for line in p_auditor.stdout.splitlines() if line.strip()]
    assert any(event.get("type") == "turn.completed" for event in events), "Codex run did not complete"

    # Verify audit checkout remained completely untouched
    checkout_status = _run(["git", "status", "--porcelain"], cwd=audit_checkout).stdout.strip()
    assert checkout_status == "", f"Audit checkout was modified: {checkout_status}"

    auditor_evidence = {
        "cli_name": "OpenAI Codex CLI",
        "binary_path": str(codex_bin),
        "version": codex_version,
        "auth_mode": "ChatGPT stored credentials (~/.codex/auth.json)",
        "command": auditor_cmd,
        "working_directory": str(audit_checkout),
        "exit_code": p_auditor.returncode,
        "duration_seconds": round(t_auditor_duration, 2),
        "audited_sha": audit_sha,
        "write_protection_verified": True,
        "write_attempt_rejected": write_attempt_rejected,
        "write_attempt_path": "calc.py",
        "write_attempt_error": write_attempt_error,
        "write_attempt_sha256_before": hashlib.sha256(before_write_attempt).hexdigest(),
        "write_attempt_sha256_after": hashlib.sha256(protected_file.read_bytes()).hexdigest(),
        "checkout_unmodified": True,
        "report_file_path": str(report_file.relative_to(work_root)),
        "report_file_sha256": hashlib.sha256(report_file.read_bytes()).hexdigest(),
        "audit_result": report_content["audit_result"],
    }

    package_dir.mkdir(parents=True, exist_ok=False)
    bundle = package_dir / "builder.bundle"
    _run(["git", "bundle", "create", str(bundle), "--all"], cwd=builder_repo)
    _run(["git", "bundle", "verify", str(bundle)], cwd=builder_repo)
    saved_report = package_dir / "codex_audit_report.json"
    shutil.copyfile(report_file, saved_report)
    (package_dir / "antigravity_output.json").write_text(json.dumps(builder_output_json, indent=2) + "\n", encoding="utf-8")
    (package_dir / "codex_events.jsonl").write_text(p_auditor.stdout, encoding="utf-8")

    def artifact(path: Path) -> dict:
        return {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    evidence = {
        "schema_version": "0.1",
        "scenario": "O0-v2-M1-cli-validation",
        "acceptance_criteria": {
            "version_recorded": True,
            "authentication_confirmed": True,
            "non_interactive_execution": True,
            "exit_codes_verified": True,
            "explicit_working_directories": True,
            "builder_file_changed": True,
            "builder_unit_test_executed": True,
            "builder_commit_created": True,
            "builder_verifiable_sha": True,
            "auditor_separate_checkout": True,
            "auditor_code_write_protected": True,
            "auditor_reports_outside_checkout": True,
            "structured_output_captured": True,
            "no_interactive_prompts": True,
        },
        "builder": builder_evidence,
        "auditor": auditor_evidence,
        "package_root": str(package_dir.relative_to(output_json.resolve().parent)).replace("\\", "/"),
        "artifacts": {"builder_bundle": artifact(bundle), "auditor_report": artifact(saved_report),
                      "builder_output": artifact(package_dir / "antigravity_output.json"),
                      "auditor_events": artifact(package_dir / "codex_events.jsonl")},
    }

    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, default=Path(f".tmp_o0_m1_run_{uuid4().hex}"))
    parser.add_argument("--output", type=Path, default=Path("O0_V2_M1_EVIDENCE_REAUDIT.json"))
    args = parser.parse_args()
    execute_m1_validation(args.work_root, args.output)
    print(f"O0 v2 M1 Validation PASSED. Evidence written to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
