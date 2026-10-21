#!/usr/bin/env python3
"""Regression tests for release sync planning, config merge, and rollback."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import release_sync


ROOT = (Path(__file__).resolve().parents[1] / "app")


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    unmanaged_tools = {
        f"tools/{path.name}"
        for path in (ROOT / "tools").glob("*.py")
        if path.name not in {"release_sync.py", "release_sync_test.py"}
    } - set(release_sync.COPY_FILES)
    if unmanaged_tools:
        raise AssertionError(f"Runtime/test modules missing from release manifest: {sorted(unmanaged_tools)}")
    if "config/translation_style_prompt.txt" not in release_sync.USER_SEED_FILES:
        raise AssertionError("The user-editable translation prompt is not protected by seed-if-missing policy")

    output_root = ROOT / "output"
    output_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="release_sync_", dir=output_root) as temp_dir:
        root = Path(temp_dir)
        stage = root / "stage"
        production = root / "production"
        stage.mkdir()
        production.mkdir()
        files = ("tools/changed.py", "tools/new.py")
        seed_files = ("config/translation_glossary.json", "config/dict_pre.txt")
        (stage / "tools").mkdir()
        (production / "tools").mkdir()
        (stage / "tools" / "changed.py").write_text("new code\n", encoding="utf-8")
        (stage / "tools" / "new.py").write_text("new file\n", encoding="utf-8")
        (production / "tools" / "changed.py").write_text("old code\n", encoding="utf-8")
        (production / "tools" / "keep.py").write_text("production only\n", encoding="utf-8")
        (stage / "config").mkdir()
        (production / "config").mkdir()
        (stage / "config" / "translation_glossary.json").write_text('{"template": "模板"}\n', encoding="utf-8")
        (production / "config" / "translation_glossary.json").write_text('{"user": "自定义"}\n', encoding="utf-8")
        (stage / "config" / "dict_pre.txt").write_text("原文\t模板\n", encoding="utf-8")
        write_json(stage / release_sync.LOCAL_CONFIG, {
            "existing": {"value": "stage default", "new_key": 42},
            "commands": {
                "axis": '"{python}" "{root}/tools/magia_timeline_adapter.py"',
                "recognize_jp": '"{python}" "{root}/tools/voicetransl_adapter.py" recognize',
                "translate_zh": '"{python}" "{root}/tools/voicetransl_adapter.py" translate',
            },
            "resources": {
                "translation_glossary": "config/translation_glossary.json",
                "translation_prompt": "config/translation_style_prompt.txt",
            },
            "new_section": {
                "enabled": True,
                "dictionary": str(stage / "config" / "dict_pre.txt"),
            },
        })
        write_json(production / release_sync.LOCAL_CONFIG, {
            "existing": {"value": "user choice"},
            "commands": {
                "axis": f'"{{python}}" "{production.as_posix()}/tools/magia_timeline_adapter.py"',
                "recognize_jp": f'"{{python}}" "{production.as_posix()}/tools/voicetransl_adapter.py" recognize',
                "translate_zh": f'"{{python}}" "{production.as_posix()}/tools/voicetransl_adapter.py" translate',
            },
            "resources": {
                "translation_glossary": f"{production.as_posix()}/config/translation_glossary.json",
                "translation_prompt": f"{production.as_posix()}/config/translation_style_prompt.txt",
            },
        })

        plan = release_sync.build_plan(stage, production, files, seed_files)
        actions = {item["path"]: item["action"] for item in plan["copy_actions"]}
        expected_actions = {
            "tools/changed.py": "CHANGED",
            "tools/new.py": "NEW",
            "config/translation_glossary.json": "PRESERVE_USER_FILE",
            "config/dict_pre.txt": "NEW",
        }
        if actions != expected_actions:
            raise AssertionError(f"Unexpected release plan: {plan}")
        if plan["local_config"]["missing_keys"] != [
            "existing.new_key",
            "new_section.dictionary",
            "new_section.enabled",
        ]:
            raise AssertionError(f"Unexpected config merge plan: {plan['local_config']}")
        if plan["local_config"]["portable_migration_keys"] != list(release_sync.PORTABLE_CONFIG_KEYS):
            raise AssertionError(f"Legacy project defaults were not planned for migration: {plan['local_config']}")
        custom_config = {
            "commands": {"axis": '"{python}" "C:/custom/tools/magia_timeline_adapter.py"'},
        }
        if release_sync.migrate_legacy_project_values(
            custom_config,
            release_sync.read_json(stage / release_sync.LOCAL_CONFIG),
            production,
        ):
            raise AssertionError("A custom command was incorrectly migrated")
        if "C:/custom/" not in custom_config["commands"]["axis"]:
            raise AssertionError("A custom command was overwritten")
        try:
            release_sync.apply_sync(
                stage,
                production,
                confirmed=False,
                copy_files=files,
                seed_files=seed_files,
            )
        except PermissionError:
            pass
        else:
            raise AssertionError("Release sync wrote without explicit confirmation")

        result = release_sync.apply_sync(
            stage,
            production,
            confirmed=True,
            copy_files=files,
            seed_files=seed_files,
        )
        if result["status"] != "DONE":
            raise AssertionError(result)
        if (production / "tools" / "changed.py").read_text(encoding="utf-8") != "new code\n":
            raise AssertionError("Changed source was not synchronized")
        if (production / "tools" / "new.py").read_text(encoding="utf-8") != "new file\n":
            raise AssertionError("New source was not synchronized")
        if (production / "tools" / "keep.py").read_text(encoding="utf-8") != "production only\n":
            raise AssertionError("Production-only file was modified")
        if (production / "config" / "translation_glossary.json").read_text(encoding="utf-8") != '{"user": "自定义"}\n':
            raise AssertionError("Existing user glossary was overwritten")
        if (production / "config" / "dict_pre.txt").read_text(encoding="utf-8") != "原文\t模板\n":
            raise AssertionError("Missing dictionary template was not seeded")
        if result["preserved_user_files"] != ["config/translation_glossary.json"]:
            raise AssertionError(f"Preserved files were not reported: {result}")
        merged = release_sync.read_json(production / release_sync.LOCAL_CONFIG)
        if merged["existing"]["value"] != "user choice":
            raise AssertionError("Existing local config value was overwritten")
        if merged["existing"]["new_key"] != 42 or not merged["new_section"]["enabled"]:
            raise AssertionError("Missing local config defaults were not added")
        stage_defaults = release_sync.read_json(stage / release_sync.LOCAL_CONFIG)
        for dotted_key in release_sync.PORTABLE_CONFIG_KEYS:
            if release_sync.nested_value(merged, dotted_key) != release_sync.nested_value(stage_defaults, dotted_key):
                raise AssertionError(f"Legacy project path was not migrated: {dotted_key}")
        if result["local_config_migrated_keys"] != list(release_sync.PORTABLE_CONFIG_KEYS):
            raise AssertionError(f"Migrated config keys were not reported: {result}")
        expected_dictionary = str(production / "config" / "dict_pre.txt")
        if merged["new_section"]["dictionary"] != expected_dictionary:
            raise AssertionError(f"Stage path leaked into production config: {merged}")
        if not (Path(result["backup_root"]) / "sync_result.json").is_file():
            raise AssertionError("Release backup manifest was not created")

        rollback_stage = root / "rollback_stage"
        rollback_production = root / "rollback_production"
        rollback_stage.mkdir()
        rollback_production.mkdir()
        (rollback_stage / "tools").mkdir()
        (rollback_production / "tools").mkdir()
        rollback_files = ("tools/first.py", "tools/second.py")
        for name in ("first.py", "second.py"):
            (rollback_stage / "tools" / name).write_text(f"new {name}\n", encoding="utf-8")
            (rollback_production / "tools" / name).write_text(f"old {name}\n", encoding="utf-8")
        write_json(rollback_stage / release_sync.LOCAL_CONFIG, {"new": True})
        write_json(rollback_production / release_sync.LOCAL_CONFIG, {"user": True})
        calls = 0

        def fail_second_copy(source: Path, destination: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated copy failure")
            release_sync.atomic_copy(source, destination)

        try:
            release_sync.apply_sync(
                rollback_stage,
                rollback_production,
                confirmed=True,
                copy_files=rollback_files,
                seed_files=(),
                copier=fail_second_copy,
            )
        except RuntimeError as exc:
            if "simulated copy failure" not in str(exc):
                raise
        else:
            raise AssertionError("Simulated release failure was ignored")
        for name in ("first.py", "second.py"):
            actual = (rollback_production / "tools" / name).read_text(encoding="utf-8")
            if actual != f"old {name}\n":
                raise AssertionError(f"Rollback did not restore {name}: {actual!r}")
        if release_sync.read_json(rollback_production / release_sync.LOCAL_CONFIG) != {"user": True}:
            raise AssertionError("Rollback changed the production config")

        try:
            release_sync.safe_relative("../escape.py")
        except ValueError:
            pass
        else:
            raise AssertionError("Path traversal was accepted")

    print("Release sync tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
