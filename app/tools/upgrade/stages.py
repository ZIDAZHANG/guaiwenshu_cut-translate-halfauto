#!/usr/bin/env python3
"""Stage implementations of work package W03: INPUT, MEDIA_PREP, OCR and ASR (plan 1.1, section 10.1).

Each stage declares the dependencies that decide its cache signature (plan 10.3) and writes its outputs into the
attempt directory handed over by the runner. Settings are read from the configuration snapshot frozen in the
request; only the local resource bindings (paths) come from the current ``resources.local.json``, and their content
identity is checked against the references frozen at submit time.

A missing or changed tool is reported before any engine starts (RESOURCE_MISSING / RESOURCE_CHANGED) and names
every affected role.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from upgrade import media, recognition
from upgrade.contracts import ContractError, new_id, parse_json_text, require_record
from upgrade.resource_catalog import inspect_resources, resolve_resource
from upgrade.runner import StageContext, update_project
from upgrade.storage import atomic_write_text, render_json

ROLE_DEFAULT_IDS = {"ffprobe": "ffprobe", "ffmpeg": "ffmpeg", "asr_engine": "whisper-cli", "asr_model": "whisper-model",
                    "vad_model": "whisper-vad-model", "ocr_engine": "magia-timeline"}
ROLE_KINDS = {"ffprobe": "ffprobe", "ffmpeg": "ffmpeg", "asr_engine": "asr_engine", "asr_model": "model", "vad_model": "model", "ocr_engine": "ocr_engine"}
FULL_FRAME_ROI = {"left": 0.0, "top": 0.0, "right": 1.0, "bottom": 1.0}
SPEECH_MODES = ("combined", "speech_only")
VISUAL_MODES = ("combined", "visual_only")


# --------------------------------------------------------------------------- settings and resources

def frozen_settings(request: dict) -> dict:
    settings = request["config_snapshot"]["engine_settings"]
    return settings if isinstance(settings, dict) else {}


def _section(settings: dict, name: str) -> dict:
    value = settings.get(name)
    return value if isinstance(value, dict) else {}


def roles_for(request: dict) -> list[str]:
    """Every tool/model role this request needs before it reaches the SOURCE stage."""
    mode = request["recognition_mode"]
    roles = ["ffprobe", "ffmpeg"]
    if mode in VISUAL_MODES:
        roles.append("ocr_engine")
    if mode in SPEECH_MODES:
        roles += ["asr_engine", "asr_model"]
        if bool(_section(frozen_settings(request), "recognition").get("vad", True)):
            roles.append("vad_model")
    return roles


def resolve_roles(config: dict, request: dict, roles: list[str], *, report: dict | None = None) -> dict[str, dict]:
    """Bind roles to declared resources. ``engine_settings.resources`` may map a role to another resource id."""
    bindings = _section(frozen_settings(request), "resources")
    report = report or inspect_resources(config, probe=False)
    frozen = {ref["resource_id"]: ref for ref in request["resource_refs"]}
    resolved: dict[str, dict] = {}
    failures: list[dict] = []
    for role in roles:
        resource_id = str(bindings.get(role) or ROLE_DEFAULT_IDS[role])
        try:
            item = resolve_resource(resource_id, report)
        except ContractError as exc:
            failures.append({"role": role, "resource_id": resource_id, "code": exc.code, "message": exc.message})
            continue
        if item["manifest"]["kind"] != ROLE_KINDS[role]:
            failures.append({"role": role, "resource_id": resource_id, "code": "RESOURCE_MISSING",
                             "message": f"resource is declared as {item['manifest']['kind']}, the role needs {ROLE_KINDS[role]}"})
            continue
        submitted = frozen.get(resource_id)
        if submitted and submitted["content_sha256"] != item["resource_ref"]["content_sha256"]:
            failures.append({"role": role, "resource_id": resource_id, "code": "RESOURCE_CHANGED",
                             "message": "content differs from the identity frozen when the request was submitted",
                             "expected": submitted["content_sha256"], "observed": item["resource_ref"]["content_sha256"]})
            continue
        resolved[role] = item
    if failures:
        code = "RESOURCE_CHANGED" if all(item["code"] == "RESOURCE_CHANGED" for item in failures) else "RESOURCE_MISSING"
        names = ", ".join(f"{item['role']} ({item['resource_id']})" for item in failures)
        raise ContractError(code, f"required resources are missing or changed: {names}; no engine was started", details={"failures": failures})
    return resolved


def _roles(ctx: StageContext, roles: list[str]) -> dict[str, dict]:
    """Resolve once per request run; the catalog inspection is shared between stages through ``ctx.scratch``."""
    if "resource_report" not in ctx.scratch:
        ctx.scratch["resource_report"] = inspect_resources(ctx.config, probe=False)
    return resolve_roles(ctx.config, ctx.request, roles, report=ctx.scratch["resource_report"])


def _identity(resource: dict) -> str:
    return resource["resource_ref"]["content_sha256"]


def _read_json(path: Path) -> dict:
    value = parse_json_text(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ContractError("SCHEMA_INVALID", "stage output is not a JSON object", details={"path": str(path)})
    return value


def _media_runtime(ctx: StageContext, roles: dict[str, dict]) -> media.MediaRuntime:
    settings = frozen_settings(ctx.request)
    return media.MediaRuntime(project_dir=ctx.project_dir, ffprobe=roles["ffprobe"], ffmpeg=roles.get("ffmpeg"), log_dir=ctx.attempt_dir / "logs",
                              owner_token=ctx.owner_token, cancel=ctx.cancel, emit=ctx.emit, media_settings=_section(settings, "media"),
                              download_settings=_section(settings, "download"))


def _recognition_runtime(ctx: StageContext, roles: dict[str, dict]) -> recognition.RecognitionRuntime:
    app_config = ctx.request["config_snapshot"]["app_config"]
    return recognition.RecognitionRuntime(project_dir=ctx.project_dir, output_dir=ctx.attempt_dir, ascii_temp_root=Path(app_config["ascii_temp_root"]),
                                          resources=roles, owner_token=ctx.owner_token, cancel=ctx.cancel, emit=ctx.emit,
                                          capability_id=str(app_config["defaults"]["gpu_profile"]))


def _audio_none(request: dict) -> bool:
    return request["extensions"].get("audio_stream") == "none"


# --------------------------------------------------------------------------- INPUT

class InputStage:
    """Register (download if needed) and probe the media; check stream selection and the mode's audio requirement."""
    name = "INPUT"

    def signature_inputs(self, ctx: StageContext) -> dict:
        request = ctx.request
        job_input = request["input"]
        stat = None
        if job_input["kind"] == "local_video" and not job_input["source_media_id"]:
            path = Path(job_input["value"])
            if path.is_file():
                found = path.stat()
                stat = {"bytes": found.st_size, "mtime_ns": found.st_mtime_ns}
        return {"input": job_input, "video_stream_index": request["video_stream_index"], "audio_stream_index": request["audio_stream_index"],
                "audio_none": _audio_none(request), "copy_original": bool(request["extensions"].get("copy_original")), "stat": stat,
                "needs_audio": request["recognition_mode"] in SPEECH_MODES,
                "download": _section(frozen_settings(request), "download") if job_input["kind"] == "url" else None,
                "ffprobe": _identity(_roles(ctx, ["ffprobe"])["ffprobe"]), "mapping_version": media.MAPPING_VERSION}

    def run(self, ctx: StageContext) -> dict:
        request = ctx.request
        roles = _roles(ctx, roles_for(request))  # complete pre-check: nothing long starts with a missing engine
        runtime = _media_runtime(ctx, roles)
        job_input = request["input"]
        if (request["recognition_mode"] == "supplied_source" and not job_input["source_media_id"] and job_input["kind"] == "local_video"
                and not Path(job_input["value"]).is_file()):
            raise ContractError("INPUT_MISSING", "this supplied_source request names no media file; give the video with the request "
                                "(media taken from a registered source revision is not available in this build)",
                                details={"source_revision_id": request["source_revision_id"], "input": job_input["value"]})
        extensions: dict[str, Any] = {}
        if _audio_none(request):
            extensions["audio_stream"] = "none"
        if request["extensions"].get("copy_original"):
            extensions["copy_original"] = True
        if job_input["kind"] == "url" and not job_input["source_media_id"]:
            download = media.download_media(job_input["value"], runtime=runtime)
            extensions["local_path"] = download["local_path"]
            extensions["origin"] = download["origin"]
            atomic_write_text(ctx.attempt_dir / "download.json", render_json(download))
        media_input = require_record("MediaInput", {"input": job_input, "selected_video_stream": request["video_stream_index"],
                                                    "selected_audio_stream": request["audio_stream_index"], "project_id": request["project_id"],
                                                    "extensions": extensions})
        ctx.check_cancelled()
        record = media.probe_media(media_input, runtime=runtime)
        atomic_write_text(ctx.attempt_dir / "media_input.json", render_json(media_input))
        atomic_write_text(ctx.attempt_dir / "media_record.json", render_json(record))
        media_id = record["media_id"]
        update_project(ctx.data_root, request["project_id"], lambda project: project["media_ids"].append(media_id) if media_id not in project["media_ids"] else None)
        if request["recognition_mode"] in SPEECH_MODES and record["selected_audio_stream"] is None:
            raise ContractError("NEEDS_SELECTION", "this media has no selected audio stream; choose the visual_only mode for it (the mode is not changed silently)",
                                details={"kind": "no_audio_stream", "media_id": media_id, "recognition_mode": request["recognition_mode"]})
        return {"ok": True, "media_id": media_id, "source_sha256": record["source_sha256"], "duration_ms": record["duration_ms"],
                "clock_verified": record["clock"]["verified"], "clock_problem": record["extensions"].get("clock_problem"),
                "selected_video_stream": record["selected_video_stream"], "selected_audio_stream": record["selected_audio_stream"],
                "frame_timing": record["extensions"].get("frame_timing"), "unsupported_profile": record["extensions"].get("unsupported_profile")}


# --------------------------------------------------------------------------- MEDIA_PREP

class MediaPrepStage:
    """Time base check and the necessary derivatives: review/recognition video and the ASR WAV (only when the mode needs audio)."""
    name = "MEDIA_PREP"

    def signature_inputs(self, ctx: StageContext) -> dict:
        record = _read_json(ctx.output_dir("INPUT") / "media_record.json")
        roles = _roles(ctx, ["ffprobe", "ffmpeg"])
        mode = ctx.request["recognition_mode"]
        settings = _section(frozen_settings(ctx.request), "media")
        return {"source_sha256": record["source_sha256"], "video_stream": record["selected_video_stream"], "audio_stream": record["selected_audio_stream"],
                "needs_audio": mode in SPEECH_MODES, "needs_visual": mode in VISUAL_MODES,
                "media_settings": {key: settings.get(key) for key in ("auto_convert", "preset", "crf", "accepted_containers", "accepted_video_codecs")},
                "ffprobe": _identity(roles["ffprobe"]), "ffmpeg": _identity(roles["ffmpeg"]), "profile": media.COMPAT_PROFILE_ID,
                "mapping_version": media.MAPPING_VERSION}

    def run(self, ctx: StageContext) -> dict:
        record = _read_json(ctx.output_dir("INPUT") / "media_record.json")
        roles = _roles(ctx, ["ffprobe", "ffmpeg"])
        mode = ctx.request["recognition_mode"]
        plan = require_record("MediaPlan", {
            "media_id": record["media_id"], "needs_visual": mode in VISUAL_MODES, "needs_audio": mode in SPEECH_MODES,
            "compatibility_profile": media.COMPAT_PROFILE_ID, "run_id": ctx.task["run_id"], "attempt_id": ctx.attempt_id or new_id(),
            "extensions": {"selected_audio_stream": record["selected_audio_stream"]}})
        result = media.prepare_media(plan, runtime=_media_runtime(ctx, roles))
        atomic_write_text(ctx.attempt_dir / "media_plan.json", render_json(plan))
        atomic_write_text(ctx.attempt_dir / "media_record.json", render_json(result["media_record"]))
        atomic_write_text(ctx.attempt_dir / "prepare_result.json", render_json({key: result[key] for key in ("video_for_review", "audio_for_asr", "derivation_refs")}))
        audio = result["audio_for_asr"]
        return {"ok": True, "media_id": record["media_id"], "direct_usable": result["media_record"]["extensions"]["direct_usable"],
                "derivative_reasons": result["media_record"]["extensions"]["derivative_reasons"],
                "video_for_review": {"kind": result["video_for_review"]["kind"], "media_revision": result["video_for_review"]["media_revision"]},
                "audio_for_asr": {"media_revision": audio["media_revision"], "output_duration_ms": audio["output_duration_ms"]} if audio else None,
                "clock_mapping": result["media_record"]["clock"]["mapping_kind"]}


# --------------------------------------------------------------------------- OCR / ASR

def _recognition_request(ctx: StageContext, mode: str, prepared: dict) -> dict:
    settings = frozen_settings(ctx.request)
    record = _read_json(ctx.output_dir("MEDIA_PREP") / "media_record.json")
    roi = _section(settings, "ocr").get("roi") or FULL_FRAME_ROI
    audio = prepared["audio_for_asr"]
    return require_record("RecognitionRequest", {
        "run_id": ctx.task["run_id"], "media_id": record["media_id"], "recognition_mode": mode,
        "video_artifact": prepared["video_for_review"]["ref"] if mode == "visual_only" else None,
        "audio_artifact": audio["ref"] if (mode == "speech_only" and audio) else None,
        "roi": roi, "engine_settings": settings, "resource_refs": ctx.request["resource_refs"]})


class OcrStage:
    """On-screen text with the existing MagiaTimeline engine -> VisualEvidence."""
    name = "OCR"

    def signature_inputs(self, ctx: StageContext) -> dict:
        prepared = _read_json(ctx.output_dir("MEDIA_PREP") / "prepare_result.json")
        settings = _section(frozen_settings(ctx.request), "ocr")
        return {"video_identity": prepared["video_for_review"]["video_identity"],
                "ocr": {"strategy": settings.get("strategy", "dtd"), "preset": settings.get("preset", "default"), "engine": settings.get("engine", "speculative"),
                        "max_resolution": settings.get("max_resolution", 0), "ocr": settings.get("ocr", True), "roi": settings.get("roi") or FULL_FRAME_ROI},
                "engine": _identity(_roles(ctx, ["ocr_engine"])["ocr_engine"]), "adapter_version": recognition.ADAPTER_VERSION}

    def run(self, ctx: StageContext) -> dict:
        prepared = _read_json(ctx.output_dir("MEDIA_PREP") / "prepare_result.json")
        roles = _roles(ctx, ["ocr_engine"])
        result = recognition.recognize(_recognition_request(ctx, "visual_only", prepared), runtime=_recognition_runtime(ctx, roles))
        evidence = result["visual_evidence"]
        atomic_write_text(ctx.attempt_dir / "visual_evidence.json", render_json(evidence))
        atomic_write_text(ctx.attempt_dir / "recognition_result.json", render_json({"skip_reasons": result["skip_reasons"], "issues": result["issues"]}))
        return {"ok": True, "evidence_id": evidence["evidence_id"], "events": len(evidence["events"]),
                "text_events": evidence["extensions"]["text_event_count"], "evidence_level": evidence["evidence_level"],
                "empty": evidence["extensions"]["visual_empty"], "issue_codes": [issue["code"] for issue in result["issues"]]}


class AsrStage:
    """Speech with the existing whisper.cpp engine -> SpeechEvidence (serial, after OCR)."""
    name = "ASR"

    def signature_inputs(self, ctx: StageContext) -> dict:
        prepared = _read_json(ctx.output_dir("MEDIA_PREP") / "prepare_result.json")
        if not prepared["audio_for_asr"]:
            raise ContractError("NEEDS_SELECTION", "speech recognition needs an audio stream; none was prepared", details={"kind": "no_audio_stream"})
        settings = _section(frozen_settings(ctx.request), "recognition")
        roles = _roles(ctx, [role for role in roles_for(ctx.request) if role in ("asr_engine", "asr_model", "vad_model")])
        return {"audio_sha256": prepared["audio_for_asr"]["ref"]["sha256"],
                "recognition": {"language": settings.get("language", "ja"), "vad": settings.get("vad", True),
                                "word_timestamps": settings.get("word_timestamps", True), "dtw_model": settings.get("dtw_model")},
                "engine": _identity(roles["asr_engine"]), "model": _identity(roles["asr_model"]),
                "vad_model": _identity(roles["vad_model"]) if "vad_model" in roles else None, "adapter_version": recognition.ADAPTER_VERSION}

    def run(self, ctx: StageContext) -> dict:
        prepared = _read_json(ctx.output_dir("MEDIA_PREP") / "prepare_result.json")
        roles = _roles(ctx, [role for role in roles_for(ctx.request) if role in ("asr_engine", "asr_model", "vad_model")])
        result = recognition.recognize(_recognition_request(ctx, "speech_only", prepared), runtime=_recognition_runtime(ctx, roles))
        evidence = result["speech_evidence"]
        atomic_write_text(ctx.attempt_dir / "speech_evidence.json", render_json(evidence))
        atomic_write_text(ctx.attempt_dir / "recognition_result.json", render_json({"skip_reasons": result["skip_reasons"], "issues": result["issues"]}))
        return {"ok": True, "evidence_id": evidence["evidence_id"], "parents": len(evidence["parents"]), "words": len(evidence["word_times"]),
                "word_timing_status": evidence["extensions"]["word_timing_status"], "selected_attempt": evidence["extensions"]["selected_attempt"],
                "empty": evidence["extensions"]["asr_empty"], "issue_codes": [issue["code"] for issue in result["issues"]]}


# --------------------------------------------------------------------------- submit-time stream check

def preflight_streams(config: dict, request_like: dict, video: Path, *, mode: str, video_stream: int | None, audio_stream: int | None,
                      audio_none: bool) -> dict:
    """Resolve the stream selection of a local file before it is queued (plan 12.1: NEEDS_SELECTION instead of guessing).

    Returns ``{"checked": bool, "video_stream": int|None, "audio_stream": int|None, "warning": str|None}``. Without a
    registered ffprobe nothing is probed here; the INPUT stage reports the missing resource when the queue runs.
    """
    try:
        roles = resolve_roles(config, request_like, ["ffprobe"])
    except ContractError as exc:
        return {"checked": False, "video_stream": video_stream, "audio_stream": audio_stream,
                "warning": f"stream selection was not checked at submit time: {exc.message}"}
    with tempfile.TemporaryDirectory(prefix="roast-preflight-") as temp:
        runtime = media.MediaRuntime(project_dir=Path(temp), ffprobe=roles["ffprobe"], log_dir=Path(temp) / "logs", owner_token="preflight")
        _, raw = media.probe_format_streams(video, runtime)
    selected_video, selected_audio = media.select_streams(media.normalize_streams(raw), requested_video=video_stream, requested_audio=audio_stream,
                                                          audio_none=audio_none)
    if mode in SPEECH_MODES and selected_audio is None:
        raise ContractError("NEEDS_SELECTION", "this file has no usable audio stream for the chosen mode; use --mode visual_only",
                            details={"kind": "no_audio_stream", "recognition_mode": mode})
    return {"checked": True, "video_stream": selected_video, "audio_stream": selected_audio, "warning": None}


__all__ = ["ROLE_DEFAULT_IDS", "ROLE_KINDS", "roles_for", "resolve_roles", "frozen_settings", "InputStage", "MediaPrepStage", "OcrStage", "AsrStage",
           "preflight_streams"]
