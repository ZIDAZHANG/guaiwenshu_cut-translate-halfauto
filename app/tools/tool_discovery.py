#!/usr/bin/env python3
"""Discover supported external tools from user-selected installation roots."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable


def _existing_file(candidates: Iterable[Path]) -> Path | None:
    return next((path.resolve() for path in candidates if path.is_file()), None)


def _existing_file_ascii_first(candidates: Iterable[Path]) -> Path | None:
    """Prefer pure-ASCII paths: whisper.cpp cannot load models from paths
    containing non-ASCII characters (crashes with 0xC0000409 on Windows).

    Returns the raw (unresolved) path so directory junctions are preserved:
    resolve() would expose the junction target (e.g. G:\\视频存档\\...) which
    whisper.cpp cannot load."""
    files = [path for path in candidates if path.is_file()]
    if not files:
        return None

    def is_ascii_path(path: Path) -> bool:
        try:
            str(path).encode("ascii")
            return True
        except UnicodeEncodeError:
            return False

    ascii_files = [path for path in files if is_ascii_path(path)]
    return (ascii_files or files)[0]


def _largest_file(directories: Iterable[Path], patterns: Iterable[str], *, reject: tuple[str, ...] = ()) -> Path | None:
    files: list[Path] = []
    for directory in directories:
        if not directory.is_dir():
            continue
        for pattern in patterns:
            files.extend(path for path in directory.glob(pattern) if path.is_file())
    files = [path for path in files if not any(token.casefold() in path.name.casefold() for token in reject)]
    return max(files, key=lambda path: path.stat().st_size, default=None)


def _component(label: str, path: Path | None, required: bool) -> dict[str, Any]:
    return {
        "label": label,
        "path": str(path) if path else "",
        "exists": bool(path and path.exists()),
        "required": required,
    }


def _candidate_roots(selected: Path, signature: str) -> list[Path]:
    selected = selected.expanduser()
    if selected.is_file():
        selected = selected.parent
    candidates = [selected]
    if selected.name.casefold() == "_internal":
        candidates.append(selected.parent)
    if selected.parent.name.casefold() == "_internal":
        candidates.append(selected.parent.parent)
    if selected.is_dir():
        try:
            candidates.extend(child for child in selected.iterdir() if child.is_dir() and (child / signature).exists())
        except OSError:
            pass
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate.absolute()
        key = str(resolved).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(resolved)
    return unique


def discover_magia(selected: Path | str) -> dict[str, Any]:
    selected_path = Path(selected)
    roots = _candidate_roots(selected_path, "MagiaTimeline.exe")
    root = max(
        roots,
        key=lambda candidate: sum(
            int((candidate / relative).exists())
            for relative in ("MagiaTimeline.exe", "config.yml", "template.asst", "PaddleOCRModels")
        ),
    )
    components = {
        "executable": _component("MagiaTimeline 主程序", _existing_file([root / "MagiaTimeline.exe"]), True),
        "config": _component("基础配置", _existing_file([root / "config.yml"]), True),
        "template": _component("ASS 模板", _existing_file([root / "template.asst"]), True),
        "models": _component("PaddleOCR 模型", (root / "PaddleOCRModels").resolve() if (root / "PaddleOCRModels").is_dir() else None, True),
        "schema": _component("配置架构", _existing_file([root / "ConfigSchema.json"]), False),
    }
    missing = [item["label"] for item in components.values() if item["required"] and not item["exists"]]
    return {
        "kind": "magia",
        "selected": str(selected_path),
        "root": str(root),
        "valid": not missing,
        "capabilities": {"timeline": not missing},
        "missing": missing,
        "components": components,
    }


def _path_has_non_ascii(path: str | Path) -> bool:
    try:
        str(path).encode("ascii")
        return False
    except UnicodeEncodeError:
        return True


def _ascii_whisper_fallback(voice: dict[str, Any], project_root: Path | None = None) -> dict[str, str]:
    """When the discovered Whisper lives under a non-ASCII path, whisper.cpp
    crashes loading the model (0xC0000409). Return an ASCII runtime fallback
    (runtime\\whisper) with the same binaries when one exists.

    Note: resolve() follows directory junctions (e.g. runtime\\whisper\\model_store
    may point at G:\\视频存档\\...), so ASCII checks use the raw path before resolve.
    """
    if not any(
        _path_has_non_ascii(item["path"])
        for item in (voice["components"]["whisper_executable"], voice["components"]["whisper_model"])
    ):
        return {}
    runtime_roots = []
    if project_root is not None:
        current = project_root.resolve()
        for _ in range(4):
            runtime_roots.append(current / "runtime" / "whisper")
            if current.parent == current:
                break
            current = current.parent
    for runtime_whisper in runtime_roots:
        if not runtime_whisper.is_dir():
            continue
        fallback: dict[str, str] = {}
        executable = runtime_whisper / "whisper-cli.exe"
        model_store = runtime_whisper / "model_store" / "ggml-large-v3.bin"
        if not all(
            candidate.is_file() and not _path_has_non_ascii(str(candidate))
            for candidate in (executable, model_store)
        ):
            continue
        fallback.update({"whisper_executable": str(executable), "whisper_model": str(model_store)})
        vad_model = runtime_whisper / "ggml-silero-v5.1.2.bin"
        if vad_model.is_file() and not _path_has_non_ascii(str(vad_model)):
            fallback["vad_model"] = str(vad_model)
        return fallback
    return {}


def _ascii_llama_fallback(voice: dict[str, Any], project_root: Path | None = None) -> dict[str, str]:
    """llama.cpp (Sakura server) also cannot open models under non-ASCII paths.
    Return an ASCII runtime\\llama fallback when one exists."""
    components = voice["components"]
    if not any(
        _path_has_non_ascii(item["path"]) for item in (components["llama_server"], components["sakura_model"])
    ):
        return {}
    runtime_roots = []
    if project_root is not None:
        current = project_root.resolve()
        for _ in range(4):
            runtime_roots.append(current / "runtime" / "llama")
            if current.parent == current:
                break
            current = current.parent
    for runtime_llama in runtime_roots:
        if not runtime_llama.is_dir():
            continue
        server = runtime_llama / "llama-server.exe"
        model = runtime_llama / "model_store" / "Sakura-Galtransl-7B-v3.7.gguf"
        if all(
            candidate.is_file() and not _path_has_non_ascii(str(candidate))
            for candidate in (server, model)
        ):
            return {"llama_server": str(server), "sakura_model": str(model)}
    return {}


def discover_voice(selected: Path | str, *, project_root: Path | None = None) -> dict[str, Any]:
    selected_path = Path(selected)
    roots = _candidate_roots(selected_path, "VoiceTransl.exe")

    def score(candidate: Path) -> int:
        known = (
            "VoiceTransl.exe",
            "_internal/whisper/whisper-cli.exe",
            "_internal/llama/llama-server.exe",
            "_internal/translate/translate.exe",
            "_internal/project/config.yaml",
        )
        return sum(int((candidate / relative).exists()) for relative in known)

    root = max(roots, key=score)
    internal = root / "_internal"
    whisper_dirs = (internal / "whisper", root / "whisper", root / "runtime" / "whisper")
    llama_dirs = (internal / "llama", root / "llama", root / "runtime" / "llama")
    model_dirs = (*whisper_dirs, *(directory / "model_store" for directory in whisper_dirs))
    sakura_model_dirs = (*llama_dirs, *(directory / "model_store" for directory in llama_dirs))

    whisper_exe = _existing_file_ascii_first(directory / "whisper-cli.exe" for directory in whisper_dirs)
    whisper_model = _existing_file_ascii_first(directory / "ggml-large-v3.bin" for directory in model_dirs)
    if whisper_model is None:
        whisper_model = _largest_file(model_dirs, ("ggml-*.bin",), reject=("silero", "vad"))
    vad_model = _existing_file_ascii_first(directory / "ggml-silero-v5.1.2.bin" for directory in model_dirs)
    llama_server = _existing_file(directory / "llama-server.exe" for directory in llama_dirs)
    sakura_model = _existing_file(directory / "Sakura-Galtransl-7B-v3.7.gguf" for directory in sakura_model_dirs)
    if sakura_model is None:
        sakura_model = _largest_file(sakura_model_dirs, ("*Sakura*.gguf", "*.gguf"))

    project = internal / "project"
    guidelines = internal / "translation_guidelines"
    components = {
        "voice_executable": _component("VoiceTransl 主程序", _existing_file([root / "VoiceTransl.exe"]), False),
        "whisper_executable": _component("Whisper 主程序", whisper_exe, True),
        "whisper_model": _component("Whisper 模型", whisper_model, True),
        "vad_model": _component("VAD 模型", vad_model, False),
        "llama_server": _component("Sakura 模型服务", llama_server, True),
        "sakura_model": _component("Sakura 模型", sakura_model, True),
        "translate_executable": _component("VoiceTransl 原生翻译程序", _existing_file([internal / "translate" / "translate.exe"]), False),
        "project_config": _component("原生翻译配置", _existing_file([project / "config.yaml"]), False),
        "pre_dictionary": _component("翻译前字典", _existing_file([project / "dict_pre.txt"]), False),
        "gpt_dictionary": _component("GPT 字典", _existing_file([project / "dict_gpt.txt"]), False),
        "post_dictionary": _component("翻译后字典", _existing_file([project / "dict_after.txt"]), False),
        "basic_guideline": _component("Extra Prompt/翻译规则", _existing_file([guidelines / "Basic.md"]), False),
    }
    selected_recognition_ready = components["whisper_executable"]["exists"] and components["whisper_model"]["exists"]
    selected_translation_ready = components["llama_server"]["exists"] and components["sakura_model"]["exists"]
    native_ready = components["translate_executable"]["exists"] and components["project_config"]["exists"]
    missing = [
        item["label"]
        for key, item in components.items()
        if key in {"whisper_executable", "whisper_model", "llama_server", "sakura_model"} and not item["exists"]
    ]
    warnings: list[str] = []
    for key in ("whisper_executable", "whisper_model", "vad_model"):
        path = components[key]["path"]
        if path and _path_has_non_ascii(path):
            warnings.append(
                f"{components[key]['label']} 位于含中文/非 ASCII 字符的路径，whisper.cpp 无法加载，"
                "请改用纯 ASCII 路径的 Whisper（如 runtime\\whisper）。"
            )
    for key in ("llama_server", "sakura_model"):
        path = components[key]["path"]
        if path and _path_has_non_ascii(path):
            warnings.append(
                f"{components[key]['label']} 位于含中文/非 ASCII 字符的路径，llama.cpp 无法加载，"
                "请改用纯 ASCII 路径的 Sakura 运行时（如 runtime\\llama）。"
            )

    provisional = {"components": components}
    whisper_overrides = _ascii_whisper_fallback(provisional, project_root)
    llama_overrides = _ascii_llama_fallback(provisional, project_root)

    def compatible_pair(first: str, second: str, overrides: dict[str, str]) -> bool:
        values = (overrides.get(first) or components[first]["path"], overrides.get(second) or components[second]["path"])
        return bool(all(values) and not any(_path_has_non_ascii(value) for value in values))

    recognition_ready = bool(selected_recognition_ready and compatible_pair("whisper_executable", "whisper_model", whisper_overrides))
    translation_ready = bool(selected_translation_ready and compatible_pair("llama_server", "sakura_model", llama_overrides))
    if selected_recognition_ready and not recognition_ready:
        missing.append("纯 ASCII Whisper 运行时")
    if selected_translation_ready and not translation_ready:
        missing.append("纯 ASCII Sakura 运行时")

    return {
        "kind": "voice",
        "selected": str(selected_path),
        "root": str(root),
        "valid": bool(recognition_ready and translation_ready),
        "capabilities": {
            "recognition": bool(recognition_ready),
            "translation": bool(translation_ready),
            "native_translation": bool(native_ready),
        },
        "missing": missing,
        "warnings": warnings,
        "runtime_overrides": {"whisper": whisper_overrides, "llama": llama_overrides},
        "components": components,
    }


def infer_magia_root(config: dict[str, Any]) -> str:
    explicit = str(config.get("external_tools", {}).get("magia_root", "")).strip()
    if explicit:
        return explicit
    executable = str(config.get("axis_settings", {}).get("executable", "")).strip()
    return str(Path(executable).parent) if executable else ""


def infer_voice_root(config: dict[str, Any]) -> str:
    explicit = str(config.get("external_tools", {}).get("voice_root", "")).strip()
    if explicit:
        return explicit
    values = (
        config.get("recognition", {}).get("executable", ""),
        config.get("translation", {}).get("server_executable", ""),
        config.get("translation", {}).get("executable", ""),
    )
    for value in values:
        if not value:
            continue
        path = Path(str(value))
        for parent in (path.parent, *path.parents):
            if parent.name.casefold() == "_internal":
                return str(parent.parent)
            if (parent / "VoiceTransl.exe").is_file():
                return str(parent)
    return ""


def apply_discovery(
    config: dict[str, Any],
    magia: dict[str, Any] | None,
    voice: dict[str, Any] | None,
    *,
    project_root: Path | None = None,
) -> dict[str, Any]:
    external = config.setdefault("external_tools", {})
    tools = config.setdefault("tools", {})
    if magia:
        external["magia_root"] = magia["root"]
        executable = magia["components"]["executable"]["path"]
        if executable:
            config.setdefault("axis_settings", {})["executable"] = executable
            tools["axis_generator"] = executable
    if voice:
        external["voice_root"] = voice["root"]
        recognition = config.setdefault("recognition", {})
        translation = config.setdefault("translation", {})
        component = voice["components"]

        runtime_overrides = voice.get("runtime_overrides", {}) if isinstance(voice.get("runtime_overrides"), dict) else {}
        whisper_overrides = runtime_overrides.get("whisper", {}) if isinstance(runtime_overrides.get("whisper"), dict) else {}
        if not whisper_overrides and project_root is not None:
            whisper_overrides = _ascii_whisper_fallback(voice, project_root)
        if whisper_overrides:
            print(
                "[discovery] Whisper 位于含中文的路径，whisper.cpp 无法加载；"
                f"已改用 ASCII 运行时：{whisper_overrides.get('whisper_executable', '')}",
                flush=True,
            )

        def whisper_value(key: str) -> str:
            return whisper_overrides.get(key) or component[key]["path"]

        whisper_pair = (whisper_value("whisper_executable"), whisper_value("whisper_model"))
        whisper_compatible = bool(all(whisper_pair) and not any(_path_has_non_ascii(value) for value in whisper_pair))
        if whisper_compatible:
            recognition["executable"] = whisper_value("whisper_executable")
            tools["jp_recognizer"] = whisper_value("whisper_executable")
            recognition["model"] = whisper_value("whisper_model")
            if whisper_value("vad_model"):
                recognition["vad_model"] = whisper_value("vad_model")
        elif any(whisper_pair):
            print("[discovery] 未找到完整的纯 ASCII Whisper 运行时，保留现有识别配置。", flush=True)

        llama_overrides = runtime_overrides.get("llama", {}) if isinstance(runtime_overrides.get("llama"), dict) else {}
        if not llama_overrides and project_root is not None:
            llama_overrides = _ascii_llama_fallback(voice, project_root)
        if llama_overrides:
            print(
                "[discovery] Sakura 模型服务位于含中文的路径，llama.cpp 无法加载；"
                f"已改用 ASCII 运行时：{llama_overrides.get('llama_server', '')}",
                flush=True,
            )

        def llama_value(key: str) -> str:
            return llama_overrides.get(key) or component[key]["path"]

        if component["translate_executable"]["path"]:
            translation["executable"] = component["translate_executable"]["path"]
        llama_pair = (llama_value("llama_server"), llama_value("sakura_model"))
        llama_compatible = bool(all(llama_pair) and not any(_path_has_non_ascii(value) for value in llama_pair))
        if llama_compatible:
            translation["server_executable"] = llama_value("llama_server")
            tools["machine_translator"] = llama_value("llama_server")
            translation["model"] = llama_value("sakura_model")
        elif any(llama_pair):
            print("[discovery] 未找到完整的纯 ASCII Sakura 运行时，保留现有翻译配置。", flush=True)
        external["voice_native"] = {
            key: component[key]["path"]
            for key in (
                "project_config",
                "pre_dictionary",
                "gpt_dictionary",
                "post_dictionary",
                "basic_guideline",
            )
            if component[key]["path"]
        }
    return config


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover MagiaTimeline and VoiceTransl components.")
    parser.add_argument("--magia-root", type=Path)
    parser.add_argument("--voice-root", type=Path)
    args = parser.parse_args()
    result: dict[str, Any] = {}
    if args.magia_root:
        result["magia"] = discover_magia(args.magia_root)
    if args.voice_root:
        result["voice"] = discover_voice(args.voice_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all(item.get("valid") for item in result.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
