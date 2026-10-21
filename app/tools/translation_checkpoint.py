#!/usr/bin/env python3
"""Translation checkpoint and progress persistence with crash-safe atomic writes."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from task_state import atomic_write_json, now_iso
from voice_common import file_sha256, load_json


def translation_signature(
    source: Path,
    prompt: str,
    glossary: dict[str, object],
    model: object,
    native_settings: dict | None = None,
) -> str:
    import hashlib

    payload = {
        "source_sha256": file_sha256(source),
        "prompt": prompt,
        "glossary": glossary,
        "model": model,
        "native": native_settings or {},
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def load_translation_checkpoint(path: Path, signature: str) -> tuple[dict[int, str], int]:
    checkpoint = load_json(path, {})
    if checkpoint.get("signature") != signature:
        if path.is_file():
            stale = path.with_name(f"{path.stem}.stale_{time.strftime('%Y%m%d_%H%M%S')}{path.suffix}")
            shutil.copy2(path, stale)
        return {}, 0
    translated: dict[int, str] = {}
    for key, value in checkpoint.get("translations", {}).items():
        try:
            item_id = int(key)
        except (TypeError, ValueError):
            continue
        text = str(value).strip()
        if text:
            translated[item_id] = text
    return translated, int(checkpoint.get("attempt", 0))


def save_translation_checkpoint(
    path: Path,
    *,
    signature: str,
    status: str,
    attempt: int,
    total: int,
    translated: dict[int, str],
    error: str | None = None,
    validation: dict | None = None,
) -> None:
    atomic_write_json(path, {
        "schema_version": 1,
        "signature": signature,
        "status": status,
        "attempt": attempt,
        "completed": len(translated),
        "total": total,
        "updated_at": now_iso(),
        "translations": {str(key): translated[key] for key in sorted(translated)},
        "error": error,
        "validation": validation or {},
    })


def save_translation_progress(
    path: Path,
    *,
    completed: int,
    total: int,
    started_at: float,
    status: str,
    baseline_completed: int = 0,
    error: str | None = None,
) -> None:
    elapsed = max(0.001, time.monotonic() - started_at)
    processed_this_attempt = max(0, completed - baseline_completed)
    rate = processed_this_attempt / elapsed if processed_this_attempt else 0.0
    remaining = max(0, total - completed)
    eta_seconds = 0.0 if remaining == 0 else (remaining / rate if rate > 0 else None)
    atomic_write_json(path, {
        "status": status,
        "completed": completed,
        "total": total,
        "progress": round(completed / total, 4) if total else 1.0,
        "elapsed_seconds": round(elapsed, 1),
        "processed_this_attempt": processed_this_attempt,
        "eta_seconds": round(eta_seconds, 1) if eta_seconds is not None else None,
        "updated_at": now_iso(),
        "error": error,
    })
