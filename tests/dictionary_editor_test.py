#!/usr/bin/env python3
"""Regression tests for the in-place dictionary editor."""

from __future__ import annotations

import tempfile
from pathlib import Path

from dictionary_editor import (
    load_glossary_dictionary,
    load_txt_dictionary,
    parse_txt_line,
    serialize_glossary_dictionary,
    serialize_txt_dictionary,
    update_dictionary_row,
    write_glossary_dictionary,
    write_txt_dictionary,
)
from voice_dictionary import load_native_dictionary


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="dictionary_editor_") as temp_dir:
        root = Path(temp_dir)

        # --- plain text dictionary round-trip ---
        txt = root / "dict_pre.txt"
        txt.write_text(
            "// 前置词典\n"
            "トレーナー\t训练员\n"
            "^^ゲート\t闸门\n"
            "1^レース\t比赛 # 仅一次\n"
            "単独\t单独\n",
            encoding="utf-8",
        )
        rows, header = load_txt_dictionary(txt)
        assert header == "// 前置词典", header
        assert len(rows) == 4, rows
        assert rows[0] == {"source": "トレーナー", "target": "训练员", "mode": "all", "note": ""}, rows[0]
        assert rows[1]["mode"] == "prefix" and rows[1]["source"] == "ゲート", rows[1]
        assert rows[2]["mode"] == "once" and rows[2]["note"] == "仅一次", rows[2]

        parsed = parse_txt_line("  先頭\t队首")
        assert parsed is not None and parsed["source"] == "先頭", parsed
        assert parse_txt_line("// 注释") is None
        assert parse_txt_line("只有一列") is None

        rows[1]["target"] = "闸门（起跑）"
        write_txt_dictionary(txt, rows, header)
        again_rows, again_header = load_txt_dictionary(txt)
        assert again_header == "// 前置词典", again_header
        assert again_rows[1]["target"] == "闸门（起跑）", again_rows[1]
        assert again_rows[2]["mode"] == "once" and again_rows[2]["note"] == "仅一次", again_rows[2]
        native_rows, unsupported = load_native_dictionary(txt)
        assert unsupported == 0, unsupported
        native_once = next(entry for entry in native_rows if entry.source == "レース")
        assert native_once.target == "比赛" and native_once.note == "仅一次", native_once
        assert "先頭" not in [r["source"] for r in again_rows]

        # --- GPT arrow format and hidden metadata preservation ---
        gpt = root / "dict_gpt.txt"
        gpt.write_text("// GPT\nファイン -> Fine # 固定角色名\n", encoding="utf-8")
        gpt_rows, gpt_header = load_txt_dictionary(gpt, gpt=True)
        assert gpt_rows[0]["target"] == "Fine" and gpt_rows[0]["note"] == "固定角色名", gpt_rows
        update_dictionary_row(gpt_rows, 0, source="ファイン", target="Fine（统一）", mode="once")
        assert gpt_rows[0]["note"] == "固定角色名", gpt_rows[0]
        write_txt_dictionary(gpt, gpt_rows, gpt_header, gpt=True)
        gpt_reloaded, _ = load_txt_dictionary(gpt, gpt=True)
        assert gpt_reloaded[0]["mode"] == "once", gpt_reloaded[0]
        assert gpt_reloaded[0]["note"] == "固定角色名", gpt_reloaded[0]

        try:
            serialize_txt_dictionary([{"source": "未完成", "target": ""}], "")
        except ValueError as exc:
            assert "缺少译文" in str(exc), exc
        else:
            raise AssertionError("A one-sided dictionary row was silently discarded")

        try:
            serialize_txt_dictionary(
                [{"source": "重复", "target": "一"}, {"source": "重复", "target": "二"}],
                "",
            )
        except ValueError as exc:
            assert "重复" in str(exc), exc
        else:
            raise AssertionError("Duplicate dictionary sources were accepted")

        # --- glossary JSON round-trip with advanced fields ---
        glossary = root / "translation_glossary.json"
        glossary.write_text(
            "{\n"
            '  "ファイン": "Fine",\n'
            '  "アルトレ": {"default": "阿尔特雷", "trainer_context": "阿尔丹训练员", "note": "根据上下文选择"},\n'
            '  "目白アルダン": {"default": "目白阿尔丹", "possessive": "true"}\n'
            "}\n",
            encoding="utf-8",
        )
        g_rows = load_glossary_dictionary(glossary)
        assert len(g_rows) == 3, g_rows
        by_source = {row["source"]: row for row in g_rows}
        assert by_source["ファイン"]["target"] == "Fine" and not by_source["ファイン"]["advanced"], by_source
        assert by_source["アルトレ"]["advanced"] == {"trainer_context": "阿尔丹训练员", "note": "根据上下文选择"}, by_source["アルトレ"]
        assert by_source["目白アルダン"]["advanced"] == {"possessive": "true"}, by_source["目白アルダン"]

        by_source["アルトレ"]["target"] = "阿尔丹训练员"
        by_source["ファイン"]["target"] = "Fine（翻好）"
        write_glossary_dictionary(glossary, g_rows)
        reloaded = load_glossary_dictionary(glossary)
        again_by = {row["source"]: row for row in reloaded}
        assert again_by["アルトレ"]["target"] == "阿尔丹训练员", again_by["アルトレ"]
        assert again_by["アルトレ"]["advanced"]["trainer_context"] == "阿尔丹训练员", again_by["アルトレ"]
        assert again_by["目白アルダン"]["advanced"]["possessive"] == "true", again_by["目白アルダン"]
        assert again_by["ファイン"]["target"] == "Fine（翻好）", again_by["ファイン"]

        broken = root / "broken_glossary.json"
        broken.write_text('{"ファイン": ', encoding="utf-8")
        try:
            load_glossary_dictionary(broken)
        except ValueError as exc:
            assert "JSON 格式错误" in str(exc), exc
        else:
            raise AssertionError("Malformed glossary JSON was treated as an empty dictionary")

        wrong_root = root / "wrong_root.json"
        wrong_root.write_text("[]", encoding="utf-8")
        try:
            load_glossary_dictionary(wrong_root)
        except ValueError as exc:
            assert "最外层必须是对象" in str(exc), exc
        else:
            raise AssertionError("A non-object glossary was accepted")

        # --- new file creation ---
        new_txt = root / "new_dict.txt"
        write_txt_dictionary(new_txt, [{"source": "新人", "target": "新人"}], "")
        assert "新人\t新人" in new_txt.read_text(encoding="utf-8"), new_txt.read_text(encoding="utf-8")

    print("Dictionary editor tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
