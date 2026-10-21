#!/usr/bin/env python3
"""Audit independent word predictions without replacing the original transcript."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
import sys
import time
import wave
from pathlib import Path
from subtitle_axis_align import Cue, parse_subtitle
from subtitle_segments import normalized_chars
from task_state import atomic_write_json

EVIDENCE_VERSION = 1
TIMESTAMP_KIND = "qwen3_independent_audio_coordinates"

def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def source_identity(speech: list[Cue]) -> list[dict]:
    return [{"index":cue.index,"start_ms":cue.start,"end_ms":cue.end,"text":cue.text} for cue in speech]

def map_prediction_units(speech: list[Cue], units: list[dict], raw_ms: list,
                         fixed_ms: list, audio_duration_ms: int) -> dict:
    """Only unchanged, observed timestamps enter word_times; keep all rejected evidence."""
    if audio_duration_ms <= 0 or len(raw_ms) != 2 * len(units) or len(fixed_ms) != len(raw_ms):
        raise ValueError("Prediction timestamps or audio duration are incomplete")
    if len({cue.index for cue in speech}) != len(speech):
        raise ValueError("Ambiguous original cue indexes")
    text = "\n".join(cue.text for cue in speech)
    normalized, offsets = normalized_chars(text)
    returned = "".join(normalized_chars(unit["text"])[0] for unit in units)
    if returned != normalized:
        raise ValueError("Forced alignment did not preserve the complete normalized original text")
    parents=[];cursor=0
    for cue in speech:
        parents.append((cue,cursor,cursor+len(cue.text)))
        cursor+=len(cue.text)+1
    raw_ms=[value if isinstance(value,str) or (isinstance(value,(int,float)) and math.isfinite(value)) else repr(value) for value in raw_ms]
    fixed_ms=[value if isinstance(value,str) or (isinstance(value,(int,float)) and math.isfinite(value)) else repr(value) for value in fixed_ms]
    invalid_neighbors=set()
    for pos in range(1,len(raw_ms)):
        prior,current=raw_ms[pos-1],raw_ms[pos]
        if (isinstance(prior,(int,float)) and isinstance(current,(int,float))
                and math.isfinite(prior) and math.isfinite(current) and current < prior):
            invalid_neighbors.update(((pos-1)//2,pos//2))
    mapped=[];words={str(cue.index):[] for cue in speech};unusable=[];cursor=0
    for index,unit in enumerate(units):
        fragment,_=normalized_chars(unit["text"])
        left=offsets[cursor][0] if fragment else None
        right=offsets[cursor+len(fragment)-1][1] if fragment else None
        cursor+=len(fragment)
        ranges=[]
        if fragment:
            for cue,parent_start,parent_end in parents:
                a,b=max(left,parent_start),min(right,parent_end)
                if b>a:ranges.append({"cue_index":cue.index,"char_start":a-parent_start,"char_end":b-parent_start})
        start,end=raw_ms[2*index:2*index+2];fixed_start,fixed_end=fixed_ms[2*index:2*index+2]
        flags=[]
        if not fragment:flags.append("NON_LEXICAL_MODEL_UNIT")
        if len(ranges)!=1:flags.append("WORD_CROSSES_SOURCE_CUES")
        finite=all(isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) for value in (start,end,fixed_start,fixed_end))
        if not finite:flags.append("NON_FINITE_TIMESTAMP")
        elif not 0 <= start < end <= audio_duration_ms:flags.append("INVALID_AUDIO_TIMESTAMP")
        if start!=fixed_start or end!=fixed_end:flags.append("SDK_TIMESTAMP_REPAIRED")
        if index in invalid_neighbors:flags.append("RAW_TIMESTAMP_INVERSION")
        item={"unit_index":index,"text":unit["text"],"source_ranges":ranges,
              "raw_start_ms":start,"raw_end_ms":end,"sdk_start_ms":fixed_start,"sdk_end_ms":fixed_end,
              "flags":flags}
        mapped.append(item)
        if flags:
            unusable.append(item)
            continue
        span=ranges[0]
        words[str(span["cue_index"])].append({"char_start":span["char_start"],"char_end":span["char_end"],
             "start_ms":round(start),"end_ms":round(end),"text":unit["text"],"unit_index":index,
             "raw_start_ms":start,"raw_end_ms":end,"sdk_timestamp_repaired":False})
    return {"schema_version":EVIDENCE_VERSION,"timestamp_kind":TIMESTAMP_KIND,
            "raw_prediction_audited":True,"audio_duration_ms":audio_duration_ms,
            "timing_evidence_status":("unusable" if not any(words.values()) else "usable_with_gaps") if unusable else "usable",
            "source_cues":source_identity(speech),"input_text":text,"word_times":words,
            "prediction_units":mapped,"unusable_tokens":unusable,
            "sdk_repaired_unit_count":sum("SDK_TIMESTAMP_REPAIRED" in item["flags"] for item in mapped),
            "normalized_source_preserved":True}

def validate_evidence(evidence: dict, speech: list[Cue], audio: Path | None = None) -> None:
    if (evidence.get("schema_version") != EVIDENCE_VERSION or evidence.get("timestamp_kind") != TIMESTAMP_KIND
            or evidence.get("raw_prediction_audited") is not True):
        raise ValueError("Unaudited or unsupported independent timestamp evidence")
    if evidence.get("source_cues") != source_identity(speech):
        raise ValueError("Independent alignment belongs to different original speech")
    units=evidence["prediction_units"]
    rebuilt=map_prediction_units(speech,[{"text":item["text"]} for item in units],
        [value for item in units for value in (item["raw_start_ms"],item["raw_end_ms"])],
        [value for item in units for value in (item["sdk_start_ms"],item["sdk_end_ms"])],
        evidence["audio_duration_ms"])
    for key in rebuilt:
        if evidence.get(key)!=rebuilt[key]:
            raise ValueError("Independent alignment evidence was altered: "+key)
    if audio is not None:
        if evidence.get("audio_sha256") != file_sha256(audio):
            raise ValueError("Independent alignment belongs to different audio")
        with wave.open(str(audio),"rb") as stream:
            duration=round(stream.getnframes()*1000/stream.getframerate())
        if duration!=evidence["audio_duration_ms"]:
            raise ValueError("Independent alignment audio duration differs")

def verify_model(model: Path, manifest_path: Path) -> dict:
    manifest=json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if manifest.get("repo")!="Qwen/Qwen3-ForcedAligner-0.6B" or not manifest.get("revision"):
        raise ValueError("Unsupported forced alignment model manifest")
    if not manifest.get("files"):raise ValueError("Model manifest is empty")
    verified=[]
    for item in manifest["files"]:
        path=(model/item["name"]).resolve()
        if not path.is_relative_to(model.resolve()) or not path.is_file():
            raise ValueError("Missing or unsafe model file")
        if path.stat().st_size!=item["bytes"] or file_sha256(path)!=item["sha256"]:
            raise ValueError("Model file identity differs: "+item["name"])
        verified.append(item["name"])
    if not {"config.json","model.safetensors"}.issubset(verified):
        raise ValueError("Model weights or configuration are not verified")
    return manifest

def align_local(audio: Path, source: Path, model_path: Path, model_manifest: Path, output: Path) -> dict:
    import importlib.metadata
    os.environ.update(HF_HOME=str(output.parent/"alignment_cache"),HF_HUB_OFFLINE="1",
                      TRANSFORMERS_OFFLINE="1",HF_HUB_DISABLE_IMPLICIT_TOKEN="1")
    speech=parse_subtitle(source)[2]
    with wave.open(str(audio),"rb") as stream:
        duration=round(stream.getnframes()*1000/stream.getframerate())
    manifest=verify_model(model_path,model_manifest)
    if importlib.metadata.version("qwen-asr")!="0.0.6":
        raise ValueError("Qwen SDK differs from the audited 0.0.6 implementation")
    import torch
    from qwen_asr import Qwen3ForcedAligner
    from qwen_asr.inference.qwen3_forced_aligner import Qwen3ForceAlignProcessor
    class AuditedProcessor(Qwen3ForceAlignProcessor):
        def __init__(self):
            super().__init__();self.captures=[]
        def parse_timestamp(self,word_list,timestamp):
            raw=timestamp.tolist()
            fixed=self.fix_timestamp(timestamp)
            self.captures.append({"words":list(word_list),"raw":raw,"fixed":list(fixed)})
            return super().parse_timestamp(word_list,timestamp)
    print("[alignment] loading fixed local model on CPU",flush=True);start=time.monotonic()
    model=Qwen3ForcedAligner.from_pretrained(str(model_path),device_map="cpu",dtype=torch.float32,local_files_only=True)
    processor=AuditedProcessor();model.aligner_processor=processor
    print("[alignment] aligning the complete supplied text and audio",flush=True)
    prediction=model.align(audio=str(audio),text="\n".join(cue.text for cue in speech),language="Japanese")[0]
    if len(processor.captures)!=1:raise ValueError("Unsupported SDK timestamp capture structure")
    capture=processor.captures[0]
    units=[{"text":item.text} for item in prediction]
    if capture["words"]!=[unit["text"] for unit in units]:
        raise ValueError("SDK output words differ from audited raw predictions")
    result=map_prediction_units(speech,units,capture["raw"],capture["fixed"],duration)
    result.update(audio_sha256=file_sha256(audio),source_file_sha256=file_sha256(source),
                  model_identity={"repo":manifest["repo"],"revision":manifest["revision"],
                                  "manifest_sha256":file_sha256(model_manifest)},
                  sdk_version="0.0.6",elapsed_seconds=round(time.monotonic()-start,2),
                  input_audio=str(audio.resolve()),input_source=str(source.resolve()))
    validate_evidence(result,speech,audio)
    atomic_write_json(output,result)
    print("[alignment] retained original text; accepted "+str(sum(len(value) for value in result["word_times"].values()))+
          " units, kept "+str(len(result["unusable_tokens"]))+" rejected units",flush=True)
    return result

def run_external_alignment(settings: dict, config_base: Path, audio: Path, source: Path, output: Path) -> dict:
    """Use a separate configured runtime; the existing VoiceTransl environment is untouched."""
    import subprocess
    def configured(name: str) -> Path:
        value=settings.get(name)
        if not isinstance(value,str) or not value.strip():
            raise ValueError("Independent word alignment requires "+name)
        path=Path(value)
        return path.resolve() if path.is_absolute() else (config_base/path).resolve()
    python,model,manifest=[configured(name) for name in ("python","model","model_manifest")]
    if not python.is_file():raise ValueError("Independent alignment Python runtime is missing")
    if not audio.is_file():raise ValueError("Independent word alignment requires the actual WAV audio")
    worker=Path(__file__).resolve()
    command=[str(python),"-B","-X","utf8",str(worker),"--audio",str(audio),"--input",str(source),
             "--model",str(model),"--model-manifest",str(manifest),"--output",str(output)]
    output.parent.mkdir(parents=True,exist_ok=True)
    log=output.with_suffix(".log")
    with log.open("w",encoding="utf-8") as stream:
        process=subprocess.Popen(command,stdout=stream,stderr=subprocess.STDOUT,
                                 creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        code=process.wait()
    if code:
        raise RuntimeError("Independent alignment failed with exit code "+str(code)+": "+str(log))
    evidence=json.loads(output.read_text(encoding="utf-8-sig"))
    validate_evidence(evidence,parse_subtitle(source)[2],audio)
    return evidence


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio",type=Path,required=True)
    parser.add_argument("--input",type=Path,required=True)
    parser.add_argument("--model",type=Path,required=True)
    parser.add_argument("--model-manifest",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    try:
        align_local(args.audio,args.input,args.model,args.model_manifest,args.output)
        return 0
    except Exception as exc:
        atomic_write_json(args.output.with_suffix(".failure.json"),{
            "status":"failed","error":str(exc),"audio":str(args.audio),"source":str(args.input),
            "original_transcript_unchanged":True,"published_output":False})
        print("[alignment] failed: "+str(exc),file=sys.stderr,flush=True)
        return 1

if __name__=="__main__":
    raise SystemExit(main())
