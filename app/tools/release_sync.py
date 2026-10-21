#!/usr/bin/env python3
"""Plan or apply a narrow, reversible source sync into the production project."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Callable, Iterable


COPY_FILES = (
    "config/roast_pipeline.example.json",
    "tools/artifact_validation.py",
    "tools/capture_app_window.py",
    "tools/diagnostics.py",
    "tools/forced_alignment.py",
    "tools/dictionary_editor.py",
    "tools/jianying_bridge.py",
    "tools/jianying_ui_import.py",
    "tools/jianying_uia_probe.py",
    "tools/magia_timeline_adapter.py",
    "tools/magia_utf8_runtime.py",
    "tools/one_click_roast.py",
    "tools/roast_inbox.py",
    "tools/roast_pipeline.py",
    "tools/sakura_client.py",
    "tools/subtitle_axis_align.py",
    "tools/subtitle_segments.py",
    "tools/task_state.py",
    "tools/tool_discovery.py",
    "tools/translation_checkpoint.py",
    "tools/voice_common.py",
    "tools/voice_dictionary.py",
    "tools/voicetransl_adapter.py",
)
USER_SEED_FILES = (
    "config/translation_style_prompt.txt",
    "config/translation_glossary.json",
    "config/dict_pre.txt",
    "config/dict_gpt.txt",
    "config/dict_after.txt",
)
LOCAL_CONFIG = "config/roast_pipeline.local.json"
EXCLUDED_STAGE_FILES = (
    "config/roast_pipeline.regression.local.json",
    "build_candidate.spec",
    "candidate_release",
    "output",
    "work",
)
PORTABLE_CONFIG_KEYS = (
    "commands.axis",
    "commands.recognize_jp",
    "commands.translate_zh",
    "resources.translation_glossary",
    "resources.translation_prompt",
)


def safe_relative(value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError(f"Unsafe release path: {value}")
    return relative


def safe_join(root: Path, relative: str) -> Path:
    root = root.resolve()
    result = (root / safe_relative(relative)).resolve()
    if not result.is_relative_to(root):
        raise ValueError(f"Path escapes release root: {relative}")
    return result


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def flatten(data: dict, prefix: str = "") -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            result.update(flatten(value, path))
        else:
            result[path] = value
    return result


def merge_missing(current: dict, defaults: dict) -> list[str]:
    added: list[str] = []

    def merge(target: dict, source: dict, prefix: str = "") -> None:
        for key, value in source.items():
            path = f"{prefix}.{key}" if prefix else key
            if key not in target:
                target[key] = value
                added.extend(flatten({key: value}, prefix).keys() if isinstance(value, dict) else [path])
            elif isinstance(value, dict) and isinstance(target[key], dict):
                merge(target[key], value, path)

    merge(current, defaults)
    return sorted(added)


def nested_value(data: dict, dotted_key: str) -> object:
    value: object = data
    for part in dotted_key.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def set_nested_value(data: dict, dotted_key: str, value: object) -> None:
    parts = dotted_key.split(".")
    target = data
    for part in parts[:-1]:
        child = target.get(part)
        if not isinstance(child, dict):
            raise ValueError(f"Cannot migrate non-object config path: {dotted_key}")
        target = child
    target[parts[-1]] = value


def _normalized_config_text(value: str) -> str:
    return value.replace("\\", "/").casefold()


def _matches_legacy_project_default(current: str, default: str, production_root: Path) -> bool:
    root_forms = (str(production_root.resolve()), production_root.resolve().as_posix())
    for root in root_forms:
        tokenized = re.sub(re.escape(root), "{root}", current, flags=re.IGNORECASE)
        relative = re.sub(
            re.escape(root) + r"[\\/]*",
            "",
            current,
            flags=re.IGNORECASE,
        )
        if _normalized_config_text(tokenized) == _normalized_config_text(default):
            return True
        if _normalized_config_text(relative) == _normalized_config_text(default):
            return True
    return False


def migrate_legacy_project_values(current: dict, defaults: dict, production_root: Path) -> list[str]:
    """Migrate only known install-root defaults; preserve every custom value."""
    migrated: list[str] = []
    for dotted_key in PORTABLE_CONFIG_KEYS:
        existing = nested_value(current, dotted_key)
        default = nested_value(defaults, dotted_key)
        if not isinstance(existing, str) or not isinstance(default, str) or existing == default:
            continue
        if _matches_legacy_project_default(existing, default, production_root):
            set_nested_value(current, dotted_key, default)
            migrated.append(dotted_key)
    return sorted(migrated)


def relocate_project_paths(value: object, stage_root: Path, production_root: Path) -> object:
    """Rewrite absolute stage-root references before seeding production config."""
    if isinstance(value, dict):
        return {
            key: relocate_project_paths(item, stage_root, production_root)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [relocate_project_paths(item, stage_root, production_root) for item in value]
    if not isinstance(value, str):
        return value
    replacements = (
        (str(stage_root.resolve()), str(production_root.resolve())),
        (stage_root.resolve().as_posix(), production_root.resolve().as_posix()),
    )
    relocated = value
    for old, new in replacements:
        relocated = re.sub(re.escape(old), lambda _match, replacement=new: replacement, relocated, flags=re.IGNORECASE)
    return relocated


def atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.sync.tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.sync.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_roots(stage_root: Path, production_root: Path) -> tuple[Path, Path]:
    stage_root = stage_root.resolve()
    production_root = production_root.resolve()
    if not stage_root.is_dir():
        raise FileNotFoundError(f"Stage root does not exist: {stage_root}")
    if not production_root.is_dir():
        raise FileNotFoundError(f"Production root does not exist: {production_root}")
    if stage_root == production_root:
        raise ValueError("Stage and production roots must be different")
    return stage_root, production_root


def build_plan(
    stage_root: Path,
    production_root: Path,
    copy_files: Iterable[str] = COPY_FILES,
    seed_files: Iterable[str] = USER_SEED_FILES,
) -> dict:
    stage_root, production_root = validate_roots(stage_root, production_root)
    actions: list[dict] = []
    planned_files = [(relative, False) for relative in copy_files]
    planned_files.extend((relative, True) for relative in seed_files)
    for relative, seed_only in planned_files:
        source = safe_join(stage_root, relative)
        destination = safe_join(production_root, relative)
        if not source.is_file():
            actions.append({"path": relative, "action": "MISSING_SOURCE", "policy": "seed_if_missing" if seed_only else "replace"})
            continue
        source_hash = sha256(source)
        if not destination.is_file():
            action = "NEW"
            destination_hash = None
        else:
            destination_hash = sha256(destination)
            if source_hash == destination_hash:
                action = "IDENTICAL"
            else:
                action = "PRESERVE_USER_FILE" if seed_only else "CHANGED"
        actions.append({
            "path": relative,
            "action": action,
            "policy": "seed_if_missing" if seed_only else "replace",
            "source_sha256": source_hash,
            "destination_sha256": destination_hash,
            "source_bytes": source.stat().st_size,
            "destination_bytes": destination.stat().st_size if destination.is_file() else None,
        })

    stage_config_path = safe_join(stage_root, LOCAL_CONFIG)
    production_config_path = safe_join(production_root, LOCAL_CONFIG)
    stage_config = relocate_project_paths(read_json(stage_config_path), stage_root, production_root)
    if not isinstance(stage_config, dict):
        raise ValueError(f"Stage local config must be an object: {stage_config_path}")
    production_config = read_json(production_config_path)
    config_migrations = migrate_legacy_project_values(
        production_config,
        stage_config,
        production_root,
    )
    stage_flat = flatten(stage_config)
    production_flat = flatten(production_config)
    config_missing = sorted(set(stage_flat) - set(production_flat))
    config_value_differences = sorted(
        key for key in set(stage_flat) & set(production_flat) if stage_flat[key] != production_flat[key]
    )
    return {
        "schema_version": 2,
        "mode": "PLAN",
        "stage_root": str(stage_root),
        "production_root": str(production_root),
        "copy_actions": actions,
        "local_config": {
            "strategy": "migrate_known_project_defaults_add_missing_preserve_custom_values",
            "missing_keys": config_missing,
            "portable_migration_keys": config_migrations,
            "existing_value_difference_keys": config_value_differences,
        },
        "excluded": list(EXCLUDED_STAGE_FILES),
    }


def apply_sync(
    stage_root: Path,
    production_root: Path,
    *,
    confirmed: bool,
    copy_files: Iterable[str] = COPY_FILES,
    seed_files: Iterable[str] = USER_SEED_FILES,
    copier: Callable[[Path, Path], None] = atomic_copy,
) -> dict:
    if not confirmed:
        raise PermissionError("Production sync requires the explicit confirmation flag")
    copy_files = tuple(copy_files)
    seed_files = tuple(seed_files)
    plan = build_plan(stage_root, production_root, copy_files, seed_files)
    missing = [item["path"] for item in plan["copy_actions"] if item["action"] == "MISSING_SOURCE"]
    if missing:
        raise FileNotFoundError(f"Release sources are missing: {missing}")

    stage_root, production_root = validate_roots(stage_root, production_root)
    actions = {item["path"]: item["action"] for item in plan["copy_actions"]}
    copy_targets = [relative for relative in copy_files if actions[relative] != "IDENTICAL"]
    seed_targets = [relative for relative in seed_files if actions[relative] == "NEW"]

    stage_config = relocate_project_paths(
        read_json(safe_join(stage_root, LOCAL_CONFIG)),
        stage_root,
        production_root,
    )
    if not isinstance(stage_config, dict):
        raise ValueError("Stage local config must be an object")
    production_config = read_json(safe_join(production_root, LOCAL_CONFIG))
    config_migrated = migrate_legacy_project_values(
        production_config,
        stage_config,
        production_root,
    )
    config_added = merge_missing(production_config, stage_config)
    config_changed = bool(config_added or config_migrated)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_root = production_root / "release_backups" / f"source_sync_{timestamp}_{uuid.uuid4().hex[:8]}"
    tracked = copy_targets + seed_targets + ([LOCAL_CONFIG] if config_changed else [])
    existed: dict[str, bool] = {}
    for relative in tracked:
        destination = safe_join(production_root, relative)
        existed[relative] = destination.is_file()
        if destination.is_file():
            backup = safe_join(backup_root, relative)
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, backup)

    written: list[str] = []
    verification: dict | None = None
    try:
        for relative in copy_targets + seed_targets:
            copier(safe_join(stage_root, relative), safe_join(production_root, relative))
            written.append(relative)

        if config_changed:
            atomic_write_json(safe_join(production_root, LOCAL_CONFIG), production_config)
            written.append(LOCAL_CONFIG)

        verification = build_plan(stage_root, production_root, copy_files, seed_files)
        unresolved = [
            item["path"]
            for item in verification["copy_actions"]
            if item["action"] not in {"IDENTICAL", "PRESERVE_USER_FILE"}
        ]
        remaining_config_keys = verification["local_config"]["missing_keys"]
        remaining_migrations = verification["local_config"]["portable_migration_keys"]
        if unresolved or remaining_config_keys or remaining_migrations:
            raise RuntimeError(
                "Post-sync verification failed: "
                f"files={unresolved}, config_keys={remaining_config_keys}, "
                f"portable_migrations={remaining_migrations}"
            )
    except BaseException:
        for relative in reversed(written):
            destination = safe_join(production_root, relative)
            if existed[relative]:
                atomic_copy(safe_join(backup_root, relative), destination)
            else:
                destination.unlink(missing_ok=True)
        atomic_write_json(backup_root / "sync_result.json", {
            "status": "ROLLED_BACK",
            "written_before_failure": written,
            "plan": plan,
        })
        raise
    if verification is None:
        raise RuntimeError("Post-sync verification did not run")
    result = {
        "schema_version": 2,
        "mode": "APPLY",
        "status": "DONE",
        "backup_root": str(backup_root),
        "copied_files": copy_targets + seed_targets,
        "preserved_user_files": [
            relative for relative in seed_files if actions[relative] == "PRESERVE_USER_FILE"
        ],
        "local_config_added_keys": config_added,
        "local_config_migrated_keys": config_migrated,
        "verification": verification,
    }
    atomic_write_json(backup_root / "sync_result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan or apply the controlled kilo_stage source sync.")
    parser.add_argument("--stage-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--production-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="Optional JSON report path (normally inside kilo_stage).")
    parser.add_argument("--apply", action="store_true", help="Write the planned source/config changes.")
    parser.add_argument(
        "--confirm-production-write",
        action="store_true",
        help="Second explicit gate required together with --apply.",
    )
    args = parser.parse_args()
    if args.apply:
        result = apply_sync(
            args.stage_root,
            args.production_root,
            confirmed=args.confirm_production_write,
        )
    else:
        result = build_plan(args.stage_root, args.production_root)
    if args.output:
        atomic_write_json(args.output.resolve(), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
