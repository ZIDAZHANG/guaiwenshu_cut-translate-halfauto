"""T03/T06 (W03, queue level): INPUT, MEDIA_PREP, OCR and ASR running inside the persistent queue.

Media stages use a real FFmpeg/ffprobe on S05 material; the two recognition engines are replaced by doubles at the
process-launch seam. Checked here: the per-mode skip rules, the audio pre-check, resource pre-checks before any
engine starts, cache reuse limited to real dependencies, resume after a resource was fixed, a real HTTP download
through yt-dlp, and the command-line stream pre-check.
"""
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
import functools
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

from support import w03
from upgrade import contracts as c
from upgrade import recognition
from upgrade import runner as r
from upgrade.config import APP_CONFIG_NAME, default_app_config, save_app_config
from upgrade.resource_catalog import inspect_resources, resolve_resource
from upgrade.stage_registry import default_registry
from upgrade.storage import init_data_root, safe_join, verify_committed


@unittest.skipIf(w03.media_tools_problem(), f"real FFmpeg/ffprobe not available: {w03.media_tools_problem()}")
class StageTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._fixtures = tempfile.TemporaryDirectory()
        cls.fixtures = Path(cls._fixtures.name) / "素材 (stages)"
        cls.fixtures.mkdir()
        cls.video = w03.make_cfr_mp4(cls.fixtures / "有声 片源.mp4")
        cls.silent = w03.make_cfr_mp4(cls.fixtures / "无声 片源.mp4", audio=False)
        cls.two_audio = w03.make_two_audio_mkv(cls.fixtures / "双音轨.mkv")
        cls.served = Path(cls._fixtures.name) / "served"
        cls.served.mkdir()
        (cls.served / "clip.mp4").write_bytes(cls.video.read_bytes())

    @classmethod
    def tearDownClass(cls):
        cls._fixtures.cleanup()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "数据根 (stages)"
        init_data_root(self.root)
        self.config = default_app_config(self.root, ascii_temp_root=str(self.tmp / "ascii"))
        save_app_config(self.root / APP_CONFIG_NAME, self.config)
        self.engine_settings: dict = {}
        self.engine_paths = w03.make_engine_resources(self.tmp / "engines")
        w03.register_resources(self.config, {"ffmpeg": Path(w03.FFMPEG), "ffprobe": Path(w03.FFPROBE)})
        self.double = w03.EngineDouble()

    def tearDown(self):
        self._tmp.cleanup()

    def register_engines(self, *names):
        w03.register_resources(self.config, {name: self.engine_paths[name] for name in (names or self.engine_paths)})

    def resource_refs(self):
        report = inspect_resources(self.config, probe=False)
        return [resolve_resource(resource_id, report)["resource_ref"] for resource_id in sorted(report["manifests"])]

    def submit(self, mode, video=None, *, settings=None, input_kind="local_video", **kw):
        request = r.build_job_request(self.config, self.engine_settings if settings is None else settings, input_kind=input_kind,
                                      input_value=str(video or self.video), recognition_mode=mode, resource_refs=self.resource_refs(), **kw)
        return r.submit(self.root, request)

    def run_queue(self):
        with patch.object(recognition, "run_engine", self.double), patch.object(recognition, "prepare_utf8_executable", w03.passthrough_utf8):
            return r.QueueRunner(self.root, self.config, self.engine_settings, default_registry()).run_queue()

    def task(self, envelope):
        envelope = r.load_envelope(self.root, envelope["request"]["request_id"])
        project = r.project_dir(self.root, envelope["request"]["project_id"])
        return envelope, project, r.load_task(r.run_dir(project, envelope["run_id"]))

    def stage(self, task, name):
        return next(stage for stage in task["stages"] if stage["name"] == name)

    def stage_dir(self, project, task, name):
        return safe_join(project, self.stage(task, name)["output_manifest"]["path"]).parent

    def statuses(self, task):
        return {stage["name"]: (stage["status"], stage["skip_reason"]) for stage in task["stages"]}

    def events(self, project, task):
        return (r.run_dir(project, task["run_id"]) / "events.jsonl").read_text(encoding="utf-8")


class ModeTests(StageTestCase):
    def test_combined_runs_media_then_ocr_then_asr_and_stops_honestly_at_source(self):
        self.register_engines()
        queued = self.submit("combined")
        self.run_queue()
        envelope, project, task = self.task(queued)
        statuses = self.statuses(task)
        for name in ("INPUT", "MEDIA_PREP", "OCR", "ASR"):
            self.assertEqual(statuses[name], ("DONE", None), name)
            directory = self.stage_dir(project, task, name)
            manifest = c.load_record("StageManifest", directory / "manifest.json")
            verify_committed(directory, manifest["outputs"])
        self.assertEqual(statuses["SOURCE"][0], "FAILED")                         # W04 is not built yet: no placeholder result
        self.assertEqual((envelope["status"], envelope["last_error"]["code"]), ("FAILED", "STAGE_UNAVAILABLE"))
        record = c.load_record("MediaRecord", self.stage_dir(project, task, "MEDIA_PREP") / "media_record.json")
        visual = c.load_record("VisualEvidence", self.stage_dir(project, task, "OCR") / "visual_evidence.json")
        speech = c.load_record("SpeechEvidence", self.stage_dir(project, task, "ASR") / "speech_evidence.json")
        self.assertEqual({visual["media_id"], speech["media_id"]}, {record["media_id"]})
        verify_committed(self.stage_dir(project, task, "OCR"), visual["raw_refs"])   # raw and normalized evidence are both in the committed attempt
        verify_committed(self.stage_dir(project, task, "ASR"), speech["raw_refs"])
        self.assertEqual([call["executable"] for call in self.double.calls], ["MagiaTimeline.exe", "whisper-cli.exe"])
        self.assertEqual(r.load_project(self.root, task["project_id"])["media_ids"], [record["media_id"]])
        # the engines received the prepared media, staged on ASCII paths
        self.assertEqual(speech["audio_artifact"]["path"].split("/")[:2], ["media", record["media_id"]])
        self.assertEqual(visual["extensions"]["video_sha256"], record["source_sha256"])  # common MP4: the original itself was recognized

    def test_each_mode_skips_exactly_what_it_does_not_need(self):
        self.register_engines()
        cases = [
            ("speech_only", self.video, {"OCR": ("SKIPPED", "MODE"), "ASR": ("DONE", None)}, ["whisper-cli.exe"], True),
            ("visual_only", self.silent, {"OCR": ("DONE", None), "ASR": ("SKIPPED", "MODE")}, ["MagiaTimeline.exe"], False),
            ("supplied_source", self.video, {"OCR": ("SKIPPED", "USER_SOURCE"), "ASR": ("SKIPPED", "USER_SOURCE")}, [], False),
        ]
        for mode, video, expected, launched, has_wav in cases:
            with self.subTest(mode):
                self.double.calls.clear()
                extra = {"source_revision_id": c.new_id()} if mode == "supplied_source" else {}
                queued = self.submit(mode, video, **extra)
                self.run_queue()
                envelope, project, task = self.task(queued)
                statuses = self.statuses(task)
                self.assertEqual((statuses["INPUT"], statuses["MEDIA_PREP"]), (("DONE", None), ("DONE", None)))
                for name, value in expected.items():
                    self.assertEqual(statuses[name], value, name)
                self.assertEqual([call["executable"] for call in self.double.calls], launched)
                self.assertEqual(statuses["SOURCE"][0], "FAILED")
                self.assertEqual(envelope["last_error"]["code"], "STAGE_UNAVAILABLE")
                prepared = json.loads((self.stage_dir(project, task, "MEDIA_PREP") / "prepare_result.json").read_text(encoding="utf-8"))
                self.assertEqual(prepared["audio_for_asr"] is not None, has_wav)
                self.assertEqual(bool(list((project / "media").rglob("*.wav"))), has_wav)   # no audio is extracted for modes that do not listen

    def test_speech_modes_on_silent_media_ask_for_visual_only_instead_of_switching(self):
        self.register_engines()
        for mode in ("combined", "speech_only"):
            with self.subTest(mode):
                queued = self.submit(mode, self.silent)
                self.run_queue()
                envelope, project, task = self.task(queued)
                self.assertEqual((envelope["status"], envelope["last_error"]["code"], envelope["last_error"]["details"]["kind"]),
                                 ("FAILED", "NEEDS_SELECTION", "no_audio_stream"))
                self.assertEqual(envelope["request"]["recognition_mode"], mode)   # the request is not rewritten
                self.assertEqual(self.statuses(task)["OCR"][0], "PENDING")
        self.assertEqual(self.double.calls, [])

    def test_unverifiable_clock_stops_at_media_prep(self):
        self.register_engines()
        glued = w03.make_discontinuous_ts(self.tmp / "glued.ts")
        queued = self.submit("visual_only", glued)
        self.run_queue()
        envelope, project, task = self.task(queued)
        statuses = self.statuses(task)
        self.assertEqual((statuses["INPUT"][0], statuses["MEDIA_PREP"][0], envelope["last_error"]["code"]), ("DONE", "FAILED", "MEDIA_CLOCK_DISCONTINUITY"))
        self.assertEqual(self.double.calls, [])


class ResourceAndCacheTests(StageTestCase):
    def test_missing_engines_are_named_before_anything_long_starts_and_resume_continues(self):
        queued = self.submit("combined")                                          # only ffmpeg/ffprobe are registered
        self.run_queue()
        envelope, project, task = self.task(queued)
        error = envelope["last_error"]
        self.assertEqual((envelope["status"], error["code"], self.statuses(task)["INPUT"][0]), ("FAILED", "RESOURCE_MISSING", "FAILED"))
        self.assertEqual(sorted(item["role"] for item in error["details"]["failures"]), ["asr_engine", "asr_model", "ocr_engine", "vad_model"])
        self.assertIn("whisper-cli", error["message"])
        self.assertEqual(self.double.calls, [])
        self.assertFalse((project / "media").exists() and any((project / "media").iterdir()))   # nothing was probed, copied or derived
        # fix the resources, resume the same request: a new attempt, same run, no new request
        self.register_engines()
        r.resume(self.root, envelope["run_id"])
        self.run_queue()
        resumed, _, task = self.task(queued)
        self.assertEqual(resumed["run_id"], envelope["run_id"])
        self.assertEqual([attempt["status"] for attempt in self.stage(task, "INPUT")["attempts"]], ["FAILED", "DONE"])
        self.assertEqual(self.statuses(task)["ASR"], ("DONE", None))

    def test_changed_resource_content_is_refused(self):
        self.register_engines()
        queued = self.submit("speech_only")
        self.engine_paths["whisper-model"].write_bytes(b"a different model with the same file name")
        self.run_queue()
        envelope, _, _ = self.task(queued)
        self.assertEqual(envelope["last_error"]["code"], "RESOURCE_CHANGED")
        self.assertEqual([item["role"] for item in envelope["last_error"]["details"]["failures"]], ["asr_model"])
        self.assertEqual(self.double.calls, [])

    def test_only_real_dependencies_invalidate_recognition(self):
        self.register_engines()
        first = self.submit("combined")
        self.run_queue()
        _, project, first_task = self.task(first)
        self.assertEqual(len(self.double.calls), 2)
        # another timing mode over the same evidence: no engine runs again (plan 10.3)
        second = self.submit("combined", timing_mode="speech", project_id=first["request"]["project_id"])
        self.run_queue()
        _, _, second_task = self.task(second)
        self.assertEqual(len(self.double.calls), 2)
        self.assertEqual(self.events(project, second_task).count("stage_cache_hit"), 4)
        for name in ("OCR", "ASR"):
            self.assertEqual(self.stage(second_task, name)["output_manifest"], self.stage(first_task, name)["output_manifest"])
        # a new OCR region: on-screen text is recognized again, the speech evidence is kept
        third = self.submit("combined", project_id=first["request"]["project_id"],
                            settings={"ocr": {"roi": {"left": 0.0, "top": 0.5, "right": 1.0, "bottom": 1.0}}})
        self.run_queue()
        _, _, third_task = self.task(third)
        self.assertEqual([call["executable"] for call in self.double.calls[2:]], ["MagiaTimeline.exe"])
        self.assertEqual(self.stage(third_task, "ASR")["output_manifest"], self.stage(first_task, "ASR")["output_manifest"])
        self.assertNotEqual(self.stage(third_task, "OCR")["output_manifest"], self.stage(first_task, "OCR")["output_manifest"])
        visual = c.load_record("VisualEvidence", self.stage_dir(project, third_task, "OCR") / "visual_evidence.json")
        self.assertEqual(visual["roi"], {"left": 0.0, "top": 0.5, "right": 1.0, "bottom": 1.0})
        # a queued request keeps the settings it was submitted with, whatever the global settings say later
        self.assertEqual(third["request"]["config_snapshot"]["engine_settings"]["ocr"]["roi"]["top"], 0.5)
        # speech-only over the same audio reuses the ASR evidence as well
        fourth = self.submit("speech_only", project_id=first["request"]["project_id"])
        self.run_queue()
        _, _, fourth_task = self.task(fourth)
        self.assertEqual(len(self.double.calls), 3)
        self.assertEqual(self.stage(fourth_task, "ASR")["output_manifest"], self.stage(first_task, "ASR")["output_manifest"])

    def test_explicit_audio_stream_selects_what_is_recognized(self):
        self.register_engines()
        unselected = self.submit("speech_only", self.two_audio)
        chosen = self.submit("speech_only", self.two_audio, audio_stream_index=2)
        self.run_queue()
        envelope, _, _ = self.task(unselected)
        self.assertEqual((envelope["last_error"]["code"], envelope["last_error"]["details"]["kind"]), ("NEEDS_SELECTION", "audio_stream"))
        _, project, task = self.task(chosen)
        record = c.load_record("MediaRecord", self.stage_dir(project, task, "MEDIA_PREP") / "media_record.json")
        self.assertEqual((record["selected_audio_stream"], self.statuses(task)["ASR"]), (2, ("DONE", None)))
        self.assertEqual(len(self.double.launched("whisper")), 1)


class UrlInputTests(StageTestCase):
    def test_single_video_url_is_downloaded_by_yt_dlp_then_handled_like_a_local_file(self):
        try:
            import yt_dlp  # noqa: F401
        except ImportError:
            self.skipTest("yt-dlp is not installed in this environment")
        self.register_engines("whisper-cli", "whisper-model", "whisper-vad-model")
        handler = functools.partial(SimpleHTTPRequestHandler, directory=str(self.served))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/clip.mp4"
            queued = self.submit("speech_only", url, input_kind="url")
            self.run_queue()
        finally:
            server.shutdown()
            server.server_close()
        envelope, project, task = self.task(queued)
        self.assertEqual(self.statuses(task)["ASR"], ("DONE", None), envelope["last_error"])
        record = c.load_record("MediaRecord", self.stage_dir(project, task, "INPUT") / "media_record.json")
        source = Path(record["source_ref"]["absolute_path"])
        downloads = project / "media" / "downloads"
        self.assertEqual(source.parent.parent.parent, downloads)                  # the download lives in this project, committed once
        self.assertEqual((record["source_sha256"], record["extensions"]["origin"]["kind"], record["extensions"]["origin"]["url"]),
                         (c.sha256_file(self.video), "url", url))
        manifest = c.load_record("MediaManifest", source.parent.parent / "manifest.json")
        verify_committed(source.parent.parent, manifest["outputs"])
        names = {Path(ref["path"]).name for ref in manifest["outputs"]}
        self.assertIn("video.info.json", names)                                   # info JSON and the real downloaded file are both kept
        argv = json.loads((source.parent.parent / "logs" / "yt-dlp" / "process.json").read_text(encoding="utf-8"))["argv"]
        self.assertIn("--no-playlist", argv)
        self.assertEqual(argv[-1], url)


class CommandLineTests(StageTestCase):
    def cli(self, *args):
        completed = subprocess.run([sys.executable, "-X", "utf8", "-m", "upgrade.cli", *args], capture_output=True, text=True, encoding="utf-8", env=os.environ.copy())
        return completed.returncode, json.loads(completed.stdout) if completed.stdout.strip() else None, completed.stderr

    def test_submit_resolves_streams_or_asks_and_queue_run_uses_the_real_stages(self):
        config = str(self.root / APP_CONFIG_NAME)
        code, result, _ = self.cli("submit", "--config", config, "--video", str(self.two_audio), "--mode", "speech_only")
        self.assertEqual((code, result["ok"], result["error"]["code"], result["error"]["details"]["kind"]), (2, False, "NEEDS_SELECTION", "audio_stream"))
        code, result, _ = self.cli("submit", "--config", config, "--video", str(self.silent), "--mode", "combined")
        self.assertEqual((code, result["error"]["code"], result["error"]["details"]["kind"]), (2, "NEEDS_SELECTION", "no_audio_stream"))
        self.assertEqual(r.list_envelopes(self.root), [])                         # nothing ambiguous was queued
        code, result, stderr = self.cli("submit", "--config", config, "--video", str(self.two_audio), "--mode", "speech_only", "--audio-stream", "2")
        self.assertEqual((code, result["ok"], result["warnings"]), (0, True, []), stderr)
        request = r.load_envelope(self.root, result["ids"]["request_id"])["request"]
        self.assertEqual((request["video_stream_index"], request["audio_stream_index"]), (0, 2))
        code, result, _ = self.cli("submit", "--config", config, "--video", str(self.video), "--mode", "visual_only", "--audio-stream", "none")
        silent_request = r.load_envelope(self.root, result["ids"]["request_id"])["request"]
        self.assertEqual((code, silent_request["audio_stream_index"], silent_request["extensions"]["audio_stream"]), (0, None, "none"))
        # the queue command runs the registered W03 stages; without engines the first request stops with a named resource error
        code, result, _ = self.cli("queue", "run", "--data-root", str(self.root))
        self.assertEqual((code, result["ids"]["processed"], result["ids"]["failed"]), (0, 2, 2))
        errors = [envelope["last_error"] for envelope in r.list_envelopes(self.root)]
        self.assertEqual({error["code"] for error in errors}, {"RESOURCE_MISSING"})
        code, result, _ = self.cli("worker", "no-such-worker")
        self.assertEqual((code, result["ok"]), (2, False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
