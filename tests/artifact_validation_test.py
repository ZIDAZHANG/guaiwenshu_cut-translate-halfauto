#!/usr/bin/env python3
"""Focused regression tests for pipeline artifact validation."""

from __future__ import annotations

import tempfile
from pathlib import Path

from artifact_validation import validate_subtitle


def write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def assert_valid(path: Path, **kwargs: object) -> None:
    result = validate_subtitle(path, "test", **kwargs)
    if not result.valid:
        raise AssertionError(f"Expected valid artifact, got: {result.errors}")


def assert_invalid(path: Path, expected_fragment: str, **kwargs: object) -> None:
    result = validate_subtitle(path, "test", **kwargs)
    if result.valid:
        raise AssertionError(f"Expected invalid artifact: {path}")
    if not any(expected_fragment in error for error in result.errors):
        raise AssertionError(f"Missing {expected_fragment!r} in {result.errors}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="artifact_validation_") as temp_dir:
        root = Path(temp_dir)
        good_ja = write(
            root / "good_ja.srt",
            "1\n00:00:00,000 --> 00:00:01,500\nファインです\n\n"
            "2\n00:00:02,000 --> 00:00:03,000\n今日は晴れ\n",
        )
        assert_valid(good_ja, require_text=True, expected_language="ja")

        empty = write(
            root / "empty.srt",
            "1\n00:00:00,000 --> 00:00:01,000\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\n\n",
        )
        assert_invalid(empty, "all subtitle cues are empty", require_text=True)

        partly_empty = write(
            root / "partly_empty.srt",
            "1\n00:00:00,000 --> 00:00:01,000\n第一句\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\n第二句\n\n"
            "3\n00:00:02,000 --> 00:00:03,000\n\n"
            "4\n00:00:03,000 --> 00:00:04,000\n第四句\n",
        )
        assert_invalid(partly_empty, "empty subtitle cues are not allowed", require_text=True)

        malformed = write(
            root / "malformed.ass",
            "[Events]\n"
            "Format: Layer, Start, End, Style, Text\n"
            "Dialogue: 0,0:00:00.00,0:00:01.00,Default,正常\n"
            "Dialogue: 0,not-a-time,0:00:02.00,Default,损坏\n",
        )
        assert_invalid(malformed, "parsed 1 of 2", require_text=True)

        backwards = write(
            root / "backwards.srt",
            "1\n00:00:03,000 --> 00:00:04,000\n第一句\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\n第二句\n",
        )
        assert_invalid(backwards, "moves backwards", require_text=True)

        latin_only = write(
            root / "latin.srt",
            "1\n00:00:00,000 --> 00:00:01,000\nhello\n",
        )
        for language in ("ja", "zh"):
            result = validate_subtitle(latin_only, "name", require_text=True, expected_language=language)
            assert result.valid, result.errors
            assert any("language is unconfirmed" in item for item in result.warnings)
        symbols = write(root / "symbols.srt", "1\n00:00:00,000 --> 00:00:01,000\n・・・😀\n")
        assert_valid(symbols, require_text=True, expected_language="ja")

        too_long = write(
            root / "too_long.srt",
            "1\n00:00:00,000 --> 00:03:00,000\n中文\n",
        )
        assert_invalid(
            too_long,
            "extends beyond",
            require_text=True,
            expected_language="zh",
            media_duration_seconds=30,
        )

    print("Artifact validation tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
