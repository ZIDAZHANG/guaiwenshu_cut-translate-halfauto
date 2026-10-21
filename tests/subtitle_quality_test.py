#!/usr/bin/env python3
"""Behavior regressions for subtitle alignment, term repair and resume."""

import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
import argparse
import json

from artifact_validation import validate_file, validate_review_csv, validate_json
from sakura_client import start_server
from roast_pipeline import execute_stage
from subtitle_axis_align import Cue, align, write_report
from task_state import TaskState
from voice_dictionary import ensure_required_terms
import voicetransl_adapter


class SubtitleQualityTests(unittest.TestCase):
    def test_configured_vad_is_used_and_missing_model_is_not_silenced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable, model, vad, audio = [root / name for name in ("whisper.exe", "large.bin", "custom-vad.bin", "audio.wav")]
            for path in (executable, model, vad, audio):
                path.write_bytes(b"fixture")
            config = root / "config.json"
            config.write_text(json.dumps({"recognition": {"executable": str(executable), "model": str(model), "vad": True, "vad_model": str(vad)}}), encoding="utf-8")
            args = argparse.Namespace(config=config, executable=None, model=None, language=None, vad=None, audio=audio, output=root / "jp.srt")
            commands = []
            def process(command, *_):
                commands.append(command)
                Path(command[command.index("-of") + 1] + ".srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n日本語です。\n", encoding="utf-8")
                return 0
            with patch.object(voicetransl_adapter, "stream_process", side_effect=process):
                self.assertEqual(voicetransl_adapter.recognize(args), 0)
                self.assertEqual(commands[0][commands[0].index("--vad-model") + 1], str(vad))
                vad.unlink()
                with self.assertRaises(SystemExit):
                    voicetransl_adapter.recognize(args)
                self.assertEqual(len(commands), 1)

    def test_existing_server_model_is_checked_without_stopping_it(self):
        with patch("sakura_client.endpoint_ready", return_value=True), patch("sakura_client.http_json", return_value={"data": [{"id": "other.gguf"}]}), patch("sakura_client.subprocess.Popen") as launch:
            with self.assertRaises(SystemExit):
                start_server({"model": "Sakura.gguf"}, "http://127.0.0.1:8989", Path("unused.log"))
            launch.assert_not_called()
        with patch("sakura_client.endpoint_ready", return_value=True), patch("sakura_client.http_json", return_value={"data": [{"id": "G:\\models\\Sakura.gguf"}]}):
            self.assertIsNone(start_server({"model": "D:/models/Sakura.gguf"}, "http://127.0.0.1:8989", Path("unused.log")))

    def test_failed_ui_json_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            path.write_text('{"state":"failed","steps":[]}', encoding="utf-8")
            self.assertFalse(validate_json(path, expected_values={"state": "completed"}).valid)

    def test_touching_cues_do_not_merge(self):
        cues = [Cue(i * 2000, (i + 1) * 2000, f"中文{i}", i + 1) for i in range(5)]
        for tolerance in (0, 1200):
            with self.subTest(tolerance=tolerance):
                texts, rows = align(cues, cues, [], tolerance, .18, 3, "none")
                self.assertEqual(texts, [cue.text for cue in cues])
                self.assertTrue(all(row["confidence"] == "high" for row in rows))

    def test_max_join_keeps_actual_best(self):
        target = [Cue(2000, 3000, "", 1)]
        sources = [Cue(0, 1900, "前一句", 1), Cue(2000, 3000, "当前句", 2)]
        self.assertEqual(align(target, sources, [], 1200, .18, 1, "none")[0], ["当前句"])

    def test_contained_segments_keep_order_and_real_repetition(self):
        target = [Cue(0, 4000, "", 1)]
        sources = [Cue(0, 2000, "真的？", 1), Cue(2000, 4000, "真的？", 2)]
        texts, rows = align(target, sources, [], 1200, .18, 3, "none")
        self.assertEqual(texts, ["真的？\n真的？"])
        self.assertEqual(rows[0]["zh_indices"], "1,2")

    def test_crossing_source_is_never_silently_high(self):
        targets = [Cue(0, 2000, "", 1), Cue(2000, 4000, "", 2)]
        texts, rows = align(targets, [Cue(0, 4000, "跨两个画面的句子", 1)], [], 0, .18, 3, "none")
        self.assertTrue(all(texts))
        self.assertTrue(all("CHECK" in row["flags"] for row in rows))
        self.assertTrue(all(row["confidence"] != "high" for row in rows))

    def test_nearby_fallback_is_explicitly_uncertain(self):
        _, rows = align([Cue(2100, 3000, "", 1)], [Cue(0, 2000, "附近的句子", 1)], [], 1200, .18, 3, "none")
        self.assertIn("CHECK", rows[0]["flags"])
        self.assertIn("NEARBY_ZH", rows[0]["flags"])

    def test_missing_term_does_not_invent_sentence_tail(self):
        self.assertEqual(ensure_required_terms("ファインが勝った。", "美妙赢了。", {"ファイン": "Fine"}), "美妙赢了。")
        self.assertEqual(ensure_required_terms("ファインが勝った。", "ファイン赢了。", {"ファイン": "Fine"}), "Fine赢了。")
        self.assertEqual(ensure_required_terms("アルトレのトレーナー", "アルトレ来了。", {"アルトレ": {"default": "阿尔特雷", "trainer_context": "阿尔丹训练员"}}), "阿尔丹训练员来了。")

    def test_resume_rechecks_same_path_content_and_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "jp.srt", root / "zh.srt"
            source.write_text("昨天", encoding="utf-8")
            task = TaskState.open_or_create(root / "task.json", source={}, job_dir=root, display_name="resume")
            calls = []
            settings = {"prompt": "直译"}

            def action():
                calls.append(1)
                output.write_text(source.read_text(encoding="utf-8") + settings["prompt"], encoding="utf-8")

            def run():
                execute_stage(task, "TRANSLATION", component="test", action=action,
                              validate=lambda: {"zh": validate_file(output, "zh")},
                              inputs=lambda: {"jp": str(source), "settings": settings},
                              outputs=lambda: {"zh": str(output)})
            run()
            run()
            self.assertEqual(len(calls), 1)
            source.write_text("明天", encoding="utf-8")
            run()
            self.assertEqual(len(calls), 2)
            settings["prompt"] = "保留顺序"
            run()
            self.assertEqual(len(calls), 3)
            self.assertEqual(output.read_text(encoding="utf-8"), "明天保留顺序")

    def test_review_reports_quality_separately_from_validity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review.csv"
            _, rows = align([Cue(0, 2000, "", 1)], [Cue(0, 2000, "中文", 1)], [], 1200, .18, 3, "none")
            write_report(path, rows)
            result = validate_review_csv(path, require_provenance=True)
            self.assertTrue(result.valid, result.errors)
            self.assertEqual(result.details["needs_review"], 0)


if __name__ == "__main__":
    unittest.main()
