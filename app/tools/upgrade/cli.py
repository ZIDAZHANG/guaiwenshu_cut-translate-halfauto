#!/usr/bin/env python3
"""v2 command-line entry (plan 1.1, section 12.1). Development form: ``python -m upgrade.cli``; frozen form: ``roast.exe``.

Every command prints one OperationResult JSON object on stdout (``worker`` is the internal exception: it runs a bundled module),
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
    config_path = Path(args.config) if args.config else Path(report["data_root"]) / cfg.APP_CONFIG_NAME
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


# --------------------------------------------------------------------------- queue (W02)

def _data_root_config(data_root: str) -> dict:
    from upgrade.config import APP_CONFIG_NAME, load_app_config
    return load_app_config(Path(data_root) / APP_CONFIG_NAME)


def _engine_settings_for(config: dict) -> dict:
    from upgrade.config import ENGINE_SETTINGS_NAME, load_engine_settings, resolve_in_data_root
    return load_engine_settings(resolve_in_data_root(config, ENGINE_SETTINGS_NAME))


def _resource_refs_for(config: dict) -> list[dict]:
    from upgrade.resource_catalog import inspect_resources, resolve_resource
    report = inspect_resources(config, probe=False)
    return [resolve_resource(resource_id, report)["resource_ref"] for resource_id in sorted(report["manifests"])
            if report["manifests"][resource_id].get("extensions", {}).get("identity") != "changed"]


def _parse_stream(value: str | None) -> int | None:
    if value is None or value == "none":
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ContractError("NEEDS_SELECTION", f"stream index must be an integer or 'none', got {value!r}") from exc


def _dictionary_for_request(config: dict, engine: dict, *, accept: bool) -> tuple[dict, list]:
    """Plan 8.5 pre-check: freeze the dictionary revision for this request and settle the acceptance of unsupported entries."""
    from upgrade.dictionary import prepare_for_request
    prepared = prepare_for_request(config["data_root"], engine, accept_warnings=accept)
    warnings = [{"code": "DICTIONARY_UNSUPPORTED_ACCEPTED", "count": len(prepared["unsupported"]), "entries": prepared["unsupported"]}] if prepared["unsupported"] else []
    warnings += [{"code": "DICTIONARY_CONFLICT", **item} for item in prepared["warnings"]]
    return prepared, warnings


def cmd_submit(args: argparse.Namespace) -> int:
    from upgrade.runner import build_job_request, project_dir, submit
    config = _load_config(args.config)
    engine = _engine_settings_for(config)
    source_media_id = None
    if args.mode == "supplied_source":
        if not (args.project and args.source_revision):
            raise ContractError("NEEDS_SELECTION", "supplied_source needs --project ID and --source-revision ID")
    elif args.source_revision:
        raise ContractError("NEEDS_SELECTION", "--source-revision is only valid with --mode supplied_source")
    if args.timing_mode and args.mode != "combined":
        raise ContractError("NEEDS_SELECTION", "--timing-mode only applies to --mode combined (speech_only=speech, visual_only=screen are fixed)")
    video_stream, audio_stream = _parse_stream(args.video_stream), _parse_stream(args.audio_stream)
    prepared, warnings = _dictionary_for_request(config, engine, accept=args.accept_dictionary_warnings)
    if args.video:
        video = Path(args.video)
        if not video.is_file():
            raise ContractError("INPUT_MISSING", f"video file not found: {video}", details={"path": str(video)})
        input_kind, input_value, default_name = "local_video", str(video.resolve()), video.stem
        from upgrade.stages import preflight_streams
        checked = preflight_streams(config, {"config_snapshot": {"engine_settings": engine}, "resource_refs": []}, video.resolve(), mode=args.mode,
                                    video_stream=video_stream, audio_stream=audio_stream, audio_none=args.audio_stream == "none")
        video_stream, audio_stream = checked["video_stream"], checked["audio_stream"]
        if checked["warning"]:
            warnings.append(checked["warning"])
    elif args.url:
        input_kind, input_value, default_name = "url", args.url, args.url
    else:
        if args.mode != "supplied_source":
            raise ContractError("NEEDS_SELECTION", "give --video FILE or --url URL")
        from upgrade.source import load_source_revision
        loaded = load_source_revision(project_dir(config["data_root"], args.project), args.source_revision)
        source_media_id = loaded["record"]["media_id"]             # the media the source revision is bound to (plan 10.1: SOURCE reads the revision)
        input_kind, input_value, default_name = "local_video", f"supplied-source:{args.source_revision}", args.project
    request = build_job_request(
        config, engine, input_kind=input_kind, input_value=input_value, recognition_mode=args.mode, timing_mode=args.timing_mode,
        project_id=args.project, video_stream_index=video_stream, audio_stream_index=audio_stream,
        source_revision_id=args.source_revision, source_media_id=source_media_id, dictionary_revision_id=prepared["revision_id"],
        resource_refs=_resource_refs_for(config), accepted_dictionary_warnings=prepared["accepted_ids"],
    )
    if args.audio_stream == "none":
        request["extensions"]["audio_stream"] = "none"
    if args.copy_original:
        request["extensions"]["copy_original"] = True
    envelope = submit(config["data_root"], request, display_name=args.display_name or default_name)
    _emit(_result("submit", ok=True, ids={"project_id": request["project_id"], "request_id": request["request_id"], "status": envelope["status"],
                                           "dictionary_revision_id": prepared["revision_id"],
                                           "duplicate": envelope["request"]["request_id"] == request["request_id"] and envelope["created_at"] != request["created_at"]},
                  warnings=warnings))
    return EXIT_OK


def cmd_queue_run(args: argparse.Namespace) -> int:
    from upgrade.runner import QueueRunner
    from upgrade.stage_registry import default_registry
    config = _data_root_config(args.data_root)
    runner = QueueRunner(config["data_root"], config, _engine_settings_for(config), default_registry(), event_sink=lambda e: _log(json.dumps(e, ensure_ascii=False)))
    summary = runner.run_queue(continue_on_error=None if args.continue_on_error is None else args.continue_on_error)
    _emit(_result("queue.run", ok=True, ids=summary))
    return EXIT_OK


def cmd_queue_cancel(args: argparse.Namespace) -> int:
    from upgrade.runner import cancel
    config = _data_root_config(args.data_root)
    envelope = cancel(config["data_root"], args.request)
    _emit(_result("queue.cancel", ok=True, ids={"request_id": args.request, "status": envelope["status"]}))
    return EXIT_OK


def cmd_resume(args: argparse.Namespace) -> int:
    from upgrade.runner import resume
    config = _data_root_config(args.data_root)
    envelope = resume(config["data_root"], args.run)
    _emit(_result("resume", ok=True, ids={"run_id": args.run, "request_id": envelope["request"]["request_id"], "status": envelope["status"]}))
    return EXIT_OK


def cmd_inbox_intake(args: argparse.Namespace) -> int:
    from upgrade.inbox import intake
    from upgrade.runner import build_job_request
    config = _load_config(args.config)
    engine = _engine_settings_for(config)
    refs = _resource_refs_for(config)
    prepared, _ = _dictionary_for_request(config, engine, accept=False)   # legacy inbox files carry no acceptance; unaccepted entries stop the intake

    def factory(parsed: dict, source: Path) -> dict:
        url = str(parsed.get("url", "")).strip()
        video = str(parsed.get("video_file", parsed.get("video", ""))).strip()
        if not url and not video:
            raise ContractError("INPUT_MISSING", "legacy request has neither url nor video_file", details={"source": str(source)})
        mode = str(parsed.get("mode", "")).strip() or config["defaults"]["recognition_mode"]
        timing = str(parsed.get("timing_mode", "")).strip() or None
        return build_job_request(config, engine, input_kind="url" if url else "local_video", input_value=url or video, recognition_mode=mode,
                                 timing_mode=timing if mode == "combined" else None, dictionary_revision_id=prepared["revision_id"], resource_refs=refs,
                                 accepted_dictionary_warnings=prepared["accepted_ids"])

    rows = intake(config["data_root"], args.inbox, factory, event_sink=lambda e: _log(json.dumps(e, ensure_ascii=False)))
    failed = [row for row in rows if row["action"] == "failed"]
    _emit(_result("inbox.intake", ok=not failed, ids={"rows": rows},
                  error=ContractError("SCHEMA_INVALID", f"{len(failed)} inbox file(s) could not be ingested", details={"failed": failed}) if failed else None))
    return EXIT_OK if not failed else EXIT_DATA


# --------------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="roast", description="字幕烤肉助手 v2 命令行（计划 1.1 第 12.1 节）。每条命令在 stdout 输出一份 OperationResult JSON。")
    sub = parser.add_subparsers(dest="group", required=True)

    config_parser = sub.add_parser("config", help="配置文件与数据根")
    config_sub = config_parser.add_subparsers(dest="command", required=True)
    init = config_sub.add_parser("init", help="创建 DATA_ROOT 布局并写出默认 AppConfig（首次显式选择数据根）")
    init.add_argument("--data-root", required=True)
    init.add_argument("--config", default=None, help="AppConfig JSON 输出路径；默认 <data-root>/settings/app.json")
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

    submit_parser = sub.add_parser("submit", help="持久化一个任务请求（不自动开始推理）")
    source_group = submit_parser.add_mutually_exclusive_group()
    source_group.add_argument("--video", default=None)
    source_group.add_argument("--url", default=None)
    submit_parser.add_argument("--config", required=True)
    submit_parser.add_argument("--mode", required=True, choices=("combined", "speech_only", "visual_only", "supplied_source"))
    submit_parser.add_argument("--timing-mode", default=None, choices=("mixed", "screen", "speech"))
    submit_parser.add_argument("--project", default=None, help="已有项目 ID；不给则新建项目")
    submit_parser.add_argument("--source-revision", default=None, help="supplied_source 模式的源字幕版本 ID")
    submit_parser.add_argument("--video-stream", default=None, help="视频流序号；单一合法流可省略")
    submit_parser.add_argument("--audio-stream", default=None, help="音频流序号或 none")
    submit_parser.add_argument("--copy-original", action="store_true")
    submit_parser.add_argument("--accept-dictionary-warnings", action="store_true")
    submit_parser.add_argument("--display-name", default=None)
    submit_parser.set_defaults(func=cmd_submit, command="request")

    queue_parser = sub.add_parser("queue", help="队列执行与取消")
    queue_sub = queue_parser.add_subparsers(dest="command", required=True)
    queue_run = queue_sub.add_parser("run", help="取得执行锁后按顺序处理 QUEUED 请求，队列耗尽即退出")
    queue_run.add_argument("--data-root", required=True)
    queue_run.add_argument("--continue-on-error", dest="continue_on_error", action="store_true", default=None)
    queue_run.add_argument("--stop-on-error", dest="continue_on_error", action="store_false")
    queue_run.set_defaults(func=cmd_queue_run)
    queue_cancel = queue_sub.add_parser("cancel", help="取消未开始请求，或请求执行器停止正在运行的请求")
    queue_cancel.add_argument("--data-root", required=True)
    queue_cancel.add_argument("--request", required=True)
    queue_cancel.set_defaults(func=cmd_queue_cancel)

    resume_parser = sub.add_parser("resume", help="把失败/中断的运行重新排回队列（保留原配置，新增 attempt）")
    resume_parser.add_argument("--data-root", required=True)
    resume_parser.add_argument("--run", required=True)
    resume_parser.set_defaults(func=cmd_resume, command="run")

    inbox_parser = sub.add_parser("inbox", help="旧收件箱接管")
    inbox_sub = inbox_parser.add_subparsers(dest="command", required=True)
    inbox_intake = inbox_sub.add_parser("intake", help="把旧 inbox 的 pending/working 请求入库到 v2 队列（内容哈希去重）")
    inbox_intake.add_argument("--config", required=True)
    inbox_intake.add_argument("--inbox", required=True)
    inbox_intake.set_defaults(func=cmd_inbox_intake)
    return parser


def run_worker(argv: list[str]) -> int:
    """Internal worker entry (plan 12.3): ``roast worker NAME ARGS...`` runs a bundled module in its own owned process."""
    if argv and argv[0] == "yt-dlp":
        try:
            import yt_dlp
        except ImportError as exc:
            _emit(_result("worker.yt-dlp", ok=False, error=ContractError("CAPABILITY_MISSING", f"yt-dlp is not installed in this runtime: {exc}")))
            return EXIT_RESOURCE
        yt_dlp.main(argv[1:])  # exits the process with yt-dlp's own exit code
        return EXIT_OK
    _emit(_result("worker", ok=False, error=ContractError("NEEDS_SELECTION", f"unknown worker {argv[:1]}", details={"supported": ["yt-dlp"]})))
    return EXIT_ARGS


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv[:1] == ["worker"]:
        return run_worker(argv[1:])
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_ARGS if exc.code not in (0, None) else EXIT_OK
    operation = f"{args.group}.{getattr(args, 'command', 'run')}"
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
