"""同评委可比对照（只读纯函数）。

在当前校准范围与逐项裁决形成的**最终分**基础上，对每一对选手逐项目比较：

- 每个项目只取双方在该项目上**共同评过分的评委**（评委集合的交集），
  分别求「同一评委集合」上的最终分均分（共同评委均分），两个均分使用
  **完全相同**的评委集合，因此差值不受双方评委覆盖不同、评委严宽差异的影响；
- 至少有一个项目存在共同评委时，该选手对为**可比**：可比总分差 =
  各可比项目共同评委均分之差逐项求和，据此给出**可比领先者**；
- 某项目任一方没有评分（缺项目）或该项目没有任何共同评委时，该项目列为
  **不可比**，既不按 0 分计入，也不参与差值汇总；全部项目都不可比时，
  整对选手列为**不可比**（同样不按 0 分）。

结果同时给出**全量口径**的方向：以当前分析结果的**最终名次**为准——
名次相同（主榜锚定并列组）即为**全量并列**，不构成反向；名次有先后时，再
与同评委可比总分差的方向对照，两者相反的选手对标为**反向**；不可比等情况
分别标注。注意不能直接用全量总分差落 ``TIE_EPS`` 容差判定并列：主榜并列组
以组最高分锚定（形如 90、90−0.6e-9、90−1.2e-9 的名次为 1、1、3），组外两
人的分差可能仍在容差内，但名次已有先后，此时不构成全量并列。逐项可比均分
与不可比规则沿用现有口径。整个计算只读，不触碰评分、裁决或会话；评分、裁
决或校准范围变化后由调用方重新计算。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .scoring import TIE_EPS

# 选手对的可比性。
PAIR_COMPARABLE = "comparable"          # 至少有一个可比项目
PAIR_INCOMPARABLE = "incomparable"      # 全部项目都不可比（不按 0 分）
# 不可比原因（逐项目 / 整对）。
REASON_MISSING_ITEM = "missing_item"    # 一方在该项目没有任何评分
REASON_NO_COMMON_JUDGE = "no_common_judge"  # 双方都有该项目，但无共同评委
# 全量口径方向 vs 同评委可比方向 的关系。
DIR_SAME = "same"                       # 两方向一致
DIR_REVERSED = "reversed"               # 两方向相反（重点标注）
DIR_FULL_TIE = "full_tie"               # 全量最终名次并列（无方向可对照）
DIR_PAIR_INCOMPARABLE = "incomparable"  # 该选手对不可比（无同评委方向）
# 逐项目可比领先者。
LEAD_A = "a"
LEAD_B = "b"
LEAD_TIE = "tie"

PAIR_STATUS_LABELS = {
    PAIR_COMPARABLE: "可比",
    PAIR_INCOMPARABLE: "不可比",
}
ITEM_REASON_LABELS = {
    REASON_MISSING_ITEM: "一方缺该项目",
    REASON_NO_COMMON_JUDGE: "无共同评委",
}
DIRECTION_LABELS = {
    DIR_SAME: "同向",
    DIR_REVERSED: "反向",
    DIR_FULL_TIE: "全量并列",
    DIR_PAIR_INCOMPARABLE: "不可比",
}
LEADER_LABELS = {
    LEAD_A: "A 领先",
    LEAD_B: "B 领先",
    LEAD_TIE: "持平",
}


def _leader(delta: float) -> str:
    if delta > TIE_EPS:
        return LEAD_A
    if delta < -TIE_EPS:
        return LEAD_B
    return LEAD_TIE


def _rank_leader(rank_a: int, rank_b: int) -> str:
    """全量口径领先者：以当前分析结果的**最终名次**为准。

    竞赛名次数字越小越靠前；名次相同（主榜锚定并列组）即为全量并列。
    这里不能用全量总分差落容差：锚定组外两人的分差仍可能在 ``TIE_EPS`` 内，
    但名次已有先后（如 1、1、3 中的第 2 与第 3 名），不构成并列。
    """
    if rank_a < rank_b:
        return LEAD_A
    if rank_b < rank_a:
        return LEAD_B
    return LEAD_TIE


def _direction(full_lead: str, pair_comparable: bool, comp_lead: str | None) -> str:
    if not pair_comparable:
        return DIR_PAIR_INCOMPARABLE
    if full_lead == LEAD_TIE:
        # 全量最终名次并列：不构成反向。
        return DIR_FULL_TIE
    if comp_lead == LEAD_TIE:
        # 可比总分差持平：全量方向在同评委口径下没有反向，按非反向处理。
        return DIR_SAME
    return DIR_SAME if full_lead == comp_lead else DIR_REVERSED


def common_judge_comparison(analysis: Mapping[str, Any]) -> dict[str, Any]:
    """基于一次 ``analyze`` 结果计算全部选手对的同评委可比对照（纯函数，只读）。

    只使用 ``analysis`` 中的 ``items``、各选手的 ``eff_total`` / ``eff_rank``
    与逐项目 ``eff_avg`` 及逐条评分的 ``judge`` / ``effective``（最终分）。
    """
    contestants = list(analysis["contestants"])  # 已按最终名次排序
    items = list(analysis["items"])

    # contestant -> item -> {judge: effective}，并保留逐项目全量最终均分。
    item_scores: dict[str, dict[str, dict[str, float]]] = {}
    item_eff_avg: dict[str, dict[str, float]] = {}
    full_total: dict[str, float] = {}
    eff_rank: dict[str, int] = {}
    for block in contestants:
        name = block["contestant"]
        full_total[name] = float(block["eff_total"])
        eff_rank[name] = int(block["eff_rank"])
        item_scores[name] = {}
        item_eff_avg[name] = {}
        for ib in block["items"]:
            item_scores[name][ib["item"]] = {
                str(s["judge"]): float(s["effective"]) for s in ib["scores"]
            }
            item_eff_avg[name][ib["item"]] = float(ib["eff_avg"])

    pairs: list[dict[str, Any]] = []
    totals = {
        "pairs": 0,
        "comparable": 0,
        "incomparable": 0,
        "reversed": 0,
        "same_direction": 0,
        "full_tie": 0,
        "a_leads": 0,
        "b_leads": 0,
        "ties": 0,
    }
    incomparable_reasons = {REASON_MISSING_ITEM: 0, REASON_NO_COMMON_JUDGE: 0}
    comparable_item_total = 0

    for ia, block_a in enumerate(contestants):
        name_a = block_a["contestant"]
        for block_b in contestants[ia + 1:]:
            name_b = block_b["contestant"]
            totals["pairs"] += 1
            rows: list[dict[str, Any]] = []
            comparable_items = 0

            for item in items:
                scores_a = item_scores[name_a].get(item)
                scores_b = item_scores[name_b].get(item)
                if scores_a is None or scores_b is None:
                    both_missing = scores_a is None and scores_b is None
                    missing_side = (
                        None if both_missing
                        else name_a if scores_a is None
                        else name_b
                    )
                    rows.append(
                        {
                            "item": item,
                            "comparable": False,
                            "reason": REASON_MISSING_ITEM,
                            "reason_label": ITEM_REASON_LABELS[REASON_MISSING_ITEM],
                            "missing_side": missing_side,
                            "both_missing": both_missing,
                            "a_has_item": scores_a is not None,
                            "b_has_item": scores_b is not None,
                            "common_judges": [],
                            "common_count": 0,
                            "a_avg": None,
                            "b_avg": None,
                            "delta": None,
                            "leader": None,
                            "a_full_avg": item_eff_avg[name_a].get(item),
                            "b_full_avg": item_eff_avg[name_b].get(item),
                        }
                    )
                    continue

                # 双方都有该项目：共同评委以全体评委的首次出现顺序为准。
                common_judges = [
                    j for j in analysis["judges"]
                    if j in scores_a and j in scores_b
                ]
                if not common_judges:
                    reason = REASON_NO_COMMON_JUDGE
                    rows.append(
                        {
                            "item": item,
                            "comparable": False,
                            "reason": reason,
                            "reason_label": ITEM_REASON_LABELS[reason],
                            "missing_side": None,
                            "both_missing": False,
                            "a_has_item": True,
                            "b_has_item": True,
                            "common_judges": [],
                            "common_count": 0,
                            "a_avg": None,
                            "b_avg": None,
                            "delta": None,
                            "leader": None,
                            # 明细用：双方在该项目各自的全部评委覆盖（最终分）。
                            "a_judges": [
                                {"judge": j, "effective": scores_a[j]}
                                for j in analysis["judges"] if j in scores_a
                            ],
                            "b_judges": [
                                {"judge": j, "effective": scores_b[j]}
                                for j in analysis["judges"] if j in scores_b
                            ],
                            "a_full_avg": item_eff_avg[name_a].get(item),
                            "b_full_avg": item_eff_avg[name_b].get(item),
                        }
                    )
                    continue

                a_vals = [scores_a[j] for j in common_judges]
                b_vals = [scores_b[j] for j in common_judges]
                a_avg = sum(a_vals) / len(a_vals)
                b_avg = sum(b_vals) / len(b_vals)
                delta = a_avg - b_avg
                comparable_items += 1
                rows.append(
                    {
                        "item": item,
                        "comparable": True,
                        "reason": None,
                        "reason_label": None,
                        "missing_side": None,
                        "both_missing": False,
                        "a_has_item": True,
                        "b_has_item": True,
                        "common_judges": list(common_judges),
                        "common_count": len(common_judges),
                        "a_avg": a_avg,
                        "b_avg": b_avg,
                        "delta": delta,
                        "leader": _leader(delta),
                        # 明细用：双方在该项目各自的全部评委覆盖（最终分）。
                        "a_judges": [
                            {"judge": j, "effective": scores_a[j]}
                            for j in analysis["judges"] if j in scores_a
                        ],
                        "b_judges": [
                            {"judge": j, "effective": scores_b[j]}
                            for j in analysis["judges"] if j in scores_b
                        ],
                        "a_full_avg": item_eff_avg[name_a][item],
                        "b_full_avg": item_eff_avg[name_b][item],
                    }
                )

            is_comparable = comparable_items > 0
            full_delta = full_total[name_a] - full_total[name_b]
            # 全量口径领先/并列以当前分析结果的最终名次为准（锚定并列组），
            # 不用 full_delta 落 TIE_EPS 容差。
            full_lead = _rank_leader(eff_rank[name_a], eff_rank[name_b])
            if is_comparable:
                comp_a_total = sum(
                    r["a_avg"] for r in rows if r["comparable"]
                )
                comp_b_total = sum(
                    r["b_avg"] for r in rows if r["comparable"]
                )
                comp_delta = comp_a_total - comp_b_total
                comp_leader = _leader(comp_delta)
                direction = _direction(full_lead, True, comp_leader)
            else:
                comp_a_total = comp_b_total = None
                comp_delta = None
                comp_leader = None
                direction = DIR_PAIR_INCOMPARABLE

            # 汇总不可比原因（整对不可比时才计入对级原因统计；逐项目始终可见）。
            if not is_comparable:
                used: set[str] = set()
                for r in rows:
                    if not r["comparable"] and r["reason"] not in used:
                        incomparable_reasons[r["reason"]] += 1
                        used.add(r["reason"])

            status = PAIR_COMPARABLE if is_comparable else PAIR_INCOMPARABLE
            if is_comparable:
                totals["comparable"] += 1
                comparable_item_total += comparable_items
                totals[
                    {"a": "a_leads", "b": "b_leads", "tie": "ties"}[comp_leader]
                ] += 1
            else:
                totals["incomparable"] += 1
            if direction == DIR_REVERSED:
                totals["reversed"] += 1
            elif direction == DIR_SAME:
                totals["same_direction"] += 1
            elif direction == DIR_FULL_TIE:
                totals["full_tie"] += 1

            pairs.append(
                {
                    "a": name_a,
                    "b": name_b,
                    "status": status,
                    "status_label": PAIR_STATUS_LABELS[status],
                    "comparable": is_comparable,
                    "a_rank": eff_rank[name_a],
                    "b_rank": eff_rank[name_b],
                    "a_full_total": full_total[name_a],
                    "b_full_total": full_total[name_b],
                    "full_delta": full_delta,
                    "full_leader": full_lead,
                    "comparable_items": comparable_items,
                    "incomparable_items": len(items) - comparable_items,
                    "a_comparable_total": comp_a_total,
                    "b_comparable_total": comp_b_total,
                    "comparable_delta": comp_delta,
                    "comparable_leader": comp_leader,
                    "direction": direction,
                    "direction_label": DIRECTION_LABELS[direction],
                    "reversed": direction == DIR_REVERSED,
                    "items": rows,
                }
            )

    scope = analysis.get("scope", "")
    return {
        "scope": scope,
        "scope_label": analysis.get("scope_label") or scope,
        "tie_eps": TIE_EPS,
        "status_labels": dict(PAIR_STATUS_LABELS),
        "item_reason_labels": dict(ITEM_REASON_LABELS),
        "direction_labels": dict(DIRECTION_LABELS),
        "leader_labels": dict(LEADER_LABELS),
        "summary": {
            **totals,
            "contestants": len(contestants),
            "items": len(items),
            "comparable_item_total": comparable_item_total,
            "incomparable_missing_item_pairs": incomparable_reasons[
                REASON_MISSING_ITEM
            ],
            "incomparable_no_common_pairs": incomparable_reasons[
                REASON_NO_COMMON_JUDGE
            ],
        },
        "pairs": pairs,
    }
