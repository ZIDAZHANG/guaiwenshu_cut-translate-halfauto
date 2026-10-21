#!/usr/bin/env python3
"""Regression test for MagiaTimeline ASCII staging preparation."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml
from unittest.mock import patch
from magia_timeline_adapter import normalize_output_encoding


ROOT = (Path(__file__).resolve().parents[1] / "app")
ADAPTER = ROOT / "tools" / "magia_timeline_adapter.py"



def test_native_output_encoding(root: Path) -> None:
    content = "[Events]\nFormat: Start, End, Text\nDialogue: 0:00:00.00,0:00:06.00,メジロアルダンとトレーナー\n"
    legacy = root / "legacy.ass"
    legacy.write_bytes(b"\xef\xbb\xbf" + content.encode("cp936"))
    with patch("magia_timeline_adapter.native_output_encoding", return_value="cp936"):
        details = normalize_output_encoding(legacy)
    assert legacy.read_text(encoding="utf-8-sig") == content
    assert details["source_encoding"] == "cp936"
    legacy.write_text(content, encoding="utf-8-sig")
    assert normalize_output_encoding(legacy)["source_encoding"] == "utf-8"
    assert legacy.read_text(encoding="utf-8-sig") == content
    damaged = root / "damaged.ass"
    damaged.write_bytes(b"\xef\xbb\xbf\x81")
    original = damaged.read_bytes()
    with patch("magia_timeline_adapter.native_output_encoding", return_value="cp936"):
        try:
            normalize_output_encoding(damaged)
        except UnicodeDecodeError:
            pass
        else:
            raise AssertionError("Invalid bytes must not be silently replaced")
    assert damaged.read_bytes() == original


def main() -> int:
    test_output_root = ROOT / "output"
    test_output_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="magia_adapter_", dir=test_output_root) as temp_dir:
        root = Path(temp_dir)
        test_native_output_encoding(root)
        app_root = root / "fake_magia"
        app_root.mkdir()
        executable = app_root / "MagiaTimeline.exe"
        executable.write_bytes(b"not executed in prepare-only mode")
        (app_root / "template.asst").write_text("[Events]\n", encoding="utf-8")
        (app_root / "config.yml").write_text(
            "source: []\n"
            "destination: []\n"
            "assTemplate: template.asst\n"
            "strategy: dtd\n"
            "preset: default\n"
            "engine: speculative\n"
            "dtd:\n"
            "  default:\n"
            "    dialogRect: [0.0, 1.0, 0.7, 1.0]\n",
            encoding="utf-8",
        )
        (app_root / "PaddleOCRModels").mkdir()

        media_dir = root / "带中文与emoji_😀"
        media_dir.mkdir()
        video = media_dir / "赛马娘 视频.mp4"
        video.write_bytes(b"fake video")
        output = media_dir / "字幕.ass"
        evidence = media_dir / "日志"
        staging_root = root / "ascii_staging"

        completed = subprocess.run(
            [
                sys.executable,
                str(ADAPTER),
                "--video",
                str(video),
                "--output",
                str(output),
                "--executable",
                str(executable),
                "--work-dir",
                str(evidence),
                "--staging-root",
                str(staging_root),
                "--prepare-only",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stdout)

        config_path = evidence / "config.ascii.yml"
        config_bytes = config_path.read_bytes()
        if not config_bytes.isascii():
            raise AssertionError("Prepared MagiaTimeline config is not ASCII-only")
        config = yaml.safe_load(config_bytes.decode("ascii"))
        assert config["maxResolution"] == 0, "Original resolution must not be reduced by a project default"
        serialized_paths = config["source"] + config["destination"] + [config["assTemplate"]]
        if not all(str(value).isascii() for value in serialized_paths):
            raise AssertionError(f"Unicode path leaked into Magia config: {serialized_paths}")
        if str(video) in config_path.read_text(encoding="ascii"):
            raise AssertionError("Original Unicode video path leaked into Magia config")
        if list(staging_root.glob("magia_job_*")):
            raise AssertionError("Temporary Magia workspace was not cleaned")

        validation_stub = {"prepare_output": str(config_path), "stdout": completed.stdout}
        (root / "test_evidence.json").write_text(
            json.dumps(validation_stub, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print("MagiaTimeline ASCII staging test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
