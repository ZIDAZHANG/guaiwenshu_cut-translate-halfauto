#!/usr/bin/env python3
"""Run MagiaTimeline with an isolated per-job configuration."""

from __future__ import annotations

import argparse
import json
import locale
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from artifact_validation import validate_subtitle
from magia_utf8_runtime import prepare_utf8_executable


def configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def hidden_process_kwargs() -> dict:
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startupinfo}


import yaml


SCRIPT_ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_settings(config_path: Path | None) -> dict:
    if not config_path:
        return {}
    return load_json(config_path).get("axis_settings", {})


def resolve_region(settings: dict, cli_value: list[float] | None) -> list[float]:
    region = cli_value or settings.get("dialog_rect") or [0.0, 1.0, 0.0, 1.0]
    values = [float(value) for value in region]
    if len(values) != 4 or any(value < 0.0 or value > 1.0 for value in values):
        raise ValueError("dialog rectangle must contain four values between 0.0 and 1.0")
    if values[0] >= values[1] or values[2] >= values[3]:
        raise ValueError("dialog rectangle left/top must be smaller than right/bottom")
    return values


def update_strategy_region(data: dict, strategy: str, preset: str, region: list[float]) -> bool:
    strategy_data = data.get(strategy)
    if not isinstance(strategy_data, dict):
        return False
    preset_data = strategy_data.get(preset)
    if not isinstance(preset_data, dict):
        return False
    if "dialogRect" not in preset_data:
        return False
    preset_data["dialogRect"] = region
    return True


def decode_console_line(raw_line: bytes) -> str:
    encodings = ["utf-8", locale.getpreferredencoding(False), "cp932", "gb18030"]
    for encoding in dict.fromkeys(encodings):
        try:
            return raw_line.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw_line.decode("utf-8", errors="replace")


def stream_process(command: list[str], cwd: Path, log_path: Path) -> int:
    child_env = os.environ.copy()
    child_env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    with log_path.open("w", encoding="utf-8", errors="replace", newline="\n") as log:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=child_env,
            **hidden_process_kwargs(),
        )
        assert process.stdin is not None
        process.stdin.write(b"\n")
        process.stdin.flush()
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = decode_console_line(raw_line)
            try:
                print(line, end="", flush=True)
            except UnicodeEncodeError:
                encoding = sys.stdout.encoding or "utf-8"
                safe_line = line.encode(encoding, errors="replace").decode(encoding)
                print(safe_line, end="", flush=True)
            log.write(line)
        return process.wait()


def _directory_is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write_probe_{os.getpid()}_{uuid.uuid4().hex}"
        probe.write_bytes(b"ok")
        probe.unlink()
        return True
    except OSError:
        return False


def choose_ascii_staging_root(explicit: Path | None, video: Path, evidence_dir: Path) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(explicit)
    env_root = os.environ.get("SUBTITLE_ROAST_ASCII_TEMP", "").strip()
    if env_root:
        candidates.append(Path(env_root))
    candidates.append(SCRIPT_ROOT / "runtime" / "magia_ascii")

    for source in (video.parent, evidence_dir.parent):
        for parent in (source, *source.parents):
            if str(parent).isascii():
                candidates.append(parent / ".magia_ascii")
                break

    candidates.append(Path(tempfile.gettempdir()) / "subtitle_roast_ascii")
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        key = str(resolved).casefold()
        if key in seen or not str(resolved).isascii():
            continue
        seen.add(key)
        if _directory_is_writable(resolved):
            return resolved
    raise RuntimeError(
        "No writable ASCII-only staging directory is available. "
        "Set axis_settings.ascii_staging_root or SUBTITLE_ROAST_ASCII_TEMP."
    )


def link_or_copy_file(source: Path, target: Path) -> str:
    try:
        os.link(source, target)
        return "hardlink"
    except OSError:
        shutil.copy2(source, target)
        return "copy"


def link_or_copy_directory(source: Path, target: Path) -> str:
    try:
        target.symlink_to(source, target_is_directory=True)
        return "symlink"
    except OSError:
        pass
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(target), str(source)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            **hidden_process_kwargs(),
        )
        if completed.returncode == 0 and target.is_dir():
            return "junction"
    shutil.copytree(source, target)
    return "copy"


def atomic_publish(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with source.open("rb") as read_handle, temporary.open("wb") as write_handle:
            shutil.copyfileobj(read_handle, write_handle)
            write_handle.flush()
            os.fsync(write_handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def copy_evidence(source: Path, destination: Path) -> None:
    if source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)



def native_output_encoding() -> str:
    if os.name == "nt":
        import ctypes
        return f"cp{ctypes.windll.kernel32.GetACP()}"
    return locale.getpreferredencoding(False)


def normalize_output_encoding(source: Path) -> dict:
    """Frozen Magia may write a UTF-8 BOM followed by Windows ACP text."""
    payload = source.read_bytes().removeprefix(b"\xef\xbb\xbf")
    try:
        text = payload.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        encoding = native_output_encoding()
        text = payload.decode(encoding)
    source.write_text(text, encoding="utf-8-sig", newline="")
    return {"source_encoding": encoding, "output_encoding": "utf-8-sig"}


def locate_output(target: Path, started_at: float) -> Path | None:
    candidates = [target]
    if target.suffix.lower() != ".ass":
        candidates.append(target.with_suffix(".ass"))
    candidates.extend(
        sorted(
            target.parent.glob(f"{target.stem}*.ass"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    )
    for candidate in candidates:
        if candidate.exists() and candidate.stat().st_size > 0 and candidate.stat().st_mtime >= started_at - 2:
            return candidate
    return None


def main() -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Generate an accurate ASS timeline with MagiaTimeline.")
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--strategy")
    parser.add_argument("--preset")
    parser.add_argument("--engine", choices=("speculative", "framewise"))
    parser.add_argument("--max-resolution", type=int)
    parser.add_argument("--dialog-rect", type=float, nargs=4, metavar=("LEFT", "RIGHT", "TOP", "BOTTOM"))
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--staging-root", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--ocr", action=argparse.BooleanOptionalAction, default=None)
    args = parser.parse_args()

    settings = load_settings(args.config)
    executable_value = args.executable or str(settings.get("executable", "")).strip()
    if not executable_value:
        raise SystemExit("MagiaTimeline is not configured. Select its installation folder in the desktop app.")
    executable = Path(executable_value)
    app_root = executable.parent
    template_config = app_root / "config.yml"
    ass_template = app_root / "template.asst"
    strategy = args.strategy or str(settings.get("strategy", "dtd"))
    preset = args.preset or str(settings.get("preset", "default"))
    engine = args.engine or str(settings.get("engine", "speculative"))
    max_resolution = int(settings.get("max_resolution", 0)) if args.max_resolution is None else args.max_resolution
    ocr_enabled = bool(settings.get("ocr", True)) if args.ocr is None else args.ocr
    region = resolve_region(settings, args.dialog_rect)

    video = args.video.resolve()
    output = args.output.resolve()
    evidence_dir = (args.work_dir or output.parent.parent / "logs" / "magia_timeline").resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)

    for required in (executable, template_config, ass_template, video):
        if not required.exists():
            raise SystemExit(f"required file does not exist: {required}")

    if not args.prepare_only:
        executable, compatibility = prepare_utf8_executable(executable, SCRIPT_ROOT / "runtime" / "magia_utf8")
        (evidence_dir / "runtime_compatibility.json").write_text(
            json.dumps(compatibility, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    staging_root_value = args.staging_root or (
        Path(str(settings["ascii_staging_root"])) if settings.get("ascii_staging_root") else None
    )
    staging_root = choose_ascii_staging_root(staging_root_value, video, evidence_dir)
    stage_dir = Path(tempfile.mkdtemp(prefix="magia_job_", dir=staging_root))
    if not str(stage_dir).isascii():
        raise SystemExit(f"MagiaTimeline staging path is not ASCII-only: {stage_dir}")

    video_suffix = video.suffix.lower() if video.suffix.lower() in {".mp4", ".mkv", ".webm", ".avi"} else ".mp4"
    stage_video = stage_dir / f"input{video_suffix}"
    stage_output = stage_dir / "output.ass"
    task_config = stage_dir / "config.yml"
    task_log = stage_dir / "MagiaTimeline.log"
    evidence_config = evidence_dir / "config.ascii.yml"
    evidence_log = evidence_dir / "MagiaTimeline.log"
    validation_path = evidence_dir / "timeline_validation.json"

    try:
        media_mode = link_or_copy_file(video, stage_video)
        shutil.copy2(ass_template, stage_dir / "template.asst")
        schema = app_root / "ConfigSchema.json"
        if schema.is_file():
            shutil.copy2(schema, stage_dir / schema.name)
        paddle_models = app_root / "PaddleOCRModels"
        if paddle_models.is_dir():
            if ocr_enabled:
                shutil.copytree(paddle_models, stage_dir / "PaddleOCRModels")
                model_mode = "isolated_copy"
            else:
                model_mode = link_or_copy_directory(paddle_models, stage_dir / "PaddleOCRModels")
        else:
            model_mode = "missing"

        data = yaml.safe_load(template_config.read_text(encoding="utf-8-sig"))
        data["source"] = [str(stage_video)]
        data["destination"] = [str(stage_output.with_suffix(""))]
        data["assTemplate"] = str(stage_dir / "template.asst")
        data["strategy"] = strategy
        data["preset"] = preset
        data["engine"] = engine
        data["maxResolution"] = max_resolution
        data.setdefault("framewise", {})["debug"] = False
        data.setdefault("speculative", {})["debug"] = False
        data["extraJobs"] = ["ocr"] if ocr_enabled else []
        if ocr_enabled:
            data.setdefault("ocr", {}).update({
                "standaloneOutput": False, "standaloneOutputSuffix": "-ocr.txt", "separator": "",
                "doPaddle": True, "nonMajorBoxSuppressionMaxRatio": 0,
                "nonMajorBoxSuppressionMinRank": 1, "doTesseract": False, "tesseractLang": "jpn",
            })
        region_applied = update_strategy_region(data, strategy, preset, region)
        config_text = yaml.safe_dump(data, allow_unicode=False, sort_keys=False, width=120)
        if not config_text.isascii():
            raise RuntimeError("Generated MagiaTimeline config unexpectedly contains non-ASCII text")
        task_config.write_text(config_text, encoding="ascii", newline="\n")
        copy_evidence(task_config, evidence_config)

        print(f"[magia] executable: {executable}")
        print(f"[magia] source: {video}")
        print(f"[magia] output: {output}")
        print(f"[magia] ASCII staging: {stage_dir} ({media_mode}, models={model_mode})")
        print(f"[magia] strategy={strategy}, preset={preset}, engine={engine}")
        if region_applied:
            print(f"[magia] dialogRect={region}")
        else:
            print(f"[magia] selected preset has no dialogRect; keeping its specialized regions")

        if args.prepare_only:
            print(f"[magia] ASCII configuration prepared: {evidence_config}")
            return 0

        started_at = time.time()
        code = stream_process([str(executable)], stage_dir, task_log)
        copy_evidence(task_log, evidence_log)
        generated = locate_output(stage_output, started_at)
        if generated is None:
            if code != 0:
                raise SystemExit(f"MagiaTimeline exited with code {code}; see {evidence_log}")
            raise SystemExit(f"MagiaTimeline returned success but no ASS output was created; see {evidence_log}")

        copy_evidence(generated, evidence_dir / "native_output.ass")
        try:
            encoding_details = normalize_output_encoding(generated)
        except (UnicodeDecodeError, LookupError) as exc:
            copy_evidence(generated, evidence_dir / "failed_candidate.ass")
            raise SystemExit(f"Cannot decode MagiaTimeline output without losing text: {exc}") from exc
        validation = validate_subtitle(generated, "timeline", require_text=ocr_enabled,
                                       expected_language="ja" if ocr_enabled else None)
        validation.details["visual_ocr_enabled"] = ocr_enabled
        validation.details.update(encoding_details)
        validation_path.write_text(
            json.dumps(validation.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if code != 0:
            copy_evidence(generated, evidence_dir / "failed_candidate.ass")
            raise SystemExit(
                f"MagiaTimeline exited with code {code}; candidate output was preserved but not published"
            )
        if not validation.valid:
            copy_evidence(generated, evidence_dir / "failed_candidate.ass")
            raise SystemExit(f"MagiaTimeline returned success but produced an invalid ASS: {validation.summary()}")

        atomic_publish(generated, output)
        validation.path = str(output)
        validation.details["native_output"] = str(evidence_dir / "native_output.ass")
        validation_path.write_text(json.dumps(validation.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[magia] validated: {validation.summary()}")
        print(f"[magia] completed: {output}")
        return 0
    finally:
        copy_evidence(task_config, evidence_config)
        copy_evidence(task_log, evidence_log)
        shutil.rmtree(stage_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
