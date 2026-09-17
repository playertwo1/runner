"""QueueService and QueueRepository for Ideias Standard M4 Queue Management."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.orchestrate_handoffs import HandoffError


class QueueRepository:
    def __init__(self, root_dir: Path | str | None = None) -> None:
        self.root_dir = Path(root_dir) if root_dir else None
        self._states: dict[str, dict[str, Any]] = {}

    def get_state(self, invocation_id: str) -> dict[str, Any] | None:
        if self.root_dir:
            file_path = self.root_dir / f"task-queue-execution-{invocation_id}.json"
            if file_path.is_file():
                return json.loads(file_path.read_text(encoding="utf-8"))
        state = self._states.get(invocation_id)
        return json.loads(json.dumps(state)) if state is not None else None

    def getState(self, invocation_id: str) -> dict[str, Any] | None:
        return self.get_state(invocation_id)

    def create_state(self, invocation_id: str, state: dict[str, Any]) -> None:
        copy_state = json.loads(json.dumps(state))
        self._states[invocation_id] = copy_state
        if self.root_dir:
            self.root_dir.mkdir(parents=True, exist_ok=True)
            file_path = self.root_dir / f"task-queue-execution-{invocation_id}.json"
            file_path.write_text(json.dumps(copy_state, ensure_ascii=False, indent=2), encoding="utf-8")

    def createState(self, invocation_id: str, state: dict[str, Any]) -> None:
        self.create_state(invocation_id, state)

    def update_state(self, invocation_id: str, updates: dict[str, Any]) -> None:
        existing = self.get_state(invocation_id) or {"status": "PENDING", "results": []}
        existing.update(json.loads(json.dumps(updates)))
        self._states[invocation_id] = existing
        if self.root_dir:
            self.root_dir.mkdir(parents=True, exist_ok=True)
            file_path = self.root_dir / f"task-queue-execution-{invocation_id}.json"
            file_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")

    def updateState(self, invocation_id: str, updates: dict[str, Any]) -> None:
        self.update_state(invocation_id, updates)


class QueueService:
    def __init__(self, queue_repo: QueueRepository) -> None:
        self.queue_repo = queue_repo

    def invoke(self, invocation_id: str, payload: Any = None, on_conflict: str = "raise") -> None:
        # CORREÇÃO: Bloqueio de mutação antes de iniciar processamento
        existing_state = self.queue_repo.get_state(invocation_id)
        if existing_state:
            if existing_state.get("status") in ("ACCEPTED", "IN_PROGRESS", "COMPLETED"):
                if on_conflict == "raise":
                    raise HandoffError(
                        f"INVOCATION_ALREADY_EXISTS: Invocation '{invocation_id}' already exists with status '{existing_state.get('status')}'."
                    )
                return

        self.queue_repo.create_state(invocation_id, {
            "status": "PENDING",
            "payload": payload,
            "results": [],
        })
