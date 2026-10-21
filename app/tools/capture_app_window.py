#!/usr/bin/env python3
"""Capture one visible Windows application window for local visual QA."""

from __future__ import annotations

import argparse
import ctypes
import json
import time
from ctypes import wintypes
from pathlib import Path

from PIL import ImageGrab


def find_window(title_fragment: str) -> tuple[int, str]:
    user32 = ctypes.windll.user32
    matches: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        title = buffer.value
        if title_fragment.casefold() in title.casefold():
            matches.append((int(hwnd), title))
        return True

    user32.EnumWindows(callback, 0)
    if not matches:
        raise RuntimeError(f"No visible window contains title: {title_fragment}")
    return matches[0]


def capture(hwnd: int, destination: Path, click: tuple[int, int] | None = None) -> dict:
    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, 9)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.5)
    rectangle = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rectangle)):
        raise ctypes.WinError()
    bounds = (rectangle.left, rectangle.top, rectangle.right, rectangle.bottom)
    if click is not None:
        user32.SetCursorPos(rectangle.left + click[0], rectangle.top + click[1])
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
        time.sleep(0.4)
    image = ImageGrab.grab(bbox=bounds, all_screens=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination)
    return {"path": str(destination.resolve()), "bounds": bounds, "size": image.size}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--click", nargs=2, type=int, metavar=("X", "Y"))
    args = parser.parse_args()
    ctypes.windll.user32.SetProcessDPIAware()
    hwnd, title = find_window(args.title)
    result = capture(hwnd, args.output, tuple(args.click) if args.click else None)
    result.update({"hwnd": hwnd, "title": title})
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
