#!/usr/bin/env python3
"""
Jianying/CapCut desktop bridge helpers.

This does not blindly modify Jianying drafts. It first classifies the draft
content file. If the draft is not a normal JSON document, the safe output is an
import package containing video, subtitle, review report, and a manifest.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


def classify_draft(draft_dir: Path) -> dict[str, Any]:
    content = draft_dir / "draft_content.json"
    meta = draft_dir / "draft_meta_info.json"
    result: dict[str, Any] = {
        "draft_dir": str(draft_dir),
        "exists": draft_dir.exists(),
        "draft_content": str(content),
        "draft_content_exists": content.exists(),
        "draft_meta_exists": meta.exists(),
        "format": "missing",
        "safe_to_patch": False,
        "reason": "",
    }
    if not draft_dir.exists():
        result["reason"] = "Draft directory does not exist."
        return result
    if not content.exists():
        result["reason"] = "draft_content.json does not exist."
        return result

    raw = content.read_bytes()
    sample = raw[:256]
    result["draft_content_size"] = len(raw)
    result["sample_prefix"] = sample[:64].decode("utf-8", errors="replace")

    try:
        text = raw.decode("utf-8-sig")
        parsed = json.loads(text)
    except Exception as exc:  # noqa: BLE001 - diagnostic tool
        result["format"] = "opaque_or_encoded"
        result["reason"] = f"draft_content.json is not plain JSON: {type(exc).__name__}: {exc}"
        return result

    result["format"] = "plain_json"
    result["top_level_type"] = type(parsed).__name__
    if isinstance(parsed, dict):
        result["top_level_keys"] = sorted(parsed.keys())[:80]
        has_materials = "materials" in parsed
        has_tracks = "tracks" in parsed
        result["safe_to_patch"] = bool(has_materials and has_tracks)
        if result["safe_to_patch"]:
            result["reason"] = "Looks like a patchable Jianying JSON draft, but patching should still back up first."
        else:
            result["reason"] = "Plain JSON, but expected materials/tracks keys were not found."
    else:
        result["reason"] = "Plain JSON, but top-level value is not an object."
    return result


def copy_if_present(src: Path, dst_dir: Path, label: str) -> dict[str, Any]:
    item = {"label": label, "source": str(src), "copied": False, "target": ""}
    if not src or not src.exists():
        item["reason"] = "missing"
        return item
    dst = dst_dir / src.name
    if src.resolve() != dst.resolve():
        shutil.copy2(src, dst)
    item["copied"] = True
    item["target"] = str(dst)
    item["size"] = dst.stat().st_size
    return item


def write_text_atomic(path: Path, text: str) -> None:
    """Write UTF-8 text without exposing a partially written output file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def create_import_package(
    out_dir: Path,
    video: Path | None,
    subtitle: Path | None,
    review: Path | None,
    draft_status: dict[str, Any] | None,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    if video:
        copied.append(copy_if_present(video, out_dir, "video"))
    if subtitle:
        copied.append(copy_if_present(subtitle, out_dir, "subtitle"))
    if review:
        copied.append(copy_if_present(review, out_dir, "review"))

    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "purpose": "Import these files into Jianying for final manual review.",
        "files": copied,
        "draft_status": draft_status,
        "recommended_flow": [
            "Open Jianying.",
            "Create or open the target project.",
            "Import the video file from this package.",
            "Import the subtitle file from this package.",
            "Use the review CSV to check low-confidence or mismatched subtitle lines.",
        ],
    }
    manifest_path = out_dir / "manifest.json"
    write_text_atomic(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))

    note_path = out_dir / "jianying_import_note.txt"
    write_text_atomic(
        note_path,
        "\n".join(
            [
                "剪映手动导入包",
                "",
                "使用方法：",
                "1. 打开剪映并新建或打开目标草稿。",
                "2. 导入本目录中的视频文件。",
                "3. 导入本目录中的 ASS/SRT 字幕文件。",
                "4. 参照 review.csv 检查低置信度或时间匹配异常条目。",
                "",
                "若 manifest.json 中 draft_status.format 为 opaque_or_encoded，",
                "说明草稿不是可安全修改的明文 JSON，请勿直接改写 draft_content.json。",
                "",
            ]
        ),
    )
    return manifest_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect Jianying draft safety and create an import package.")
    parser.add_argument("--draft", type=Path, help="Existing Jianying draft directory to inspect.")
    parser.add_argument("--video", type=Path, help="Video file to package.")
    parser.add_argument("--subtitle", type=Path, help="Final ASS/SRT subtitle to package.")
    parser.add_argument("--review", type=Path, help="Review CSV/TSV to package.")
    parser.add_argument("--out", type=Path, required=True, help="Output import package directory.")
    parser.add_argument("--status-out", type=Path, help="Optional path for draft status JSON.")
    args = parser.parse_args(argv)

    draft_status = classify_draft(args.draft) if args.draft else None
    if args.status_out and draft_status is not None:
        write_text_atomic(args.status_out, json.dumps(draft_status, ensure_ascii=False, indent=2))

    manifest = create_import_package(args.out, args.video, args.subtitle, args.review, draft_status)
    print(f"Wrote import package manifest: {manifest}")
    if draft_status:
        print(f"Draft format: {draft_status['format']}")
        print(f"Safe to patch: {draft_status['safe_to_patch']}")
        print(f"Reason: {draft_status['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
