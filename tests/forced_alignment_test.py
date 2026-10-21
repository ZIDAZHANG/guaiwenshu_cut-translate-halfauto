"""Independent alignment must retain source text and expose SDK timestamp repairs."""
import copy,tempfile,unittest
from pathlib import Path
from subtitle_axis_align import Cue
from forced_alignment import map_prediction_units,validate_evidence

class IndependentAlignmentTests(unittest.TestCase):
    def test_source_ranges_keep_punctuation_names_and_real_repeats(self):
        cues=[Cue(100,400,"ｶﾞ。Alice!",13),Cue(500,900,"ｶﾞ。Alice!",21)]
        result=map_prediction_units(cues,[{"text":"ガ"},{"text":"Alice"},{"text":"ガ"},{"text":"Alice"}],
            [80,200,220,420,480,600,620,920],[80,200,220,420,480,600,620,920],1000)
        self.assertEqual(result["source_cues"][0]["text"],"ｶﾞ。Alice!")
        self.assertEqual(result["word_times"]["13"][0]["char_end"],2)
        self.assertEqual(result["word_times"]["21"][0]["char_start"],0)
        self.assertEqual(result["word_times"]["13"][-1]["end_ms"],420)
        validate_evidence(result,cues)

    def test_long_text_has_no_project_length_limit(self):
        original="長い台詞"*4000+"。"
        cues=[Cue(100,500,original,1)]
        result=map_prediction_units(cues,[{"text":original}],[10,900],[10,900],1000)
        self.assertEqual(result["source_cues"][0]["text"],original)
        self.assertEqual(result["word_times"]["1"][0]["char_end"],len(original)-1)
        validate_evidence(result,cues)

    def test_sdk_repaired_values_never_become_observed_times(self):
        cues=[Cue(0,1000,"甲乙丙",1)]
        result=map_prediction_units(cues,[{"text":"甲"},{"text":"乙"},{"text":"丙"}],
            [100,400,200,600,600,900],[100,200,200,600,600,900],1000)
        self.assertEqual(len(result["unusable_tokens"]),2)
        self.assertEqual(result["sdk_repaired_unit_count"],1)
        self.assertEqual(result["word_times"]["1"][0]["text"],"丙")
        self.assertEqual(result["prediction_units"][0]["raw_end_ms"],400)
        validate_evidence(result,cues)

    def test_zero_duration_and_out_of_audio_are_retained_unusable(self):
        cues=[Cue(0,1000,"甲乙",1)]
        result=map_prediction_units(cues,[{"text":"甲"},{"text":"乙"}],[100,100,900,1200],[100,100,900,1200],1000)
        self.assertEqual(result["word_times"]["1"],[])
        self.assertEqual(len(result["unusable_tokens"]),2)
        validate_evidence(result,cues)

    def test_normalized_missing_or_duplicate_source_is_rejected(self):
        for text in ("甲","甲甲乙"):
            with self.subTest(text=text),self.assertRaisesRegex(ValueError,"complete"):
                map_prediction_units([Cue(0,1000,"甲乙。",1)],[{"text":text}],[0,900],[0,900],1000)

    def test_a_unit_crossing_original_cues_is_not_split_by_proportion(self):
        cues=[Cue(0,400,"甲",1),Cue(400,1000,"乙",2)]
        result=map_prediction_units(cues,[{"text":"甲乙"}],[200,700],[200,700],1000)
        self.assertEqual(result["word_times"],{"1":[],"2":[]})
        self.assertEqual(len(result["prediction_units"][0]["source_ranges"]),2)
        self.assertIn("WORD_CROSSES_SOURCE_CUES",result["unusable_tokens"][0]["flags"])
        validate_evidence(result,cues)

    def test_forged_word_coordinates_and_stale_source_are_rejected(self):
        cues=[Cue(0,1000,"甲。",1)]
        result=map_prediction_units(cues,[{"text":"甲"}],[100,900],[100,900],1000)
        altered=copy.deepcopy(result);altered["word_times"]["1"][0]["start_ms"]=200
        with self.assertRaisesRegex(ValueError,"altered"):validate_evidence(altered,cues)
        with self.assertRaisesRegex(ValueError,"different"):validate_evidence(result,[Cue(0,1000,"乙。",1)])
        result["raw_prediction_audited"]=False
        with self.assertRaisesRegex(ValueError,"Unaudited"):validate_evidence(result,cues)

    def test_non_finite_values_round_trip_as_rejected_evidence(self):
        import json
        cues=[Cue(0,1000,"甲乙。",1)]
        result=map_prediction_units(cues,[{"text":"甲"},{"text":"乙"}],
            [float("nan"),200,300,900],[float("nan"),200,300,900],1000)
        saved=json.loads(json.dumps(result,allow_nan=False))
        self.assertEqual([w["text"] for w in saved["word_times"]["1"]],["乙"])
        self.assertIn("NON_FINITE_TIMESTAMP",saved["unusable_tokens"][0]["flags"])
        validate_evidence(saved,cues)


if __name__=="__main__":unittest.main()
