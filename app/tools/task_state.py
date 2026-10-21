#!/usr/bin/env python3
"""Atomic task checkpoints and structured logging for the roast pipeline."""

from __future__ import annotations

import json
import os
import re
import socket
import tempfile
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
TASK_STATUSES = {"PENDING", "RUNNING", "DONE", "FAILED", "INTERRUPTED", "CANCELLED"}
STAGE_STATUSES = TASK_STATUSES | {"SKIPPED"}
DEFAULT_STAGES = (
    "INPUT",
    "TIMELINE",
    "ASR",
    "SEGMENTATION",
    "TRANSLATION",
    "FINAL_OUTPUT",
    "NLE_EXPORT",
)
SENSITIVE_KEY_RE = re.compile(
    r"(?:api[_-]?key|token|secret|password|passwd|authorization|cookie|credential)",
    re.IGNORECASE,
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
URL_CREDENTIAL_RE = re.compile(r"(?P<scheme>https?://)[^/@\s:]+:[^/@\s]+@", re.IGNORECASE)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def atomic_write_text(path: Path, text: str) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def redact(value: Any, key: str = "") -> Any:
    """Return a JSON-safe copy with common credentials removed."""
    if key and SENSITIVE_KEY_RE.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(item_key): redact(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        value = BEARER_RE.sub("Bearer <redacted>", value)
        return URL_CREDENTIAL_RE.sub(r"\g<scheme><redacted>@", value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def append_jsonl(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(redact(event), ensure_ascii=False, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def process_is_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


class TaskAlreadyRunningError(RuntimeError):
    pass


class TaskState:
    def __init__(self, path: Path, data: dict[str, Any]):
        self.path = path.resolve()
        self.data = data
        self.event_log = self.path.parent / "logs" / "task_events.jsonl"
        self._validate_shape()

    @classmethod
    def open_or_create(
        cls,
        path: Path,
        *,
        source: dict[str, Any],
        job_dir: Path,
        display_name: str,
        stages: Iterable[str] = DEFAULT_STAGES,
    ) -> "TaskState":
        path = path.resolve()
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            state = cls(path, data)
            state._recover_if_needed()
            return state

        timestamp = now_iso()
        stage_map = {
            stage: {
                "status": "PENDING",
                "attempt": 0,
                "started_at": None,
                "finished_at": None,
                "heartbeat_at": None,
                "inputs": {},
                "outputs": {},
                "validation": {},
                "error": None,
                "history": [],
            }
            for stage in stages
        }
        data = {
            "schema_version": SCHEMA_VERSION,
            "task_id": str(uuid.uuid4()),
            "display_name": display_name,
            "job_dir": str(job_dir.resolve()),
            "source": redact(source),
            "status": "PENDING",
            "current_stage": None,
            "created_at": timestamp,
            "updated_at": timestamp,
            "owner": None,
            "stages": stage_map,
            "last_error": None,
        }
        state = cls(path, data)
        state.save()
        state.log("task_created", message="Task checkpoint created")
        return state

    def _validate_shape(self) -> None:
        if self.data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"Unsupported task schema: {self.data.get('schema_version')!r}")
        if self.data.get("status") not in TASK_STATUSES:
            raise ValueError(f"Invalid task status: {self.data.get('status')!r}")
        if not isinstance(self.data.get("stages"), dict):
            raise ValueError("Task state has no stage map")
        for name, stage in self.data["stages"].items():
            if not isinstance(stage, dict) or stage.get("status") not in STAGE_STATUSES:
                raise ValueError(f"Invalid stage state: {name}")

    def _recover_if_needed(self) -> None:
        if self.data.get("status") != "RUNNING":
            return
        owner = self.data.get("owner") or {}
        owner_pid = owner.get("pid")
        if owner_pid != os.getpid() and process_is_alive(owner_pid):
            raise TaskAlreadyRunningError(f"Task is already running in process {owner_pid}")
        timestamp = now_iso()
        for name, stage in self.data["stages"].items():
            if stage.get("status") == "RUNNING":
                stage["status"] = "INTERRUPTED"
                stage["finished_at"] = timestamp
                stage["error"] = {
                    "kind": "unclean_shutdown",
                    "message": "Previous process ended before the stage checkpoint was completed",
                    "recoverable": True,
                }
                stage.setdefault("history", []).append({
                    "status": "INTERRUPTED",
                    "at": timestamp,
                    "reason": "unclean_shutdown",
                })
        self.data["status"] = "INTERRUPTED"
        self.data["current_stage"] = None
        self.data["owner"] = None
        self.data["last_error"] = {
            "kind": "unclean_shutdown",
            "message": "Recovered an unfinished task after its process stopped",
            "recoverable": True,
        }
        self.save()
        self.log("task_recovered", level="warning", message="Stale RUNNING state changed to INTERRUPTED")

    @property
    def status(self) -> str:
        return str(self.data["status"])

    def stage_status(self, stage: str) -> str:
        return str(self._stage(stage)["status"])

    def _stage(self, name: str) -> dict[str, Any]:
        try:
            return self.data["stages"][name]
        except KeyError as exc:
            raise KeyError(f"Unknown task stage: {name}") from exc

    def save(self) -> None:
        self.data["updated_at"] = now_iso()
        atomic_write_json(self.path, redact(self.data))

    def log(
        self,
        event_type: str,
        *,
        stage: str | None = None,
        component: str | None = None,
        level: str = "info",
        message: str = "",
        data: dict[str, Any] | None = None,
    ) -> None:
        append_jsonl(self.event_log, {
            "timestamp": now_iso(),
            "task_id": self.data["task_id"],
            "event": event_type,
            "level": level,
            "stage": stage,
            "component": component,
            "message": message,
            "data": data or {},
        })

    def start_stage(
        self,
        stage_name: str,
        *,
        inputs: dict[str, Any] | None = None,
        outputs: dict[str, Any] | None = None,
        component: str | None = None,
    ) -> None:
        stage = self._stage(stage_name)
        timestamp = now_iso()
        stage["status"] = "RUNNING"
        stage["attempt"] = int(stage.get("attempt", 0)) + 1
        stage["started_at"] = timestamp
        stage["finished_at"] = None
        stage["heartbeat_at"] = timestamp
        stage["inputs"] = redact(inputs or {})
        stage["outputs"] = redact(outputs or {})
        stage["validation"] = {}
        stage["error"] = None
        stage.setdefault("history", []).append({"status": "RUNNING", "at": timestamp})
        self.data["status"] = "RUNNING"
        self.data["current_stage"] = stage_name
        self.data["owner"] = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "started_at": timestamp,
        }
        self.data["last_error"] = None
        self.save()
        self.log("stage_started", stage=stage_name, component=component, data={"attempt": stage["attempt"]})

    def heartbeat(self, stage_name: str, *, data: dict[str, Any] | None = None) -> None:
        stage = self._stage(stage_name)
        if stage.get("status") != "RUNNING":
            return
        stage["heartbeat_at"] = now_iso()
        self.save()
        if data:
            self.log("stage_heartbeat", stage=stage_name, data=data)

    def complete_stage(
        self,
        stage_name: str,
        *,
        validation: dict[str, Any] | None = None,
        outputs: dict[str, Any] | None = None,
        component: str | None = None,
    ) -> None:
        stage = self._stage(stage_name)
        timestamp = now_iso()
        stage["status"] = "DONE"
        stage["finished_at"] = timestamp
        stage["heartbeat_at"] = timestamp
        if outputs is not None:
            stage["outputs"] = redact(outputs)
        stage["validation"] = redact(validation or {})
        stage["error"] = None
        stage.setdefault("history", []).append({"status": "DONE", "at": timestamp})
        self.data["current_stage"] = None
        if all(item.get("status") in {"DONE", "SKIPPED"} for item in self.data["stages"].values()):
            self.data["status"] = "DONE"
            self.data["owner"] = None
        else:
            self.data["status"] = "RUNNING"
        self.save()
        self.log("stage_completed", stage=stage_name, component=component, data={"validation": validation or {}})

    def skip_stage(
        self,
        stage_name: str,
        *,
        reason: str,
        component: str | None = None,
        outputs: dict[str, Any] | None = None,
    ) -> None:
        stage = self._stage(stage_name)
        timestamp = now_iso()
        stage["status"] = "SKIPPED"
        stage["finished_at"] = timestamp
        stage["heartbeat_at"] = timestamp
        stage["outputs"] = redact(outputs or {})
        stage["validation"] = {"skipped": True, "reason": reason}
        stage["error"] = None
        stage.setdefault("history", []).append({"status": "SKIPPED", "at": timestamp, "reason": reason})
        self.data["current_stage"] = None
        self.data["last_error"] = None
        if all(item.get("status") in {"DONE", "SKIPPED"} for item in self.data["stages"].values()):
            self.data["status"] = "DONE"
            self.data["owner"] = None
        else:
            self.data["status"] = "RUNNING"
        self.save()
        self.log(
            "stage_skipped",
            stage=stage_name,
            component=component,
            message=reason,
            data={"outputs": outputs or {}},
        )

    def fail_stage(
        self,
        stage_name: str,
        *,
        kind: str,
        message: str,
        component: str | None = None,
        exit_code: int | None = None,
        recoverable: bool = True,
        details: dict[str, Any] | None = None,
    ) -> None:
        stage = self._stage(stage_name)
        timestamp = now_iso()
        error = {
            "kind": kind,
            "message": message,
            "component": component,
            "exit_code": exit_code,
            "recoverable": recoverable,
            "details": redact(details or {}),
        }
        stage["status"] = "FAILED"
        stage["finished_at"] = timestamp
        stage["heartbeat_at"] = timestamp
        stage["error"] = error
        stage.setdefault("history", []).append({"status": "FAILED", "at": timestamp, "kind": kind})
        self.data["status"] = "FAILED"
        self.data["current_stage"] = None
        self.data["owner"] = None
        self.data["last_error"] = error
        self.save()
        self.log(
            "stage_failed",
            stage=stage_name,
            component=component,
            level="error",
            message=message,
            data=error,
        )

    def interrupt(self, *, message: str = "Task interrupted", cancelled: bool = False) -> None:
        task_status = "CANCELLED" if cancelled else "INTERRUPTED"
        timestamp = now_iso()
        current = self.data.get("current_stage")
        if current:
            stage = self._stage(current)
            if stage.get("status") == "RUNNING":
                stage["status"] = task_status
                stage["finished_at"] = timestamp
                stage["error"] = {
                    "kind": task_status.lower(),
                    "message": message,
                    "recoverable": not cancelled,
                }
                stage.setdefault("history", []).append({"status": task_status, "at": timestamp})
        self.data["status"] = task_status
        self.data["current_stage"] = None
        self.data["owner"] = None
        self.data["last_error"] = {
            "kind": task_status.lower(),
            "message": message,
            "recoverable": not cancelled,
        }
        self.save()
        self.log("task_interrupted", level="warning", message=message, data={"status": task_status})

    def ensure_stages(self, names: Iterable[str]) -> None:
        """Insert new pipeline stages before their downstream cached work."""
        names = list(names)
        old = self.data["stages"]
        missing = [name for name in names if name not in old]
        ordered = {}
        for name in names:
            ordered[name] = old.get(name, {
                "status": "PENDING", "attempt": 0, "started_at": None, "finished_at": None,
                "heartbeat_at": None, "inputs": {}, "outputs": {}, "validation": {},
                "error": None, "history": [],
            })
        ordered.update({name: stage for name, stage in old.items() if name not in ordered})
        if list(ordered) == list(old):
            return
        self.data["stages"] = ordered
        if missing:
            self.reset_from(missing[0], reason="Pipeline now includes a source segmentation stage")
        else:
            self.save()

    def reset_from(self, stage_name: str, *, reason: str, preserve: Iterable[str] = ()) -> None:
        names = list(self.data["stages"])
        try:
            start = names.index(stage_name)
        except ValueError as exc:
            raise KeyError(f"Unknown task stage: {stage_name}") from exc
        timestamp = now_iso()
        preserved = set(preserve)
        for name in names[start:]:
            stage = self._stage(name)
            old_status = stage.get("status")
            if name in preserved and old_status in {"DONE", "SKIPPED"}:
                continue
            if old_status != "PENDING":
                stage.setdefault("history", []).append({
                    "status": "PENDING",
                    "at": timestamp,
                    "reason": reason,
                    "previous_status": old_status,
                })
            stage["status"] = "PENDING"
            stage["started_at"] = None
            stage["finished_at"] = None
            stage["heartbeat_at"] = None
            stage["validation"] = {}
            stage["error"] = None
        self.data["status"] = "PENDING"
        self.data["current_stage"] = None
        self.data["owner"] = None
        self.data["last_error"] = None
        self.save()
        self.log("stages_reset", level="warning", stage=stage_name, message=reason)

    def snapshot(self) -> dict[str, Any]:
        return deepcopy(self.data)


def scan_unfinished(work_dir: Path) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    if not work_dir.is_dir():
        return tasks
    for path in work_dir.glob("*/task.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("status") != "DONE":
            tasks.append({
                "task_path": str(path.resolve()),
                "task_id": data.get("task_id"),
                "display_name": data.get("display_name", path.parent.name),
                "status": data.get("status", "UNKNOWN"),
                "current_stage": data.get("current_stage"),
                "updated_at": data.get("updated_at"),
                "last_error": data.get("last_error"),
            })
    tasks.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return tasks
