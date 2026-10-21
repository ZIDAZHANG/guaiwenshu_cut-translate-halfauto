#!/usr/bin/env python3
"""Run a local end-to-end smoke test for the roast pipeline."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from roast_pipeline import download_auth_args
from subtitle_axis_align import Cue, match_score
from voicetransl_adapter import ensure_required_terms, parse_translations, protect_terms


ROOT = (Path(__file__).resolve().parents[1] / "app")
JOB = ROOT / "work" / "smoke_test"
JAPANESE_JOB = ROOT / "work" / "smoke_japanese_only"
INPUTS = ROOT / "output" / "smoke_inputs"
TEST_CONFIG = INPUTS / "pipeline.test.json"


def fixture_config() -> dict:
    config = json.loads((ROOT / "config/roast_pipeline.example.json").read_text(encoding="utf-8"))
    config["tools"].update(python=sys.executable, ffmpeg=os.environ.get("FFMPEG_BINARY", "ffmpeg"))
    return config

TIMELINE_ASS = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,30,30,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,
Dialogue: 0,0:00:04.00,0:00:06.00,Default,,0,0,0,,
"""

JP_SRT = """1
00:00:01,000 --> 00:00:03,000
ファイン、行きます！

2
00:00:04,000 --> 00:00:06,000
たわけ、まだ終わっていませんわ。
"""

ZH_SRT = """1
00:00:01,000 --> 00:00:03,000
Fine，要上了！

2
00:00:04,000 --> 00:00:06,000
蠢货，还没有结束呢。
"""


def test_adapter_helpers() -> None:
    glossary = {
        "ファイン": "Fine",
        "ファイトレ": "Fineトレ",
        "アルトレ": {"default": "阿尔特雷", "trainer_context": "阿尔丹训练员"},
        "たわけ": "蠢货",
    }
    protected = protect_terms("ファインとたわけ、アルトレのトレーナー", glossary)
    if protected != "Fineと蠢货、阿尔丹训练员のトレーナー":
        raise AssertionError(f"unexpected glossary protection: {protected}")
    response = json.dumps({"translations": [{"id": 1, "text": "测试"}]}, ensure_ascii=False)
    if parse_translations(response, [1]) != {1: "测试"}:
        raise AssertionError("Sakura response parser failed")
    restored = ensure_required_terms(
        "距離を縮めようとする目白アルダン。",
        "试图拉近距离。",
        {"目白アルダン": {"default": "目白阿尔丹", "possessive": "true"}},
    )
    if restored != "试图拉近距离。":
        raise AssertionError(f"required term restoration failed: {restored}")
    flat_restored = ensure_required_terms(
        "距離を縮めようとする目白アルダン。",
        "试图拉近距离。",
        {"目白アルダン": "目白阿尔丹"},
    )
    if flat_restored != "试图拉近距离。":
        raise AssertionError(f"legacy glossary invented sentence content: {flat_restored}")

    target = Cue(start=23_160, end=24_500, text="")
    nearby = Cue(start=21_330, end=22_300, text="这也太狠了吧？")
    if match_score(target, nearby, 1200) <= 0:
        raise AssertionError("nearby non-overlapping subtitle was not matched by edge gap")

    cookies, js_runtime, extractor = download_auth_args({
        "download_settings": {
            "cookies_from_browser": "Edge",
            "js_runtime": r"C:\Program Files\Adobe\node.exe",
            "youtube_client": "Android VR",
        }
    })
    if "edge" not in cookies or "Adobe" not in js_runtime or "android_vr" not in extractor:
        raise AssertionError("YouTube compatibility arguments were not generated")


def make_test_video(video: Path) -> None:
    config = fixture_config()
    ffmpeg = config.get("tools", {}).get("ffmpeg", "ffmpeg")
    command = [
        ffmpeg,
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=1280x720:rate=30:duration=8",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=8",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(video),
    ]
    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        raise RuntimeError(f"ffmpeg could not create the smoke-test video:\n{completed.stdout}")


def write_inputs() -> tuple[Path, Path, Path, Path]:
    INPUTS.mkdir(parents=True, exist_ok=True)
    TEST_CONFIG.write_text(json.dumps(fixture_config(), ensure_ascii=False), encoding="utf-8")
    video = INPUTS / "video.mp4"
    timeline = INPUTS / "timeline.ass"
    jp = INPUTS / "jp_ocr.srt"
    zh = INPUTS / "zh_machine.srt"
    make_test_video(video)
    timeline.write_text(TIMELINE_ASS, encoding="utf-8")
    jp.write_text(JP_SRT, encoding="utf-8")
    zh.write_text(ZH_SRT, encoding="utf-8")
    return video, timeline, jp, zh


def main() -> int:
    test_adapter_helpers()
    if JOB.exists():
        shutil.rmtree(JOB)
    if JAPANESE_JOB.exists():
        shutil.rmtree(JAPANESE_JOB)
    video, timeline, jp, zh = write_inputs()
    cmd = [
        sys.executable,
        str(ROOT / "tools" / "roast_pipeline.py"),
        "local-file",
        "--config",
        str(TEST_CONFIG),
        "--job-name",
        "smoke_test",
        "--work-dir",
        str(ROOT / "work"),
        "--video-file",
        str(video),
        "--timeline-file",
        str(timeline),
        "--jp-file",
        str(jp),
        "--zh-file",
        str(zh),
        "--skip-audio",
        "--skip-external",
        "--wait-missing",
        "--copy-media",
        "--no-jianying-ui",
    ]
    print("Running:", subprocess.list2cmdline(cmd))
    completed = subprocess.run(cmd, cwd=str(ROOT))
    if completed.returncode != 0:
        return completed.returncode

    required = [
        JOB / "deliverables" / "final.ass",
        JOB / "deliverables" / "review.csv",
        JOB / "jianying_import" / "manifest.json",
        JOB / "job_manifest.json",
        JOB / "README_external_tools.txt",
        JOB / "source" / "input_video.mp4",
        JOB / "subs" / "timeline.ass",
        JOB / "subs" / "jp_ocr.srt",
        JOB / "subs" / "zh_machine.srt",
    ]
    missing = [p for p in required if not p.exists() or p.stat().st_size == 0]
    if missing:
        print("Missing smoke-test outputs:")
        for path in missing:
            print(" -", path)
        return 1

    if (JOB / "source" / "input_video.mp4").stat().st_size < 10_000:
        print("smoke-test video is too small to be a valid media file")
        return 1

    final_text = (JOB / "deliverables" / "final.ass").read_text(encoding="utf-8-sig")
    if "Fine" not in final_text or "蠢货" not in final_text:
        print("final.ass does not contain expected translated text")
        return 1

    manifest_text = (JOB / "job_manifest.json").read_text(encoding="utf-8")
    if "final.ass" not in manifest_text or "jianying_import" not in manifest_text:
        print("job_manifest.json does not contain expected output paths")
        return 1

    for validation_name, artifact_name in (("jp_validation.json", "jp_ocr.srt"), ("zh_validation.json", "zh_machine.srt")):
        validation = json.loads((JOB / "subs" / validation_name).read_text(encoding="utf-8"))
        published_path = Path(validation["path"])
        if published_path != (JOB / "subs" / artifact_name).resolve() or not published_path.is_file():
            print(f"{validation_name} does not reference the published subtitle: {published_path}")
            return 1

    final_validation = json.loads(
        (JOB / "deliverables" / "final_validation.json").read_text(encoding="utf-8")
    )
    for artifact_name in ("final", "review"):
        published_path = Path(final_validation[artifact_name]["path"])
        if not published_path.is_file() or ".tmp" in published_path.name:
            print(
                f"final_validation.json does not reference the published {artifact_name} artifact: "
                f"{published_path}"
            )
            return 1

    index_text = (ROOT / "work" / "jobs_index.json").read_text(encoding="utf-8")
    if "smoke_test" not in index_text or "job_manifest.json" not in index_text:
        print("jobs_index.json does not contain the smoke_test job")
        return 1

    legacy_task = json.loads((JOB / "task.json").read_text(encoding="utf-8"))
    legacy_task.get("source", {}).pop("output_mode", None)
    (JOB / "task.json").write_text(
        json.dumps(legacy_task, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    resumed = subprocess.run(
        cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if resumed.returncode != 0:
        print(resumed.stdout)
        return resumed.returncode
    expected_resumes = [
        "[resume] INPUT: validated DONE checkpoint, skipping",
        "[resume] TIMELINE: validated DONE checkpoint, skipping",
        "[resume] ASR: validated DONE checkpoint, skipping",
        "[resume] SEGMENTATION: validated DONE checkpoint, skipping",
        "[resume] TRANSLATION: validated DONE checkpoint, skipping",
        "[resume] FINAL_OUTPUT: validated DONE checkpoint, skipping",
        "[resume] NLE_EXPORT: validated DONE checkpoint, skipping",
    ]
    missing_resume_lines = [line for line in expected_resumes if line not in resumed.stdout]
    if missing_resume_lines:
        print("Checkpoint resume did not skip every validated stage:")
        print(resumed.stdout)
        return 1

    (JOB / "subs" / "zh_machine.srt").write_text(
        "1\n00:00:01,000 --> 00:00:03,000\ninvalid latin-only artifact\n",
        encoding="utf-8",
    )
    repaired = subprocess.run(
        cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if repaired.returncode != 0:
        print(repaired.stdout)
        return repaired.returncode
    if "[resume] TRANSLATION: saved output is invalid, rerunning" not in repaired.stdout:
        print("Corrupt translation did not invalidate its stage:")
        print(repaired.stdout)
        return 1
    for upstream in ("INPUT", "TIMELINE", "ASR"):
        if f"[resume] {upstream}: validated DONE checkpoint, skipping" not in repaired.stdout:
            print(f"Repair unnecessarily reran upstream stage {upstream}:")
            print(repaired.stdout)
            return 1
    task_state = json.loads((JOB / "task.json").read_text(encoding="utf-8"))
    if task_state["status"] != "DONE" or any(
        stage["status"] != "DONE" for stage in task_state["stages"].values()
    ):
        print("Task did not return to DONE after selective repair")
        return 1

    japanese_config = fixture_config()
    japanese_config["axis_settings"]["enabled"] = False
    japanese_config["recognition"]["enabled"] = False
    japanese_config["translation"]["enabled"] = False
    japanese_config["translation"]["engine"] = "不翻译，仅生成日文"
    japanese_config_path = INPUTS / "japanese_only_config.json"
    japanese_config_path.write_text(
        json.dumps(japanese_config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    japanese_cmd = [
        sys.executable,
        str(ROOT / "tools" / "roast_pipeline.py"),
        "local-file",
        "--config",
        str(japanese_config_path),
        "--job-name",
        "smoke_japanese_only",
        "--work-dir",
        str(ROOT / "work"),
        "--video-file",
        str(video),
        "--jp-file",
        str(jp),
        "--skip-audio",
        "--skip-external",
        "--copy-media",
        "--skip-jianying-package",
        "--no-jianying-ui",
    ]
    japanese_run = subprocess.run(
        japanese_cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if japanese_run.returncode != 0:
        print(japanese_run.stdout)
        return japanese_run.returncode
    japanese_output = JAPANESE_JOB / "deliverables" / "japanese.srt"
    if not japanese_output.is_file() or "ファイン" not in japanese_output.read_text(encoding="utf-8-sig"):
        print("Japanese-only mode did not publish japanese.srt")
        return 1
    japanese_state = json.loads((JAPANESE_JOB / "task.json").read_text(encoding="utf-8"))
    expected_stage_statuses = {
        "INPUT": "DONE",
        "TIMELINE": "SKIPPED",
        "ASR": "DONE",
        "SEGMENTATION": "DONE",
        "TRANSLATION": "SKIPPED",
        "FINAL_OUTPUT": "DONE",
        "NLE_EXPORT": "SKIPPED",
    }
    actual_stage_statuses = {
        name: stage["status"] for name, stage in japanese_state["stages"].items()
    }
    if japanese_state["status"] != "DONE" or actual_stage_statuses != expected_stage_statuses:
        print("Japanese-only stage status is incorrect:", actual_stage_statuses)
        return 1
    if (JAPANESE_JOB / "subs" / "timeline.ass").exists() or (JAPANESE_JOB / "subs" / "zh_machine.srt").exists():
        print("Japanese-only mode generated unnecessary timeline or translation files")
        return 1

    invalid_config = json.loads(json.dumps(japanese_config))
    invalid_config["translation"]["enabled"] = True
    invalid_config["translation"]["engine"] = "Sakura 本地模型"
    invalid_config_path = INPUTS / "missing_axis_config.json"
    invalid_config_path.write_text(
        json.dumps(invalid_config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    invalid_cmd = japanese_cmd.copy()
    invalid_cmd[invalid_cmd.index(str(japanese_config_path))] = str(invalid_config_path)
    invalid_cmd.extend(["--dry-run"])
    invalid_run = subprocess.run(
        invalid_cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if invalid_run.returncode != 0:
        print("Audio timing mode must work when the visual axis is disabled:")
        print(invalid_run.stdout)
        return 1

    print("Smoke test passed")
    print("Job:", JOB)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
