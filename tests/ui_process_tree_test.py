#!/usr/bin/env python3
"""Verify manual skip/stop terminate child processes and checkpoint the task."""

from __future__ import annotations

import importlib.util
import json
import queue
import tempfile
import threading
import time
from pathlib import Path


ROOT = (Path(__file__).resolve().parents[1] / "app")
MODULE_PATH = ROOT / "tools" / "one_click_roast.py"


def load_ui_module():
    spec = importlib.util.spec_from_file_location("one_click_roast_process_test", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load one_click_roast.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_fake_pipeline(path: Path) -> None:
    path.write_text(
        r'''import argparse
import json
import subprocess
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
job_dir.mkdir(parents=True, exist_ok=True)

if source.endswith("hang"):
    stages = {
        name: {
            "status": "RUNNING" if name == "ASR" else "PENDING",
            "attempt": 1 if name == "ASR" else 0,
            "history": [],
        }
        for name in ("INPUT", "TIMELINE", "ASR", "TRANSLATION", "FINAL_OUTPUT", "NLE_EXPORT")
    }
    (job_dir / "task.json").write_text(json.dumps({
        "schema_version": 1,
        "task_id": source,
        "display_name": args.job_name,
        "job_dir": str(job_dir),
        "source": {"video_file": args.video_file or ""},
        "status": "RUNNING",
        "current_stage": "ASR",
        "owner": {"pid": 1},
        "stages": stages,
        "last_error": None,
    }, ensure_ascii=False), encoding="utf-8")
    marker = job_dir / "child_survived.txt"
    child_code = (
        "import sys,time; from pathlib import Path; "
        "time.sleep(4); Path(sys.argv[1]).write_text('survived', encoding='utf-8')"
    )
    subprocess.Popen(
        [sys.executable, "-c", child_code, str(marker)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print("child process started", flush=True)
    time.sleep(30)
else:
    (job_dir / "deliverables").mkdir(parents=True, exist_ok=True)
    (job_dir / "deliverables" / "final.ass").write_text("[Events]\n", encoding="utf-8")
    print("success after skip", flush=True)
''',
        encoding="utf-8",
    )


class WorkerState:
    def __init__(self) -> None:
        self.messages: queue.Queue[object] = queue.Queue()
        self.stop_requested = threading.Event()
        self.skip_requested = threading.Event()
        self.process = None


def wait_for(path: Path, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {path}")


def drain(state: WorkerState) -> list[tuple]:
    messages = []
    while not state.messages.empty():
        messages.append(state.messages.get_nowait())
    return messages


def run_worker(module, state: WorkerState, batch: list[dict], output_dir: Path) -> threading.Thread:
    settings = {
        "output_dir": str(output_dir),
        "single_job_name": "",
        "copy_media": False,
        "overwrite": False,
        "continue_on_error": True,
        "inactivity_timeout_seconds": 60,
    }
    worker = threading.Thread(target=module.AutoRoastApp._worker, args=(state, batch, settings), daemon=True)
    worker.start()
    return worker


def main() -> int:
    module = load_ui_module()
    output_root = Path(tempfile.mkdtemp(prefix="ui_process_tree_", dir=ROOT / "output"))
    fake_pipeline = output_root / "fake_pipeline.py"
    write_fake_pipeline(fake_pipeline)
    module.PIPELINE = fake_pipeline

    skip_jobs = output_root / "skip_jobs"
    skip_state = WorkerState()
    skip_batch = [
        {"id": "skip", "source": str(output_root / "manualhang.mp4"), "is_url": False},
        {"id": "after", "source": str(output_root / "after_skip.mp4"), "is_url": False},
    ]
    skip_worker = run_worker(module, skip_state, skip_batch, skip_jobs)
    skip_task = skip_jobs / "manualhang" / "task.json"
    wait_for(skip_task)
    skip_state.skip_requested.set()
    skip_worker.join(timeout=15)
    if skip_worker.is_alive():
        raise AssertionError("Manual skip did not terminate the worker process tree")
    skip_messages = drain(skip_state)
    skip_kinds = [message[0] for message in skip_messages]
    if "item_skip" not in skip_kinds or "item_done" not in skip_kinds or skip_kinds[-1] != "batch_done":
        raise AssertionError(f"Manual skip did not continue to the next item: {skip_kinds}")
    skip_event = next(message for message in skip_messages if message[0] == "item_skip")
    if skip_event[2] != "手动跳过":
        raise AssertionError(f"Unexpected manual skip event: {skip_event}")
    skip_data = json.loads(skip_task.read_text(encoding="utf-8"))
    if skip_data["status"] != "CANCELLED" or skip_data["stages"]["ASR"]["status"] != "CANCELLED":
        raise AssertionError(f"Manual skip was not checkpointed as CANCELLED: {skip_data}")

    stop_jobs = output_root / "stop_jobs"
    stop_state = WorkerState()
    stop_batch = [{"id": "stop", "source": str(output_root / "stophang.mp4"), "is_url": False}]
    stop_worker = run_worker(module, stop_state, stop_batch, stop_jobs)
    stop_task = stop_jobs / "stophang" / "task.json"
    wait_for(stop_task)
    stop_state.stop_requested.set()
    stop_worker.join(timeout=15)
    if stop_worker.is_alive():
        raise AssertionError("Stop did not terminate the worker process tree")
    stop_messages = drain(stop_state)
    if not stop_messages or stop_messages[-1][0] != "batch_stopped":
        raise AssertionError(f"Stop did not end the batch: {stop_messages}")
    stop_data = json.loads(stop_task.read_text(encoding="utf-8"))
    if stop_data["status"] != "CANCELLED" or stop_data["stages"]["ASR"]["status"] != "CANCELLED":
        raise AssertionError(f"Stop was not checkpointed as CANCELLED: {stop_data}")

    time.sleep(4.5)
    survivor_markers = list(output_root.rglob("child_survived.txt"))
    if survivor_markers:
        raise AssertionError(f"Child processes survived skip/stop: {survivor_markers}")

    print("UI process tree tests passed")
    print(f"Evidence: {output_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
