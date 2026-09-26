"""Tests for the judge leniency/severity offset diagnosis (raw-score only)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.judge_offsets import judge_offset_diagnosis
from app.scoring import analyze


def recs(data):
    # data: list of (contestant, judge, item, score)
    return [
        {"id": i + 1, "contestant": c, "judge": j, "item": it, "score": s}
        for i, (c, j, it, s) in enumerate(data)
    ]


def judge_map(result):
    return {j["judge"]: j for j in result["judges"]}


def pair_map(result):
    return {(p["judge_a"], p["judge_b"]): p for p in result["pairs"]}


# 三角形数据：JA/JB/JC 两两共评，观测分差互不一致（存在拟合残差）。
# 边 JA-JB：分差 [2,2,2] → 中位数 2，权重 3
# 边 JA-JC：分差 [1,2]   → 中位数 1.5，权重 2
# 边 JB-JC：分差 [-1,0,0] → 中位数 0，权重 3
TRIANGLE = [
    ("C1", "JA", "P1", 9), ("C1", "JB", "P1", 7), ("C1", "JC", "P1", 8),
    ("C2", "JA", "P1", 8), ("C2", "JB", "P1", 6), ("C2", "JC", "P1", 6),
    ("C3", "JA", "P1", 7), ("C3", "JB", "P1", 5),
    ("C4", "JB", "P1", 4), ("C4", "JC", "P1", 4),
]


class TestJudgeOffsetDiagnosis(unittest.TestCase):
    def test_two_judges_exact_fit(self):
        data = [
            ("A", "JA", "P1", 9), ("B", "JA", "P1", 8), ("C", "JA", "P1", 7),
            ("A", "JB", "P1", 7), ("B", "JB", "P1", 6), ("C", "JB", "P1", 5),
        ]
        jo = judge_offset_diagnosis(recs(data))
        s = jo["summary"]
        self.assertEqual(s["judges"], 2)
        self.assertEqual(s["estimable_judges"], 2)
        self.assertEqual(s["non_estimable_judges"], 0)
        self.assertEqual(s["components"], 1)
        self.assertEqual(s["pairs"], 1)
        self.assertEqual(s["common_records"], 3)
        judges = judge_map(jo)
        # 观测分差中位数 2 → 偏移量 ±1（分量内均值零），拟合无残差。
        self.assertAlmostEqual(judges["JA"]["offset"], 1.0)
        self.assertAlmostEqual(judges["JB"]["offset"], -1.0)
        self.assertTrue(judges["JA"]["estimable"])
        self.assertEqual(judges["JA"]["component"], 1)
        self.assertEqual(judges["JA"]["coverage"], 3)
        self.assertEqual(judges["JA"]["partners"], 1)
        pair = jo["pairs"][0]
        self.assertEqual((pair["judge_a"], pair["judge_b"]), ("JA", "JB"))
        self.assertEqual(pair["common_count"], 3)
        self.assertAlmostEqual(pair["observed_diff"], 2.0)
        self.assertAlmostEqual(pair["fitted_diff"], 2.0)
        self.assertAlmostEqual(pair["residual"], 0.0)
        self.assertAlmostEqual(s["weighted_residual_ss"], 0.0)
        self.assertAlmostEqual(s["max_abs_residual"], 0.0)

    def test_median_of_even_count(self):
        # 两条共评记录：分差 1 与 3 → 中位数 2。
        data = [
            ("A", "JA", "P1", 9), ("B", "JA", "P1", 6),
            ("A", "JB", "P1", 8), ("B", "JB", "P1", 3),
        ]
        pair = judge_offset_diagnosis(recs(data))["pairs"][0]
        self.assertEqual(pair["common_count"], 2)
        self.assertAlmostEqual(pair["observed_diff"], 2.0)

    def test_triangle_weighted_least_squares(self):
        jo = judge_offset_diagnosis(recs(TRIANGLE))
        s = jo["summary"]
        self.assertEqual(s["components"], 1)
        self.assertEqual(s["pairs"], 3)
        self.assertEqual(s["common_records"], 8)
        judges = judge_map(jo)
        # 手算加权最小二乘解（分量内零均值）：
        # JA = 25/21，JB = -2/3，JC = -11/21。
        self.assertAlmostEqual(judges["JA"]["offset"], 25 / 21)
        self.assertAlmostEqual(judges["JB"]["offset"], -2 / 3)
        self.assertAlmostEqual(judges["JC"]["offset"], -11 / 21)
        self.assertAlmostEqual(
            sum(j["offset"] for j in jo["judges"]), 0.0, places=9
        )
        # 共评覆盖量 = 该评委参与的共评记录条数。
        self.assertEqual(judges["JA"]["coverage"], 5)  # 3 (JA-JB) + 2 (JA-JC)
        self.assertEqual(judges["JB"]["coverage"], 6)  # 3 (JA-JB) + 3 (JB-JC)
        self.assertEqual(judges["JC"]["coverage"], 5)  # 2 (JA-JC) + 3 (JB-JC)
        self.assertEqual(judges["JA"]["partners"], 2)

        pairs = pair_map(jo)
        ab, ac, bc = pairs[("JA", "JB")], pairs[("JA", "JC")], pairs[("JB", "JC")]
        self.assertAlmostEqual(ab["observed_diff"], 2.0)
        self.assertAlmostEqual(ac["observed_diff"], 1.5)
        self.assertAlmostEqual(bc["observed_diff"], 0.0)
        self.assertEqual((ab["common_count"], ac["common_count"], bc["common_count"]), (3, 2, 3))
        # 拟合分差 = 偏移量之差；残差 = 观测 − 拟合。
        self.assertAlmostEqual(ab["fitted_diff"], 13 / 7)
        self.assertAlmostEqual(ac["fitted_diff"], 12 / 7)
        self.assertAlmostEqual(bc["fitted_diff"], -1 / 7)
        self.assertAlmostEqual(ab["residual"], 1 / 7)
        self.assertAlmostEqual(ac["residual"], -3 / 14)
        self.assertAlmostEqual(bc["residual"], 1 / 7)
        # 加权残差平方和 = 3·(1/7)² + 2·(3/14)² + 3·(1/7)² = 3/14。
        self.assertAlmostEqual(s["weighted_residual_ss"], 3 / 14)
        self.assertAlmostEqual(s["max_abs_residual"], 3 / 14)
        for p in jo["pairs"]:
            self.assertEqual(p["component"], 1)

    def test_chain_exact_fit(self):
        # 链式图（树）：JA-JB、JB-JC 有共评，JA-JC 无共评 → 无该评委对。
        data = [
            ("A", "JA", "P1", 9), ("A", "JB", "P1", 7),
            ("B", "JA", "P1", 8), ("B", "JB", "P1", 6),
            ("C", "JB", "P1", 5), ("C", "JC", "P1", 3),
            ("D", "JB", "P1", 6), ("D", "JC", "P1", 5),
        ]
        jo = judge_offset_diagnosis(recs(data))
        self.assertEqual(jo["summary"]["pairs"], 2)
        judges = judge_map(jo)
        # 树结构可精确拟合：JA-JB=2、JB-JC=1.5，零均值 → 11/6, -1/6, -5/3。
        self.assertAlmostEqual(judges["JA"]["offset"], 11 / 6)
        self.assertAlmostEqual(judges["JB"]["offset"], -1 / 6)
        self.assertAlmostEqual(judges["JC"]["offset"], -5 / 3)
        for p in jo["pairs"]:
            self.assertAlmostEqual(p["residual"], 0.0)
        self.assertAlmostEqual(jo["summary"]["weighted_residual_ss"], 0.0)

    def test_disconnected_components_no_cross_comparison(self):
        # JA/JB 一个分量，JC/JD 另一个分量；JE 无共评边 → 不可估计。
        data = [
            ("A", "JA", "P1", 9), ("A", "JB", "P1", 7),
            ("B", "JA", "P1", 8), ("B", "JB", "P1", 6),
            ("A", "JC", "P2", 9), ("A", "JD", "P2", 5),
            ("B", "JC", "P2", 7), ("B", "JD", "P2", 3),
            ("A", "JE", "P3", 6),
        ]
        jo = judge_offset_diagnosis(recs(data))
        s = jo["summary"]
        self.assertEqual(s["judges"], 5)
        self.assertEqual(s["estimable_judges"], 4)
        self.assertEqual(s["non_estimable_judges"], 1)
        self.assertEqual(s["components"], 2)
        judges = judge_map(jo)
        # 每个分量内部各自均值归零：JA/JB = ±1，JC/JD = ±2。
        self.assertAlmostEqual(judges["JA"]["offset"], 1.0)
        self.assertAlmostEqual(judges["JB"]["offset"], -1.0)
        self.assertAlmostEqual(judges["JC"]["offset"], 2.0)
        self.assertAlmostEqual(judges["JD"]["offset"], -2.0)
        self.assertEqual(judges["JA"]["component"], 1)
        self.assertEqual(judges["JB"]["component"], 1)
        self.assertEqual(judges["JC"]["component"], 2)
        self.assertEqual(judges["JD"]["component"], 2)
        # 无共评边的评委：不可估计，偏移量为 None，覆盖量 0。
        self.assertFalse(judges["JE"]["estimable"])
        self.assertIsNone(judges["JE"]["offset"])
        self.assertIsNone(judges["JE"]["component"])
        self.assertEqual(judges["JE"]["coverage"], 0)
        self.assertEqual(judges["JE"]["partners"], 0)
        # 分量摘要：各含 1 对、2 条共评记录。
        comps = jo["components"]
        self.assertEqual(len(comps), 2)
        self.assertEqual(comps[0]["judges"], ["JA", "JB"])
        self.assertEqual(comps[1]["judges"], ["JC", "JD"])
        self.assertEqual([c["pairs"] for c in comps], [1, 1])
        self.assertEqual([c["common_records"] for c in comps], [2, 2])
        # 评委对按（分量, 评委甲, 评委乙）排序，不跨分量产生评委对。
        self.assertEqual(
            [(p["component"], p["judge_a"], p["judge_b"]) for p in jo["pairs"]],
            [(1, "JA", "JB"), (2, "JC", "JD")],
        )

    def test_co_rating_requires_same_contestant_and_item(self):
        # JA 评了 P1 的 A，JB 只评了 P2 的 A：同一选手但不同项目，不构成共评。
        data = [
            ("A", "JA", "P1", 9),
            ("A", "JB", "P2", 7),
        ]
        jo = judge_offset_diagnosis(recs(data))
        self.assertEqual(jo["summary"]["pairs"], 0)
        self.assertEqual(jo["summary"]["components"], 0)
        self.assertEqual(jo["summary"]["estimable_judges"], 0)
        judges = judge_map(jo)
        self.assertFalse(judges["JA"]["estimable"])
        self.assertFalse(judges["JB"]["estimable"])

    def test_empty_records(self):
        jo = judge_offset_diagnosis([])
        self.assertEqual(jo["judges"], [])
        self.assertEqual(jo["pairs"], [])
        self.assertEqual(jo["components"], [])
        s = jo["summary"]
        self.assertEqual(s["judges"], 0)
        self.assertEqual(s["pairs"], 0)
        self.assertEqual(s["common_records"], 0)
        self.assertEqual(s["weighted_residual_ss"], 0.0)
        self.assertIsNone(s["max_abs_residual"])

    def test_independent_of_scope_and_decisions(self):
        records = recs(TRIANGLE)
        a_judge = analyze(records, {}, scope="judge")["judge_offsets"]
        a_item = analyze(records, {}, scope="judge_item")["judge_offsets"]
        # 全部评分保留原分，不应改变诊断结果。
        decisions = {r["id"]: (True, "复核") for r in records}
        a_decided = analyze(records, decisions, scope="judge")["judge_offsets"]
        self.assertEqual(a_judge, a_item)
        self.assertEqual(a_judge, a_decided)
        judges = judge_map(a_judge)
        self.assertAlmostEqual(judges["JA"]["offset"], 25 / 21)


if __name__ == "__main__":
    unittest.main()
