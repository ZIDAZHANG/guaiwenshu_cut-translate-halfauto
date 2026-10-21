#!/usr/bin/env python3
"""Regression tests for VoiceTransl artifact safety and translation resume."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

import voicetransl_adapter as adapter
import sakura_client
import translation_checkpoint
import voice_common


ROOT = (Path(__file__).resolve().parents[1] / "app")


def srt_text(lines: list[str]) -> str:
    blocks = []
    for index, line in enumerate(lines, start=1):
        blocks.append(
            f"{index}\n00:00:{index - 1:02d},000 --> 00:00:{index:02d},000\n{line}"
        )
    return "\n\n".join(blocks) + "\n"


def main() -> int:
    test_root = ROOT / "output"
    test_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="voicetransl_adapter_", dir=test_root) as temp_dir:
        root = Path(temp_dir)
        whisper_dir = root / "voice" / "_internal" / "whisper"
        whisper_dir.mkdir(parents=True)
        executable = whisper_dir / "whisper-cli.exe"
        model = whisper_dir / "ggml-large-v3.bin"
        audio = root / "audio.wav"
        for path in (executable, model, audio):
            path.write_bytes(b"test")
        config = root / "config.json"
        config.write_text("{}", encoding="utf-8")
        jp_output = root / "jp.srt"
        pipeline_output = root / "中文任务" / "subs" / "jp.srt"
        expected_component_logs = root / "中文任务" / "logs" / "voicetransl"
        if adapter.component_log_dir(pipeline_output) != expected_component_logs.resolve():
            raise AssertionError("Pipeline VoiceTransl logs were not routed by task/component")
        if adapter.component_log_dir(jp_output) != root.resolve():
            raise AssertionError("Standalone VoiceTransl log behavior changed")

        original_stream = adapter.stream_process
        whisper_log_paths: list[Path] = []

        def valid_whisper(command: list[str], cwd: Path, log_path: Path) -> int:
            whisper_log_paths.append(log_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            output_base = Path(command[command.index("-of") + 1])
            Path(f"{output_base}.srt").write_text(
                srt_text(["ファインです", "今日は晴れです"]),
                encoding="utf-8",
            )
            log_path.write_text("whisper fake success\n", encoding="utf-8")
            return 0

        adapter.stream_process = valid_whisper
        recognize_args = argparse.Namespace(
            config=config,
            executable=executable,
            model=model,
            language="ja",
            vad=False,
            audio=audio,
            output=jp_output,
        )
        adapter.recognize(recognize_args)
        pipeline_args = argparse.Namespace(**vars(recognize_args))
        pipeline_args.output = pipeline_output
        adapter.recognize(pipeline_args)
        if whisper_log_paths[-1] != expected_component_logs / "whisper.log":
            raise AssertionError(f"Whisper log did not use the task/component directory: {whisper_log_paths[-1]}")
        success_validation = json.loads((root / "jp_validation.json").read_text(encoding="utf-8"))
        if success_validation["path"] != str(jp_output.resolve()) or not Path(success_validation["path"]).is_file():
            raise AssertionError(f"Successful Whisper validation did not reference the published SRT: {success_validation}")
        known_good = jp_output.read_bytes()

        def nonzero_valid_whisper(command: list[str], cwd: Path, log_path: Path) -> int:
            output_base = Path(command[command.index("-of") + 1])
            Path(f"{output_base}.srt").write_text(
                srt_text(["終了時だけ異常コードです", "字幕自体は有効です"]),
                encoding="utf-8",
            )
            log_path.write_text("whisper fake valid output with exit code 1\n", encoding="utf-8")
            return 1

        adapter.stream_process = nonzero_valid_whisper
        try:
            adapter.recognize(recognize_args)
        except SystemExit:
            pass
        else:
            raise AssertionError("A failed ASR process was treated as complete")
        nonzero_validation = json.loads((root / "jp_validation.json").read_text(encoding="utf-8"))
        if nonzero_validation["valid"] or not Path(nonzero_validation["path"]).is_file():
            raise AssertionError(f"Failed candidate evidence was not preserved: {nonzero_validation}")
        if nonzero_validation["details"].get("process_exit_code") != 1:
            raise AssertionError(f"Whisper exit code was not recorded: {nonzero_validation}")
        if jp_output.read_bytes() != known_good:
            raise AssertionError("A failed recognition replaced known-good subtitles")

        def invalid_whisper(command: list[str], cwd: Path, log_path: Path) -> int:
            output_base = Path(command[command.index("-of") + 1])
            Path(f"{output_base}.srt").write_text("1\ninvalid-time --> invalid-time\nhello\n", encoding="utf-8")
            log_path.write_text("whisper fake invalid success\n", encoding="utf-8")
            return 0

        adapter.stream_process = invalid_whisper
        try:
            adapter.recognize(recognize_args)
        except SystemExit as exc:
            if "invalid Japanese SRT" not in str(exc):
                raise AssertionError(exc)
        else:
            raise AssertionError("Invalid Whisper artifact was accepted")
        if jp_output.read_bytes() != known_good:
            raise AssertionError("Invalid Whisper run overwrote the known-good output")
        failed_validation = json.loads((root / "jp_validation.json").read_text(encoding="utf-8"))
        if not Path(failed_validation["path"]).is_file() or "whisper_failed_candidate_" not in Path(failed_validation["path"]).name:
            raise AssertionError(f"Invalid Whisper validation did not reference preserved evidence: {failed_validation}")
        adapter.stream_process = original_stream

        original_http_json = sakura_client.http_json
        captured_payloads: list[dict] = []

        def structured_response(url: str, payload: dict | None = None, timeout: int = 10) -> dict:
            assert payload is not None
            captured_payloads.append(payload)
            return {
                "choices": [{"message": {"content": '{"translations":[{"id":41,"text":"结构化译文"}]}'}}]
            }

        sakura_client.http_json = structured_response
        structured_result = sakura_client.translate_json_batch(
            "http://127.0.0.1:8989",
            "test-model",
            "直译。",
            [{"id": 41, "text": "テスト"}],
            retries=0,
        )
        if structured_result != {41: "结构化译文"}:
            raise AssertionError(f"Structured translation result was not parsed: {structured_result}")
        response_format = captured_payloads[0].get("response_format", {})
        schema = response_format.get("schema", {})
        item_schema = schema.get("properties", {}).get("translations", {}).get("items", {})
        if response_format.get("type") != "json_object" or item_schema.get("properties", {}).get("id", {}).get("enum") != [41]:
            raise AssertionError(f"Translation request was not schema-constrained: {response_format}")
        sakura_client.http_json = original_http_json

        jp_source = root / "source.srt"
        jp_source.write_text(srt_text(["一つ目です", "二つ目です", "三つ目です"]), encoding="utf-8")
        prompt = root / "prompt.txt"
        glossary = root / "glossary.json"
        prompt.write_text("原文结构を保って中国語へ直訳する。", encoding="utf-8")
        glossary.write_text(json.dumps({"三つ": "三号"}, ensure_ascii=False), encoding="utf-8")
        native_root = root / "voice_native"
        native_root.mkdir()
        pre_dictionary = native_root / "dict_pre.txt"
        gpt_dictionary = native_root / "dict_gpt.txt"
        post_dictionary = native_root / "dict_after.txt"
        guideline = native_root / "Basic.md"
        project_config = native_root / "config.yaml"
        pre_dictionary.write_text("一つ\t壱\n", encoding="utf-8")
        gpt_dictionary.write_text(
            "二つ -> 第二 # 数量词\n三つ -> 原生三 # 与自定义术语冲突\n",
            encoding="utf-8",
        )
        post_dictionary.write_text("第一句中文\t第一句后处理\n", encoding="utf-8")
        guideline.write_text("保留原句顺序和吐槽节奏。", encoding="utf-8")
        project_config.write_text(
            "common:\n  gpt.change_prompt: AdditionalPrompt\n  gpt.prompt_content: 不要擅自补全未完句。\n",
            encoding="utf-8",
        )
        syntax_dictionary = native_root / "dict_syntax.txt"
        syntax_dictionary.write_text(
            "// comment\n^^先頭\t开头\n1^同\t一\npre_jp\t条件\t検索\t置換\n",
            encoding="utf-8",
        )
        syntax_entries, syntax_unsupported = adapter.load_native_dictionary(syntax_dictionary)
        if adapter.apply_native_dictionary("先頭同同", syntax_entries) != "开头一同":
            raise AssertionError("VoiceTransl ^^ / 1^ replacement modes were not preserved")
        if syntax_unsupported != 1:
            raise AssertionError("Advanced conditional dictionary entries were not reported")
        translation_config = root / "translation.json"
        translation_config.write_text(
            json.dumps({
                "resources": {
                    "translation_prompt": str(prompt),
                    "translation_glossary": str(glossary),
                },
                "translation": {
                    "model": "Sakura-Galtransl-7B-v3.7.gguf",
                    "batch_size": 1,
                    "use_native_dictionaries": True,
                    "native_pre_dictionary": str(pre_dictionary),
                    "native_gpt_dictionary": str(gpt_dictionary),
                    "native_post_dictionary": str(post_dictionary),
                    "native_guideline": str(guideline),
                    "native_project_config": str(project_config),
                    "native_prompt_mode": "追加",
                },
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        zh_output = root / "zh.srt"
        translate_args = argparse.Namespace(
            config=translation_config,
            input=jp_source,
            output=zh_output,
            endpoint="http://127.0.0.1:8989",
            batch_size=1,
        )

        original_start_server = adapter.start_server
        original_stop_server = adapter.stop_server
        original_translate_batch = adapter.translate_batch
        adapter.start_server = lambda settings, endpoint, log_path: None
        adapter.stop_server = lambda process: None
        first_calls: list[int] = []
        first_batches: dict[int, dict] = {}
        first_prompts: dict[int, str] = {}

        def fail_second(endpoint: str, model_name: str, system_prompt: str, batch: list[dict], retries: int = 2, **kwargs) -> dict[int, str]:
            item_id = int(batch[0]["id"])
            first_calls.append(item_id)
            first_batches[item_id] = batch[0].copy()
            first_prompts[item_id] = system_prompt + "\n[Glossary]\n" + kwargs.get("glossary_text", "")
            if item_id == 2:
                raise RuntimeError("simulated interruption")
            return {item_id: "第一句中文"}

        adapter.translate_batch = fail_second
        try:
            adapter.translate(translate_args)
        except RuntimeError as exc:
            if "simulated interruption" not in str(exc):
                raise
        else:
            raise AssertionError("Simulated translation interruption was ignored")
        checkpoint = json.loads((root / "translation_checkpoint.json").read_text(encoding="utf-8"))
        if checkpoint["completed"] != 1 or checkpoint["status"] != "FAILED":
            raise AssertionError(f"Unexpected partial checkpoint: {checkpoint}")
        failed_progress = json.loads((root / "translation_progress.json").read_text(encoding="utf-8"))
        if failed_progress.get("error") != "simulated interruption":
            raise AssertionError(f"Translation progress did not preserve the real error: {failed_progress}")
        if first_batches[1]["text"] != "壱目です":
            raise AssertionError(f"Native pre-dictionary was not applied: {first_batches[1]}")
        if "保留原句顺序和吐槽节奏" not in first_prompts[1] or "不要擅自补全未完句" not in first_prompts[1]:
            raise AssertionError("Native guideline and project prompt were not appended")
        if "[Glossary]" not in first_prompts[2] or "二つ->第二 #数量词" not in first_prompts[2]:
            raise AssertionError("Matched native GPT dictionary entry was not added to the batch prompt")
        if "二つ->第二" in first_prompts[1]:
            raise AssertionError("Unmatched native GPT dictionary entry leaked into another batch")

        resumed_calls: list[int] = []
        resumed_prompts: dict[int, str] = {}

        def finish_remaining(endpoint: str, model_name: str, system_prompt: str, batch: list[dict], retries: int = 2, **kwargs) -> dict[int, str]:
            item_id = int(batch[0]["id"])
            resumed_calls.append(item_id)
            resumed_prompts[item_id] = system_prompt + "\n[Glossary]\n" + kwargs.get("glossary_text", "")
            return {item_id: {2: "第二句中文", 3: "第三句中文"}[item_id]}

        adapter.translate_batch = finish_remaining
        adapter.translate(translate_args)
        zh_validation = json.loads((root / "zh_validation.json").read_text(encoding="utf-8"))
        if zh_validation["path"] != str(zh_output.resolve()) or not Path(zh_validation["path"]).is_file():
            raise AssertionError(f"Successful translation validation did not reference the published SRT: {zh_validation}")
        if resumed_calls != [2, 3]:
            raise AssertionError(f"Translation did not resume from checkpoint: {resumed_calls}")
        if "三つ->三号" not in resumed_prompts[3]:
            raise AssertionError("Matched custom glossary entry was not added to the batch prompt")
        if "三つ->原生三" in resumed_prompts[3]:
            raise AssertionError("Native GPT dictionary overrode the custom glossary for the same source term")
        if "三つ->三号" in resumed_prompts[2]:
            raise AssertionError("Unmatched custom glossary entry leaked into another batch")
        final_checkpoint = json.loads((root / "translation_checkpoint.json").read_text(encoding="utf-8"))
        if final_checkpoint["status"] != "DONE" or final_checkpoint["completed"] != 3:
            raise AssertionError(f"Translation checkpoint was not completed: {final_checkpoint}")
        final_text = jp_source.parent.joinpath("zh.srt").read_text(encoding="utf-8-sig")
        if "第一句后处理" not in final_text:
            raise AssertionError("Resumed translation output is incomplete")
        if "第三句中文 三号" in final_text:
            raise AssertionError("A missing glossary term was invented at the sentence tail")
        term_review = json.loads((root / "translation_term_review.json").read_text(encoding="utf-8"))
        if not any(issue["line"] == 3 and "三号" in issue["missing_terms"] for issue in term_review["issues"]):
            raise AssertionError("Missing glossary term was not recorded for review")

        post_dictionary.write_text(
            "第一句中文\t第一句后处理\n第二句中文\t第二句后处理\n",
            encoding="utf-8",
        )
        changed_dictionary_calls: list[int] = []

        def translate_after_dictionary_change(
            endpoint: str,
            model_name: str,
            system_prompt: str,
            batch: list[dict],
            retries: int = 2,
            **kwargs,
        ) -> dict[int, str]:
            item_id = int(batch[0]["id"])
            changed_dictionary_calls.append(item_id)
            return {item_id: {1: "第一句中文", 2: "第二句中文", 3: "第三句中文"}[item_id]}

        adapter.translate_batch = translate_after_dictionary_change
        adapter.translate(translate_args)
        if changed_dictionary_calls != [1, 2, 3]:
            raise AssertionError(
                f"Changing a native dictionary did not invalidate the old checkpoint: {changed_dictionary_calls}"
            )
        changed_text = zh_output.read_text(encoding="utf-8-sig")
        if "第二句后处理" not in changed_text:
            raise AssertionError("Updated native post-dictionary was not applied")

        original_signature = adapter.native_file_signature
        def changed_client_signature(path):
            value = original_signature(path)
            if path.name == "sakura_client.py":
                value = {**value, "sha256": "changed-request-protocol"}
            return value
        adapter.native_file_signature = changed_client_signature
        changed_dictionary_calls.clear()
        try:
            adapter.translate(translate_args)
            if changed_dictionary_calls != [1, 2, 3]:
                raise AssertionError("Changing the request client reused old translations")
            changed_dictionary_calls.clear()
            adapter.translate(translate_args)
            if changed_dictionary_calls:
                raise AssertionError("Unchanged request client did not resume its checkpoint")
        finally:
            adapter.native_file_signature = original_signature

        sampling_calls = []
        def translate_with_sampling(endpoint, model_name, system_prompt, batch, retries=2, **kwargs):
            sampling_calls.append((int(batch[0]["id"]), kwargs["temperature"], kwargs["top_p"]))
            return {int(batch[0]["id"]): "完整中文"}
        adapter.translate_batch = translate_with_sampling
        adapter.translate(translate_args)
        sampling_calls.clear()
        adapter.translate(translate_args)
        if sampling_calls:
            raise AssertionError("Unchanged sampler did not resume saved translations")
        changed_config = json.loads(translation_config.read_text(encoding="utf-8"))
        changed_config["translation"].update(temperature=0.55, top_p=0.95)
        translation_config.write_text(json.dumps(changed_config, ensure_ascii=False), encoding="utf-8")
        adapter.translate(translate_args)
        if sampling_calls != [(1, 0.55, 0.95), (2, 0.55, 0.95), (3, 0.55, 0.95)]:
            raise AssertionError(f"Sampler change reused stale translations or was clamped: {sampling_calls}")
        sampling_calls.clear()
        adapter.translate(translate_args)
        if sampling_calls:
            raise AssertionError("Changed sampler did not resume its own checkpoint")

        adapter.start_server = original_start_server
        adapter.stop_server = original_stop_server
        adapter.translate_batch = original_translate_batch

    test_partial_retry()
    test_sakura_server_cleanup()
    test_translation_progress_eta()
    test_model_identity()
    print("VoiceTransl adapter tests passed")
    return 0


def test_partial_retry() -> None:
    """Sakura occasionally omits one id from a batch; translate_batch must
    retry only the missing items instead of failing the whole batch."""
    import sakura_client as sc

    calls: list[list[int]] = []

    def fake_http_json(url: str, payload: dict | None = None, timeout: int = 10) -> dict:
        user_content = payload["messages"][1]["content"]
        batch_json = user_content[user_content.rfind("\n") + 1 :]
        batch = json.loads(batch_json)
        batch_ids = [int(item["id"]) for item in batch]
        calls.append(batch_ids)
        if len(calls) == 1 and len(batch_ids) > 1:
            returned = batch[:-1]
        else:
            returned = batch
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"translations": [{"id": item["id"], "text": f"译文{item['id']}"} for item in returned]},
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

    original = sc.http_json
    original_sleep = sc.time.sleep
    sc.http_json = fake_http_json
    sc.time.sleep = lambda _seconds: None
    try:
        result = sc.translate_json_batch(
            "http://127.0.0.1:9",
            "test-model",
            "prompt",
            [{"id": 1, "text": "a"}, {"id": 2, "text": "b"}, {"id": 3, "text": "c"}],
            retries=3,
        )
    finally:
        sc.http_json = original
        sc.time.sleep = original_sleep
    if result != {1: "译文1", 2: "译文2", 3: "译文3"}:
        raise AssertionError(f"Partial-batch retry did not recover all ids: {result}")
    if calls != [[1, 2, 3], [3]]:
        raise AssertionError(f"Partial-batch retry sent wrong batches: {calls}")

    empty_calls: list[list[int]] = []

    def empty_then_valid(url: str, payload: dict | None = None, timeout: int = 10) -> dict:
        user_content = payload["messages"][1]["content"]
        batch = json.loads(user_content[user_content.rfind("\n") + 1 :])
        batch_ids = [int(item["id"]) for item in batch]
        empty_calls.append(batch_ids)
        if len(empty_calls) == 1:
            translations = [
                {"id": 1, "text": "   "},
                {"id": 2, "text": "有效译文"},
                {"id": 999, "text": "不应接收"},
            ]
        else:
            translations = [{"id": 1, "text": "补回译文"}]
        return {"choices": [{"message": {"content": json.dumps({"translations": translations}, ensure_ascii=False)}}]}

    sc.http_json = empty_then_valid
    sc.time.sleep = lambda _seconds: None
    try:
        recovered = sc.translate_json_batch(
            "http://127.0.0.1:9",
            "test-model",
            "prompt",
            [{"id": 1, "text": "a"}, {"id": 2, "text": "b"}],
            retries=2,
        )
    finally:
        sc.http_json = original
        sc.time.sleep = original_sleep
    if recovered != {1: "补回译文", 2: "有效译文"}:
        raise AssertionError(f"Empty or extraneous translation was accepted: {recovered}")
    if empty_calls != [[1, 2], [1]]:
        raise AssertionError(f"Empty translation was not retried locally: {empty_calls}")


def test_sakura_server_cleanup() -> None:
    """A startup timeout must not leave a server process or open log handle."""
    sc = sakura_client

    class FakeProcess:
        def __init__(self) -> None:
            self.returncode = None
            self.terminated = False
            self.killed = False

        def poll(self):
            return 0 if self.terminated or self.killed else None

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

        def wait(self, timeout=None) -> int:
            self.returncode = 0
            return 0

    with tempfile.TemporaryDirectory(prefix="sakura_cleanup_") as temp_dir:
        root = Path(temp_dir)
        executable = root / "llama-server.exe"
        model = root / "model.gguf"
        executable.write_bytes(b"exe")
        model.write_bytes(b"model")
        log_path = root / "sakura.log"
        fake = FakeProcess()
        original_ready = sc.endpoint_ready
        original_popen = sc.subprocess.Popen
        original_wait = sc.wait_for_endpoint
        sc.endpoint_ready = lambda _endpoint: False
        launch_commands = []
        def launch(command, **kwargs):
            launch_commands.append(command)
            return fake
        sc.subprocess.Popen = launch
        sc.wait_for_endpoint = lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit("timeout"))
        try:
            try:
                sc.start_server(
                    {"server_executable": str(executable), "model": str(model)},
                    "http://127.0.0.1:8989",
                    log_path,
                )
            except SystemExit as exc:
                if "timeout" not in str(exc):
                    raise
            else:
                raise AssertionError("Simulated Sakura startup timeout was ignored")
            log = getattr(fake, "_roast_log", None)
            if not fake.terminated or log is None or not log.closed:
                raise AssertionError("Sakura startup timeout leaked its process or log handle")
            command = launch_commands[0]
            if command[command.index("--ctx-size") + 1] != "16384" or command[command.index("--parallel") + 1] != "1":
                raise AssertionError("Sakura startup ignored bounded context or sequential processing")

            sc.subprocess.Popen = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("spawn failed"))
            failed_log = root / "spawn_failed.log"
            try:
                sc.start_server(
                    {"server_executable": str(executable), "model": str(model)},
                    "http://127.0.0.1:8989",
                    failed_log,
                )
            except OSError as exc:
                if "spawn failed" not in str(exc):
                    raise
            else:
                raise AssertionError("Simulated Sakura spawn failure was ignored")
            renamed = failed_log.with_suffix(".closed")
            failed_log.rename(renamed)
        finally:
            sc.endpoint_ready = original_ready
            sc.subprocess.Popen = original_popen
            sc.wait_for_endpoint = original_wait


def test_translation_progress_eta() -> None:
    with tempfile.TemporaryDirectory(prefix="translation_eta_") as temp_dir:
        path = Path(temp_dir) / "progress.json"
        original_monotonic = translation_checkpoint.time.monotonic
        translation_checkpoint.time.monotonic = lambda: 110.0
        try:
            translation_checkpoint.save_translation_progress(
                path,
                completed=2,
                total=5,
                started_at=100.0,
                status="RUNNING",
                baseline_completed=2,
            )
            idle = json.loads(path.read_text(encoding="utf-8"))
            if idle["processed_this_attempt"] != 0 or idle["eta_seconds"] is not None:
                raise AssertionError(f"Old checkpoint work distorted resumed ETA: {idle}")
            translation_checkpoint.save_translation_progress(
                path,
                completed=3,
                total=5,
                started_at=100.0,
                status="RUNNING",
                baseline_completed=2,
            )
            active = json.loads(path.read_text(encoding="utf-8"))
            if active["processed_this_attempt"] != 1 or active["eta_seconds"] != 20.0:
                raise AssertionError(f"Resumed ETA did not use this attempt's rate: {active}")
        finally:
            translation_checkpoint.time.monotonic = original_monotonic


def test_model_identity() -> None:
    with tempfile.TemporaryDirectory(prefix="model_identity_") as temp_dir:
        model = Path(temp_dir) / "Sakura-Galtransl.gguf"
        model.write_bytes(b"first")
        first = adapter._model_identity(model)
        model.write_bytes(b"second-version")
        second = adapter._model_identity(model)
        if first == second or first["path"] != second["path"]:
            raise AssertionError(f"Same-name model replacement was not detected: {first}, {second}")


if __name__ == "__main__":
    raise SystemExit(main())
