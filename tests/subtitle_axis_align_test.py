#!/usr/bin/env python3
"""Focused regression tests for aligned subtitle rendering and fallback settings."""

from __future__ import annotations

import csv
import tempfile
from pathlib import Path

from artifact_validation import validate_review_csv, validate_subtitle
from one_click_roast import (
    ALIGNMENT_FALLBACK_LABELS,
    alignment_fallback_label,
    alignment_fallback_value,
)
from subtitle_axis_align import (
    FALLBACK_CHOICES,
    Cue,
    main as align_main,
    normalize_fallback,
    parse_ass,
    parse_srt,
    render_srt,
)


def ass_text(count: int) -> str:
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for index in range(count):
        start = index * 2
        lines.append(
            f"Dialogue: 0,0:00:{start:02d}.00,0:00:{start + 1:02d}.00,Default,,0,0,0,,Timeline_{index + 1}"
        )
    return "\n".join(lines) + "\n"


def srt_text(count: int) -> str:
    blocks = []
    for index in range(count):
        start = index * 2
        blocks.append(
            f"{index + 1}\n00:00:{start:02d},000 --> 00:00:{start + 1:02d},000\n中文第{index + 1}句"
        )
    return "\n\n".join(blocks) + "\n"


def align_files(root: Path, timeline_count: int, translated_count: int, stem: str) -> tuple[Path, Path]:
    timeline = root / f"{stem}_timeline.ass"
    translated = root / f"{stem}_zh.srt"
    output = root / f"{stem}_final.ass"
    review = root / f"{stem}_review.csv"
    timeline.write_text(ass_text(timeline_count), encoding="utf-8")
    translated.write_text(srt_text(translated_count), encoding="utf-8")
    result = align_main(
        [
            "--timeline",
            str(timeline),
            "--zh",
            str(translated),
            "--out",
            str(output),
            "--report",
            str(review),
            "--tolerance-ms",
            "0",
        ]
    )
    if result != 0:
        raise AssertionError(f"Alignment returned {result}")
    return output, review


def main() -> int:
    if set(ALIGNMENT_FALLBACK_LABELS.values()) != set(FALLBACK_CHOICES):
        raise AssertionError("GUI fallback options drifted from the aligner CLI")
    for label, value in ALIGNMENT_FALLBACK_LABELS.items():
        if alignment_fallback_value(label) != value or alignment_fallback_label(value) != label:
            raise AssertionError(f"Fallback option does not round-trip: {label} -> {value}")
    if normalize_fallback("nearest") != "none" or normalize_fallback("machine") != "none":
        raise AssertionError("Legacy invalid GUI fallback values are not migrated safely")

    with tempfile.TemporaryDirectory(prefix="subtitle_axis_align_") as temp_dir:
        root = Path(temp_dir)
        output, review = align_files(root, timeline_count=20, translated_count=19, stem="small_gap")
        output_cues = parse_ass(output).cues
        if len(output_cues) != 19 or any(not cue.text for cue in output_cues):
            raise AssertionError("Unmatched timing cues leaked into the ASS as blank Dialogue events")
        final_validation = validate_subtitle(
            output,
            "final",
            require_text=True,
            expected_language="zh",
            expected_cues=20,
        )
        if not final_validation.valid:
            raise AssertionError(f"A small reviewed gap should remain publishable: {final_validation.errors}")
        review_validation = validate_review_csv(review, minimum_rows=20)
        if not review_validation.valid:
            raise AssertionError(review_validation.errors)
        with review.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        if len(rows) != 20 or rows[-1]["confidence"] != "empty" or "CHECK" not in rows[-1]["flags"]:
            raise AssertionError("The omitted cue is not preserved in the review report")

        sparse_output, _ = align_files(root, timeline_count=10, translated_count=5, stem="large_gap")
        sparse_validation = validate_subtitle(
            sparse_output,
            "final",
            require_text=True,
            expected_language="zh",
            expected_cues=10,
        )
        if not sparse_validation.valid or not any("cue count differs" in item for item in sparse_validation.warnings):
            raise AssertionError("Count difference must be recorded without arbitrary rejection")

        rendered = render_srt(
            [Cue(0, 1000, "", 1), Cue(1000, 2000, "", 2)],
            ["中文", ""],
            set(),
        )
        rendered_path = root / "rendered.srt"
        rendered_path.write_text(rendered, encoding="utf-8")
        parsed = parse_srt(rendered_path)
        if len(parsed) != 1 or parsed[0].text != "中文":
            raise AssertionError("SRT rendering did not omit blank cues cleanly")

    print("Subtitle axis alignment tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
