#!/usr/bin/env python3
"""Import a video and ASS/SRT subtitle into Jianying through guarded UI automation."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

import win32api
import win32con
import win32gui
from pywinauto import Desktop
from pywinauto.keyboard import send_keys


ROOT = Path(__file__).resolve().parents[1]
PROBE_PATH = ROOT / "tools" / "jianying_uia_probe.py"
DEFAULT_CONFIG = ROOT / "config" / "roast_pipeline.local.json"


def installed_major_version(config: Path = DEFAULT_CONFIG) -> int:
    apps = Path.home() / "AppData" / "Local" / "JianyingPro" / "Apps"
    versions: list[int] = []
    if config.exists():
        try:
            data = json.loads(config.read_text(encoding="utf-8"))
            configured_exe = Path(data.get("local", {}).get("jianying_exe", ""))
            if configured_exe.exists():
                try:
                    return int(configured_exe.parent.name.split(".", 1)[0])
                except ValueError:
                    version = win32api.GetFileVersionInfo(str(configured_exe), "\\")
                    return int(version["FileVersionMS"] >> 16)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    configure = apps / "Configure.ini"
    if configure.exists():
        for line in configure.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().lower().startswith("last_version="):
                try:
                    versions.append(int(line.split("=", 1)[1].strip().split(".", 1)[0]))
                except ValueError:
                    pass
                break
    if apps.exists():
        for path in apps.iterdir():
            if path.is_dir():
                try:
                    versions.append(int(path.name.split(".", 1)[0]))
                except ValueError:
                    continue
    return max(versions, default=6)


def load_probe_module():
    spec = importlib.util.spec_from_file_location("jianying_uia_probe", PROBE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Jianying UI probe")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bring_foreground(handle: int) -> None:
    if not win32gui.IsWindow(handle):
        raise RuntimeError("Jianying window no longer exists")
    win32gui.ShowWindow(handle, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(handle)
    deadline = time.time() + 3
    while time.time() < deadline:
        if win32gui.GetForegroundWindow() == handle:
            return
        time.sleep(0.1)
    raise RuntimeError("Jianying could not obtain foreground focus; automation stopped")


def click_relative(handle: int, x: int, y: int) -> None:
    bring_foreground(handle)
    left, top, _, _ = win32gui.GetWindowRect(handle)
    win32api.SetCursorPos((left + x, top + y))
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0)


def scaled_point(handle: int, x: int, y: int, reference_width: int, reference_height: int) -> tuple[int, int]:
    left, top, right, bottom = win32gui.GetWindowRect(handle)
    width = max(right - left, 1)
    height = max(bottom - top, 1)
    return round(x * width / reference_width), round(y * height / reference_height)


def click_reference(handle: int, x: int, y: int, reference_width: int, reference_height: int) -> None:
    click_relative(handle, *scaled_point(handle, x, y, reference_width, reference_height))


def window_area(window) -> int:
    rect = window.rectangle()
    return rect.width() * rect.height()


def is_editor_sized(window) -> bool:
    rect = window.rectangle()
    return rect.width() >= 1000 and rect.height() >= 700


def dismiss_known_popup(window) -> bool:
    class_name = window.element_info.class_name or ""
    if class_name.startswith("SplashDialog"):
        try:
            print(f"[jianying-ui] dismissing popup: class={class_name} rect={window.rectangle()}", flush=True)
            click_reference(window.handle, 390, 457, 580, 502)
            time.sleep(0.8)
            return True
        except Exception:
            return False
    return False


def window_classes(window) -> set[str]:
    try:
        return {item.element_info.class_name for item in window.descendants()}
    except Exception:
        return set()


def has_class(classes: set[str], prefix: str) -> bool:
    return any(value.startswith(prefix) for value in classes)


def wait_for_editor(probe, executable: Path, home_handle: int, timeout: float):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for candidate in sorted(probe.matching_windows(executable), key=window_area, reverse=True):
            if dismiss_known_popup(candidate):
                continue
            if not is_editor_sized(candidate):
                continue
            classes = window_classes(candidate)
            if candidate.handle != home_handle or has_class(classes, "MainTimeLine_"):
                try:
                    bring_foreground(candidate.handle)
                    return Desktop(backend="uia").window(handle=candidate.handle)
                except Exception:
                    continue
        time.sleep(0.5)
    diagnostic = ROOT / "output" / "jianying_wait_editor_timeout.png"
    candidates = sorted(probe.matching_windows(executable), key=window_area, reverse=True)
    for candidate in candidates:
        try:
            rect = candidate.rectangle()
            print(f"[jianying-ui] timeout window: handle={candidate.handle} class={candidate.element_info.class_name!r} title={candidate.window_text()!r} rect={rect}", flush=True)
        except Exception:
            continue
    if candidates:
        try:
            candidates[0].capture_as_image().save(diagnostic)
            print(f"[jianying-ui] timeout screenshot: {diagnostic}", flush=True)
        except Exception:
            pass
    raise RuntimeError("Jianying editor did not appear")


def new_draft_confirmation_visible(handle: int) -> bool:
    try:
        image = Desktop(backend="uia").window(handle=handle).capture_as_image().convert("RGB")
        x = round(712 * image.width / 1600)
        y = round(539 * image.height / 900)
        red, green, blue = image.getpixel((x, y))
        return red < 80 and green > 130 and blue > 130
    except Exception:
        return False


def wait_for_new_draft(config: Path, before: Path | None, timeout: float) -> Path | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        after = latest_draft(config)
        if after and after != before:
            return after
        time.sleep(0.4)
    return None


def open_editor(probe, config: Path, create: bool, reuse_open_editor: bool, timeout: float):
    exe = probe.executable_from_config(config)
    if not exe.exists():
        raise RuntimeError(f"Jianying executable not found: {exe}")
    subprocess.Popen([str(exe)], cwd=str(exe.parent))
    deadline = time.time() + timeout
    home_handle = 0
    create_shortcut_attempted = False
    before_create = latest_draft(config) if create else None
    while time.time() < deadline:
        candidates = sorted(probe.matching_windows(exe), key=window_area, reverse=True)
        for candidate in candidates:
            if dismiss_known_popup(candidate):
                continue
            if not is_editor_sized(candidate):
                continue
            if reuse_open_editor:
                bring_foreground(candidate.handle)
                print(f"[jianying-ui] reusing open editor: handle={candidate.handle} rect={candidate.rectangle()}", flush=True)
                return Desktop(backend="uia").window(handle=candidate.handle)
            if create and not create_shortcut_attempted:
                create_shortcut_attempted = True
                bring_foreground(candidate.handle)
                print(f"[jianying-ui] trying Ctrl+N create: handle={candidate.handle} rect={candidate.rectangle()}", flush=True)
                send_keys("^n", pause=0.05)
                time.sleep(1)
                if new_draft_confirmation_visible(candidate.handle):
                    click_reference(candidate.handle, 748, 539, 1600, 900)
                if wait_for_new_draft(config, before_create, min(10.0, timeout)):
                    time.sleep(2)
                    ready = sorted(probe.matching_windows(exe), key=window_area, reverse=True)
                    ready = [window for window in ready if is_editor_sized(window)]
                    if ready:
                        bring_foreground(ready[0].handle)
                        return Desktop(backend="uia").window(handle=ready[0].handle)
                print("[jianying-ui] Ctrl+N create did not produce a draft; falling back to the home button", flush=True)
            classes = window_classes(candidate)
            if has_class(classes, "MainTimeLine_") and not create:
                try:
                    bring_foreground(candidate.handle)
                    return Desktop(backend="uia").window(handle=candidate.handle)
                except Exception:
                    continue
            try:
                bring_foreground(candidate.handle)
                print(f"[jianying-ui] clicking home window: handle={candidate.handle} rect={candidate.rectangle()} create={create}", flush=True)
                home_x, home_y = (743, 120) if create else (350, 410)
                click_reference(candidate.handle, home_x, home_y, 1215, 808)
                home_handle = candidate.handle
                break
            except Exception:
                continue
        if home_handle:
            break
        time.sleep(0.5)
    if not home_handle:
        other_windows = probe.matching_windows()
        if other_windows:
            raise RuntimeError(
                "检测到另一个剪映安装正在运行。请先关闭它，"
                "或在烤肉器设置中选择该实例对应的 JianyingPro.exe。"
            )
        raise RuntimeError("Jianying home page did not become ready")
    editor = wait_for_editor(probe, exe, home_handle, timeout)
    bring_foreground(editor.handle)
    return editor


def wait_for_file_dialog(timeout: float):
    deadline = time.time() + timeout
    desktop = Desktop(backend="uia")
    while time.time() < deadline:
        handles: list[int] = []

        def visit(handle: int, _: object) -> None:
            if win32gui.IsWindowVisible(handle) and win32gui.GetClassName(handle) == "#32770":
                handles.append(handle)

        win32gui.EnumWindows(visit, None)
        if handles:
            handle = max(handles, key=lambda item: (win32gui.GetWindowRect(item)[2] - win32gui.GetWindowRect(item)[0]) * (win32gui.GetWindowRect(item)[3] - win32gui.GetWindowRect(item)[1]))
            return desktop.window(handle=handle)
        time.sleep(0.2)
    raise RuntimeError("Windows file picker did not appear")


def choose_file(path: Path, timeout: float) -> None:
    if not path.exists():
        raise RuntimeError(f"input file does not exist: {path}")
    dialog = wait_for_file_dialog(timeout)
    edit = dialog.child_window(auto_id="1148", control_type="Edit")
    edit.wait("visible enabled ready", timeout=timeout)
    edit.set_edit_text(str(path.resolve()))
    open_button = dialog.child_window(auto_id="1", control_type="Button")
    open_button.wait("visible enabled ready", timeout=timeout)
    open_button.click_input()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            exists = dialog.exists(timeout=0.1)
        except Exception:
            return
        if not exists:
            return
        time.sleep(0.2)
    raise RuntimeError(f"file picker did not close after selecting: {path}")


def click_and_choose_file(
    editor_handle: int,
    path: Path,
    timeout: float,
    candidates: list[tuple[int, int]],
    reference_width: int = 1600,
    reference_height: int = 900,
) -> None:
    deadline = time.time() + timeout
    attempts: list[str] = []
    for x, y in candidates:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        click_reference(editor_handle, x, y, reference_width, reference_height)
        try:
            dialog = wait_for_file_dialog(min(5.0, remaining))
        except RuntimeError:
            attempts.append(f"({x},{y})")
            continue

        if not path.exists():
            raise RuntimeError(f"input file does not exist: {path}")
        edit = dialog.child_window(auto_id="1148", control_type="Edit")
        edit.wait("visible enabled ready", timeout=max(1.0, deadline - time.time()))
        edit.set_edit_text(str(path.resolve()))
        open_button = dialog.child_window(auto_id="1", control_type="Button")
        open_button.wait("visible enabled ready", timeout=max(1.0, deadline - time.time()))
        open_button.click_input()
        while time.time() < deadline:
            try:
                exists = dialog.exists(timeout=0.1)
            except Exception:
                return
            if not exists:
                return
            time.sleep(0.2)
        raise RuntimeError(f"file picker did not close after selecting: {path}")

    attempted = ", ".join(attempts) or "none"
    raise RuntimeError(f"file picker did not appear after clicks: {attempted}")


def import_video(editor_handle: int, video: Path, timeout: float, major_version: int) -> None:
    del major_version
    bring_foreground(editor_handle)
    send_keys("^i", pause=0.05)
    choose_file(video, timeout)
    time.sleep(3)


def add_first_media_to_timeline(editor_handle: int) -> None:
    click_reference(editor_handle, 46, 62, 1600, 900)
    time.sleep(0.8)
    left, top, _, _ = win32gui.GetWindowRect(editor_handle)
    hover_x, hover_y = scaled_point(editor_handle, 220, 235, 1600, 900)
    win32api.SetCursorPos((left + hover_x, top + hover_y))
    time.sleep(0.5)
    click_reference(editor_handle, 283, 271, 1600, 900)
    time.sleep(3)


def import_subtitle(editor_handle: int, subtitle: Path, timeout: float, major_version: int) -> None:
    if major_version >= 10:
        click_reference(editor_handle, 163, 62, 1600, 900)
        time.sleep(0.8)
        click_and_choose_file(editor_handle, subtitle, timeout, [(455, 205), (455, 220)])
    elif major_version >= 8:
        click_reference(editor_handle, 163, 62, 1600, 900)
        time.sleep(0.8)
        click_reference(editor_handle, 456, 200, 1600, 900)
        choose_file(subtitle, timeout)
    else:
        click_reference(editor_handle, 188, 62, 1600, 900)
        time.sleep(0.4)
        click_reference(editor_handle, 70, 435, 1600, 900)
        time.sleep(0.4)
        click_reference(editor_handle, 400, 190, 1600, 900)
        choose_file(subtitle, timeout)
    time.sleep(3)


def add_first_subtitle_to_timeline(editor_handle: int) -> None:
    click_reference(editor_handle, 163, 62, 1600, 900)
    time.sleep(0.8)
    left, top, _, _ = win32gui.GetWindowRect(editor_handle)
    hover_x, hover_y = scaled_point(editor_handle, 195, 350, 1600, 900)
    win32api.SetCursorPos((left + hover_x, top + hover_y))
    time.sleep(0.5)
    click_reference(editor_handle, 230, 386, 1600, 900)
    time.sleep(3)


def latest_draft(config: Path) -> Path | None:
    data = json.loads(config.read_text(encoding="utf-8"))
    root = Path(data.get("local", {}).get("draft_root", ""))
    if not root.exists():
        return None
    drafts = [path for path in root.iterdir() if (path / "draft_content.json").exists()]
    return max(drafts, key=lambda path: (path / "draft_content.json").stat().st_mtime) if drafts else None


def capture_window(handle: int, path: Path) -> None:
    bring_foreground(handle)
    path.parent.mkdir(parents=True, exist_ok=True)
    Desktop(backend="uia").window(handle=handle).capture_as_image().save(path)


def capture_diagnostic(probe, executable: Path, editor_handle: int | None, path: Path) -> tuple[bool, str]:
    handles: list[int] = []
    if editor_handle and win32gui.IsWindow(editor_handle):
        handles.append(editor_handle)
    for candidate in sorted(probe.matching_windows(executable), key=window_area, reverse=True):
        if candidate.handle not in handles:
            handles.append(candidate.handle)
    for handle in handles:
        try:
            capture_window(handle, path)
            return True, ""
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
    return False, locals().get("last_error", "no Jianying window available")


def draft_status(path: Path | None) -> dict[str, object]:
    content = path / "draft_content.json" if path else None
    exists = bool(content and content.exists())
    return {
        "draft_after": str(path or ""),
        "draft_content": str(content or ""),
        "draft_content_size": content.stat().st_size if exists else 0,
        "draft_content_mtime": (
            datetime.fromtimestamp(content.stat().st_mtime).isoformat(timespec="seconds") if exists else ""
        ),
    }


def write_status(path: Path, status: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(status, ensure_ascii=False, indent=2), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Guarded Jianying video/subtitle UI importer")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--video", type=Path)
    parser.add_argument("--subtitle", type=Path)
    parser.add_argument("--add-first-media", action="store_true", help="Add the first existing media item to the timeline")
    parser.add_argument("--add-first-subtitle", action="store_true", help="Add the first existing local subtitle item to the timeline")
    parser.add_argument("--skip-add-to-timeline", action="store_true", help="Import --video into the media bin without adding it to the timeline")
    parser.add_argument("--skip-add-subtitle-to-timeline", action="store_true", help="Import --subtitle into local subtitles without adding it to the timeline")
    parser.add_argument("--create", action="store_true", help="Create a new draft; otherwise open the latest draft")
    parser.add_argument("--reuse-open-editor", action="store_true", help="Use the currently open editor without navigating from the home page")
    parser.add_argument("--timeout", type=float, default=40.0)
    parser.add_argument("--screenshot", type=Path, default=ROOT / "output" / "jianying_ui_import.png")
    parser.add_argument("--status-out", type=Path, default=ROOT / "output" / "jianying_ui_import.json")
    args = parser.parse_args()
    if args.create and args.reuse_open_editor:
        parser.error("--create and --reuse-open-editor cannot be used together")
    if not args.video and not args.subtitle and not args.add_first_media and not args.add_first_subtitle:
        parser.error("at least one import or add action is required")

    probe = load_probe_module()
    executable = probe.executable_from_config(args.config)
    major_version = installed_major_version(args.config)
    before = latest_draft(args.config)
    editor_handle: int | None = None
    steps: list[str] = []
    stage = "opening_editor"
    try:
        editor = open_editor(probe, args.config, args.create, args.reuse_open_editor, args.timeout)
        editor_handle = editor.handle
        steps.append("editor_opened")
        if args.video:
            stage = "importing_video_to_media_bin"
            import_video(editor_handle, args.video, args.timeout, major_version)
            steps.append("video_imported_to_media_bin")
            if not args.skip_add_to_timeline:
                stage = "adding_video_to_timeline"
                add_first_media_to_timeline(editor_handle)
                steps.append("video_added_to_timeline")
        elif args.add_first_media:
            stage = "adding_existing_video_to_timeline"
            add_first_media_to_timeline(editor_handle)
            steps.append("existing_video_added_to_timeline")
        if args.subtitle:
            stage = "importing_subtitle"
            import_subtitle(editor_handle, args.subtitle, args.timeout, major_version)
            steps.append("subtitle_imported")
            if not args.skip_add_subtitle_to_timeline:
                stage = "adding_subtitle_to_timeline"
                add_first_subtitle_to_timeline(editor_handle)
                steps.append("subtitle_added_to_timeline")
        if args.add_first_subtitle:
            stage = "adding_subtitle_to_timeline"
            add_first_subtitle_to_timeline(editor_handle)
            steps.append("subtitle_added_to_timeline")
        stage = "capturing_result"
        time.sleep(3)
        capture_window(editor_handle, args.screenshot)
        after = latest_draft(args.config)
        status: dict[str, object] = {
            "state": "completed",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "steps": steps,
            "jianying_executable": str(executable),
            "jianying_major_version": major_version,
            "draft_before": str(before or ""),
            "screenshot": str(args.screenshot),
            **draft_status(after),
        }
        write_status(args.status_out, status)
        return 0
    except Exception as exc:
        captured, capture_error = capture_diagnostic(probe, executable, editor_handle, args.screenshot)
        after = latest_draft(args.config)
        status = {
            "state": "failed",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "failed_stage": stage,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "steps": steps,
            "jianying_executable": str(executable),
            "jianying_major_version": major_version,
            "draft_before": str(before or ""),
            "screenshot": str(args.screenshot) if captured else "",
            "screenshot_error": capture_error,
            **draft_status(after),
        }
        write_status(args.status_out, status)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
