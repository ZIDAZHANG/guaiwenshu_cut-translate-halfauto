#!/usr/bin/env python3
"""Watch a folder for video links and submit them to the roasting pipeline."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "tools" / "roast_pipeline.py"
DEFAULT_CONFIG = ROOT / "config" / "roast_pipeline.local.json"
DEFAULT_INBOX = ROOT / "inbox"
SUPPORTED_SUFFIXES = {".txt", ".url", ".json"}


def read_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def clean_name(value: str) -> str:
    value = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", value, flags=re.UNICODE)
    return value.strip("._") or "roast_job"


def parse_request(path: Path) -> dict:
    if path.suffix.lower() == ".json":
        data = json.loads(read_text(path))
        if not isinstance(data, dict):
            raise ValueError("JSON request must be an object")
        return data

    text = read_text(path)
    values: dict[str, object] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        if line.upper().startswith("URL="):
            values["url"] = line.split("=", 1)[1].strip()
        elif re.match(r"https?://", line, flags=re.IGNORECASE):
            values.setdefault("url", line)
        elif "=" in line:
            key, value = line.split("=", 1)
            values[key.strip().lower()] = value.strip()
    return values


def bool_value(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "是"}


def build_job_name(request: dict, request_id: str) -> str:
    if request.get("job_name"):
        return clean_name(str(request["job_name"]))
    url = str(request.get("url", ""))
    host = urlparse(url).netloc.replace("www.", "") or "video"
    return clean_name(f"{datetime.now():%Y%m%d_%H%M%S}_{host}_{request_id[:8]}")


def build_command(request: dict, config: Path, request_id: str, dry_run: bool) -> tuple[list[str], str]:
    url = str(request.get("url", "")).strip()
    video_file = str(request.get("video_file", request.get("video", ""))).strip()
    if not url and not video_file:
        raise ValueError("request does not contain a URL or video_file")

    job_name = build_job_name(request, request_id)
    command = [sys.executable, str(PIPELINE), url or "local-file", "--config", str(config), "--job-name", job_name]
    option_map = {
        "video_file": "--video-file",
        "audio_file": "--audio-file",
        "timeline_file": "--timeline-file",
        "jp_file": "--jp-file",
        "zh_file": "--zh-file",
        "proxy": "--proxy",
        "jianying_draft": "--jianying-draft",
    }
    normalized = dict(request)
    if video_file:
        normalized["video_file"] = video_file
    for key, flag in option_map.items():
        value = str(normalized.get(key, "")).strip()
        if value:
            command.extend([flag, value])

    boolean_flags = {
        "copy_media": ("--copy-media", bool(video_file)),
        "skip_download": ("--skip-download", False),
        "skip_audio": ("--skip-audio", False),
        "skip_external": ("--skip-external", False),
        "wait_missing": ("--wait-missing", True),
        "skip_jianying_package": ("--skip-jianying-package", False),
    }
    for key, (flag, default) in boolean_flags.items():
        if bool_value(normalized.get(key), default):
            command.append(flag)

    wait_timeout = str(normalized.get("wait_timeout", "")).strip()
    if wait_timeout:
        command.extend(["--wait-timeout", wait_timeout])
    if dry_run or bool_value(normalized.get("dry_run"), False):
        command.append("--dry-run")
    return command, job_name


def write_status(inbox: Path, request_id: str, payload: dict) -> Path:
    status_dir = inbox / "status"
    status_dir.mkdir(parents=True, exist_ok=True)
    path = status_dir / f"{request_id}.json"
    data = {"request_id": request_id, "updated_at": datetime.now().isoformat(timespec="seconds"), **payload}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def run_request(request: dict, request_id: str, inbox: Path, config: Path, dry_run: bool) -> int:
    command, job_name = build_command(request, config, request_id, dry_run)
    write_status(inbox, request_id, {"state": "running", "job_name": job_name, "url": request.get("url", "")})
    print(f"[inbox] 开始任务：{job_name}", flush=True)
    completed = subprocess.run(command, cwd=ROOT)
    state = "completed" if completed.returncode == 0 else "failed"
    write_status(
        inbox,
        request_id,
        {
            "state": state,
            "job_name": job_name,
            "url": request.get("url", ""),
            "exit_code": completed.returncode,
            "job_dir": str(ROOT / "work" / job_name),
        },
    )
    print(f"[inbox] 任务{('完成' if completed.returncode == 0 else '失败')}：{job_name}", flush=True)
    return completed.returncode


def process_file(path: Path, inbox: Path, config: Path, dry_run: bool) -> int:
    request_id = clean_name(path.stem)
    working_dir = inbox / "working"
    working_dir.mkdir(parents=True, exist_ok=True)
    working = working_dir / path.name
    path.replace(working)
    try:
        request = parse_request(working)
        code = run_request(request, request_id, inbox, config, dry_run)
    except Exception as exc:
        code = 1
        write_status(inbox, request_id, {"state": "failed", "error": str(exc)})
        print(f"[inbox] 无法处理 {working.name}：{exc}", file=sys.stderr, flush=True)

    destination_dir = inbox / ("done" if code == 0 else "failed")
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / working.name
    if destination.exists():
        destination = destination_dir / f"{working.stem}_{datetime.now():%Y%m%d_%H%M%S}{working.suffix}"
    working.replace(destination)
    return code


def pending_files(inbox: Path) -> list[Path]:
    return sorted(
        (path for path in inbox.iterdir() if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES),
        key=lambda path: path.stat().st_mtime,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Watch a folder and roast submitted video links.")
    parser.add_argument("--inbox", type=Path, default=DEFAULT_INBOX)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--watch", action="store_true", help="Keep watching for new requests")
    parser.add_argument("--once", action="store_true", help="Process current requests once")
    parser.add_argument("--url", help="Submit one URL immediately")
    parser.add_argument("--poll", type=float, default=3.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    args.inbox.mkdir(parents=True, exist_ok=True)
    if args.url:
        request_id = f"direct_{datetime.now():%Y%m%d_%H%M%S}"
        return run_request({"url": args.url}, request_id, args.inbox, args.config, args.dry_run)

    print(f"[inbox] 正在监听：{args.inbox}", flush=True)
    exit_code = 0
    while True:
        for path in pending_files(args.inbox):
            exit_code = max(exit_code, process_file(path, args.inbox, args.config, args.dry_run))
        if not args.watch:
            return exit_code
        time.sleep(max(args.poll, 0.5))


if __name__ == "__main__":
    raise SystemExit(main())
