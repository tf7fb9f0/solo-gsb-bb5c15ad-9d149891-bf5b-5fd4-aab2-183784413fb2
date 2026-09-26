"""晋级门槛检查（只读纯函数）。

在当前校准范围与逐项裁决形成的**项目最终均分 / 最终总分**基础上，按用户给定
的一个或多个「项目最终均分最低线」（可选再给一条「最终总分线」），逐位判断
选手是否达标：

- ``pass``（达标）：所有设门槛项目都有评分且均分不低于对应最低线，
  且（若设置总分线）最终总分不低于总分线；
- ``fail``（未达标）：设门槛项目均有评分，但至少一项均分低于最低线，
  或总分低于总分线，逐条给出原因（含差距）；
- ``insufficient_data``（数据不足）：缺少任一被设门槛的项目，列出缺失项目；
  **绝不按 0 分处理**，也不再判定其总分线。

整个计算只读，不触碰评分、裁决或会话；评分、裁决或校准范围变化后由调用方
重新计算。
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from .scoring import SCOPE_LABELS, TIE_EPS

PROMO_PASS = "pass"
PROMO_FAIL = "fail"
PROMO_INSUFFICIENT = "insufficient_data"
PROMO_STATUSES = (PROMO_PASS, PROMO_FAIL, PROMO_INSUFFICIENT)
PROMO_STATUS_LABELS = {
    PROMO_PASS: "达标",
    PROMO_FAIL: "未达标",
    PROMO_INSUFFICIENT: "数据不足",
}

REASON_MISSING_ITEMS = "missing_items"
REASON_ITEM_BELOW = "item_below"
REASON_TOTAL_BELOW = "total_below"


class PromotionValidationError(ValueError):
    """请求参数不合法（未设项目门槛、项目不存在/重复、数值非法等）。"""


def _f2(value: float) -> str:
    return f"{value:.2f}"


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PromotionValidationError(f"{label}必须是数字。")
    value = float(value)
    if not math.isfinite(value):
        raise PromotionValidationError(f"{label}必须是有限数字（不接受 NaN/Infinity）。")
    return value


def promotion_check(
    analysis: Mapping[str, Any],
    item_thresholds: Sequence[Mapping[str, Any]],
    total_threshold: float | None = None,
) -> dict[str, Any]:
    """基于一次 ``analyze`` 结果执行只读的晋级门槛检查。

    ``item_thresholds`` 为 ``[{"item": 项目名, "min_avg": 最低均分}, ...]``，
    至少一条；``total_thresholds`` 为可选的最终总分最低线。
    """
    if not isinstance(item_thresholds, Sequence) or isinstance(item_thresholds, (str, bytes)):
        raise PromotionValidationError("item_thresholds 必须是门槛数组。")

    normalized: list[dict[str, float | str]] = []
    seen: set[str] = set()
    for idx, entry in enumerate(item_thresholds, start=1):
        if not isinstance(entry, Mapping):
            raise PromotionValidationError(f"第 {idx} 个项目门槛格式不正确。")
        item = entry.get("item")
        if not isinstance(item, str) or not item.strip():
            raise PromotionValidationError(f"第 {idx} 个项目门槛缺少项目名。")
        item = item.strip()
        if item in seen:
            raise PromotionValidationError(f"项目「{item}」的最低线重复设置，请合并为一条。")
        seen.add(item)
        min_avg = _finite_number(entry.get("min_avg"), f"项目「{item}」的最低均分")
        normalized.append({"item": item, "min_avg": min_avg})

    if not normalized:
        raise PromotionValidationError("请至少为一个项目填写最终项目均分最低线。")

    items = list(analysis["items"])
    unknown = [str(e["item"]) for e in normalized if e["item"] not in items]
    if unknown:
        avail = "、".join(items) if items else "（当前没有项目）"
        raise PromotionValidationError(
            f"项目「{'、'.join(unknown)}」不存在，可选：{avail}。"
        )

    total_line: float | None = None
    if total_threshold is not None:
        total_line = _finite_number(total_threshold, "最终总分最低线")

    threshold_order = [str(e["item"]) for e in normalized]
    threshold_map = {str(e["item"]): float(e["min_avg"]) for e in normalized}

    rows: list[dict[str, Any]] = []
    counts = {PROMO_PASS: 0, PROMO_FAIL: 0, PROMO_INSUFFICIENT: 0}

    # blocks 已按最终名次排序，结果保持该次序。
    for block in analysis["contestants"]:
        avgs = {ib["item"]: float(ib["eff_avg"]) for ib in block["items"]}
        present = set(avgs)
        missing_items = [it for it in threshold_order if it not in present]
        total = float(block["eff_total"])

        item_cells = []
        for it in items:
            line = threshold_map.get(it)
            has_data = it in present
            if line is None:
                passes: bool | None = None
            else:
                passes = has_data and avgs[it] + TIE_EPS >= line
            item_cells.append(
                {
                    "item": it,
                    "avg": avgs.get(it),
                    "has_data": has_data,
                    "threshold": line,
                    "passes": passes,
                }
            )

        reasons: list[dict[str, Any]] = []
        total_check = None
        if missing_items:
            # 数据不足：缺失的是被设门槛的项目，不按 0 分处理，也不再判定总分线。
            status = PROMO_INSUFFICIENT
            reasons.append(
                {
                    "kind": REASON_MISSING_ITEMS,
                    "items": missing_items,
                    "message": "缺少被设门槛的项目：" + "、".join(missing_items) + "（数据不足，不按 0 分处理）",
                }
            )
        else:
            below = []
            for it in threshold_order:
                line = threshold_map[it]
                avg = avgs[it]
                if avg + TIE_EPS < line:
                    below.append(
                        {
                            "item": it,
                            "avg": avg,
                            "threshold": line,
                            "gap": line - avg,
                        }
                    )
            if below:
                detail = "；".join(
                    f"「{d['item']}」最终均分 {_f2(d['avg'])} 低于最低线 {_f2(d['threshold'])}"
                    f"（差 {_f2(d['gap'])}）"
                    for d in below
                )
                reasons.append(
                    {
                        "kind": REASON_ITEM_BELOW,
                        "items": below,
                        "message": "项目均分低于门槛：" + detail,
                    }
                )
            if total_line is not None:
                total_ok = total + TIE_EPS >= total_line
                total_check = {
                    "threshold": total_line,
                    "passes": total_ok,
                    "gap": max(0.0, total_line - total),
                }
                if not total_ok:
                    reasons.append(
                        {
                            "kind": REASON_TOTAL_BELOW,
                            "total": total,
                            "threshold": total_line,
                            "gap": total_line - total,
                            "message": f"最终总分 {_f2(total)} 低于总分线 {_f2(total_line)}"
                            f"（差 {_f2(total_line - total)}）",
                        }
                    )
            status = PROMO_FAIL if reasons else PROMO_PASS

        counts[status] += 1
        rows.append(
            {
                "contestant": block["contestant"],
                "rank": block["eff_rank"],
                "status": status,
                "status_label": PROMO_STATUS_LABELS[status],
                "total": total,
                "total_threshold": total_line,
                "total_check": total_check,
                "items": item_cells,
                "missing_items": missing_items,
                "reasons": reasons,
            }
        )

    scope = analysis.get("scope", "")
    return {
        "scope": scope,
        "scope_label": analysis.get("scope_label") or SCOPE_LABELS.get(scope, scope),
        "status_labels": dict(PROMO_STATUS_LABELS),
        "thresholds": {"items": normalized, "total": total_line},
        "summary": {"contestants": len(rows), **counts},
        "contestants": rows,
    }
