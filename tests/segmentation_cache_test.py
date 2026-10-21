#!/usr/bin/env python3
"""Exercise real pipeline checkpoints with changing/corrupt segment evidence."""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from subtitle_axis_align import parse_subtitle

ROOT = (Path(__file__).resolve().parents[1] / "app")
WORKSPACE = ROOT.parent.resolve()
VIDEO = ROOT / "output/smoke_inputs/video.mp4"
FFMPEG = Path(os.environ.get("FFMPEG_BINARY") or shutil.which("ffmpeg") or "ffmpeg")
PRODUCER = r'''
import argparse,json
from pathlib import Path
parser=argparse.ArgumentParser()
parser.add_argument("--input",type=Path,required=True)
parser.add_argument("--output",type=Path,required=True)
args=parser.parse_args()
records=json.loads(args.input.with_suffix(".segments.json").read_text(encoding="utf-8-sig"))
texts={"前半。":"第一部分。","後半。":"第二部分。"}
entries=[]
blocks=[]
def time(ms):
    h,rem=divmod(ms,3600000);m,rem=divmod(rem,60000);s,milli=divmod(rem,1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"
for item in records["segments"]:
    assert item["context"][0]["text"]=="前半。後半。"
    text=texts[item["text"]]
    entries.append({"cue_id":item["id"],"index":item["index"],
                    "start_ms":item["start_ms"],"end_ms":item["end_ms"],"text":text})
    blocks.append(f'{item["index"]}\n{time(item["start_ms"])} --> {time(item["end_ms"])}\n{text}')
args.output.write_text("\n\n".join(blocks)+"\n",encoding="utf-8")
args.output.with_suffix(".segments.json").write_text(json.dumps({"translations":entries},ensure_ascii=False),encoding="utf-8")
'''
AXIS = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,20,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,前半。
Dialogue: 0,0:00:02.00,0:00:04.00,Default,,0,0,0,,後半。
"""

class CacheEvidenceTests(unittest.TestCase):
    def test_sidecar_corruption_changes_and_mode_switch(self):
        self.assertTrue(VIDEO.is_file(), "Run the existing smoke input generator first")
        with tempfile.TemporaryDirectory(prefix="segment_cache_",dir=WORKSPACE/"test-results") as directory:
            root=Path(directory).resolve()
            self.assertTrue(root.is_relative_to(WORKSPACE))
            jp,axis,producer,config_path = [root/name for name in ("jp.srt","axis.ass","producer.py","config.json")]
            jp.write_text("1\n00:00:00,000 --> 00:00:04,000\n前半。後半。\n",encoding="utf-8")
            axis.write_text(AXIS,encoding="utf-8")
            native=root/"native_magia"
            models=native/"PaddleOCRModels"
            models.mkdir(parents=True)
            native_exe=native/"MagiaTimeline.exe"
            native_exe.write_bytes(b"fixture executable: command invokes producer")
            axis_model=models/"model.txt"
            axis_model.write_text(AXIS,encoding="utf-8")
            axis_producer=root/"axis_producer.py"
            axis_producer.write_text(
                "import argparse\nfrom pathlib import Path\np=argparse.ArgumentParser()\n"
                "p.add_argument('--output',type=Path)\na=p.parse_args()\n"
                "model=Path(__file__).parent/'native_magia/PaddleOCRModels/model.txt'\n"
                "a.output.write_text(model.read_text(encoding='utf-8'),encoding='utf-8')\n",encoding="utf-8")
            producer.write_text(PRODUCER,encoding="utf-8")
            words={
                "schema_version":1,"source_cues":[{"index":1,"start_ms":0,"end_ms":4000,"text":"前半。後半。"}],
                "word_times":{"1":[
                    {"char_start":0,"char_end":2,"start_ms":100,"end_ms":1800},
                    {"char_start":3,"char_end":5,"start_ms":2300,"end_ms":3800}]}}
            word_path=jp.with_suffix(".word_times.json")
            word_path.write_text(json.dumps(words,ensure_ascii=False),encoding="utf-8")
            config={
                "work_dir":str(root/"work"),"tools":{"python":sys.executable,"ffmpeg":str(FFMPEG),"fixture_translator":str(producer),"fixture_axis":str(axis_producer)},
                "media_compatibility":{"auto_convert":False,"accepted_containers":[".mp4"],"accepted_video_codecs":["h264"]},
                "recognition":{"enabled":True},"axis_settings":{"enabled":True,"executable":str(native_exe)},
                "translation":{"enabled":True},"alignment":{"timing_mode":"screen"},
                "jianying_ui":{"enabled":False},"resources":{},
                "commands":{"axis":'"{python}" "{fixture_axis}" --output "{timeline}"',
                            "translate_zh":'"{python}" "{fixture_translator}" --input "{jp}" --output "{zh}"'},
                "expected_outputs":{
                    "timeline":"{job_dir}/subs/timeline.ass","jp":"{job_dir}/subs/jp_ocr.srt",
                    "zh":"{job_dir}/subs/zh_machine.srt","final":"{job_dir}/deliverables/final.ass",
                    "review":"{job_dir}/deliverables/review.csv",
                    "jianying_import":"{job_dir}/jianying_import","jianying_status":"{job_dir}/jianying_status.json"}}
            config_path.write_text(json.dumps(config,ensure_ascii=False),encoding="utf-8")
            command=[sys.executable,str(ROOT/"tools/roast_pipeline.py"),"fixture", "--config",str(config_path),
                     "--job-name","cache","--video-file",str(VIDEO),"--jp-file",str(jp),
                     "--skip-audio","--no-jianying-ui","--skip-jianying-package"]
            job=root/"work/cache"
            runs=[]
            def run():
                result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,
                                      encoding="utf-8",check=False,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
                runs.append(result.stdout)
                self.assertEqual(result.returncode,0,result.stdout)
                return json.loads((job/"task.json").read_text(encoding="utf-8-sig"))
            initial=run()
            self.assertEqual([cue.text for cue in parse_subtitle(job/"deliverables/final.ass")[2]],
                             ["第一部分。","第二部分。"])
            self.assertEqual(initial["stages"]["ASR"]["attempt"],1)
            resumed=run()
            self.assertEqual(resumed["stages"]["TRANSLATION"]["attempt"],1)
            self.assertIn("[resume] SEGMENTATION: validated DONE checkpoint, skipping",runs[-1])
            axis_model.write_text(AXIS+"\n; changed OCR model fixture\n",encoding="utf-8")
            changed_model=run()
            self.assertEqual(changed_model["stages"]["TIMELINE"]["attempt"],2)
            self.assertEqual(changed_model["stages"]["ASR"]["attempt"],1)
            self.assertIn("[resume] ASR: validated DONE checkpoint, skipping",runs[-1])
            words["word_times"]["1"][0]["start_ms"]=300
            word_path.write_text(json.dumps(words,ensure_ascii=False),encoding="utf-8")
            changed=run()
            self.assertEqual(changed["stages"]["ASR"]["attempt"],2)
            copied=json.loads((job/"subs/jp_ocr.word_times.json").read_text(encoding="utf-8"))
            self.assertEqual(copied["word_times"]["1"][0]["start_ms"],300)
            identities=job/"subs/zh_machine.segments.json"
            value=json.loads(identities.read_text(encoding="utf-8"))
            value["translations"][0]["cue_id"]="wrong-task-cue"
            identities.write_text(json.dumps(value,ensure_ascii=False),encoding="utf-8")
            repaired=run()
            self.assertEqual(repaired["stages"]["ASR"]["attempt"],2)
            self.assertEqual(repaired["stages"]["TRANSLATION"]["attempt"],
                             changed["stages"]["TRANSLATION"]["attempt"]+1)
            fixed_ids=json.loads(identities.read_text(encoding="utf-8"))
            source_ids=json.loads((job/"subs/translation_input.segments.json").read_text(encoding="utf-8"))
            self.assertEqual(fixed_ids["translations"][0]["cue_id"],source_ids["segments"][0]["id"])
            canonical=job/"subs/translation_input.segments.json"
            value=json.loads(canonical.read_text(encoding="utf-8"))
            value["parents"][0]["text"]="前半。別の。"
            value["segments"][1]["text"]="別の。"
            canonical.write_text(json.dumps(value,ensure_ascii=False),encoding="utf-8")
            repaired_source=run()
            self.assertEqual(repaired_source["stages"]["ASR"]["attempt"],2)
            self.assertEqual(repaired_source["stages"]["SEGMENTATION"]["attempt"],
                             repaired["stages"]["SEGMENTATION"]["attempt"]+1)
            config["alignment"]["timing_mode"]="speech"
            config_path.write_text(json.dumps(config,ensure_ascii=False),encoding="utf-8")
            switched=run()
            cues=parse_subtitle(job/"deliverables/final.ass")[2]
            self.assertEqual([(cue.start,cue.end) for cue in cues],[(300,1800),(2300,3800)])
            self.assertEqual(switched["stages"]["ASR"]["attempt"],2)
            self.assertEqual([cue.text for cue in cues],["第一部分。","第二部分。"])
            quality=json.loads((job/"deliverables/quality_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(quality["uncovered_source_characters"],0)
            (WORKSPACE/"test-results/cache_sidecar_runs.log").write_text("\n".join(runs),encoding="utf-8")

    def test_latin_names_are_preserved_with_language_evidence(self):
        with tempfile.TemporaryDirectory(prefix="latin_segments_", dir=WORKSPACE/"test-results") as directory:
            root = Path(directory).resolve()
            self.assertTrue(root.is_relative_to(WORKSPACE))
            jp, axis, producer, config_path = [root/name for name in ("jp.srt", "axis.ass", "producer.py", "config.json")]
            jp.write_text("1\n00:00:00,000 --> 00:00:04,000\nBGM. OK.\n", encoding="utf-8")
            axis.write_text(AXIS.replace("前半。", "BGM.").replace("後半。", "OK."), encoding="utf-8")
            producer.write_text(PRODUCER.replace("前半。後半。", "BGM. OK.").replace(
                '"前半。":"第一部分。","後半。":"第二部分。"', '"BGM.":"BGM.","OK.":"OK."').replace('texts[item["text"]]', 'texts[item["text"].strip()]'), encoding="utf-8")
            config = {
                "work_dir":str(root/"work"), "tools":{"python":sys.executable,"ffmpeg":str(FFMPEG),"fixture_translator":str(producer)},
                "media_compatibility":{"auto_convert":False,"accepted_containers":[".mp4"],"accepted_video_codecs":["h264"]},
                "recognition":{"enabled":True}, "axis_settings":{"enabled":True}, "translation":{"enabled":True},
                "alignment":{"timing_mode":"screen"}, "jianying_ui":{"enabled":False}, "resources":{},
                "commands":{"translate_zh":'"{python}" "{fixture_translator}" --input "{jp}" --output "{zh}"'},
                "expected_outputs":{"timeline":"{job_dir}/subs/timeline.ass","jp":"{job_dir}/subs/jp_ocr.srt",
                    "zh":"{job_dir}/subs/zh_machine.srt","final":"{job_dir}/deliverables/final.ass",
                    "review":"{job_dir}/deliverables/review.csv","jianying_import":"{job_dir}/jianying_import",
                    "jianying_status":"{job_dir}/jianying_status.json"}}
            config_path.write_text(json.dumps(config,ensure_ascii=False),encoding="utf-8")
            command=[sys.executable,str(ROOT/"tools/roast_pipeline.py"),"fixture","--config",str(config_path),
                     "--job-name","latin","--video-file",str(VIDEO),"--timeline-file",str(axis),"--jp-file",str(jp),
                     "--skip-audio","--no-jianying-ui","--skip-jianying-package"]
            result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding="utf-8",
                                  check=False,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            self.assertEqual(result.returncode,0,result.stdout)
            job=root/"work/latin"
            self.assertEqual([cue.text for cue in parse_subtitle(job/"deliverables/final.ass")[2]],["BGM.","OK."])
            quality=json.loads((job/"deliverables/quality_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(quality["status"],"needs_review")
            self.assertTrue(quality["validation_warnings"]["jp"])
            self.assertTrue(quality["validation_warnings"]["zh"])

    def _fixture_config(self,root,producer):
        config=json.loads((ROOT/"config/roast_pipeline.example.json").read_text(encoding="utf-8-sig"))
        config["work_dir"]=str(root/"work")
        config["tools"].update(python=sys.executable,ffmpeg=str(FFMPEG),fixture_translator=str(producer))
        config["media_compatibility"]["auto_convert"]=False
        config["commands"]["translate_zh"]='"{python}" "{fixture_translator}" --input "{jp}" --output "{zh}"'
        config["jianying_ui"]["enabled"]=False
        return config

    def test_independent_audio_evidence_and_model_changes_preserve_original_asr(self):
        import wave
        with tempfile.TemporaryDirectory(prefix="forced_cache_",dir=WORKSPACE/"test-results") as directory:
            root=Path(directory).resolve()
            jp,axis,producer,config_path,launcher=[root/name for name in ("jp.srt","axis.ass","producer.py","config.json","launcher.py")]
            jp.write_text("1\n00:00:00,000 --> 00:00:01,500\n前半。後半。\n",encoding="utf-8")
            axis.write_text(AXIS.replace("0:00:02.00","0:00:00.80").replace("0:00:04.00","0:00:02.00"),encoding="utf-8")
            producer.write_text(PRODUCER,encoding="utf-8")
            audio=root/"audio.wav"
            with wave.open(str(audio),"wb") as wav:
                wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(b"\x00\x00"*32000)
            model=root/"model";model.mkdir();stamp=model/"clock.json";stamp.write_text('{"start":100}',encoding="utf-8")
            manifest=root/"model_manifest.json";manifest.write_text('{"fixture":true}',encoding="utf-8")
            launcher.write_text(
                "import sys,json,wave\nfrom pathlib import Path\nsys.path.insert(0,"+repr(str(ROOT/"tools"))+")\n"+
                "import roast_pipeline\nfrom forced_alignment import map_prediction_units,file_sha256,validate_evidence\n"+
                "from subtitle_axis_align import parse_subtitle\nfrom task_state import atomic_write_json\n"+
                "def fixture(settings,config_base,audio,source,output):\n"+
                " speech=parse_subtitle(source)[2]\n with wave.open(str(audio),'rb') as wav: duration=round(wav.getnframes()*1000/wav.getframerate())\n"+
                " start=json.loads((Path(settings['model'])/'clock.json').read_text(encoding='utf-8'))['start']\n"+
                " times=[start,500,1000,1800]\n result=map_prediction_units(speech,[{'text':'前半'},{'text':'後半'}],times,times,duration)\n"+
                " result.update(audio_sha256=file_sha256(audio),model_identity={'fixture':True})\n"+
                " validate_evidence(result,speech,audio)\n atomic_write_json(output,result)\n return result\n"+
                "roast_pipeline.run_external_alignment=fixture\nraise SystemExit(roast_pipeline.main())\n",encoding="utf-8")
            config=self._fixture_config(root,producer);config["alignment"]["timing_mode"]="speech"
            config["forced_alignment"]={"enabled":True,"python":sys.executable,"model":str(model),"model_manifest":str(manifest)}
            config_path.write_text(json.dumps(config,ensure_ascii=False),encoding="utf-8")
            command=[sys.executable,str(launcher),"fixture","--config",str(config_path),"--job-name","forced","--video-file",str(VIDEO),
                "--audio-file",str(audio),"--timeline-file",str(axis),"--jp-file",str(jp),"--no-jianying-ui","--skip-jianying-package"]
            job=root/"work/forced";runs=[]
            def run():
                result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding="utf-8",
                    check=False,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
                runs.append(result.stdout);self.assertEqual(result.returncode,0,result.stdout)
                return json.loads((job/"task.json").read_text(encoding="utf-8-sig"))
            initial=run()
            self.assertEqual([(c.start,c.end) for c in parse_subtitle(job/"deliverables/final.ass")[2]],[(100,500),(1000,1800)])
            self.assertEqual(parse_subtitle(job/"subs/jp_ocr.srt")[2][0].end,1500)
            resumed=run();self.assertEqual(resumed["stages"]["SEGMENTATION"]["attempt"],1)
            stamp.write_text('{"start":200}',encoding="utf-8")
            changed=run();self.assertEqual(changed["stages"]["SEGMENTATION"]["attempt"],2);self.assertEqual(changed["stages"]["ASR"]["attempt"],1)
            self.assertEqual(parse_subtitle(job/"deliverables/final.ass")[2][0].start,200)
            sidecar=job/"subs/translation_input.segments.forced_words.json";value=json.loads(sidecar.read_text(encoding="utf-8"))
            value["word_times"]["1"][0]["start_ms"]=300;sidecar.write_text(json.dumps(value,ensure_ascii=False),encoding="utf-8")
            repaired=run();self.assertEqual(repaired["stages"]["SEGMENTATION"]["attempt"],3);self.assertEqual(repaired["stages"]["ASR"]["attempt"],1)
            sidecar.unlink()
            missing=run();self.assertEqual(missing["stages"]["SEGMENTATION"]["attempt"],4);self.assertEqual(missing["stages"]["ASR"]["attempt"],1)
            (WORKSPACE/"test-results/cache_independent_runs.log").write_text("\n".join(runs),encoding="utf-8")

    def test_same_caption_background_cut_and_real_repeat_survive_final_output(self):
        with tempfile.TemporaryDirectory(prefix="same_caption_",dir=WORKSPACE/"test-results") as directory:
            root=Path(directory).resolve()
            jp,axis,producer,config_path=[root/name for name in ("jp.srt","axis.ass","producer.py","config.json")]
            jp.write_text("1\n00:00:00,000 --> 00:00:04,000\n同じ台詞。\n\n2\n00:00:04,000 --> 00:00:06,000\n同じ台詞。\n",encoding="utf-8")
            axis_text=AXIS.replace("前半。","同じ台詞。").replace("後半。","同じ台詞。")
            axis_text+='Dialogue: 0,0:00:04.00,0:00:06.00,Default,,0,0,0,,同じ台詞。\n'
            axis.write_text(axis_text,encoding="utf-8")
            producer.write_text(PRODUCER.replace("前半。後半。","同じ台詞。").replace(
                '"前半。":"第一部分。","後半。":"第二部分。"', '"同じ台詞。":"相同台词。"'),encoding="utf-8")
            config=self._fixture_config(root,producer)
            config_path.write_text(json.dumps(config,ensure_ascii=False),encoding="utf-8")
            command=[sys.executable,str(ROOT/"tools/roast_pipeline.py"),"fixture","--config",str(config_path),"--job-name","background",
                "--video-file",str(VIDEO),"--timeline-file",str(axis),"--jp-file",str(jp),"--skip-audio","--no-jianying-ui","--skip-jianying-package"]
            result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding="utf-8",
                check=False,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            self.assertEqual(result.returncode,0,result.stdout)
            job=root/"work/background";records=json.loads((job/"subs/translation_input.segments.json").read_text(encoding="utf-8"))
            self.assertEqual([len(i["visual_ids"]) for i in records["segments"]],[2,1])
            self.assertEqual([(c.start,c.end,c.text) for c in parse_subtitle(job/"deliverables/final.ass")[2]],
                [(0,4000,"相同台词。"),(4000,6000,"相同台词。")])
            quality=json.loads((job/"deliverables/quality_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(quality["uncovered_source_characters"],0)
            (WORKSPACE/"test-results/cache_background_final.log").write_text(result.stdout,encoding="utf-8")

    def test_supplied_translation_can_switch_timing_mode_without_changing_text(self):
        with tempfile.TemporaryDirectory(prefix="retime_translation_",dir=WORKSPACE/"test-results") as directory:
            root=Path(directory).resolve()
            jp,axis,producer,config_path=[root/name for name in ("jp.srt","axis.ass","producer.py","config.json")]
            jp.write_text("1\n00:00:00,000 --> 00:00:04,000\n前半。後半。\n",encoding="utf-8")
            axis.write_text(AXIS,encoding="utf-8");producer.write_text(PRODUCER,encoding="utf-8")
            words={"schema_version":1,"source_cues":[{"index":1,"start_ms":0,"end_ms":4000,"text":"前半。後半。"}],
                "word_times":{"1":[{"char_start":0,"char_end":2,"start_ms":100,"end_ms":1800},
                                 {"char_start":3,"char_end":5,"start_ms":2300,"end_ms":3800}]}}
            jp.with_suffix(".word_times.json").write_text(json.dumps(words,ensure_ascii=False),encoding="utf-8")
            config=self._fixture_config(root,producer);config_path.write_text(json.dumps(config,ensure_ascii=False),encoding="utf-8")
            base=[sys.executable,str(ROOT/"tools/roast_pipeline.py"),"fixture","--config",str(config_path),"--video-file",str(VIDEO),
                "--timeline-file",str(axis),"--jp-file",str(jp),"--skip-audio","--no-jianying-ui","--skip-jianying-package"]
            def run(extra):
                return subprocess.run(base+extra,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding="utf-8",
                    creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0),check=False)
            first=run(["--job-name","screen","--timing-mode","screen"]);self.assertEqual(first.returncode,0,first.stdout)
            supplied=root/"work/screen/subs/zh_machine.srt";original=supplied.read_bytes()
            second=run(["--job-name","speech","--timing-mode","speech","--zh-file",str(supplied)])
            self.assertEqual(second.returncode,0,second.stdout)
            final=parse_subtitle(root/"work/speech/deliverables/final.ass")[2]
            self.assertEqual([(c.start,c.end,c.text) for c in final],[(100,1800,"第一部分。"),(2300,3800,"第二部分。")])
            self.assertEqual(supplied.read_bytes(),original)
            retained=root/"work/speech/subs/zh_machine.supplied.srt"
            self.assertEqual(retained.read_bytes(),original)
            ids=json.loads((root/"work/speech/subs/zh_machine.segments.json").read_text(encoding="utf-8"))
            self.assertEqual(ids["retiming"]["original_timestamps"][0]["end_ms"],2000)
            bad=root/"bad.srt";bad.write_bytes(original)
            bad_ids=json.loads(supplied.with_suffix(".segments.json").read_text(encoding="utf-8"))
            bad_ids["translations"][0]["cue_id"]="different-original-source"
            bad.with_suffix(".segments.json").write_text(json.dumps(bad_ids,ensure_ascii=False),encoding="utf-8")
            rejected=run(["--job-name","rejected","--timing-mode","speech","--zh-file",str(bad)])
            self.assertNotEqual(rejected.returncode,0,rejected.stdout)
            self.assertFalse((root/"work/rejected/deliverables/final.ass").exists())

    def test_independent_alignment_handles_coarse_asr_overshoot_but_final_stays_bounded(self):
        import wave
        with tempfile.TemporaryDirectory(prefix="coarse_asr_",dir=WORKSPACE/"test-results") as directory:
            root=Path(directory).resolve()
            jp,axis,producer,config_path,launcher=[root/name for name in ("jp.srt","axis.ass","producer.py","config.json","launcher.py")]
            jp.write_text("1\n00:00:00,000 --> 00:00:09,000\n前半。後半。\n",encoding="utf-8")
            axis.write_text(AXIS.replace("0:00:02.00","0:00:04.00").replace("0:00:04.00,Default,,0,0,0,,後半。","0:00:08.00,Default,,0,0,0,,後半。"),encoding="utf-8")
            producer.write_text(PRODUCER,encoding="utf-8")
            audio=root/"audio.wav"
            with wave.open(str(audio),"wb") as wav:
                wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(b"\x00\x00"*128000)
            model=root/"model";model.mkdir();clock=model/"clock.json";clock.write_text("[100,2000,6000,7800]",encoding="utf-8")
            manifest=root/"manifest.json";manifest.write_text('{"fixture":true}',encoding="utf-8")
            launcher.write_text(
                "import sys,json,wave\nfrom pathlib import Path\nsys.path.insert(0,"+repr(str(ROOT/"tools"))+")\n"+
                "import roast_pipeline\nfrom forced_alignment import map_prediction_units,file_sha256\nfrom subtitle_axis_align import parse_subtitle\nfrom task_state import atomic_write_json\n"+
                "def fixture(settings,config_base,audio,source,output):\n"+
                " with wave.open(str(audio),'rb') as wav:duration=round(wav.getnframes()*1000/wav.getframerate())\n"+
                " times=json.loads((Path(settings['model'])/'clock.json').read_text(encoding='utf-8'))\n"+
                " result=map_prediction_units(parse_subtitle(source)[2],[{'text':'前半'},{'text':'後半'}],times,times,duration)\n"+
                " result['audio_sha256']=file_sha256(audio);atomic_write_json(output,result);return result\n"+
                "roast_pipeline.run_external_alignment=fixture\nraise SystemExit(roast_pipeline.main())\n",encoding="utf-8")
            config=self._fixture_config(root,producer)
            config["forced_alignment"]={"enabled":True,"python":sys.executable,"model":str(model),"model_manifest":str(manifest)}
            config_path.write_text(json.dumps(config,ensure_ascii=False),encoding="utf-8")
            base=[sys.executable,str(launcher),"fixture","--config",str(config_path),"--video-file",str(VIDEO),
                "--audio-file",str(audio),"--timeline-file",str(axis),"--jp-file",str(jp),"--timing-mode","speech","--no-jianying-ui","--skip-jianying-package"]
            def run(name):
                return subprocess.run(base+["--job-name",name],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding="utf-8",
                    creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0),check=False)
            result=run("aligned");self.assertEqual(result.returncode,0,result.stdout)
            job=root/"work/aligned"
            self.assertEqual(parse_subtitle(job/"subs/jp_ocr.srt")[2][0].end,9000)
            self.assertEqual([(c.start,c.end) for c in parse_subtitle(job/"deliverables/final.ass")[2]],[(100,2000),(6000,7800)])
            validation=json.loads((job/"subs/jp_validation.json").read_text(encoding="utf-8"))
            self.assertTrue(validation["warnings"]);self.assertTrue(validation["details"]["coarse_asr_outside_media"])
            clock.write_text("[100,2000,8500,8900]",encoding="utf-8")
            rejected=run("unresolved");self.assertNotEqual(rejected.returncode,0,rejected.stdout)
            self.assertIn("SEGMENTATION failed",rejected.stdout)
            self.assertNotIn("[run] translate_zh:",rejected.stdout)
            self.assertFalse((root/"work/unresolved/deliverables/final.ass").exists())
            config["forced_alignment"]["enabled"]=False;config_path.write_text(json.dumps(config,ensure_ascii=False),encoding="utf-8")
            strict=run("native_only");self.assertNotEqual(strict.returncode,0,strict.stdout)

    def test_native_coarse_asr_can_use_observed_screen_times_but_not_publish_overshoot(self):
        with tempfile.TemporaryDirectory(prefix="screen_coarse_",dir=WORKSPACE/"test-results") as directory:
            root=Path(directory)
            jp,axis,producer,config_path=[root/name for name in ("jp.srt","axis.ass","producer.py","config.json")]
            jp.write_text("1\n00:00:00,000 --> 00:00:09,000\n前半。後半。\n",encoding="utf-8")
            axis.write_text(AXIS,encoding="utf-8")
            producer.write_text(PRODUCER,encoding="utf-8")
            config=self._fixture_config(root,producer)
            config["forced_alignment"]={"enabled":False}
            config_path.write_text(json.dumps(config,ensure_ascii=False),encoding="utf-8")
            base=[sys.executable,str(ROOT/"tools/roast_pipeline.py"),"fixture","--config",str(config_path),
                  "--video-file",str(VIDEO),"--timeline-file",str(axis),"--jp-file",str(jp),
                  "--skip-audio","--no-jianying-ui","--skip-jianying-package"]
            def run(name,mode):
                return subprocess.run(base+["--job-name",name,"--timing-mode",mode],
                    stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding="utf-8",
                    creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0),check=False)
            accepted=run("screen","mixed")
            self.assertEqual(accepted.returncode,0,accepted.stdout)
            job=root/"work/screen"
            self.assertEqual(parse_subtitle(job/"subs/jp_ocr.srt")[2][0].end,9000)
            self.assertEqual([(c.start,c.end) for c in parse_subtitle(job/"deliverables/final.ass")[2]],
                             [(0,2000),(2000,4000)])
            quality=json.loads((job/"deliverables/quality_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(quality["status"],"needs_review")
            self.assertTrue(quality["validation_warnings"]["jp"])
            rejected=run("speech","speech")
            self.assertNotEqual(rejected.returncode,0,rejected.stdout)
            self.assertIn("SEGMENTATION failed",rejected.stdout)
            self.assertNotIn("[run] translate_zh:",rejected.stdout)
            self.assertFalse((root/"work/speech/deliverables/final.ass").exists())


if __name__=="__main__":
    unittest.main()
