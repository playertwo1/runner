#!/usr/bin/env python3
"""Run the controlled real Builder/FAIL/fix/PASS O0 cycle."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from scripts.orchestrate_handoffs import init_state


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        command, cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _runner(source: Path, config: Path) -> tuple[int, dict]:
    process = subprocess.Popen(
        [sys.executable, "-m", "scripts.o0_runner", "--config", str(config)],
        cwd=source,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = process.communicate()
    if process.returncode:
        raise RuntimeError(stderr.strip())
    return process.pid, json.loads(stdout)


def _state_evidence(state: dict) -> dict:
    fields = (
        "run_id", "machine_state", "next_actor", "audit_round", "max_audit_rounds",
        "builder_head_sha", "audit_target_sha", "last_audited_sha", "last_audit_result",
        "phase", "gate", "human_gate_required", "approval",
    )
    return {field: state[field] for field in fields}


def execute_controlled_cycle(work_root: Path, output: Path) -> dict:
    source = SOURCE_ROOT
    work_root = work_root.resolve()
    builder = work_root / "builder-workspace"
    audits = work_root / "audit-workspaces"
    reports = work_root / "reports"
    state = work_root / "orchestrator-state.json"
    config = work_root / "runner.json"
    builder.mkdir(parents=True)
    _run(["git", "init", "-b", "builder/o0-c38-e2e"], cwd=builder)
    _run(["git", "config", "user.name", "O0 C38 Builder"], cwd=builder)
    _run(["git", "config", "user.email", "o0-c38@example.invalid"], cwd=builder)

    policy = source / "orchestration" / "builder-auditor-policy.json"
    init_state(
        policy,
        state,
        project_id="o0-c38-controlled-e2e",
        phase="O0",
        gate="S1",
        builder_branch="builder/o0-c38-e2e",
    )
    actor = source / "scripts" / "o0_e2e_actor.py"
    _write_json(config, {
        "repository": str(builder),
        "state_path": str(state),
        "reports_dir": str(reports),
        "builder_workspace": str(builder),
        "audit_workspaces": str(audits),
        "builder_command": [sys.executable, str(actor)],
        "auditor_command": [sys.executable, str(actor)],
    })

    runner_pids: list[int] = []
    steps: list[dict] = []
    canonical_reports: list[dict] = []
    for index, role in enumerate(("BUILDER", "AUDITOR", "BUILDER", "AUDITOR"), start=1):
        config_payload = json.loads(config.read_text(encoding="utf-8"))
        command_key = "builder_command" if role == "BUILDER" else "auditor_command"
        config_payload[command_key] = [
            sys.executable,
            "-c",
            (
                "import os,runpy;"
                f"os.environ['O0_E2E_ACTOR_ROLE']={role!r};"
                f"runpy.run_path({str(actor)!r},run_name='__main__')"
            ),
        ]
        _write_json(config, config_payload)
        pid, step = _runner(source, config)
        runner_pids.append(pid)
        steps.append(step)
        report_name = "builder-report.json" if role == "BUILDER" else "audit-report.json"
        history = reports / "history" / f"{index:02d}-{report_name}"
        history.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(reports / report_name, history)
        canonical_reports.append({
            "path": history.relative_to(work_root).as_posix(),
            "sha256": hashlib.sha256(history.read_bytes()).hexdigest(),
        })

    events = [
        json.loads(line)
        for line in (reports / "process-events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    evidence_references = []
    for path in sorted((reports / "evidence").glob("*.json")):
        envelope = json.loads(path.read_text(encoding="utf-8"))
        evidence_references.append({
            "evidence_id": envelope["evidence_id"],
            "path": path.relative_to(work_root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })

    final = json.loads(state.read_text(encoding="utf-8"))
    result = {
        "schema_version": "0.1",
        "scenario": "O0-C38-real-basic-cycle",
        "builder_workspace": "builder-workspace",
        "audit_workspace_root": "audit-workspaces",
        "audit_workspaces": [
            {
                "path": (audits / sha).relative_to(work_root).as_posix(),
                "head_sha": _run(["git", "rev-parse", "HEAD"], cwd=audits / sha),
            }
            for sha in (steps[0]["audit_target_sha"], steps[2]["audit_target_sha"])
        ],
        "runner_process_ids": runner_pids,
        "actor_process_ids": [event["pid"] for event in events],
        "steps": [_state_evidence(step) for step in steps],
        "initial_sha": steps[0]["audit_target_sha"],
        "corrected_sha": steps[2]["audit_target_sha"],
        "evidence_references": evidence_references,
        "canonical_reports": canonical_reports,
        "final_state": _state_evidence(final),
    }
    _write_json(output, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    execute_controlled_cycle(args.work_root, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
