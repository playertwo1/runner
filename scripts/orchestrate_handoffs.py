#!/usr/bin/env python3
"""Deterministic Builder/Auditor handoff state machine for Ideias Standard.

This module coordinates state and evidence only. It intentionally does not
launch AI providers, edit project code, register a human gate automatically,
or start the next phase.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = {
    "policy": ROOT / "schemas" / "orchestration-policy.schema.json",
    "state": ROOT / "schemas" / "orchestrator-state.schema.json",
    "builder": ROOT / "schemas" / "builder-report.schema.json",
    "audit": ROOT / "schemas" / "audit-report.schema.json",
    "builder-findings": ROOT / "schemas" / "builder-findings.schema.json",
    "evidence": ROOT / "schemas" / "evidence-envelope.schema.json",
    "reaudit": ROOT / "schemas" / "reaudit-handoff.schema.json",
    "operation": ROOT / "schemas" / "operation-record.schema.json",
    "operation-journal": ROOT / "schemas" / "operation-journal.schema.json",
    "runner-failure": ROOT / "schemas" / "runner-failure.schema.json",
    "task-queue": ROOT / "schemas" / "task-queue.schema.json",
}

NEXT_ACTOR_BY_STATE = {
    "READY_FOR_BUILD": "BUILDER",
    "FIX_REQUIRED": "BUILDER",
    "READY_FOR_AUDIT": "AUDITOR",
    "AUDITING": "AUDITOR",
    "WAITING_PRODUCT_AUTHORITY": "PRODUCT_AUTHORITY",
    "GATE_APPROVED": "STOP",
    "BLOCKED": "PRODUCT_AUTHORITY",
}

BLOCKED_REASONS = {
    "AUDITOR_ESCALATED": {"code": "AUDITOR_ESCALATED", "source": "AUDITOR", "evidence_ref": "last_audit_report"},
    "BUILDER_DISPUTED": {"code": "BUILDER_DISPUTED", "source": "BUILDER", "evidence_ref": "last_builder_report"},
    "AUDIT_ROUND_LIMIT_REACHED": {"code": "AUDIT_ROUND_LIMIT_REACHED", "source": "ORCHESTRATOR", "evidence_ref": "orchestrator_state"},
    "BUILDER_BLOCKED": {"code": "BUILDER_BLOCKED", "source": "BUILDER", "evidence_ref": "last_builder_report"},
}


class HandoffError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise HandoffError(f"JSON root must be an object: {path}")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def accepted_audit_snapshot(report_path: Path, audit_round: int = 1) -> Path:
    return report_path.with_name(report_path.name + f".accepted.{audit_round}")


def seal_audit_report(report_path: Path, audit_round: int = 1) -> None:
    snapshot = accepted_audit_snapshot(report_path, audit_round)
    raw = report_path.read_bytes()
    if snapshot.exists():
        if snapshot.read_bytes() != raw:
            raise HandoffError("Audit report differs from accepted audit report")
        return
    with snapshot.open("xb") as stream:
        stream.write(raw)


def validate_accepted_audit_report(report_path: Path, audit_round: int = 1) -> None:
    snapshot = accepted_audit_snapshot(report_path, audit_round)
    if snapshot.exists() and snapshot.read_bytes() != report_path.read_bytes():
        raise HandoffError("Audit report differs from accepted audit report")


def validate_with_schema(data: dict[str, Any], schema_name: str) -> None:
    schema = load_json(SCHEMAS[schema_name])
    errors = sorted(
        Draft202012Validator(schema).iter_errors(data),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        details = "; ".join(
            f"/{'/'.join(map(str, error.absolute_path)) or ''}: {error.message}"
            for error in errors
        )
        raise HandoffError(f"{schema_name} schema validation failed: {details}")


def validate_report_evidence(
    state: dict[str, Any], report_path: Path, report: dict[str, Any], report_kind: str
) -> None:
    expected_round = state["audit_round"] + (1 if report_kind == "audit" else 0)
    expected_target = state["audit_target_sha"] if report_kind == "audit" else report["result_sha"]
    items = list(report["checks"])
    if report_kind == "audit":
        items.extend(report["findings"])
    for item in items:
        reference = item["evidence"]
        evidence_path = report_path.parent / "evidence" / f"{reference['evidence_id']}.json"
        if not evidence_path.is_file():
            raise HandoffError(f"Referenced evidence does not exist: {reference['evidence_id']}")
        raw = evidence_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
            raise HandoffError(f"Referenced evidence digest mismatch: {reference['evidence_id']}")
        envelope = load_json(evidence_path)
        validate_with_schema(envelope, "evidence")
        canonical = json.dumps(
            envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if raw != canonical:
            raise HandoffError(f"Referenced evidence is not canonical: {reference['evidence_id']}")
        if (
            envelope["evidence_id"] != reference["evidence_id"]
            or envelope["run_id"] != state["run_id"]
            or envelope["audit_round"] != expected_round
            or envelope["audit_target_sha"] != expected_target
        ):
            raise HandoffError(f"Referenced evidence has an invalid audit binding: {reference['evidence_id']}")


def canonicalize_report_evidence(
    state: dict[str, Any], report_path: Path, report: dict[str, Any], report_kind: str
) -> dict[str, Any]:
    expected_round = state["audit_round"] + (1 if report_kind == "audit" else 0)
    expected_target = state["audit_target_sha"] if report_kind == "audit" else report.get("result_sha")
    items = list(report.get("checks", []))
    if report_kind == "audit":
        items.extend(report.get("findings", []))
    changed = False
    for item in items:
        content = item.get("evidence")
        if not isinstance(content, str):
            continue
        identity = f"{state['run_id']}:{expected_round}:{expected_target}:{report_kind}:{item.get('id')}:{content}"
        evidence_id = f"r{expected_round}-{report_kind}-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"
        envelope = {
            "schema_version": "0.1",
            "evidence_id": evidence_id,
            "run_id": state["run_id"],
            "audit_round": expected_round,
            "audit_target_sha": expected_target,
            "content": content,
        }
        validate_with_schema(envelope, "evidence")
        raw = json.dumps(
            envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        evidence_root = report_path.parent / "evidence"
        evidence_root.mkdir(parents=True, exist_ok=True)
        evidence_path = evidence_root / f"{evidence_id}.json"
        if evidence_path.exists() and evidence_path.read_bytes() != raw:
            raise HandoffError(f"Evidence ID already exists: {evidence_id}")
        if not evidence_path.exists():
            evidence_path.write_bytes(raw)
        item["evidence"] = {
            "evidence_id": evidence_id,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        changed = True
    if changed:
        write_json(report_path, report)
    return report


def validate_state(state: dict[str, Any]) -> None:
    validate_with_schema(state, "state")
    machine_state = state["machine_state"]
    expected_actor = NEXT_ACTOR_BY_STATE[machine_state]
    if state["next_actor"] != expected_actor:
        raise HandoffError(f"next_actor must be {expected_actor} for {machine_state}")
    blocked_reason = state["blocked_reason"]
    if machine_state == "BLOCKED":
        if blocked_reason is None:
            raise HandoffError("BLOCKED requires blocked_reason")
        code = blocked_reason["code"]
        if blocked_reason != BLOCKED_REASONS[code]:
            raise HandoffError("blocked_reason fields are inconsistent")
        if state["last_audit_result"] == "ESCALATE" and code != "AUDITOR_ESCALATED":
            raise HandoffError("blocked_reason is inconsistent with audit escalation")
        if state["last_audit_result"] == "FAIL" and state["audit_round"] >= state["max_audit_rounds"] and code != "AUDIT_ROUND_LIMIT_REACHED":
            raise HandoffError("blocked_reason is inconsistent with audit round limit")
        if code == "AUDITOR_ESCALATED" and state["last_audit_result"] != "ESCALATE":
            raise HandoffError("AUDITOR_ESCALATED requires audit escalation")
        if code == "AUDIT_ROUND_LIMIT_REACHED" and not (
            state["last_audit_result"] == "FAIL"
            and state["audit_round"] >= state["max_audit_rounds"]
        ):
            raise HandoffError("AUDIT_ROUND_LIMIT_REACHED requires exhausted audit rounds")
        if code in {"BUILDER_BLOCKED", "BUILDER_DISPUTED"}:
            report_path = state["last_builder_report"]
            if report_path is None:
                raise HandoffError("Builder blocked_reason requires last_builder_report")
            try:
                builder_result = load_json(Path(report_path)).get("result")
            except (OSError, json.JSONDecodeError, HandoffError) as error:
                raise HandoffError("Referenced Builder report is unavailable or invalid") from error
            expected_code = {
                "BLOCKED": "BUILDER_BLOCKED",
                "DISPUTED": "BUILDER_DISPUTED",
            }.get(builder_result)
            if code != expected_code:
                raise HandoffError("blocked_reason does not match Builder result")
    elif blocked_reason is not None:
        raise HandoffError(f"{machine_state} requires blocked_reason to be null")
    builder_sha = state["builder_head_sha"]
    target_sha = state["audit_target_sha"]
    audited_sha = state["last_audited_sha"]

    if machine_state == "READY_FOR_BUILD":
        if any(sha is not None for sha in (builder_sha, target_sha, audited_sha)):
            raise HandoffError("READY_FOR_BUILD requires all relevant SHAs to be null")
    else:
        if builder_sha is None:
            raise HandoffError(f"{machine_state} requires builder_head_sha")

    if machine_state in {"READY_FOR_AUDIT", "AUDITING", "FIX_REQUIRED", "WAITING_PRODUCT_AUTHORITY", "GATE_APPROVED"}:
        if target_sha is None:
            raise HandoffError(f"{machine_state} requires audit_target_sha")
        if builder_sha != target_sha:
            raise HandoffError("builder_head_sha must equal audit_target_sha")

    if (audited_sha is None) != (state["last_audit_result"] is None):
        raise HandoffError("last_audited_sha and last_audit_result must be set together")
    if audited_sha is not None:
        if target_sha is None:
            raise HandoffError("last_audited_sha requires audit_target_sha")
        if audited_sha != target_sha:
            raise HandoffError("last_audited_sha must equal audit_target_sha")
    if machine_state in {"FIX_REQUIRED", "WAITING_PRODUCT_AUTHORITY", "GATE_APPROVED"} and audited_sha is None:
        raise HandoffError(f"{machine_state} requires last_audited_sha")


def set_machine_state(state: dict[str, Any], machine_state: str, blocked_reason: str | None = None) -> None:
    state["machine_state"] = machine_state
    state["next_actor"] = NEXT_ACTOR_BY_STATE[machine_state]
    state["blocked_reason"] = None if blocked_reason is None else dict(BLOCKED_REASONS[blocked_reason])


def blocking_findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [finding for finding in report.get("findings", []) if finding.get("blocking") is True]


def make_run_id(
    policy_id: str,
    project_id: str,
    phase: str,
    gate: str,
    builder_branch: str,
) -> str:
    seed = [policy_id, project_id, phase, gate, builder_branch]
    canonical = json.dumps(seed, ensure_ascii=False, separators=(",", ":"))
    return "run-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def init_state(
    policy_path: Path,
    state_path: Path,
    *,
    project_id: str,
    phase: str,
    gate: str,
    builder_branch: str,
) -> dict[str, Any]:
    if state_path.exists():
        raise HandoffError(f"State already exists: {state_path}")
    policy = load_json(policy_path)
    validate_with_schema(policy, "policy")
    if policy["builder_role_id"] == policy["auditor_role_id"]:
        raise HandoffError("Builder and Auditor role IDs must be distinct")

    state = {
        "schema_version": "0.1",
        "run_id": make_run_id(
            policy["id"], project_id, phase, gate, builder_branch
        ),
        "project_id": project_id,
        "phase": phase,
        "gate": gate,
        "machine_state": "READY_FOR_BUILD",
        "next_actor": "BUILDER",
        "blocked_reason": None,
        "builder_branch": builder_branch,
        "builder_executor_id": None,
        "auditor_executor_id": None,
        "product_authority_id": policy["product_authority_id"],
        "builder_head_sha": None,
        "audit_target_sha": None,
        "last_audited_sha": None,
        "audit_round": 0,
        "max_audit_rounds": policy["max_audit_rounds"],
        "last_builder_report": None,
        "last_audit_report": None,
        "last_audit_report_sha256": None,
        "last_audit_result": None,
        "human_gate_required": True,
        "approval": None,
        "updated_at": now(),
        "message": "Ready for Builder.",
    }
    validate_state(state)
    write_json(state_path, state)
    return state


def builder_handoff(
    state_path: Path,
    report_path: Path,
    commit_sha: str | None = None,
) -> dict[str, Any]:
    state = load_json(state_path)
    report = load_json(report_path)
    validate_state(state)
    if state["machine_state"] not in {"READY_FOR_BUILD", "FIX_REQUIRED"}:
        raise HandoffError(f"Builder handoff not allowed from {state['machine_state']}")
    report = canonicalize_report_evidence(state, report_path, report, "builder")
    validate_with_schema(report, "builder")
    validate_report_evidence(state, report_path, report, "builder")

    result_sha = report["result_sha"]
    if commit_sha is not None and commit_sha != result_sha:
        raise HandoffError("Builder report result_sha does not match commit_sha")
    commit_sha = result_sha

    state["last_builder_report"] = str(report_path)
    if state.get("auditor_executor_id") == report["executor_id"]:
        raise HandoffError("Builder and Auditor executor identities must be distinct")
    state["builder_executor_id"] = report["executor_id"]
    state["builder_head_sha"] = commit_sha

    if report["result"] == "READY_FOR_AUDIT":
        if commit_sha != state.get("audit_target_sha"):
            state["last_audited_sha"] = None
            state["last_audit_result"] = None
        state["audit_target_sha"] = commit_sha
        set_machine_state(state, "READY_FOR_AUDIT")
        state["message"] = f"Audit target frozen at {commit_sha}."
    else:
        reason = "BUILDER_DISPUTED" if report["result"] == "DISPUTED" else "BUILDER_BLOCKED"
        set_machine_state(state, "BLOCKED", reason)
        state["message"] = (
            f"Builder returned {report['result']}; Product Authority or explicit conflict resolution is required."
        )

    state["updated_at"] = now()
    validate_state(state)
    write_json(state_path, state)
    return state


def audit_handoff(
    state_path: Path,
    report_path: Path,
) -> dict[str, Any]:
    state = load_json(state_path)
    report = load_json(report_path)
    validate_state(state)
    if state["machine_state"] not in {"READY_FOR_AUDIT", "AUDITING"}:
        raise HandoffError(f"Audit handoff not allowed from {state['machine_state']}")
    if state["audit_round"] >= state["max_audit_rounds"]:
        raise HandoffError("Maximum audit rounds already reached")
    report = canonicalize_report_evidence(state, report_path, report, "audit")
    validate_with_schema(report, "audit")
    validate_report_evidence(state, report_path, report, "audit")

    target = state.get("audit_target_sha")
    if report["audited_sha"] != target:
        raise HandoffError(
            f"Audit SHA mismatch: report={report['audited_sha']} state={target}"
        )
    if report["gate_registration"] != "NOT_AUTHORIZED":
        raise HandoffError("Auditor cannot register a development gate")
    if report["executor_id"] == state.get("builder_executor_id"):
        raise HandoffError("Builder and Auditor executor identities must be distinct")
    if report["audit_result"] == "PASS" and blocking_findings(report):
        raise HandoffError("Audit PASS cannot contain blocking findings")
    invalid_checks = [check for check in report["checks"] if check["status"] in {"FAIL", "NOT_RUN"}]
    if report["audit_result"] == "PASS" and invalid_checks:
        raise HandoffError("Audit PASS requires every mandatory check to be PASS or NOT_APPLICABLE")
    if report["audit_result"] == "FAIL" and not report["findings"]:
        raise HandoffError("Audit FAIL requires at least one finding")
    finding_ids = [finding["id"] for finding in report["findings"]]
    if len(finding_ids) != len(set(finding_ids)):
        raise HandoffError("Duplicate audit finding ID")

    state["audit_round"] += 1
    state["last_audit_report"] = str(report_path)
    state["last_audit_report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
    state["auditor_executor_id"] = report["executor_id"]
    state["last_audited_sha"] = report["audited_sha"]
    state["last_audit_result"] = report["audit_result"]

    if report["audit_result"] == "PASS":
        set_machine_state(state, "WAITING_PRODUCT_AUTHORITY")
        state["message"] = (
            "Independent audit passed for the frozen SHA. Automation must stop until explicit Product Authority gate registration."
        )
    elif report["audit_result"] == "ESCALATE":
        set_machine_state(state, "BLOCKED", "AUDITOR_ESCALATED")
        state["message"] = "Auditor escalated a decision, risk, or canonical conflict."
    elif state["audit_round"] >= state["max_audit_rounds"]:
        set_machine_state(state, "BLOCKED", "AUDIT_ROUND_LIMIT_REACHED")
        state["message"] = "Maximum audit/correction rounds reached."
    else:
        set_machine_state(state, "FIX_REQUIRED")
        state["message"] = "Audit failed; findings are ready for Builder correction."

    state["updated_at"] = now()
    validate_state(state)
    seal_audit_report(report_path, state["audit_round"])
    write_json(state_path, state)
    return state


def approve_gate(
    state_path: Path,
    *,
    executor_id: str,
    role: str,
    gate: str,
    audited_sha: str,
) -> dict[str, Any]:
    state = load_json(state_path)
    validate_state(state)
    if state["machine_state"] != "WAITING_PRODUCT_AUTHORITY":
        raise HandoffError(
            f"Gate registration requires WAITING_PRODUCT_AUTHORITY, not {state['machine_state']}"
        )
    if state["last_audit_result"] != "PASS":
        raise HandoffError("Gate cannot be registered without independent audit PASS")
    if state["last_audited_sha"] != state["audit_target_sha"]:
        raise HandoffError("Audited SHA no longer matches the frozen audit target")
    if role != "PRODUCT_AUTHORITY" or executor_id != state["product_authority_id"]:
        raise HandoffError("Gate approval requires the configured Product Authority identity")
    if executor_id in {state.get("builder_executor_id"), state.get("auditor_executor_id")}:
        raise HandoffError("Builder or Auditor cannot self-assign Product Authority")
    if gate != state["gate"] or audited_sha != state["last_audited_sha"]:
        raise HandoffError("Approval must explicitly match the gate and audited SHA")

    state["approval"] = {
        "executor_id": executor_id,
        "role": role,
        "authority": "GATE_APPROVAL",
        "gate": gate,
        "audited_sha": audited_sha,
        "approved_at": now(),
    }
    set_machine_state(state, "GATE_APPROVED")
    state["message"] = (
        "Gate registration recorded from explicit Product Authority action. The next phase is not started automatically."
    )
    state["updated_at"] = now()
    validate_state(state)
    write_json(state_path, state)
    return state


def status(state_path: Path) -> dict[str, Any]:
    state = load_json(state_path)
    validate_state(state)
    return state


def next_actor(state: dict[str, Any]) -> str:
    return state["next_actor"]


def print_state(state: dict[str, Any]) -> None:
    print(json.dumps(state, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--policy", required=True, type=Path)
    init.add_argument("--state", required=True, type=Path)
    init.add_argument("--project-id", required=True)
    init.add_argument("--phase", required=True)
    init.add_argument("--gate", required=True)
    init.add_argument("--builder-branch", required=True)

    builder = sub.add_parser("builder-handoff")
    builder.add_argument("--state", required=True, type=Path)
    builder.add_argument("--report", required=True, type=Path)
    builder.add_argument("--commit-sha")

    audit = sub.add_parser("audit-handoff")
    audit.add_argument("--state", required=True, type=Path)
    audit.add_argument("--report", required=True, type=Path)

    approve = sub.add_parser("approve-gate")
    approve.add_argument("--state", required=True, type=Path)
    approve.add_argument("--executor-id", required=True)
    approve.add_argument("--role", required=True)
    approve.add_argument("--gate", required=True)
    approve.add_argument("--audited-sha", required=True)

    show = sub.add_parser("status")
    show.add_argument("--state", required=True, type=Path)

    args = parser.parse_args()
    try:
        if args.command == "init":
            result = init_state(
                args.policy,
                args.state,
                project_id=args.project_id,
                phase=args.phase,
                gate=args.gate,
                builder_branch=args.builder_branch,
            )
        elif args.command == "builder-handoff":
            result = builder_handoff(args.state, args.report, args.commit_sha)
        elif args.command == "audit-handoff":
            result = audit_handoff(args.state, args.report)
        elif args.command == "approve-gate":
            result = approve_gate(args.state, executor_id=args.executor_id, role=args.role, gate=args.gate, audited_sha=args.audited_sha)
        elif args.command == "status":
            result = status(args.state)
        else:
            raise HandoffError(f"Unsupported command: {args.command}")
        print_state(result)
        return 0
    except (HandoffError, OSError, json.JSONDecodeError) as exc:
        print(f"HANDOFF ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
