#!/usr/bin/env python3
"""Behavior checks for native clock recovery; provider outputs are fixtures."""
import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import voicetransl_adapter as adapter


class ClockRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.exe = self.root / "whisper-cli.exe"
        self.model = self.root / "ggml-large-v3.bin"
        self.audio = self.root / "complete_audio.wav"
        self.vad_model = self.root / "ggml-silero-v5.1.2.bin"
        for path in (self.exe, self.model, self.audio, self.vad_model):
            path.write_bytes(b"fixture")
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"recognition": {"word_timestamps": True,
            "dtw_model": "large.v3", "vad_model": str(self.vad_model)}}), encoding="utf-8")
        self.output = self.root / "task/subs/jp.srt"
        self.args = argparse.Namespace(config=self.config, executable=self.exe,
            model=self.model, language="ja", vad=True, audio=self.audio, output=self.output)
        self.commands = []

    def tearDown(self):
        self.temp.cleanup()

    def emit(self, command, cwd, log, *, conflict=False, code=0, corrupt=False):
        self.commands.append(command)
        base = Path(command[command.index("-of") + 1])
        base.parent.mkdir(parents=True, exist_ok=True)
        Path(str(base) + ".srt").write_text("1\n00:00:00,500 --> 00:00:01,000\nはい\n", encoding="utf-8")
        document = {"transcription": [{"text": "はい", "tokens": [{"text": "はい", "id": 1,
            "offsets": {"from": 490 if conflict else 600, "to": 900}, "t_dtw": 70}]}]}
        Path(str(base) + ".json").write_text("broken" if corrupt else json.dumps(document, ensure_ascii=False), encoding="utf-8")
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("fixture native log", encoding="utf-8")
        return code

    def recovery(self):
        return next((self.root / "task/logs/voicetransl/recognition_attempts").glob("*/recovery.json"))

    def test_conflict_reprocesses_complete_audio_and_preserves_both_raw_results(self):
        def provider(command, cwd, log):
            return self.emit(command, cwd, log, conflict="--vad" in command)
        with patch.object(adapter, "stream_process", provider):
            self.assertEqual(adapter.recognize(self.args), 0)
        self.assertEqual(len(self.commands), 2)
        self.assertIn("--vad", self.commands[0])
        self.assertNotIn("--vad", self.commands[1])
        self.assertNotIn("--vad-model", self.commands[1])
        for command in self.commands:
            self.assertIn(str(self.audio.resolve()), command)
            self.assertEqual(command[command.index("-m") + 1], str(self.model))
        audit = json.loads(self.recovery().read_text(encoding="utf-8"))
        self.assertEqual(audit["selected"], "without_vad")
        folder = self.recovery().parent
        self.assertEqual(json.loads((folder / "vad/jp.word_times.json").read_text())["timing_evidence_status"], "unusable")
        self.assertEqual(json.loads((folder / "without_vad/jp.word_times.json").read_text())["timing_evidence_status"], "usable")
        self.assertEqual(self.output.with_suffix(".whisper.json").read_bytes(), (folder / "without_vad/jp.json").read_bytes())
        self.assertNotEqual((folder / "vad/jp.whisper.json").read_bytes(), (folder / "without_vad/jp.json").read_bytes())

    def test_valid_initial_clock_does_not_retry_or_disable_user_vad(self):
        with patch.object(adapter, "stream_process", self.emit):
            adapter.recognize(self.args)
        self.assertEqual(len(self.commands), 1)
        self.assertIn("--vad", self.commands[0])

    def test_failed_recovery_never_selects_partial_output(self):
        def provider(command, cwd, log):
            return self.emit(command, cwd, log, conflict="--vad" in command,
                             code=0 if "--vad" in command else 7)
        with patch.object(adapter, "stream_process", provider):
            adapter.recognize(self.args)
        audit = json.loads(self.recovery().read_text())
        self.assertEqual(audit["selected"], "vad")
        self.assertEqual(audit["recovery_exit_code"], 7)
        self.assertEqual(self.output.with_suffix(".whisper.json").read_bytes(),
                         (self.recovery().parent / "vad/jp.whisper.json").read_bytes())
        self.assertEqual(json.loads(self.output.with_suffix(".word_times.json").read_text())["timing_evidence_status"], "unusable")

    def test_corrupt_recovery_evidence_does_not_replace_initial_source(self):
        def provider(command, cwd, log):
            return self.emit(command, cwd, log, conflict="--vad" in command, corrupt="--vad" not in command)
        with patch.object(adapter, "stream_process", provider):
            adapter.recognize(self.args)
        self.assertEqual(json.loads(self.recovery().read_text())["selected"], "vad")
        self.assertTrue((self.recovery().parent / "without_vad/jp.json").is_file())

    def test_user_no_vad_with_unusable_clock_does_not_loop(self):
        self.args.vad = False
        def provider(command, cwd, log):
            return self.emit(command, cwd, log, conflict=True)
        with patch.object(adapter, "stream_process", provider):
            adapter.recognize(self.args)
        self.assertEqual(len(self.commands), 1)

    def test_interrupt_retains_original_attempt_and_preexisting_output(self):
        self.output.parent.mkdir(parents=True)
        self.output.write_bytes(b"known good output")
        def provider(command, cwd, log):
            if "--vad" not in command:
                raise KeyboardInterrupt()
            return self.emit(command, cwd, log, conflict=True)
        with patch.object(adapter, "stream_process", provider), self.assertRaises(KeyboardInterrupt):
            adapter.recognize(self.args)
        self.assertEqual(self.output.read_bytes(), b"known good output")
        self.assertEqual(json.loads(self.recovery().read_text())["state"], "INTERRUPTED")
        self.assertTrue((self.recovery().parent / "vad/jp.whisper.json").is_file())


if __name__ == "__main__":
    unittest.main()
