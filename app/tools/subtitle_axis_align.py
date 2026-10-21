#!/usr/bin/env python3
"""
Align subtitle text tracks onto a verified timing track.

Typical use:
  python tools/subtitle_axis_align.py --timeline empty.ass --zh machine.srt --jp ocr_jp.srt --out aligned.ass --report review.tsv
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import re
import sys
from pathlib import Path
from typing import Iterable, Optional


TIME_RE = re.compile(r"(?P<h>\d+):(?P<m>\d{2}):(?P<s>\d{2})[,.](?P<ms>\d{1,3})")
FALLBACK_CHOICES = ("none", "jp", "timeline")
ALIGNMENT_VERSION = 3
LEGACY_FALLBACK_ALIASES = {
    "nearest": "none",
    "machine": "none",
}


@dataclasses.dataclass
class Cue:
    start: int
    end: int
    text: str
    index: int = 0
    raw: object = None

    @property
    def duration(self) -> int:
        return max(1, self.end - self.start)


@dataclasses.dataclass
class AssData:
    lines: list[str]
    format_fields: list[str]
    cues: list[Cue]


def read_text(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp932", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8-sig")


def parse_time(value: str) -> int:
    m = TIME_RE.search(value.strip())
    if not m:
        raise ValueError(f"Bad subtitle time: {value!r}")
    ms = int(m.group("ms").ljust(3, "0")[:3])
    return ((int(m.group("h")) * 60 + int(m.group("m"))) * 60 + int(m.group("s"))) * 1000 + ms


def fmt_srt_time(ms: int) -> str:
    ms = max(0, int(ms))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def fmt_ass_time(ms: int) -> str:
    ms = max(0, int(ms))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, milli = divmod(rem, 1000)
    return f"{h:d}:{m:02d}:{s:02d}.{milli // 10:02d}"


def normalize_text(text: str) -> str:
    text = text.replace("\\N", "\n").replace("\\n", "\n")
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\{[^}]*\}", "", text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return "\n".join(part.strip() for part in text.splitlines() if part.strip()).strip()


def ass_escape(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", r"\N")


def normalize_fallback(value: object) -> str:
    normalized = str(value or "none").strip().lower()
    normalized = LEGACY_FALLBACK_ALIASES.get(normalized, normalized)
    if normalized not in FALLBACK_CHOICES:
        raise ValueError(f"Unsupported alignment fallback: {value!r}")
    return normalized


def parse_srt(path: Path) -> list[Cue]:
    text = read_text(path).replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", text.strip())
    cues: list[Cue] = []
    for block in blocks:
        lines = [line.rstrip("\n") for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        time_line_pos = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if time_line_pos is None:
            continue
        left, right = lines[time_line_pos].split("-->", 1)
        cue_text = "\n".join(lines[time_line_pos + 1 :])
        cues.append(Cue(parse_time(left), parse_time(right), normalize_text(cue_text), len(cues) + 1))
    return cues


def parse_ass(path: Path) -> AssData:
    lines = read_text(path).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    in_events = False
    fields: list[str] = []
    cues: list[Cue] = []

    for line_no, line in enumerate(lines):
        stripped = line.strip()
        if stripped.lower() == "[events]":
            in_events = True
            continue
        if in_events and stripped.startswith("[") and stripped.endswith("]"):
            in_events = False
        if not in_events:
            continue
        if stripped.lower().startswith("format:"):
            fields = [part.strip() for part in stripped.split(":", 1)[1].split(",")]
            continue
        if stripped.lower().startswith("dialogue:") and fields:
            payload = line.split(":", 1)[1].lstrip()
            parts = payload.split(",", len(fields) - 1)
            if len(parts) != len(fields):
                continue
            field_map = {name.lower(): i for i, name in enumerate(fields)}
            try:
                start = parse_time(parts[field_map["start"]])
                end = parse_time(parts[field_map["end"]])
            except (KeyError, ValueError):
                continue
            text_idx = field_map.get("text")
            cue_text = normalize_text(parts[text_idx]) if text_idx is not None else ""
            cues.append(Cue(start, end, cue_text, len(cues) + 1, raw=(line_no, parts)))
    return AssData(lines, fields, cues)


def parse_subtitle(path: Path) -> tuple[str, object, list[Cue]]:
    suffix = path.suffix.lower()
    if suffix in (".ass", ".ssa"):
        ass = parse_ass(path)
        return "ass", ass, ass.cues
    if suffix in (".srt", ".vtt"):
        return "srt", None, parse_srt(path)
    raise ValueError(f"Unsupported subtitle format: {path}")


def overlap_ms(a: Cue, b: Cue) -> int:
    return max(0, min(a.end, b.end) - max(a.start, b.start))


def match_score(target: Cue, source: Cue, tolerance_ms: int) -> float:
    overlap = overlap_ms(target, source)
    if overlap > 0:
        union = max(target.end, source.end) - min(target.start, source.start)
        return overlap / max(1, union)
    if source.end <= target.start:
        gap = target.start - source.end
    else:
        gap = source.start - target.end
    if tolerance_ms > 0 and gap <= tolerance_ms:
        return max(0.05, 0.35 * (1 - gap / max(1, tolerance_ms)))
    return 0.0


def best_match(target: Cue, source_cues: list[Cue], tolerance_ms: int) -> tuple[Optional[Cue], float]:
    best: tuple[Optional[Cue], float] = (None, 0.0)
    for source in source_cues:
        score = match_score(target, source, tolerance_ms)
        if score > best[1]:
            best = (source, score)
    return best


def nearby_texts(target: Cue, source_cues: list[Cue], tolerance_ms: int, min_score: float) -> list[tuple[Cue, float]]:
    matches: list[tuple[Cue, float]] = []
    for cue in source_cues:
        score = match_score(target, cue, tolerance_ms)
        if score >= min_score:
            matches.append((cue, score))
    matches.sort(key=lambda item: (item[0].start, item[0].end))
    return matches


def joined_text(matches: Iterable[tuple[Cue, float]], max_join: int) -> str:
    texts: list[str] = []
    for cue, _score in matches:
        text = normalize_text(cue.text)
        if text:
            texts.append(text)
    return "\n".join(texts)


def confidence(score: float, text: str, index_delta: Optional[int]) -> str:
    if not text:
        return "empty"
    if score >= 0.55 and (index_delta is None or abs(index_delta) <= 2):
        return "high"
    if score >= 0.25 and (index_delta is None or abs(index_delta) <= 5):
        return "medium"
    return "low"


def align(
    timeline: list[Cue],
    zh: list[Cue],
    jp: list[Cue],
    tolerance_ms: int,
    min_join_score: float,
    max_join: int,
    fallback: str,
) -> tuple[list[str], list[dict[str, object]]]:
    if tolerance_ms < 0:
        raise ValueError("Alignment tolerance must be nonnegative")
    fallback = normalize_fallback(fallback)
    output_texts: list[str] = []
    rows: list[dict[str, object]] = []

    def owners(cues: list[Cue]) -> dict[int, int]:
        result = {}
        for position, cue in enumerate(cues):
            overlaps = [overlap_ms(target, cue) for target in timeline]
            if overlaps and max(overlaps) > 0:
                result[position] = max(range(len(overlaps)), key=overlaps.__getitem__)
        return result

    zh_owners, jp_owners = owners(zh), owners(jp)

    def select(target: Cue, index: int, cues: list[Cue], ownership: dict[int, int], label: str):
        positives = [(cue, match_score(target, cue, 0)) for cue in cues if cue.text and overlap_ms(target, cue) > 0]
        flags = []
        candidates = positives
        if not candidates:
            best, score = best_match(target, [cue for cue in cues if cue.text], tolerance_ms)
            if best is None:
                return [], 0.0, flags
            return [(best, score)], score, [f"NEARBY_{label}"]
        # Legacy join arguments remain accepted but never discard source content.
        selected = sorted(candidates, key=lambda item: (item[0].start, item[0].end, item[0].index))
        if any(overlap_ms(target, cue) < cue.duration for cue, _ in selected):
            flags.append(f"CROSS_TARGET_{label}")
        intervals = sorted((max(target.start, cue.start), min(target.end, cue.end)) for cue, _ in selected)
        covered, end = 0, target.start
        for start, stop in intervals:
            covered += max(0, stop - max(start, end))
            end = max(end, stop)
        return selected, covered / target.duration, flags

    for i, target in enumerate(timeline, start=1):
        zh_matches, zh_score, zh_flags = select(target, i - 1, zh, zh_owners, "ZH")
        jp_matches, jp_score, jp_flags = select(target, i - 1, jp, jp_owners, "JP")
        zh_best = max(zh_matches, key=lambda item: item[1])[0] if zh_matches else None
        jp_best = max(jp_matches, key=lambda item: item[1])[0] if jp_matches else None
        zh_text = joined_text(zh_matches, max_join)
        jp_text = joined_text(jp_matches, max_join)

        chosen = zh_text
        source = "zh"
        if not chosen and fallback == "jp":
            chosen = jp_text
            source = "jp"
        elif not chosen and fallback == "timeline":
            chosen = normalize_text(target.text)
            source = "timeline"

        conf = confidence(zh_score if source == "zh" else jp_score, chosen, None)
        flags: list[str] = zh_flags + jp_flags
        visual_text = normalize_text(target.text)
        if visual_text and jp_text and re.search(r"[\u3040-\u30ff\u3400-\u9fff]", visual_text):
            comparable = lambda value: "".join(char for char in value if char.isalnum())
            if comparable(visual_text) != comparable(jp_text):
                flags.append("TEXT_MISMATCH")
        if flags or source == "timeline":
            conf = "low" if chosen else "empty"
        if conf in ("low", "empty"):
            flags.append("CHECK")
        if zh_best and jp_best and abs(zh_best.start - jp_best.start) > tolerance_ms:
            flags.append("ZH_JP_TIME_MISMATCH")
            if "CHECK" not in flags:
                flags.append("CHECK")
            conf = "low" if chosen else "empty"
        if len(zh_matches) > 1:
            flags.append("MERGED_ZH")
        if len(jp_matches) > 1:
            flags.append("MERGED_JP")

        output_texts.append(chosen)
        rows.append(
            {
                "line": i,
                "start": fmt_srt_time(target.start),
                "end": fmt_srt_time(target.end),
                "confidence": conf,
                "flags": ",".join(flags),
                "source": source,
                "zh_index": zh_best.index if zh_best else "",
                "zh_score": f"{zh_score:.3f}",
                "jp_index": jp_best.index if jp_best else "",
                "jp_score": f"{jp_score:.3f}",
                "alignment_version": ALIGNMENT_VERSION,
                "zh_indices": ",".join(str(cue.index) for cue, _ in zh_matches),
                "jp_indices": ",".join(str(cue.index) for cue, _ in jp_matches),
                "zh_overlap_ms": sum(overlap_ms(target, cue) for cue, _ in zh_matches),
                "jp_overlap_ms": sum(overlap_ms(target, cue) for cue, _ in jp_matches),
                "zh_text": zh_text,
                "jp_text": jp_text,
                "output_text": chosen.replace("\n", " / "),
            }
        )
    for label in ("zh", "jp"):
        usages: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            for source_id in str(row[f"{label}_indices"]).split(","):
                if source_id:
                    usages.setdefault(source_id, []).append(row)
        for matched_rows in usages.values():
            if len(matched_rows) > 1:
                for row in matched_rows:
                    flags = str(row["flags"]).split(",") if row["flags"] else []
                    for flag in (f"SOURCE_REUSED_{label.upper()}", "CHECK"):
                        if flag not in flags:
                            flags.append(flag)
                    row["flags"] = ",".join(flags)
                    if row["confidence"] != "empty":
                        row["confidence"] = "low"
    return output_texts, rows


def render_srt(timeline: list[Cue], texts: list[str], mark_low: set[int]) -> str:
    blocks: list[str] = []
    for timeline_index, (cue, text) in enumerate(zip(timeline, texts), start=1):
        if not text.strip():
            continue
        if timeline_index in mark_low:
            text = "[CHECK] " + text
        output_index = len(blocks) + 1
        blocks.append(f"{output_index}\n{fmt_srt_time(cue.start)} --> {fmt_srt_time(cue.end)}\n{text}")
    return "\n\n".join(blocks) + "\n"


def render_ass(ass: AssData, texts: list[str], mark_low: set[int]) -> str:
    out: list[Optional[str]] = list(ass.lines)
    field_map = {name.lower(): i for i, name in enumerate(ass.format_fields)}
    text_idx = field_map.get("text")
    if text_idx is None:
        raise ValueError("ASS Events format has no Text field.")
    for cue, text in zip(ass.cues, texts):
        if not cue.raw:
            continue
        line_no, parts = cue.raw
        if not text.strip():
            out[line_no] = None
            continue
        if cue.index in mark_low:
            text = "[CHECK] " + text
        parts[text_idx] = ass_escape(text)
        out[line_no] = "Dialogue: " + ",".join(parts)
    return "\n".join(line for line in out if line is not None)


def write_report(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["line"])
        writer.writeheader()
        writer.writerows(rows)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Align subtitle text tracks onto a verified timing track.")
    parser.add_argument("--timeline", required=True, type=Path, help="Verified empty/timing subtitle, ASS or SRT.")
    parser.add_argument("--zh", type=Path, help="Machine Chinese subtitle, ASS or SRT.")
    parser.add_argument("--jp", type=Path, help="OCR Japanese subtitle, ASS or SRT.")
    parser.add_argument("--out", required=True, type=Path, help="Output subtitle path.")
    parser.add_argument("--report", type=Path, help="Review TSV/CSV path.")
    parser.add_argument("--fallback", choices=FALLBACK_CHOICES, default="none")
    parser.add_argument("--tolerance-ms", type=int, default=1200)
    parser.add_argument("--min-join-score", type=float, default=0.18, help="Deprecated compatibility argument; never excludes content.")
    parser.add_argument("--max-join", type=int, default=3, help="Deprecated compatibility argument; output has no join-count cap.")
    parser.add_argument("--mark-low", action="store_true", help="Prefix low-confidence output lines with [CHECK].")
    parser.add_argument("--bilingual", action="store_true", help="Append matched Japanese text below each Chinese line.")
    args = parser.parse_args(argv)

    timeline_kind, timeline_data, timeline = parse_subtitle(args.timeline)
    _zh_kind, _zh_data, zh = parse_subtitle(args.zh) if args.zh else ("", None, [])
    _jp_kind, _jp_data, jp = parse_subtitle(args.jp) if args.jp else ("", None, [])

    if not timeline:
        raise SystemExit("Timeline subtitle has no cues.")
    if not zh and not jp:
        raise SystemExit("Provide at least --zh or --jp.")

    texts, rows = align(
        timeline=timeline,
        zh=zh,
        jp=jp,
        tolerance_ms=args.tolerance_ms,
        min_join_score=args.min_join_score,
        max_join=args.max_join,
        fallback=args.fallback,
    )
    if args.bilingual:
        bilingual_texts: list[str] = []
        for row, zh_text in zip(rows, texts):
            jp_text = str(row["jp_text"])
            if zh_text == jp_text:
                jp_text = ""
            bilingual_texts.append("\n".join(part for part in (zh_text, jp_text) if part))
        texts = bilingual_texts
    low_lines = {int(row["line"]) for row in rows if "CHECK" in str(row["flags"]).split(",")}

    if args.out.suffix.lower() in (".ass", ".ssa") or timeline_kind == "ass":
        if timeline_kind != "ass" or not isinstance(timeline_data, AssData):
            output = render_srt(timeline, texts, low_lines if args.mark_low else set())
        else:
            output = render_ass(timeline_data, texts, low_lines if args.mark_low else set())
    else:
        output = render_srt(timeline, texts, low_lines if args.mark_low else set())
    write_text(args.out, output)

    if args.report:
        write_report(args.report, rows)

    counts: dict[str, int] = {}
    for row in rows:
        counts[str(row["confidence"])] = counts.get(str(row["confidence"]), 0) + 1
    print(f"Wrote: {args.out}")
    if args.report:
        print(f"Report: {args.report}")
    print("Confidence:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
