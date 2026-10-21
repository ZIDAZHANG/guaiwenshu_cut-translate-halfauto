#!/usr/bin/env python3
"""Verify HTTP evidence against real loopback bytes, including failed responses."""
from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import sakura_client as client


class HttpEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "中文任务"
        self.requests = []
        self.responses = [(200, b'{"ok": true}')]
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                owner.requests.append(body)
                status, response, *content_types = owner.responses.pop(0)
                self.send_response(status)
                self.send_header("Content-Type", content_types[0] if content_types else "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                if content_types:
                    # Socket writes split UTF-8 and SSE JSON at arbitrary byte positions.
                    for start in range(0, len(response), 7):
                        self.wfile.write(response[start:start + 7])
                        self.wfile.flush()
                else:
                    self.wfile.write(response)
            def log_message(self, *_):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def exchanges(self):
        return sorted(p.parent for p in self.root.rglob("exchange.json"))

    def sse(self, chunks, *, done=True, finish="stop"):
        events = []
        for content in chunks:
            events.append({"choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]})
        if finish is not None:
            events.append({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
        body = "".join("data: " + json.dumps(e, ensure_ascii=False) + "\n\n" for e in events)
        if done:
            body += "data: [DONE]\n\n"
        return body.encode("utf-8")

    def test_stream_keeps_exact_received_bytes_and_complete_unbounded_content(self):
        chunks = ["日本語→中文\n", "完整未完句…" * 1000, "重复句。重复句。"]
        body = self.sse(chunks)
        self.responses = [(200, body, "text/event-stream; charset=utf-8")]
        seen = []
        result = client.http_json(self.endpoint, {"stream": True}, audit_dir=self.root, on_stream=seen.append)
        self.assertEqual(result["choices"][0]["message"]["content"], "".join(chunks))
        self.assertEqual(seen, chunks)
        self.assert_bytes(self.exchanges()[0], body, "PARSED", 200)
        self.assertTrue(json.loads((self.exchanges()[0] / "exchange.json").read_text())["response_complete"])

    def test_stream_without_completion_marker_retains_partial_body_and_refuses_success(self):
        body = self.sse(["收到的半句"], done=False, finish=None)
        self.responses = [(200, body, "text/event-stream")]
        with self.assertRaisesRegex(ValueError, "before a complete final response"):
            client.http_json(self.endpoint, {"stream": True}, audit_dir=self.root)
        self.assert_bytes(self.exchanges()[0], body, "FAILED", 200)
        self.assertFalse(json.loads((self.exchanges()[0] / "exchange.json").read_text())["response_complete"])

    def test_invalid_stream_event_keeps_received_bytes_before_parse(self):
        body = b'data: {"broken":\n\n'
        self.responses = [(200, body, "text/event-stream")]
        with self.assertRaises(json.JSONDecodeError):
            client.http_json(self.endpoint, {"stream": True}, audit_dir=self.root)
        self.assert_bytes(self.exchanges()[0], body, "FAILED", 200)

    def test_streamed_model_length_stop_is_still_rejected(self):
        body = self.sse(["截断的译文"], finish="length")
        self.responses = [(200, body, "text/event-stream")]
        with self.assertRaisesRegex(RuntimeError, "truncated"):
            client.translate_batch(self.endpoint, "test", "直译", [{"id": 1, "text": "原文"}], retries=0, audit_dir=self.root)
        self.assert_bytes(self.exchanges()[0], body, "PARSED", 200)
        self.assertEqual(json.loads(next(self.root.rglob("translation.json")).read_text())["state"], "FAILED")

    def test_finished_subrequest_is_reported_before_later_unit_fails(self):
        answer = json.dumps({"choices": [{"message": {"content": "已经收到的完整译文"}, "finish_reason": "stop"}]}, ensure_ascii=False).encode("utf-8")
        self.responses = [(200, answer), (500, b'{"error":"fixture failure"}')]
        committed = {}
        batch = [{"id": 1, "text": "先に"}, {"id": 2, "text": "多行\n未完句"}]
        with self.assertRaises(RuntimeError):
            client.translate_batch(self.endpoint, "test", "直译", batch, retries=0, audit_dir=self.root, on_mapped=committed.update)
        self.assertEqual(committed, {1: "已经收到的完整译文"})
        self.assertEqual({json.loads(p.read_text())["state"] for p in self.root.rglob("translation.json")}, {"MAPPED", "FAILED"})

    def test_adapter_commits_partial_batch_and_resumes_only_missing_cue(self):
        import argparse
        import voicetransl_adapter as adapter
        source, output = Path(self.temp.name) / "source.srt", self.root / "zh.srt"
        source.write_text("1\n00:00:00,000 --> 00:00:01,000\n先に\n\n2\n00:00:01,000 --> 00:00:02,000\n多行\n未完句\n", encoding="utf-8")
        prompt, glossary, model = [Path(self.temp.name) / name for name in ("prompt.txt", "terms.json", "model.gguf")]
        prompt.write_text("忠实翻译、保留全文和未完句", encoding="utf-8")
        glossary.write_text("{}", encoding="utf-8")
        model.write_bytes(b"fixture model identity; no actual model invocation")
        config = Path(self.temp.name) / "config.json"
        config.write_text(json.dumps({"resources": {"translation_prompt": str(prompt), "translation_glossary": str(glossary)},
                                     "translation": {"model": str(model), "use_native_dictionaries": False}}), encoding="utf-8")
        args = argparse.Namespace(config=config, input=source, output=output, endpoint=self.endpoint, batch_size=10)
        answer = lambda text: json.dumps({"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}, ensure_ascii=False).encode("utf-8")
        self.responses = [(200, answer("先前的完整译文"))] + [(500, b'{"error":"fixture failure"}')] * 3
        with patch.object(adapter, "start_server", return_value=None), patch.object(adapter, "stop_server"), patch.object(client.time, "sleep"):
            with self.assertRaises(RuntimeError):
                adapter.translate(args)
        checkpoint_path = self.root / "translation_checkpoint.json"
        checkpoint = json.loads(checkpoint_path.read_text())
        self.assertEqual(checkpoint["translations"], {"1": "先前的完整译文"})
        self.assertEqual(checkpoint["status"], "FAILED")
        self.assertFalse(output.exists())
        self.responses = [(200, answer("后续的完整译文\n未完…"))]
        with patch.object(adapter, "start_server", return_value=None), patch.object(adapter, "stop_server"):
            self.assertEqual(adapter.translate(args), 0)
        checkpoint = json.loads(checkpoint_path.read_text())
        self.assertEqual(checkpoint["completed"], 2)
        self.assertIn("先前的完整译文", output.read_text(encoding="utf-8-sig"))
        user = json.loads(self.requests[-1])["messages"][1]["content"]
        self.assertNotIn("先に", user)
        self.assertIn("多行\n未完句", user)

    def test_untranslated_japanese_is_preserved_and_reaches_final_review(self):
        import argparse
        import voicetransl_adapter as adapter
        from subtitle_axis_align import Cue, parse_srt
        from subtitle_segments import build_segments, publish_segment_output
        from artifact_validation import validate_review_csv
        source, output = Path(self.temp.name) / "source.srt", self.root / "zh.srt"
        original = "最後スーッときつい格好のラモさん映ってダメだった"
        returned = "最后スーッときつい格好のラモさん映ってダメだった"
        source.write_text(f"1\n00:00:00,000 --> 00:00:01,000\n{original}\n", encoding="utf-8")
        records = build_segments([Cue(0, 1000, original, 1)], mode="speech")
        source.with_suffix(".segments.json").write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        prompt, glossary, model = [Path(self.temp.name) / name for name in ("prompt.txt", "terms.json", "model.gguf")]
        prompt.write_text("保留全部内容", encoding="utf-8")
        glossary.write_text("{}", encoding="utf-8")
        model.write_bytes(b"fixture, not a native model")
        config = Path(self.temp.name) / "config.json"
        config.write_text(json.dumps({"resources": {"translation_prompt": str(prompt), "translation_glossary": str(glossary)},
                                     "translation": {"model": str(model), "use_native_dictionaries": False}}), encoding="utf-8")
        args = argparse.Namespace(config=config, input=source, output=output, endpoint=self.endpoint, batch_size=10)
        self.responses = [(200, self.answer(returned))]
        with patch.object(adapter, "start_server", return_value=None), patch.object(adapter, "stop_server"):
            self.assertEqual(adapter.translate(args), 0)
        translations = parse_srt(output)
        self.assertEqual(translations[0].text, returned)
        metadata = json.loads(output.with_suffix(".segments.json").read_text())
        self.assertEqual(metadata["language_review"][0]["line"], 1)
        final, review = self.root / "final.ass", self.root / "review.csv"
        publish_segment_output(records, translations, final, review, translation_metadata=metadata)
        self.assertEqual(validate_review_csv(review).details["needs_review"], 1)
        self.assertIn("TRANSLATION_JAPANESE_PRESENT", review.read_text(encoding="utf-8-sig"))

    def test_dictionary_approved_japanese_name_is_not_treated_as_untranslated_speech(self):
        from voice_dictionary import has_unreviewed_kana
        self.assertFalse(has_unreviewed_kana("ラモさん真漂亮", ["ラモさん"]))
        self.assertTrue(has_unreviewed_kana("ラモさんスーッと出现", ["ラモさん"]))
        self.assertFalse(has_unreviewed_kana("拉莫・纳刚，唯有你！"))
        self.assertTrue(has_unreviewed_kana("ｽｰｯと"))

    def test_native_gpt_name_omission_and_explicit_uncertainty_reach_final_review(self):
        import argparse
        import voicetransl_adapter as adapter
        from subtitle_axis_align import Cue, parse_srt
        from subtitle_segments import build_segments, publish_segment_output
        from artifact_validation import validate_review_csv
        source, output = Path(self.temp.name) / "source.srt", self.root / "zh.srt"
        originals = ["サプライズラモーヌやめろ", "ラモーヌが来た"]
        returned = ["别给我惊喜了", "ラモーヌ [待确认]来了"]
        source.write_text("1\n00:00:00,000 --> 00:00:01,000\n" + originals[0] +
                          "\n\n2\n00:00:01,000 --> 00:00:02,000\n" + originals[1] + "\n", encoding="utf-8")
        records = build_segments([Cue(i*1000,(i+1)*1000,text,i+1) for i,text in enumerate(originals)], mode="speech")
        source.with_suffix(".segments.json").write_text(json.dumps(records,ensure_ascii=False),encoding="utf-8")
        prompt, glossary, model, dictionary = [Path(self.temp.name)/name for name in ("prompt.txt","terms.json","model.gguf","gpt.txt")]
        prompt.write_text("保留全部人名", encoding="utf-8")
        glossary.write_text("{}", encoding="utf-8")
        model.write_bytes(b"fixture, not a native model")
        dictionary.write_text("ラモーヌ\tラモーヌ [待确认]\n",encoding="utf-8")
        config = Path(self.temp.name)/"config.json"
        config.write_text(json.dumps({"resources":{"translation_prompt":str(prompt),"translation_glossary":str(glossary)},
                                     "translation":{"model":str(model),"native_gpt_dictionary":str(dictionary)}}),encoding="utf-8")
        self.responses = [(200,self.answer("\n".join(returned)))]
        args = argparse.Namespace(config=config,input=source,output=output,endpoint=self.endpoint,batch_size=10)
        with patch.object(adapter,"start_server",return_value=None),patch.object(adapter,"stop_server"):
            self.assertEqual(adapter.translate(args),0)
        metadata = json.loads(output.with_suffix(".segments.json").read_text())
        self.assertEqual(metadata["language_review"],[])
        self.assertEqual(metadata["term_review"],[{"line":1,"missing_terms":["ラモーヌ [待确认]"]}])
        self.assertIn("ラモーヌ->ラモーヌ [待确认]",json.loads(self.requests[0])["messages"][1]["content"])
        self.assertIn("TRANSLATION_TERM_MISSING", metadata["translations"][0]["flags"])
        self.assertIn("TRANSLATION_UNCERTAIN", metadata["translations"][1]["flags"])
        self.assertEqual([cue.text for cue in parse_srt(output)],returned)
        final, review = self.root/"final.ass", self.root/"review.csv"
        publish_segment_output(records,parse_srt(output),final,review,translation_metadata=metadata)
        self.assertEqual(validate_review_csv(review).details["needs_review"],2)

    def assert_bytes(self, directory, response, state, status):
        self.assertEqual((directory / "response.body").read_bytes(), response)
        request = (directory / "request.json").read_bytes()
        self.assertIn(request, self.requests)
        meta = json.loads((directory / "exchange.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["request_sha256"], hashlib.sha256(request).hexdigest())
        self.assertEqual(meta["response_sha256"], hashlib.sha256(response).hexdigest())
        self.assertEqual(meta["response_bytes"], len(response))
        self.assertEqual((meta["state"], meta["status"]), (state, status))
        self.assertIn("finished_at", meta)

    def test_exact_unicode_request_and_unmodified_response(self):
        body = (" " + json.dumps({"结果": "日本語\n中文", "空格": True}, ensure_ascii=False) + " \n").encode("utf-8")
        self.responses = [(200, body)]
        payload = {"text": "日本語\n未完句…" * 4000, "context": "父句全部"}
        result = client.http_json(self.endpoint, payload, audit_dir=self.root)
        self.assertEqual(result["结果"], "日本語\n中文")
        self.assertEqual(self.requests, [json.dumps(payload, ensure_ascii=False).encode("utf-8")])
        self.assert_bytes(self.exchanges()[0], body, "PARSED", 200)

    def test_malformed_json_keeps_the_complete_body(self):
        body = b'{"unfinished": ' + '原始错误'.encode("utf-8")
        self.responses = [(200, body)]
        with self.assertRaises(json.JSONDecodeError):
            client.http_json(self.endpoint, {"input": "原文"}, audit_dir=self.root)
        self.assert_bytes(self.exchanges()[0], body, "FAILED", 200)

    def test_invalid_utf8_is_not_replaced(self):
        body = b'{"result":"\xff"}'
        self.responses = [(200, body)]
        with self.assertRaises(UnicodeDecodeError):
            client.http_json(self.endpoint, {"input": "原文"}, audit_dir=self.root)
        self.assert_bytes(self.exchanges()[0], body, "FAILED", 200)

    def test_http_failure_retains_error_body_and_status(self):
        body = '错误：不支持请求格式'.encode("utf-8")
        self.responses = [(422, body)]
        with self.assertRaises(urllib.error.HTTPError):
            client.http_json(self.endpoint, {"input": "原文"}, audit_dir=self.root)
        self.assert_bytes(self.exchanges()[0], body, "FAILED", 422)

    def test_schema_fallback_keeps_both_requests(self):
        error = b'{"error":"schema unsupported"}'
        answer = json.dumps({"choices": [{"message": {"content": '{"translations":[{"id":7,"text":"完整译文"}]}'}, "finish_reason": "stop"}]}, ensure_ascii=False).encode("utf-8")
        self.responses = [(422, error), (200, answer)]
        with patch.object(client.time, "sleep"):
            result = client.translate_json_batch(self.endpoint, "test-model", "忠实翻译", [{"id": 7, "text": "原文", "context": "完整父句"}], retries=1, audit_dir=self.root)
        self.assertEqual(result, {7: "完整译文"})
        self.assertEqual(len(self.exchanges()), 2)
        self.assertIn("response_format", json.loads(self.requests[0]))
        self.assertNotIn("response_format", json.loads(self.requests[1]))
        actual = {(p / "response.body").read_bytes(): p for p in self.exchanges()}
        self.assert_bytes(actual[error], error, "FAILED", 422)
        self.assert_bytes(actual[answer], answer, "PARSED", 200)

    def test_generation_truncation_keeps_response_but_is_rejected(self):
        answer = json.dumps({"choices": [{"message": {"content": '{"translations":[{"id":1,"text":"不完整译文"}]}'}, "finish_reason": "length"}]}, ensure_ascii=False).encode("utf-8")
        self.responses = [(200, answer)]
        with self.assertRaisesRegex(RuntimeError, "truncated"):
            client.translate_batch(self.endpoint, "test-model", "忠实翻译", [{"id": 1, "text": "原文"}], retries=0, audit_dir=self.root)
        self.assert_bytes(self.exchanges()[0], answer, "PARSED", 200)


    @staticmethod
    def answer(text, finish_reason="stop"):
        return json.dumps({"choices": [{"message": {"content": text}, "finish_reason": finish_reason}]}, ensure_ascii=False).encode("utf-8")

    def test_plain_body_retains_context_style_and_nonsequential_ids(self):
        batch = [{"id": 41, "cue_id": "stable-a", "text": "城の話", "context": [{"parent_id": "parent-a", "text": "完全な父句"}]},
                 {"id": 7, "cue_id": "stable-b", "text": "次の話", "context": [{"parent_id": "parent-b", "text": "未完句…"}]}]
        body = self.answer("城堡的事\n接下来这事")
        self.responses = [(200, body)]
        result = client.translate_batch(self.endpoint, "test-model", "完整风格\n[Glossary]\n城->城堡", batch, retries=0, audit_dir=self.root)
        self.assertEqual(result, {41: "城堡的事", 7: "接下来这事"})
        payload = json.loads(self.requests[0])
        self.assertNotIn("response_format", payload)
        self.assertNotIn("max_tokens", payload)
        self.assertEqual(payload["temperature"], 0.3)
        self.assertEqual(payload["top_p"], 0.8)
        self.assertIn("完整风格\n[Glossary]\n城->城堡", payload["messages"][0]["content"])
        self.assertIn("完全な父句", payload["messages"][0]["content"])
        self.assertIn("未完句…", payload["messages"][0]["content"])
        self.assertNotIn("parent-a", payload["messages"][0]["content"])
        self.assertNotIn("完全な父句", payload["messages"][1]["content"])
        self.assertTrue(payload["messages"][1]["content"].endswith("城の話\n次の話"))
        records = list(self.root.rglob("translation.json"))
        record = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertEqual(record["items"], batch)
        self.assertEqual(record["state"], "MAPPED")
        self.assert_bytes(self.exchanges()[0], body, "PARSED", 200)

    def test_visual_ocr_is_sent_as_labeled_reference_without_overwriting_audio_body(self):
        batch = [{"id": 7, "text": "ある男のせいで", "context": [
            {"kind": "speech_parent", "text": "ある男のせいでラブホに見える"},
            {"kind": "visual_ocr", "visual_id": "native-page", "text": "アルダンのせいでラブホに見える"}]}]
        self.responses = [(200, self.answer("因为阿尔丹"))]
        translated = client.translate_batch(self.endpoint, "test", "全部の風格", batch,
                                            retries=0, audit_dir=self.root)
        self.assertEqual(translated, {7: "因为阿尔丹"})
        payload = json.loads(self.requests[0])
        self.assertIn("画面 OCR 参考：アルダンのせいでラブホに見える", payload["messages"][0]["content"])
        self.assertIn("语音父句：ある男のせいでラブホに見える", payload["messages"][0]["content"])
        self.assertNotIn("native-page", payload["messages"][0]["content"])
        self.assertTrue(payload["messages"][1]["content"].endswith("ある男のせいで"))
        self.assertNotIn("ラブホ", payload["messages"][1]["content"])

    def test_complete_parent_already_in_body_is_not_repeated_as_system_japanese(self):
        text = "最後スーッときつい格好のラモさん映ってダメだった"
        batch = [{"id": 7, "text": text, "context": [{"parent_id": "p", "text": text}]},
                 {"id": 8, "text": text, "context": [{"parent_id": "q", "text": text}]}]
        self.responses = [(200, self.answer("第一条完整译文\n第二条完整译文"))]
        result = client.translate_batch(self.endpoint, "test", "完整风格", batch, retries=0, audit_dir=self.root)
        self.assertEqual(result, {7: "第一条完整译文", 8: "第二条完整译文"})
        payload = json.loads(self.requests[0])
        self.assertNotIn(text, payload["messages"][0]["content"])
        self.assertEqual(payload["messages"][1]["content"].count(text), 2)
        record = json.loads(next(self.root.rglob("translation.json")).read_text(encoding="utf-8"))
        self.assertEqual(record["items"], batch)
    def test_missing_line_is_not_assigned_to_a_following_cue(self):
        batch = [{"id": 41, "text": "最初", "context": "全体の父句"}, {"id": 7, "text": "次"}]
        self.responses = [(200, self.answer("缺了一行")), (200, self.answer("第一句")), (200, self.answer("第二句"))]
        result = client.translate_batch(self.endpoint, "test-model", "忠实翻译", batch, retries=0, audit_dir=self.root)
        self.assertEqual(result, {41: "第一句", 7: "第二句"})
        self.assertEqual(len(self.requests), 3)
        for raw in self.requests[1:]:
            user = "\n".join(item["content"] for item in json.loads(raw)["messages"])
            for original in ("最初", "次", "全体の父句"):
                self.assertIn(original, user)
        records = [json.loads(p.read_text(encoding="utf-8")) for p in self.root.rglob("translation.json")]
        self.assertEqual(sorted(r["state"] for r in records), ["AMBIGUOUS_LINES", "MAPPED", "MAPPED"])

    def test_multiline_cue_keeps_every_line_and_neighboring_reference(self):
        batch = [{"id": 1, "text": "上の行\n下の行"}, {"id": 2, "text": "次"}, {"id": 3, "text": "最後"}]
        self.responses = [(200, self.answer("上面一行\n下面一行")), (200, self.answer("下一句\n最后一句"))]
        result = client.translate_batch(self.endpoint, "test-model", "忠实翻译", batch, retries=0, audit_dir=self.root)
        self.assertEqual(result, {1: "上面一行\n下面一行", 2: "下一句", 3: "最后一句"})
        self.assertEqual(len(self.requests), 2)
        for raw in self.requests:
            user = "\n".join(item["content"] for item in json.loads(raw)["messages"])
            self.assertIn("上の行\n下の行", user)
            self.assertIn("最後", user)

    def test_plain_transient_failure_retries_without_grammar(self):
        self.responses = [(200, b'{"error":"temporary failure"}'), (200, self.answer("完整译文"))]
        with patch.object(client.time, "sleep") as sleep:
            result = client.translate_batch(self.endpoint, "test-model", "忠实翻译", [{"id": 1, "text": "原文"}], retries=1, audit_dir=self.root)
        self.assertEqual(result, {1: "完整译文"})
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(len(self.exchanges()), 2)
        for raw in self.requests:
            self.assertNotIn("response_format", json.loads(raw))

    def test_true_repeated_source_and_translation_are_retained(self):
        batch = [{"id": 1, "text": "同じ"}, {"id": 2, "text": "同じ"}, {"id": 3, "text": "異なる"}]
        self.responses = [(200, self.answer("一样\n一样\n不同"))]
        result = client.translate_batch(self.endpoint, "test-model", "忠实翻译", batch, retries=0)
        self.assertEqual(result, {1: "一样", 2: "一样", 3: "不同"})

    def test_explicit_sampler_is_sent_without_project_clamping(self):
        self.responses = [(200, self.answer("完整译文"))]
        result = client.translate_batch(self.endpoint, "test-model", "忠实翻译",
                                        [{"id": 1, "text": "原文"}], retries=0,
                                        temperature=0.55, top_p=0.95)
        self.assertEqual(result, {1: "完整译文"})
        payload = json.loads(self.requests[0])
        self.assertEqual(payload["temperature"], 0.55)
        self.assertEqual(payload["top_p"], 0.95)


if __name__ == "__main__":
    unittest.main()
