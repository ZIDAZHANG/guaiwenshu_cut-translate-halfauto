#!/usr/bin/env python3
"""Quality checks (plan 1.1, section 11): one read-only function, ``check_quality(QualityInput) -> QualityReport``.

Nothing here repairs, merges, deletes, shifts or clamps a subtitle (plan 11.1): a rule only reports what it measured,
with the file line or cue it is about. ERROR means the structure or identity cannot hold and the revision must not be
committed/exported as it is; WARNING is suspicious content left to a person; INFO states something unknown or
informational. The function returns the report record and writes nothing; the caller that owns the commit stores it.

Rules evaluated on a parsed human file (W05): QC01_PARSE, QC02_TIME, QC03_BOUNDS, QC06_ORDER, QC20_REVIEW_BASE,
QC21_MEDIA_BINDING (structure-only inspection when no media is given) and QC23_EMPTY. Messages are written for the
person who reads the review report, in Chinese; they never contain a user path.
"""

from __future__ import annotations

from typing import Any

from upgrade.contracts import HUMAN_STATUSES, SCHEMA_VERSION, SCOPES, new_id, require_record, utc_now

BOUNDS_TOLERANCE_MS = {"srt": 1, "ass": 10}      # QC03: format quantisation only; the written value is kept
REQUIRED_SCOPES = {"srt": ("text", "timing"), "ass": ("text", "timing", "style")}   # plan 3.3: what REVIEWED needs
HARD_RULES = ("QC01_PARSE", "QC02_TIME", "QC03_BOUNDS", "QC04_IDENTITY", "QC05_COVERAGE", "QC06_ORDER", "QC17_TRANSLATION", "QC21_MEDIA_BINDING")
MAX_LISTED = 20                                    # ids listed inside one summary issue; the count is always exact


# --------------------------------------------------------------------------- issues and human status

def make_issue(revision_id: str, rule_id: str, severity: str, message: str, *, recommended: str, cue_ids: list[str] | None = None, start_ms: int | None = None,
               end_ms: int | None = None, source_refs: list[str] | None = None, observed: Any = None, threshold: Any = None, verification: str = "measured") -> dict:
    return require_record("Issue", {
        "issue_id": new_id(), "rule_id": rule_id, "severity": severity, "revision_id": revision_id, "cue_ids": cue_ids or [],
        "start_ms": start_ms if isinstance(start_ms, int) and start_ms >= 0 else None, "end_ms": end_ms if isinstance(end_ms, int) and end_ms >= 0 else None,
        "source_refs": source_refs or [], "observed": observed, "threshold": threshold, "message": message, "recommended_action": recommended,
        "verification": verification})


def confirmation(revision_id: str, subtitle_sha256: str, media_id: str | None, fmt: str, attestations: list[dict], *, default: str) -> dict:
    """Human status of one subtitle file: only attestations for exactly these bytes and this media count (plan 6.6)."""
    matching = [item for item in attestations if item["revision_id"] == revision_id and item["subtitle_sha256"] == subtitle_sha256
                and (media_id is None or item["media_id"] == media_id)]
    confirmed = [scope for scope in SCOPES if any(scope in item["scopes"] for item in matching)]
    if not matching:
        status = default
    elif all(scope in confirmed for scope in REQUIRED_SCOPES[fmt]):
        status = "REVIEWED"
    else:
        status = "PARTIAL"
    assert status in HUMAN_STATUSES
    return {"subtitle_sha256": subtitle_sha256, "human_status": status, "confirmed_scopes": confirmed,
            "attestation_ids": sorted(item["attestation_id"] for item in matching)}


# --------------------------------------------------------------------------- rules on a parsed view

def _timed(cue: dict) -> bool:
    return isinstance(cue["start_ms"], int) and isinstance(cue["end_ms"], int) and cue["end_ms"] > cue["start_ms"]


def _dialogues(view: dict) -> list[dict]:
    return [cue for cue in view["cues"] if cue["kind"] == "dialogue"]


def _listed(values: list) -> list:
    return values[:MAX_LISTED]


def _parse_and_time(revision_id: str, view: dict) -> list[dict]:
    by_ordinal = {cue["ordinal"]: cue for cue in view["cues"]}
    issues = []
    for problem in view["problems"]:
        cue = by_ordinal.get(problem.get("ordinal"))
        rule_id = problem["rule_id"]
        recommended = ("在编辑器里修正这条字幕的起止时间后重新保存并再次登记；程序不会补时长，也不会取绝对值" if rule_id == "QC02_TIME"
                       else "在编辑器里修正这一处后重新保存并再次登记；原文件已按原字节保留，没有被改写")
        issues.append(make_issue(revision_id, rule_id, "ERROR", f"第 {problem['line']} 行：{problem['message']}", recommended=recommended,
                                 cue_ids=[cue["cue_id"]] if cue else [], start_ms=cue["start_ms"] if cue else None, end_ms=cue["end_ms"] if cue else None,
                                 source_refs=[f"line:{problem['line']}"], observed=problem.get("observed")))
    return issues


def _bounds(revision_id: str, view: dict, media_record: dict, tolerance_ms: int) -> list[dict]:
    duration_ms = media_record["duration_ms"]
    issues = []
    for cue in _dialogues(view):
        if not _timed(cue) or cue["end_ms"] <= duration_ms:
            continue
        beyond = cue["end_ms"] - duration_ms
        observed = {"end_ms": cue["end_ms"], "media_duration_ms": duration_ms, "beyond_ms": beyond}
        if beyond > tolerance_ms:
            issues.append(make_issue(revision_id, "QC03_BOUNDS", "ERROR", f"第 {cue['line']} 行：字幕结束时间超出视频时长 {beyond} 毫秒",
                                     recommended="确认这份字幕对应的是这个视频；若视频剪辑过，请先把剪辑后的视频登记为新媒体。程序不会把时间钳到片尾，也不会削去文字",
                                     cue_ids=[cue["cue_id"]], start_ms=cue["start_ms"], end_ms=cue["end_ms"], source_refs=[f"line:{cue['line']}"], observed=observed,
                                     threshold={"tolerance_ms": tolerance_ms}))
        else:
            issues.append(make_issue(revision_id, "QC03_BOUNDS", "INFO", f"第 {cue['line']} 行：字幕结束时间比视频时长多 {beyond} 毫秒，在格式量化容差之内，原值保留",
                                     recommended="无需处理", cue_ids=[cue["cue_id"]], start_ms=cue["start_ms"], end_ms=cue["end_ms"], source_refs=[f"line:{cue['line']}"],
                                     observed=observed, threshold={"tolerance_ms": tolerance_ms}))
    return issues


def _order(revision_id: str, view: dict, revision_kind: str, human_file: bool) -> list[dict]:
    severity = "INFO" if human_file else "ERROR"
    issues = []
    if view["format"] == "srt":
        labels = [cue["notes"].get("index_label") for cue in view["cues"]]
        odd = [cue["cue_id"] for position, cue in enumerate(view["cues"], start=1) if labels[position - 1] != str(position)]
        if odd:
            issues.append(make_issue(revision_id, "QC06_ORDER", severity,
                                     f"有 {len(odd)} 条字幕的编号不是从 1 起连续的顺序号" + ("（人工文件的编号只作展示，不作身份；原文件已原样保留）" if human_file else ""),
                                     recommended="无需处理" if human_file else "机器字幕的编号必须连续，需要修正生成逻辑", cue_ids=_listed(odd), observed={"count": len(odd)}))
    timed = [cue for cue in _dialogues(view) if _timed(cue)]
    unsorted = [cue["cue_id"] for previous, cue in zip(timed, timed[1:]) if cue["start_ms"] < previous["start_ms"]]
    if unsorted:
        issues.append(make_issue(revision_id, "QC06_ORDER", severity,
                                 f"有 {len(unsorted)} 条字幕没有按开始时间排在前一条之后" + ("（人工文件的顺序原样保留，不重排）" if human_file else ""),
                                 recommended="无需处理；需要时可在编辑器里排序后另存再登记" if human_file else "机器字幕必须按规范顺序输出，需要修正生成逻辑",
                                 cue_ids=_listed(unsorted), observed={"count": len(unsorted)}))
    return issues


def _review_base(revision_id: str, base: dict) -> list[dict]:
    issues = []
    newer = base.get("superseded_by") or []
    if newer:
        issues.append(make_issue(revision_id, "QC20_REVIEW_BASE", "INFO", f"这份人工稿基于的版本之后又产生了 {len(newer)} 个更新的机器版本；人工稿仍绑定原来的基础版本",
                                 recommended="可以继续使用这份人工稿；想用新的机器结果时，从新版本另建校对副本。程序不会自动变基或合并",
                                 observed={"code": "BASE_SUPERSEDED", "base_revision_id": base.get("base_revision_id"), "newer_revision_ids": _listed(list(newer))}))
    provenance = base.get("provenance_status")
    if provenance in ("partial", "unmapped"):
        counts = base.get("counts") or {}
        reason = "没有可对照的基础版本" if not base.get("base_revision_id") else "有字幕经过改字、改时间、拆分或合并"
        issues.append(make_issue(revision_id, "QC20_REVIEW_BASE", "INFO", f"来源映射不完整（{provenance}）：{reason}，这些字幕与源文的对应没有被审计",
                                 recommended="这不妨碍编辑、播放和导出；需要追溯源文时请人工对照", observed={"provenance_status": provenance, **counts}, verification="measured"))
    diff = base.get("style_diff")
    if diff:
        parts = [f"{label} {len(diff[key])} 个" for key, label in (("styles_added", "新增样式"), ("styles_removed", "删除样式"), ("styles_changed", "修改样式"),
                                                                  ("script_info_changed", "脚本信息变动")) if diff.get(key)]
        issues.append(make_issue(revision_id, "QC20_REVIEW_BASE", "WARNING", "样式表与基础版本不同：" + "，".join(parts) + "。程序分不清是有意改样式还是编辑器丢了样式",
                                 recommended="请对照下面列出的样式名确认；若不是有意修改，请从基础版本重新建立校对副本", observed=diff))
    return issues


def _empty(revision_id: str, view: dict) -> list[dict]:
    if any(cue["text"].strip() for cue in _dialogues(view)):
        return []
    return [make_issue(revision_id, "QC23_EMPTY", "WARNING", "这份字幕没有任何可显示的文字", recommended="确认导入的是正确的文件；程序不会生成占位字幕",
                       observed={"dialogue_events": len(_dialogues(view)), "events": len(view["cues"])})]


# --------------------------------------------------------------------------- interface

def check_quality(quality_input: dict) -> dict:
    """QualityInput -> QualityReport (plan 6.8 / 11). Read-only: no file is written and the input is not modified."""
    item = require_record("QualityInput", quality_input)
    revision_id, view, extensions = item["revision_id"], item["parsed_view"], item["extensions"]
    media_record = item["media_record"]
    human_file = item["revision_kind"] == "review" or extensions.get("origin") == "human"
    issues = _parse_and_time(revision_id, view)
    unknown: list[str] = []
    if media_record is not None:
        tolerance = extensions.get("bounds_tolerance_ms")
        issues += _bounds(revision_id, view, media_record, BOUNDS_TOLERANCE_MS[view["format"]] if tolerance is None else int(tolerance))
    else:
        unknown.append("media_bounds")
        issues.append(make_issue(revision_id, "QC21_MEDIA_BINDING", "INFO", "没有绑定媒体：这次只做了字幕结构检视，没有核对时间是否落在视频之内",
                                 recommended="正式登记和导出前必须选定一个已登记的媒体版本", verification="unknown"))
    issues += _order(revision_id, view, item["revision_kind"], human_file)
    if isinstance(extensions.get("review_base"), dict):
        issues += _review_base(revision_id, extensions["review_base"])
    issues += _empty(revision_id, view)
    status = confirmation(revision_id, item["subtitle_ref"]["sha256"], media_record["media_id"] if media_record else None, view["format"], item["attestations"],
                          default="PARTIAL" if human_file else "UNREVIEWED")
    return require_record("QualityReport", {
        "schema_version": SCHEMA_VERSION, "revision_id": revision_id, "rule_set_version": int(item["rule_config"]["rule_set_version"]), "issues": issues,
        "unknown_capabilities": unknown, "structural_status": "invalid" if any(issue["severity"] == "ERROR" for issue in issues) else "valid",
        "human_status": status["human_status"], "confirmed_scopes": status["confirmed_scopes"], "generated_at": utc_now()})


__all__ = ["BOUNDS_TOLERANCE_MS", "REQUIRED_SCOPES", "HARD_RULES", "make_issue", "confirmation", "check_quality"]
