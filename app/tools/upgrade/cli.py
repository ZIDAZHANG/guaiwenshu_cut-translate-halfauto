#!/usr/bin/env python3
"""v2 command-line entry (plan 1.1, section 12.1). Development form: ``python -m upgrade.cli``; frozen form: ``roast.exe``.

W01 implements the resource/config commands. Every command prints one OperationResult JSON object on stdout,
writes logs to stderr and exits with the fixed codes: 0 ok, 2 arguments/config, 3 resources/media,
4 process failure, 5 data/identity, 6 cancelled, 7 exclusive-lock conflict.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from upgrade.contracts import (
    EXIT_ARGS, EXIT_DATA, EXIT_OK, EXIT_RESOURCE, LICENSE_STATUSES, RESOURCE_KINDS, ContractError, parse_json_text, require_record,
)


def _result(operation: str, *, ok: bool, ids: dict | None = None, artifact_refs: list | None = None,
            error: ContractError | None = None, warnings: list | None = None) -> dict:
    result = {"ok": ok, "operation": operation, "ids": ids or {}, "artifact_refs": artifact_refs or [],
              "error": error.to_dict() if error else None, "warnings": warnings or []}
    return require_record("OperationResult", result)


def _emit(result: dict) -> None:
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    sys.stdout.flush()


def _log(message: str) -> None:
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


# --------------------------------------------------------------------------- commands

def cmd_config_init(args: argparse.Namespace) -> int:
    from upgrade import config as cfg
    from upgrade.resource_catalog import save_declarations
    from upgrade.storage import ensure_ascii_root, init_data_root
    report = init_data_root(args.data_root)
    app_config = cfg.default_app_config(report["data_root"], ascii_temp_root=args.ascii_temp_root)
    ensure_ascii_root(app_config["ascii_temp_root"])
    config_path = Path(args.config)
    if config_path.exists() and not args.force:
        raise ContractError("OUTPUT_EXISTS", "config file already exists; pass --force to replace it", details={"path": str(config_path)})
    cfg.save_app_config(config_path, app_config)
    resources_path = cfg.resolve_in_data_root(app_config, app_config["resources_file"])
    if not resources_path.exists():
        save_declarations(resources_path, {})
    _emit(_result("config.init", ok=True, ids={"config": str(config_path), "data_root": report["data_root"], "resources_file": str(resources_path)}))
    return EXIT_OK


def cmd_config_migrate(args: argparse.Namespace) -> int:
    from upgrade import config as cfg
    from upgrade.storage import atomic_write_text, render_json
    legacy_path = Path(args.legacy)
    try:
        legacy = parse_json_text(legacy_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise ContractError("INPUT_MISSING", f"cannot read legacy config: {exc}", details={"path": str(legacy_path)}) from exc
    migrated = cfg.migrate_legacy_config(legacy, data_root=args.data_root, ascii_temp_root=args.ascii_temp_root)
    config_path = Path(args.config)
    if config_path.exists() and not args.force:
        raise ContractError("OUTPUT_EXISTS", "config file already exists; pass --force to replace it", details={"path": str(config_path)})
    cfg.save_app_config(config_path, migrated["app_config"])
    engine_path = Path(args.engine_settings) if args.engine_settings else config_path.parent / "engine.json"
    cfg.save_engine_settings(engine_path, migrated["engine_settings"])
    report = {key: migrated[key] for key in ("mapped", "unsupported_commands", "suggested_resources", "legacy_extra", "notes")}
    report_path = Path(args.report) if args.report else config_path.parent / "migration-report.json"
    atomic_write_text(report_path, render_json(report))
    warnings = [{"code": "CONFIG_UNSUPPORTED_COMMAND", "key": item["key"]} for item in migrated["unsupported_commands"]]
    _emit(_result("config.migrate", ok=True, ids={"config": str(config_path), "engine_settings": str(engine_path), "report": str(report_path)}, warnings=warnings))
    return EXIT_OK


def _load_config(path: str) -> dict:
    from upgrade.config import load_app_config
    return load_app_config(path)


def cmd_resources_inspect(args: argparse.Namespace) -> int:
    from upgrade.resource_catalog import inspect_resources
    from upgrade.storage import atomic_write_text, render_json
    config = _load_config(args.config)
    report = inspect_resources(config, probe=not args.no_probe)
    report_path = Path(args.report)
    atomic_write_text(report_path, render_json(report))
    _log(f"inspected {len(report['manifests'])} resource(s), {len(report['problems'])} problem(s) -> {report_path}")
    ids = {"report": str(report_path), "resources_file": report["resources_file"], "resource_ids": sorted(report["manifests"])}
    if report["problems"]:
        error = ContractError("RESOURCE_MISSING", "one or more declared resources are missing or changed; see report",
                              details={"problems": report["problems"]})
        _emit(_result("resources.inspect", ok=False, ids=ids, error=error))
        return EXIT_RESOURCE
    _emit(_result("resources.inspect", ok=True, ids=ids, warnings=[report["hash_cache_note"]]))
    return EXIT_OK


def cmd_resources_register(args: argparse.Namespace) -> int:
    from upgrade.resource_catalog import declare_resource, register_resource
    config = _load_config(args.config)
    declaration = declare_resource(args.kind, args.path, files=args.file, upstream_url=args.upstream_url,
                                   upstream_revision=args.upstream_revision, license_status=args.license_status,
                                   version_args=args.version_arg if args.version_arg is not None else None)
    saved = register_resource(config, args.id, declaration)
    _emit(_result("resources.register", ok=True, ids={"resource_id": args.id, "content_sha256": saved["expected_content_sha256"]}))
    return EXIT_OK


def cmd_resources_rebind(args: argparse.Namespace) -> int:
    from upgrade.resource_catalog import rebind_resource
    config = _load_config(args.config)
    result = rebind_resource(config, args.id, args.path)
    _emit(_result("resources.rebind", ok=True, ids=result))
    return EXIT_OK


# --------------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="roast", description="字幕烤肉助手 v2 命令行（计划 1.1 第 12.1 节）。每条命令在 stdout 输出一份 OperationResult JSON。")
    sub = parser.add_subparsers(dest="group", required=True)

    config_parser = sub.add_parser("config", help="配置文件与数据根")
    config_sub = config_parser.add_subparsers(dest="command", required=True)
    init = config_sub.add_parser("init", help="创建 DATA_ROOT 布局并写出默认 AppConfig（首次显式选择数据根）")
    init.add_argument("--data-root", required=True)
    init.add_argument("--config", required=True, help="AppConfig JSON 输出路径")
    init.add_argument("--ascii-temp-root", default=None)
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_config_init)
    migrate = config_sub.add_parser("migrate", help="把旧 roast_pipeline.local.json 迁移为 v2 AppConfig + engine settings；旧模板只记录不执行")
    migrate.add_argument("--legacy", required=True)
    migrate.add_argument("--data-root", required=True)
    migrate.add_argument("--config", required=True)
    migrate.add_argument("--engine-settings", default=None)
    migrate.add_argument("--report", default=None)
    migrate.add_argument("--ascii-temp-root", default=None)
    migrate.add_argument("--force", action="store_true")
    migrate.set_defaults(func=cmd_config_migrate)

    resources_parser = sub.add_parser("resources", help="外部工具/模型资源登记与检查")
    resources_sub = resources_parser.add_subparsers(dest="command", required=True)
    inspect = resources_sub.add_parser("inspect", help="检查 resources.local.json 中的全部资源并写报告；只做版本/帮助探针")
    inspect.add_argument("--config", required=True)
    inspect.add_argument("--report", required=True)
    inspect.add_argument("--no-probe", action="store_true", help="不运行版本/帮助探针，只做存在性与哈希")
    inspect.set_defaults(func=cmd_resources_inspect)
    register = resources_sub.add_parser("register", help="登记一个资源的逻辑 ID 与本机路径，并冻结其内容身份")
    register.add_argument("--config", required=True)
    register.add_argument("--id", required=True)
    register.add_argument("--kind", required=True, choices=RESOURCE_KINDS)
    register.add_argument("--path", required=True)
    register.add_argument("--file", action="append", default=None, help="目录型资源的相对文件路径，可重复")
    register.add_argument("--upstream-url", default=None)
    register.add_argument("--upstream-revision", default=None)
    register.add_argument("--license-status", default="unresolved", choices=LICENSE_STATUSES)
    register.add_argument("--version-arg", action="append", default=None, help="版本/帮助探针参数，可重复；不传用按类型默认")
    register.set_defaults(func=cmd_resources_register)
    rebind = resources_sub.add_parser("rebind", help="资源移动后只更新本机路径；内容身份必须一致")
    rebind.add_argument("--config", required=True)
    rebind.add_argument("--id", required=True)
    rebind.add_argument("--path", required=True)
    rebind.set_defaults(func=cmd_resources_rebind)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_ARGS if exc.code not in (0, None) else EXIT_OK
    operation = f"{args.group}.{args.command}"
    try:
        return int(args.func(args))
    except ContractError as exc:
        _log(str(exc))
        _emit(_result(operation, ok=False, error=exc))
        return exc.exit_code
    except KeyboardInterrupt:
        _emit(_result(operation, ok=False, error=ContractError("PROCESS_CANCELLED", "cancelled by user")))
        return 6
    except OSError as exc:
        error = ContractError("PROCESS_FAILED", f"{type(exc).__name__}: {exc}")
        _emit(_result(operation, ok=False, error=error))
        return EXIT_DATA


if __name__ == "__main__":
    raise SystemExit(main())
