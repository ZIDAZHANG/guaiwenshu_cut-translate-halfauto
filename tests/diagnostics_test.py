#!/usr/bin/env python3
"""Regression test for diagnostic bundle privacy and useful evidence."""

from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path

from diagnostics import create_diagnostic_bundle


ROOT = (Path(__file__).resolve().parents[1] / "app")


def main() -> int:
    output_root = ROOT / "output"
    output_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="diagnostics_", dir=output_root) as temp_dir:
        job = Path(temp_dir) / "秘密标题任务"
        (job / "logs").mkdir(parents=True)
        (job / "source").mkdir()
        (job / "subs").mkdir()
        (job / "deliverables").mkdir()
        task = {
            "schema_version": 1,
            "task_id": "test",
            "status": "FAILED",
            "current_stage": None,
            "source": {
                "video_file": str(Path.home() / "Videos" / "private.mp4"),
                "api_key": "SUPER_SECRET_API_KEY",
                "url": "https://name:password@example.test/watch?token=URL_SECRET",
            },
            "last_error": {
                "component": "Whisper",
                "message": "authorization=AUTH_SECRET failed",
            },
            "stages": {},
        }
        (job / "task.json").write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
        (job / "logs" / "desktop.log").write_text(
            "Authorization: Bearer BEARER_SECRET\n"
            "[00:00:01.000 --> 00:00:02.000] これは秘密の字幕です\n"
            "token=LOG_SECRET\n",
            encoding="utf-8",
        )
        (job / "logs" / "timeline_validation.json").write_text(
            json.dumps({"valid": True, "path": str(job / "subs" / "timeline.ass")}),
            encoding="utf-8",
        )
        (job / "subs" / "whisper.log").write_text(
            "token=LEGACY_LOG_SECRET\n"
            "[00:00:03.000 --> 00:00:04.000] 古い場所の秘密字幕\n",
            encoding="utf-8",
        )
        (job / "subs" / "sakura_server.log").write_text(
            "Sakura legacy component log is available\n",
            encoding="utf-8",
        )
        (job / "source" / "video.mp4").write_bytes(b"PRIVATE_VIDEO_BYTES")
        (job / "subs" / "jp.srt").write_text("PRIVATE_SUBTITLE_BODY", encoding="utf-8")
        (job / "deliverables" / "review.csv").write_text("PRIVATE_REVIEW_BODY", encoding="utf-8")

        (job / "logs" / "long.log").write_text(
            "EARLY_DIAGNOSTIC_ERROR_MUST_SURVIVE\n" + "INFO worker active\n" * 130000 + "FINAL_DIAGNOSTIC_STATUS\n",
            encoding="utf-8",
        )

        bundle = create_diagnostic_bundle(job)
        with zipfile.ZipFile(bundle) as archive:
            name_list = archive.namelist()
            names = set(name_list)
            combined = b"\n".join(archive.read(name) for name in names).decode("utf-8", errors="replace")
        if "EARLY_DIAGNOSTIC_ERROR_MUST_SURVIVE" not in combined or "FINAL_DIAGNOSTIC_STATUS" not in combined:
            raise AssertionError("Diagnostic bundle discarded errors from the beginning of a long log")
        if len(name_list) != len(names):
            raise AssertionError(f"Diagnostic bundle contains duplicate ZIP entries: {name_list}")
        required = {
            "diagnostics/summary.json",
            "diagnostics/task.json",
            "diagnostics/logs/desktop.log",
            "diagnostics/subs/whisper.log",
            "diagnostics/subs/sakura_server.log",
            "diagnostics/bundle_manifest.json",
        }
        if not required.issubset(names):
            raise AssertionError(f"Diagnostic bundle is missing evidence: {required - names}")
        forbidden = (
            "SUPER_SECRET_API_KEY",
            "URL_SECRET",
            "AUTH_SECRET",
            "BEARER_SECRET",
            "LOG_SECRET",
            "LEGACY_LOG_SECRET",
            "これは秘密の字幕です",
            "古い場所の秘密字幕",
            "PRIVATE_VIDEO_BYTES",
            "PRIVATE_SUBTITLE_BODY",
            "PRIVATE_REVIEW_BODY",
            str(Path.home()),
        )
        leaked = [value for value in forbidden if value in combined]
        if leaked:
            raise AssertionError(f"Diagnostic bundle leaked private content: {leaked}")
        if "<subtitle text omitted>" not in combined or "artifact_validation" in combined:
            raise AssertionError("Unexpected diagnostic log sanitization result")
        if any(name.endswith((".mp4", ".srt", ".ass", ".csv")) for name in names):
            raise AssertionError(f"Media or subtitle content was included: {names}")

    print("Diagnostics tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
