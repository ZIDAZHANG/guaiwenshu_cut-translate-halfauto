"""User-authorized unlimited content and truthful completion regressions."""
import argparse, contextlib, io, json, tempfile, unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import artifact_validation as validation
import roast_pipeline
import sakura_client
import voicetransl_adapter as adapter
from subtitle_axis_align import Cue, align

class UnrestrictedPipelineTests(unittest.TestCase):
    def test_all_contained_source_cues_survive_legacy_join_limit(self):
        target=[Cue(0,9000,"",1)]
        sources=[Cue(i*1000,(i+1)*1000,f"第{i+1}句",i+1) for i in range(9)]
        text,rows=align(target,sources,[],1200,.18,3,"none")
        self.assertEqual(text,["\n".join(c.text for c in sources)])
        self.assertEqual(rows[0]["zh_indices"],",".join(str(i) for i in range(1,10)))

    def test_long_content_and_duration_are_preserved(self):
        original="長い台詞を省略しない。"*3000
        sources=[Cue(0,1800000,original,1)]
        text,_=align([Cue(0,1800000,"",1)],sources,[],1200,.18,3,"none")
        self.assertEqual(text,[original])

    def test_matching_time_cannot_verify_conflicting_japanese(self):
        _,rows=align([Cue(0,2000,"今日は晴れです",1)],
                     [Cue(0,2000,"明天下雨。",1)],[Cue(0,2000,"明日は雨です",1)],1200,.18,3,"none")
        self.assertIn("TEXT_MISMATCH",rows[0]["flags"])
        self.assertIn("CHECK",rows[0]["flags"])
        self.assertNotEqual(rows[0]["confidence"],"high")

    def test_count_difference_is_evidence_not_arbitrary_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"subtitle.srt"
            path.write_text("1\n00:00:00,000 --> 00:00:10,000\n完整内容\n",encoding="utf-8")
            result=validation.validate_subtitle(path,"final",require_text=True,expected_cues=100)
            self.assertTrue(result.valid,result.errors)
            self.assertEqual(result.details["cue_difference"],99)
            self.assertTrue(any("cue count differs" in item for item in result.warnings))

    def test_job_history_keeps_every_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);job=root/"new";job.mkdir()
            index=root/"jobs_index.json"
            index.write_text(json.dumps({"jobs":[{"job_dir":str(root/str(i))} for i in range(230)]}),encoding="utf-8")
            args=argparse.Namespace(jianying_draft=None,dry_run=False,skip_download=True,skip_audio=True,skip_external=True,wait_missing=False,copy_media=False,skip_jianying_package=True)
            with contextlib.redirect_stdout(io.StringIO()):
                roast_pipeline.write_job_manifest(job,{},args)
            self.assertEqual(len(json.loads(index.read_text(encoding="utf-8"))["jobs"]),231)

    def test_translation_request_does_not_cap_output_per_cue(self):
        requests=[]
        long_text="完整翻译"*3000
        def respond(url,payload=None,timeout=None):
            requests.append(payload)
            return {"choices":[{"finish_reason":"stop","message":{"content":long_text}}]}
        with patch.object(sakura_client,"http_json",side_effect=respond):
            result=sakura_client.translate_batch("http://127.0.0.1:8989","model","直译",[{"id":1,"text":"原文"*3000}],retries=0)
        self.assertEqual(result,{1:long_text})
        self.assertFalse("max_tokens" in requests[0], "Project output cap is still present")

    def test_truncated_provider_response_cannot_be_completed(self):
        response={"choices":[{"finish_reason":"length","message":{"content":'{"translations":[{"id":1,"text":"部分内容"}]}'}}]}
        with patch.object(sakura_client,"http_json",return_value=response):
            with self.assertRaisesRegex(RuntimeError,"truncat"):
                sakura_client.translate_batch("http://127.0.0.1:8989","model","直译",[{"id":1,"text":"原文"}],retries=0)

    def test_server_uses_user_context_without_project_clamp(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);exe=root/"server.exe";model=root/"model.gguf"
            exe.write_bytes(b"fixture");model.write_bytes(b"fixture")
            process=MagicMock();process.poll.return_value=0
            with patch.object(sakura_client,"endpoint_ready",return_value=False),patch.object(sakura_client,"wait_for_endpoint"),patch.object(sakura_client,"require_server_model"),patch.object(sakura_client.subprocess,"Popen",return_value=process) as launch:
                sakura_client.start_server({"server_executable":str(exe),"model":str(model),"context_size":131072},"http://127.0.0.1:8989",root/"server.log")
                try:
                    command=launch.call_args.args[0]
                    self.assertEqual(command[command.index("--ctx-size")+1],"131072")
                finally:
                    sakura_client.stop_server(process)

    def test_user_batch_size_is_not_silently_reduced_to_thirty(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/"jp.srt";output=root/"zh.srt";prompt=root/"prompt.txt"
            source.write_text("\n\n".join(f"{i+1}\n00:00:{i:02d},000 --> 00:00:{i+1:02d},000\n日本語{i+1}" for i in range(50))+"\n",encoding="utf-8")
            prompt.write_text("完整直译",encoding="utf-8")
            config=root/"config.json"
            config.write_text(json.dumps({"translation":{"batch_size":50,"use_native_dictionaries":False},"resources":{"translation_prompt":str(prompt)}}),encoding="utf-8")
            batches=[]
            def translate(endpoint,model,prompt,items,**kwargs):
                batches.append(items)
                return {item["id"]:f"中文{item['id']}" for item in items}
            args=argparse.Namespace(config=config,input=source,output=output,endpoint=None,batch_size=None)
            with patch.object(adapter,"start_server",return_value=None),patch.object(adapter,"translate_batch",side_effect=translate),contextlib.redirect_stdout(io.StringIO()):
                adapter.translate(args)
            self.assertEqual([len(batch) for batch in batches],[50])

    def test_external_file_wait_has_no_implicit_one_hour_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"late.srt"
            def delivered_after_wait(_):
                path.write_text("complete external subtitle", encoding="utf-8")
            with patch.object(roast_pipeline.time, "time", return_value=7200), \
                 patch.object(roast_pipeline.time, "sleep", side_effect=delivered_after_wait), \
                 contextlib.redirect_stdout(io.StringIO()):
                roast_pipeline.wait_for_file("late", path, 0)
            self.assertEqual(path.read_text(encoding="utf-8"), "complete external subtitle")

    def test_explicit_external_file_deadline_is_still_honored(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(roast_pipeline.time, "time", side_effect=[0, 11]), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    roast_pipeline.wait_for_file("late", Path(directory)/"absent.srt", 10)

    def test_failed_asr_preserves_candidate_and_known_good_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ("whisper.exe","model.bin","audio.wav"):(root/name).write_bytes(b"fixture")
            config=root/"config.json"
            config.write_text(json.dumps({"recognition":{"executable":str(root/"whisper.exe"),"model":str(root/"model.bin"),"vad":False}}),encoding="utf-8")
            output=root/"jp.srt";output.write_text("known good",encoding="utf-8")
            args=argparse.Namespace(config=config,executable=None,model=None,language=None,vad=None,audio=root/"audio.wav",output=output)
            def failed(command,*_):
                Path(command[command.index("-of")+1]+".srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n日本語です。\n",encoding="utf-8")
                return 1
            with patch.object(adapter,"stream_process",side_effect=failed),contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):adapter.recognize(args)
            self.assertEqual(output.read_text(encoding="utf-8"),"known good")
            self.assertTrue(list(root.glob("whisper_failed_candidate_*.srt")))

if __name__=="__main__":unittest.main()
