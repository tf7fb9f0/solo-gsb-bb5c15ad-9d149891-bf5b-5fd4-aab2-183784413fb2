"""待补评分名次边界（只读纯函数）。

在当前校准范围与逐项裁决形成的**最终分**基础上，选定一个项目、目标评委人数
与待补评分的最终分上下限：每位选手在该项目的评分一律补足至目标人数，每条
待补分可**独立**取闭区间 ``[score_min, score_max]`` 内的任意值。由此：

- 该项目均分区间 = （已有最终分之和 + 缺口 × 下/上限） / 目标人数；
- 最终总分区间 = 其余项目保持现值（当前最终总分 − 该项目当前最终均分，
  缺失该项目时不减）+ 该项目均分区间；
- 可达到的最好名次：本人总分取**上限**、他人总分取**下限**时的竞赛名次；
- 可达到的最差名次：本人总分取**下限**、他人总分取**上限**时的竞赛名次。

两种极端情形的总分向量都直接交给主排名函数 :func:`app.scoring.competition_rank`
计算名次，并列组按组最高分锚定（同分容差沿用主排名的 ``TIE_EPS``），与主排名
口径完全一致，不会发生相邻成绩比较导致的同分链式扩散。若任一选手在该项目的
已有评分数超过目标人数，整个请求被拒绝（由调用方转为 400）。

整个计算只读，不触碰评分、裁决或会话；评分、裁决或校准范围变化后由调用方
重新计算。
"""
from __future__ import annotations

import math
from typing import Any, Mapping

from .scoring import SCOPE_LABELS, competition_rank

MAX_TARGET = 1_000_000


class PendingRankValidationError(ValueError):
    """请求参数不合法（项目不存在、目标人数/区间非法、已有评分超过目标等）。"""


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PendingRankValidationError(f"{label}必须是数字。")
    value = float(value)
    if not math.isfinite(value):
        raise PendingRankValidationError(f"{label}必须是有限数字（不接受 NaN/Infinity）。")
    return value


def _target_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PendingRankValidationError("目标评委人数必须是整数。")
    if value < 1:
        raise PendingRankValidationError("目标评委人数至少为 1。")
    if value > MAX_TARGET:
        raise PendingRankValidationError(f"目标评委人数不能超过 {MAX_TARGET}。")
    return value


def pending_rank_bounds(
    analysis: Mapping[str, Any],
    item: Any,
    target: Any,
    score_min: Any,
    score_max: Any,
) -> dict[str, Any]:
    """计算选定项目补足评分后的总分区间与可达到名次区间（纯函数，只读）。

    ``analysis`` 为 :func:`app.scoring.analyze` 的输出（只使用其中的
    ``scope``、``items`` 与每位选手的 ``eff_total`` / ``eff_rank`` / 逐项目
    ``eff_avg`` 与逐条 ``effective`` 最终分）。
    """
    if isinstance(item, str):
        item = item.strip()
    if not item:
        raise PendingRankValidationError("请选择要分析的项目。")
    goal = _target_int(target)
    lo = _finite_number(score_min, "待补评分下限")
    hi = _finite_number(score_max, "待补评分上限")
    if lo > hi:
        raise PendingRankValidationError(
            f"待补评分下限（{lo}）不能大于上限（{hi}）。"
        )

    items = list(analysis["items"])
    if item not in items:
        avail = "、".join(items) if items else "（当前没有项目）"
        raise PendingRankValidationError(f"项目「{item}」不存在，可选：{avail}。")

    blocks = list(analysis["contestants"])

    # 先汇总每位选手在该项目的已有最终分，并校验没有任何选手超过目标人数。
    per: dict[str, dict[str, Any]] = {}
    overflow: list[tuple[str, int]] = []
    for b in blocks:
        name = b["contestant"]
        ib = next((x for x in b["items"] if x["item"] == item), None)
        if ib is None:
            per[name] = {"count": 0, "sum": 0.0, "avg": None}
            continue
        n = len(ib["scores"])
        per[name] = {
            "count": n,
            "sum": sum(float(s["effective"]) for s in ib["scores"]),
            "avg": float(ib["eff_avg"]),
        }
        if n > goal:
            overflow.append((name, n))
    if overflow:
        detail = "、".join(f"「{name}」已有 {n} 条" for name, n in overflow)
        raise PendingRankValidationError(
            f"目标评委人数 {goal} 小于以下选手在项目「{item}」的已有评分数：{detail}；"
            "请提高目标评委人数。"
        )

    rows: list[dict[str, Any]] = []
    for b in blocks:
        name = b["contestant"]
        info = per[name]
        gap = goal - info["count"]
        avg_min = (info["sum"] + gap * lo) / goal
        avg_max = (info["sum"] + gap * hi) / goal
        # 其余项目保持现值：从当前最终总分中剔除该项目当前贡献后再加区间。
        base = float(b["eff_total"]) - (info["avg"] or 0.0)
        rows.append(
            {
                "contestant": name,
                "rank": b["eff_rank"],
                "current_total": float(b["eff_total"]),
                "has_item": info["avg"] is not None,
                "existing_count": info["count"],
                "gap": gap,
                "existing_sum": info["sum"],
                "current_item_avg": info["avg"],
                "item_avg_min": avg_min,
                "item_avg_max": avg_max,
                "total_min": base + avg_min,
                "total_max": base + avg_max,
            }
        )

    # 竞赛名次（1224）：构造最好/最差两个极端情形的总分向量，直接交由主排名
    # 函数计算，并列组锚定与同分容差因此与主排名完全一致（避免按相邻成绩
    # 比较造成的同分链式扩散）。最好情形：本人取总分上限、他人取下限；
    # 最差情形：本人取下限、他人取上限。
    for row in rows:
        name = row["contestant"]
        best_vec = {
            r["contestant"]: (r["total_max"] if r is row else r["total_min"])
            for r in rows
        }
        worst_vec = {
            r["contestant"]: (r["total_min"] if r is row else r["total_max"])
            for r in rows
        }
        row["best_rank"] = competition_rank(best_vec.items())[name]
        row["worst_rank"] = competition_rank(worst_vec.items())[name]

    scope = analysis.get("scope", "")
    return {
        "scope": scope,
        "scope_label": analysis.get("scope_label") or SCOPE_LABELS.get(scope, scope),
        "item": item,
        "target": goal,
        "score_min": lo,
        "score_max": hi,
        "summary": {
            "contestants": len(rows),
            "contestants_with_gap": sum(1 for r in rows if r["gap"] > 0),
            "gap_total": sum(r["gap"] for r in rows),
            "missing_item": sum(1 for r in rows if not r["has_item"]),
        },
        "contestants": rows,
    }
