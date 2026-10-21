#!/usr/bin/env python3
"""In-place dictionary editor used by the desktop app (one_click_roast.py).

Supports two formats:
- Plain-text dictionaries (VoiceTransl dict_pre/dict_gpt/dict_after):
  lines of "source<TAB>target", optional "# note", "^^" prefix mode,
  "1^" once mode, comment lines starting with "//" or "\\\\".
- Glossary JSON (translation_glossary.json): {"source": "target"} or
  {"source": {"default": ..., "trainer_context": ..., "possessive": ...}}.

Editing keeps advanced fields and comments that the editor does not expose.
"""

from __future__ import annotations

import json
from pathlib import Path

from task_state import atomic_write_text


def load_txt_dictionary(path: Path, *, gpt: bool = False) -> tuple[list[dict], str]:
    """Read a plain-text dictionary into editable rows plus preserved header."""
    if not path.is_file():
        return [], ""
    raw = path.read_text(encoding="utf-8-sig")
    header_lines: list[str] = []
    rows: list[dict] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "\\\\")):
            header_lines.append(line)
            continue
        row = parse_txt_line(line, gpt=gpt)
        if row is None:
            header_lines.append(line)
            continue
        rows.append(row)
    return rows, "\n".join(header_lines)


def parse_txt_line(line: str, *, gpt: bool = False) -> dict | None:
    """Parse one dictionary line into {"source","target","mode","note"} or None."""
    stripped = line.strip()
    if not stripped or stripped.startswith(("//", "\\\\")):
        return None
    if gpt and "->" in stripped and "\t" not in stripped:
        source, remainder = stripped.split("->", 1)
        target, marker, note = remainder.partition("#")
        source = source.strip()
        target = target.strip()
        note = note.strip() if marker else ""
    else:
        parts = stripped.replace("    ", "\t").split("\t")
        if len(parts) < 2 or not parts[0].strip():
            return None
        source = parts[0].strip()
        target = parts[1].strip()
        note = "\t".join(parts[2:]).strip() if len(parts) > 2 else ""
        if not note and "#" in target:
            target, _, note = target.partition("#")
            target = target.strip()
            note = note.strip()
    if not source:
        return None
    mode = "all"
    if source.startswith("^^"):
        source = source[2:]
        mode = "prefix"
    elif source.startswith("1^"):
        source = source[2:]
        mode = "once"
    return {"source": source, "target": target.strip(), "mode": mode, "note": note.strip()}


def validate_dictionary_rows(rows: list[dict]) -> None:
    """Reject rows that would otherwise be silently lost or overwritten."""
    seen: dict[str, int] = {}
    for index, row in enumerate(rows, start=1):
        source = str(row.get("source", "")).strip()
        target = str(row.get("target", "")).strip()
        if not source and not target:
            continue
        if not source or not target:
            missing = "原文" if not source else "译文"
            raise ValueError(f"第 {index} 个词条缺少{missing}，请补全或删除该行。")
        if source in seen:
            raise ValueError(f"第 {index} 个词条与第 {seen[source]} 个词条的原文重复：{source}")
        seen[source] = index


def update_dictionary_row(
    rows: list[dict],
    index: int,
    *,
    source: str,
    target: str,
    mode: str | None = None,
    note: str | None = None,
) -> None:
    """Update editable fields without dropping hidden note/advanced metadata."""
    if index < 0 or index >= len(rows):
        raise IndexError("Dictionary row index is out of range")
    rows[index]["source"] = source
    rows[index]["target"] = target
    if mode is not None:
        rows[index]["mode"] = mode
    if note is not None:
        rows[index]["note"] = note


def serialize_txt_dictionary(rows: list[dict], header: str, *, gpt: bool = False) -> str:
    """Serialize edited rows back to plain-text dictionary lines."""
    validate_dictionary_rows(rows)
    lines = [header] if header else []
    for row in rows:
        source = row["source"].strip()
        target = row["target"].strip()
        if not source or not target:
            continue
        mode = row.get("mode", "all")
        prefix = "^^" if mode == "prefix" else ("1^" if mode == "once" else "")
        note = row.get("note", "").strip()
        if gpt:
            line = f"{prefix}{source} -> {target}"
            if note:
                line += f" # {note}"
        else:
            line = f"{prefix}{source}\t{target}"
            if note:
                line += f"\t{note}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def write_txt_dictionary(path: Path, rows: list[dict], header: str, *, gpt: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, serialize_txt_dictionary(rows, header, gpt=gpt))


def load_glossary_dictionary(path: Path) -> list[dict]:
    """Read glossary JSON into editable rows (one per term)."""
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"词典 JSON 格式错误（第 {exc.lineno} 行，第 {exc.colno} 列）：{path}"
        ) from exc
    if not isinstance(raw, dict):
        raise ValueError(f"词典 JSON 的最外层必须是对象：{path}")
    rows: list[dict] = []
    for source, value in raw.items():
        if isinstance(value, dict):
            row = {
                "source": str(source),
                "target": str(value.get("default", "")),
                "advanced": {key: str(item) for key, item in value.items() if key != "default"},
            }
        else:
            row = {"source": str(source), "target": str(value), "advanced": {}}
        rows.append(row)
    return rows


def serialize_glossary_dictionary(rows: list[dict]) -> str:
    """Serialize glossary rows back to JSON, preserving advanced fields."""
    validate_dictionary_rows(rows)
    payload: dict[str, object] = {}
    for row in rows:
        source = row.get("source", "").strip()
        target = row.get("target", "").strip()
        if not source or not target:
            continue
        advanced = row.get("advanced") or {}
        if advanced:
            payload[source] = {"default": target, **advanced}
        else:
            payload[source] = target
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def write_glossary_dictionary(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, serialize_glossary_dictionary(rows))
