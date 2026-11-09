#!/usr/bin/env python3
"""Machine subtitle files (plan 1.1, sections 6.7, 7.5, 9.5; W04).

Fixed machine formats: SRT = UTF-8 BOM, CRLF, numbered from 1, milliseconds; ASS = UTF-8 BOM, CRLF, centiseconds
rounded to the nearest 10 ms (positive half up). A cue whose rounded ASS duration would not be positive is reported as
TIME_PRECISION_COLLAPSE and never lengthened; the original milliseconds of every cue are kept in a sidecar.

Styles: the machine timeline's own ASS header is reused when the run has one (the OCR output), otherwise the fixed base
style of plan 7.5 (PlayRes 1920x1080, Microsoft YaHei 80, white text with a black outline, Outline 2, Shadow 2,
Alignment 2, MarginL/R 10, MarginV 200, ScaleX/Y 100, WrapStyle 0; the remaining fields follow the audited Default
row). Human ASS files are never rewritten here; this module only produces machine output.
"""

from __future__ import annotations

from pathlib import Path

from upgrade.contracts import ContractError

MACHINE_FORMAT_VERSION = 1
ASS_RESOLUTION_MS = 10
EVENT_FIELDS = ("Layer", "Start", "End", "Style", "Name", "MarginL", "MarginR", "MarginV", "Effect", "Text")
BASE_STYLE_ID = "base_v1"
BASE_HEADER = (
    "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1920", "PlayResY: 1080", "WrapStyle: 0", "",
    "[V4+ Styles]",
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
    "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
    "Style: Default,Microsoft YaHei,80,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,200,1",
)
DEFAULT_EVENT = {"Layer": "0", "Style": "Default", "Name": "", "MarginL": "0", "MarginR": "0", "MarginV": "0", "Effect": ""}


# --------------------------------------------------------------------------- time formatting

def srt_time(ms: int) -> str:
    if ms < 0:
        raise ContractError("SUBTITLE_INVALID", "negative time cannot be written", details={"ms": ms})
    hours, rest = divmod(int(ms), 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, milli = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milli:03d}"


def ass_centiseconds(ms: int) -> int:
    """Nearest 10 ms, positive half rounded up (plan 6.7)."""
    return (int(ms) + ASS_RESOLUTION_MS // 2) // ASS_RESOLUTION_MS


def ass_time(centiseconds: int) -> str:
    hours, rest = divmod(int(centiseconds), 360_000)
    minutes, rest = divmod(rest, 6_000)
    seconds, centi = divmod(rest, 100)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}.{centi:02d}"


# --------------------------------------------------------------------------- SRT

def srt_lines(text: str) -> list[str]:
    """Cue text lines as SRT can carry them: a blank line would end the block, so empty lines are not representable."""
    return [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if line.strip()]


def render_srt_bytes(cues: list[tuple[int, int, str]]) -> bytes:
    blocks = []
    for number, (start_ms, end_ms, text) in enumerate(cues, start=1):
        if end_ms <= start_ms:
            raise ContractError("SUBTITLE_INVALID", f"cue {number} has a non-positive duration", details={"index": number, "start_ms": start_ms, "end_ms": end_ms})
        blocks.append("\r\n".join([str(number), f"{srt_time(start_ms)} --> {srt_time(end_ms)}", *srt_lines(text)]) + "\r\n")
    return ("﻿" + "\r\n".join(blocks)).encode("utf-8")


# --------------------------------------------------------------------------- ASS

def ass_escape(text: str) -> str:
    from subtitle_axis_align import ass_escape as legacy_escape
    return legacy_escape(text.replace("\r\n", "\n").replace("\r", "\n"))


def parse_template(template_text: str) -> dict:
    """Split a machine timeline ASS into the parts the machine output reuses: header sections before [Events], the
    event field order, the Dialogue events (field dict per event, in file order) and any sections after [Events]."""
    lines = template_text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    header: list[str] = []
    trailer: list[str] = []
    fields: list[str] = []
    events: list[dict[str, str]] = []
    section = None
    seen_events = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped.lower()
            if section == "[events]":
                seen_events = True
                continue
        if section != "[events]":
            (trailer if seen_events else header).append(line)
            continue
        if stripped.lower().startswith("format:"):
            fields = [part.strip() for part in stripped.split(":", 1)[1].split(",")]
            continue
        if stripped.lower().startswith("dialogue:") and fields:
            parts = line.split(":", 1)[1].lstrip().split(",", len(fields) - 1)
            if len(parts) == len(fields):
                events.append(dict(zip(fields, parts)))
    while header and not header[-1].strip():
        header.pop()
    styles = [line.split(":", 1)[1].split(",", 1)[0].strip() for line in header if line.strip().lower().startswith("style:")]
    if not styles or not any(line.strip().lower() in ("[v4+ styles]", "[v4 styles]") for line in header):
        raise ContractError("SUBTITLE_INVALID", "the ASS template has no style table; the machine ASS cannot inherit its style", details={"styles": styles})
    return {"header": header, "fields": fields, "events": events, "trailer": [line for line in trailer if line.strip()], "styles": styles}


def base_template() -> dict:
    return {"header": list(BASE_HEADER), "fields": list(EVENT_FIELDS), "events": [], "trailer": [], "styles": ["Default"]}


def render_ass(cues: list[dict], template: dict) -> tuple[bytes, list[dict]]:
    """``cues``: [{index, cue_id, start_ms, end_ms, text, template_event (int|None)}] -> (bytes, timing sidecar rows).

    Event fields other than Start/End/Text come from the associated template event (the OCR event the cue was built
    from) or from the defaults with the template's first style when no event is associated.
    """
    default_style = "Default" if "Default" in template["styles"] else template["styles"][0]
    collapsed = []
    rows = []
    dialogues = []
    for cue in cues:
        start_cs, end_cs = ass_centiseconds(cue["start_ms"]), ass_centiseconds(cue["end_ms"])
        rows.append({"index": cue["index"], "cue_id": cue["cue_id"], "start_ms": cue["start_ms"], "end_ms": cue["end_ms"],
                     "ass_start_cs": start_cs, "ass_end_cs": end_cs, "template_event": cue.get("template_event")})
        if end_cs <= start_cs:
            collapsed.append({"cue_id": cue["cue_id"], "index": cue["index"], "start_ms": cue["start_ms"], "end_ms": cue["end_ms"]})
            continue
        source = template["events"][cue["template_event"]] if cue.get("template_event") is not None and cue["template_event"] < len(template["events"]) else {}
        values = {**DEFAULT_EVENT, "Style": default_style}
        for name in ("Layer", "Style", "Name", "MarginL", "MarginR", "MarginV", "Effect"):
            if str(source.get(name, "")).strip():
                values[name] = source[name].strip()
        values.update({"Start": ass_time(start_cs), "End": ass_time(end_cs), "Text": ass_escape(cue["text"])})
        dialogues.append("Dialogue: " + ",".join(values[name] for name in EVENT_FIELDS))
    if collapsed:
        raise ContractError("TIME_PRECISION_COLLAPSE", f"{len(collapsed)} cue(s) would have no duration at ASS precision; the cue is not lengthened",
                            details={"cues": collapsed, "resolution_ms": ASS_RESOLUTION_MS})
    lines = [*template["header"], "", "[Events]", "Format: " + ", ".join(EVENT_FIELDS), *dialogues]
    if template["trailer"]:
        lines += ["", *template["trailer"]]
    return ("﻿" + "\r\n".join(lines) + "\r\n").encode("utf-8"), rows


# --------------------------------------------------------------------------- read-back checks (QC01 / QC02 / QC06 on the files just written)

def parse_srt_file(path: Path) -> list:
    from subtitle_axis_align import parse_srt
    return parse_srt(Path(path))


def parse_ass_file(path: Path) -> list:
    from subtitle_axis_align import parse_ass
    return parse_ass(Path(path)).cues


def verify_written(path: Path, kind: str, expected: list[dict], *, duration_ms: int | None) -> dict:
    """Re-read a machine file with the legacy parser and compare it with what should have been written.

    ``expected``: [{index, start_ms, end_ms, text}]. ASS times are compared at 10 ms. Returns a small report; raises
    SUBTITLE_INVALID on any structural difference (QC01 parse, QC02 time, QC03 bounds, QC06 order).
    """
    from subtitle_axis_align import normalize_text
    raw = Path(path).read_bytes()
    problems: list[str] = []
    if not raw.startswith(b"\xef\xbb\xbf"):
        problems.append("missing UTF-8 BOM")
    text = raw.decode("utf-8")
    if "\n" in text.replace("\r\n", ""):
        problems.append("bare LF line ending")
    cues = parse_srt_file(path) if kind == "srt" else parse_ass_file(path)
    if len(cues) != len(expected):
        problems.append(f"parsed {len(cues)} cue(s), expected {len(expected)}")
    if kind == "srt":
        numbers = [int(block.split("\r\n", 1)[0]) for block in text.lstrip("﻿").split("\r\n\r\n") if block.strip()]
        if numbers != list(range(1, len(expected) + 1)):
            problems.append("SRT numbering is not 1..n")
    tolerance = 0 if kind == "srt" else ASS_RESOLUTION_MS
    last_start = -1
    for cue, want in zip(cues, expected):
        want_start, want_end = (want["start_ms"], want["end_ms"]) if kind == "srt" else (ass_centiseconds(want["start_ms"]) * 10, ass_centiseconds(want["end_ms"]) * 10)
        if (cue.start, cue.end) != (want_start, want_end):
            problems.append(f"cue {want['index']}: time {cue.start}-{cue.end} differs from {want_start}-{want_end}")
        if cue.start < 0 or cue.end <= cue.start:
            problems.append(f"cue {want['index']}: non-positive duration or negative start")
        if cue.start < last_start:
            problems.append(f"cue {want['index']}: order moves backwards")
        last_start = cue.start
        if normalize_text(cue.text) != normalize_text(want["text"]):
            problems.append(f"cue {want['index']}: text differs after read-back")
        if duration_ms is not None and cue.end > duration_ms + max(1, tolerance):
            problems.append(f"cue {want['index']}: ends {cue.end - duration_ms} ms after the media (QC03_BOUNDS)")
    if problems:
        raise ContractError("SUBTITLE_INVALID", f"the generated {kind.upper()} did not read back as written: {problems[0]}",
                            details={"kind": kind, "path": str(path), "problems": problems})
    return {"kind": kind, "cues": len(cues), "bytes": len(raw), "bom": True, "line_ending": "CRLF", "time_tolerance_ms": tolerance}


__all__ = ["MACHINE_FORMAT_VERSION", "ASS_RESOLUTION_MS", "EVENT_FIELDS", "BASE_STYLE_ID", "BASE_HEADER", "srt_time", "ass_centiseconds", "ass_time",
           "render_srt_bytes", "parse_template", "base_template", "render_ass", "verify_written"]
