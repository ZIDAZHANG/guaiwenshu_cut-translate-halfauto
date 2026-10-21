#!/usr/bin/env python3
"""Use VoiceTransl's bundled Whisper and Sakura model from the roast pipeline.

Thin orchestration layer: recognition, translation workflows and the CLI.
Dictionaries/terms live in voice_dictionary, the Sakura client in sakura_client,
checkpointing in translation_checkpoint, and generic helpers in voice_common.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from artifact_validation import validate_subtitle
from subtitle_axis_align import normalize_text, parse_srt, read_text
from task_state import atomic_write_json, atomic_write_text
from subtitle_segments import load_whisper_word_times
from translation_checkpoint import (
    load_translation_checkpoint,
    save_translation_checkpoint,
    save_translation_progress,
    translation_signature,
)
from voice_common import (
    component_log_dir,
    configure_utf8_stdio,
    decode_console_line,
    failed_candidate_path,
    file_sha256,
    hidden_process_kwargs,
    load_json,
    load_pipeline_config,
    preserve_failed_candidate,
    render_srt,
    stream_process,
)
from voice_dictionary import (
    NativeDictionaryEntry,
    apply_native_dictionary,
    effective_native_prompt,
    ensure_required_terms,
    missing_required_terms,
    has_unreviewed_kana,
    glossary_pairs,
    load_native_dictionary,
    native_file_signature,
    native_glossary_prompt,
    normalize_glossary,
    process_native_escape,
    protect_terms,
    resolve_config_path,
    resolved_term_target,
)
from sakura_client import (
    endpoint_ready,
    extract_json,
    http_json,
    parse_translations,
    start_server,
    stop_server,
    translate_batch,
    translation_response_schema,
    wait_for_endpoint,
)

#: How often translation_progress.json is refreshed during long runs (seconds).
PROGRESS_WRITE_INTERVAL_SECONDS = 10.0

def recognize(args: argparse.Namespace) -> int:
    cfg = load_pipeline_config(args.config)
    settings = cfg.get("recognition", {})
    executable_value = args.executable or str(settings.get("executable", "")).strip()
    if not executable_value:
        raise SystemExit("Whisper is not configured. Select the VoiceTransl installation folder in the desktop app.")
    executable = Path(executable_value)
    model = args.model or Path(str(settings.get("model", executable.parent / "ggml-large-v3.bin")))
    language = args.language or str(settings.get("language", "ja"))
    vad_enabled = settings.get("vad", True) if args.vad is None else args.vad
    audio = args.audio.resolve()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    for required in (executable, model, audio):
        if not required.exists():
            raise SystemExit(f"required file does not exist: {required}")
    if "faster" in str(settings.get("engine", "")).lower():
        raise SystemExit("Faster Whisper needs a CTranslate2 model. Select VoiceTransl Whisper for the bundled ggml model.")

    candidate = output.with_name(f".{output.stem}.{uuid.uuid4().hex}.tmp.srt")
    output_base = candidate.with_suffix("")
    command = [
        str(executable),
        "-m",
        str(model),
        "-osrt",
        "-l",
        language,
        str(audio),
        "-of",
        str(output_base),
    ]
    word_timestamps = bool(settings.get("word_timestamps", False))
    if word_timestamps:
        dtw_model = str(settings.get("dtw_model", "")).strip()
        if not dtw_model and "large-v3" in model.name.lower():
            dtw_model = "large.v3"
        if not dtw_model:
            raise SystemExit("Word timestamps require recognition.dtw_model for the selected Whisper model")
        command.extend(["-ojf", "-dtw", dtw_model])
    if vad_enabled:
        configured_vad = str(settings.get("vad_model", "") or "").strip()
        vad_model = resolve_config_path(configured_vad, args.config) if configured_vad else executable.parent / "ggml-silero-v5.1.2.bin"
        if not vad_model.is_file():
            raise SystemExit(f"VAD is enabled but its model does not exist: {vad_model}; select a VAD model or disable VAD")
        command.extend(["--vad", "--vad-model", str(vad_model)])

    print(f"[voice] Whisper source: {audio}")
    print(f"[voice] Whisper model: {model.name}")
    code = stream_process(command, executable.parent.parent, component_log_dir(output) / "whisper.log")
    # whisper.cpp appends ".srt" to -of verbatim. output_base already ends
    # in ".tmp", so Path.with_suffix(".srt") would incorrectly drop ".tmp".
    generated = candidate
    if not generated.exists() or generated.stat().st_size == 0:
        outcome = f"exited with code {code}" if code != 0 else "returned success"
        raise SystemExit(f"Whisper {outcome} but did not create a non-empty SRT: {generated}")
    validation = validate_subtitle(generated, "jp_asr", require_text=True, expected_language="ja")
    validation.details["process_exit_code"] = code
    if code != 0:
        validation.fail(f"Whisper exited with code {code}; source coverage is unconfirmed")
        validation.details["source_coverage"] = "unconfirmed"
    word_evidence = None
    generated_json = Path(str(output_base) + ".json")
    if validation.valid and word_timestamps:
        try:
            word_evidence = load_whisper_word_times(generated_json, parse_srt(generated))
        except (OSError, ValueError, KeyError, UnicodeError) as exc:
            validation.fail(f"Whisper character timestamp evidence is invalid: {exc}")
        validation.details["word_timestamp_kind"] = "whisper_cpp_token_offsets"
        if word_evidence is not None:
            validation.details["word_timing_status"] = word_evidence["timing_evidence_status"]
            if word_evidence["timing_evidence_status"] == "unusable":
                validation.warnings.append("Native token time coordinates conflict with original utterance times; word timing is unusable")
                print("[voice] warning: native word times are unusable; original ASR utterance times retained")
    if validation.valid and vad_enabled and word_evidence is not None and word_evidence["timing_evidence_status"] == "unusable":
        # The bundled VAD can return restored utterance times with token clocks
        # still in compacted-audio coordinates. Re-run the same complete audio
        # with the same model; never guess, clamp, or interpolate a correction.
        attempts = component_log_dir(output) / "recognition_attempts" / uuid.uuid4().hex
        attempts.mkdir(parents=True)
        initial = attempts / "vad"
        initial.mkdir()
        shutil.copy2(generated, initial / "jp.srt")
        shutil.copy2(generated_json, initial / "jp.whisper.json")
        initial_evidence = dict(word_evidence, source_json=str(initial / "jp.whisper.json"))
        atomic_write_json(initial / "jp.word_times.json", initial_evidence)
        initial_log = component_log_dir(output) / "whisper.log"
        if initial_log.is_file():
            shutil.copy2(initial_log, initial / "whisper.log")
        atomic_write_json(initial / "validation.json", validation.to_dict())
        retry_folder = attempts / "without_vad"
        retry_folder.mkdir()
        retry_base = retry_folder / "jp"
        retry_command = command[:command.index("--vad")]
        retry_command[retry_command.index("-of") + 1] = str(retry_base)
        audit = {"state": "RUNNING", "reason": "NATIVE_TIME_COORDINATES_CONFLICT",
                 "initial_command": command, "recovery_command": retry_command,
                 "initial_source_sha256": file_sha256(generated),
                 "initial_json_sha256": file_sha256(generated_json),
                 "coordinate_issue_count": len(word_evidence["coordinate_issues"]),
                 "same_complete_audio": str(audio), "same_model": str(model), "selected": "vad"}
        atomic_write_json(attempts / "recovery.json", audit)
        print("[voice] native token clock conflicts: preserving VAD evidence and recognizing the same audio without VAD", flush=True)
        try:
            retry_code = stream_process(retry_command, executable.parent.parent, retry_folder / "whisper.log")
            retry_srt = retry_base.with_suffix(".srt")
            retry_json = retry_base.with_suffix(".json")
            retry_validation = validate_subtitle(retry_srt, "jp_asr", require_text=True, expected_language="ja")
            retry_validation.details["process_exit_code"] = retry_code
            if retry_code != 0:
                retry_validation.fail(f"Whisper without VAD exited with code {retry_code}; source coverage is unconfirmed")
            retry_evidence = None
            if retry_validation.valid:
                try:
                    retry_evidence = load_whisper_word_times(retry_json, parse_srt(retry_srt))
                except (OSError, ValueError, KeyError, UnicodeError) as exc:
                    retry_validation.fail(f"Whisper without VAD character timestamp evidence is invalid: {exc}")
            atomic_write_json(retry_folder / "validation.json", retry_validation.to_dict())
            if retry_evidence is not None:
                atomic_write_json(retry_folder / "jp.word_times.json", retry_evidence)
            if retry_validation.valid and retry_evidence is not None and retry_evidence["timing_evidence_status"] == "usable":
                # Copy the selected native files to a fresh publication candidate.
                # Keep both attempts unchanged in the audit folder.
                selected_base = output.parent / f".{output.stem}.{uuid.uuid4().hex}.tmp"
                generated = Path(str(selected_base) + ".srt")
                generated_json = Path(str(selected_base) + ".json")
                shutil.copy2(retry_srt, generated)
                shutil.copy2(retry_json, generated_json)
                validation, word_evidence = retry_validation, retry_evidence
                audit["selected"] = "without_vad"
                print("[voice] native word coordinates recovered without VAD; both complete attempts retained", flush=True)
            else:
                validation.warnings.append("Same-model recognition without VAD did not recover verified word coordinates; original utterances retained")
            audit.update(state="FINISHED", recovery_exit_code=retry_code,
                         recovery_valid=retry_validation.valid,
                         recovery_timing_status=retry_evidence["timing_evidence_status"] if retry_evidence else "unavailable")
            for label, path in (("recovery_source", retry_srt), ("recovery_json", retry_json)):
                if path.is_file():
                    audit[label + "_sha256"] = file_sha256(path)
        except BaseException as exc:
            audit.update(state="INTERRUPTED" if isinstance(exc, KeyboardInterrupt) else "FAILED",
                         error_type=type(exc).__name__, error=str(exc))
            raise
        finally:
            atomic_write_json(attempts / "recovery.json", audit)
        validation.details["native_clock_recovery"] = {"audit": str(attempts / "recovery.json"),
                                                       "selected": audit["selected"]}
        validation.details["word_timestamp_kind"] = "whisper_cpp_token_offsets"
        validation.details["word_timing_status"] = word_evidence["timing_evidence_status"]
    validation_path = output.parent / "jp_validation.json"
    if not validation.valid:
        evidence = preserve_failed_candidate(generated, output, "whisper")
        if generated_json.is_file():
            preserve_failed_candidate(generated_json, output.with_suffix(".whisper.json"), "whisper_tokens")
        if evidence is not None:
            validation.path = str(evidence.resolve())
        atomic_write_json(validation_path, validation.to_dict())
        outcome = f"exited with code {code} and" if code != 0 else "returned success but"
        raise SystemExit(
            f"Whisper {outcome} produced an invalid Japanese SRT: {validation.summary()}; "
            f"candidate preserved at {evidence}"
        )
    if word_evidence is not None:
        raw_evidence = output.with_suffix(".whisper.json")
        os.replace(generated_json, raw_evidence)
        word_evidence["source_json"] = str(raw_evidence)
        atomic_write_json(output.with_suffix(".word_times.json"), word_evidence)
    os.replace(generated, output)
    validation.path = str(output.resolve())
    atomic_write_json(validation_path, validation.to_dict())
    print(f"[voice] validated: {validation.summary()}")
    print(f"[voice] Japanese SRT completed: {output}")
    return 0


def _native_path(settings: dict, discovered_native: dict, setting_key: str, discovered_key: str, config_path: Path) -> Path:
    configured = str(settings.get(setting_key, "") or "").strip()
    value = configured or str(discovered_native.get(discovered_key, "") or "").strip()
    return resolve_config_path(value, config_path)


def _model_identity(path: Path) -> dict[str, object]:
    """Invalidate checkpoints when a same-named model file is replaced.

    Hashing a multi-gigabyte GGUF on every run would be wasteful, so the
    identity uses its resolved location, size and nanosecond mtime.
    """
    identity: dict[str, object] = {"name": path.name, "path": str(path)}
    try:
        stat = path.stat()
    except OSError:
        identity["exists"] = False
    else:
        identity.update({"exists": True, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
    return identity


def translate(args: argparse.Namespace) -> int:
    cfg = load_pipeline_config(args.config)
    settings = cfg.get("translation", {})
    resources = cfg.get("resources", {})
    external_tools = cfg.get("external_tools", {})
    discovered_native = external_tools.get("voice_native", {}) if isinstance(external_tools, dict) else {}
    source = args.input.resolve()
    output = args.output.resolve()
    cues = parse_srt(source)
    if not cues:
        raise SystemExit(f"no subtitle cues found in {source}")

    segments_path = getattr(args, "segments", None) or source.with_suffix(".segments.json")
    segments = load_json(segments_path, {}) if segments_path.is_file() else {}
    segment_items = {int(item["index"]): item for item in segments.get("segments", [])}
    if segments:
        if set(segment_items) != {cue.index for cue in cues} or any(
            normalize_text(segment_items[cue.index]["text"]) != cue.text or
            segment_items[cue.index]["start_ms"] != cue.start or segment_items[cue.index]["end_ms"] != cue.end
            for cue in cues
        ):
            raise SystemExit("Segmentation identities do not match translation input")
    prompt_path = resolve_config_path(resources.get("translation_prompt", ""), args.config)
    glossary_path = resolve_config_path(resources.get("translation_glossary", ""), args.config)
    prompt = read_text(prompt_path) if prompt_path.exists() else "完整、忠实地把日文字幕直译为简体中文，保留未完句和真实重复。"
    glossary = normalize_glossary(glossary_pairs(glossary_path)) if glossary_path.exists() else {}

    use_native_dictionaries = bool(settings.get("use_native_dictionaries", True))
    native_prompt_mode = str(settings.get("native_prompt_mode", "不使用"))
    pre_dictionary_path = _native_path(settings, discovered_native, "native_pre_dictionary", "pre_dictionary", args.config)
    gpt_dictionary_path = _native_path(settings, discovered_native, "native_gpt_dictionary", "gpt_dictionary", args.config)
    post_dictionary_path = _native_path(settings, discovered_native, "native_post_dictionary", "post_dictionary", args.config)
    guideline_path = _native_path(settings, discovered_native, "native_guideline", "basic_guideline", args.config)
    project_config_path = _native_path(settings, discovered_native, "native_project_config", "project_config", args.config)
    prompt = effective_native_prompt(prompt, native_prompt_mode, guideline_path, project_config_path)

    pre_entries: list[NativeDictionaryEntry] = []
    gpt_entries: list[NativeDictionaryEntry] = []
    post_entries: list[NativeDictionaryEntry] = []
    unsupported_native = 0
    if use_native_dictionaries:
        pre_entries, pre_unsupported = load_native_dictionary(pre_dictionary_path)
        gpt_entries, gpt_unsupported = load_native_dictionary(gpt_dictionary_path, gpt=True)
        post_entries, post_unsupported = load_native_dictionary(post_dictionary_path)
        unsupported_native = pre_unsupported + gpt_unsupported + post_unsupported
        print(
            f"[voice] native dictionaries: pre={len(pre_entries)} gpt={len(gpt_entries)} "
            f"post={len(post_entries)}",
            flush=True,
        )
        if unsupported_native:
            print(
                f"[voice] warning: skipped {unsupported_native} conditional, situational, or malformed "
                "native dictionary entries; direct SRT mode does not expose all GalTransl sentence metadata",
                flush=True,
            )

    system_prompt = prompt
    required_glossary_entries = [
        NativeDictionaryEntry(source_term, resolved_term_target(source_term, "", glossary))
        for source_term in glossary
    ]
    endpoint = args.endpoint or str(settings.get("endpoint", "http://127.0.0.1:8989"))
    model_path = Path(str(settings.get("model", "Sakura-Galtransl-7B-v3.7.gguf")))
    batch_size = int(settings.get("batch_size", 10) if args.batch_size is None else args.batch_size)
    if batch_size <= 0:
        raise SystemExit("Translation batch size must be positive")
    output.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output.parent / "translation_checkpoint.json"
    progress_path = output.parent / "translation_progress.json"
    native_signature = {
        "adapter": native_file_signature(Path(__file__)),
        "client": native_file_signature(Path(__file__).with_name("sakura_client.py")),
        "dictionary_logic": native_file_signature(Path(__file__).with_name("voice_dictionary.py")),
        "enabled": use_native_dictionaries,
        "prompt_mode": native_prompt_mode,
        "temperature": float(settings.get("temperature", 0.3)),
        "top_p": float(settings.get("top_p", 0.8)),
        "pre_dictionary": native_file_signature(pre_dictionary_path),
        "gpt_dictionary": native_file_signature(gpt_dictionary_path),
        "post_dictionary": native_file_signature(post_dictionary_path),
        "guideline": native_file_signature(guideline_path),
        "project_config": native_file_signature(project_config_path),
    }
    if segments:
        native_signature["segmentation"] = native_file_signature(segments_path)
    signature = translation_signature(source, prompt, glossary, _model_identity(model_path), native_signature)
    translated, previous_attempt = load_translation_checkpoint(checkpoint_path, signature)
    valid_ids = {cue.index for cue in cues}
    translated = {item_id: text for item_id, text in translated.items() if item_id in valid_ids}
    attempt = previous_attempt + 1
    started_at = time.monotonic()
    baseline_completed = len(translated)
    last_progress_write = started_at
    save_translation_checkpoint(
        checkpoint_path,
        signature=signature,
        status="RUNNING",
        attempt=attempt,
        total=len(cues),
        translated=translated,
    )
    save_translation_progress(
        progress_path,
        completed=len(translated),
        total=len(cues),
        started_at=started_at,
        status="RUNNING",
        baseline_completed=baseline_completed,
    )
    if translated:
        print(f"[voice] resumed {len(translated)} / {len(cues)} translated cues from checkpoint", flush=True)

    pending = [cue for cue in cues if cue.index not in translated]
    server: subprocess.Popen[str] | None = None
    generated_characters = 0

    def commit_mapped(values: dict[int, str]) -> None:
        nonlocal last_progress_write
        translated.update(values)
        save_translation_checkpoint(checkpoint_path, signature=signature, status="RUNNING",
                                    attempt=attempt, total=len(cues), translated=translated)
        save_translation_progress(progress_path, completed=len(translated), total=len(cues),
                                  started_at=started_at, status="RUNNING", baseline_completed=baseline_completed)
        last_progress_write = time.monotonic()
        print(f"[voice] 已保存译文：{len(translated)} / {len(cues)}", flush=True)

    def stream_progress(content: str) -> None:
        nonlocal generated_characters, last_progress_write
        generated_characters += len(content)
        if time.monotonic() - last_progress_write >= PROGRESS_WRITE_INTERVAL_SECONDS:
            save_translation_progress(progress_path, completed=len(translated), total=len(cues),
                                      started_at=started_at, status="RUNNING", baseline_completed=baseline_completed)
            last_progress_write = time.monotonic()
            print(f"[voice] 正在接收模型译文：已收到 {generated_characters} 字符；已保存 {len(translated)} / {len(cues)} 条", flush=True)
    try:
        if pending:
            server = start_server(settings, endpoint, component_log_dir(output) / "sakura_server.log")
        for offset in range(0, len(pending), batch_size):
            current = pending[offset : offset + batch_size]
            batch = [
                {"id": cue.index, "text": apply_native_dictionary(cue.text, pre_entries)}
                for cue in current
            ]
            if segments:
                for item in batch:
                    segment = segment_items[item["id"]]
                    item["cue_id"] = segment["id"]
                    item["context"] = segment["context"]
            batch_prompt = system_prompt
            batch_source_text = "\n".join(str(item["text"]) for item in batch)
            batch_reference_text = batch_source_text + "\n" + "\n".join(
                str(parent["text"]) for item in batch for parent in item.get("context", [])
                if isinstance(parent, dict) and "text" in parent)
            required_glossary = native_glossary_prompt(
                required_glossary_entries,
                batch_reference_text,
            )
            native_glossary = native_glossary_prompt(
                gpt_entries,
                batch_reference_text,
                excluded_sources=set(glossary),
            )
            glossary_sections = [section for section in (required_glossary, native_glossary) if section]
            print(
                f"[voice] translating batch; completed={len(translated)} total={len(cues)} "
                f"next_ids={current[0].index}-{current[-1].index}",
                flush=True,
            )
            translated.update(translate_batch(
                endpoint, model_path.name, batch_prompt, batch,
                audit_dir=component_log_dir(output) / "translation_requests",
                on_mapped=commit_mapped, on_stream=stream_progress,
                temperature=native_signature["temperature"], top_p=native_signature["top_p"],
                glossary_text="\n".join(glossary_sections),
            ))
            save_translation_checkpoint(
                checkpoint_path,
                signature=signature,
                status="RUNNING",
                attempt=attempt,
                total=len(cues),
                translated=translated,
            )
            now = time.monotonic()
            if now - last_progress_write >= PROGRESS_WRITE_INTERVAL_SECONDS:
                save_translation_progress(
                    progress_path,
                    completed=len(translated),
                    total=len(cues),
                    started_at=started_at,
                    status="RUNNING",
                    baseline_completed=baseline_completed,
                )
                last_progress_write = now
    except BaseException as exc:
        save_translation_checkpoint(
            checkpoint_path,
            signature=signature,
            status="INTERRUPTED" if isinstance(exc, KeyboardInterrupt) else "FAILED",
            attempt=attempt,
            total=len(cues),
            translated=translated,
            error=str(exc),
        )
        save_translation_progress(
            progress_path,
            completed=len(translated),
            total=len(cues),
            started_at=started_at,
            status="INTERRUPTED" if isinstance(exc, KeyboardInterrupt) else "FAILED",
            baseline_completed=baseline_completed,
            error=str(exc),
        )
        raise
    finally:
        stop_server(server)

    missing = [cue.index for cue in cues if cue.index not in translated]
    if missing:
        error = f"translation checkpoint is missing subtitle ids: {missing}"
        save_translation_checkpoint(
            checkpoint_path,
            signature=signature,
            status="FAILED",
            attempt=attempt,
            total=len(cues),
            translated=translated,
            error=error,
        )
        save_translation_progress(
            progress_path,
            completed=len(translated),
            total=len(cues),
            started_at=started_at,
            status="FAILED",
            baseline_completed=baseline_completed,
            error=error,
        )
        raise SystemExit(error)
    texts = [
        ensure_required_terms(
            cue.text,
            apply_native_dictionary(translated[cue.index], post_entries),
            glossary,
        )
        for cue in cues
    ]
    term_issues = [{"line": cue.index, "missing_terms": list(dict.fromkeys(
                       missing_required_terms(cue.text, text, glossary) +
                       [entry.target for entry in gpt_entries if entry.source not in glossary
                        and entry.source in apply_native_dictionary(cue.text, pre_entries)
                        and entry.target not in text]))}
                   for cue, text in zip(cues, texts)]
    term_issues = [issue for issue in term_issues if issue["missing_terms"]]
    atomic_write_json(output.parent / "translation_term_review.json", {"issue_count": len(term_issues), "issues": term_issues})
    if term_issues:
        print(f"[voice] warning: {len(term_issues)} cues need glossary review; no missing term was appended", flush=True)
    allowed_japanese_terms = [entry.target for entry in required_glossary_entries + gpt_entries + post_entries]
    language_issues = [{"line": cue.index, "text": text, "source_text": cue.text,
                       "reason": "Japanese letters remain outside dictionary-approved terms"}
                      for cue, text in zip(cues, texts) if has_unreviewed_kana(text, allowed_japanese_terms)]
    atomic_write_json(output.parent / "translation_language_review.json",
                      {"issue_count": len(language_issues), "issues": language_issues})
    language_issue_ids = {item["line"] for item in language_issues}
    term_issue_ids = {item["line"] for item in term_issues}
    def translation_flags(cue, text):
        flags = []
        if cue.index in language_issue_ids:
            flags += ["TRANSLATION_JAPANESE_PRESENT", "CHECK"]
        if cue.index in term_issue_ids:
            flags += ["TRANSLATION_TERM_MISSING", "CHECK"]
        if "[待确认]" in text:
            flags += ["TRANSLATION_UNCERTAIN", "CHECK"]
        return sorted(set(flags))
    if language_issues:
        print(f"[voice] warning: {len(language_issues)} cues retain Japanese letters; complete text preserved for review", flush=True)
    candidate = output.with_name(f".{output.stem}.{uuid.uuid4().hex}.tmp.srt")
    atomic_write_text(candidate, "\ufeff" + render_srt(cues, texts))
    validation = validate_subtitle(
        candidate,
        "zh_machine",
        require_text=True,
        expected_language="zh",
        expected_cues=len(cues),
    )
    validation_path = output.parent / "zh_validation.json"
    if not validation.valid:
        evidence = preserve_failed_candidate(candidate, output, "translation")
        if evidence is not None:
            validation.path = str(evidence.resolve())
        atomic_write_json(validation_path, validation.to_dict())
        save_translation_checkpoint(
            checkpoint_path,
            signature=signature,
            status="FAILED",
            attempt=attempt,
            total=len(cues),
            translated=translated,
            error=validation.summary(),
            validation=validation.to_dict(),
        )
        save_translation_progress(
            progress_path,
            completed=len(translated),
            total=len(cues),
            started_at=started_at,
            status="FAILED",
            baseline_completed=baseline_completed,
            error=validation.summary(),
        )
        raise SystemExit(
            f"translation returned success but produced an invalid Chinese SRT: {validation.summary()}; "
            f"candidate preserved at {evidence}"
        )
    if segments:
        atomic_write_json(output.with_suffix(".segments.json"), {
            "schema_version": 1, "source_signature": native_file_signature(segments_path),
            "language_review": language_issues,
            "term_review": term_issues,
            "translations": [{"cue_id": segment_items[cue.index]["id"], "index": cue.index,
                              "start_ms": cue.start, "end_ms": cue.end, "text": texts[position],
                              "flags": translation_flags(cue, texts[position])}
                             for position, cue in enumerate(cues)],
        })
    os.replace(candidate, output)
    validation.path = str(output.resolve())
    atomic_write_json(validation_path, validation.to_dict())
    save_translation_checkpoint(
        checkpoint_path,
        signature=signature,
        status="DONE",
        attempt=attempt,
        total=len(cues),
        translated=translated,
        validation=validation.to_dict(),
    )
    save_translation_progress(
        progress_path,
        completed=len(cues),
        total=len(cues),
        started_at=started_at,
        status="DONE",
        baseline_completed=baseline_completed,
    )
    print(f"[voice] validated: {validation.summary()}")
    print(f"[voice] Chinese SRT completed: {output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="VoiceTransl command-line adapter")
    subparsers = parser.add_subparsers(dest="action", required=True)

    recognition = subparsers.add_parser("recognize")
    recognition.add_argument("--config", required=True, type=Path)
    recognition.add_argument("--audio", required=True, type=Path)
    recognition.add_argument("--output", required=True, type=Path)
    recognition.add_argument("--executable", type=Path)
    recognition.add_argument("--model", type=Path)
    recognition.add_argument("--language")
    recognition.add_argument("--vad", action=argparse.BooleanOptionalAction, default=None)
    recognition.set_defaults(handler=recognize)

    translation = subparsers.add_parser("translate")
    translation.add_argument("--config", required=True, type=Path)
    translation.add_argument("--input", required=True, type=Path)
    translation.add_argument("--segments", type=Path)
    translation.add_argument("--output", required=True, type=Path)
    translation.add_argument("--endpoint")
    translation.add_argument("--batch-size", type=int)
    translation.set_defaults(handler=translate)
    return parser


def main() -> int:
    configure_utf8_stdio()
    args = build_parser().parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
