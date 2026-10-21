#!/usr/bin/env python3
"""Regression test for queue skipping, timeout handling, and UTF-8 logs."""

from __future__ import annotations

import importlib.util
import json
import queue
import tempfile
import threading
from pathlib import Path


ROOT = (Path(__file__).resolve().parents[1] / "app")
MODULE_PATH = ROOT / "tools" / "one_click_roast.py"


def load_ui_module():
    spec = importlib.util.spec_from_file_location("one_click_roast_test", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load one_click_roast.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_fake_pipeline(path: Path) -> None:
    path.write_text(
        """import argparse
import json
import os
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("url", nargs="?")
parser.add_argument("--video-file")
parser.add_argument("--job-name", required=True)
parser.add_argument("--work-dir", required=True)
args, _ = parser.parse_known_args()
source = Path(args.video_file or args.url or "unknown").stem
job_dir = Path(args.work_dir) / args.job_name
stages = {
    name: {"status": "PENDING", "history": []}
    for name in ("INPUT", "TIMELINE", "ASR", "TRANSLATION", "FINAL_OUTPUT", "NLE_EXPORT")
}
print(f"日本語ログ: {source}", flush=True)
print(f"中文日志: {source}", flush=True)
if source == "fail":
    stages["TRANSLATION"]["status"] = "FAILED"
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.json").write_text(json.dumps({
        "schema_version": 1,
        "task_id": "fake-fail",
        "display_name": args.job_name,
        "job_dir": str(job_dir),
        "source": {"video_file": args.video_file or ""},
        "status": "FAILED",
        "current_stage": None,
        "owner": None,
        "stages": stages,
        "last_error": {
            "kind": "component_exit",
            "message": "Sakura batch request failed",
            "component": "Sakura",
            "exit_code": 3,
        },
    }, ensure_ascii=False), encoding="utf-8")
    print("translate_zh failed with exit code 3", flush=True)
    raise SystemExit(3)
if source == "hang":
    stages["ASR"]["status"] = "RUNNING"
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.json").write_text(json.dumps({
        "schema_version": 1,
        "task_id": "fake-hang",
        "display_name": args.job_name,
        "job_dir": str(job_dir),
        "source": {"video_file": args.video_file or ""},
        "status": "RUNNING",
        "current_stage": "ASR",
        "owner": {"pid": os.getpid()},
        "stages": stages,
        "last_error": None,
    }, ensure_ascii=False), encoding="utf-8")
    time.sleep(10)
(job_dir / "deliverables").mkdir(parents=True, exist_ok=True)
(job_dir / "deliverables" / "final.ass").write_text("[Events]\\n", encoding="utf-8")
print(f"[done] job_dir={job_dir}", flush=True)
""",
        encoding="utf-8",
    )


class WorkerState:
    def __init__(self) -> None:
        self.messages: queue.Queue[object] = queue.Queue()
        self.stop_requested = threading.Event()
        self.skip_requested = threading.Event()
        self.process = None


def main() -> int:
    module = load_ui_module()
    output_root = Path(tempfile.mkdtemp(prefix="batch_queue_", dir=ROOT / "output"))
    fake_pipeline = output_root / "fake_pipeline.py"
    write_fake_pipeline(fake_pipeline)
    module.PIPELINE = fake_pipeline

    batch = []
    for index, stem in enumerate(("fail", "hang", "success"), start=1):
        batch.append({
            "id": f"item_{index}",
            "source": str(output_root / f"{stem}.mp4"),
            "is_url": False,
        })
    settings = {
        "output_dir": str(output_root / "jobs"),
        "single_job_name": "",
        "copy_media": False,
        "overwrite": False,
        "continue_on_error": True,
        "inactivity_timeout_seconds": 1,
    }
    state = WorkerState()
    module.AutoRoastApp._worker(state, batch, settings)

    messages = []
    while not state.messages.empty():
        messages.append(state.messages.get_nowait())
    kinds = [message[0] for message in messages]
    expected = [
        "item_start", "line", "line", "line", "item_skip",
        "item_start", "line", "line", "item_skip",
        "item_start", "line", "line", "line", "item_done", "batch_done",
    ]
    if kinds != expected:
        raise AssertionError(f"Unexpected queue events: {kinds}")
    skip_statuses = [message[2] for message in messages if message[0] == "item_skip"]
    if skip_statuses != ["失败已跳过", "超时已跳过"]:
        raise AssertionError(f"Unexpected skip statuses: {skip_statuses}")
    skip_details = [message[3] for message in messages if message[0] == "item_skip"]
    if "机器翻译 / Sakura" not in skip_details[0]:
        raise AssertionError(f"Structured stage/component error was not shown: {skip_details[0]}")
    hang_state = json.loads((output_root / "jobs" / "hang" / "task.json").read_text(encoding="utf-8"))
    if hang_state["status"] != "INTERRUPTED" or hang_state["stages"]["ASR"]["status"] != "INTERRUPTED":
        raise AssertionError(f"Timed-out task was not checkpointed as INTERRUPTED: {hang_state}")
    success_log = output_root / "jobs" / "success" / "logs" / "desktop.log"
    log_text = success_log.read_text(encoding="utf-8")
    if "日本語ログ" not in log_text or "中文日志" not in log_text:
        raise AssertionError("UTF-8 log content was not preserved")
    if not (output_root / "jobs" / "success" / "deliverables" / "final.ass").is_file():
        raise AssertionError("The item after skipped failures did not complete")
    print("Batch queue smoke test passed")
    print(f"Evidence: {output_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
