#!/usr/bin/env python3
"""Provider-neutral operational runner for one Builder/Auditor handoff."""
from __future__ import annotations

import argparse
import contextlib
import ctypes
from ctypes import wintypes
import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

if sys.platform == "linux":
    import fcntl

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.orchestrate_handoffs import (
    HandoffError,
    accepted_audit_snapshot,
    audit_handoff,
    builder_handoff,
    load_json,
    next_actor,
    seal_audit_report,
    status,
    validate_accepted_audit_report,
    validate_with_schema,
    write_json,
)

REQUIRED_CONFIG = {
    "repository",
    "state_path",
    "reports_dir",
    "builder_workspace",
    "audit_workspaces",
    "builder_command",
    "auditor_command",
}

_LANDLOCK_CREATE_RULESET = 444
_LANDLOCK_ADD_RULE = 445
_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_RULE_PATH_BENEATH = 1
_LANDLOCK_CREATE_RULESET_VERSION = 1
_PR_SET_NO_NEW_PRIVS = 38

_WRITE_FILE = 1 << 1
_REMOVE_DIR = 1 << 4
_REMOVE_FILE = 1 << 5
_MAKE_CHAR = 1 << 6
_MAKE_DIR = 1 << 7
_MAKE_REG = 1 << 8
_MAKE_SOCK = 1 << 9
_MAKE_FIFO = 1 << 10
_MAKE_BLOCK = 1 << 11
_MAKE_SYM = 1 << 12
_REFER = 1 << 13
_TRUNCATE = 1 << 14
_EVIDENCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OPERATION_ID = re.compile(r"^op-[0-9a-f]{64}$")


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
        ("reserved", ctypes.c_uint32),
    ]


class ActorInterrupted(HandoffError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"Actor {reason.lower()}; operation is interrupted and resumable")


class ActorExitError(HandoffError):
    def __init__(self, exit_code: int):
        self.exit_code = exit_code
        super().__init__(f"Actor command failed with exit code {exit_code}")


def load_config(path: Path) -> dict[str, Any]:
    data = load_json(path)
    missing = sorted(REQUIRED_CONFIG - data.keys())
    if missing:
        raise HandoffError(f"Runner config missing: {', '.join(missing)}")
    for key in ("builder_command", "auditor_command"):
        if not isinstance(data[key], list) or not data[key] or not all(
            isinstance(item, str) and item for item in data[key]
        ):
            raise HandoffError(f"{key} must be a non-empty string array")
    timeout = data.get("lock_timeout_seconds", 0)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout < 0
    ):
        raise HandoffError("lock_timeout_seconds must be a non-negative finite number")
    actor_timeout = data.get("actor_timeout_seconds")
    if actor_timeout is not None and (
        isinstance(actor_timeout, bool)
        or not isinstance(actor_timeout, (int, float))
        or not math.isfinite(actor_timeout)
        or actor_timeout <= 0
    ):
        raise HandoffError("actor_timeout_seconds must be a positive finite number")
    if "cancel_path" in data and (not isinstance(data["cancel_path"], str) or not data["cancel_path"]):
        raise HandoffError("cancel_path must be a non-empty string")
    if "resume_interrupted" in data and not isinstance(data["resume_interrupted"], bool):
        raise HandoffError("resume_interrupted must be boolean")
    operation_id = data.get("operation_id")
    if operation_id is not None and (
        not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id)
    ):
        raise HandoffError("operation_id must be op- followed by 64 lowercase hex characters")
    return data


class StateLock:
    """Kernel-owned process lock; a leftover lock file has no authority."""

    def __init__(self, state_path: Path, timeout: float):
        self.path = state_path.with_name(state_path.name + ".lock")
        self.timeout = timeout
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._lock()
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    self.stream.close()
                    self.stream = None
                    raise HandoffError("Runner state lock is busy")
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        self.stream.seek(0)
        self.stream.truncate()
        self.stream.write(f"pid={os.getpid()}\n".encode("ascii"))
        self.stream.flush()
        return self

    def _lock(self) -> None:
        if os.name == "nt":
            import msvcrt

            self.stream.seek(0, os.SEEK_END)
            if self.stream.tell() == 0:
                self.stream.write(b"\0")
                self.stream.flush()
            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __exit__(self, exc_type, exc_value, traceback):
        if self.stream is None:
            return
        if os.name == "nt":
            import msvcrt

            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        self.stream.close()


def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def operation_identity(current: dict[str, Any], actor: str) -> tuple[str, dict[str, Any]]:
    source = {
        "run_id": current["run_id"],
        "machine_state": current["machine_state"],
        "actor": actor,
        "audit_round": current["audit_round"],
        "builder_head_sha": current.get("builder_head_sha"),
        "audit_target_sha": current.get("audit_target_sha"),
        "last_audited_sha": current.get("last_audited_sha"),
    }
    return operation_id_from_source(source), source


def operation_id_from_source(source: dict[str, Any]) -> str:
    digest = hashlib.sha256(canonical_json_bytes(source)).hexdigest()
    return f"op-{digest}"


def _operation_paths(reports_dir: Path, operation_id: str) -> tuple[Path, Path]:
    root = reports_dir / "operations"
    return root / f"{operation_id}.json", root / f"{operation_id}.report.json"


def _journal_path(reports_dir: Path, operation_id: str) -> Path:
    return reports_dir / "operations" / f"{operation_id}.journal.json"


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        temporary.write_bytes(payload)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_canonical_atomic(path: Path, payload: dict[str, Any]) -> None:
    _write_bytes_atomic(path, canonical_json_bytes(payload))


def _validate_journal(journal: dict[str, Any], raw: bytes) -> None:
    validate_with_schema(journal, "operation-journal")
    validate_with_schema(journal["source_state"], "state")
    derived_id, derived_source = operation_identity(journal["source_state"], journal["actor"])
    if (
        raw != canonical_json_bytes(journal)
        or derived_id != journal["operation_id"]
        or derived_source != journal["source"]
        or journal["actor"] != journal["source"].get("actor")
    ):
        raise HandoffError("Persisted operation journal is invalid")
    ready = journal["phase"] == "REPORT_READY"
    if ready != (journal["report_sha256"] is not None and journal["result"] is not None):
        raise HandoffError("Persisted operation journal phase is inconsistent")
    interrupted = journal["phase"] == "INTERRUPTED"
    if interrupted != (journal.get("interruption_reason") is not None):
        raise HandoffError("Persisted operation interruption reason is inconsistent")
    if ready:
        validate_with_schema(journal["result"], "state")


def load_recovery_journal(reports_dir: Path) -> tuple[Path, dict[str, Any]] | None:
    root = reports_dir / "operations"
    journals = sorted(root.glob("op-*.journal.json")) if root.is_dir() else []
    if not journals:
        return None
    if len(journals) != 1:
        raise HandoffError("Multiple incomplete operation journals require manual resolution")
    path = journals[0]
    raw = path.read_bytes()
    journal = load_json(path)
    _validate_journal(journal, raw)
    return path, journal


def prepare_operation_journal(
    reports_dir: Path,
    operation_id: str,
    source: dict[str, Any],
    source_state: dict[str, Any],
    actor: str,
    report_path: Path,
) -> tuple[Path, dict[str, Any]]:
    path = _journal_path(reports_dir, operation_id)
    if path.exists():
        raise HandoffError(f"Operation journal already exists: {operation_id}")
    journal = {
        "schema_version": "0.1",
        "operation_id": operation_id,
        "source": source,
        "source_state": source_state,
        "actor": actor,
        "phase": "PREPARED",
        "report_path": str(report_path),
        "report_sha256": None,
        "result": None,
        "interruption_reason": None,
    }
    validate_with_schema(journal, "operation-journal")
    _write_canonical_atomic(path, journal)
    return path, journal


def compute_transition(
    state_path: Path,
    source_state: dict[str, Any],
    report_path: Path,
    actor: str,
    operation_id: str,
) -> dict[str, Any]:
    temporary = state_path.with_suffix(state_path.suffix + f".{operation_id}.next")
    if temporary.exists():
        temporary.unlink()
    write_json(temporary, source_state)
    try:
        return (
            builder_handoff(temporary, report_path)
            if actor == "BUILDER"
            else audit_handoff(temporary, report_path)
        )
    finally:
        temporary.unlink(missing_ok=True)


def mark_report_ready(
    journal_path: Path,
    journal: dict[str, Any],
    report_path: Path,
    result: dict[str, Any],
) -> dict[str, Any]:
    ready = {
        **journal,
        "phase": "REPORT_READY",
        "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        "result": result,
    }
    validate_with_schema(ready, "operation-journal")
    _write_canonical_atomic(journal_path, ready)
    return ready


def mark_actor_interrupted(journal_path: Path, journal: dict[str, Any], reason: str) -> None:
    interrupted = {**journal, "phase": "INTERRUPTED", "interruption_reason": reason}
    validate_with_schema(interrupted, "operation-journal")
    _write_canonical_atomic(journal_path, interrupted)


def prepare_interrupted_resume(journal_path: Path, journal: dict[str, Any]) -> dict[str, Any]:
    prepared = {**journal, "phase": "PREPARED", "interruption_reason": None}
    validate_with_schema(prepared, "operation-journal")
    _write_canonical_atomic(journal_path, prepared)
    return prepared


def finish_recovered_operation(
    state_path: Path,
    reports_dir: Path,
    journal_path: Path,
    journal: dict[str, Any],
    current: dict[str, Any],
    recovered: bool,
) -> dict[str, Any]:
    report_path = Path(journal["report_path"])
    if not report_path.is_file() or hashlib.sha256(report_path.read_bytes()).hexdigest() != journal["report_sha256"]:
        raise HandoffError("Recovery report is missing or differs from its journal")
    result = journal["result"]
    expected = compute_transition(
        state_path,
        journal["source_state"],
        report_path,
        journal["actor"],
        journal["operation_id"],
    )
    expected_without_time = {key: value for key, value in expected.items() if key != "updated_at"}
    result_without_time = {key: value for key, value in result.items() if key != "updated_at"}
    if expected_without_time != result_without_time:
        raise HandoffError("Persisted operation journal result is not derived from its report")
    source_matches = current == journal["source_state"]
    if source_matches:
        write_json(state_path, result)
        current = status(state_path)
    elif current != result:
        raise HandoffError("Recovery state matches neither operation source nor result")
    completed = persist_operation(
        reports_dir,
        journal["operation_id"],
        journal["source"],
        journal["actor"],
        report_path,
        current,
    )
    journal_path.unlink()
    return {**completed, "operation_recovered": recovered}


def replay_operation(
    reports_dir: Path, operation_id: str, current: dict[str, Any]
) -> dict[str, Any] | None:
    record_path, snapshot_path = _operation_paths(reports_dir, operation_id)
    if not record_path.is_file():
        return None
    raw_record = record_path.read_bytes()
    record = load_json(record_path)
    validate_with_schema(record, "operation")
    validate_with_schema(record["result"], "state")
    if raw_record != canonical_json_bytes(record) or record.get("operation_id") != operation_id:
        raise HandoffError("Persisted operation record is invalid")
    if (
        operation_id_from_source(record["source"]) != operation_id
        or record["actor"] != record["source"]["actor"]
        or record["result"] != current
    ):
        raise HandoffError("Persisted operation record identity or result is inconsistent")
    report_path = Path(record["report_path"])
    if not report_path.is_file() or not snapshot_path.is_file():
        raise HandoffError("Persisted operation report is missing")
    expected = record["report_sha256"]
    if (
        hashlib.sha256(report_path.read_bytes()).hexdigest() != expected
        or hashlib.sha256(snapshot_path.read_bytes()).hexdigest() != expected
    ):
        raise HandoffError("Operation payload differs from the persisted identity")
    return {
        **record["result"],
        "operation_id": operation_id,
        "operation_replayed": True,
    }


def check_report_not_already_accepted(
    reports_dir: Path, report_digest: str, current_operation_id: str
) -> None:
    operations_root = reports_dir / "operations"
    if not operations_root.is_dir():
        return
    for path in operations_root.glob("op-*.json"):
        if path.name.endswith(".journal.json"):
            continue
        if path.name == f"{current_operation_id}.json":
            continue
        try:
            record = load_json(path)
            if record.get("report_sha256") == report_digest:
                raise HandoffError("Report has already been accepted for another operation")
        except (OSError, json.JSONDecodeError, UnicodeError, ValueError):
            continue


def count_operation_failures(reports_dir: Path, operation_id: str) -> int:
    failure_root = reports_dir / "runner-failures"
    if not failure_root.is_dir():
        return 0
    count = 0
    for path in failure_root.glob("*.json"):
        try:
            record = load_json(path)
            if record.get("operation_id") == operation_id:
                count += 1
        except (OSError, json.JSONDecodeError, UnicodeError, ValueError):
            continue
    return count


def persist_operation(
    reports_dir: Path,
    operation_id: str,
    source: dict[str, Any],
    actor: str,
    report_path: Path,
    result: dict[str, Any],
) -> dict[str, Any]:
    record_path, snapshot_path = _operation_paths(reports_dir, operation_id)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    report_bytes = report_path.read_bytes()
    report_digest = hashlib.sha256(report_bytes).hexdigest()
    if record_path.exists():
        replayed = replay_operation(reports_dir, operation_id, result)
        if replayed is None:
            raise HandoffError(f"Operation identity already exists: {operation_id}")
        return replayed
    check_report_not_already_accepted(reports_dir, report_digest, operation_id)
    if not snapshot_path.exists() or snapshot_path.read_bytes() != report_bytes:
        _write_bytes_atomic(snapshot_path, report_bytes)
    record = {
        "schema_version": "0.1",
        "operation_id": operation_id,
        "source": source,
        "actor": actor,
        "report_path": str(report_path),
        "report_sha256": report_digest,
        "result": result,
    }
    validate_with_schema(record, "operation")
    validate_with_schema(result, "state")
    _write_canonical_atomic(record_path, record)
    return {**result, "operation_id": operation_id, "operation_replayed": False}


def store_evidence(
    root: Path,
    *,
    evidence_id: str,
    content: str,
    current: dict[str, Any],
) -> dict[str, str]:
    if not _EVIDENCE_ID.fullmatch(evidence_id):
        raise HandoffError("Unsafe evidence_id")
    envelope = {
        "schema_version": "0.1",
        "evidence_id": evidence_id,
        "run_id": current["run_id"],
        "audit_round": current["audit_round"],
        "audit_target_sha": current["audit_target_sha"],
        "content": content,
    }
    validate_with_schema(envelope, "evidence")
    canonical = canonical_json_bytes(envelope)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{evidence_id}.json"
    digest = hashlib.sha256(canonical).hexdigest()
    if path.exists():
        if path.read_bytes() != canonical:
            raise HandoffError(f"Evidence ID already exists: {evidence_id}")
    else:
        _write_bytes_atomic(path, canonical)
    return {"evidence_id": evidence_id, "sha256": digest}


def evidence_id(kind: str, source_id: str, current: dict[str, Any], content: str = "") -> str:
    identity = f"{current['run_id']}:{current['audit_round']}:{current['audit_target_sha']}:{kind}:{source_id}:{content}"
    return f"r{current['audit_round']}-{kind}-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"


def resolve_evidence(
    root: Path,
    reference: dict[str, str],
    current: dict[str, Any],
) -> dict[str, Any]:
    evidence_id = reference.get("evidence_id")
    digest = reference.get("sha256")
    if not isinstance(evidence_id, str) or not _EVIDENCE_ID.fullmatch(evidence_id):
        raise HandoffError("Invalid evidence reference ID")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise HandoffError("Invalid evidence reference digest")
    path = root / f"{evidence_id}.json"
    if not path.is_file():
        raise HandoffError(f"Evidence does not exist: {evidence_id}")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise HandoffError(f"Evidence digest mismatch: {evidence_id}")
    envelope = load_json(path)
    validate_with_schema(envelope, "evidence")
    if raw != canonical_json_bytes(envelope):
        raise HandoffError(f"Evidence is not canonically encoded: {evidence_id}")
    if (
        envelope["evidence_id"] != evidence_id
        or envelope["run_id"] != current.get("run_id")
        or envelope["audit_round"] != current.get("audit_round")
        or envelope["audit_target_sha"] != current.get("audit_target_sha")
    ):
        raise HandoffError(f"Evidence is linked to another audit: {evidence_id}")
    return envelope


def canonicalize_report_evidence(
    report_path: Path,
    report_kind: str,
    current: dict[str, Any],
    evidence_root: Path,
) -> dict[str, Any]:
    payload = load_json(report_path)
    collections = [payload.get("checks", [])]
    kinds = [f"{report_kind}-check"]
    if report_kind == "audit":
        collections.append(payload.get("findings", []))
        kinds.append("audit-finding")
    for items, kind in zip(collections, kinds):
        for item in items:
            raw = item.get("evidence")
            if isinstance(raw, str):
                item["evidence"] = store_evidence(
                    evidence_root,
                    evidence_id=evidence_id(kind, item.get("id", "missing"), current, raw),
                    content=raw,
                    current=current,
                )
            elif isinstance(raw, dict):
                resolve_evidence(evidence_root, raw, current)
            else:
                raise HandoffError("Report evidence must be text input or a valid reference")
    validate_with_schema(payload, report_kind)
    write_json(report_path, payload)
    return payload


def resolve_path(config_path: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (config_path.parent / path).resolve()


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def validate_workspaces(builder: Path, audits: Path) -> None:
    if _overlap(builder, audits):
        raise HandoffError("Builder and Auditor workspace roots must be separate")


def validate_auditor_boundaries(state: Path, audits: Path, reports: Path) -> None:
    if _overlap(reports, state) or _overlap(reports, audits):
        raise HandoffError("Auditor report path must not expose state or audit workspace to writes")


def verify_commit(repository: Path, sha: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(repository), "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise HandoffError(f"Unknown result SHA: {sha}")


def git_head(workspace: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise HandoffError("Cannot resolve workspace HEAD")
    return result.stdout.strip()


def make_read_only(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        mode = path.stat().st_mode
        path.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def prepare_audit_workspace(repository: Path, audit_root: Path, sha: str) -> Path:
    verify_commit(repository, sha)
    audit_root.mkdir(parents=True, exist_ok=True)
    workspace = audit_root / sha
    if workspace.exists():
        if git_head(workspace) != sha:
            raise HandoffError("Existing audit workspace does not match audit_target_sha")
    else:
        result = subprocess.run(
            ["git", "-C", str(repository), "worktree", "add", "--detach", str(workspace), sha],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise HandoffError(f"Cannot create audit workspace: {result.stderr.strip()}")
    make_read_only(workspace)
    return workspace


def cleanup_audit_workspace(repository: Path, workspace: Path) -> None:
    if not workspace.exists():
        return
    for path in [workspace, *workspace.rglob("*")]:
        try:
            mode = path.stat().st_mode
            path.chmod(mode | stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
        except OSError:
            pass
    subprocess.run(
        ["git", "-C", str(repository), "worktree", "remove", "--force", str(workspace)],
        capture_output=True,
        check=False,
    )
    subprocess.run(
        ["git", "-C", str(repository), "worktree", "prune"],
        capture_output=True,
        check=False,
    )


def _landlock_write_access(abi: int) -> int:
    access = (
        _WRITE_FILE
        | _REMOVE_DIR
        | _REMOVE_FILE
        | _MAKE_CHAR
        | _MAKE_DIR
        | _MAKE_REG
        | _MAKE_SOCK
        | _MAKE_FIFO
        | _MAKE_BLOCK
        | _MAKE_SYM
    )
    if abi >= 2:
        access |= _REFER
    if abi >= 3:
        access |= _TRUNCATE
    return access


def _auditor_write_sandbox(allowed_directory: Path):
    """Return a child-only Landlock setup; fail closed when unavailable."""
    allowed_directory = allowed_directory.resolve()

    def restrict() -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        syscall = libc.syscall
        abi = syscall(
            _LANDLOCK_CREATE_RULESET,
            ctypes.c_void_p(),
            ctypes.c_size_t(0),
            ctypes.c_uint(_LANDLOCK_CREATE_RULESET_VERSION),
        )
        if abi < 1:
            os._exit(126)
        access = _landlock_write_access(abi)
        ruleset_attr = _RulesetAttr(access)
        ruleset_fd = syscall(
            _LANDLOCK_CREATE_RULESET,
            ctypes.byref(ruleset_attr),
            ctypes.sizeof(ruleset_attr),
            ctypes.c_uint(0),
        )
        if ruleset_fd < 0:
            os._exit(126)
        parent_fd = os.open(allowed_directory, os.O_PATH | os.O_CLOEXEC)
        try:
            path_attr = _PathBeneathAttr(access, parent_fd, 0)
            if syscall(
                _LANDLOCK_ADD_RULE,
                ruleset_fd,
                _LANDLOCK_RULE_PATH_BENEATH,
                ctypes.byref(path_attr),
                ctypes.c_uint(0),
            ) != 0:
                os._exit(126)
            if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
                os._exit(126)
            if syscall(_LANDLOCK_RESTRICT_SELF, ruleset_fd, ctypes.c_uint(0)) != 0:
                os._exit(126)
        finally:
            os.close(parent_fd)
            os.close(ruleset_fd)

    return restrict


@contextlib.contextmanager
def _windows_auditor_write_sandbox(
    workspace: Path,
    report: Path,
    env: dict[str, str],
):
    """Restricts write access on Windows during Auditor execution; fail closed."""
    if shutil.which("icacls") is None:
        raise HandoffError("Auditor write sandbox is unavailable; refusing unsafe execution")

    applied_dirs: list[Path] = []
    applied_files: list[Path] = []
    report_parent = report.parent.resolve()

    def _is_subpath(p: Path, parent: Path) -> bool:
        try:
            p.relative_to(parent)
            return True
        except ValueError:
            return False

    targets_to_protect: list[Path] = []
    ws_resolved = workspace.resolve()
    if ws_resolved.exists() and not _is_subpath(ws_resolved, report_parent):
        targets_to_protect.append(ws_resolved)

    for env_key in (
        "CANONICAL",
        "IDEAS_STANDARD_STATE",
        "IDEAS_STANDARD_STATE_SNAPSHOT",
        "IDEAS_STANDARD_FINDINGS",
        "IDEAS_STANDARD_REAUDIT_HANDOFF",
    ):
        val = env.get(env_key)
        if val:
            p = Path(val).resolve()
            if p.exists() and not _is_subpath(p, report_parent) and p not in targets_to_protect:
                targets_to_protect.append(p)

    def _icacls_path(p: Path) -> str:
        s = str(p.resolve())
        if os.name == "nt" and not s.startswith("\\\\?\\"):
            return "\\\\?\\" + s
        return s

    def _rollback() -> None:
        for f in reversed(applied_files):
            subprocess.run(
                ["icacls", _icacls_path(f), "/remove:d", "*S-1-1-0"],
                capture_output=True,
                check=False,
            )
        for d in reversed(applied_dirs):
            subprocess.run(
                ["icacls", _icacls_path(d), "/remove:d", "*S-1-1-0", "/t"],
                capture_output=True,
                check=False,
            )

    try:
        for target in targets_to_protect:
            target_str = _icacls_path(target)
            if target.is_dir():
                res = subprocess.run(
                    ["icacls", target_str, "/deny", "*S-1-1-0:(OI)(CI)(WD,AD,WA,WEA,DC,DE)", "/t"],
                    capture_output=True,
                    check=False,
                )
                if res.returncode != 0:
                    raise HandoffError(f"Failed to apply write sandbox to {target}: {res.stderr.decode('utf-8', errors='ignore')}")
                applied_dirs.append(target)
            elif target.is_file():
                res = subprocess.run(
                    ["icacls", target_str, "/deny", "*S-1-1-0:(WD,AD,WA,WEA,DC,DE)"],
                    capture_output=True,
                    check=False,
                )
                if res.returncode != 0:
                    raise HandoffError(f"Failed to apply write sandbox to {target}: {res.stderr.decode('utf-8', errors='ignore')}")
                applied_files.append(target)
    except Exception as exc:
        _rollback()
        raise HandoffError(f"Auditor write sandbox is unavailable; refusing unsafe execution ({exc})") from exc

    try:
        yield
    finally:
        _rollback()


def _terminate_actor_process(process: subprocess.Popen) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        try:
            process.kill()
        except OSError:
            pass
        if result.returncode != 0:
            raise HandoffError(f"Actor process tree termination failed (taskkill exit {result.returncode})")
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass


def run_actor(
    command: list[str],
    workspace: Path,
    report: Path,
    env: dict[str, str],
    *,
    write_sandbox: bool = False,
    timeout_seconds: float | None = None,
    cancel_path: Path | None = None,
) -> None:
    findings_path = env.get("IDEAS_STANDARD_FINDINGS")
    findings_fd = None
    findings_handle = None
    pycache_tmp = report.parent / ".pycache"
    pycache_tmp.mkdir(parents=True, exist_ok=True)
    actor_env = {
        **os.environ,
        **env,
        "IDEAS_STANDARD_REPORT": str(report),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPYCACHEPREFIX": str(pycache_tmp),
    }
    if findings_path is not None:
        state_path = env.get("IDEAS_STANDARD_STATE")
        if state_path is None:
            raise HandoffError("Builder findings require canonical state at launch")
        validate_builder_findings_handoff(status(Path(state_path)), Path(findings_path))
        if hasattr(os, "memfd_create") and sys.platform == "linux":
            findings_bytes = Path(findings_path).read_bytes()
            findings_fd = os.memfd_create("ideas-builder-findings", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
            try:
                with os.fdopen(os.dup(findings_fd), "wb") as stream:
                    stream.write(findings_bytes)
                fcntl.fcntl(
                    findings_fd,
                    fcntl.F_ADD_SEALS,
                    fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL,
                )
                validate_builder_findings_handoff(status(Path(state_path)), Path(findings_path))
                if Path(findings_path).read_bytes() != findings_bytes:
                    raise HandoffError("Builder findings changed during immutable handoff preparation")
            except Exception:
                os.close(findings_fd)
                raise
            actor_env["IDEAS_STANDARD_FINDINGS"] = f"/proc/self/fd/{findings_fd}"
        elif os.name == "nt":
            # Keep a read-only share lock until the actor exits: FILE_SHARE_READ
            # denies both writes and rename/delete of the validated handoff.
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_file = kernel32.CreateFileW
            create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.HANDLE]
            create_file.restype = wintypes.HANDLE
            findings_handle = create_file(str(Path(findings_path).resolve()), 0x80000000,
                                         0x00000001, None, 3, 0, None)
            if findings_handle == wintypes.HANDLE(-1).value:
                raise HandoffError(f"Cannot lock Builder findings: {ctypes.WinError(ctypes.get_last_error())}")
            try:
                validate_builder_findings_handoff(status(Path(state_path)), Path(findings_path))
            except Exception:
                kernel32.CloseHandle(findings_handle)
                raise
            actor_env["IDEAS_STANDARD_FINDINGS"] = str(findings_path)
        else:
            actor_env["IDEAS_STANDARD_FINDINGS"] = str(findings_path)

    if cancel_path is not None and cancel_path.exists():
        if findings_fd is not None:
            os.close(findings_fd)
        if findings_handle is not None:
            kernel32.CloseHandle(findings_handle)
        raise ActorInterrupted("CANCELLED")

    if write_sandbox and os.name != "nt" and sys.platform != "linux":
        raise HandoffError("Auditor write sandbox is unavailable; refusing unsafe execution")

    sandbox_ctx = (
        _windows_auditor_write_sandbox(workspace, report, actor_env)
        if write_sandbox and os.name == "nt"
        else contextlib.nullcontext()
    )

    try:
        report.parent.mkdir(parents=True, exist_ok=True)
        actor_log_path = report.parent / "last_actor_output.log"
        with sandbox_ctx:
            with open(actor_log_path, "wb") as log_file:
                if timeout_seconds is None and cancel_path is None:
                    process = subprocess.run(
                        command, cwd=workspace, env=actor_env, check=False,
                        stdin=subprocess.DEVNULL,
                        stdout=log_file, stderr=subprocess.STDOUT,
                        preexec_fn=_auditor_write_sandbox(report.parent) if write_sandbox and os.name != "nt" else None,
                        pass_fds=(findings_fd,) if findings_fd is not None else (),
                    )
                else:
                    process = subprocess.Popen(
                        command, cwd=workspace, env=actor_env,
                        stdin=subprocess.DEVNULL,
                        stdout=log_file, stderr=subprocess.STDOUT,
                        preexec_fn=_auditor_write_sandbox(report.parent) if write_sandbox and os.name != "nt" else None,
                        pass_fds=(findings_fd,) if findings_fd is not None else (),
                        start_new_session=os.name != "nt",
                    )
                    deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
                    while process.poll() is None:
                        reason = None
                        if cancel_path is not None and cancel_path.exists():
                            reason = "CANCELLED"
                        elif deadline is not None and time.monotonic() >= deadline:
                            reason = "TIMEOUT"
                        if reason is not None:
                            _terminate_actor_process(process)
                            process.wait()
                            raise ActorInterrupted(reason)
                        time.sleep(0.02)
                    process.wait()
    finally:
        if findings_fd is not None:
            os.close(findings_fd)
        if findings_handle is not None:
            kernel32.CloseHandle(findings_handle)
    if process.returncode == 126 and write_sandbox:
        raise HandoffError("Auditor write sandbox is unavailable; refusing unsafe execution")
    if process.returncode != 0:
        raise ActorExitError(process.returncode)
    if not report.is_file():
        raise HandoffError(f"Actor did not produce report: {report}")


def write_state_snapshot(state_path: Path, audit_root: Path, target: str) -> Path:
    snapshot_root = audit_root / "state-snapshots"
    snapshot_root.mkdir(parents=True, exist_ok=True)
    snapshot = snapshot_root / f"{target}.json"
    current_bytes = state_path.read_bytes()
    if snapshot.exists():
        if snapshot.read_bytes() != current_bytes:
            raise HandoffError("Existing Auditor state snapshot differs from canonical state")
        return snapshot
    snapshot.write_bytes(current_bytes)
    snapshot.chmod(snapshot.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    return snapshot


def verify_audit_after(
    workspace: Path,
    state_path: Path,
    target: str,
    state_before: bytes,
) -> None:
    if state_path.read_bytes() != state_before:
        raise HandoffError("Canonical state changed during Auditor execution")
    if status(state_path).get("audit_target_sha") != target:
        raise HandoffError("audit_target_sha changed during Auditor execution")
    if git_head(workspace) != target:
        raise HandoffError("Auditor workspace HEAD changed from audit_target_sha")
    dirty = subprocess.run(
        ["git", "-C", str(workspace), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if dirty.returncode != 0 or dirty.stdout:
        raise HandoffError("Auditor modified or invalidated the frozen audit workspace")


def validate_accepted_audit_digest(current: dict[str, Any], report_path: Path) -> bytes:
    expected = current.get("last_audit_report_sha256")
    if not isinstance(expected, str) or not _SHA256.fullmatch(expected):
        raise HandoffError("Accepted audit report digest is missing or invalid")
    raw = report_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise HandoffError("Audit report differs from accepted audit report digest")
    return raw


def prepare_builder_findings(
    current: dict[str, Any],
    reports_dir: Path,
) -> Path | None:
    if current["machine_state"] != "FIX_REQUIRED":
        return None
    if current.get("last_audit_result") != "FAIL" or current.get("audit_round", 0) < 1:
        raise HandoffError("FIX_REQUIRED requires a recorded FAIL audit round")
    report_ref = current.get("last_audit_report")
    if not report_ref:
        raise HandoffError("FIX_REQUIRED requires last_audit_report")
    accepted_bytes = validate_accepted_audit_digest(current, Path(report_ref))
    validate_accepted_audit_report(Path(report_ref), current["audit_round"])
    report = json.loads(accepted_bytes)
    validate_with_schema(report, "audit")
    target = current.get("audit_target_sha")
    if (
        report["audit_result"] != "FAIL"
        or report["audited_sha"] != target
        or current.get("last_audited_sha") != target
    ):
        raise HandoffError("Audit findings do not match the current failed audit target")
    finding_ids = [finding["id"] for finding in report["findings"]]
    if len(finding_ids) != len(set(finding_ids)):
        raise HandoffError("Duplicate audit finding ID")
    findings = []
    for finding in report["findings"]:
        forwarded = dict(finding)
        resolve_evidence(Path(report_ref).parent / "evidence", finding["evidence"], current)
        findings.append(forwarded)
    handoff = reports_dir / "builder-findings.json"
    write_json(
        handoff,
        {
            "audit_target_sha": target,
            "audit_round": current["audit_round"],
            "findings": findings,
        },
    )
    validate_builder_findings_handoff(current, handoff)
    return handoff


def validate_builder_findings_handoff(current: dict[str, Any], handoff: Path) -> None:
    payload = load_json(handoff)
    validate_with_schema(payload, "builder-findings")
    if (
        payload["audit_target_sha"] != current.get("audit_target_sha")
        or payload["audit_round"] != current.get("audit_round")
    ):
        raise HandoffError("Builder findings are not linked to the current audit round")
    report_ref = current.get("last_audit_report")
    if not report_ref:
        raise HandoffError("Builder findings require the canonical audit report")
    accepted_bytes = validate_accepted_audit_digest(current, Path(report_ref))
    validate_accepted_audit_report(Path(report_ref), current["audit_round"])
    audit_report = json.loads(accepted_bytes)
    validate_with_schema(audit_report, "audit")
    if (
        audit_report.get("audit_result") != "FAIL"
        or audit_report.get("audited_sha") != current.get("audit_target_sha")
        or payload["findings"] != audit_report.get("findings")
    ):
        raise HandoffError("Builder findings differ from the canonical audit report")
    finding_ids = [finding["id"] for finding in payload["findings"]]
    if len(finding_ids) != len(set(finding_ids)):
        raise HandoffError("Duplicate audit finding ID")
    for finding in payload["findings"]:
        resolve_evidence(handoff.parent / "evidence", finding["evidence"], current)


def prepare_reaudit_handoff(
    current: dict[str, Any],
    builder_report: dict[str, Any],
    repository: Path,
    findings_handoff: Path,
    reports_dir: Path,
) -> Path:
    previous_sha = current["last_audited_sha"]
    new_sha = builder_report["result_sha"]
    findings_payload = load_json(findings_handoff)
    audit_payload = load_json(Path(current["last_audit_report"]))
    validate_with_schema(audit_payload, "audit")
    validate_builder_findings_handoff(current, findings_handoff)
    forwarded_findings = findings_payload["findings"]
    if len(forwarded_findings) != len(audit_payload["findings"]):
        raise HandoffError("Reaudit findings are not linked to the corrected audit")
    evidence_root = reports_dir / "evidence"
    reaudit_findings = []
    for forwarded, source in zip(forwarded_findings, audit_payload["findings"]):
        forwarded_without_evidence = {key: value for key, value in forwarded.items() if key != "evidence"}
        source_without_evidence = {key: value for key, value in source.items() if key != "evidence"}
        resolved = resolve_evidence(findings_handoff.parent / "evidence", forwarded["evidence"], current)
        source_resolved = resolve_evidence(Path(current["last_audit_report"]).parent / "evidence", source["evidence"], current)
        if forwarded_without_evidence != source_without_evidence or resolved["content"] != source_resolved["content"]:
            raise HandoffError("Reaudit findings are not linked to the corrected audit")
        reaudit_finding = dict(forwarded)
        reaudit_finding["evidence"] = store_evidence(
            evidence_root,
            evidence_id=forwarded["evidence"]["evidence_id"],
            content=resolved["content"],
            current=current,
        )
        reaudit_findings.append(reaudit_finding)
    if (
        findings_payload.get("audit_target_sha") != previous_sha
        or findings_payload.get("audit_round") != current["audit_round"]
    ):
        raise HandoffError("Reaudit findings are not linked to the corrected audit")

    changed_paths = reaudit_changed_paths(repository, previous_sha, new_sha)
    declared_paths = sorted(builder_report["changed_paths"])
    findings = audit_payload["findings"]
    checks = audit_payload["checks"]
    source_evidence_root = Path(current["last_audit_report"]).parent / "evidence"
    context_findings = [
        {**finding, "evidence": resolve_evidence(source_evidence_root, finding["evidence"], current)["content"]}
        for finding in findings
    ]
    context_checks = [
        {**check, "evidence": resolve_evidence(source_evidence_root, check["evidence"], current)["content"]}
        for check in checks
    ]
    reasons = reaudit_context_reasons(
        context_findings,
        context_checks,
        changed_paths,
        declared_paths,
        audit_payload["residual_risks"],
    )

    payload = {
        "schema_version": "0.1",
        "previous_audited_sha": previous_sha,
        "new_audit_target_sha": new_sha,
        "audit_round": current["audit_round"],
        "context_mode": "FULL" if reasons else "DELTA",
        "full_context_reasons": reasons,
        "findings": reaudit_findings,
        "changed_paths": changed_paths,
        "declared_changed_paths": declared_paths,
        "reusable_evidence": [
            {
                "check_id": check["id"],
                "status": check["status"],
                "evidence": store_evidence(
                    evidence_root,
                    evidence_id=evidence_id(
                        "check",
                        check["id"],
                        current,
                        resolve_evidence(source_evidence_root, check["evidence"], current)["content"],
                    ),
                    content=resolve_evidence(source_evidence_root, check["evidence"], current)["content"],
                    current=current,
                ),
            }
            for check in checks
        ],
    }
    validate_with_schema(payload, "reaudit")
    handoff = reports_dir / "reaudit-handoff.json"
    write_json(handoff, payload)
    return handoff


def reaudit_changed_paths(repository: Path, previous_sha: str, new_sha: str) -> list[str]:
    diff = subprocess.run(
        ["git", "-C", str(repository), "diff", "--name-only", f"{previous_sha}..{new_sha}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if diff.returncode != 0:
        raise HandoffError("Cannot resolve reaudit delta")
    return sorted(path for path in diff.stdout.splitlines() if path)


def reaudit_context_reasons(
    findings: list[dict[str, Any]],
    checks: list[dict[str, Any]],
    changed_paths: list[str],
    declared_paths: list[str],
    residual_risks: list[Any],
) -> list[str]:
    reasons = []
    if changed_paths != declared_paths:
        reasons.append("OUT_OF_SCOPE_CHANGE")
    if (
        not findings
        or any(not finding.get("evidence") for finding in findings)
        or not checks
        or any(not check.get("evidence") for check in checks)
    ):
        reasons.append("MISSING_EVIDENCE")
    if any(finding["severity"] in {"HIGH", "CRITICAL"} for finding in findings) or residual_risks:
        reasons.append("MATERIAL_RISK")
    return reasons


def validate_reaudit_handoff(current: dict[str, Any], handoff: Path, repository: Path) -> None:
    payload = load_json(handoff)
    validate_with_schema(payload, "reaudit")
    audit_payload = load_json(Path(current["last_audit_report"]))
    validate_with_schema(audit_payload, "audit")
    builder_report_ref = current.get("last_builder_report")
    if not builder_report_ref:
        raise HandoffError("Reaudit requires the canonical Builder report")
    builder_payload = load_json(Path(builder_report_ref))
    validate_with_schema(builder_payload, "builder")
    canonical_declared_paths = sorted(builder_payload["changed_paths"])
    if (
        builder_payload["result_sha"] != current["audit_target_sha"]
        or payload["declared_changed_paths"] != canonical_declared_paths
    ):
        raise HandoffError("Reaudit handoff differs from the canonical Builder report")
    evidence_context = {**current, "audit_target_sha": payload["previous_audited_sha"]}
    if len(payload["findings"]) != len(audit_payload["findings"]):
        raise HandoffError("Reaudit handoff is inconsistent with canonical evidence")
    for forwarded, source in zip(payload["findings"], audit_payload["findings"]):
        forwarded_without_evidence = {key: value for key, value in forwarded.items() if key != "evidence"}
        source_without_evidence = {key: value for key, value in source.items() if key != "evidence"}
        resolved = resolve_evidence(handoff.parent / "evidence", forwarded["evidence"], evidence_context)
        source_resolved = resolve_evidence(Path(current["last_audit_report"]).parent / "evidence", source["evidence"], evidence_context)
        if forwarded_without_evidence != source_without_evidence or resolved["content"] != source_resolved["content"]:
            raise HandoffError("Reaudit handoff is inconsistent with canonical evidence")
    reusable_evidence = payload["reusable_evidence"]
    if len(reusable_evidence) != len(audit_payload["checks"]):
        raise HandoffError("Reaudit handoff is inconsistent with canonical evidence")
    for forwarded, source in zip(reusable_evidence, audit_payload["checks"]):
        resolved = resolve_evidence(handoff.parent / "evidence", forwarded["evidence"], evidence_context)
        source_resolved = resolve_evidence(Path(current["last_audit_report"]).parent / "evidence", source["evidence"], evidence_context)
        if (
            forwarded["check_id"] != source["id"]
            or forwarded["status"] != source["status"]
            or resolved["content"] != source_resolved["content"]
        ):
            raise HandoffError("Reaudit handoff is inconsistent with canonical evidence")
    changed_paths = reaudit_changed_paths(
        repository, payload["previous_audited_sha"], payload["new_audit_target_sha"]
    )
    context_findings = [
        {**finding, "evidence": resolve_evidence(Path(current["last_audit_report"]).parent / "evidence", finding["evidence"], evidence_context)["content"]}
        for finding in audit_payload["findings"]
    ]
    context_checks = [
        {**check, "evidence": resolve_evidence(Path(current["last_audit_report"]).parent / "evidence", check["evidence"], evidence_context)["content"]}
        for check in audit_payload["checks"]
    ]
    reasons = reaudit_context_reasons(
        context_findings,
        context_checks,
        changed_paths,
        canonical_declared_paths,
        audit_payload["residual_risks"],
    )
    if (
        payload["new_audit_target_sha"] != current["audit_target_sha"]
        or payload["previous_audited_sha"] != audit_payload["audited_sha"]
        or payload["audit_round"] != current["audit_round"]
        or payload["changed_paths"] != changed_paths
    ):
        raise HandoffError("Reaudit handoff is inconsistent with canonical evidence")
    if payload["full_context_reasons"] != reasons or payload["context_mode"] != ("FULL" if reasons else "DELTA"):
        raise HandoffError("Reaudit context decision is inconsistent")



def validate_builder_result(
    current: dict[str, Any],
    payload: dict[str, Any],
    repository: Path,
    findings_handoff: Path | None,
    findings_before: bytes | None,
    builder_workspace: Path | None = None,
) -> None:
    validation_payload = json.loads(json.dumps(payload))
    for check in validation_payload.get("checks", []):
        if isinstance(check.get("evidence"), str):
            check["evidence"] = {"evidence_id": "transient", "sha256": "0" * 64}
    validate_with_schema(validation_payload, "builder")
    result_sha = payload["result_sha"]
    verify_commit(repository, result_sha)
    previous_target = current.get("audit_target_sha")
    if current["machine_state"] == "FIX_REQUIRED" and (
        not previous_target or result_sha == previous_target
    ):
        raise HandoffError("Builder correction must produce a new SHA")
    if git_head(builder_workspace or repository) != result_sha:
        raise HandoffError("Builder report result_sha does not match Builder workspace HEAD")
    if current["machine_state"] != "FIX_REQUIRED":
        return
    if findings_handoff is None or findings_before is None:
        raise HandoffError("Builder correction requires findings from the failed audit")
    if not findings_handoff.is_file() or findings_handoff.read_bytes() != findings_before:
        raise HandoffError("Builder findings changed during correction")
    findings = load_json(findings_handoff)
    if (
        findings.get("audit_target_sha") != previous_target
        or findings.get("audit_round") != current.get("audit_round")
    ):
        raise HandoffError("Builder findings are not linked to the corrected audit round")


def failure_kind(exc: Exception) -> str:
    if isinstance(exc, ActorExitError):
        return "ACTOR_EXIT_NONZERO"
    if isinstance(exc, ActorInterrupted):
        return exc.reason
    if isinstance(exc, json.JSONDecodeError):
        return "INVALID_JSON"
    if isinstance(exc, HandoffError):
        return "HANDOFF_REJECTED"
    if isinstance(exc, OSError):
        return "IO_ERROR"
    return "INTERNAL_ERROR"


def safe_failure_message(exc: Exception) -> str:
    if isinstance(exc, ActorExitError):
        return f"Actor command failed with exit code {exc.exit_code}"
    if isinstance(exc, ActorInterrupted):
        return exc.reason
    message = str(exc)
    safe_exact = {
        "Runner state lock is busy",
        "Persisted operation journal result is not derived from its report",
        "Persisted operation record identity or result is inconsistent",
        "Operation payload differs from the persisted identity",
        "operation_id does not match the current state transition",
    }
    if message in safe_exact:
        return message
    if message.startswith("state schema validation failed:"):
        return "state schema validation failed"
    if message.startswith("Unknown result SHA"):
        return "Unknown result SHA"
    if "does not match audit_target_sha" in message:
        return "Audit workspace does not match audit_target_sha"
    if message.startswith("Operation retry limit exceeded"):
        return "Operation retry limit exceeded"
    if message.startswith("Operation has already been completed"):
        return message
    if message.startswith("Report has already been accepted"):
        return message
    if message.startswith("Operation interrupted by "):
        return message
    if message == "Cancellation request must be cleared before resuming":
        return message
    return failure_kind(exc)


def persist_runner_failure(
    config_path: Path, config: dict[str, Any] | None, state_path: Path | None, exc: Exception
) -> None:
    raw = state_path.read_bytes() if state_path is not None and state_path.is_file() else None
    current = None
    try:
        if raw is not None and state_path is not None:
            current = status(state_path)
    except (HandoffError, OSError, ValueError, UnicodeError, json.JSONDecodeError):
        pass
    root = (
        resolve_path(config_path, config["reports_dir"])
        if config is not None else config_path.parent
    )
    actor = current.get("next_actor") if current is not None else None
    operation_id = None
    journal_present = False
    if config is not None:
        journal_present = any((root / "operations").glob("op-*.journal.json"))
        try:
            pending = load_recovery_journal(root)
        except (HandoffError, json.JSONDecodeError, OSError, ValueError, UnicodeError):
            pending = None
        if pending is not None:
            operation_id = pending[1]["operation_id"]
    if operation_id is None and not journal_present and actor in {"BUILDER", "AUDITOR"}:
        operation_id = operation_identity(current, actor)[0]
    evidence = {
        "schema_version": "0.1",
        "failure_id": f"failure-{uuid.uuid4().hex}",
        "kind": failure_kind(exc),
        "runner_exit_code": 2,
        "actor_exit_code": exc.exit_code if isinstance(exc, ActorExitError) else None,
        "run_id": current["run_id"] if current is not None else None,
        "operation_id": operation_id,
        "machine_state": current["machine_state"] if current is not None else None,
        "state_sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
    }
    validate_with_schema(evidence, "runner-failure")
    failure_root = root / "runner-failures"
    _write_canonical_atomic(failure_root / f"{evidence['failure_id']}.json", evidence)


def run_once(config_path: Path) -> dict[str, Any]:
    config_path = config_path.resolve()
    config = load_config(config_path)
    state_path = resolve_path(config_path, config["state_path"])
    with StateLock(state_path, float(config.get("lock_timeout_seconds", 0))):
        try:
            return _run_once_locked(config_path, config)
        except Exception as exc:
            persist_runner_failure(config_path, config, state_path, exc)
            setattr(exc, "_runner_failure_recorded", True)
            raise


def _run_once_locked(config_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    repository = resolve_path(config_path, config["repository"])
    state_path = resolve_path(config_path, config["state_path"])
    reports_dir = resolve_path(config_path, config["reports_dir"])
    builder_workspace = resolve_path(config_path, config["builder_workspace"])
    audit_root = resolve_path(config_path, config["audit_workspaces"])
    validate_workspaces(builder_workspace, audit_root)
    validate_auditor_boundaries(state_path, audit_root, reports_dir)

    current = status(state_path)
    requested_operation = config.get("operation_id")
    recovery = load_recovery_journal(reports_dir)
    recovering_report = False
    resuming_interrupted = False
    if recovery is not None:
        journal_path, journal = recovery
        operation_id = journal["operation_id"]
        actor = journal["actor"]
        operation_source = journal["source"]
        if requested_operation is not None and requested_operation != operation_id:
            raise HandoffError("Requested operation differs from incomplete operation journal")
        if journal["phase"] == "REPORT_READY":
            return finish_recovered_operation(
                state_path, reports_dir, journal_path, journal, current, True
            )
        if journal["phase"] == "INTERRUPTED":
            if current != journal["source_state"]:
                raise HandoffError("Interrupted operation source state has changed")
            if not config.get("resume_interrupted", False):
                raise HandoffError(
                    f"Operation interrupted by {journal['interruption_reason']}; explicit resume_interrupted required"
                )
            if "cancel_path" in config and resolve_path(config_path, config["cancel_path"]).exists():
                raise HandoffError("Cancellation request must be cleared before resuming")
            Path(journal["report_path"]).unlink(missing_ok=True)
            journal = prepare_interrupted_resume(journal_path, journal)
            resuming_interrupted = True
        if operation_identity(current, actor)[0] != operation_id:
            raise HandoffError("Prepared operation journal does not match current state")
        recovering_report = not resuming_interrupted
    elif requested_operation is not None:
        replayed = replay_operation(reports_dir, requested_operation, current)
        if replayed is not None:
            return replayed
    if recovery is None:
        actor = next_actor(current)
        if actor in {"BUILDER", "AUDITOR"}:
            expected_operation, operation_source = operation_identity(current, actor)
            if requested_operation is not None and requested_operation != expected_operation:
                raise HandoffError("operation_id does not match the current state transition")
            operation_id = requested_operation or expected_operation
            if requested_operation is None and _operation_paths(reports_dir, operation_id)[0].exists():
                raise HandoffError(f"Operation has already been completed: {operation_id}")
        elif requested_operation is not None:
            raise HandoffError("operation_id is not valid while automation is stopped")
        else:
            return current
    max_retries = int(config.get("max_retries", 3))
    if not recovering_report and count_operation_failures(reports_dir, operation_id) >= max_retries:
        raise HandoffError(f"Operation retry limit exceeded: {operation_id}")
    common_env = {
        "IDEAS_STANDARD_PROJECT_ID": current["project_id"],
        "IDEAS_STANDARD_PHASE": current["phase"],
        "IDEAS_STANDARD_GATE": current["gate"],
    }
    actor_timeout = config.get("actor_timeout_seconds")
    cancel_path = resolve_path(config_path, config["cancel_path"]) if "cancel_path" in config else None

    if actor == "BUILDER":
        if not builder_workspace.is_dir() or not os.access(builder_workspace, os.W_OK):
            raise HandoffError("Builder workspace must exist and be writable")
        report = reports_dir / "builder-report.json"
        if recovering_report:
            if Path(journal["report_path"]) != report or not report.is_file():
                raise HandoffError("Prepared Builder operation has no durable report")
        elif not resuming_interrupted:
            report.unlink(missing_ok=True)
            journal_path, journal = prepare_operation_journal(
                reports_dir, operation_id, operation_source, current, actor, report
            )
        builder_env = {**common_env, "IDEAS_STANDARD_STATE": str(state_path)}
        findings_handoff = prepare_builder_findings(current, reports_dir)
        findings_before = None
        if findings_handoff is not None:
            validate_builder_findings_handoff(current, findings_handoff)
            builder_env["IDEAS_STANDARD_FINDINGS"] = str(findings_handoff)
            findings_before = findings_handoff.read_bytes()
        if not recovering_report:
            try:
                run_actor(
                    config["builder_command"], builder_workspace, report, builder_env,
                    **({"timeout_seconds": actor_timeout} if actor_timeout is not None else {}),
                    **({"cancel_path": cancel_path} if cancel_path is not None else {}),
                )
            except ActorInterrupted as exc:
                mark_actor_interrupted(journal_path, journal, exc.reason)
                report.unlink(missing_ok=True)
                raise
            except (HandoffError, OSError):
                if not report.is_file():
                    journal_path.unlink(missing_ok=True)
                raise
        payload = load_json(report)
        report_context = {**current, "audit_target_sha": payload.get("result_sha")}
        payload = canonicalize_report_evidence(report, "builder", report_context, reports_dir / "evidence")
        validate_builder_result(
            current,
            payload,
            repository,
            findings_handoff,
            findings_before,
            builder_workspace,
        )
        report_digest = hashlib.sha256(report.read_bytes()).hexdigest()
        check_report_not_already_accepted(reports_dir, report_digest, operation_id)
        if current["machine_state"] == "FIX_REQUIRED":
            prepare_reaudit_handoff(
                current,
                payload,
                repository,
                findings_handoff,
                reports_dir,
            )
        result = compute_transition(state_path, current, report, actor, operation_id)
        journal = mark_report_ready(journal_path, journal, report, result)
        return finish_recovered_operation(
            state_path, reports_dir, journal_path, journal, current, recovering_report
        )

    if actor == "AUDITOR":
        target = current.get("audit_target_sha")
        if not target:
            raise HandoffError("Auditor requires audit_target_sha")
        auditor_env = {
            **common_env,
            "IDEAS_STANDARD_AUDIT_TARGET_SHA": target,
        }
        if current["audit_round"] > 0:
            reaudit_handoff = reports_dir / "reaudit-handoff.json"
            if not reaudit_handoff.is_file():
                raise HandoffError("Reaudit requires reaudit-handoff.json")
            validate_reaudit_handoff(current, reaudit_handoff, repository)
            auditor_env["IDEAS_STANDARD_REAUDIT_HANDOFF"] = str(reaudit_handoff)
        workspace = prepare_audit_workspace(repository, audit_root, target)
        snapshot = write_state_snapshot(state_path, audit_root, target)
        state_before = state_path.read_bytes()
        report = reports_dir / "audit-report.json"
        if recovering_report:
            if Path(journal["report_path"]) != report or not report.is_file():
                raise HandoffError("Prepared Auditor operation has no durable report")
        elif not resuming_interrupted:
            report.unlink(missing_ok=True)
            journal_path, journal = prepare_operation_journal(
                reports_dir, operation_id, operation_source, current, actor, report
            )
        auditor_env["IDEAS_STANDARD_STATE_SNAPSHOT"] = str(snapshot)
        if not recovering_report:
            try:
                run_actor(
                    config["auditor_command"],
                    workspace,
                    report,
                    auditor_env,
                    write_sandbox=True,
                    **({"timeout_seconds": actor_timeout} if actor_timeout is not None else {}),
                    **({"cancel_path": cancel_path} if cancel_path is not None else {}),
                )
            except ActorInterrupted as exc:
                mark_actor_interrupted(journal_path, journal, exc.reason)
                report.unlink(missing_ok=True)
                raise
            except (HandoffError, OSError):
                if not report.is_file():
                    journal_path.unlink(missing_ok=True)
                raise
        verify_audit_after(workspace, state_path, target, state_before)
        report_digest = hashlib.sha256(report.read_bytes()).hexdigest()
        check_report_not_already_accepted(reports_dir, report_digest, operation_id)
        audit_context = {**current, "audit_round": current["audit_round"] + 1}
        canonicalize_report_evidence(report, "audit", audit_context, reports_dir / "evidence")
        result = compute_transition(state_path, current, report, actor, operation_id)
        journal = mark_report_ready(journal_path, journal, report, result)
        return finish_recovered_operation(
            state_path, reports_dir, journal_path, journal, current, recovering_report
        )

    return current


def preflight_check(config: dict[str, Any], config_path: Path) -> list[str]:
    """Fast sanity check before executing multi-agent runs."""
    issues = []
    repo = resolve_path(config_path, config.get("repository", ""))
    if not repo.exists():
        issues.append(f"Repository path does not exist: {repo}")
    builder_ws = resolve_path(config_path, config.get("builder_workspace", ""))
    if not builder_ws.exists():
        issues.append(f"Builder workspace does not exist: {builder_ws}")
    return issues


def run_loop(config_path: Path, max_steps: int = 20) -> dict[str, Any]:
    config_path = config_path.resolve()
    config = load_config(config_path)
    issues = preflight_check(config, config_path)
    if issues:
        raise HandoffError(f"Preflight check failed: {'; '.join(issues)}")
    state_path = resolve_path(config_path, config["state_path"])

    steps = 0
    current = status(state_path)
    while steps < max_steps:
        if current.get("machine_state") in {"WAITING_PRODUCT_AUTHORITY", "BLOCKED", "GATE_APPROVED"}:
            break
        current = run_once(config_path)
        steps += 1
        if current.get("machine_state") in {"WAITING_PRODUCT_AUTHORITY", "BLOCKED", "GATE_APPROVED"}:
            break
    if steps >= max_steps and current.get("machine_state") not in {"WAITING_PRODUCT_AUTHORITY", "BLOCKED", "GATE_APPROVED"}:
        raise HandoffError("Runner loop exceeded max_steps without reaching terminal state")
    return current


def run_task_queue(
    config_path: Path,
    queue_path: Path,
    max_steps_per_task: int = 20,
    invocation_id: str | None = None,
    on_conflict: str = "raise",
) -> dict[str, Any]:
    """Execute a sequence of pre-authorized tasks within the same phase (O0 v2 M4).

    Invariants enforced:
      - Queue remains outside the task state machine (orchestrator-state.json).
      - Each task receives its own unique run_id matching ^run-[0-9a-f]{64}$.
      - Upon technical PASS, only the next authorized task of the same phase starts.
      - Automatic phase advancement is prohibited (O0-C29); stops on phase mismatch.
      - Technical PASS never infers product approval (approval remains null).
      - Stops on queue completion, human gate, BLOCKED state, or unauthorized scope change.
      - Idempotent invocation: duplicate invocation preserves accepted state without mutation.
    """
    config_path = config_path.resolve()
    queue_path = queue_path.resolve()
    base_config = load_config(config_path)
    state_path = resolve_path(config_path, base_config["state_path"])
    reports_dir = resolve_path(config_path, base_config["reports_dir"])
    repository = resolve_path(config_path, base_config["repository"])
    audit_root = resolve_path(config_path, base_config["audit_workspaces"])

    queue_data = load_json(queue_path)
    validate_with_schema(queue_data, "task-queue")

    effective_invocation_id = (
        invocation_id
        or queue_data.get("invocation_id")
        or queue_data.get("queue_id")
    )
    reports_dir.mkdir(parents=True, exist_ok=True)
    queue_record_path = (
        reports_dir / f"task-queue-execution-{effective_invocation_id}.json"
        if effective_invocation_id
        else reports_dir / "task-queue-execution.json"
    )

    # Pre-execution mutation lock & idempotency check
    existing_record: dict[str, Any] | None = None
    if queue_record_path.is_file():
        try:
            existing_record = load_json(queue_record_path)
        except Exception:
            existing_record = None
    elif effective_invocation_id and (reports_dir / "task-queue-execution.json").is_file():
        try:
            candidate = load_json(reports_dir / "task-queue-execution.json")
            if candidate.get("invocation_id") == effective_invocation_id:
                existing_record = candidate
        except Exception:
            existing_record = None

    if existing_record is not None:
        existing_status = existing_record.get("status")
        if existing_status in ("ACCEPTED", "IN_PROGRESS", "COMPLETED"):
            if on_conflict == "raise":
                raise HandoffError(
                    f"INVOCATION_ALREADY_EXISTS: Queue invocation '{effective_invocation_id or queue_path.name}' "
                    f"already exists with status '{existing_status}'."
                )
            return existing_record

    queue_phase = queue_data["phase"]
    tasks = queue_data["tasks"]
    if not tasks:
        raise HandoffError("Task queue must contain at least one task")

    seen_ids: set[str] = set()
    for task in tasks:
        task_id = task["task_id"]
        if task_id in seen_ids:
            raise HandoffError(f"Duplicate task_id in queue: {task_id}")
        seen_ids.add(task_id)
        if task["phase"] != queue_phase:
            raise HandoffError(
                f"Task '{task_id}' phase '{task['phase']}' differs from queue phase '{queue_phase}'; "
                "automatic phase advancement is prohibited by O0-C29"
            )

    queue_record: dict[str, Any] = {
        "schema_version": "0.1",
        "queue_path": str(queue_path),
        "invocation_id": effective_invocation_id,
        "phase": queue_phase,
        "total_tasks": len(tasks),
        "executed_tasks": [],
        "results": [],
        "status": "IN_PROGRESS",
        "stop_reason": None,
        "active_task_index": None,
        "active_task_id": None,
    }

    # The durable invocation identity must exist before any task state can be
    # removed or initialized. An interrupted invocation is therefore rejected
    # on replay without touching an accepted task result.
    write_json(queue_record_path, queue_record)

    builder_branch = base_config.get("builder_branch", "main")
    project_id = base_config.get("project_id", "ideias-standard")

    reports_dir.mkdir(parents=True, exist_ok=True)
    task_config_path = config_path.parent / f"{config_path.stem}.task_active.json"

    try:
        for idx, task in enumerate(tasks):
            task_id = task["task_id"]
            task_run_id = f"run-{hashlib.sha256(f'{task_id}-{uuid.uuid4().hex}'.encode('utf-8')).hexdigest()}"

            queue_record["active_task_index"] = idx
            queue_record["active_task_id"] = task_id
            write_json(queue_record_path, queue_record)

            # Verify phase invariance
            if task["phase"] != queue_phase:
                queue_record["status"] = "STOPPED_PHASE_GATE"
                queue_record["stop_reason"] = (
                    f"Task '{task_id}' requires phase '{task['phase']}' while current phase is '{queue_phase}'; "
                    "automatic phase advancement is prohibited by O0-C29"
                )
                break

            # Clean any leftover active reports from prior tasks
            state_path.unlink(missing_ok=True)
            for item in list(reports_dir.glob("audit-report.json*")) + list(reports_dir.glob("builder-report.json*")):
                item.unlink(missing_ok=True)
            task_state = {
                "schema_version": "0.1",
                "run_id": task_run_id,
                "project_id": project_id,
                "phase": queue_phase,
                "gate": "NONE",
                "machine_state": "READY_FOR_BUILD",
                "next_actor": "BUILDER",
                "blocked_reason": None,
                "builder_branch": builder_branch,
                "builder_executor_id": None,
                "auditor_executor_id": None,
                "product_authority_id": "owner",
                "builder_head_sha": None,
                "audit_target_sha": None,
                "last_audited_sha": None,
                "audit_round": 0,
                "max_audit_rounds": int(base_config.get("max_retries", 3)),
                "last_builder_report": None,
                "last_audit_report": None,
                "last_audit_report_sha256": None,
                "last_audit_result": None,
                "human_gate_required": True,
                "approval": None,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "message": f"Initialized task {task_id}: {task['goal']}",
            }
            validate_with_schema(task_state, "state")
            write_json(state_path, task_state)

            # Build task-specific configuration
            task_config = dict(base_config)
            if "builder_command" in task:
                task_config["builder_command"] = task["builder_command"]
            if "auditor_command" in task:
                task_config["auditor_command"] = task["auditor_command"]
            write_json(task_config_path, task_config)

            # Inject task context into environment
            task_env_vars = {
                "IDEAS_STANDARD_TASK_ID": task_id,
                "IDEAS_STANDARD_TASK_GOAL": task["goal"],
                "IDEAS_STANDARD_TASK_SCOPE": json.dumps(task["scope"]),
                "IDEAS_STANDARD_TASK_CRITERIA": json.dumps(task["acceptance_criteria"]),
            }
            if "model_tier" in task:
                task_env_vars["IDEAS_STANDARD_MODEL_TIER"] = str(task["model_tier"])
            for k, v in task_env_vars.items():
                os.environ[k] = v

            # Execute the single-task loop
            try:
                final_task_state = run_loop(task_config_path, max_steps=max_steps_per_task)
            finally:
                for k in task_env_vars:
                    os.environ.pop(k, None)

            # Invariants verification
            if final_task_state.get("run_id") != task_run_id:
                raise HandoffError(f"Task {task_id} run_id was mutated during execution")
            if final_task_state.get("approval") is not None:
                raise HandoffError(f"Task {task_id} unexpectedly recorded product approval")

            # Archive task reports
            task_reports_dir = reports_dir / "tasks" / task_id
            task_reports_dir.mkdir(parents=True, exist_ok=True)
            for item in list(reports_dir.glob("audit-report.json*")) + list(reports_dir.glob("builder-report.json*")):
                shutil.copy2(item, task_reports_dir / item.name)
                item.unlink(missing_ok=True)

            task_summary = {
                "task_id": task_id,
                "run_id": task_run_id,
                "goal": task["goal"],
                "scope": task["scope"],
                "machine_state": final_task_state.get("machine_state"),
                "last_audit_result": final_task_state.get("last_audit_result"),
                "last_audited_sha": final_task_state.get("last_audited_sha"),
                "audit_round": final_task_state.get("audit_round"),
                "approval": final_task_state.get("approval"),
            }
            queue_record["executed_tasks"].append(task_summary)
            queue_record["results"].append(task_id)
            write_json(queue_record_path, queue_record)

            # Check stop conditions
            if final_task_state.get("machine_state") == "BLOCKED":
                queue_record["status"] = "BLOCKED"
                queue_record["stop_reason"] = (
                    f"Task '{task_id}' entered BLOCKED state: {final_task_state.get('blocked_reason')}"
                )
                break

            if (
                final_task_state.get("machine_state") != "WAITING_PRODUCT_AUTHORITY"
                or final_task_state.get("last_audit_result") != "PASS"
            ):
                queue_record["status"] = "STOPPED"
                queue_record["stop_reason"] = (
                    f"Task '{task_id}' finished in unexpected state: {final_task_state.get('machine_state')} "
                    f"with result {final_task_state.get('last_audit_result')}"
                )
                break

            # Technical PASS achieved
            if idx + 1 < len(tasks):
                next_task = tasks[idx + 1]
                # Check phase boundary (O0-C29)
                if next_task["phase"] != queue_phase:
                    queue_record["status"] = "STOPPED_PHASE_GATE"
                    queue_record["stop_reason"] = (
                        f"Next task '{next_task['task_id']}' specifies phase '{next_task['phase']}', "
                        f"differing from current phase '{queue_phase}'; automatic phase advancement prohibited by O0-C29"
                    )
                    break
                # Check explicit human gate requirement
                if task.get("human_gate_required") and task.get("gate", "NONE") != "NONE":
                    queue_record["status"] = "STOPPED_HUMAN_GATE"
                    queue_record["stop_reason"] = (
                        f"Task '{task_id}' requires explicit human gate before proceeding"
                    )
                    break
            else:
                queue_record["status"] = "COMPLETED"
                queue_record["active_task_index"] = None
                queue_record["active_task_id"] = None
            if base_config.get("cleanup_audit_workspaces", False) or os.environ.get("IDEAS_STANDARD_CLEANUP_WORKSPACES") == "1":
                last_sha = final_task_state.get("last_audited_sha")
                if last_sha:
                    cleanup_audit_workspace(repository, audit_root / last_sha)
    finally:
        task_config_path.unlink(missing_ok=True)

    write_json(queue_record_path, queue_record)
    if effective_invocation_id and queue_record_path != (reports_dir / "task-queue-execution.json"):
        write_json(reports_dir / "task-queue-execution.json", queue_record)
    return queue_record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--loop", action="store_true", help="Run handoff loop until terminal state or human gate")
    parser.add_argument("--queue", type=Path, default=None, help="Execute pre-authorized task queue")
    parser.add_argument("--invocation-id", type=str, default=None, help="Explicit invocation ID for queue")
    parser.add_argument("--cleanup-workspaces", action="store_true", help="Clean temporary audit workspaces upon completion")
    args = parser.parse_args()
    if args.cleanup_workspaces:
        os.environ["IDEAS_STANDARD_CLEANUP_WORKSPACES"] = "1"
    try:
        if args.queue:
            result = run_task_queue(args.config, args.queue, invocation_id=args.invocation_id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("status") == "COMPLETED" else 1
        elif args.loop:
            result = run_loop(args.config)
        else:
            result = run_once(args.config)
        print(json.dumps({**result, "next_actor": next_actor(result)}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        if not getattr(exc, "_runner_failure_recorded", False):
            try:
                config = load_config(args.config)
                state_path = resolve_path(args.config.resolve(), config["state_path"])
            except Exception:
                config = None
                state_path = None
            try:
                persist_runner_failure(args.config.resolve(), config, state_path, exc)
            except Exception:
                print("RUNNER ERROR: EVIDENCE_WRITE_FAILED", file=sys.stderr)
                return 2
        print(f"RUNNER ERROR: {safe_failure_message(exc)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
