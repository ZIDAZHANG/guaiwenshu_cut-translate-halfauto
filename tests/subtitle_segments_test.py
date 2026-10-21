#!/usr/bin/env python3
import unittest
from subtitle_axis_align import Cue
from subtitle_segments import audit_segment_records, build_segments, normalized_chars, render_source_srt

class SegmentTests(unittest.TestCase):
    def test_identical_time_and_text_events_keep_distinct_source_occurrences(self):
        speech = [Cue(1000, 3000, "同じ。", 1), Cue(1000, 3000, "同じ。", 2)]
        records = build_segments(speech, mode="speech")
        audit_segment_records(records)
        self.assertEqual([item["text"] for item in records["segments"]], ["同じ。", "同じ。"])
        self.assertEqual(len({parent["id"] for parent in records["parents"]}), 2)
        self.assertEqual(len({item["id"] for item in records["segments"]}), 2)
        self.assertTrue(all(not item["missing_chars"] for item in records["coverage"]))
        self.assertEqual(records, build_segments(speech, mode="speech"))

    def test_identical_visual_events_are_all_auditable(self):
        visual = [Cue(1000, 3000, "同じ。", 1), Cue(1000, 3000, "同じ。", 2)]
        records = build_segments([], visual)
        audit_segment_records(records)
        self.assertEqual(len(records["visual_events"]), 2)
        self.assertEqual(len({item["id"] for item in records["visual_events"]}), 2)
        represented = {key for item in records["segments"] for key in item["visual_ids"]}
        represented.update(item["id"] for item in records["unresolved_visual_events"])
        self.assertEqual(represented, {item["id"] for item in records["visual_events"]})

    def test_kana_script_difference_keeps_two_utterances_in_same_visual_caption(self):
        speech = [Cue(12800, 13660, "でででで", 1), Cue(13660, 14320, "あら?", 2)]
        result = build_segments(speech, [Cue(13090, 14630, "デデデデ！！ 三あら?]", 1)])
        self.assertEqual(len(result["segments"]), 1)
        self.assertEqual(result["segments"][0]["text"], "でででで\nあら?")
        self.assertEqual(len(result["segments"][0]["source_ranges"]), 2)
        self.assertTrue(all(not c["missing_chars"] for c in result["coverage"]))

    def test_native_word_clock_does_not_assign_next_name_to_previous_caption(self):
        # Actual no-VAD native clocks from the recovered 8/11 test slice.
        speech = [Cue(23600, 25680, "ねえこれ、速攻ホテル前", 1),
                  Cue(25680, 28060, "ある男子服だとさらにデカく見える", 2)]
        pages = [Cue(23830, 24630, "ねえこれ…", 1),
                 Cue(24630, 25990, "ソッコーホテルマエ!", 2),
                 Cue(25990, 28360, "アルグン私服とらデカく見る", 3)]
        clocks = [
            [(0,1,23660,23780),(1,2,23780,23890),(2,4,24010,24330),
             (5,6,24510,24690),(6,7,24690,24870),(7,8,24870,25030),
             (8,9,25090,25170),(9,10,25250,25410),(10,11,25410,25610)],
            [(0,2,25740,25970),(2,3,25970,26100),(3,4,26130,26250),
             (4,5,26250,26390),(5,6,26390,26530),(6,7,26530,26670),
             (7,8,26670,26810),(8,9,26810,26950),(9,10,26950,27090),
             (10,11,27090,27230),(11,12,27230,27370),(12,13,27370,27510),
             (13,14,27510,27650),(14,16,27650,28060)]]
        words = {str(index): [dict(zip(("char_start", "char_end", "start_ms", "end_ms"), row))
                             for row in rows] for index, rows in enumerate(clocks, 1)}
        for mode in ("mixed", "speech"):
            records = build_segments(speech, pages, words, mode)
            audit_segment_records(records)
            self.assertEqual([c["text"] for c in records["segments"]],
                             ["ねえこれ、", "速攻ホテル前", "ある男子服だとさらにデカく見える"])
            self.assertTrue(all(not c["missing_chars"] for c in records["coverage"]))
            self.assertEqual(records["segments"][2]["source_ranges"][0]["char_start"], 0)

    def test_continuous_sentence_across_three_pages(self):
        speech = [Cue(0, 6000, "私はモブウマ娘、今日も走る。お尻が割れました。", 1)]
        pages = [Cue(0, 2000, "私はモブウマ娘、", 1),
                 Cue(2000, 4000, "今日も走る。", 2),
                 Cue(4000, 6000, "お尻が割れました。", 3)]
        result = build_segments(speech, pages)
        self.assertEqual([item["text"] for item in result["segments"]], [page.text for page in pages])
        self.assertEqual(result["needs_review"], 0)
        self.assertEqual("".join(item["text"] for item in result["segments"]), speech[0].text)
        self.assertEqual(result["coverage"][0]["covered_chars"], len(speech[0].text))

    def test_many_utterances_in_one_page(self):
        speech = [Cue(i * 1000, (i + 1) * 1000, f"台詞{i}。", i + 1) for i in range(8)]
        result = build_segments(speech, [Cue(0, 8000, "".join(item.text for item in speech), 1)])
        self.assertEqual(len(result["segments"]), 1)
        self.assertEqual(len(result["segments"][0]["source_ranges"]), 8)
        self.assertEqual(result["needs_review"], 0)

    def test_background_cut_same_caption_is_one_segment(self):
        speech = [Cue(0, 4000, "同じ字幕。", 1)]
        result = build_segments(speech, [Cue(0, 2000, "同じ字幕。", 1), Cue(2000, 4000, "同じ字幕。", 2)])
        self.assertEqual(len(result["segments"]), 1)
        self.assertEqual(result["segments"][0]["end_ms"], 4000)
        self.assertEqual(len(result["segments"][0]["visual_ids"]), 2)

    def test_actual_repeated_utterance_is_preserved(self):
        speech = [Cue(0, 2000, "同じ字幕。", 1), Cue(2000, 4000, "同じ字幕。", 2)]
        result = build_segments(speech, [Cue(0, 2000, "同じ字幕。", 1), Cue(2000, 4000, "同じ字幕。", 2)])
        self.assertEqual(len(result["segments"]), 2)
        self.assertNotEqual(result["segments"][0]["source_ranges"], result["segments"][1]["source_ranges"])

    def test_no_content_or_duration_cap(self):
        text = "長い台詞を省略しない。" * 3000
        result = build_segments([Cue(0, 3600000, text, 1)], [Cue(0, 3600000, text, 1)])
        self.assertEqual(result["segments"][0]["text"], text)
        self.assertEqual(result["segments"][0]["end_ms"], 3600000)

    def test_unicode_comparison_keeps_original(self):
        text = "ｷﾞｬﾙ　ウマ娘①、走る。"
        result = build_segments([Cue(0, 4000, text, 1)], [Cue(0, 4000, "ギャルウマ娘1、走る。", 1)])
        self.assertEqual(result["needs_review"], 0)
        self.assertEqual(result["segments"][0]["text"], text)
        normalized, offsets = normalized_chars(text)
        self.assertEqual(normalized, "ぎゃるうま娘1走る")
        self.assertEqual(offsets[0], (0, 2))

    def test_text_conflict_requires_review(self):
        result = build_segments([Cue(0, 4000, "今日は晴れ。", 1)], [Cue(0, 4000, "明日は雨。", 1)])
        self.assertGreater(result["needs_review"], 0)
        self.assertEqual(result["coverage"][0]["covered_chars"], len("今日は晴れ。"))
        self.assertTrue(any("TEXT_CONFLICT" in item["flags"] for item in result["segments"]))

    def test_placeholder_axis_does_not_duplicate_speech(self):
        source = "連続する長い台詞をそのまま保つ。"
        result = build_segments([Cue(0, 6000, source, 1)],
                                [Cue(0, 2000, "Subtitle_Dialog_0", 1),
                                 Cue(2000, 4000, "Subtitle_Dialog_1", 2),
                                 Cue(4000, 6000, "Subtitle_Dialog_2", 3)])
        self.assertEqual(len(result["segments"]), 1)
        self.assertEqual(result["segments"][0]["text"], source)
        self.assertIn("CHECK", result["segments"][0]["flags"])

    def test_audio_mode_needs_no_visual_axis(self):
        result = build_segments([Cue(1000, 3000, "語音字幕。", 1)], None, mode="speech")
        self.assertEqual(result["segments"][0]["start_ms"], 1000)
        self.assertEqual(result["needs_review"], 0)

    def test_speech_boundaries_use_word_evidence(self):
        speech = [Cue(0, 6000, "前半。後半。", 1)]
        words = {"1": [{"char_start": 0, "char_end": 2, "start_ms": 500, "end_ms": 2500},
                       {"char_start": 3, "char_end": 5, "start_ms": 3000, "end_ms": 5500}]}
        result = build_segments(speech, [Cue(0, 3000, "前半。", 1), Cue(3000, 6000, "後半。", 2)],
                                words, mode="speech")
        self.assertEqual([(item["start_ms"], item["end_ms"]) for item in result["segments"]],
                         [(500, 2500), (3000, 5500)])
        self.assertEqual(result["needs_review"], 0)

    def test_partial_word_timing_cannot_reverse_sentence_halves(self):
        speech = [Cue(0, 4000, "前半。後半。", 1)]
        words = {"1": [{"char_start": 0, "char_end": 2, "start_ms": 500, "end_ms": 1500}]}
        result = build_segments(speech, [Cue(0, 2000, "前半。", 1), Cue(2000, 4000, "後半。", 2)],
                                words, mode="speech")
        self.assertEqual([item["text"] for item in result["segments"]], ["前半。", "後半。"])
        self.assertTrue(all("WORD_TIMING_MISSING" in item["flags"] for item in result["segments"]))
        self.assertEqual([(item["start_ms"], item["end_ms"]) for item in result["segments"]],
                         [(0, 4000), (0, 4000)])
        self.assertEqual((result["segments"][0]["speech_start_ms"], result["segments"][0]["speech_end_ms"]),
                         (500, 1500))

    def test_incomplete_timing_spanning_parents_preserves_source_order(self):
        speech = [Cue(0, 4000, "前半。後半。", 1), Cue(4000, 8000, "続き。結び。", 2)]
        words = {"1": [{"char_start": 0, "char_end": 2, "start_ms": 500, "end_ms": 1500}],
                 "2": [{"char_start": 0, "char_end": 2, "start_ms": 4500, "end_ms": 5500},
                       {"char_start": 3, "char_end": 5, "start_ms": 6500, "end_ms": 7500}]}
        result = build_segments(speech, [Cue(0, 2000, "前半。", 1), Cue(2000, 6000, "後半。続き。", 2),
                                         Cue(6000, 8000, "結び。", 3)], words, mode="speech")
        self.assertEqual([item["text"] for item in result["segments"]], ["前半。", "後半。\n続き。", "結び。"])
        self.assertTrue(all("WORD_TIMING_MISSING" in item["flags"] for item in result["segments"]))
        self.assertTrue(all(not item["missing_chars"] for item in result["coverage"]))

    def test_missing_word_evidence_is_not_proportional_timing(self):
        result = build_segments([Cue(0, 6000, "前半。後半。", 1)],
                                [Cue(0, 3000, "前半。", 1), Cue(3000, 6000, "後半。", 2)],
                                mode="speech")
        self.assertTrue(all("WORD_TIMING_MISSING" in item["flags"] for item in result["segments"]))
        self.assertEqual(result["segments"][0]["start_ms"], 0)
        self.assertEqual(result["segments"][1]["start_ms"], 0)

    def test_unvoiced_visual_text_survives(self):
        result = build_segments([], [Cue(0, 2000, "・・・", 1)])
        self.assertEqual(result["segments"][0]["text"], "・・・")
        self.assertIn("VISUAL_ONLY", result["segments"][0]["flags"])

    def test_stable_identity(self):
        speech, pages = [Cue(0, 6000, "台詞。", 1)], [Cue(0, 6000, "台詞。", 1)]
        self.assertEqual(build_segments(speech, pages)["segments"][0]["id"],
                         build_segments(speech, pages)["segments"][0]["id"])

    def test_whole_source_srt_retained(self):
        result = build_segments([Cue(0, 6000, "全文。", 1)], [Cue(0, 3000, "全", 1)])
        self.assertEqual(result["coverage"][0]["covered_chars"], len("全文。"))
        self.assertIn("文。", render_source_srt(result))


class CorrespondenceRegressionTests(unittest.TestCase):
    def test_misrecognized_name_stays_with_its_entire_caption(self):
        speech=[Cue(0,3000,"ある男のせいでラブホに見える。",1)]
        pages=[Cue(0,3000,"アルダンのせいでラブホに見える",1)]
        records=build_segments(speech,pages)
        self.assertEqual([i["text"] for i in records["segments"]],[speech[0].text])
        self.assertIn("TEXT_CONFLICT",records["segments"][0]["flags"])
        audit_segment_records(records)

    def test_phonetic_prefix_and_suffix_are_not_orphaned(self):
        speech=[Cue(0,2000,"速攻ホテル前。",1)]
        records=build_segments(speech,[Cue(0,2000,"ソッコーホテルマエ！",1)])
        self.assertEqual([i["text"] for i in records["segments"]],[speech[0].text])
        self.assertIn("TEXT_CONFLICT",records["segments"][0]["flags"])

    def test_next_page_is_not_consumed_by_a_previous_fuzzy_match(self):
        speech=[Cue(14000,18000,"あら、愛しているのね。サプライズラモーヌやめろ。",1),
                Cue(17600,21300,"最後スーッときつい格好のラモさん映ってダメだった。",2)]
        pages=[Cue(14630,16330,"あら、愛しているのね。",1),
               Cue(16330,18030,"サプライズラモーヌやメろ",2),
               Cue(18030,21590,"最後ーソとキツイ格好のラモさん映ってグメだった",3)]
        records=build_segments(speech,pages)
        self.assertEqual([i["text"] for i in records["segments"]],
                         ["あら、愛しているのね。","サプライズラモーヌやめろ。",speech[1].text])
        self.assertEqual(records["unresolved_visual_events"],[])
        audit_segment_records(records)

    def test_word_clock_breaks_text_tie_without_stealing_next_name_prefix(self):
        speech=[Cue(23520,26000,"ねえこれ、速攻ホテル前。",1),
                Cue(25620,28300,"ある男子服だとさらにデカく見える。",2)]
        pages=[Cue(23830,24630,"ねえこれ",1),Cue(24630,25990,"ソッコーホテルマエ！",2),
               Cue(25990,28360,"アルグン私服とらデカく見る",3)]
        words={"1":[{"char_start":0,"char_end":4,"start_ms":23840,"end_ms":24320},
                    {"char_start":5,"char_end":7,"start_ms":24640,"end_ms":25040},
                    {"char_start":7,"char_end":10,"start_ms":25120,"end_ms":25440},
                    {"char_start":10,"char_end":11,"start_ms":25440,"end_ms":25680}],
               "2":[{"char_start":0,"char_end":2,"start_ms":26000,"end_ms":26160},
                    {"char_start":2,"char_end":4,"start_ms":26160,"end_ms":26480},
                    {"char_start":4,"char_end":5,"start_ms":26480,"end_ms":26560},
                    {"char_start":5,"char_end":7,"start_ms":26720,"end_ms":26960},
                    {"char_start":7,"char_end":13,"start_ms":26960,"end_ms":27680},
                    {"char_start":13,"char_end":16,"start_ms":27680,"end_ms":28080}]}
        records=build_segments(speech,pages,words)
        self.assertEqual([i["text"] for i in records["segments"]],
                         ["ねえこれ、","速攻ホテル前。",speech[1].text])
        audit_segment_records(records)

    def test_coarse_time_tie_preserves_next_name_without_inventing_words(self):
        speech=[Cue(23520,25620,"ねえこれ、速攻ホテル前。",1),
                Cue(25620,28010,"ある男子服だとさらにデカく見える。",2)]
        pages=[Cue(23830,24630,"ねえこれ",1),Cue(24630,25990,"ソッコーホテルマエ！",2),
               Cue(25990,28360,"アルグン私服とらデカく見る",3)]
        records=build_segments(speech,pages)
        self.assertEqual([i["text"] for i in records["segments"]],
                         ["ねえこれ、","速攻ホテル前。",speech[1].text])
        self.assertTrue(all(not p["words"] for p in records["parents"]))
        self.assertTrue(all(i["timing_source"]=="visual_page" for i in records["segments"]))
        audit_segment_records(records)

    def test_identical_fuzzy_caption_across_background_cut_is_not_repeated(self):
        text="ある男のせいでラブホに見える。"
        pages=[Cue(0,2000,"アルダンのせいでラブホに見える",1),
               Cue(2000,4000,"アルダンのせいでラブホに見える",2)]
        for mode in ["mixed","speech"]:
            records=build_segments([Cue(0,4000,text,1)],pages,mode=mode)
            self.assertEqual([i["text"] for i in records["segments"]],[text])
            self.assertEqual(records["segments"][0]["end_ms"],4000)
            self.assertEqual(len(records["segments"][0]["visual_ids"]),2)
            self.assertEqual(records["unresolved_visual_events"],[])
            audit_segment_records(records)

    def test_unvoiced_caption_keeps_full_time_across_background_cut(self):
        for mode in ["mixed","screen","speech"]:
            records=build_segments([], [Cue(0,2000,"画面だけの台詞。",1),
                                       Cue(2000,4000,"画面だけの台詞。",2)], mode=mode)
            self.assertEqual(len(records["segments"]),1)
            item=records["segments"][0]
            self.assertEqual((item["start_ms"],item["end_ms"]),(0,4000))
            self.assertEqual(len(item["visual_ids"]),2)
            self.assertEqual(item["text"],"画面だけの台詞。")
            self.assertEqual(item["source_ranges"],[])
            audit_segment_records(records)

    def test_unvoiced_punctuation_cut_keeps_all_visual_events(self):
        records=build_segments([], [Cue(0,2000,"・・・",1),Cue(2000,4000,"・・・",2)])
        self.assertEqual(len(records["segments"]),1)
        self.assertEqual((records["segments"][0]["start_ms"],records["segments"][0]["end_ms"]),(0,4000))
        self.assertEqual(len(records["segments"][0]["visual_ids"]),2)
        audit_segment_records(records)

    def test_true_fuzzy_repeat_within_one_parent_is_preserved(self):
        text="ある男の台詞。"
        records=build_segments([Cue(0,4000,text+text,1)],
                               [Cue(0,2000,"アルダンの台詞",1),Cue(2000,4000,"アルダンの台詞",2)])
        self.assertEqual([i["text"] for i in records["segments"]],[text,text])
        self.assertEqual(records["unresolved_visual_events"],[])
        audit_segment_records(records)

    def test_fuzzy_pages_split_continuous_parent_and_keep_all_source(self):
        speech=[Cue(0,5000,"ある男が走る。ある男が笑う。",1)]
        records=build_segments(speech,[Cue(0,2500,"アルダンが走る。",1),
                                      Cue(2500,5000,"アルダンが笑う。",2)])
        self.assertEqual([i["text"] for i in records["segments"]],["ある男が走る。","ある男が笑う。"])
        self.assertTrue(all("TEXT_CONFLICT" in i["flags"] for i in records["segments"]))
        audit_segment_records(records)


class EvidenceTests(unittest.TestCase):
    def test_byte_level_japanese_tokens_are_losslessly_reassembled(self):
        import json, tempfile
        from pathlib import Path
        from subtitle_segments import load_whisper_word_times
        first, second = b"\xe3\x82", b"\xae"
        tokens = [
            {"text": first.decode("utf-8", errors="surrogateescape"), "id": 1,
             "offsets": {"from": 10, "to": 20}, "t_dtw": 1},
            {"text": second.decode("utf-8", errors="surrogateescape"), "id": 2,
             "offsets": {"from": 20, "to": 30}, "t_dtw": 2},
            {"text": "ャル", "id": 3, "offsets": {"from": 30, "to": 80}, "t_dtw": 3}]
        document = {"transcription": [{"text": "ギャル", "tokens": tokens}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tokens.json"
            path.write_bytes(json.dumps(document, ensure_ascii=False).encode("utf-8", errors="surrogateescape"))
            with self.assertRaises(UnicodeDecodeError):
                path.read_text(encoding="utf-8")
            evidence = load_whisper_word_times(path, [Cue(0, 100, "ギャル", 1)])
            words = evidence["word_times"]["1"]
            self.assertEqual([(word["text"], word["char_start"], word["char_end"]) for word in words],
                             [("ギ", 0, 1), ("ャル", 1, 3)])
            self.assertEqual(words[0]["token_ids"], [1, 2])
            with self.assertRaises(ValueError):
                load_whisper_word_times(path, [Cue(0, 100, "別の言葉", 1)])

    def test_partial_token_coverage_does_not_claim_word_boundaries(self):
        words = {"1": [{"char_start": 0, "char_end": 1, "start_ms": 500, "end_ms": 600}]}
        records = build_segments([Cue(0, 6000, "前半。後半。", 1)],
                                 [Cue(0, 3000, "前半。", 1)], words, "speech")
        self.assertTrue(all(item["timing_source"] != "speech_words" for item in records["segments"]))
        self.assertGreater(records["needs_review"], 0)

    def test_speech_mode_keeps_same_caption_across_background_cut(self):
        result = build_segments([Cue(0, 4000, "同じ台詞。", 1)],
                                [Cue(0, 2000, "同じ台詞。", 1), Cue(2000, 4000, "同じ台詞。", 2)], mode="speech")
        self.assertEqual(len(result["segments"]), 1)
        self.assertEqual(result["segments"][0]["timing_source"], "speech_parent")

    def test_range_audit_rejects_duplicate_characters_despite_green_coverage(self):
        from copy import deepcopy
        from subtitle_segments import audit_segment_records
        result = build_segments([Cue(0, 4000, "前半。後半。", 1)],
                                [Cue(0, 2000, "前半。", 1), Cue(2000, 4000, "後半。", 2)])
        audit_segment_records(result)
        damaged = deepcopy(result)
        damaged["segments"][1]["source_ranges"] = damaged["segments"][0]["source_ranges"]
        damaged["segments"][1]["text"] = damaged["segments"][0]["text"]
        with self.assertRaises(ValueError):
            audit_segment_records(damaged)

    def test_segment_ids_prevent_wrong_translation_and_preserve_good_output(self):
        import tempfile
        from pathlib import Path
        from subtitle_segments import publish_segment_output
        from subtitle_axis_align import parse_subtitle
        from artifact_validation import validate_review_csv
        records = build_segments([Cue(0, 4000, "前半。後半。", 1)],
                                 [Cue(0, 2000, "前半。", 1), Cue(2000, 4000, "後半。", 2)])
        translations = [Cue(0, 2000, "前半部分。", 1), Cue(2000, 4000, "后半部分。", 2)]
        metadata = {"translations": [
            {"cue_id": item["id"], "index": item["index"], "start_ms": item["start_ms"],
             "end_ms": item["end_ms"], "text": cue.text}
            for item, cue in zip(records["segments"], translations)]}
        with tempfile.TemporaryDirectory() as directory:
            output, report = Path(directory) / "final.ass", Path(directory) / "review.csv"
            publish_segment_output(records, translations, output, report, translation_metadata=metadata)
            self.assertEqual([cue.text for cue in parse_subtitle(output)[2]], [cue.text for cue in translations])
            validation = validate_review_csv(report, require_provenance=True)
            self.assertTrue(validation.valid, validation.errors)
            self.assertEqual(validation.details["needs_review"], 0)
            known_good = output.read_bytes()
            metadata["translations"][1]["cue_id"] = metadata["translations"][0]["cue_id"]
            with self.assertRaises(ValueError):
                publish_segment_output(records, translations, output, report, translation_metadata=metadata)
            self.assertEqual(output.read_bytes(), known_good)

    def test_old_six_stage_task_migrates_without_repeating_recognition(self):
        import tempfile
        from pathlib import Path
        from task_state import DEFAULT_STAGES, TaskState
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_names = [name for name in DEFAULT_STAGES if name != "SEGMENTATION"]
            state = TaskState.open_or_create(root/"task.json", source={}, job_dir=root,
                                            display_name="旧任务", stages=old_names)
            for name in old_names:
                state.start_stage(name)
                state.complete_stage(name)
            state.ensure_stages(DEFAULT_STAGES)
            self.assertEqual(list(state.data["stages"]), list(DEFAULT_STAGES))
            self.assertEqual(state.stage_status("ASR"), "DONE")
            self.assertEqual(state.data["stages"]["ASR"]["attempt"], 1)
            self.assertEqual(state.stage_status("SEGMENTATION"), "PENDING")
            self.assertEqual(state.stage_status("TRANSLATION"), "PENDING")

    def test_native_compacted_token_times_are_not_used_as_video_times(self):
        import json, tempfile
        from pathlib import Path
        from subtitle_segments import load_whisper_word_times
        document={"transcription":[
            {"text":"日本", "tokens":[{"text":"日本","id":1,"offsets":{"from":200,"to":900},"t_dtw":50}]},
            {"text":"はい", "tokens":[{"text":"はい","id":2,"offsets":{"from":3300,"to":3800},"t_dtw":350}]}]}
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"native.json"
            original=json.dumps(document,ensure_ascii=False).encode("utf-8")
            path.write_bytes(original)
            evidence=load_whisper_word_times(path,[Cue(1000,3000,"日本",1),Cue(3000,4000,"はい",2)])
            self.assertEqual(evidence["word_times"],{"1":[],"2":[]})
            self.assertEqual(evidence["timing_evidence_status"],"unusable")
            self.assertEqual(len(evidence["coordinate_issues"]),1)
            self.assertEqual([item["token_ids"] for item in evidence["unusable_tokens"]],[[1],[2]])
            self.assertEqual(path.read_bytes(),original)

    def test_supplied_word_times_outside_parent_are_rejected(self):
        words={"1":[{"char_start":0,"char_end":2,"start_ms":200,"end_ms":900}]}
        with self.assertRaises(ValueError):
            build_segments([Cue(1000,3000,"日本",1)],None,words,"speech")


    def test_independent_times_use_actual_audio_without_relaxing_native_guard(self):
        from forced_alignment import map_prediction_units
        speech=[Cue(0,1500,"前半。後半。",1)]
        pages=[Cue(0,800,"前半。",1),Cue(800,2000,"後半。",2)]
        evidence=map_prediction_units(speech,[{"text":"前半"},{"text":"後半"}],
                                      [100,500,1000,1800],[100,500,1000,1800],2000)
        records=build_segments(speech,pages,mode="speech",independent_evidence=evidence)
        self.assertEqual([(i["start_ms"],i["end_ms"]) for i in records["segments"]],[(100,500),(1000,1800)])
        self.assertEqual(records["parents"][0]["end_ms"],1500)
        audit_segment_records(records)
        with self.assertRaises(ValueError):build_segments(speech,pages,evidence["word_times"],"speech")

    def test_independent_observed_edges_keep_zero_duration_interior_as_a_gap(self):
        from forced_alignment import map_prediction_units
        speech=[Cue(0,1000,"甲乙丙丁",1)]
        pages=[Cue(0,650,"甲乙丙",1),Cue(650,1000,"丁",2)]
        units=[{"text":t} for t in "甲乙丙丁"]
        times=[100,200,250,250,500,600,700,900]
        evidence=map_prediction_units(speech,units,times,times,1000)
        records=build_segments(speech,pages,mode="speech",independent_evidence=evidence)
        self.assertEqual([i["text"] for i in records["segments"]],["甲乙丙","丁"])
        self.assertEqual([(i["start_ms"],i["end_ms"]) for i in records["segments"]],[(100,600),(700,900)])
        self.assertIn("WORD_TIMING_GAPS",records["segments"][0]["flags"])
        self.assertIn("CHECK",records["segments"][0]["flags"])
        self.assertEqual(evidence["unusable_tokens"][0]["text"],"乙")
        audit_segment_records(records)

    def test_independent_missing_edge_keeps_parent_clock_and_source_order(self):
        from forced_alignment import map_prediction_units
        speech=[Cue(0,1000,"甲乙",1)];pages=[Cue(0,500,"甲",1),Cue(500,1000,"乙",2)]
        evidence=map_prediction_units(speech,[{"text":"甲"},{"text":"乙"}],
                                      [100,400,200,800],[100,200,200,800],1000)
        records=build_segments(speech,pages,mode="speech",independent_evidence=evidence)
        self.assertEqual([i["text"] for i in records["segments"]],["甲","乙"])
        self.assertTrue(all(i["timing_source"]=="speech_parent_unresolved" for i in records["segments"]))
        self.assertTrue(all((i["start_ms"],i["end_ms"])==(0,1000) for i in records["segments"]))
        audit_segment_records(records)

    def test_a_visual_split_inside_one_native_word_has_no_observed_word_boundary(self):
        speech=[Cue(0,1000,"おはよう",1)]
        pages=[Cue(0,500,"お",1),Cue(500,1000,"はよう",2)]
        words={"1":[{"char_start":0,"char_end":4,"start_ms":100,"end_ms":900}]}
        records=build_segments(speech,pages,words,"speech")
        self.assertEqual([i["text"] for i in records["segments"]],["お","はよう"])
        self.assertTrue(all(i["timing_source"]=="speech_parent_unresolved" for i in records["segments"]))
        self.assertTrue(all("WORD_TIMING_MISSING" in i["flags"] for i in records["segments"]))
        audit_segment_records(records)


if __name__ == "__main__":
    unittest.main()
