#!/usr/bin/env python3
"""Start or inspect the configured Jianying installation and its UI tree."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

from pywinauto import Desktop
import win32api
import win32con
import win32gui
import win32process


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "roast_pipeline.local.json"


def version_tuple(name: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in name.split("."))
    except ValueError:
        return None


def executable_from_config(path: Path) -> Path:
    data = json.loads(path.read_text(encoding="utf-8"))
    configured = Path(data.get("local", {}).get("jianying_exe", ""))
    if configured.is_file():
        return configured

    apps = Path.home() / "AppData" / "Local" / "JianyingPro" / "Apps"
    versioned: list[tuple[tuple[int, ...], Path]] = []
    if apps.exists():
        for directory in apps.iterdir():
            candidate = directory / "JianyingPro.exe"
            version = version_tuple(directory.name)
            if directory.is_dir() and candidate.exists() and version is not None:
                versioned.append((version, candidate))
    if versioned:
        return max(versioned, key=lambda item: item[0])[1]
    current_user_launcher = apps / "JianyingPro.exe"
    if current_user_launcher.exists():
        return current_user_launcher
    return configured


def same_installation(expected: Path, actual: Path) -> bool:
    """Return whether a window executable belongs to the selected installation."""
    expected = Path(expected)
    actual = Path(actual)
    if str(expected.resolve(strict=False)).casefold() == str(actual.resolve(strict=False)).casefold():
        return True

    # A versioned executable is an exact choice. Do not accept another version
    # merely because both versions live below the same Apps directory.
    if version_tuple(expected.parent.name) is not None:
        return False

    # A top-level launcher normally starts the real executable in a versioned
    # child directory, for example Apps/JianyingPro.exe -> Apps/11.x/...exe.
    try:
        actual.resolve(strict=False).relative_to(expected.parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def window_executable(handle: int) -> Path | None:
    _, pid = win32process.GetWindowThreadProcessId(handle)
    process = None
    try:
        process = win32api.OpenProcess(
            win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ,
            False,
            pid,
        )
        return Path(win32process.GetModuleFileNameEx(process, 0))
    except Exception:
        return None
    finally:
        if process is not None:
            win32api.CloseHandle(process)


def matching_windows(expected_executable: Path | None = None) -> list:
    handles: list[int] = []

    def visit(handle: int, _: object) -> None:
        if not win32gui.IsWindowVisible(handle):
            return
        title = win32gui.GetWindowText(handle)
        class_name = win32gui.GetClassName(handle)
        executable = window_executable(handle)
        haystack = f"{title} {class_name} {executable or ''}".lower()
        if not any(token in haystack for token in ("剪映", "jianying", "lveditor")):
            return
        if expected_executable is not None:
            if executable is None or not same_installation(expected_executable, executable):
                return
        handles.append(handle)

    win32gui.EnumWindows(visit, None)
    desktop = Desktop(backend="uia")
    return [desktop.window(handle=handle) for handle in handles]


def describe(window, max_depth: int) -> list[dict]:
    if max_depth < 0:
        return []
    root_rect = window.rectangle()
    rows = []
    for element in window.descendants():
        depth = 0
        parent = element.parent()
        while parent is not None and parent.element_info != window.element_info:
            depth += 1
            parent = parent.parent()
            if depth > max_depth:
                break
        if depth > max_depth:
            continue
        rect = element.rectangle()
        rows.append(
            {
                "depth": depth,
                "control_type": element.element_info.control_type,
                "name": element.window_text(),
                "automation_id": element.element_info.automation_id,
                "class_name": element.element_info.class_name,
                "rect": [rect.left - root_rect.left, rect.top - root_rect.top, rect.right - root_rect.left, rect.bottom - root_rect.top],
                "enabled": element.is_enabled(),
                "visible": element.is_visible(),
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Jianying UI Automation probe")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--wait", type=float, default=15.0)
    parser.add_argument("--max-depth", type=int, default=7)
    parser.add_argument("--focus", action="store_true", help="Bring Jianying to the foreground before capture")
    parser.add_argument("--out", type=Path, default=ROOT / "output" / "jianying_uia_probe.json")
    parser.add_argument("--screenshot", type=Path, default=ROOT / "output" / "jianying_uia_probe.png")
    args = parser.parse_args()

    exe = executable_from_config(args.config)
    if args.start and not matching_windows(exe):
        if not exe.exists():
            raise SystemExit(f"Jianying executable not found: {exe}")
        subprocess.Popen([str(exe)], cwd=str(exe.parent))
        deadline = time.time() + args.wait
        while time.time() < deadline and not matching_windows(exe):
            time.sleep(1)

    report = {"executable": str(exe), "windows": []}
    for window in matching_windows(exe):
        if args.focus:
            try:
                window.restore()
                window.set_focus()
                time.sleep(1)
            except Exception as exc:
                print(f"Focus failed: {exc}")
        try:
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            window.capture_as_image().save(args.screenshot)
        except Exception as exc:
            print(f"Screenshot failed: {exc}")
        report["windows"].append(
            {
                "title": window.window_text(),
                "class_name": window.element_info.class_name,
                "handle": window.handle,
                "controls": describe(window, args.max_depth),
            }
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Jianying windows: {len(report['windows'])}")
    for item in report["windows"]:
        named = [control for control in item["controls"] if control["name"].strip()]
        print(f"- {item['title']!r}: controls={len(item['controls'])}, named={len(named)}")
        for control in named[:80]:
            print(f"  {control['control_type']}: {control['name']!r} id={control['automation_id']!r} rect={control['rect']}")
    print(f"Report: {args.out}")
    if args.screenshot.exists():
        print(f"Screenshot: {args.screenshot}")
    return 0 if report["windows"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
