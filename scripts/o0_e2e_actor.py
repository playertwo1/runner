#!/usr/bin/env python3
"""Deterministic local actors used by the controlled O0 E2E cycle."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def _git(*args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True, env=env
    )
    return result.stdout.strip()


def _record_process(report: Path, role: str) -> None:
    with (report.parent / "process-events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"pid": os.getpid(), "role": role}) + "\n")


def _builder(report: Path) -> None:
    correction = bool(os.environ.get("IDEAS_STANDARD_FINDINGS"))
    artifact = Path("artifact.txt")
    artifact.write_text("fixed\n" if correction else "broken\n", encoding="utf-8")
    _git("add", "artifact.txt")
    stamp = "2020-01-02T00:00:00+00:00" if correction else "2020-01-01T00:00:00+00:00"
    commit_env = {
        **os.environ,
        "GIT_AUTHOR_DATE": stamp,
        "GIT_COMMITTER_DATE": stamp,
    }
    _git("commit", "-m", "fix controlled artifact" if correction else "add controlled artifact", env=commit_env)
    result_sha = _git("rev-parse", "HEAD")
    payload = {
        "schema_version": "0.1",
        "executor_id": "o0-c38-builder-process",
        "role": "BUILDER",
        "authority": "IMPLEMENTATION",
        "result_sha": result_sha,
        "result": "READY_FOR_AUDIT",
        "summary": "Controlled correction committed." if correction else "Controlled candidate committed.",
        "changed_paths": ["artifact.txt"],
        "checks": [{
            "id": "controlled-builder-check",
            "status": "PASS",
            "evidence": "artifact.txt committed by the Builder process",
        }],
        "limitations": [],
        "disputed_findings": [],
        "escalation": None,
    }
    report.write_text(json.dumps(payload), encoding="utf-8")


def _auditor(report: Path) -> None:
    target = os.environ["IDEAS_STANDARD_AUDIT_TARGET_SHA"]
    fixed = Path("artifact.txt").read_text(encoding="utf-8") == "fixed\n"
    finding = {
        "id": "O0-C38-CONTROLLED-001",
        "severity": "HIGH",
        "blocking": True,
        "files": ["artifact.txt"],
        "evidence": "artifact.txt contains the controlled failing value",
        "problem": "Controlled artifact is not corrected.",
        "violated_criterion": "artifact.txt must contain fixed",
        "resolution_condition": "Builder commits artifact.txt with fixed content.",
    }
    payload = {
        "schema_version": "0.1",
        "executor_id": "o0-c38-auditor-process",
        "role": "AUDITOR",
        "authority": "INDEPENDENT_AUDIT",
        "audit_result": "PASS" if fixed else "FAIL",
        "audited_sha": target,
        "summary": "Controlled candidate accepted." if fixed else "Controlled finding reproduced.",
        "findings": [] if fixed else [finding],
        "checks": [{
            "id": "controlled-audit-check",
            "status": "PASS" if fixed else "FAIL",
            "evidence": "artifact.txt content checked in the frozen audit workspace",
        }],
        "residual_risks": [],
        "gate_registration": "NOT_AUTHORIZED",
    }
    report.write_text(json.dumps(payload), encoding="utf-8")


def main() -> int:
    report = Path(os.environ["IDEAS_STANDARD_REPORT"])
    report.parent.mkdir(parents=True, exist_ok=True)
    role = os.environ["O0_E2E_ACTOR_ROLE"]
    _record_process(report, role)
    if role == "BUILDER":
        _builder(report)
    elif role == "AUDITOR":
        _auditor(report)
    else:
        raise ValueError(f"Unknown actor role: {role}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
