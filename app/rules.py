"""Batch adjudication rules: condition matching, conflict resolution by priority.

A rule selects scores by an optional judge, item, raw-score interval and
calibrated-score interval (empty / ``None`` means "any" for that dimension).
Each matching score receives a verdict (keep raw or use calibrated) together
with a rendered reason template.

When several rules match the same score the conflict is resolved by
**priority**: the rule with the smallest ``priority`` value wins; ties are
broken by the rule's position in the submitted list (first wins). Matching is
a pure function over the analyzed scores, so previews never touch sessions.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

USE_CALIBRATED = "calibrated"
USE_RAW = "raw"
CHOICES = (USE_CALIBRATED, USE_RAW)

REASON_PLACEHOLDERS = {"judge", "item", "raw", "calibrated"}


class RuleValidationError(ValueError):
    """Raised when a rule definition is invalid."""

    def __str__(self) -> str:  # noqa: D401 - Chinese message is the payload
        return self.args[0] if self.args else super().__str__()


def _as_optional_float(value: Any, field: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise RuleValidationError(f"{field} 必须是数字。")
    if num != num or num in (float("inf"), float("-inf")):
        raise RuleValidationError(f"{field} 必须是有限数字。")
    return num


def normalize_rule(raw: Mapping[str, Any], index: int) -> dict[str, Any]:
    """Validate one user-supplied rule and normalize its fields."""
    where = f"第 {index} 条规则"
    judge = (raw.get("judge") or "").strip() or None
    item = (raw.get("item") or "").strip() or None

    raw_min = _as_optional_float(raw.get("raw_min"), f"{where}的原始分下限")
    raw_max = _as_optional_float(raw.get("raw_max"), f"{where}的原始分上限")
    cal_min = _as_optional_float(raw.get("cal_min"), f"{where}的校准分下限")
    cal_max = _as_optional_float(raw.get("cal_max"), f"{where}的校准分上限")
    if raw_min is not None and raw_max is not None and raw_min > raw_max:
        raise RuleValidationError(f"{where}的原始分区间无效：下限不能大于上限。")
    if cal_min is not None and cal_max is not None and cal_min > cal_max:
        raise RuleValidationError(f"{where}的校准分区间无效：下限不能大于上限。")

    use_raw = bool(raw.get("use_raw"))
    reason = (raw.get("reason_template") or "").strip()
    if len(reason) > 2000:
        raise RuleValidationError(f"{where}的理由模板不能超过 2000 字。")

    try:
        priority = int(raw.get("priority", 100))
    except (TypeError, ValueError):
        raise RuleValidationError(f"{where}的优先级必须是整数。")
    if not 0 <= priority <= 1_000_000:
        raise RuleValidationError(f"{where}的优先级需在 0–1000000 之间。")

    name = (raw.get("name") or "").strip()
    if len(name) > 200:
        raise RuleValidationError(f"{where}的名称不能超过 200 字。")

    return {
        "name": name,
        "judge": judge,
        "item": item,
        "raw_min": raw_min,
        "raw_max": raw_max,
        "cal_min": cal_min,
        "cal_max": cal_max,
        "use_raw": use_raw,
        "reason_template": reason,
        "priority": priority,
    }


def normalize_rules(raw_rules: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if len(raw_rules) > 200:
        raise RuleValidationError("单次最多提交 200 条规则。")
    return [normalize_rule(r, i + 1) for i, r in enumerate(raw_rules)]


def _in_range(value: float, lo: float | None, hi: float | None) -> bool:
    if lo is not None and value < lo:
        return False
    if hi is not None and value > hi:
        return False
    return True


def rule_matches(rule: Mapping[str, Any], score: Mapping[str, Any]) -> bool:
    """Check an analyzed score row against one rule.

    ``score`` needs judge/item/raw/calibrated values; boundaries are
    inclusive (区间为闭区间).
    """
    if rule["judge"] is not None and score["judge"] != rule["judge"]:
        return False
    if rule["item"] is not None and score["item"] != rule["item"]:
        return False
    if not _in_range(float(score["raw"]), rule["raw_min"], rule["raw_max"]):
        return False
    if not _in_range(float(score["calibrated"]), rule["cal_min"], rule["cal_max"]):
        return False
    return True


def render_reason(template: str, score: Mapping[str, Any]) -> str:
    """Fill {judge}/{item}/{raw}/{calibrated} placeholders; leave the rest."""
    if not template:
        return ""

    class _Safe(dict):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    return template.format_map(
        _Safe(
            judge=score["judge"],
            item=score["item"],
            raw=f"{float(score['raw']):.2f}",
            calibrated=f"{float(score['calibrated']):.2f}",
        )
    )


def evaluate_rules(
    score_rows: Sequence[Mapping[str, Any]],
    rules: Sequence[Mapping[str, Any]],
    current_decisions: Mapping[int, tuple[bool, str]] | None = None,
) -> dict[str, Any]:
    """Match every analyzed score against all rules and resolve conflicts.

    score_rows: dicts carrying id/contestant/judge/item/raw/calibrated.
    rules: normalized rule dicts, their list position is the tie-breaker.
    current_decisions: optional score_id -> (use_raw, reason) used to flag how
    many scores the batch would actually change.

    Returns per-rule hit/miss/conflict/win counts, overall totals and the
    per-score assignment (winner rule + losing conflicting rules).
    """
    current_decisions = current_decisions or {}
    n = len(rules)

    per_rule = [
        {
            "index": i,
            "name": rules[i].get("name", ""),
            "priority": rules[i]["priority"],
            "use_raw": bool(rules[i]["use_raw"]),
            "hits": 0,
            "misses": 0,
            "conflicts": 0,  # scores also matched by another rule
            "wins": 0,      # scores for which this rule is the winner
        }
        for i in range(n)
    ]

    assignments: list[dict[str, Any]] = []
    conflicted = 0
    matched_total = 0
    would_change = 0

    for score in score_rows:
        hits = [i for i, rule in enumerate(rules) if rule_matches(rule, score)]
        if not hits:
            for stat in per_rule:
                stat["misses"] += 1
            assignments.append(
                {
                    "score_id": score["id"],
                    "contestant": score.get("contestant", ""),
                    "judge": score["judge"],
                    "item": score["item"],
                    "raw": score["raw"],
                    "calibrated": score["calibrated"],
                    "matched": False,
                    "matched_rule_indexes": [],
                    "winner_index": None,
                    "use_raw": None,
                    "reason": "",
                }
            )
            continue

        matched_total += 1
        # Smallest priority wins; list order breaks ties (min keeps first).
        winner = min(hits, key=lambda i: (rules[i]["priority"], i))
        if len(hits) > 1:
            conflicted += 1
        for i in hits:
            per_rule[i]["hits"] += 1
            per_rule[i]["wins"] += 1 if i == winner else 0
            if len(hits) > 1:
                per_rule[i]["conflicts"] += 1
        for i in range(n):
            if i not in hits:
                per_rule[i]["misses"] += 1

        use_raw = bool(rules[winner]["use_raw"])
        reason = render_reason(rules[winner].get("reason_template", ""), score)
        current = current_decisions.get(score["id"])
        if current is None or bool(current[0]) != use_raw or current[1] != reason:
            would_change += 1

        assignments.append(
            {
                "score_id": score["id"],
                "contestant": score.get("contestant", ""),
                "judge": score["judge"],
                "item": score["item"],
                "raw": score["raw"],
                "calibrated": score["calibrated"],
                "matched": True,
                "matched_rule_indexes": hits,
                "winner_index": winner,
                "use_raw": use_raw,
                "reason": reason,
            }
        )

    return {
        "total_scores": len(score_rows),
        "matched": matched_total,
        "unmatched": len(score_rows) - matched_total,
        "conflicted": conflicted,
        "would_change": would_change,
        "rules": per_rule,
        "assignments": assignments,
    }


def score_rows_from_analysis(analysis: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flatten analyze() output into the rows evaluate_rules() expects."""
    rows: list[dict[str, Any]] = []
    for block in analysis["contestants"]:
        contestant = block["contestant"]
        for ib in block["items"]:
            for s in ib["scores"]:
                rows.append(
                    {
                        "id": s["id"],
                        "contestant": contestant,
                        "judge": s["judge"],
                        "item": ib["item"],
                        "raw": float(s["raw"]),
                        "calibrated": float(s["calibrated"]),
                    }
                )
    return rows
