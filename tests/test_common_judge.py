"""Tests for the common-judge comparable comparison (read-only pure transform)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.common_judge import common_judge_comparison
from app.scoring import analyze


def make_records(rows):
    # rows: (contestant, judge, item, score)
    return [
        {"id": i + 1, "contestant": c, "judge": j, "item": it, "score": s}
        for i, (c, j, it, s) in enumerate(rows)
    ]


def raw_decisions(records):
    return {r["id"]: (True, "raw") for r in records}


def run(rows, decisions=None, scope="judge"):
    records = make_records(rows)
    dec = raw_decisions(records) if decisions is None else decisions
    a = analyze(records, dec, scope)
    return common_judge_comparison(a)


def pair_of(result, a, b):
    for p in result["pairs"]:
        if {p["a"], p["b"]} == {a, b}:
            return p
    raise AssertionError(f"pair {a}/{b} not found")


# 基础数据（全部「保留原分」裁决 → 最终分 == 原始分，便于精确断言）：
# A: I1={J1:8, J2:6}（均分 7）、I2={J1:10} → 全量总分 17
# B: I1={J1:7, J2:5}（均分 6）、I2={J2:9} → 全量总分 15
# C: I1={J3:5} → 全量总分 5
BASE = [
    ("A", "J1", "I1", 8.0), ("A", "J2", "I1", 6.0),
    ("A", "J1", "I2", 10.0),
    ("B", "J1", "I1", 7.0), ("B", "J2", "I1", 5.0),
    ("B", "J2", "I2", 9.0),
    ("C", "J3", "I1", 5.0),
]


class TestCommonJudgeComparison(unittest.TestCase):
    def test_common_judge_item_averages(self):
        r = run(BASE)
        p = pair_of(r, "A", "B")
        self.assertEqual(p["status"], "comparable")
        self.assertTrue(p["comparable"])
        self.assertEqual(p["comparable_items"], 1)
        self.assertEqual(p["incomparable_items"], 1)
        i1 = next(it for it in p["items"] if it["item"] == "I1")
        self.assertTrue(i1["comparable"])
        self.assertEqual(i1["common_judges"], ["J1", "J2"])
        self.assertEqual(i1["common_count"], 2)
        # 同一评委集合上的均分：A=(8+6)/2=7，B=(7+5)/2=6。
        self.assertAlmostEqual(i1["a_avg"], 7.0)
        self.assertAlmostEqual(i1["b_avg"], 6.0)
        self.assertAlmostEqual(i1["delta"], 1.0)
        self.assertEqual(i1["leader"], "a")
        # 明细：双方各自的全部评委覆盖（含非共同评委）。
        self.assertEqual([x["judge"] for x in i1["a_judges"]], ["J1", "J2"])
        self.assertEqual([x["judge"] for x in i1["b_judges"]], ["J1", "J2"])

    def test_no_common_judge_item_is_incomparable_not_zero(self):
        r = run(BASE)
        p = pair_of(r, "A", "B")
        i2 = next(it for it in p["items"] if it["item"] == "I2")
        # A 只有 J1、B 只有 J2 → 无共同评委：不可比，不按 0 分。
        self.assertFalse(i2["comparable"])
        self.assertEqual(i2["reason"], "no_common_judge")
        self.assertEqual(i2["common_judges"], [])
        self.assertIsNone(i2["delta"])
        self.assertIsNone(i2["a_avg"])
        self.assertIsNone(i2["b_avg"])
        self.assertTrue(i2["a_has_item"])
        self.assertTrue(i2["b_has_item"])
        # 可比总分只含 I1 一项（7 与 6），不把 I2 按 0 分计入。
        self.assertAlmostEqual(p["a_comparable_total"], 7.0)
        self.assertAlmostEqual(p["b_comparable_total"], 6.0)
        self.assertAlmostEqual(p["comparable_delta"], 1.0)
        self.assertEqual(p["comparable_leader"], "a")

    def test_missing_item_is_incomparable(self):
        r = run(BASE)
        p = pair_of(r, "A", "C")
        self.assertEqual(p["status"], "incomparable")
        i2 = next(it for it in p["items"] if it["item"] == "I2")
        self.assertFalse(i2["comparable"])
        self.assertEqual(i2["reason"], "missing_item")
        self.assertEqual(i2["missing_side"], "C")
        self.assertFalse(i2["b_has_item"])
        self.assertTrue(i2["a_has_item"])
        # 整对不可比：不给可比总分与领先者，方向为不可比。
        self.assertIsNone(p["comparable_delta"])
        self.assertIsNone(p["comparable_leader"])
        self.assertEqual(p["direction"], "incomparable")
        self.assertFalse(p["reversed"])

    def test_both_missing_item(self):
        rows = BASE + [("D", "J4", "I1", 9.0)]  # D 只有 I1
        r = run(rows)
        p = pair_of(r, "C", "D")
        i2 = next(it for it in p["items"] if it["item"] == "I2")
        self.assertFalse(i2["comparable"])
        self.assertEqual(i2["reason"], "missing_item")
        self.assertTrue(i2["both_missing"])
        self.assertIsNone(i2["missing_side"])
        self.assertFalse(i2["a_has_item"])
        self.assertFalse(i2["b_has_item"])

    def test_direction_same_and_full_tie(self):
        r = run(BASE)
        p = pair_of(r, "A", "B")
        # 全量总分 A 17 > B 15，可比总分 A 7 > B 6 → 同向。
        self.assertEqual(p["direction"], "same")
        self.assertFalse(p["reversed"])
        self.assertEqual(p["full_leader"], "a")
        # 全量总分并列时方向为 full_tie。
        rows = [
            ("A", "J1", "I1", 8.0), ("B", "J1", "I1", 7.0),
            ("A", "J1", "I2", 2.0), ("B", "J1", "I2", 3.0),
        ]
        r2 = run(rows)
        p2 = pair_of(r2, "A", "B")
        self.assertAlmostEqual(p2["full_delta"], 0.0)
        self.assertEqual(p2["direction"], "full_tie")
        self.assertFalse(p2["reversed"])

    def test_full_direction_follows_final_rank_not_delta_tolerance(self):
        # 最终总分 90、90−0.6e-9、90−1.2e-9：主榜锚定并列给出名次 1、1、3。
        # A/B 名次相同 → 全量并列；A/C、B/C 分差虽分别为 1.2e-9 与 0.6e-9，
        # 但名次已有先后，绝不能再按全量总分差落容差标为「全量并列」。
        rows = [
            ("A", "J1", "I1", 90.0),
            ("B", "J1", "I1", 90.0 - 0.6e-9),
            ("C", "J1", "I1", 90.0 - 1.2e-9),
        ]
        r = run(rows)
        ab = pair_of(r, "A", "B")
        ac = pair_of(r, "A", "C")
        bc = pair_of(r, "B", "C")
        self.assertEqual((ab["a_rank"], ab["b_rank"]), (1, 1))
        self.assertEqual((ac["a_rank"], ac["b_rank"]), (1, 3))
        self.assertEqual((bc["a_rank"], bc["b_rank"]), (1, 3))

        # A/B 同名次：全量并列（即便 full_delta 非零），不构成反向。
        self.assertEqual(ab["full_leader"], "tie")
        self.assertEqual(ab["direction"], "full_tie")
        self.assertFalse(ab["reversed"])

        # A/C：全量 A 领先（名次 1 vs 3），可比差 1.2e-9 超出容差也为 A → 同向。
        self.assertEqual(ac["full_leader"], "a")
        self.assertEqual(ac["comparable_leader"], "a")
        self.assertEqual(ac["direction"], "same")
        self.assertFalse(ac["reversed"])

        # B/C：分差 0.6e-9 落入容差，但名次 1 vs 3 有先后——
        # full_leader 必须是 A（B），可比持平按非反向处理，不能是 full_tie/reversed。
        self.assertLessEqual(abs(bc["full_delta"]), 1e-9)
        self.assertEqual(bc["full_leader"], "a")
        self.assertEqual(bc["comparable_leader"], "tie")
        self.assertEqual(bc["direction"], "same")
        self.assertFalse(bc["reversed"])

        s = r["summary"]
        self.assertEqual(s["pairs"], 3)
        self.assertEqual(s["full_tie"], 1)
        self.assertEqual(s["same_direction"], 2)
        self.assertEqual(s["reversed"], 0)
        self.assertEqual(r["direction_labels"]["full_tie"], "全量并列")

    def test_reversed_direction(self):
        # 全量总分 A 领先（18 vs 9），但共同评委口径下 B 领先（9 vs 8）→ 反向。
        rows = [
            ("A", "J1", "I1", 8.0), ("A", "J2", "I1", 8.0),
            ("A", "J3", "I2", 10.0), ("A", "J4", "I2", 10.0),
            ("B", "J1", "I1", 9.0), ("B", "J2", "I1", 9.0),
        ]
        r = run(rows)
        p = pair_of(r, "A", "B")
        self.assertAlmostEqual(p["full_delta"], 9.0)
        self.assertEqual(p["full_leader"], "a")
        self.assertAlmostEqual(p["comparable_delta"], -1.0)
        self.assertEqual(p["comparable_leader"], "b")
        self.assertEqual(p["direction"], "reversed")
        self.assertTrue(p["reversed"])
        self.assertEqual(r["summary"]["reversed"], 1)

    def test_comparable_tie_is_not_reversed(self):
        # 共同评委均分持平，但全量总分有方向 → 不算反向。
        rows = [
            ("A", "J1", "I1", 8.0), ("B", "J1", "I1", 8.0),
            ("A", "J2", "I2", 10.0), ("B", "J3", "I2", 5.0),
        ]
        r = run(rows)
        p = pair_of(r, "A", "B")
        self.assertEqual(p["comparable_leader"], "tie")
        self.assertEqual(p["full_leader"], "a")
        self.assertEqual(p["direction"], "same")
        self.assertFalse(p["reversed"])

    def test_summary_counts(self):
        r = run(BASE)
        s = r["summary"]
        self.assertEqual(s["pairs"], 3)
        self.assertEqual(s["comparable"], 1)
        self.assertEqual(s["incomparable"], 2)
        self.assertEqual(s["reversed"], 0)
        self.assertEqual(s["same_direction"], 1)
        self.assertEqual(s["full_tie"], 0)
        self.assertEqual(s["a_leads"], 1)
        self.assertEqual(s["b_leads"], 0)
        self.assertEqual(s["ties"], 0)
        self.assertEqual(s["contestants"], 3)
        self.assertEqual(s["items"], 2)
        self.assertEqual(s["comparable_item_total"], 1)
        # A-C、B-C 两对不可比：每对都同时命中「缺项目」与「无共同评委」。
        self.assertEqual(s["incomparable_missing_item_pairs"], 2)
        self.assertEqual(s["incomparable_no_common_pairs"], 2)
        self.assertEqual(r["status_labels"]["comparable"], "可比")
        self.assertEqual(r["direction_labels"]["reversed"], "反向")

    def test_uses_effective_scores_and_refreshes_with_decisions(self):
        records = make_records(BASE)
        a = analyze(records, raw_decisions(records), "judge")
        before = common_judge_comparison(a)
        p_before = pair_of(before, "A", "B")
        self.assertAlmostEqual(p_before["comparable_delta"], 1.0)
        # 改裁决：A 的 I1/J1 改回校准分（最终分变化）→ 同评委均分随之变化。
        target = next(
            s
            for blk in a["contestants"]
            if blk["contestant"] == "A"
            for ib in blk["items"]
            if ib["item"] == "I1"
            for s in ib["scores"]
            if s["judge"] == "J1"
        )
        decisions = dict(raw_decisions(records))
        decisions[target["id"]] = (False, "")
        after = common_judge_comparison(analyze(records, decisions, "judge"))
        p_after = pair_of(after, "A", "B")
        i1 = next(it for it in p_after["items"] if it["item"] == "I1")
        self.assertAlmostEqual(i1["a_avg"], (target["calibrated"] + 6.0) / 2)
        self.assertNotAlmostEqual(
            p_after["comparable_delta"], p_before["comparable_delta"]
        )
        # 范围切换同样反映到结果上。
        scoped = common_judge_comparison(analyze(records, decisions, "judge_item"))
        self.assertEqual(scoped["scope"], "judge_item")

    def test_pair_order_and_ranks(self):
        r = run(BASE)
        names = [(p["a"], p["b"]) for p in r["pairs"]]
        self.assertEqual(names, [("A", "B"), ("A", "C"), ("B", "C")])
        p = pair_of(r, "A", "B")
        self.assertEqual(p["a_rank"], 1)
        self.assertEqual(p["b_rank"], 2)
        self.assertAlmostEqual(p["a_full_total"], 17.0)
        self.assertAlmostEqual(p["b_full_total"], 15.0)
        self.assertAlmostEqual(p["full_delta"], 2.0)


if __name__ == "__main__":
    unittest.main()
