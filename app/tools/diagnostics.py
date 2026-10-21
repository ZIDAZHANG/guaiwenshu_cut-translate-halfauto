#!/usr/bin/env python3
"""Create a redacted, content-minimized diagnostic bundle for one task."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

from task_state import atomic_write_json, redact


SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|passwd|authorization|cookie)(\s*[:=]\s*)([^\s,;\"']+)"
)
URL_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:api[_-]?key|token|secret|password|authorization|cookie)=)[^&#\s]+"
)
TIMED_SUBTITLE_RE = re.compile(
    r"^(?P<prefix>\s*\[?\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?\s*(?:-->|\])(?:\s*\[?\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?\]?)?)[ \t]+.+$"
)
SRT_TEXT_AFTER_TIME_RE = re.compile(r"^\s*\d{1,2}:\d{2}:\d{2}[,.]\d+\s*-->.*$")


def sanitize_text(text: str, job_dir: Path | None = None) -> str:
    text = str(redact(text))
    text = SENSITIVE_ASSIGNMENT_RE.sub(r"\1\2<redacted>", text)
    text = URL_QUERY_SECRET_RE.sub(r"\1<redacted>", text)
    home = str(Path.home())
    if home:
        text = text.replace(home, r"C:\Users\<user>")
        text = text.replace(home.replace("\\", "/"), "C:/Users/<user>")
    if job_dir:
        text = text.replace(str(job_dir), "<JOB_DIR>")
        text = text.replace(str(job_dir).replace("\\", "/"), "<JOB_DIR>")
    return text


def sanitize_log(text: str, job_dir: Path) -> str:
    output: list[str] = []
    omit_next_text = False
    for raw_line in text.splitlines():
        line = sanitize_text(raw_line, job_dir)
        timed = TIMED_SUBTITLE_RE.match(line)
        if timed:
            output.append(timed.group("prefix") + " <subtitle text omitted>")
            omit_next_text = False
            continue
        if SRT_TEXT_AFTER_TIME_RE.match(line):
            output.append(line)
            omit_next_text = True
            continue
        if omit_next_text and line.strip() and not line.strip().isdigit():
            output.append("<subtitle text omitted>")
            omit_next_text = False
            continue
        if line.strip():
            omit_next_text = False
        output.append(line)
    return "\n".join(output) + "\n"


def sanitize_value(value: Any, job_dir: Path) -> Any:
    value = redact(value)
    if isinstance(value, dict):
        return {str(key): sanitize_value(item, job_dir) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_value(item, job_dir) for item in value]
    if isinstance(value, str):
        return sanitize_text(value, job_dir)
    return value


def read_text_tail(path: Path, limit: int | None = None) -> str:
    data = path.read_bytes()
    if limit is not None and len(data) > limit:
        data = data[-limit:]
        prefix = b"<earlier log content omitted>\n"
    else:
        prefix = b""
    return (prefix + data).decode("utf-8", errors="replace")


def relative_file_manifest(job_dir: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for path in job_dir.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(job_dir)
        files.append({
            "path": relative.as_posix(),
            "size_bytes": path.stat().st_size,
            "modified_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(path.stat().st_mtime)),
            "content_included": False,
        })
    return files


def diagnostic_log_paths(job_dir: Path) -> list[Path]:
    """Return current component logs plus legacy VoiceTransl logs from subs/."""
    job_dir = job_dir.resolve()
    candidates: set[Path] = set()
    logs_dir = job_dir / "logs"
    if logs_dir.is_dir():
        candidates.update(path for path in logs_dir.rglob("*") if path.is_file())
    for legacy_name in ("whisper.log", "sakura_server.log"):
        candidates.update(path for path in job_dir.rglob(legacy_name) if path.is_file())
    result: list[Path] = []
    for path in sorted(candidates):
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved.is_relative_to(job_dir):
            result.append(resolved)
    return result


def create_diagnostic_bundle(job_dir: Path, destination: Path | None = None) -> Path:
    job_dir = job_dir.resolve()
    if not job_dir.is_dir():
        raise FileNotFoundError(f"Task directory does not exist: {job_dir}")
    destination = (destination or job_dir / "deliverables" / f"diagnostics_{time.strftime('%Y%m%d_%H%M%S')}.zip").resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)

    task_path = job_dir / "task.json"
    task = {}
    if task_path.is_file():
        try:
            task = json.loads(task_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            task = {"read_error": str(exc)}
    summary = {
        "schema_version": 1,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "task_status": task.get("status", "UNKNOWN"),
        "current_stage": task.get("current_stage"),
        "last_error": task.get("last_error"),
        "environment": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "frozen": bool(getattr(sys, "frozen", False)),
        },
        "privacy": {
            "media_included": False,
            "subtitle_files_included": False,
            "subtitle_text_removed_from_logs": True,
            "environment_variables_included": False,
        },
        "files": relative_file_manifest(job_dir),
    }
    summary = sanitize_value(summary, job_dir)
    task = sanitize_value(task, job_dir)

    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    included: list[str] = []
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("diagnostics/summary.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
            included.append("diagnostics/summary.json")
            archive.writestr("diagnostics/task.json", json.dumps(task, ensure_ascii=False, indent=2) + "\n")
            included.append("diagnostics/task.json")

            for path in diagnostic_log_paths(job_dir):
                if not path.is_file() or path.suffix.lower() not in {".log", ".txt", ".jsonl", ".json"}:
                    continue
                relative = path.relative_to(job_dir).as_posix()
                archive.writestr(f"diagnostics/{relative}", sanitize_log(read_text_tail(path), job_dir))
                included.append(f"diagnostics/{relative}")

            metadata_names = {
                "job_manifest.json",
                "jianying_status.json",
                "jianying_ui_status.json",
                "final_validation.json",
                "jp_validation.json",
                "zh_validation.json",
                "timeline_validation.json",
                "translation_progress.json",
            }
            for path in job_dir.rglob("*.json"):
                if path.name not in metadata_names or path == task_path:
                    continue
                relative = path.relative_to(job_dir).as_posix()
                archive_name = f"diagnostics/{relative}"
                if archive_name in included:
                    continue
                try:
                    value = json.loads(path.read_text(encoding="utf-8-sig"))
                    payload = json.dumps(sanitize_value(value, job_dir), ensure_ascii=False, indent=2) + "\n"
                except Exception as exc:
                    payload = json.dumps({"read_error": sanitize_text(str(exc), job_dir)}, ensure_ascii=False) + "\n"
                archive.writestr(archive_name, payload)
                included.append(archive_name)

            archive.writestr(
                "diagnostics/bundle_manifest.json",
                json.dumps({
                    "included": included,
                    "excluded_content": ["video", "audio", "subtitle files", "review CSV", "model files", "environment variables"],
                }, ensure_ascii=False, indent=2) + "\n",
            )
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a redacted task diagnostic ZIP.")
    parser.add_argument("job_dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(create_diagnostic_bundle(args.job_dir, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
