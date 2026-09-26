"""Tests for the pending-score rank bounds (read-only pure transform)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.pending_rank import (
    PendingRankValidationError,
    pending_rank_bounds,
)
from app.scoring import analyze


def make_records(rows):
    # rows: (contestant, judge, item, score)
    return [
        {"id": i + 1, "contestant": c, "judge": j, "item": it, "score": s}
        for i, (c, j, it, s) in enumerate(rows)
    ]


# 多评委数据；测试默认对所有评分下「保留原分」裁决，使最终分 == 原始分，便于精确断言。
# C1: I1={J1:8, J2:6}（均分 7）、I2={J1:10} → 总分 17
# C2: I1={J1:5}（均分 5）、I2={J1:9, J2:7}（均分 8） → 总分 13
# C3: 仅 I2={J1:4}（缺 I1） → 总分 4
BASE = [
    ("C1", "J1", "I1", 8.0), ("C1", "J2", "I1", 6.0),
    ("C1", "J1", "I2", 10.0),
    ("C2", "J1", "I1", 5.0),
    ("C2", "J1", "I2", 9.0), ("C2", "J2", "I2", 7.0),
    ("C3", "J1", "I2", 4.0),
]


def raw_decisions(records):
    return {r["id"]: (True, "raw") for r in records}


def run(rows=BASE, item="I1", target=3, lo=0.0, hi=10.0, decisions=None, scope="judge"):
    records = make_records(rows)
    dec = raw_decisions(records) if decisions is None else decisions
    a = analyze(records, dec, scope)
    return pending_rank_bounds(a, item, target, lo, hi)


def by_name(result):
    return {r["contestant"]: r for r in result["contestants"]}


class TestPendingRankBounds(unittest.TestCase):
    def test_intervals_and_gaps(self):
        r = run(target=3, lo=0.0, hi=10.0)
        self.assertEqual(r["item"], "I1")
        self.assertEqual(r["target"], 3)
        self.assertEqual(r["score_min"], 0.0)
        self.assertEqual(r["score_max"], 10.0)
        self.assertEqual(r["scope"], "judge")
        rows = by_name(r)

        c1 = rows["C1"]
        self.assertEqual(c1["existing_count"], 2)
        self.assertEqual(c1["gap"], 1)
        self.assertAlmostEqual(c1["existing_sum"], 14.0)
        self.assertAlmostEqual(c1["current_item_avg"], 7.0)
        self.assertAlmostEqual(c1["item_avg_min"], 14.0 / 3)
        self.assertAlmostEqual(c1["item_avg_max"], 24.0 / 3)
        # 其余项目保持现值：基座 = 17 − 7 = 10。
        self.assertAlmostEqual(c1["total_min"], 10.0 + 14.0 / 3)
        self.assertAlmostEqual(c1["total_max"], 18.0)

        c2 = rows["C2"]
        self.assertEqual(c2["existing_count"], 1)
        self.assertEqual(c2["gap"], 2)
        self.assertAlmostEqual(c2["item_avg_min"], 5.0 / 3)
        self.assertAlmostEqual(c2["item_avg_max"], 25.0 / 3)
        self.assertAlmostEqual(c2["total_min"], 8.0 + 5.0 / 3)
        self.assertAlmostEqual(c2["total_max"], 8.0 + 25.0 / 3)

        # C3 在 I1 无评分：缺口即目标人数，均分区间即整个待补分区间。
        c3 = rows["C3"]
        self.assertFalse(c3["has_item"])
        self.assertIsNone(c3["current_item_avg"])
        self.assertEqual(c3["existing_count"], 0)
        self.assertEqual(c3["gap"], 3)
        self.assertAlmostEqual(c3["item_avg_min"], 0.0)
        self.assertAlmostEqual(c3["item_avg_max"], 10.0)
        self.assertAlmostEqual(c3["total_min"], 4.0)
        self.assertAlmostEqual(c3["total_max"], 14.0)

        self.assertEqual(
            r["summary"],
            {"contestants": 3, "contestants_with_gap": 3, "gap_total": 6, "missing_item": 1},
        )

    def test_best_and_worst_ranks(self):
        rows = by_name(run(target=3, lo=0.0, hi=10.0))
        # C1：上限 18 无人可超 → 最好 1；下限 14.67 仅被 C2 上限 16.33 超 → 最差 2。
        self.assertEqual((rows["C1"]["best_rank"], rows["C1"]["worst_rank"]), (1, 2))
        # C2：上限 16.33 高于 C1 下限 14.67 → 最好 1；下限 9.67 被两人上限超 → 最差 3。
        self.assertEqual((rows["C2"]["best_rank"], rows["C2"]["worst_rank"]), (1, 3))
        # C3：上限 14 被 C1 下限 14.67 超 → 最好 2；下限 4 被两人上限超 → 最差 3。
        self.assertEqual((rows["C3"]["best_rank"], rows["C3"]["worst_rank"]), (2, 3))

    def test_exact_tie_uses_existing_tolerance(self):
        # lo=hi=15 时 C1 与 C2 的总分区间退化为同一点 10+(14+15)/3 = 8+(5+30)/3 = 59/3：
        # 同分容差内视为并列，两人最好/最差名次都为 1（竞赛排名 1224）。
        rows = by_name(run(target=3, lo=15.0, hi=15.0))
        self.assertAlmostEqual(rows["C1"]["total_min"], 59.0 / 3)
        self.assertAlmostEqual(rows["C1"]["total_max"], 59.0 / 3)
        self.assertAlmostEqual(rows["C2"]["total_min"], 59.0 / 3)
        self.assertAlmostEqual(rows["C2"]["total_max"], 59.0 / 3)
        self.assertEqual((rows["C1"]["best_rank"], rows["C1"]["worst_rank"]), (1, 1))
        self.assertEqual((rows["C2"]["best_rank"], rows["C2"]["worst_rank"]), (1, 1))
        # C3 固定为 19，被两人超过 → 最好=最差=3。
        self.assertEqual((rows["C3"]["best_rank"], rows["C3"]["worst_rank"]), (3, 3))

    def test_best_worst_ranks_anchor_ties_no_chain_diffusion(self):
        # 三人在唯一项目 I1 上各有 1 条评分，target=1 => 缺口 0，总分区间
        # 退化为现值。相邻分差 0.6e-9 在容差内，但 C3 与最高分锚点差 1.2e-9
        # 已超界：最好/最差名次都必须是 C1=C2 第 1、C3 第 3，不得链式扩散
        # 成三人并列第一（旧的「严格高于本人人数+1」相邻比较会给出 1,1,1）。
        eps = 1e-9
        rows_data = [
            ("C1", "J", "I1", 90.0),
            ("C2", "J", "I1", 90.0 - 0.6 * eps),
            ("C3", "J", "I1", 90.0 - 1.2 * eps),
        ]
        rows = by_name(run(rows=rows_data, item="I1", target=1, lo=0.0, hi=10.0))
        for name in ("C1", "C2", "C3"):
            self.assertEqual(rows[name]["gap"], 0)
        self.assertEqual((rows["C1"]["best_rank"], rows["C1"]["worst_rank"]), (1, 1))
        self.assertEqual((rows["C2"]["best_rank"], rows["C2"]["worst_rank"]), (1, 1))
        self.assertEqual((rows["C3"]["best_rank"], rows["C3"]["worst_rank"]), (3, 3))

    def test_gap_zero_when_target_equals_existing(self):
        # target=2：C1 已有 2 条 → 缺口 0，均分/总分区间退化为现值。
        rows = by_name(run(target=2, lo=0.0, hi=10.0))
        c1 = rows["C1"]
        self.assertEqual(c1["gap"], 0)
        self.assertAlmostEqual(c1["item_avg_min"], 7.0)
        self.assertAlmostEqual(c1["item_avg_max"], 7.0)
        self.assertAlmostEqual(c1["total_min"], 17.0)
        self.assertAlmostEqual(c1["total_max"], 17.0)
        # C2 缺口 1：均分区间 [(5+0)/2, (5+10)/2]。
        self.assertAlmostEqual(rows["C2"]["item_avg_min"], 2.5)
        self.assertAlmostEqual(rows["C2"]["item_avg_max"], 7.5)

    def test_overflow_rejected(self):
        # C1 在 I1 已有 2 条评分，目标 1 < 2 → 整体拒绝并指明选手。
        with self.assertRaises(PendingRankValidationError) as ctx:
            run(target=1)
        self.assertIn("C1", str(ctx.exception))
        self.assertIn("I1", str(ctx.exception))

    def test_rows_ordered_by_current_rank_and_echo(self):
        r = run()
        self.assertEqual([row["contestant"] for row in r["contestants"]], ["C1", "C2", "C3"])
        self.assertEqual([row["rank"] for row in r["contestants"]], [1, 2, 3])
        self.assertIn("scope_label", r)

    def test_uses_effective_scores_from_decisions_and_scope(self):
        # 不下「保留原分」裁决时，最终分 = 校准分：结果必须沿用校准后的最终分。
        records = make_records(BASE)
        a = analyze(records, {}, "judge")
        r = pending_rank_bounds(a, "I1", 3, 0.0, 10.0)
        rows = by_name(r)
        blocks = {b["contestant"]: b for b in a["contestants"]}
        for name, row in rows.items():
            blk = blocks[name]
            ib = next((x for x in blk["items"] if x["item"] == "I1"), None)
            exp_sum = sum(s["effective"] for s in ib["scores"]) if ib else 0.0
            self.assertAlmostEqual(row["existing_sum"], exp_sum)
            base = blk["eff_total"] - (ib["eff_avg"] if ib else 0.0)
            self.assertAlmostEqual(row["total_max"], base + row["item_avg_max"])
        # 校准范围切换后结果随 analyze 重算，scope 标注更新。
        a2 = analyze(records, {}, "judge_item")
        r2 = pending_rank_bounds(a2, "I1", 3, 0.0, 10.0)
        self.assertEqual(r2["scope"], "judge_item")
        self.assertEqual(r2["summary"]["contestants"], 3)

    def test_degenerate_interval_allowed(self):
        # 下限 == 上限：待补分固定，区间退化但合法。
        rows = by_name(run(target=3, lo=6.0, hi=6.0))
        c2 = rows["C2"]
        self.assertAlmostEqual(c2["item_avg_min"], (5.0 + 12.0) / 3)
        self.assertAlmostEqual(c2["item_avg_max"], (5.0 + 12.0) / 3)

    def test_validation_errors(self):
        records = make_records(BASE)
        a = analyze(records, raw_decisions(records), "judge")
        # 项目为空 / 不存在。
        with self.assertRaises(PendingRankValidationError):
            pending_rank_bounds(a, "  ", 3, 0, 10)
        with self.assertRaises(PendingRankValidationError) as ctx:
            pending_rank_bounds(a, "不存在", 3, 0, 10)
        self.assertIn("不存在", str(ctx.exception))
        # 目标人数非法。
        for bad in (0, -1, 1.5, "3", True):
            with self.assertRaises(PendingRankValidationError, msg=f"target={bad!r}"):
                pending_rank_bounds(a, "I1", bad, 0, 10)
        # 上下限非法：非数字、NaN/Inf、下限 > 上限、布尔值。
        for bad in ("x", float("nan"), float("inf"), True):
            with self.assertRaises(PendingRankValidationError, msg=f"lo={bad!r}"):
                pending_rank_bounds(a, "I1", 3, bad, 10)
            with self.assertRaises(PendingRankValidationError, msg=f"hi={bad!r}"):
                pending_rank_bounds(a, "I1", 3, 0, bad)
        with self.assertRaises(PendingRankValidationError) as ctx:
            pending_rank_bounds(a, "I1", 3, 10, 0)
        self.assertIn("下限", str(ctx.exception))

    def test_item_name_is_stripped(self):
        r = run(item=" I1 ")
        self.assertEqual(r["item"], "I1")


if __name__ == "__main__":
    unittest.main()
