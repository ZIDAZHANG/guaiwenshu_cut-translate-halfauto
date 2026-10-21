#!/usr/bin/env python3
"""Validation helpers for media-pipeline artifacts."""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import re
from pathlib import Path
from typing import Any

from subtitle_axis_align import ALIGNMENT_VERSION, Cue, parse_subtitle, parse_time, read_text


KANA_RE = re.compile(r"[\u3040-\u30ff]")
CJK_RE = re.compile(r"[\u3400-\u9fff]")


@dataclasses.dataclass
class ValidationResult:
    path: str
    artifact_type: str
    valid: bool = True
    size_bytes: int = 0
    cue_count: int = 0
    nonempty_cue_count: int = 0
    first_start_ms: int | None = None
    last_end_ms: int | None = None
    kana_char_count: int = 0
    cjk_char_count: int = 0
    warnings: list[str] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)
    details: dict[str, Any] = dataclasses.field(default_factory=dict)

    def fail(self, message: str) -> None:
        self.valid = False
        self.errors.append(message)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def summary(self) -> str:
        if self.valid:
            return f"valid {self.artifact_type}: {self.cue_count} cues, {self.size_bytes} bytes"
        return "; ".join(self.errors) or f"invalid {self.artifact_type}"


class ArtifactValidationError(RuntimeError):
    def __init__(self, result: ValidationResult):
        super().__init__(result.summary())
        self.result = result


def require_valid(result: ValidationResult) -> ValidationResult:
    if not result.valid:
        raise ArtifactValidationError(result)
    return result


def validate_file(path: Path, artifact_type: str, min_bytes: int = 1) -> ValidationResult:
    path = path.resolve()
    result = ValidationResult(str(path), artifact_type)
    if not path.is_file():
        result.fail("file does not exist")
        return result
    try:
        result.size_bytes = path.stat().st_size
    except OSError as exc:
        result.fail(f"could not stat file: {exc}")
        return result
    if result.size_bytes < min_bytes:
        result.fail(f"file is too small: {result.size_bytes} < {min_bytes} bytes")
    return result


def _validate_cues(result: ValidationResult, cues: list[Cue]) -> None:
    result.cue_count = len(cues)
    result.nonempty_cue_count = sum(1 for cue in cues if cue.text.strip())
    if not cues:
        result.fail("subtitle has no parseable cues")
        return
    result.first_start_ms = min(cue.start for cue in cues)
    result.last_end_ms = max(cue.end for cue in cues)
    previous_start = -1
    backwards = 0
    for index, cue in enumerate(cues, start=1):
        if cue.start < 0:
            result.fail(f"cue {index} starts before zero")
        if cue.end <= cue.start:
            result.fail(f"cue {index} has non-positive duration")
        if cue.start < previous_start:
            backwards += 1
        previous_start = cue.start
    if backwards:
        result.fail(f"subtitle order moves backwards at {backwards} cue(s)")


def validate_subtitle(
    path: Path,
    role: str,
    *,
    require_text: bool,
    expected_language: str | None = None,
    expected_cues: int | None = None,
    media_duration_seconds: float | None = None,
) -> ValidationResult:
    path = path.resolve()
    result = validate_file(path, f"subtitle:{role}", min_bytes=16)
    if not result.valid:
        return result
    try:
        kind, _data, cues = parse_subtitle(path)
        raw_text = read_text(path)
    except Exception as exc:
        result.fail(f"subtitle parse failed: {exc}")
        return result

    result.details["format"] = kind
    raw_event_count = len(re.findall(r"(?im)^\s*Dialogue\s*:", raw_text)) if kind == "ass" else raw_text.count("-->")
    result.details["raw_event_count"] = raw_event_count
    _validate_cues(result, cues)
    if raw_event_count and raw_event_count != len(cues):
        result.fail(f"parsed {len(cues)} of {raw_event_count} timestamped events")

    if require_text and cues:
        empty_count = len(cues) - result.nonempty_cue_count
        empty_ratio = empty_count / len(cues)
        result.details["empty_ratio"] = round(empty_ratio, 4)
        if result.nonempty_cue_count == 0:
            result.fail("all subtitle cues are empty")
        elif empty_count:
            result.fail(f"empty subtitle cues are not allowed: {empty_count}/{len(cues)}")

    if expected_cues is not None and cues:
        difference = abs(len(cues) - expected_cues)
        result.details.update({"expected_cues": expected_cues, "cue_difference": difference})
        if difference:
            result.warnings.append(f"cue count differs from upstream: {len(cues)} vs {expected_cues}; inspect source coverage")

    joined = "\n".join(cue.text for cue in cues if cue.text)
    result.kana_char_count = len(KANA_RE.findall(joined))
    result.cjk_char_count = len(CJK_RE.findall(joined))
    if expected_language == "ja" and require_text:
        if result.kana_char_count == 0 and result.cjk_char_count == 0:
            result.warnings.append("Japanese subtitle contains no Kana or CJK text; language is unconfirmed")
        elif result.kana_char_count == 0:
            result.warnings.append("Japanese subtitle contains CJK text but no Kana")
    elif expected_language == "zh" and require_text:
        if result.cjk_char_count == 0:
            result.warnings.append("Chinese subtitle contains no CJK text; language is unconfirmed")
        elif result.kana_char_count > result.cjk_char_count:
            result.warnings.append("Chinese subtitle contains more Kana than CJK characters")

    if media_duration_seconds is not None and result.last_end_ms is not None:
        media_end_ms = int(media_duration_seconds * 1000)
        result.details["media_duration_ms"] = media_end_ms
        if result.last_end_ms > media_end_ms:
            result.fail("subtitle extends beyond the media duration")
    return result


def validate_review_csv(path: Path, minimum_rows: int = 1, *, require_provenance: bool = False) -> ValidationResult:
    result = validate_file(path, "review_csv", min_bytes=8)
    if not result.valid:
        return result
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except Exception as exc:
        result.fail(f"review CSV parse failed: {exc}")
        return result
    result.details["row_count"] = len(rows)
    if len(rows) < minimum_rows:
        result.fail(f"review CSV has too few rows: {len(rows)}")
    required = {"line", "start", "end", "confidence", "flags", "source", "output_text"}
    if require_provenance:
        required.update({"alignment_version", "zh_indices", "jp_indices", "zh_overlap_ms", "jp_overlap_ms"})
    if require_provenance and (not rows or not required.issubset(rows[0])):
        result.fail("review CSV is missing alignment provenance columns")
        return result
    review_lines = []
    flag_counts: dict[str, int] = {}
    usages: dict[tuple[str, str], list[dict]] = {}
    for index, row in enumerate(rows, 1):
        flags = set(str(row.get("flags", "")).split(",")) - {""}
        if "CHECK" in flags or row.get("confidence") in {"low", "empty"}:
            review_lines.append(index)
        for flag in flags:
            flag_counts[flag] = flag_counts.get(flag, 0) + 1
        if require_provenance:
            try:
                if int(row["line"]) != index or int(row["alignment_version"]) != ALIGNMENT_VERSION:
                    raise ValueError("line number or algorithm version mismatch")
                if parse_time(row["end"]) <= parse_time(row["start"]):
                    raise ValueError("non-positive duration")
                if row["confidence"] not in {"high", "medium", "low", "empty"}:
                    raise ValueError("unknown confidence")
                risky = any(flag.startswith(("NEARBY_", "CROSS_TARGET_", "SOURCE_REUSED_", "TRUNCATED_", "WEAK_OVERLAP_")) for flag in flags)
                if risky and ("CHECK" not in flags or row["confidence"] == "high"):
                    raise ValueError("risky match is not marked for review")
                for label in ("zh", "jp"):
                    if int(row[f"{label}_overlap_ms"]) < 0:
                        raise ValueError("negative overlap")
                    ids = row[f"{label}_indices"].split(",") if row[f"{label}_indices"] else []
                    if any(int(source_id) < 1 for source_id in ids) or len(ids) != len(set(ids)):
                        raise ValueError("invalid source indices")
                    for source_id in ids:
                        usages.setdefault((label, source_id), []).append(row)
            except (ValueError, TypeError, KeyError) as exc:
                result.fail(f"review row {index}: {exc}")
    for (label, source_index), matches in usages.items():
        if len(matches) > 1 and label == "jp" and all("SOURCE_SPLIT_JP" in row["flags"].split(",") for row in matches):
            try:
                ranges = []
                for row in matches:
                    spans = json.loads(row["source_ranges"])
                    declared = {int(value) for value in row["jp_indices"].split(",")}
                    if {int(span["parent_index"]) for span in spans} != declared:
                        raise ValueError("source range parent indices differ from provenance")
                    selected = [span for span in spans if int(span["parent_index"]) == int(source_index)]
                    if not selected:
                        raise ValueError("source split has no character ranges")
                    for span in selected:
                        start, end = int(span["char_start"]), int(span["char_end"])
                        if not 0 <= start < end:
                            raise ValueError("invalid source character range")
                        ranges.append((start, end))
                ranges.sort()
                if any(right > next_left for (_, right), (next_left, _) in zip(ranges, ranges[1:])):
                    raise ValueError("source character ranges overlap")
            except (ValueError, TypeError, KeyError) as exc:
                result.fail(f"invalid source split: {exc}")
            continue
        if len(matches) > 1 and any(f"SOURCE_REUSED_{label.upper()}" not in row["flags"].split(",") for row in matches):
            result.fail("reused source cue is not marked in every affected row")
    result.details.update({"needs_review": len(review_lines), "review_lines": review_lines, "flag_counts": flag_counts})
    if review_lines:
        result.warnings.append(f"{len(review_lines)} alignment rows need human review")
    return result


def validate_json(path: Path, required_keys: tuple[str, ...] = (), *, expected_values: dict | None = None) -> ValidationResult:
    result = validate_file(path, "json", min_bytes=2)
    if not result.valid:
        return result
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        result.fail(f"JSON parse failed: {exc}")
        return result
    if not isinstance(value, dict):
        result.fail("JSON root is not an object")
        return result
    missing = [key for key in required_keys if key not in value]
    if missing:
        result.fail(f"JSON is missing keys: {', '.join(missing)}")
    for key, expected in (expected_values or {}).items():
        if value.get(key) != expected:
            result.fail(f"JSON {key} is {value.get(key)!r}, expected {expected!r}")
    result.details["keys"] = sorted(value)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a subtitle pipeline artifact.")
    parser.add_argument("path", type=Path)
    parser.add_argument("--role", default="subtitle")
    parser.add_argument("--require-text", action="store_true")
    parser.add_argument("--language", choices=("ja", "zh"))
    parser.add_argument("--expected-cues", type=int)
    parser.add_argument("--media-duration", type=float)
    args = parser.parse_args()
    result = validate_subtitle(
        args.path,
        args.role,
        require_text=args.require_text,
        expected_language=args.language,
        expected_cues=args.expected_cues,
        media_duration_seconds=args.media_duration,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
