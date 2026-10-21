#!/usr/bin/env python3
"""Native VoiceTransl/GalTransl dictionaries, glossary and term protection."""

from __future__ import annotations

import codecs
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from subtitle_axis_align import read_text
from voice_common import file_sha256, load_json

import yaml


@dataclass(frozen=True)
class NativeDictionaryEntry:
    source: str
    target: str
    note: str = ""
    replace_mode: str = "all"


def has_unreviewed_kana(text: str, allowed_terms=()) -> bool:
    """Flag remaining Japanese letters without removing or rejecting text."""
    unchecked = text
    for term in sorted(set(allowed_terms), key=len, reverse=True):
        if term:
            unchecked = unchecked.replace(term, "")
    return any("LETTER" in unicodedata.name(char, "") and
               ("HIRAGANA" in unicodedata.name(char, "") or "KATAKANA" in unicodedata.name(char, ""))
               for char in unchecked)


def process_native_escape(value: str) -> str:
    """Match VoiceTransl/GalTransl's escaped dictionary text handling."""
    try:
        return codecs.escape_decode(value.encode("utf-8"))[0].decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return value


def load_native_dictionary(path: Path, *, gpt: bool = False) -> tuple[list[NativeDictionaryEntry], int]:
    if not path.is_file():
        return [], 0
    entries: list[NativeDictionaryEntry] = []
    unsupported = 0
    conditional_keys = {
        "pre_src", "post_src", "pre_dst", "post_dst", "pre_jp", "post_jp", "pre_zh", "post_zh",
        "mono", "diag",
    }
    for raw_line in read_text(path).splitlines():
        line = raw_line.strip("\ufeff\r\n")
        if not line.strip() or line.lstrip().startswith(("//", "\\\\")):
            continue
        if gpt and "->" in line and "\t" not in line:
            source, remainder = line.split("->", 1)
            target, marker, note = remainder.partition("#")
            parts = [source.strip(), target.strip(), note.strip() if marker else ""]
        else:
            parts = line.replace("    ", "\t").split("\t")
        parts = [process_native_escape(part) for part in parts]
        if len(parts) < 2 or not parts[0]:
            unsupported += 1
            continue
        if parts[0] in conditional_keys:
            unsupported += 1
            continue
        source = parts[0]
        mode = "all"
        if source.startswith("^^"):
            source = source[2:]
            mode = "prefix"
        elif source.startswith("1^"):
            source = source[2:]
            mode = "once"
        target = parts[1]
        note = parts[2] if len(parts) > 2 else ""
        if source:
            entries.append(NativeDictionaryEntry(source, target, note, mode))
    entries.sort(key=lambda entry: len(entry.source), reverse=True)
    return entries, unsupported


def apply_native_dictionary(text: str, entries: list[NativeDictionaryEntry]) -> str:
    result = text
    for entry in entries:
        if entry.replace_mode == "prefix":
            if result.startswith(entry.source):
                result = entry.target + result[len(entry.source):]
        elif entry.replace_mode == "once":
            result = result.replace(entry.source, entry.target, 1)
        else:
            result = result.replace(entry.source, entry.target)
    return result


def native_glossary_prompt(
    entries: list[NativeDictionaryEntry],
    source_text: str,
    *,
    excluded_sources: set[str] | None = None,
) -> str:
    lines: list[str] = []
    remaining = source_text
    excluded = excluded_sources or set()
    for entry in entries:
        if entry.source in excluded:
            continue
        if entry.source not in remaining:
            continue
        line = f"{entry.source}->{entry.target}"
        if entry.note:
            line += f" #{entry.note}"
        lines.append(line)
        remaining = remaining.replace(entry.source, "")
    return "\n".join(lines)


def effective_native_prompt(base_prompt: str, mode: str, guideline_path: Path, project_config: Path) -> str:
    normalized_mode = {
        "不使用": "none",
        "追加": "append",
        "覆盖": "overwrite",
        "none": "none",
        "append": "append",
        "overwrite": "overwrite",
    }.get(str(mode).strip(), "none")
    if normalized_mode == "none":
        return base_prompt
    if not guideline_path.is_file():
        raise FileNotFoundError(f"VoiceTransl translation guideline does not exist: {guideline_path}")
    native_prompt = read_text(guideline_path).strip()
    if project_config.is_file():
        try:
            native_config = yaml.safe_load(project_config.read_text(encoding="utf-8-sig")) or {}
            common = native_config.get("common", {}) if isinstance(native_config, dict) else {}
            change_mode = str(common.get("gpt.change_prompt", "no"))
            extra = str(common.get("gpt.prompt_content", "")).strip()
            if change_mode == "AdditionalPrompt" and extra:
                native_prompt = extra + "\n" + native_prompt
            elif change_mode == "OverwritePrompt" and extra:
                native_prompt = extra
        except Exception as exc:
            print(f"[voice] warning: could not read native prompt config: {exc}", flush=True)
    if normalized_mode == "overwrite":
        return native_prompt
    return base_prompt.rstrip() + "\n\nVoiceTransl translation guideline:\n" + native_prompt


def resolve_config_path(value: object, config_path: Path) -> Path:
    text = str(value or "").strip()
    if not text:
        return Path("__not_configured__")
    path = Path(text).expanduser()
    if path.is_absolute():
        return path
    project_root = config_path.resolve().parent.parent
    candidates = (project_root / path, config_path.resolve().parent / path, Path.cwd() / path)
    return next((candidate for candidate in candidates if candidate.exists()), candidates[0])


def native_file_signature(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"path": str(path), "exists": False}
    return {
        "path": str(path),
        "exists": True,
        "size": path.stat().st_size,
        "sha256": file_sha256(path),
    }


def glossary_pairs(path: Path) -> dict[str, dict[str, str]]:
    """Load the glossary JSON. Values may be a plain string or an object with
    default/trainer_context/possessive keys; both normalize to an object."""
    raw = load_json(path, {})
    return normalize_glossary(raw)


#: Legacy flat-glossary terms that always used the possessive "的" form.
#: Kept as a migration fallback so old flat configs behave identically.
LEGACY_POSSESSIVE_TERMS = {"目白アルダン", "メジロアルダン"}


def normalize_glossary(value: dict[str, object]) -> dict[str, dict[str, str]]:
    """Accept either the structured or the legacy flat glossary representation."""
    normalized: dict[str, dict[str, str]] = {}
    for source, target in value.items():
        if isinstance(target, dict):
            normalized[str(source)] = {str(key): str(item) for key, item in target.items()}
        else:
            entry: dict[str, str] = {"default": str(target)}
            if str(source) in LEGACY_POSSESSIVE_TERMS:
                entry["possessive"] = "true"
            normalized[str(source)] = entry
    return normalized


def resolved_term_target(source: str, text: str, glossary: dict[str, object]) -> str:
    entry = glossary.get(source)
    if isinstance(entry, str):
        return entry
    trainer_context = str(entry.get("trainer_context", "") or "").strip() if isinstance(entry, dict) else ""
    if trainer_context and "トレーナー" in text:
        return trainer_context
    return str(entry.get("default", source)) if isinstance(entry, dict) else str(entry or source)


def protect_terms(text: str, glossary: dict[str, object]) -> str:
    protected = text
    for source in sorted(glossary, key=len, reverse=True):
        protected = protected.replace(source, resolved_term_target(source, text, glossary))
    return protected


def ensure_required_terms(source_text: str, translated_text: str, glossary: dict[str, object]) -> str:
    """Replace identifiable source spellings in place; never invent missing text."""
    result = translated_text.strip()
    for source in sorted(glossary, key=len, reverse=True):
        if source not in source_text:
            continue
        target = resolved_term_target(source, source_text, glossary)
        result = result.replace(source, target)
    return result


def missing_required_terms(source_text: str, translated_text: str, glossary: dict[str, object]) -> list[str]:
    missing = []
    remaining = source_text
    for source in sorted(glossary, key=len, reverse=True):
        if source in remaining:
            target = resolved_term_target(source, source_text, glossary)
            if target and target not in translated_text:
                missing.append(target)
            remaining = remaining.replace(source, "")
    return missing
