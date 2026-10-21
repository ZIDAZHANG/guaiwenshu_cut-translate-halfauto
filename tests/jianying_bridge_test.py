#!/usr/bin/env python3
"""Regression tests for the safe Jianying import-package bridge."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from jianying_bridge import classify_draft, create_import_package


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="jianying_bridge_") as temp_dir:
        root = Path(temp_dir) / "中文目录" / ("长路径" * 12)
        root.mkdir(parents=True)

        missing = classify_draft(root / "不存在的草稿")
        assert missing["format"] == "missing"
        assert missing["safe_to_patch"] is False

        opaque_dir = root / "不透明草稿"
        opaque_dir.mkdir()
        (opaque_dir / "draft_content.json").write_bytes(b"\x00\xffnot-json")
        opaque = classify_draft(opaque_dir)
        assert opaque["format"] == "opaque_or_encoded"
        assert opaque["safe_to_patch"] is False

        plain_dir = root / "明文草稿"
        plain_dir.mkdir()
        (plain_dir / "draft_content.json").write_text(
            json.dumps({"materials": {}, "tracks": []}, ensure_ascii=False),
            encoding="utf-8-sig",
        )
        plain = classify_draft(plain_dir)
        assert plain["format"] == "plain_json"
        assert plain["safe_to_patch"] is True

        inputs = root / "输入"
        inputs.mkdir()
        video = inputs / "原视频.mp4"
        subtitle = inputs / "中文字幕.ass"
        review = inputs / "复核表.csv"
        video.write_bytes(b"video")
        subtitle.write_text("[Events]\nDialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,测试", encoding="utf-8")
        review.write_text("序号,状态\n1,通过\n", encoding="utf-8")

        out_dir = root / "输出" / "任务一" / "jianying_import"
        manifest_path = create_import_package(out_dir, video, subtitle, review, opaque)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["draft_status"]["format"] == "opaque_or_encoded"
        assert [item["copied"] for item in manifest["files"]] == [True, True, True]
        assert {item["label"] for item in manifest["files"]} == {"video", "subtitle", "review"}
        assert (out_dir / video.name).read_bytes() == b"video"
        assert (out_dir / subtitle.name).read_text(encoding="utf-8").startswith("[Events]")

        note_path = out_dir / "jianying_import_note.txt"
        note = note_path.read_text(encoding="utf-8")
        assert "剪映手动导入包" in note
        assert "ASS/SRT" in note
        assert "review.csv" in note
        assert "opaque_or_encoded" in note
        assert "????" not in note
        assert "\ufffd" not in note

        # Re-running into the same task folder must replace metadata cleanly.
        create_import_package(out_dir, video, subtitle, review, plain)
        replaced = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert replaced["draft_status"]["format"] == "plain_json"
        assert not list(out_dir.glob(".*.tmp"))

    print("Jianying bridge tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
