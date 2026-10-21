#!/usr/bin/env python3
"""Regression tests for shallow external-tool discovery and config migration."""

from __future__ import annotations

import tempfile
from pathlib import Path

from tool_discovery import (
    apply_discovery,
    discover_magia,
    discover_voice,
    infer_magia_root,
    infer_voice_root,
)


ROOT = (Path(__file__).resolve().parents[1] / "app")


def touch(path: Path, size: int = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def populate_voice(root: Path, *, native: bool = True) -> Path:
    touch(root / "VoiceTransl.exe")
    touch(root / "_internal" / "whisper" / "whisper-cli.exe")
    touch(root / "_internal" / "whisper" / "ggml-small.bin", 8)
    large_model = touch(root / "_internal" / "whisper" / "ggml-large-v3.bin", 16)
    touch(root / "_internal" / "whisper" / "ggml-silero-v5.1.2.bin", 32)
    touch(root / "_internal" / "llama" / "llama-server.exe")
    touch(root / "_internal" / "llama" / "Sakura-Galtransl-7B-v3.7.gguf", 64)
    if native:
        touch(root / "_internal" / "translate" / "translate.exe")
        for name in ("config.yaml", "dict_pre.txt", "dict_gpt.txt", "dict_after.txt"):
            touch(root / "_internal" / "project" / name)
        touch(root / "_internal" / "translation_guidelines" / "Basic.md")
    return large_model


def populate_ascii_runtime(project_root: Path, *, complete: bool) -> None:
    touch(project_root / "runtime" / "whisper" / "whisper-cli.exe")
    touch(project_root / "runtime" / "llama" / "llama-server.exe")
    if complete:
        touch(project_root / "runtime" / "whisper" / "model_store" / "ggml-large-v3.bin")
        touch(project_root / "runtime" / "llama" / "model_store" / "Sakura-Galtransl-7B-v3.7.gguf")


def main() -> int:
    output_root = ROOT / "output"
    output_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tool_discovery_", dir=output_root) as temp_dir:
        root = Path(temp_dir)
        installs = root / "下载的软件"
        magia_root = installs / "MagiaTimeline-1.1.0"
        for name in ("MagiaTimeline.exe", "config.yml", "template.asst"):
            touch(magia_root / name)
        (magia_root / "PaddleOCRModels").mkdir(parents=True)

        voice_root = installs / "VoiceTransl-v1.21-win-sakura"
        large_model = populate_voice(voice_root)

        magia = discover_magia(installs)
        if not magia["valid"] or Path(magia["root"]) != magia_root.resolve():
            raise AssertionError(f"Magia root was not discovered from its parent: {magia}")
        voice_without_runtime = discover_voice(voice_root / "_internal")
        if voice_without_runtime["valid"]:
            raise AssertionError("A Chinese-path VoiceTransl install was accepted without an ASCII runtime")
        if "纯 ASCII Whisper 运行时" not in voice_without_runtime["missing"]:
            raise AssertionError(f"Missing Whisper compatibility warning: {voice_without_runtime}")
        if "纯 ASCII Sakura 运行时" not in voice_without_runtime["missing"]:
            raise AssertionError(f"Missing Sakura compatibility warning: {voice_without_runtime}")
        if not voice_without_runtime["capabilities"]["native_translation"]:
            raise AssertionError("Python-readable native dictionaries were incorrectly disabled")
        if Path(voice_without_runtime["components"]["whisper_model"]["path"]) != large_model.resolve():
            raise AssertionError("VAD model was incorrectly selected as the Whisper model")

        partial_project = root / "partial_project"
        populate_ascii_runtime(partial_project, complete=False)
        partial = discover_voice(voice_root, project_root=partial_project)
        if partial["valid"] or partial["runtime_overrides"]["whisper"] or partial["runtime_overrides"]["llama"]:
            raise AssertionError(f"Partial ASCII runtime was accepted: {partial}")

        project_root = root / "portable_project"
        populate_ascii_runtime(project_root, complete=True)
        voice = discover_voice(voice_root, project_root=project_root)
        if not voice["valid"] or not voice["capabilities"]["native_translation"]:
            raise AssertionError(f"Complete ASCII runtime was not used: {voice}")
        if Path(voice["components"]["whisper_model"]["path"]) != large_model.resolve():
            raise AssertionError("VAD model was incorrectly selected as the Whisper model")
        whisper_override = voice["runtime_overrides"]["whisper"]
        llama_override = voice["runtime_overrides"]["llama"]
        if not whisper_override or not llama_override:
            raise AssertionError(f"Complete runtime overrides were not returned: {voice}")

        config: dict = {}
        apply_discovery(config, magia, voice)
        if Path(config["axis_settings"]["executable"]).name != "MagiaTimeline.exe":
            raise AssertionError("Magia executable was not applied to config")
        if Path(config["recognition"]["executable"]) != Path(whisper_override["whisper_executable"]):
            raise AssertionError("ASCII Whisper executable was not applied to config")
        if Path(config["translation"]["model"]) != Path(llama_override["sakura_model"]):
            raise AssertionError("ASCII Sakura model was not applied to config")
        if infer_magia_root(config) != str(magia_root.resolve()):
            raise AssertionError("Magia root migration failed")
        if infer_voice_root(config) != str(voice_root.resolve()):
            raise AssertionError("VoiceTransl root migration failed")

        ascii_voice_root = root / "VoiceTransl-ascii"
        populate_voice(ascii_voice_root, native=False)
        ascii_voice = discover_voice(ascii_voice_root)
        if not ascii_voice["valid"] or any(ascii_voice["runtime_overrides"].values()):
            raise AssertionError(f"A directly compatible ASCII install was rejected: {ascii_voice}")

        incomplete = root / "incomplete_voice"
        touch(incomplete / "_internal" / "whisper" / "whisper-cli.exe")
        result = discover_voice(incomplete)
        if result["valid"] or not result["missing"]:
            raise AssertionError("Incomplete VoiceTransl directory was accepted")

    print("Tool discovery tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
