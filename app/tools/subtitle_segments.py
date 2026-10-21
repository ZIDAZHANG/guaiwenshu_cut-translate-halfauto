#!/usr/bin/env python3
"""Keep visual pages, original speech ranges and translation identities together."""
from __future__ import annotations
import argparse
import hashlib
import json
import unicodedata
from pathlib import Path
from subtitle_axis_align import Cue, fmt_srt_time, overlap_ms, parse_subtitle
from task_state import atomic_write_json, atomic_write_text

SEGMENTATION_VERSION = 8


def stable_id(kind: str, value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return kind + "_" + hashlib.sha256(encoded).hexdigest()[:24]


def normalized_chars(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Compare normalized text while retaining offsets into the original string."""
    chars, offsets = [], []
    start = 0
    while start < len(text):
        end = start + 1
        while end < len(text) and (unicodedata.combining(text[end]) or text[end] in "\uff9e\uff9f"):
            end += 1
        for char in unicodedata.normalize("NFKC", text[start:end]).casefold():
            if char.isalnum():
                # OCR often uses katakana where native speech transcription
                # uses hiragana. Compare the same kana sound while retaining
                # the exact original glyph and character range for output.
                if "ァ" <= char <= "ヶ":
                    char = chr(ord(char) - 0x60)
                chars.append(char)
                offsets.append((start, end))
        start = end
    return "".join(chars), offsets


def is_visual_text(text: str) -> bool:
    return bool(text.strip()) and not text.startswith("Subtitle_Dialog_")


def range_timing(parent: dict, start: int, end: int) -> tuple[int, int, bool, str]:
    words = [word for word in parent.get("words", [])
             if word["char_start"] < end and word["char_end"] > start]
    if words:
        covered = {position for word in words for position in range(word["char_start"], word["char_end"])}
        _, offsets = normalized_chars(parent["text"])
        required = {position for left, right in offsets if left < end and right > start
                    for position in range(max(start, left), min(end, right))}
        boundaries_observed = all(start <= word["char_start"] and word["char_end"] <= end for word in words)
        complete = required.issubset(covered) and boundaries_observed
        independent = parent.get("word_timing_kind") == "qwen3_independent_audio_coordinates"
        if complete:
            return min(word["start_ms"] for word in words), max(word["end_ms"] for word in words), True, "speech_forced_words" if independent else "speech_words"
        if independent and required and boundaries_observed and min(required) in covered and max(required) in covered:
            # Only the observed outer word edges determine this range. Missing
            # interior times stay missing; no character-level interpolation.
            return min(word["start_ms"] for word in words), max(word["end_ms"] for word in words), True, "speech_forced_boundaries"
    complete = start == 0 and end == len(parent["text"])
    return parent["start_ms"], parent["end_ms"], complete, "speech_parent" if complete else "speech_parent_unresolved"


def align_page_text(parents: list[dict], pages: list[dict], *, _merge_copies: bool = True) -> dict:
    """Reconcile the whole ordered text, retaining substitutions and omissions.

    Unit edit distance chooses a monotone correspondence. Existing word clocks,
    or the original parent clock when absent, only break equal text-cost ties;
    they do not shift, interpolate, or invent boundaries.
    Every non-exact page remains a review candidate.
    """
    if _merge_copies and any(
            left["end_ms"] == right["start_ms"]
            and normalized_chars(left["text"])[0] == normalized_chars(right["text"])[0]
            for left, right in zip(pages, pages[1:])):
        working = [page | {"member_ids": [page["id"]]} for page in pages]
        best = align_page_text(parents, working, _merge_copies=False)
        trials, index = [], 1
        while index < len(working):
            left, right = working[index - 1:index + 1]
            if (left["end_ms"] != right["start_ms"]
                    or normalized_chars(left["text"])[0] != normalized_chars(right["text"])[0]):
                index += 1
                continue
            merged = left | {"end_ms": right["end_ms"],
                             "member_ids": left["member_ids"] + right["member_ids"]}
            proposal = working[:index - 1] + [merged] + working[index + 1:]
            candidate = align_page_text(parents, proposal, _merge_copies=False)
            before, after = best["evidence"]["edit_distance"], candidate["evidence"]["edit_distance"]
            trials.append({"visual_ids": merged["member_ids"], "original_edit_distance": before,
                           "merged_edit_distance": after, "merged": after < before})
            # An actual spoken repeat makes the unmerged text cheaper. An equal
            # text cost is uncertain and never authorizes deleting a repeat.
            if after < before:
                working, best = proposal, candidate
            else:
                index += 1
        groups = {page["id"]: page["member_ids"] for page in working if len(page["member_ids"]) > 1}
        best["merged_visual_ids"] = groups
        best["continuation_of"] = {member: first for first, members in groups.items() for member in members[1:]}
        best["evidence"]["duplicate_page_trials"] = trials
        best["evidence"]["visual_groups"] = [
            {"visual_ids": page["member_ids"], "start_ms": page["start_ms"], "end_ms": page["end_ms"]}
            for page in working if len(page["member_ids"]) > 1]
        return best

    source, target = [], []
    for parent in parents:
        text, offsets = normalized_chars(parent["text"])
        source.extend((char, parent, left, right) for char, (left, right) in zip(text, offsets))
    for page in pages:
        text, offsets = normalized_chars(page["text"])
        target.extend((char, page, left, right) for char, (left, right) in zip(text, offsets))
    clocks = [
        [(word["start_ms"], word["end_ms"]) for word in parent["words"]
         if word["char_start"] < right and word["char_end"] > left]
        for _, parent, left, right in source
    ]
    eligible = {
        page["id"]: {
            parent["id"] for parent in parents
            if parent.get("candidate_start_ms", parent["start_ms"]) < page["end_ms"]
            and parent.get("candidate_end_ms", parent["end_ms"]) > page["start_ms"]
        } for page in pages
    }
    n, m = len(source), len(target)
    word_clock_characters = sum(bool(clock) for clock in clocks)
    clocks = [clock or [(item[1]["start_ms"], item[1]["end_ms"])]
              for item, clock in zip(source, clocks)]
    # The sum bounds every possible temporal tie cost for this actual input.
    # Text edit cost always dominates, without an arbitrary similarity cutoff.
    weight = 1 + sum(end - start for clock in clocks for start, end in clock)
    penalties = {
        page["id"]: [
            sum(end - start - max(0, min(end, page["end_ms"]) - max(start, page["start_ms"]))
                for start, end in clock)
            for clock in clocks
        ] for page in pages
    }
    trace = bytearray((n + 1) * (m + 1))
    previous = [j * weight for j in range(n + 1)]
    for j in range(1, n + 1):
        trace[j] = 2
    for i, (char, page, _, _) in enumerate(target, 1):
        row = [i * weight] + [0] * n
        trace[i * (n + 1)] = 4
        allowed = eligible[page["id"]]
        for j, (other, parent, _, _) in enumerate(source, 1):
            best, mask = previous[j] + weight, 4
            insertion = row[j - 1] + weight
            if insertion < best:
                best, mask = insertion, 2
            elif insertion == best:
                mask |= 2
            if parent["id"] in allowed:
                diagonal = previous[j - 1] + (char != other) * weight + penalties[page["id"]][j - 1]
                if diagonal < best:
                    best, mask = diagonal, 1
                elif diagonal == best:
                    mask |= 1
            row[j] = best
            trace[i * (n + 1) + j] = mask
        previous = row

    owners = [None] * n
    operations, anchors, ambiguous = [], set(), set()
    i, j = m, n
    while i or j:
        mask = trace[i * (n + 1) + j]
        page = target[i - 1][1] if i else None
        parent = source[j - 1][1] if j else None
        operation = {}
        if mask not in {1, 2, 4} and page:
            ambiguous.add(page["id"])
        if mask & 1:
            owners[j - 1] = page["id"]
            kind = "equal" if target[i - 1][0] == source[j - 1][0] else "replace"
            if kind == "equal":
                anchors.add((parent["id"], page["id"]))
            operation = {"operation": kind,
                         "source_range": {"parent_id": parent["id"], "char_start": source[j - 1][2],
                                          "char_end": source[j - 1][3]},
                         "visual_range": {"visual_id": page["id"], "char_start": target[i - 1][2],
                                          "char_end": target[i - 1][3]}}
            i -= 1
            j -= 1
        elif mask & 2:
            operation = {"operation": "source_only",
                         "source_range": {"parent_id": parent["id"], "char_start": source[j - 1][2],
                                          "char_end": source[j - 1][3]}}
            j -= 1
        else:
            operation = {"operation": "visual_only",
                         "visual_range": {"visual_id": page["id"], "char_start": target[i - 1][2],
                                          "char_end": target[i - 1][3]}}
            i -= 1
        operations.append(operation)
    operations.reverse()
    # A shared caption character from one utterance does not support stealing
    # a substituted name prefix from the next utterance. Require the anchor
    # within each actual utterance/page relation, then preserve its other text.
    owners = [owner if (source[index][1]["id"], owner) in anchors else None
              for index, owner in enumerate(owners)]
    for parent in parents:
        positions = [j for j, item in enumerate(source) if item[1]["id"] == parent["id"]]
        observed = {owners[j] for j in positions if owners[j] is not None}
        if len(observed) == 1:
            owner = next(iter(observed))
            for j in positions:
                if owners[j] is None:
                    owners[j] = owner
        else:
            # Deleted characters inside the same observed page stay with it.
            for first, last in zip(
                    [j for j in positions if owners[j] is not None],
                    [j for j in positions if owners[j] is not None][1:]):
                if owners[first] == owners[last]:
                    for j in range(first + 1, last):
                        owners[j] = owners[first]
    mapping = {page["id"]: [] for page in pages}
    for (_, parent, left, right), owner in zip(source, owners):
        if owner is not None:
            mapping[owner].append((parent, left, right))
    return {"mapping": mapping, "ambiguous_pages": ambiguous,
            "evidence": {"method": "monotone_edit_with_timing_ties",
                         "edit_distance": previous[-1] // weight,
                         "temporal_tie_cost": previous[-1] % weight,
                         "word_clock_characters": word_clock_characters,
                         "coarse_clock_characters": n - word_clock_characters,
                         "source_normalized_characters": n, "visual_normalized_characters": m,
                         "ambiguous_visual_ids": sorted(ambiguous), "operations": operations}}


def build_segments(speech: list[Cue], visual: list[Cue] | None = None,
                   word_times: dict | None = None, mode: str = "mixed", *,
                   independent_evidence: dict | None = None) -> dict:
    if mode not in {"mixed", "screen", "speech"}:
        raise ValueError(f"Unknown timing mode: {mode}")
    if independent_evidence is not None:
        from forced_alignment import validate_evidence
        validate_evidence(independent_evidence, speech)
        word_times = independent_evidence["word_times"]
    parents = [{"id": stable_id("speech", [ordinal, cue.start, cue.end, cue.text]),
                "index": cue.index, "start_ms": cue.start, "end_ms": cue.end,
                "text": cue.text, "words": (word_times or {}).get(str(cue.index), [])}
               for ordinal, cue in enumerate(speech)]
    for parent in parents:
        if independent_evidence is not None:
            parent["word_timing_kind"] = independent_evidence["timestamp_kind"]
            parent["candidate_start_ms"] = min([parent["start_ms"]] + [word["start_ms"] for word in parent["words"]])
            parent["candidate_end_ms"] = max([parent["end_ms"]] + [word["end_ms"] for word in parent["words"]])
        for word in parent["words"]:
            clock_start = 0 if independent_evidence is not None else parent["start_ms"]
            clock_end = independent_evidence["audio_duration_ms"] if independent_evidence is not None else parent["end_ms"]
            if not (0 <= word["char_start"] < word["char_end"] <= len(parent["text"])
                    and clock_start <= word["start_ms"] < word["end_ms"] <= clock_end):
                raise ValueError("Invalid character timestamp evidence")
    pages = [{"id": stable_id("visual", [ordinal, cue.start, cue.end, cue.text]),
              "index": cue.index, "start_ms": cue.start, "end_ms": cue.end,
              "text": cue.text} for ordinal, cue in enumerate(visual or []) if is_visual_text(cue.text)]
    used = {parent["id"]: set() for parent in parents}
    by_id = {parent["id"]: parent for parent in parents}
    segments, unresolved = [], []
    text_alignment = None

    def spans_for(mapping: list[tuple[dict, int, int]], start: int, end: int) -> list[dict]:
        spans = []
        for parent, raw_start, raw_end in mapping[start:end]:
            if spans and spans[-1]["parent_id"] == parent["id"]:
                spans[-1]["char_end"] = raw_end
            else:
                spans.append({"parent_id": parent["id"], "char_start": raw_start, "char_end": raw_end})
        for span in spans:
            parent = by_id[span["parent_id"]]
            # Assign punctuation/spacing at the next boundary to the preceding words.
            _, offsets = normalized_chars(parent["text"])
            next_starts = [offset[0] for offset in offsets if offset[0] >= span["char_end"]]
            span["char_end"] = next_starts[0] if next_starts else len(parent["text"])
            if offsets and span["char_start"] == offsets[0][0]:
                span["char_start"] = 0
        return spans

    def occupied(spans: list[dict]) -> bool:
        return any(used[span["parent_id"]].intersection(range(span["char_start"], span["char_end"]))
                   for span in spans)

    def add_segment(spans: list[dict], page: dict | None, flags: list[str]) -> dict:
        source = "\n".join(by_id[span["parent_id"]]["text"][span["char_start"]:span["char_end"]]
                           for span in spans) if spans else page["text"]
        timings = [range_timing(by_id[span["parent_id"]], span["char_start"], span["char_end"])
                   for span in spans]
        speech_start = min(item[0] for item in timings) if timings else None
        speech_end = max(item[1] for item in timings) if timings else None
        if any(item[3] == "speech_forced_boundaries" for item in timings):
            flags += ["WORD_TIMING_GAPS", "CHECK"]
        if page and mode != "speech":
            start, end = page["start_ms"], page["end_ms"]
            timing_source = "visual_page"
        elif timings:
            start, end = speech_start, speech_end
            sources = {item[3] for item in timings}
            timing_source = next(iter(sources)) if len(sources) == 1 else "speech_mixed_evidence"
            if not all(item[2] for item in timings):
                timing_source = "speech_parent_unresolved"
            if timing_source.endswith("unresolved"):
                flags += ["WORD_TIMING_MISSING", "CHECK"]
        else:
            start, end = page["start_ms"], page["end_ms"]
            timing_source = "unvoiced_visual"
        item = {"id": stable_id("cue", {"ranges": spans, "visual": page["id"] if page else None}),
                "start_ms": start, "end_ms": end, "text": source, "source_ranges": spans,
                "visual_ids": [page["id"]] if page else [], "visual_text": page["text"] if page else "",
                "speech_start_ms": speech_start, "speech_end_ms": speech_end,
                "timing_source": timing_source, "flags": sorted(set(flags)),
                "context": [{"parent_id": span["parent_id"], "text": by_id[span["parent_id"]]["text"]}
                            for span in spans]}
        segments.append(item)
        for span in spans:
            used[span["parent_id"]].update(range(span["char_start"], span["char_end"]))
        return item

    for page in pages:
        continuation = text_alignment.get("continuation_of", {}).get(page["id"]) if text_alignment else None
        if continuation and any(page["id"] in item["visual_ids"] for item in segments):
            continue
        target, _ = normalized_chars(page["text"])
        candidates = [parent for parent in parents
                      if parent.get("candidate_start_ms", parent["start_ms"]) < page["end_ms"] and parent.get("candidate_end_ms", parent["end_ms"]) > page["start_ms"]]
        joined, mapping = "", []
        for parent in candidates:
            normalized, offsets = normalized_chars(parent["text"])
            joined += normalized
            mapping += [(parent, start, end) for start, end in offsets]
        matches = []
        if target:
            position = joined.find(target)
            while position >= 0:
                spans = spans_for(mapping, position, position + len(target))
                timings = [range_timing(by_id[span["parent_id"]], span["char_start"], span["char_end"])
                           for span in spans]
                temporal = sum(overlap_ms(Cue(page["start_ms"], page["end_ms"], ""),
                                          Cue(start, end, "")) for start, end, _, _ in timings)
                matches.append((spans, temporal, position))
                position = joined.find(target, position + 1)
        available = [match for match in matches if not occupied(match[0])]
        if available:
            spans, _, _ = max(available, key=lambda item: (item[1], -item[2]))
            flags = ["AMBIGUOUS_TEXT_OCCURRENCE", "CHECK"] if len(available) > 1 and not all(
                range_timing(by_id[span["parent_id"]], span["char_start"], span["char_end"])[2]
                for candidate, _, _ in available for span in candidate) else []
            add_segment(spans, page, flags)
            continue
        if segments and matches:
            previous = segments[-1]
            same_text = normalized_chars(previous["visual_text"])[0] == target
            same_spans = any(match[0] == previous["source_ranges"] for match in matches)
            if same_text and same_spans and previous["end_ms"] == page["start_ms"]:
                if mode != "speech":
                    previous["end_ms"] = page["end_ms"]
                previous["visual_ids"].append(page["id"])
                continue
        # Align all pages together so a locally repeated substring cannot steal
        # the next page, and substitutions do not create orphan name fragments.
        if text_alignment is None:
            text_alignment = align_page_text(parents, pages)
        page_mapping = text_alignment["mapping"].get(page["id"], [])
        if page_mapping:
            spans = spans_for(page_mapping, 0, len(page_mapping))
            if not occupied(spans):
                flags = ["TEXT_CONFLICT", "CHECK"]
                if page["id"] in text_alignment["ambiguous_pages"]:
                    flags.append("TEXT_ALIGNMENT_AMBIGUOUS")
                item = add_segment(spans, page, flags)
                members = text_alignment.get("merged_visual_ids", {}).get(page["id"], [page["id"]])
                item["visual_ids"] = members
                if mode != "speech":
                    item["end_ms"] = next(event["end_ms"] for event in pages if event["id"] == members[-1])
                continue
        if not candidates:
            previous = segments[-1] if segments else None
            if (previous and not previous["source_ranges"] and previous["end_ms"] == page["start_ms"]
                    and unicodedata.normalize("NFKC", previous["visual_text"]).strip().casefold()
                    == unicodedata.normalize("NFKC", page["text"]).strip().casefold()):
                previous["end_ms"] = page["end_ms"]
                previous["visual_ids"].append(page["id"])
                continue
            add_segment([], page, ["VISUAL_ONLY", "CHECK"])
        else:
            unresolved.append(page | {"flags": ["VISUAL_MAPPING_UNRESOLVED", "CHECK"]})

    for parent in parents:
        start = 0
        while start < len(parent["text"]):
            if start in used[parent["id"]]:
                start += 1
                continue
            end = start + 1
            while end < len(parent["text"]) and end not in used[parent["id"]]:
                end += 1
            flags = ["UNMAPPED_SPEECH", "CHECK"] if pages else (["VISUAL_TEXT_MISSING", "CHECK"] if visual else [])
            span = {"parent_id": parent["id"], "char_start": start, "char_end": end}
            if parent["text"][start:end].strip():
                add_segment([span], None, flags)
            else:
                used[parent["id"]].update(range(start, end))
            start = end

    if mode == "speech":
        unresolved_parents = {
            span["parent_id"] for item in segments if item["timing_source"] == "speech_parent_unresolved"
            for span in item["source_ranges"]
        }
        # A page can contain several parents. Keep every connected sibling on
        # the same original utterance clock when one split boundary is missing.
        previous = None
        while previous != unresolved_parents:
            previous = set(unresolved_parents)
            for item in segments:
                ids = {span["parent_id"] for span in item["source_ranges"]}
                if ids.intersection(unresolved_parents):
                    unresolved_parents.update(ids)
        for item in segments:
            ids = {span["parent_id"] for span in item["source_ranges"]}
            if ids.intersection(unresolved_parents):
                item["start_ms"] = min(by_id[key]["start_ms"] for key in ids)
                item["end_ms"] = max(by_id[key]["end_ms"] for key in ids)
                item["timing_source"] = "speech_parent_unresolved"
                item["flags"] = sorted(set(item["flags"] + ["WORD_TIMING_MISSING", "CHECK"]))
        parent_order = {parent["id"]: index for index, parent in enumerate(parents)}
        page_order = {page["id"]: index for index, page in enumerate(pages)}
        def source_order(item):
            if item["source_ranges"]:
                return tuple((parent_order[span["parent_id"]], span["char_start"])
                             for span in item["source_ranges"])
            return ((-1, min(page_order[key] for key in item["visual_ids"])),)
        segments.sort(key=lambda item: (item["start_ms"], source_order(item), item["end_ms"], item["id"]))
    else:
        segments.sort(key=lambda item: (item["start_ms"], item["end_ms"], item["id"]))
    for index, item in enumerate(segments, 1):
        item["index"] = index
    coverage = [{"parent_id": parent["id"], "total_chars": len(parent["text"]),
                 "covered_chars": len(used[parent["id"]]),
                 "missing_chars": sorted(set(range(len(parent["text"]))) - used[parent["id"]])}
                for parent in parents]
    result = {"schema_version": 1, "segmentation_version": SEGMENTATION_VERSION, "mode": mode,
            "parents": parents, "visual_events": pages, "segments": segments,
            "unresolved_visual_events": unresolved, "coverage": coverage,
            "needs_review": sum("CHECK" in item["flags"] for item in segments) + len(unresolved)}
    if text_alignment is not None:
        result["text_alignment"] = text_alignment["evidence"]
    if independent_evidence is not None:
        result["independent_alignment"] = independent_evidence
    return result



def load_whisper_word_times(path: Path, speech: list[Cue]) -> dict:
    """Reassemble UTF-8 fragments emitted as separate byte-level BPE tokens."""
    raw = json.loads(path.read_bytes().decode("utf-8", errors="surrogateescape"))
    transcription = raw["transcription"]
    if len(transcription) != len(speech):
        raise ValueError("Whisper JSON and SRT have different transcription counts")
    word_times, unusable, coordinate_issues = {}, [], []
    for cue, utterance in zip(speech, transcription):
        original = utterance["text"]
        normalized, original_offsets = normalized_chars(original)
        cue_normalized, cue_offsets = normalized_chars(cue.text)
        if normalized != cue_normalized:
            raise ValueError(f"Whisper JSON text differs from SRT cue {cue.index}")
        lexical = [token for token in utterance["tokens"]
                   if not (token["text"].startswith("[_") and token["text"].endswith("]"))]
        combined = b"".join(token["text"].encode("utf-8", errors="surrogateescape") for token in lexical)
        reconstructed = combined.decode("utf-8")
        if reconstructed != original:
            raise ValueError(f"Token bytes differ from Whisper text in cue {cue.index}")
        pending, group, cursor, entries = b"", [], 0, []
        for token in lexical:
            pending += token["text"].encode("utf-8", errors="surrogateescape")
            group.append(token)
            try:
                text = pending.decode("utf-8")
            except UnicodeDecodeError:
                continue
            positions = [index for index, (left, right) in enumerate(original_offsets)
                         if left < cursor + len(text) and right > cursor]
            start_ms = min(item["offsets"]["from"] for item in group)
            end_ms = max(item["offsets"]["to"] for item in group)
            if positions and end_ms > start_ms:
                entries.append({"char_start": cue_offsets[positions[0]][0],
                                "char_end": cue_offsets[positions[-1]][1],
                                "start_ms": start_ms, "end_ms": end_ms, "text": text,
                                "token_ids": [item["id"] for item in group],
                                "dtw_ms": [item["t_dtw"] * 10 for item in group if item.get("t_dtw", -1) >= 0]})
            elif positions:
                unusable.append({"cue_index": cue.index, "text": text, "start_ms": start_ms, "end_ms": end_ms})
            cursor += len(text)
            pending, group = b"", []
        if pending:
            raise ValueError("Incomplete UTF-8 token bytes")
        for entry in entries:
            if not cue.start <= entry["start_ms"] < entry["end_ms"] <= cue.end:
                coordinate_issues.append({"cue_index": cue.index,
                    "parent_start_ms": cue.start, "parent_end_ms": cue.end, **entry})
        word_times[str(cue.index)] = entries
    if coordinate_issues:
        # VAD may restore utterance times but leave tokens in compacted-audio coordinates.
        # Preserve every native value; do not infer or interpolate an offset correction.
        for cue_index, entries in word_times.items():
            unusable.extend({"cue_index": int(cue_index), "reason": "NATIVE_TIME_COORDINATES_CONFLICT", **entry}
                            for entry in entries)
            word_times[cue_index] = []
    return {"schema_version": 2, "timestamp_kind": "whisper_cpp_token_offsets",
            "timing_evidence_status": "unusable" if coordinate_issues else "usable",
            "coordinate_issues": coordinate_issues,
             "word_times": word_times, "unusable_tokens": unusable,
            "source_cues": [{"index": cue.index, "start_ms": cue.start, "end_ms": cue.end, "text": cue.text}
                            for cue in speech],
            "source_json": str(path.resolve())}



def audit_segment_records(records: dict) -> None:
    parents = {parent["id"]: parent for parent in records["parents"]}
    if len(parents) != len(records["parents"]):
        raise ValueError("Duplicate speech identities")
    independent = records.get("independent_alignment")
    if independent is not None:
        from forced_alignment import validate_evidence
        validate_evidence(independent, [Cue(parent["start_ms"], parent["end_ms"], parent["text"], parent["index"])
                                       for parent in records["parents"]])
        for parent in records["parents"]:
            if parent["words"] != independent["word_times"].get(str(parent["index"]), []):
                raise ValueError("Canonical words differ from audited independent predictions")
    assigned = {key: set() for key in parents}
    identities = set()
    for index, item in enumerate(records["segments"], 1):
        if item["index"] != index or item["id"] in identities or not 0 <= item["start_ms"] < item["end_ms"]:
            raise ValueError("Invalid canonical segment identity or timestamps")
        identities.add(item["id"])
        source = []
        for span in item["source_ranges"]:
            parent = parents[span["parent_id"]]
            start, end = span["char_start"], span["char_end"]
            if not 0 <= start < end <= len(parent["text"]):
                raise ValueError("Source range is outside its parent utterance")
            positions = set(range(start, end))
            if positions.intersection(assigned[parent["id"]]):
                raise ValueError("Original source characters were assigned to multiple segments")
            assigned[parent["id"]].update(positions)
            source.append(parent["text"][start:end])
        if source and "\n".join(source) != item["text"]:
            raise ValueError("Canonical segment text differs from its original source ranges")
    for parent in parents.values():
        missing = [position for position in range(len(parent["text"]))
                   if position not in assigned[parent["id"]] and not parent["text"][position].isspace()]
        if missing:
            raise ValueError(f"Original source characters are missing: {parent['id']} {missing}")


def render_source_srt(records: dict) -> str:
    return "\n".join(f'{item["index"]}\n{fmt_srt_time(item["start_ms"])} --> {fmt_srt_time(item["end_ms"])}\n'
                     f'{item["text"]}\n' for item in records["segments"])



def publish_segment_output(records: dict, translations: list[Cue], output: Path, report: Path,
                           template: Path | None = None, translation_metadata: dict | None = None,
                           bilingual: bool = False, mark_low: bool = False) -> None:
    from subtitle_axis_align import ALIGNMENT_VERSION, ass_escape, fmt_ass_time, parse_ass, write_report
    audit_segment_records(records)
    items = records["segments"]
    if len(items) != len(translations) or any(
        item["index"] != cue.index or item["start_ms"] != cue.start or item["end_ms"] != cue.end
        for item, cue in zip(items, translations)
    ):
        raise ValueError("Translations do not match every canonical segment and its timestamps")
    identities = (translation_metadata or {}).get("translations", [])
    if identities:
        if len(identities) != len(items) or any(
            identity["cue_id"] != item["id"] or identity["text"] != cue.text or
            identity["index"] != item["index"] or identity["start_ms"] != item["start_ms"] or
            identity["end_ms"] != item["end_ms"]
            for identity, item, cue in zip(identities, items, translations)
        ):
            raise ValueError("Translation identity evidence is stale or belongs to different segments")
    parents = {parent["id"]: parent for parent in records["parents"]}
    usages = {}
    for item in items:
        for parent_id in {span["parent_id"] for span in item["source_ranges"]}:
            usages[parent_id] = usages.get(parent_id, 0) + 1
    rows, texts = [], []
    for position, (item, translation) in enumerate(zip(items, translations)):
        flags = set(item["flags"])
        if identities:
            flags.update(identities[position].get("flags", []))
        if not identities:
            flags.update({"TRANSLATION_IDENTITY_UNVERIFIED", "CHECK"})
        parent_ids = list(dict.fromkeys(span["parent_id"] for span in item["source_ranges"]))
        if any(usages[parent_id] > 1 for parent_id in parent_ids):
            flags.add("SOURCE_SPLIT_JP")
        text = translation.text
        if bilingual:
            text += "\n" + item["text"]
        if mark_low and "CHECK" in flags:
            text = "[CHECK] " + text
        texts.append(text)
        rows.append({
            "line": item["index"], "start": fmt_srt_time(item["start_ms"]),
            "end": fmt_srt_time(item["end_ms"]), "confidence": "low" if "CHECK" in flags else "high",
            "flags": ",".join(sorted(flags)), "source": "canonical_segment", "output_text": text,
            "alignment_version": ALIGNMENT_VERSION, "zh_indices": str(translation.index),
            "jp_indices": ",".join(str(parents[key]["index"]) for key in parent_ids),
            "zh_overlap_ms": max(0, min(item["end_ms"], translation.end) - max(item["start_ms"], translation.start)),
            "jp_overlap_ms": sum(max(0, min(item["end_ms"], parents[key]["end_ms"]) -
                                     max(item["start_ms"], parents[key]["start_ms"])) for key in parent_ids),
            "jp_text": item["text"], "cue_id": item["id"],
            "source_ranges": json.dumps([span | {"parent_index": parents[span["parent_id"]]["index"]}
                                         for span in item["source_ranges"]], ensure_ascii=False),
            "visual_ids": ",".join(item["visual_ids"]), "timing_source": item["timing_source"],
        })
    if output.suffix.lower() in {".ass", ".ssa"}:
        ass = parse_ass(template) if template and template.suffix.lower() in {".ass", ".ssa"} else None
        if ass and ass.format_fields:
            fields = ass.format_fields
            header = [line for line in ass.lines if not line.strip().lower().startswith("dialogue:")]
            if not any(line.strip().lower() == "[events]" for line in header):
                raise ValueError("ASS template has no Events section")
        else:
            fields = ["Layer", "Start", "End", "Style", "Name", "MarginL", "MarginR", "MarginV", "Effect", "Text"]
            header = ["[Script Info]", "ScriptType: v4.00+", "PlayResX: 1920", "PlayResY: 1080", "",
                      "[V4+ Styles]",
                      "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
                      "Style: Default,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,20,1",
                      "", "[Events]", "Format: " + ", ".join(fields)]
        field_map = {field.lower(): index for index, field in enumerate(fields)}
        if not {"start", "end", "text"}.issubset(field_map):
            raise ValueError("ASS template lacks required fields")
        events = []
        for item, text in zip(items, texts):
            template_cue = max(ass.cues, key=lambda cue: overlap_ms(
                Cue(item["start_ms"], item["end_ms"], ""), cue), default=None) if ass else None
            parts = list(template_cue.raw[1]) if template_cue and template_cue.raw else [""] * len(fields)
            for field, value in (("layer", "0"), ("style", "Default"),
                                 ("marginl", "0"), ("marginr", "0"), ("marginv", "0")):
                if field in field_map and not parts[field_map[field]]:
                    parts[field_map[field]] = value
            parts[field_map["start"]] = fmt_ass_time(item["start_ms"])
            parts[field_map["end"]] = fmt_ass_time(item["end_ms"])
            parts[field_map["text"]] = ass_escape(text)
            events.append("Dialogue: " + ",".join(parts))
        atomic_write_text(output, "\ufeff" + "\n".join(header + events) + "\n")
    else:
        atomic_write_text(output, "\ufeff" + "\n".join(
            f'{item["index"]}\n{fmt_srt_time(item["start_ms"])} --> {fmt_srt_time(item["end_ms"])}\n{text}\n'
            for item, text in zip(items, texts)))
    write_report(report, rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jp", required=True, type=Path)
    parser.add_argument("--timeline", type=Path)
    parser.add_argument("--word-times", type=Path)
    parser.add_argument("--mode", choices=("mixed", "screen", "speech"), default="mixed")
    parser.add_argument("--records", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    speech = parse_subtitle(args.jp)[2]
    visual = parse_subtitle(args.timeline)[2] if args.timeline else None
    words = json.loads(args.word_times.read_text(encoding="utf-8-sig")) if args.word_times else None
    records = build_segments(speech, visual, words.get("word_times", words) if words else None, args.mode)
    audit_segment_records(records)
    atomic_write_json(args.records, records)
    atomic_write_text(args.output, render_source_srt(records))
    print(f'Segmentation: {len(records["segments"])} cues; {records["needs_review"]} unresolved')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
