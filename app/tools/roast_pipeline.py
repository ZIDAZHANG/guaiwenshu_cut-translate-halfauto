#!/usr/bin/env python3
"""
One-command orchestration skeleton for an automated subtitle roasting workflow.

This script intentionally treats commercial GUI tools as replaceable external
commands. Fill config/roast_pipeline.example.json with the real commands after
each tool is installed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

from artifact_validation import (
    ArtifactValidationError,
    ValidationResult,
    require_valid,
    validate_file,
    validate_json,
    validate_review_csv,
    validate_subtitle,
)
from subtitle_axis_align import ALIGNMENT_VERSION, normalize_fallback, normalize_text, parse_subtitle, fmt_srt_time
from task_state import DEFAULT_STAGES, TaskAlreadyRunningError, TaskState, atomic_write_json, atomic_write_text
from subtitle_segments import SEGMENTATION_VERSION, audit_segment_records, build_segments, load_whisper_word_times, publish_segment_output, render_source_srt
from voice_dictionary import glossary_pairs, has_unreviewed_kana, missing_required_terms, resolve_config_path, resolved_term_target
from forced_alignment import run_external_alignment, validate_evidence as validate_independent_evidence


ROOT = Path(__file__).resolve().parents[1]


def configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def hidden_process_kwargs() -> dict:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startupinfo}


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def slugify(value: str) -> str:
    value = re.sub(r"https?://", "", value)
    value = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", value, flags=re.UNICODE)
    return value.strip("._") or "job"


def format_value(value: str, context: dict) -> str:
    previous = None
    current = value
    while previous != current:
        previous = current
        current = current.format(**context)
    return current


def build_env(context: dict) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    proxy = str(context.get("proxy", "")).strip()
    if proxy:
        env.update({
            "HTTP_PROXY": proxy,
            "HTTPS_PROXY": proxy,
            "ALL_PROXY": proxy,
            "http_proxy": proxy,
            "https_proxy": proxy,
            "all_proxy": proxy,
        })
    return env


def download_format_arg(config: dict) -> str:
    quality = str(config.get("download_settings", {}).get("quality", "最高画质 MP4"))
    formats = {
        "最高画质 MP4": '-f "bv*+ba/b"',
        "最高 1080p": '-f "bv*[height<=1080]+ba/b[height<=1080]"',
        "最高 720p": '-f "bv*[height<=720]+ba/b[height<=720]"',
        "优先兼容 H.264": '-f "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/b[ext=mp4]"',
    }
    return formats.get(quality, formats["最高画质 MP4"])


def download_auth_args(config: dict) -> tuple[str, str, str]:
    settings = config.get("download_settings", {})
    browser = str(settings.get("cookies_from_browser", "不使用")).strip()
    browser_names = {"Edge": "edge", "Chrome": "chrome", "Firefox": "firefox"}
    cookies_arg = f'--cookies-from-browser "{browser_names[browser]}"' if browser in browser_names else ""

    runtime = str(settings.get("js_runtime", "")).strip()
    js_runtime_arg = f'--js-runtimes "node:{runtime}"' if runtime else ""

    client = str(settings.get("youtube_client", "默认")).strip()
    client_names = {"Android VR": "android_vr", "TV": "tv"}
    extractor_arg = f'--extractor-args "youtube:player_client={client_names[client]}"' if client in client_names else ""
    return cookies_arg, js_runtime_arg, extractor_arg


def run_command(label: str, template: str, context: dict, dry_run: bool) -> None:
    if not template.strip():
        print(f"[skip] {label}: no command configured")
        return
    command = format_value(template, context)
    print(f"[run] {label}: {command}")
    if dry_run:
        return
    process = subprocess.Popen(
        command,
        shell=True,
        env=build_env(context),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        **hidden_process_kwargs(),
    )
    assert process.stdout is not None
    last_line = ""
    for line in process.stdout:
        print(line, end="", flush=True)
        if line.strip():
            last_line = line.strip()
    code = process.wait()
    if code != 0:
        detail = f": {last_line}" if last_line else ""
        raise SystemExit(f"{label} failed with exit code {code}{detail}")


def validation_map(results: dict[str, ValidationResult]) -> dict[str, dict]:
    return {name: result.to_dict() for name, result in results.items()}


def require_all_valid(results: dict[str, ValidationResult]) -> dict[str, dict]:
    invalid = [f"{name}: {result.summary()}" for name, result in results.items() if not result.valid]
    if invalid:
        first = next(result for result in results.values() if not result.valid)
        first.errors.insert(0, " | ".join(invalid))
        raise ArtifactValidationError(first)
    return validation_map(results)


def exit_code_from_error(error: BaseException) -> int | None:
    value = error.code if isinstance(error, SystemExit) else None
    if isinstance(value, int):
        return value
    match = re.search(
        r"(?:exit(?:ed)?|failed)\s+with\s+(?:exit\s+)?code\s+(-?\d+)",
        str(error),
        flags=re.IGNORECASE,
    )
    return int(match.group(1)) if match else None


def classify_stage_error(error: BaseException) -> str:
    if isinstance(error, ArtifactValidationError):
        return "artifact_validation"
    code = exit_code_from_error(error)
    if code is not None and code & 0xFFFFFFFF == 0x40010004:
        return "external_termination"
    if isinstance(error, SystemExit):
        return "component_exit"
    return type(error).__name__


def checkpoint_signature(value: object) -> str:
    """Hash small inputs; identify large media/models without rereading gigabytes."""
    def snapshot(item):
        if isinstance(item, dict):
            return {str(key): snapshot(val) for key, val in item.items()}
        if isinstance(item, (list, tuple)):
            return [snapshot(val) for val in item]
        if isinstance(item, (str, Path)) and str(item):
            try:
                path = Path(item)
                if path.is_file():
                    stat = path.stat()
                    metadata = {"path": str(path.resolve()), "size": stat.st_size}
                    if stat.st_size <= 16 * 1024 * 1024:
                        metadata["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                    else:
                        metadata["mtime_ns"] = stat.st_mtime_ns
                    return metadata
            except (OSError, ValueError):
                pass
            return str(item)
        return item
    serialized = json.dumps(snapshot(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def execute_stage(
    task: TaskState | None,
    stage_name: str,
    *,
    component: str,
    action: Callable[[], None],
    validate: Callable[[], dict[str, ValidationResult]],
    inputs: Callable[[], dict],
    outputs: Callable[[], dict],
    restore: Callable[[dict], None] | None = None,
    cache_inputs: Callable[[], dict] | None = None,
) -> dict[str, dict]:
    if task is None:
        action()
        return {}

    stage_inputs = inputs()
    signature = checkpoint_signature({"inputs": stage_inputs, "dependencies": cache_inputs() if cache_inputs else {}})
    current_status = task.stage_status(stage_name)
    if current_status == "DONE" and task.data["stages"][stage_name].get("inputs", {}).get("checkpoint_signature") != signature:
        print(f"[resume] {stage_name}: inputs/settings changed or legacy checkpoint, rerunning")
        task.reset_from(stage_name, reason="Checkpoint inputs, settings or implementation changed",
                        preserve=("ASR",) if stage_name == "TIMELINE" else ())
        current_status = "PENDING"
    if current_status == "DONE":
        if restore:
            restore(dict(task.data["stages"][stage_name].get("outputs", {})))
        try:
            checked = require_all_valid(validate())
        except ArtifactValidationError as exc:
            print(f"[resume] {stage_name}: saved output is invalid, rerunning: {exc}")
            task.reset_from(stage_name, reason=f"Saved output failed validation: {exc}",
                            preserve=("ASR",) if stage_name == "TIMELINE" else ())
        else:
            print(f"[resume] {stage_name}: validated DONE checkpoint, skipping")
            return checked
    elif current_status != "PENDING":
        task.reset_from(stage_name, reason=f"Retrying stage from {current_status}",
                        preserve=("ASR",) if stage_name == "TIMELINE" else ())

    task.start_stage(stage_name, inputs={**stage_inputs, "checkpoint_signature": signature}, outputs=outputs(), component=component)
    try:
        action()
        checked = require_all_valid(validate())
    except KeyboardInterrupt:
        task.interrupt(message=f"Interrupted while running {stage_name}")
        raise
    except (SystemExit, Exception) as exc:
        details = exc.result.to_dict() if isinstance(exc, ArtifactValidationError) else {}
        task.fail_stage(
            stage_name,
            kind=classify_stage_error(exc),
            message=str(exc),
            component=component,
            exit_code=exit_code_from_error(exc),
            recoverable=True,
            details=details,
        )
        if isinstance(exc, SystemExit):
            raise
        raise SystemExit(f"{stage_name} failed: {exc}") from exc
    task.complete_stage(stage_name, validation=checked, outputs=outputs(), component=component)
    return checked


def ensure_dirs(job_dir: Path) -> None:
    for name in ("source", "subs", "deliverables", "logs"):
        (job_dir / name).mkdir(parents=True, exist_ok=True)


def path_from_template(template: str, context: dict) -> Path:
    return Path(format_value(template, context))


def maybe_copy_media(src: Path, job_dir: Path, stem: str, dry_run: bool, copy_media: bool) -> Path:
    if not copy_media:
        return src
    target = job_dir / "source" / f"{stem}{src.suffix.lower()}"
    print(f"[copy] {src} -> {target}")
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    return target


def probe_video(ffmpeg: Path, video: Path) -> tuple[str, float | None]:
    completed = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-i", str(video)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        **hidden_process_kwargs(),
    )
    output = completed.stdout or ""
    codec_match = re.search(r"Video:\s*([^,\s]+)", output, flags=re.IGNORECASE)
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
    codec = codec_match.group(1).lower() if codec_match else "unknown"
    duration = None
    if duration_match:
        hours, minutes, seconds = duration_match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return codec, duration


def validate_video_artifact(config: dict, video: Path) -> ValidationResult:
    result = validate_file(video, "video", min_bytes=1024)
    if not result.valid:
        return result
    ffmpeg = Path(str(config.get("tools", {}).get("ffmpeg", "")))
    if not ffmpeg.is_file():
        result.fail(f"ffmpeg does not exist: {ffmpeg}")
        return result
    codec, duration = probe_video(ffmpeg, video)
    result.details.update({"codec": codec, "duration_seconds": duration})
    if codec == "unknown":
        result.fail("video stream could not be detected")
    if duration is None or duration <= 0:
        result.fail("media duration could not be detected")
    settings = config.get("media_compatibility", {})
    if settings.get("auto_convert", True):
        accepted_containers = {str(value).lower() for value in settings.get("accepted_containers", [".mp4"])}
        accepted_codecs = {str(value).lower() for value in settings.get("accepted_video_codecs", ["h264"])}
        if video.suffix.lower() not in accepted_containers or codec not in accepted_codecs:
            result.fail(f"video is still incompatible after normalization: {video.suffix.lower()} / {codec}")
    return result


def validate_audio_artifact(config: dict, audio: Path) -> ValidationResult:
    result = validate_file(audio, "audio", min_bytes=44)
    if not result.valid:
        return result
    ffmpeg = Path(str(config.get("tools", {}).get("ffmpeg", "")))
    if not ffmpeg.is_file():
        result.fail(f"ffmpeg does not exist: {ffmpeg}")
        return result
    _codec, duration = probe_video(ffmpeg, audio)
    result.details["duration_seconds"] = duration
    if duration is None or duration <= 0:
        result.fail("audio duration could not be detected")
    return result


def ensure_compatible_video(config: dict, video: Path, job_dir: Path, dry_run: bool) -> Path:
    settings = config.get("media_compatibility", {})
    if not settings.get("auto_convert", True):
        print(f"[media] compatibility conversion disabled: {video}")
        return video

    ffmpeg = Path(str(config.get("tools", {}).get("ffmpeg", "")))
    if not ffmpeg.is_file():
        raise SystemExit(f"ffmpeg does not exist: {ffmpeg}")
    if not dry_run and not video.is_file():
        raise SystemExit(f"video does not exist: {video}")

    codec, duration = probe_video(ffmpeg, video) if video.is_file() else ("unknown", None)
    suffix = video.suffix.lower()
    duration_text = f" duration={duration:.2f}s" if duration is not None else ""
    print(f"[media] container={suffix or 'unknown'} codec={codec}{duration_text}")
    accepted_containers = {str(value).lower() for value in settings.get("accepted_containers", [".mp4"])}
    accepted_codecs = {str(value).lower() for value in settings.get("accepted_video_codecs", ["h264"])}
    if suffix in accepted_containers and codec in accepted_codecs:
        print("[media] video is compatible with MagiaTimeline")
        return video

    target = job_dir / "source" / "normalized_h264.mp4"
    preset = str(settings.get("preset", "fast"))
    crf = int(settings.get("crf", 18))
    command = [
        str(ffmpeg), "-y", "-i", str(video),
        "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", str(target),
    ]
    print(f"[media] converting for MagiaTimeline: {subprocess.list2cmdline(command)}")
    if dry_run:
        return target
    completed = subprocess.run(command, env=build_env({}), **hidden_process_kwargs())
    if completed.returncode != 0 or not target.is_file():
        raise SystemExit(f"video conversion failed with exit code {completed.returncode}")
    print(f"[media] compatible video ready: {target}")
    return target



def copy_subtitle_input(label: str, src: Path | None, target_value: str, dry_run: bool) -> None:
    if not src:
        return
    target = Path(target_value)
    print(f"[copy] {label}: {src} -> {target}")
    if dry_run:
        return
    if not src.exists():
        raise SystemExit(f"{label} input does not exist: {src}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() != target.resolve():
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copy2(src, temporary)
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise



def write_external_handoff(job_dir: Path, context: dict) -> None:
    note = job_dir / "README_external_tools.txt"
    lines = [
        "外部字幕工具交接单",
        "",
        "任务已准备好。没有命令行接口的软件，可读取下面的输入文件，",
        "并把结果保存到指定路径；流水线会自动继续。",
        "",
        "输入文件：",
        f"- 视频：{context.get('video', '')}",
        f"- 音频：{context.get('audio', '')}",
        "",
        "必须生成的字幕：",
        f"- 精准空轴：{context.get('timeline', '')}",
        f"- 日文识别：{context.get('jp', '')}",
        f"- 机器直译：{context.get('zh', '')}",
        "",
        "翻译规则：",
        f"- 提示词：{context.get('translation_prompt', '')}",
        f"- 术语表：{context.get('translation_glossary', '')}",
        "",
        "完成后自动生成：",
        f"- 最终字幕：{context.get('final', '')}",
        f"- 复核表：{context.get('review', '')}",
        f"- 剪映导入包：{context.get('jianying_import', '')}",
        "",
        "启用等待模式后，流水线会一直等到三个字幕文件就绪。",
        "",
    ]
    atomic_write_text(note, "\n".join(lines))
    print(f"[info] external tool handoff: {note}")


def wait_for_file(label: str, path: Path, timeout_seconds: int) -> None:
    print(f"[wait] {label}: {path}")
    if timeout_seconds < 0:
        raise ValueError("File wait timeout cannot be negative")
    deadline = time.time() + timeout_seconds if timeout_seconds > 0 else None
    while deadline is None or time.time() < deadline:
        if path.exists() and path.stat().st_size > 0:
            print(f"[ok] {label}: found")
            return
        time.sleep(2)
    raise SystemExit(f"{label} was not created within {timeout_seconds} seconds: {path}")


def require_alignment_inputs(config: dict, context: dict, wait_missing: bool, timeout_seconds: int) -> None:
    expected = config["expected_outputs"]
    required = {
        "timeline": path_from_template(expected["timeline"], context),
        "jp": path_from_template(expected["jp"], context),
        "zh": path_from_template(expected["zh"], context),
    }
    missing = [(label, path) for label, path in required.items() if not (path.exists() and path.stat().st_size > 0)]
    if not missing:
        return
    print("[info] missing subtitle inputs:")
    for label, path in missing:
        print(f"  - {label}: {path}")
    print(f"[info] see: {Path(context['job_dir']) / 'README_external_tools.txt'}")
    if not wait_missing:
        raise SystemExit(
            "Missing subtitle inputs. Fill the matching command in config, create the files manually, "
            "or rerun with --wait-missing."
        )
    for label, path in missing:
        wait_for_file(label, path, timeout_seconds)

def find_downloaded_video(job_dir: Path) -> Path:
    source = job_dir / "source"
    candidates = sorted(
        [p for p in source.glob("video.*") if p.suffix.lower() in {".mp4", ".mkv", ".webm"}],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return source / "video.mp4"
    return candidates[0]


def run_alignment(config: dict, context: dict, dry_run: bool) -> None:
    expected = config["expected_outputs"]
    alignment = config.get("alignment", {})
    timeline = path_from_template(expected["timeline"], context)
    jp = path_from_template(expected["jp"], context)
    zh = path_from_template(expected["zh"], context)
    final = path_from_template(expected["final"], context)
    review = path_from_template(expected["review"], context)
    candidate_final = final.with_name(f".{final.stem}.{uuid.uuid4().hex}.tmp{final.suffix}")
    candidate_review = review.with_name(f".{review.stem}.{uuid.uuid4().hex}.tmp{review.suffix}")

    print(f"[run] canonical segment output: {final}")
    if dry_run:
        return
    records = json.loads(Path(context["segments"]).read_text(encoding="utf-8-sig"))
    metadata_path = zh.with_suffix(".segments.json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig")) if metadata_path.is_file() else None
    publish_segment_output(records, parse_subtitle(zh)[2], candidate_final, candidate_review,
                           template=timeline if timeline.is_file() else None,
                           translation_metadata=metadata,
                           bilingual=bool(alignment.get("keep_japanese")),
                           mark_low=bool(alignment.get("mark_low")))
    timeline_result = validate_subtitle(Path(context["translation_input"]), "segmented_source", require_text=True)
    jp_result = validate_subtitle(jp, "jp_asr", require_text=True, expected_language="ja")
    final_result = validate_subtitle(
        candidate_final,
        "final",
        require_text=True,
        expected_language="zh",
        expected_cues=timeline_result.cue_count or None,
        media_duration_seconds=context.get("media_duration_seconds"),
    )
    review_result = validate_review_csv(candidate_review, minimum_rows=max(1, timeline_result.cue_count), require_provenance=True)
    results = {"timeline": timeline_result, "jp": jp_result, "final": final_result, "review": review_result}
    atomic_write_json(final.parent / "final_validation.json", validation_map(results))
    try:
        require_all_valid(results)
    except ArtifactValidationError:
        if candidate_final.is_file():
            evidence_final = final.parent / "align_failed_candidate.ass"
            shutil.move(str(candidate_final), str(evidence_final))
            final_result.path = str(evidence_final.resolve())
        if candidate_review.is_file():
            evidence_review = review.parent / "align_failed_review.csv"
            shutil.move(str(candidate_review), str(evidence_review))
            review_result.path = str(evidence_review.resolve())
        atomic_write_json(final.parent / "final_validation.json", validation_map(results))
        raise
    os.replace(candidate_final, final)
    os.replace(candidate_review, review)
    final_result.path = str(final.resolve())
    review_result.path = str(review.resolve())
    atomic_write_json(final.parent / "final_validation.json", validation_map(results))


def run_jianying_bridge(config: dict, context: dict, draft: str | None, dry_run: bool) -> None:
    expected = config["expected_outputs"]
    out_dir = path_from_template(expected.get("jianying_import", "{job_dir}/jianying_import"), context)
    status_out = path_from_template(expected.get("jianying_status", "{job_dir}/jianying_status.json"), context)
    command = [
        sys.executable,
        str(ROOT / "tools" / "jianying_bridge.py"),
        "--video",
        context.get("video", ""),
        "--subtitle",
        context.get("final", ""),
        "--review",
        context.get("review", ""),
        "--out",
        str(out_dir),
        "--status-out",
        str(status_out),
    ]
    if draft:
        command.extend(["--draft", draft])

    printable = " ".join(shlex.quote(part) for part in command if part != "")
    print(f"[run] jianying package: {printable}")
    if dry_run:
        return
    completed = subprocess.run(command, env=build_env(context), **hidden_process_kwargs())
    if completed.returncode != 0:
        raise SystemExit(f"jianying package failed with exit code {completed.returncode}")


def run_jianying_ui_import(config: dict, context: dict, dry_run: bool) -> None:
    settings = config.get("jianying_ui", {})
    status_out = Path(context["job_dir"]) / "jianying_ui_status.json"
    screenshot = Path(context["job_dir"]) / "deliverables" / "jianying_draft.png"
    command = [
        sys.executable,
        str(ROOT / "tools" / "jianying_ui_import.py"),
        "--config",
        str(context["config_path"]),
        "--video",
        context["video"],
        "--subtitle",
        context["final"],
        "--status-out",
        str(status_out),
        "--screenshot",
        str(screenshot),
    ]
    if settings.get("create_new_draft", True):
        command.append("--create")
    print(f"[run] Jianying UI import: {subprocess.list2cmdline(command)}")
    context["jianying_ui_status"] = str(status_out)
    context["jianying_draft_screenshot"] = str(screenshot)
    if dry_run:
        return
    completed = subprocess.run(command, cwd=str(ROOT), env=build_env(context), **hidden_process_kwargs())
    if completed.returncode != 0:
        raise SystemExit(f"Jianying UI import failed with exit code {completed.returncode}")


def write_job_manifest(job_dir: Path, context: dict, args: argparse.Namespace) -> None:
    manifest = {
        "job_dir": str(job_dir),
        "source": {
            "url": context.get("url", ""),
            "video": context.get("video", ""),
            "audio": context.get("audio", ""),
        },
        "subtitles": {
            "timeline": context.get("timeline", ""),
            "jp": context.get("jp", ""),
            "zh": context.get("zh", ""),
            "final": context.get("final", ""),
            "review": context.get("review", ""),
            "segments": context.get("segments", ""),
            "translation_input": context.get("translation_input", ""),
        },
        "quality": context.get("quality_summary", {}),
        "jianying": {
            "import_package": context.get("jianying_import", ""),
            "status": context.get("jianying_status", ""),
            "draft": args.jianying_draft or "",
            "ui_import_status": context.get("jianying_ui_status", ""),
            "draft_screenshot": context.get("jianying_draft_screenshot", ""),
        },
        "options": {
            "dry_run": bool(args.dry_run),
            "skip_download": bool(args.skip_download),
            "skip_audio": bool(args.skip_audio),
            "skip_external": bool(args.skip_external),
            "wait_missing": bool(args.wait_missing),
            "copy_media": bool(args.copy_media),
            "skip_jianying_package": bool(args.skip_jianying_package),
            "jianying_ui": bool(context.get("jianying_ui_enabled")),
            "proxy_enabled": bool(context.get("proxy")),
        },
        "handoff": str(job_dir / "README_external_tools.txt"),
        "smoke_test_hint": "Run tools/smoke_test.py from the installation folder to validate the local pipeline.",
    }
    path = job_dir / "job_manifest.json"
    atomic_write_json(path, manifest)
    print(f"[info] job manifest: {path}")


    index_path = job_dir.parent / "jobs_index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    if index_path.exists():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            index = {"jobs": []}
    else:
        index = {"jobs": []}

    jobs = [job for job in index.get("jobs", []) if job.get("job_dir") != str(job_dir)]
    jobs.insert(0, {
        "job_dir": str(job_dir),
        "manifest": str(path),
        "url": context.get("url", ""),
        "video": context.get("video", ""),
        "final": context.get("final", ""),
        "review": context.get("review", ""),
        "jianying_import": context.get("jianying_import", ""),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    })
    index = {"updated_at": datetime.now().isoformat(timespec="seconds"), "jobs": jobs}
    atomic_write_json(index_path, index)
    print(f"[info] jobs index: {index_path}")



def write_import_note(job_dir: Path, context: dict) -> None:
    note = job_dir / "deliverables" / "import_to_jianying.txt"
    final = context.get("final", "")
    video = context.get("video", "")
    review = context.get("review", "")
    atomic_write_text(
        note,
        "\n".join(
            [
                "剪映导入检查顺序：",
                f"1. 导入视频：{video}",
                f"2. 导入字幕：{final}",
                f"3. 对照复核表：{review}",
                "4. 重点检查 review.csv 中 confidence 为 low/empty 或 flags 不为空的行。",
                "",
            ]
        ),
    )


def retime_supplied_translation(source: Path, metadata_source: Path, target: Path, records_path: Path) -> None:
    """Reuse Chinese only when every original segment identity and text still matches."""
    records = json.loads(records_path.read_text(encoding="utf-8-sig"))
    audit_segment_records(records)
    metadata = json.loads(metadata_source.read_text(encoding="utf-8-sig"))
    cues = parse_subtitle(source)[2]
    items, entries = records["segments"], metadata.get("translations", [])
    if len(cues) != len(items) or len(entries) != len(items) or any(
        entry.get("cue_id") != item["id"] or entry.get("index") != cue.index or cue.index != item["index"] or
        entry.get("text") != cue.text or entry.get("start_ms") != cue.start or entry.get("end_ms") != cue.end
        for item, entry, cue in zip(items, entries, cues)
    ):
        raise ValueError("Supplied translation identity differs from the original source or supplied text")
    if target.resolve() in {source.resolve(), metadata_source.resolve()}:
        raise ValueError("Supplied translation must be retained separately from the derived output")
    if all((item["start_ms"], item["end_ms"]) == (cue.start, cue.end) for item, cue in zip(items, cues)):
        return
    shutil.copy2(source, target.with_suffix(".supplied.srt"))
    shutil.copy2(metadata_source, target.with_suffix(".supplied.segments.json"))
    metadata["origin_source_signature"] = metadata.get("source_signature")
    metadata["retiming"] = {
        "target_mode": records["mode"],
        "original_timestamps": [{"index":c.index,"start_ms":c.start,"end_ms":c.end} for c in cues],
        "original_srt_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "original_metadata_sha256": hashlib.sha256(metadata_source.read_bytes()).hexdigest(),
    }
    metadata["source_signature"] = {
        "path":str(records_path.resolve()),"exists":True,"size":records_path.stat().st_size,
        "sha256":hashlib.sha256(records_path.read_bytes()).hexdigest(),
    }
    for item, entry in zip(items, entries):
        entry.update(start_ms=item["start_ms"], end_ms=item["end_ms"])
    text = "\n".join(f'{item["index"]}\n{fmt_srt_time(item["start_ms"])} --> {fmt_srt_time(item["end_ms"])}\n{cue.text}\n'
                     for item, cue in zip(items, cues))
    atomic_write_text(target, text)
    atomic_write_json(target.with_suffix(".segments.json"), metadata)


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Automated roasting pipeline skeleton.")
    parser.add_argument("url", help="Video URL")
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "roast_pipeline.example.json")
    parser.add_argument("--video-file", type=Path, help="Use an existing downloaded video file instead of downloading")
    parser.add_argument("--audio-file", type=Path, help="Use an existing audio file instead of extracting one")
    parser.add_argument("--timeline-file", type=Path, help="Use an existing accurate timeline subtitle file")
    parser.add_argument("--timing-mode", choices=("mixed", "screen", "speech"), help="Choose evidence for canonical segment boundaries")
    parser.add_argument("--jp-file", type=Path, help="Use an existing Japanese OCR/ASR subtitle file")
    parser.add_argument("--zh-file", type=Path, help="Use an existing machine Chinese subtitle file")
    parser.add_argument("--copy-media", action="store_true", help="Copy existing video/audio files into the job source folder")
    parser.add_argument("--skip-audio", action="store_true", help="Do not extract audio with ffmpeg")
    parser.add_argument("--proxy", help="Optional proxy URL, for example http://127.0.0.1:7890")
    parser.add_argument("--job-name", help="Optional stable job folder name")
    parser.add_argument("--work-dir", type=Path, help="Override the output root directory for this job")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running them")
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--skip-external", action="store_true", help="Skip axis/OCR/translation commands")
    parser.add_argument("--wait-missing", action="store_true", help="Wait for timeline/jp/zh subtitles if external tools create them later")
    parser.add_argument("--wait-timeout", type=int, default=0, help="Seconds to wait for each missing subtitle; 0 waits until the file is ready")
    parser.add_argument("--jianying-draft", help="Optional existing Jianying draft directory to inspect before packaging")
    parser.add_argument("--skip-jianying-package", action="store_true", help="Do not create the Jianying import package")
    parser.add_argument("--jianying-ui", action=argparse.BooleanOptionalAction, default=None, help="Create/open a Jianying draft and import the video and final subtitle through the UI")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    axis_settings = config.get("axis_settings", {})
    recognition_settings = config.get("recognition", {})
    translation_settings = config.get("translation", {})
    translation_engine = str(translation_settings.get("engine", "Sakura 本地模型")).strip()
    translation_config_enabled = bool(translation_settings.get("enabled", True)) and (
        translation_engine != "不翻译，仅生成日文"
    )
    full_output = bool(args.zh_file) or translation_config_enabled
    output_mode = "full" if full_output else "japanese_only"
    axis_stage_enabled = full_output and (bool(args.timeline_file) or bool(axis_settings.get("enabled", True)))
    recognition_stage_enabled = bool(args.jp_file) or bool(recognition_settings.get("enabled", True))
    if not recognition_stage_enabled:
        raise SystemExit("当前模式需要日文字幕：请启用日文识别，或提供 --jp-file。")

    job_name = args.job_name or f"{datetime.now():%Y%m%d_%H%M%S}_{slugify(args.url)}"
    job_dir = ((args.work_dir or Path(config["work_dir"])) / job_name).resolve()
    ensure_dirs(job_dir)

    context = {
        **config.get("tools", {}),
        **config.get("resources", {}),
        "root": str(ROOT),
        "url": args.url,
        "job_dir": str(job_dir),
        "config_path": str(args.config.resolve()),
        "proxy": args.proxy if args.proxy is not None else config.get("proxy", ""),
        "video": "",
        "audio": "",
    }
    context["proxy_arg"] = f'--proxy "{context["proxy"]}"' if context.get("proxy") else ""
    context["format_arg"] = download_format_arg(config)
    context["cookies_arg"], context["js_runtime_arg"], context["extractor_arg"] = download_auth_args(config)
    context["audio_rate"] = str(config.get("download_settings", {}).get("audio_rate", 16000))
    expected = config["expected_outputs"]
    context.update({key: str(path_from_template(value, context)) for key, value in expected.items()})
    context["segments"] = str(job_dir / "subs" / "translation_input.segments.json")
    context["translation_input"] = str(job_dir / "subs" / "translation_input.srt")
    context["timing_mode"] = args.timing_mode or str(config.get("alignment", {}).get("timing_mode", "mixed"))

    task_source = {
        "url": args.url,
        "video_file": str(args.video_file.resolve()) if args.video_file else "",
        "audio_file": str(args.audio_file.resolve()) if args.audio_file else "",
        "timeline_file": str(args.timeline_file.resolve()) if args.timeline_file else "",
        "jp_file": str(args.jp_file.resolve()) if args.jp_file else "",
        "zh_file": str(args.zh_file.resolve()) if args.zh_file else "",
        "skip_audio": bool(args.skip_audio),
        "skip_external": bool(args.skip_external),
        "copy_media": bool(args.copy_media),
        "skip_jianying_package": bool(args.skip_jianying_package),
        "output_mode": output_mode,
    }
    task: TaskState | None = None
    if not args.dry_run:
        try:
            task = TaskState.open_or_create(
                job_dir / "task.json",
                source=task_source,
                job_dir=job_dir,
                display_name=job_name,
            )
        except TaskAlreadyRunningError as exc:
            raise SystemExit(str(exc)) from exc
        saved_source = task.data.get("source", {})
        identity_keys = (
            "url",
            "video_file",
            "audio_file",
            "timeline_file",
            "jp_file",
            "zh_file",
            "output_mode",
        )
        if any(
            str(saved_source.get(key, "")) != str(task_source[key])
            for key in identity_keys
            if key != "output_mode" or key in saved_source
        ):
            raise SystemExit(
                f"Task folder belongs to different inputs: {job_dir}. "
                "Use a new task name or restore the original inputs."
            )

    if task is not None:
        task.ensure_stages(DEFAULT_STAGES)

    independent_settings = config.get("forced_alignment", {})
    independent_enabled = bool(independent_settings.get("enabled", False))
    generate_audio = not args.audio_file and not args.skip_audio and (not args.jp_file or independent_enabled)
    need_audio = bool(args.audio_file) or generate_audio

    def dependencies(stage: str) -> dict:
        sections = {
            "INPUT": ("tools", "download_settings", "media_compatibility"),
            "TIMELINE": ("axis_settings",),
            "ASR": ("recognition",),
            "SEGMENTATION": ("alignment", "axis_settings", "forced_alignment"),
            "TRANSLATION": ("translation", "resources", "external_tools"),
            "FINAL_OUTPUT": ("alignment", "resources"),
            "NLE_EXPORT": ("jianying_ui",),
        }[stage]
        modules = {
            "INPUT": (), "TIMELINE": ("magia_timeline_adapter.py", "magia_utf8_runtime.py"),
            "ASR": ("voicetransl_adapter.py", "voice_common.py", "subtitle_segments.py"),
            "SEGMENTATION": ("subtitle_segments.py", "forced_alignment.py"),
            "TRANSLATION": ("voicetransl_adapter.py", "voice_dictionary.py", "sakura_client.py", "translation_checkpoint.py"),
            "FINAL_OUTPUT": ("subtitle_axis_align.py", "subtitle_segments.py", "artifact_validation.py"),
            "NLE_EXPORT": ("jianying_bridge.py", "jianying_ui_import.py"),
        }[stage]
        command_keys = {"INPUT": ("download", "extract_audio"), "TIMELINE": ("axis",),
                        "ASR": ("recognize_jp",), "TRANSLATION": ("translate_zh",)}.get(stage, ())
        settings = {key: config.get(key, {}) for key in sections}
        resource_paths = {key: str(resolve_config_path(value, args.config))
                          for key, value in config.get("resources", {}).items() if isinstance(value, str)} if "resources" in sections else {}
        for section in sections:
            for key, value in config.get(section, {}).items():
                if isinstance(value, str) and value and (key.startswith("native_") or key in {"model", "executable", "server_executable", "vad_model", "python", "model_manifest"}):
                    resource_paths[f"{section}.{key}"] = str(resolve_config_path(value, args.config))
        if stage == "TIMELINE" and config.get("axis_settings", {}).get("executable"):
            app_root = resolve_config_path(config["axis_settings"]["executable"], args.config).parent
            resource_paths["axis_support"] = [str(app_root / name) for name in
                ("config.yml", "template.asst", "ConfigSchema.json")]
            resource_paths["ocr_models"] = [str(path) for path in sorted(
                (app_root / "PaddleOCRModels").rglob("*")) if path.is_file()]
        if stage == "SEGMENTATION" and independent_enabled:
            model_dir = resolve_config_path(independent_settings.get("model", ""), args.config)
            resource_paths["forced_model_files"] = [str(path) for path in sorted(model_dir.rglob("*")) if path.is_file()] if model_dir.is_dir() else [str(model_dir)]
            runtime_python = resolve_config_path(independent_settings.get("python", ""), args.config)
            runtime_root = runtime_python.parent.parent
            resource_paths["forced_sdk"] = [str(runtime_root / "Lib/site-packages/qwen_asr/inference/qwen3_forced_aligner.py"),
                                            str(runtime_root / "Lib/site-packages/qwen_asr/inference/utils.py")]
        return {"settings": settings, "resources": resource_paths,
                "commands": {key: config.get("commands", {}).get(key, "") for key in command_keys},
                "implementation": [str(ROOT / "tools" / name) for name in ("roast_pipeline.py", "task_state.py", *modules)],
                "alignment_version": ALIGNMENT_VERSION if stage == "FINAL_OUTPUT" else None,
                "segmentation_version": SEGMENTATION_VERSION if stage == "SEGMENTATION" else None,
                "timing_mode": context["timing_mode"] if stage == "SEGMENTATION" else None}

    def prepare_input() -> None:
        if args.video_file:
            video_path = maybe_copy_media(args.video_file, job_dir, "input_video", args.dry_run, args.copy_media)
            print(f"[info] using existing video: {video_path}")
        else:
            if not args.skip_download:
                run_command("download", config["commands"].get("download", ""), context, args.dry_run)
            video_path = find_downloaded_video(job_dir)
        video_path = ensure_compatible_video(config, video_path, job_dir, args.dry_run)

        audio_path = None
        if args.audio_file:
            audio_path = maybe_copy_media(args.audio_file, job_dir, "input_audio", args.dry_run, args.copy_media)
            print(f"[info] using existing audio: {audio_path}")
        elif generate_audio:
            audio_path = job_dir / "source" / "audio.wav"
        context.update({"video": str(video_path), "audio": str(audio_path or "")})
        write_external_handoff(job_dir, context)
        if generate_audio:
            run_command("extract_audio", config["commands"].get("extract_audio", ""), context, args.dry_run)
        elif args.skip_audio:
            print("[skip] extract_audio: --skip-audio")
        elif args.jp_file:
            print("[skip] extract_audio: supplied Japanese subtitles")

    def restore_input(saved_outputs: dict) -> None:
        context["video"] = str(saved_outputs.get("video", ""))
        context["audio"] = str(saved_outputs.get("audio", ""))

    def validate_input() -> dict[str, ValidationResult]:
        results = {"video": validate_video_artifact(config, Path(context["video"]))}
        if need_audio:
            results["audio"] = validate_audio_artifact(config, Path(context["audio"]))
        duration = results["video"].details.get("duration_seconds")
        if duration:
            context["media_duration_seconds"] = float(duration)
        return results

    execute_stage(
        task,
        "INPUT",
        component="media",
        action=prepare_input,
        validate=validate_input,
        inputs=lambda: {key: task_source[key] for key in ("url", "video_file", "audio_file", "skip_audio", "copy_media")}
                       | {"generate_audio": generate_audio},
        outputs=lambda: {"video": context.get("video", ""), "audio": context.get("audio", "")},
        restore=restore_input,
        cache_inputs=lambda: dependencies("INPUT"),
    )
    if not args.dry_run:
        write_external_handoff(job_dir, context)

    def skip_pipeline_stage(stage_name: str, *, component: str, reason: str, outputs: dict | None = None) -> None:
        print(f"[skip] {stage_name}: {reason}")
        if task is not None:
            task.skip_stage(stage_name, reason=reason, component=component, outputs=outputs)

    def run_or_wait(label: str, supplied: Path | None, output_key: str) -> None:
        target = Path(context[output_key])
        if supplied:
            copy_subtitle_input(label, supplied, str(target), args.dry_run)
            return
        if not args.skip_external:
            run_command(label, config["commands"].get(label, ""), context, args.dry_run)
        else:
            print(f"[skip] {label}: --skip-external")
        if not args.dry_run and args.wait_missing and not (target.is_file() and target.stat().st_size > 0):
            wait_for_file(label, target, args.wait_timeout)

    def subtitle_validation(path_key: str, role: str, language: str | None, require_text: bool) -> dict[str, ValidationResult]:
        return {
            path_key: validate_subtitle(
                Path(context[path_key]),
                role,
                require_text=require_text,
                expected_language=language,
                media_duration_seconds=context.get("media_duration_seconds"),
            )
        }

    def validate_asr() -> dict[str, ValidationResult]:
        # ASR parent times are preserved source evidence. Only derived output
        # uses the media boundary as an acceptance gate, before translation.
        result = validate_subtitle(Path(context["jp"]), "jp_asr", require_text=True, expected_language="ja")
        limit = context.get("media_duration_seconds")
        if result.valid and limit is not None and result.last_end_ms > round(float(limit) * 1000):
            result.warnings.append("Original coarse ASR timestamps exceed the media; canonical segmentation must establish bounded output times")
            result.details["coarse_asr_outside_media"] = True
            result.details["media_duration_ms"] = round(float(limit) * 1000)
        results = {"jp": result}
        atomic_write_json(Path(context["jp"]).parent / "jp_validation.json", result.to_dict())
        return results

    def recognize_source() -> None:
        run_or_wait("recognize_jp", args.jp_file, "jp")
        if args.jp_file and not args.dry_run:
            for suffix in (".word_times.json", ".whisper.json"):
                source = args.jp_file.with_suffix(suffix)
                destination = Path(context["jp"]).with_suffix(suffix)
                if source.is_file() and source.resolve() != destination.resolve():
                    shutil.copy2(source, destination)

    if axis_stage_enabled:
        execute_stage(
            task,
            "TIMELINE",
            component="MagiaTimeline",
            action=lambda: run_or_wait("axis", args.timeline_file, "timeline"),
            validate=lambda: subtitle_validation("timeline", "timeline", None, False),
            inputs=lambda: {"video": context["video"], "supplied": str(args.timeline_file or "")},
            outputs=lambda: {"timeline": context["timeline"]},
            cache_inputs=lambda: dependencies("TIMELINE"),
        )
    else:
        skip_pipeline_stage(
            "TIMELINE",
            component="MagiaTimeline",
            reason="仅生成日文字幕，不需要精准字幕轴",
        )
    execute_stage(
        task,
        "ASR",
        component="VoiceTransl Whisper",
        action=recognize_source,
        validate=validate_asr,
        inputs=lambda: {"audio": context["audio"], "supplied": str(args.jp_file or ""),
                        "supplied_word_times": str(args.jp_file.with_suffix(".word_times.json")) if args.jp_file else "",
                        "supplied_native_tokens": str(args.jp_file.with_suffix(".whisper.json")) if args.jp_file else ""},
        outputs=lambda: {"jp": context["jp"]},
        cache_inputs=lambda: dependencies("ASR"),
    )

    def prepare_segments() -> None:
        if args.dry_run:
            print("[run] segment Japanese source with visual and speech evidence")
            return
        speech = parse_subtitle(Path(context["jp"]))[2]
        visual = parse_subtitle(Path(context["timeline"]))[2] if axis_stage_enabled else None
        word_path = Path(context["jp"]).with_suffix(".word_times.json")
        words, word_validation = None, {}
        if word_path.is_file():
            evidence = json.loads(word_path.read_text(encoding="utf-8-sig"))
            if ("source_cues" not in evidence or (evidence.get("timestamp_kind") == "whisper_cpp_token_offsets"
                    and evidence.get("schema_version", 0) < 2)) and Path(context["jp"]).with_suffix(".whisper.json").is_file():
                evidence = load_whisper_word_times(Path(context["jp"]).with_suffix(".whisper.json"), speech)
            source_cues = [{"index": cue.index, "start_ms": cue.start, "end_ms": cue.end, "text": cue.text} for cue in speech]
            if evidence.get("source_cues") != source_cues:
                raise ValueError("Word timestamps belong to different or unverified speech text")
            words = evidence["word_times"]
            word_validation = {"source": str(word_path), "status": evidence.get("timing_evidence_status", "supplied"),
                               "coordinate_issues": evidence.get("coordinate_issues", []),
                               "native_json": str(Path(context["jp"]).with_suffix(".whisper.json"))}
        independent = None
        if independent_enabled:
            independent = run_external_alignment(independent_settings, args.config.parent, Path(context["audio"]),
                                                  Path(context["jp"]), Path(context["segments"]).with_suffix(".forced_words.json"))
            word_validation = {"source": str(Path(context["segments"]).with_suffix(".forced_words.json")),
                               "status": independent["timing_evidence_status"],
                               "kind": independent["timestamp_kind"],
                               "unusable_token_count": len(independent["unusable_tokens"]),
                               "sdk_repaired_unit_count": independent["sdk_repaired_unit_count"],
                               "native_evidence": word_validation}
        elif word_path.is_file():
            word_validation["unusable_token_count"] = len(evidence.get("unusable_tokens", []))
        supplied_identity = args.zh_file.with_suffix(".segments.json") if args.zh_file else None
        legacy = bool(args.zh_file and not supplied_identity.is_file())
        if legacy:
            records = build_segments(speech, None, None, "speech")
            visual_records = build_segments(speech, visual, words, context["timing_mode"], independent_evidence=independent) if visual else None
            records["mode"] = context["timing_mode"]
            records["legacy_supplied_translation"] = True
            if visual_records:
                records["visual_events"] = visual_records["visual_events"]
                records["unresolved_visual_events"] = visual_records["visual_events"]
            for item in records["segments"]:
                item["flags"] = ["SUPPLIED_TRANSLATION_HAS_NO_SEGMENT_IDS", "CHECK"]
            records["needs_review"] = len(records["segments"])
        else:
            records = build_segments(speech, visual, words, context["timing_mode"], independent_evidence=independent)
        records["word_timing_validation"] = word_validation
        audit_segment_records(records)
        atomic_write_json(Path(context["segments"]), records)
        atomic_write_text(Path(context["translation_input"]), render_source_srt(records))

    def validate_segments() -> dict[str, ValidationResult]:
        record_result = validate_json(Path(context["segments"]), required_keys=("segments", "parents", "coverage"))
        source_result = validate_subtitle(Path(context["translation_input"]), "segmented_source",
                                          require_text=True, expected_language="ja",
                                          media_duration_seconds=context.get("media_duration_seconds"))
        if record_result.valid and source_result.valid:
            try:
                records = json.loads(Path(context["segments"]).read_text(encoding="utf-8-sig"))
                audit_segment_records(records)
                actual_parents = parse_subtitle(Path(context["jp"]))[2]
                if [(item["index"], item["start_ms"], item["end_ms"], item["text"]) for item in records["parents"]] != [
                    (cue.index, cue.start, cue.end, cue.text) for cue in actual_parents
                ]:
                    raise ValueError("Canonical parents differ from the original speech transcript")
                if independent_enabled:
                    evidence_path = Path(context["segments"]).with_suffix(".forced_words.json")
                    independent = json.loads(evidence_path.read_text(encoding="utf-8-sig"))
                    validate_independent_evidence(independent, actual_parents, Path(context["audio"]))
                    if not records.get("legacy_supplied_translation") and records.get("independent_alignment") != independent:
                        raise ValueError("Canonical independent alignment differs from audited audio evidence")
                cues = parse_subtitle(Path(context["translation_input"]))[2]
                if len(cues) != len(records["segments"]) or any(
                    cue.index != item["index"] or cue.start != item["start_ms"] or cue.end != item["end_ms"] or
                    cue.text != normalize_text(item["text"]) for cue, item in zip(cues, records["segments"])
                ):
                    raise ValueError("Canonical SRT differs from segment identities and source text")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                record_result.fail(str(exc))
        return {"segments": record_result, "translation_input": source_result}

    execute_stage(
        task, "SEGMENTATION", component="visual and speech segmentation",
        action=prepare_segments,
        validate=validate_segments,
        inputs=lambda: {"jp": context["jp"], "timeline": context["timeline"] if axis_stage_enabled else "",
                        "word_times": str(Path(context["jp"]).with_suffix(".word_times.json")),
                        "native_words": str(Path(context["jp"]).with_suffix(".whisper.json")),
                        "supplied_translation": str(args.zh_file or ""),
                        "alignment_audio": context["audio"] if independent_enabled else ""},
        outputs=lambda: {"segments": context["segments"], "translation_input": context["translation_input"]},
        cache_inputs=lambda: dependencies("SEGMENTATION"),
    )

    if not full_output:
        skip_pipeline_stage(
            "TRANSLATION",
            component="Sakura",
            reason="已选择仅生成日文字幕",
        )
        japanese_output = job_dir / "deliverables" / "japanese.srt"
        context["final"] = str(japanese_output)
        context["zh"] = ""
        context["review"] = ""
        context["jianying_ui_enabled"] = False

        def publish_japanese() -> None:
            print(f"[output] Japanese subtitle: {japanese_output}")
            if args.dry_run:
                return
            source_text = Path(context["translation_input"]).read_text(encoding="utf-8-sig")
            atomic_write_text(japanese_output, source_text)
            write_job_manifest(job_dir, context, args)

        def validate_japanese_output() -> dict[str, ValidationResult]:
            return {
                "japanese": validate_subtitle(
                    japanese_output,
                    "japanese_output",
                    require_text=True,
                    expected_language="ja",
                    media_duration_seconds=context.get("media_duration_seconds"),
                ),
                "job_manifest": validate_json(
                    job_dir / "job_manifest.json",
                    required_keys=("job_dir", "source", "subtitles"),
                ),
            }

        execute_stage(
            task,
            "FINAL_OUTPUT",
            component="Japanese subtitle publish",
            action=publish_japanese,
            validate=validate_japanese_output,
            inputs=lambda: {"jp": context["translation_input"], "segments": context["segments"]},
            outputs=lambda: {
                "final": str(japanese_output),
                "job_manifest": str(job_dir / "job_manifest.json"),
            },
            cache_inputs=lambda: dependencies("FINAL_OUTPUT"),
        )
        skip_pipeline_stage(
            "NLE_EXPORT",
            component="Jianying bridge",
            reason="仅日文模式不生成剪映合轴包",
            outputs={"job_manifest": str(job_dir / "job_manifest.json")},
        )
        print(f"[done] job_dir={job_dir}")
        return 0

    def translate_segments() -> None:
        if args.zh_file:
            run_or_wait("translate_zh", args.zh_file, "zh")
            supplied_metadata = args.zh_file.with_suffix(".segments.json")
            if not args.dry_run and supplied_metadata.is_file():
                target = Path(context["zh"])
                shutil.copy2(supplied_metadata, target.with_suffix(".segments.json"))
                retime_supplied_translation(args.zh_file, supplied_metadata, target, Path(context["segments"]))
        elif not args.skip_external:
            run_command("translate_zh", config["commands"].get("translate_zh", ""),
                        {**context, "jp": context["translation_input"]}, args.dry_run)
        elif not args.dry_run:
            print("[skip] translate_zh: --skip-external")
        target = Path(context["zh"])
        if not args.dry_run and args.wait_missing and not (target.is_file() and target.stat().st_size > 0):
            wait_for_file("translate_zh", target, args.wait_timeout)

    def validate_translation() -> dict[str, ValidationResult]:
        jp_result = validate_subtitle(Path(context["translation_input"]), "segmented_source", require_text=True, expected_language="ja")
        zh_result = validate_subtitle(
            Path(context["zh"]),
            "zh_machine",
            require_text=True,
            expected_language="zh",
            expected_cues=jp_result.cue_count or None,
            media_duration_seconds=context.get("media_duration_seconds"),
        )
        if zh_result.valid:
            translated_cues = parse_subtitle(Path(context["zh"]))[2]
            source_cues = parse_subtitle(Path(context["translation_input"]))[2]
            if len(source_cues) != len(translated_cues) or any(
                (source.index, source.start, source.end) != (translated.index, translated.start, translated.end)
                for source, translated in zip(source_cues, translated_cues)
            ):
                zh_result.fail("Translation does not preserve every canonical segment identity and timestamps")
            metadata_path = Path(context["zh"]).with_suffix(".segments.json")
            if metadata_path.is_file():
                try:
                    records = json.loads(Path(context["segments"]).read_text(encoding="utf-8-sig"))
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
                    entries = metadata["translations"]
                    if len(entries) != len(records["segments"]) or any(
                        entry["cue_id"] != item["id"] or entry["index"] != cue.index or
                        entry["text"] != cue.text or entry["start_ms"] != cue.start or entry["end_ms"] != cue.end
                        for entry, item, cue in zip(entries, records["segments"], translated_cues)
                    ):
                        raise ValueError("Translation identity evidence differs from the canonical source or output")
                except (ValueError, KeyError, TypeError) as exc:
                    zh_result.fail(str(exc))
            elif not args.zh_file and "voicetransl_adapter.py" in config["commands"].get("translate_zh", ""):
                zh_result.fail("VoiceTransl did not publish canonical translation identities")
        atomic_write_json(Path(context["zh"]).parent / "zh_validation.json", zh_result.to_dict())
        return {"jp": jp_result, "zh": zh_result}

    execute_stage(
        task,
        "TRANSLATION",
        component="Sakura",
        action=translate_segments,
        validate=validate_translation,
        inputs=lambda: {"jp": context["translation_input"], "segments": context["segments"], "supplied": str(args.zh_file or "")},
        outputs=lambda: {"zh": context["zh"], "translation_ids": str(Path(context["zh"]).with_suffix(".segments.json"))},
        cache_inputs=lambda: dependencies("TRANSLATION"),
    )

    def validate_final() -> dict[str, ValidationResult]:
        timeline_result = validate_subtitle(Path(context["translation_input"]), "segmented_source", require_text=True)
        final_result = validate_subtitle(
            Path(context["final"]),
            "final",
            require_text=True,
            expected_language="zh",
            expected_cues=timeline_result.cue_count or None,
            media_duration_seconds=context.get("media_duration_seconds"),
        )
        review_result = validate_review_csv(Path(context["review"]), minimum_rows=max(1, timeline_result.cue_count), require_provenance=True)
        results = {"timeline": timeline_result, "final": final_result, "review": review_result}
        atomic_write_json(Path(context["final"]).parent / "final_validation.json", validation_map(results))
        return results

    execute_stage(
        task,
        "FINAL_OUTPUT",
        component="subtitle_axis_align",
        action=lambda: run_alignment(config, context, args.dry_run),
        validate=validate_final,
        inputs=lambda: {"segments": context["segments"], "jp": context["translation_input"], "zh": context["zh"],
                        "translation_ids": str(Path(context["zh"]).with_suffix(".segments.json"))},
        outputs=lambda: {"final": context["final"], "review": context["review"]},
        cache_inputs=lambda: dependencies("FINAL_OUTPUT"),
    )

    if not args.dry_run:
        quality = dict(validate_review_csv(Path(context["review"]), require_provenance=True).details)
        glossary_path = resolve_config_path(config.get("resources", {}).get("translation_glossary", ""), args.config)
        glossary = glossary_pairs(glossary_path) if glossary_path.is_file() else {}
        jp_cues = parse_subtitle(Path(context["translation_input"]))[2]
        zh_cues = parse_subtitle(Path(context["zh"]))[2]
        term_issues = [{"line": index, "missing_terms": missing_required_terms(original.text, translated.text, glossary)}
                       for index, (original, translated) in enumerate(zip(jp_cues, zh_cues), 1)]
        term_issues = [issue for issue in term_issues if issue["missing_terms"]]
        translation_metadata_path = Path(context["zh"]).with_suffix(".segments.json")
        translation_metadata = json.loads(translation_metadata_path.read_text(encoding="utf-8-sig")) if translation_metadata_path.exists() else {}
        if "term_review" in translation_metadata:
            term_issues = translation_metadata["term_review"]
        if "language_review" in translation_metadata:
            language_issues = translation_metadata["language_review"]
        else:
            approved_targets = [resolved_term_target(source, "", glossary) for source in glossary]
            language_issues = [{"line": cue.index, "text": cue.text, "reason": "Japanese letters remain outside dictionary-approved terms"}
                               for cue in zh_cues if has_unreviewed_kana(cue.text, approved_targets)]
        import csv
        with Path(context["review"]).open(encoding="utf-8-sig", newline="") as handle:
            report_rows = list(csv.DictReader(handle))
        used_zh = {int(source_id) for row in report_rows for source_id in row["zh_indices"].split(",") if source_id}
        unmapped = [cue.index for cue in zh_cues if cue.index not in used_zh]
        segmentation = json.loads(Path(context["segments"]).read_text(encoding="utf-8-sig"))
        segmentation_review = segmentation["needs_review"]
        word_timing_unusable = segmentation.get("word_timing_validation", {}).get("status") == "unusable"
        quality["word_timing_unusable"] = word_timing_unusable
        word_timing_issue_count = segmentation.get("word_timing_validation", {}).get("unusable_token_count", 0)
        quality["word_timing_issue_count"] = word_timing_issue_count
        quality["word_timing_validation"] = segmentation.get("word_timing_validation", {})
        missing_chars = sum(len(item["missing_chars"]) for item in segmentation["coverage"])
        quality.update({"segmentation_review": segmentation_review,
                        "uncovered_source_characters": missing_chars,
                        "unresolved_visual_events": len(segmentation["unresolved_visual_events"])})
        artifact_warnings = {key: result.warnings for key, result in {
            "jp": validate_asr()["jp"],
            "zh": validate_subtitle(Path(context["zh"]), "zh_machine", require_text=True, expected_language="zh"),
            **validate_final(),
        }.items() if result.warnings}
        quality["validation_warnings"] = artifact_warnings
        quality.update({"term_issue_count": len(term_issues), "term_issues": term_issues,
                        "translation_language_issue_count": len(language_issues), "translation_language_issues": language_issues,
                        "unmapped_source_count": len(unmapped), "unmapped_zh_indices": unmapped,
                        "status": "needs_review" if quality.get("needs_review") or segmentation_review or missing_chars or term_issues or language_issues or unmapped or artifact_warnings or word_timing_unusable or word_timing_issue_count else "ready"})
        context["quality_summary"] = quality
        atomic_write_json(job_dir / "deliverables" / "quality_summary.json", quality)
        print(f"[quality] alignment_review={quality.get('needs_review', 0)} term_review={len(term_issues)} language_review={len(language_issues)}")

    jianying_ui_enabled = args.jianying_ui if args.jianying_ui is not None else bool(config.get("jianying_ui", {}).get("enabled", False))
    context["jianying_ui_enabled"] = jianying_ui_enabled

    def export_nle() -> None:
        if not args.dry_run:
            write_import_note(job_dir, context)
        try:
            if not args.skip_jianying_package:
                run_jianying_bridge(config, context, args.jianying_draft, args.dry_run)
            if jianying_ui_enabled:
                run_jianying_ui_import(config, context, args.dry_run)
        finally:
            if not args.dry_run:
                write_job_manifest(job_dir, context, args)

    def validate_nle() -> dict[str, ValidationResult]:
        results = {
            "job_manifest": validate_json(job_dir / "job_manifest.json", required_keys=("job_dir", "source", "subtitles")),
            "import_note": validate_file(job_dir / "deliverables" / "import_to_jianying.txt", "import_note", min_bytes=16),
        }
        if not args.skip_jianying_package:
            results["package_manifest"] = validate_json(Path(context["jianying_import"]) / "manifest.json")
            if args.jianying_draft:
                results["package_status"] = validate_json(Path(context["jianying_status"]))
        if jianying_ui_enabled:
            results["ui_status"] = validate_json(Path(context.get("jianying_ui_status") or job_dir / "jianying_ui_status.json"),
                                                 required_keys=("state", "steps"), expected_values={"state": "completed"})
        return results

    execute_stage(
        task,
        "NLE_EXPORT",
        component="Jianying bridge",
        action=export_nle,
        validate=validate_nle,
        inputs=lambda: {"video": context["video"], "subtitle": context["final"]},
        cache_inputs=lambda: {**dependencies("NLE_EXPORT"), "ui_enabled": jianying_ui_enabled,
                              "skip_package": args.skip_jianying_package, "draft": args.jianying_draft,
                              "quality": context.get("quality_summary", {})},
        outputs=lambda: {
            "job_manifest": str(job_dir / "job_manifest.json"),
            "jianying_import": context.get("jianying_import", ""),
            "jianying_status": context.get("jianying_status", ""),
            "jianying_ui_status": context.get("jianying_ui_status", ""),
        },
    )

    print(f"[done] job_dir={job_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
