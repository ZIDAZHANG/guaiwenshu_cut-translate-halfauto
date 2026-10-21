#!/usr/bin/env python3
"""Compatibility regressions; optional real PE fixture uses MAGIA_TEST_EXECUTABLE."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from magia_utf8_runtime import (
    COOKIE, COOKIE_MAGIC, has_utf8_manifest, prepare_utf8_executable,
    pyinstaller_payload, read_manifest, utf8_manifest,
)


class ManifestTests(unittest.TestCase):
    def test_preserves_settings_and_unicode(self):
        original = b"""<assembly xmlns="urn:schemas-microsoft-com:asm.v1">
        <description>Magia</description>
        <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security /></trustInfo>
        <application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings>
        <longPathAware xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">true</longPathAware>
        </windowsSettings></application></assembly>"""
        updated = utf8_manifest(original)
        self.assertTrue(has_utf8_manifest(updated))
        root = ET.fromstring(updated)
        self.assertIsNotNone(root.find("{urn:schemas-microsoft-com:asm.v3}trustInfo"))
        self.assertEqual(root.find(".//{http://schemas.microsoft.com/SMI/2016/WindowsSettings}longPathAware").text, "true")
        self.assertEqual(utf8_manifest(updated), updated)

    def test_archive_preserves_exact_payload(self):
        payload = "original embedded text・😃".encode("utf-8")
        cookie = COOKIE.pack(COOKIE_MAGIC, len(payload) + COOKIE.size, 0, len(payload), 312, b"python312.dll")
        archive = payload + cookie
        self.assertEqual(pyinstaller_payload(b"PE resources" + archive), archive)

    def test_invalid_archive_is_rejected(self):
        for data in (b"", b"PE", COOKIE_MAGIC, COOKIE.pack(COOKIE_MAGIC, 9999, 0, 1, 312, b"python312.dll")):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    pyinstaller_payload(data)


@unittest.skipUnless(os.name == "nt" and os.environ.get("MAGIA_TEST_EXECUTABLE"),
                     "requires explicitly selected real Magia PE fixture")
class WindowsCopyTests(unittest.TestCase):
    def test_source_is_unchanged_and_cache_tracks_corruption_and_dependencies(self):
        native = Path(os.environ["MAGIA_TEST_EXECUTABLE"])
        original = native.read_bytes()
        root = (Path(__file__).resolve().parents[1] / "app") / "output"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="magia_utf8_test_", dir=root) as temporary:
            base = Path(temporary)
            source_dir = base / "原始模型 😀"
            source_dir.mkdir()
            source = source_dir / native.name
            source.write_bytes(original)
            internal = source_dir / "_internal"
            internal.mkdir()
            dependency = internal / "dependency.bin"
            dependency.write_bytes(b"original dependency")
            cache = base / "候选_UTF8"
            first, evidence = prepare_utf8_executable(source, cache)
            self.assertNotEqual(first, source)
            self.assertTrue(evidence["embedded_archive_unchanged"])
            self.assertTrue(has_utf8_manifest(read_manifest(first)[0]))
            self.assertEqual(pyinstaller_payload(first.read_bytes()), pyinstaller_payload(original))
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(prepare_utf8_executable(source, cache)[0], first)
            first.write_bytes(b"broken cache")
            repaired, _ = prepare_utf8_executable(source, cache)
            self.assertNotEqual(repaired, first)
            self.assertEqual(pyinstaller_payload(repaired.read_bytes()), pyinstaller_payload(original))
            dependency.write_bytes(b"changed dependency")
            changed, _ = prepare_utf8_executable(source, cache)
            self.assertNotEqual(changed, repaired)
            self.assertEqual((changed.parent / "_internal/dependency.bin").read_bytes(), b"changed dependency")
            self.assertEqual(prepare_utf8_executable(changed, cache)[0], changed)
            self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), hashlib.sha256(original).digest())


if __name__ == "__main__":
    unittest.main()
