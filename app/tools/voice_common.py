#!/usr/bin/env python3
"""Shared utilities for VoiceTransl/Whisper/Sakura adapters."""

from __future__ import annotations

import hashlib
import json
import locale
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from subtitle_axis_align import Cue, fmt_srt_time


def configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def hidden_process_kwargs() -> dict:
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startupinfo}


def load_json(path: Path, fallback: dict) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return fallback


def load_pipeline_config(path: Path) -> dict:
    return load_json(path, {})


def decode_console_line(raw_line: bytes) -> str:
    encodings = ["utf-8", locale.getpreferredencoding(False), "cp932", "gb18030"]
    for encoding in dict.fromkeys(encodings):
        try:
            return raw_line.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw_line.decode("utf-8", errors="replace")


def stream_process(command: list[str], cwd: Path, log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    child_env = os.environ.copy()
    child_env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    with log_path.open("w", encoding="utf-8", errors="replace", newline="\n") as log:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=child_env,
            **hidden_process_kwargs(),
        )
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = decode_console_line(raw_line)
            try:
                print(line, end="", flush=True)
            except UnicodeEncodeError:
                encoding = sys.stdout.encoding or "utf-8"
                safe_line = line.encode(encoding, errors="replace").decode(encoding)
                print(safe_line, end="", flush=True)
            log.write(line)
        return process.wait()


def failed_candidate_path(output: Path, label: str) -> Path:
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    return output.parent / f"{label}_failed_candidate_{timestamp}{output.suffix}"


def preserve_failed_candidate(candidate: Path, output: Path, label: str) -> Path | None:
    if not candidate.is_file():
        return None
    evidence = failed_candidate_path(output, label)
    shutil.move(str(candidate), evidence)
    return evidence


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def component_log_dir(output: Path) -> Path:
    """Keep pipeline component logs together while preserving standalone behavior."""
    output = output.resolve()
    if output.parent.name.casefold() == "subs":
        return output.parent.parent / "logs" / "voicetransl"
    return output.parent


def render_srt(cues: list[Cue], translations: list[str]) -> str:
    blocks = []
    for index, (cue, text) in enumerate(zip(cues, translations), start=1):
        blocks.append(f"{index}\n{fmt_srt_time(cue.start)} --> {fmt_srt_time(cue.end)}\n{text.strip()}")
    return "\n\n".join(blocks) + "\n"
