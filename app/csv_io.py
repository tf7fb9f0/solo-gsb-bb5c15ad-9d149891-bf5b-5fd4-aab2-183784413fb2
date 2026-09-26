"""CSV parsing and audit export."""
from __future__ import annotations

import csv
import io
from typing import Any, Mapping, Sequence

COLUMN_ALIASES = {
    "contestant": {
        "选手", "选手编号", "选手id", "选手姓名", "姓名", "名称", "参赛者",
        "contestant", "competitor", "player", "name",
    },
    "judge": {
        "评委", "评委编号", "评委姓名", "裁判", "评审",
        "judge", "juror", "rater", "evaluator",
    },
    "item": {
        "项目", "项目编号", "项目名称", "比赛项目", "赛项", "环节",
        "item", "event", "project", "category", "round",
    },
    "score": {
        "分数", "得分", "评分", "分值", "成绩",
        "score", "points", "point", "mark", "marks",
    },
}

REQUIRED = ("contestant", "judge", "item", "score")

HEADER_LABELS = {
    "contestant": "选手",
    "judge": "评委",
    "item": "项目",
    "score": "分数",
}


class CSVParseError(ValueError):
    pass


def _norm(text: str) -> str:
    return (text or "").replace("﻿", "").strip().lower()


def parse_scores(content: bytes | str) -> list[dict[str, Any]]:
    """Parse an uploaded CSV into a list of score records.

    Accepts English or Chinese headers (see COLUMN_ALIASES). Extra columns
    are ignored. Raises CSVParseError with a human-readable Chinese message.
    """
    if isinstance(content, bytes):
        text = content.decode("utf-8-sig")
    else:
        text = content.lstrip("﻿")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise CSVParseError("CSV 文件为空，请包含表头与至少一行数据。")

    mapping: dict[int, str] = {}
    for idx, col in enumerate(header):
        key = _norm(col)
        for field, aliases in COLUMN_ALIASES.items():
            if key in {_norm(a) for a in aliases}:
                mapping[idx] = field

    col_index = {field: idx for idx, field in mapping.items()}
    missing = [f for f in REQUIRED if f not in col_index]
    if missing:
        raise CSVParseError(
            "缺少必需列: "
            + "、".join(HEADER_LABELS[m] for m in missing)
            + "。识别到的表头: "
            + (", ".join(h.strip() for h in header) if header else "(空)")
        )

    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for line_no, row in enumerate(reader, start=2):
        if not row or all(not str(c).strip() for c in row):
            continue
        try:
            contestant = row[col_index["contestant"]].strip()
            judge = row[col_index["judge"]].strip()
            item = row[col_index["item"]].strip()
            score_raw = row[col_index["score"]].strip()
        except IndexError:
            raise CSVParseError(f"第 {line_no} 行列数不足，请检查是否存在错列。")

        for name, value in (("选手", contestant), ("评委", judge), ("项目", item)):
            if not value:
                raise CSVParseError(f"第 {line_no} 行的「{name}」不能为空。")
        if not score_raw:
            raise CSVParseError(f"第 {line_no} 行的「分数」不能为空。")
        try:
            score = float(score_raw)
        except ValueError:
            raise CSVParseError(f"第 {line_no} 行的分数「{score_raw}」不是数字。")
        if score != score or score in (float("inf"), float("-inf")):
            raise CSVParseError(f"第 {line_no} 行的分数无效。")

        triple = (contestant, judge, item)
        if triple in seen:
            raise CSVParseError(
                f"第 {line_no} 行存在重复评分（选手={contestant}，评委={judge}，项目={item}）。"
            )
        seen.add(triple)
        records.append(
            {
                "contestant": contestant,
                "judge": judge,
                "item": item,
                "score": score,
            }
        )

    if not records:
        raise CSVParseError("未解析到任何分数行，请检查 CSV 内容。")
    return records


def _r4(value: float) -> float:
    return round(float(value), 4)


def build_audit_csv(analysis: Mapping[str, Any]) -> bytes:
    """Build a single multi-section audit CSV (utf-8-sig, Excel friendly)."""
    buf = io.StringIO()
    w = csv.writer(buf)

    scope = analysis["scope"]
    scope_name = "按评委×项目" if scope == "judge_item" else "按评委（跨项目）"
    ref = analysis["reference"]

    w.writerow(["多评委评分校准 — 审计明细"])
    w.writerow(["校准范围", scope_name])
    w.writerow(
        [
            "稳健标准化公式",
            f"Z = (原始分 - 中位数) / (MAD × {ref['mad_k']})；"
            f"校准分 = 全局中位数 {_r4(ref['global_median'])} + Z × 全局MAD {_r4(ref['global_mad'])}",
        ]
    )
    w.writerow([])

    # Section 1: per-score rows.
    header = [
        "选手", "项目", "评委", "原始分", "分组中位数", "分组MAD(原始)",
        "分组MAD(×1.4826)", "稳健Z", "校准分", "采用选择", "最终分", "理由",
    ]
    w.writerow(["【明细】逐条评分与人工裁决"])
    w.writerow(header)
    for block in analysis["contestants"]:
        contestant = block["contestant"]
        for ib in block["items"]:
            for s in ib["scores"]:
                choice = "保留原分" if s["use_raw"] else "采用校准分"
                w.writerow(
                    [
                        contestant,
                        ib["item"],
                        s["judge"],
                        _r4(s["raw"]),
                        _r4(s["group_median"]),
                        _r4(s["group_raw_mad"]),
                        _r4(s["group_mad"]),
                        round(s["z"], 3),
                        _r4(s["calibrated"]),
                        choice,
                        _r4(s["effective"]),
                        s["reason"],
                    ]
                )
    w.writerow([])

    # Section 2: contestant totals and ranks.
    w.writerow(["【汇总】选手总分与名次（总分 = 各项目内评委平均分之和）"])
    w.writerow(
        [
            "选手", "参评项目数", "原始总分", "原始名次", "校准总分",
            "校准名次", "名次变化(正=上升)", "最终总分", "最终名次", "保留原分条数",
        ]
    )
    for block in analysis["contestants"]:
        delta = block["rank_delta"]
        delta_text = ("上升 " + str(delta)) if delta > 0 else (("下降 " + str(-delta)) if delta < 0 else "持平")
        w.writerow(
            [
                block["contestant"],
                block["item_count"],
                _r4(block["raw_total"]),
                block["raw_rank"],
                _r4(block["cal_total"]),
                block["cal_rank"],
                delta_text,
                _r4(block["eff_total"]),
                block["eff_rank"],
                block["raw_overrides"],
            ]
        )
    w.writerow([])

    # Section 3: reversal pairs.
    w.writerow(["【反转】标准化前后名次发生反转的选手对"])
    reversals: Sequence[Mapping[str, Any]] = analysis["reversals"]
    if reversals:
        w.writerow(
            [
                "原始优胜者", "原始名次", "被反超者", "被反超者原始名次",
                "校准后优胜者", "优胜者校准名次", "被反超者校准名次",
                "原始分差", "校准后反向分差",
            ]
        )
        for rv in reversals:
            w.writerow(
                [
                    rv["winner_before"],
                    rv["raw_rank_before"],
                    rv["winner_after"],
                    rv["raw_rank_loser"],
                    rv["winner_after"],
                    rv["cal_rank_winner"],
                    rv["cal_rank_loser"],
                    _r4(rv["raw_gap"]),
                    _r4(rv["cal_gap"]),
                ]
            )
    else:
        w.writerow(["未检测到名次反转"])
    w.writerow([])

    # Section 4: group statistics.
    w.writerow(["【校准参数】各校准分组中位数与 MAD"])
    if scope == "judge_item":
        w.writerow(["评委", "项目", "评分条数", "中位数", "MAD(原始)", "MAD(×1.4826)"])
        for g in analysis["groups"]:
            w.writerow([g["judge"], g.get("item", ""), g["n"], _r4(g["median"]), _r4(g["raw_mad"]), _r4(g["mad"])])
    else:
        w.writerow(["评委", "评分条数", "中位数", "MAD(原始)", "MAD(×1.4826)"])
        for g in analysis["groups"]:
            w.writerow([g["judge"], g["n"], _r4(g["median"]), _r4(g["raw_mad"]), _r4(g["mad"])])
    w.writerow([])

    # Section 5: persisted batch adjudication rules.
    w.writerow(["【批处理裁决规则】按优先级数字从小到大裁决（数字相同时按规则顺序前者优先）"])
    batch_rules: Sequence[Mapping[str, Any]] = analysis.get("batch_rules", [])
    if batch_rules:
        w.writerow(
            [
                "序号", "优先级", "规则名称", "评委(空=不限)", "项目(空=不限)",
                "原始分下限", "原始分上限", "校准分下限", "校准分上限",
                "采用选择", "理由模板",
            ]
        )

        def _b(v):
            return "" if v is None else _r4(v)

        for pos, rule in enumerate(batch_rules, start=1):
            w.writerow(
                [
                    pos,
                    rule["priority"],
                    rule.get("name", ""),
                    rule.get("judge") or "",
                    rule.get("item") or "",
                    _b(rule.get("raw_min")),
                    _b(rule.get("raw_max")),
                    _b(rule.get("cal_min")),
                    _b(rule.get("cal_max")),
                    "保留原分" if rule["use_raw"] else "采用校准分",
                    rule.get("reason_template", ""),
                ]
            )
    else:
        w.writerow(["（未保存批处理裁决规则）"])

    return ("﻿" + buf.getvalue()).encode("utf-8")
