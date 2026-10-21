#!/usr/bin/env python3
"""Prepare a private UTF-8 Magia executable without changing its Python archive."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import struct
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

CODEPAGE_NS = "http://schemas.microsoft.com/SMI/2019/WindowsSettings"
ASM_NS = "urn:schemas-microsoft-com:asm.v3"
COOKIE_MAGIC = b"MEI\014\013\012\013\016"
COOKIE = struct.Struct("!8sIIII64s")


def pyinstaller_payload(data: bytes) -> bytes:
    offset = data.rfind(COOKIE_MAGIC)
    if offset < 0 or offset + COOKIE.size > len(data):
        raise ValueError("Magia executable has no readable PyInstaller archive")
    magic, package_size, toc_offset, toc_size, _version, _library = COOKIE.unpack_from(data, offset)
    end = offset + COOKIE.size
    start = end - package_size
    if magic != COOKIE_MAGIC or start < 0 or toc_offset + toc_size > package_size - COOKIE.size:
        raise ValueError("Magia executable has an invalid PyInstaller archive")
    return data[start:end]


def utf8_manifest(data: bytes) -> bytes:
    root = ET.fromstring(data)
    application = root.find(f"{{{ASM_NS}}}application")
    if application is None:
        application = ET.SubElement(root, f"{{{ASM_NS}}}application")
    settings = application.find(f"{{{ASM_NS}}}windowsSettings")
    if settings is None:
        settings = ET.SubElement(application, f"{{{ASM_NS}}}windowsSettings")
    codepage = settings.find(f"{{{CODEPAGE_NS}}}activeCodePage")
    if codepage is None:
        codepage = ET.SubElement(settings, f"{{{CODEPAGE_NS}}}activeCodePage")
    codepage.text = "UTF-8"
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def has_utf8_manifest(data: bytes) -> bool:
    page = ET.fromstring(data).find(f".//{{{CODEPAGE_NS}}}activeCodePage")
    return page is not None and (page.text or "").strip().upper() == "UTF-8"


def resource_api():
    from ctypes import wintypes as wt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "LoadLibraryExW": ([wt.LPCWSTR, wt.HANDLE, wt.DWORD], wt.HANDLE),
        "FreeLibrary": ([wt.HANDLE], wt.BOOL),
        "FindResourceW": ([wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p], wt.HANDLE),
        "SizeofResource": ([wt.HANDLE, wt.HANDLE], wt.DWORD),
        "LoadResource": ([wt.HANDLE, wt.HANDLE], wt.HANDLE),
        "LockResource": ([wt.HANDLE], ctypes.c_void_p),
        "BeginUpdateResourceW": ([wt.LPCWSTR, wt.BOOL], wt.HANDLE),
        "UpdateResourceW": ([wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, wt.WORD, ctypes.c_void_p, wt.DWORD], wt.BOOL),
        "EndUpdateResourceW": ([wt.HANDLE, wt.BOOL], wt.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    return kernel


def read_manifest(executable: Path) -> tuple[bytes, list[int]]:
    from ctypes import wintypes as wt
    kernel = resource_api()
    module = kernel.LoadLibraryExW(str(executable), None, 0x2 | 0x20)
    if not module:
        raise ctypes.WinError(ctypes.get_last_error())
    languages = []
    callback_type = ctypes.WINFUNCTYPE(wt.BOOL, wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, wt.WORD, ctypes.c_ssize_t)

    @callback_type
    def collect(_module, _kind, _name, language, _parameter):
        languages.append(language)
        return True

    kernel.EnumResourceLanguagesW.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, callback_type, ctypes.c_ssize_t]
    kernel.EnumResourceLanguagesW.restype = wt.BOOL
    try:
        resource = kernel.FindResourceW(module, ctypes.c_void_p(1), ctypes.c_void_p(24))
        if not resource:
            raise ctypes.WinError(ctypes.get_last_error())
        size = kernel.SizeofResource(module, resource)
        loaded = kernel.LoadResource(module, resource)
        address = kernel.LockResource(loaded)
        if not size or not address:
            raise ctypes.WinError(ctypes.get_last_error())
        data = ctypes.string_at(address, size)
        if not kernel.EnumResourceLanguagesW(module, ctypes.c_void_p(24), ctypes.c_void_p(1), collect, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        return data, languages
    finally:
        kernel.FreeLibrary(module)


def patch_private_executable(executable: Path, manifest: bytes, languages: list[int]) -> None:
    original = executable.read_bytes()
    archive = pyinstaller_payload(original)
    kernel = resource_api()
    handle = kernel.BeginUpdateResourceW(str(executable), False)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        buffer = ctypes.create_string_buffer(manifest)
        for language in languages:
            if not kernel.UpdateResourceW(handle, ctypes.c_void_p(24), ctypes.c_void_p(1),
                                          language, buffer, len(manifest)):
                raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        kernel.EndUpdateResourceW(handle, True)
        raise
    if not kernel.EndUpdateResourceW(handle, False):
        raise ctypes.WinError(ctypes.get_last_error())
    # Resource updates discard PyInstaller's appended archive on Windows.
    with executable.open("ab") as output:
        output.write(archive)
    if pyinstaller_payload(executable.read_bytes()) != archive:
        raise ValueError("UTF-8 compatibility copy lost or changed the embedded Python archive")
    if not has_utf8_manifest(read_manifest(executable)[0]):
        raise ValueError("UTF-8 compatibility manifest was not installed")


def internal_snapshot(directory: Path) -> list[list]:
    return [[str(path.relative_to(directory)), path.stat().st_size, path.stat().st_mtime_ns]
            for path in sorted(directory.rglob("*")) if path.is_file()]


def prepare_utf8_executable(source: Path, cache_root: Path) -> tuple[Path, dict]:
    source = source.resolve()
    if os.name != "nt":
        return source, {"mode": "native"}
    manifest, languages = read_manifest(source)
    if has_utf8_manifest(manifest):
        return source, {"mode": "native_utf8", "executable": str(source)}
    source_bytes = source.read_bytes()
    archive = pyinstaller_payload(source_bytes)
    internal = source.parent / "_internal"
    if not internal.is_dir():
        raise ValueError(f"Magia runtime dependencies are missing: {internal}")
    identity = {"source": str(source), "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
                "internal": internal_snapshot(internal), "compatibility_version": 1}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
    cache_root = cache_root.resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    for report_path in sorted(cache_root.glob(f"{key}_*/compatibility.json")):
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            executable = report_path.parent / source.name
            if (report["identity"] == identity and
                    hashlib.sha256(executable.read_bytes()).hexdigest() == report["executable_sha256"] and
                    internal_snapshot(report_path.parent / "_internal") == identity["internal"]):
                return executable, report["evidence"]
        except (OSError, KeyError, ValueError):
            continue
    target = cache_root / f"{key}_{uuid.uuid4().hex}"
    target.mkdir()
    executable = target / source.name
    shutil.copy2(source, executable)
    shutil.copytree(internal, target / "_internal")
    patch_private_executable(executable, utf8_manifest(manifest), languages)
    if source.read_bytes() != source_bytes or internal_snapshot(internal) != identity["internal"]:
        raise ValueError("Magia source runtime changed while making the compatibility copy; retry")
    evidence = {"mode": "private_utf8_copy", "source": str(source), "executable": str(executable),
                "source_sha256": identity["source_sha256"], "archive_sha256": hashlib.sha256(archive).hexdigest(),
                "embedded_archive_unchanged": True, "manifest_setting": "activeCodePage=UTF-8"}
    report = {"identity": identity, "evidence": evidence,
              "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest()}
    (target / "compatibility.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return executable, evidence
