#!/usr/bin/env python3
"""Regression tests for task checkpoints, recovery, and redaction."""

from __future__ import annotations

import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

from roast_pipeline import exit_code_from_error, run_command
from task_state import TaskState, atomic_write_json, redact, scan_unfinished


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="task_state_") as temp_dir:
        root = Path(temp_dir)
        job = root / "job"
        task_path = job / "task.json"
        state = TaskState.open_or_create(
            task_path,
            source={"video": "D:/media/测试.mp4", "api_key": "must-not-leak"},
            job_dir=job,
            display_name="测试任务",
        )
        if state.status != "PENDING":
            raise AssertionError(state.status)
        saved = json.loads(task_path.read_text(encoding="utf-8"))
        if saved["source"]["api_key"] != "<redacted>":
            raise AssertionError("Sensitive task input was not redacted")

        state.start_stage("INPUT", inputs={"video": "x.mp4"}, outputs={"audio": "audio.wav"})
        state.complete_stage("INPUT", validation={"video": {"valid": True}})
        state.start_stage("TIMELINE", component="MagiaTimeline")
        state.fail_stage(
            "TIMELINE",
            kind="artifact_validation",
            message="timeline is empty",
            component="MagiaTimeline",
            exit_code=0,
        )
        if state.status != "FAILED" or state.stage_status("INPUT") != "DONE":
            raise AssertionError("Stage failure damaged upstream state")
        if state.data["last_error"]["exit_code"] != 0:
            raise AssertionError("Fake-success exit code was not recorded")

        state.reset_from("TIMELINE", reason="retry after validation failure")
        if state.stage_status("INPUT") != "DONE" or state.stage_status("TIMELINE") != "PENDING":
            raise AssertionError("Stage reset did not preserve completed upstream work")

        state.start_stage("TIMELINE")
        stale = state.snapshot()
        stale["owner"]["pid"] = 2_147_483_000
        atomic_write_json(task_path, stale)
        recovered = TaskState.open_or_create(
            task_path,
            source={},
            job_dir=job,
            display_name="ignored",
        )
        if recovered.status != "INTERRUPTED" or recovered.stage_status("TIMELINE") != "INTERRUPTED":
            raise AssertionError("Stale RUNNING task was not recovered")

        unfinished = scan_unfinished(root)
        if len(unfinished) != 1 or unfinished[0]["status"] != "INTERRUPTED":
            raise AssertionError(f"Unexpected unfinished task scan: {unfinished}")

        events = [json.loads(line) for line in recovered.event_log.read_text(encoding="utf-8").splitlines()]
        if not any(event["event"] == "stage_failed" for event in events):
            raise AssertionError("Structured failure event was not written")
        if redact("https://name:password@example.test/file") != "https://<redacted>@example.test/file":
            raise AssertionError("URL credentials were not redacted")
        if list(job.glob(".task.json.*.tmp")):
            raise AssertionError("Atomic checkpoint temp file was left behind")

        skip_job = root / "skip_job"
        skipped = TaskState.open_or_create(
            skip_job / "task.json",
            source={"mode": "japanese_only"},
            job_dir=skip_job,
            display_name="仅日文",
            stages=("INPUT", "TRANSLATION"),
        )
        skipped.start_stage("INPUT")
        skipped.complete_stage("INPUT")
        skipped.skip_stage("TRANSLATION", reason="仅生成日文字幕", component="Sakura")
        if skipped.status != "DONE" or skipped.stage_status("TRANSLATION") != "SKIPPED":
            raise AssertionError(f"Skipped terminal stage did not complete the task: {skipped.snapshot()}")
        reopened_skip = TaskState.open_or_create(
            skip_job / "task.json",
            source={},
            job_dir=skip_job,
            display_name="ignored",
        )
        if reopened_skip.status != "DONE":
            raise AssertionError("A task containing SKIPPED stages could not be reopened")

        exit_messages = {
            "Whisper exited with code 7": 7,
            "recognize_jp failed with exit code 1": 1,
            "component failed with code -9": -9,
        }
        for message, expected in exit_messages.items():
            actual = exit_code_from_error(SystemExit(message))
            if actual != expected:
                raise AssertionError(f"Exit code was not parsed from {message!r}: {actual}")

        failing_child = root / "failing_child.py"
        failing_child.write_text(
            "print('底层组件错误详情', flush=True)\nraise SystemExit(5)\n",
            encoding="utf-8",
        )
        captured = io.StringIO()
        try:
            with redirect_stdout(captured):
                run_command(
                    "fake_component",
                    '"{python}" "{script}"',
                    {"python": sys.executable, "script": str(failing_child), "proxy": ""},
                    False,
                )
        except SystemExit as exc:
            error_text = str(exc)
            if "exit code 5" not in error_text or "底层组件错误详情" not in error_text:
                raise AssertionError(f"Child error detail was not preserved: {error_text}")
        else:
            raise AssertionError("Failing child command was accepted")
        if "底层组件错误详情" not in captured.getvalue():
            raise AssertionError("Child output was not streamed to the parent")

    print("Task state tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
