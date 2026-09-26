"""Tests for the judge ranking disagreement panel (raw-score only)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.scoring import analyze, judge_disagreement


def recs(data):
    # data: list of (contestant, judge, item, score)
    return [
        {"id": i + 1, "contestant": c, "judge": j, "item": it, "score": s}
        for i, (c, j, it, s) in enumerate(data)
    ]


def only_group(result):
    assert len(result["groups"]) == 1, result["groups"]
    return result["groups"][0]


class TestJudgeDisagreement(unittest.TestCase):
    def test_all_concordant(self):
        data = [
            ("C1", "JA", "P1", 8), ("C2", "JA", "P1", 7), ("C3", "JA", "P1", 6),
            ("C1", "JB", "P1", 9), ("C2", "JB", "P1", 7.5), ("C3", "JB", "P1", 5),
        ]
        dg = judge_disagreement(recs(data))
        g = only_group(dg)
        self.assertTrue(g["sufficient"])
        self.assertEqual(g["common_count"], 3)
        self.assertEqual(g["pairs_total"], 3)
        self.assertEqual(g["comparable_pairs"], 3)
        self.assertEqual(g["counts"]["concordant"], 3)
        self.assertEqual(g["counts"]["discordant"], 0)
        self.assertEqual(g["counts"]["one_tie"], 0)
        self.assertEqual(g["counts"]["both_tie"], 0)
        self.assertEqual(g["discordance_rate"], 0.0)
        self.assertEqual(g["discordant_pairs"], [])
        s = dg["summary"]
        self.assertEqual(s["groups"], 1)
        self.assertEqual(s["sufficient_groups"], 1)
        self.assertEqual(s["insufficient_groups"], 0)
        self.assertEqual(s["common_contestant_pairs"], 3)
        self.assertEqual(s["comparable_pairs"], 3)
        self.assertEqual(s["discordant_pairs"], 0)
        self.assertEqual(s["discordance_rate"], 0.0)

    def test_discordant_pair_with_raw_scores(self):
        data = [
            ("A", "JA", "P1", 8), ("B", "JA", "P1", 7),
            ("A", "JB", "P1", 6), ("B", "JB", "P1", 9),
        ]
        g = only_group(judge_disagreement(recs(data)))
        self.assertEqual(g["counts"]["concordant"], 0)
        self.assertEqual(g["counts"]["discordant"], 1)
        self.assertEqual(g["comparable_pairs"], 1)
        self.assertEqual(g["discordance_rate"], 1.0)
        pair = g["discordant_pairs"]
        self.assertEqual(len(pair), 1)
        p = pair[0]
        self.assertEqual(p["contestant_a"], "A")
        self.assertEqual(p["contestant_b"], "B")
        self.assertEqual((p["judge_a_score_of_a"], p["judge_a_score_of_b"]), (8.0, 7.0))
        self.assertEqual((p["judge_b_score_of_a"], p["judge_b_score_of_b"]), (6.0, 9.0))
        self.assertEqual(p["judge_a_prefers"], "A")
        self.assertEqual(p["judge_b_prefers"], "B")

    def test_one_sided_and_both_side_ties_excluded_from_comparable(self):
        # 单方同分：JA 给 A/B 同分，JB 非同分。
        data_one = [
            ("A", "JA", "P1", 8), ("B", "JA", "P1", 8),
            ("A", "JB", "P1", 9), ("B", "JB", "P1", 7),
        ]
        g = only_group(judge_disagreement(recs(data_one)))
        self.assertEqual(g["counts"]["one_tie"], 1)
        self.assertEqual(g["comparable_pairs"], 0)
        self.assertIsNone(g["discordance_rate"])
        self.assertEqual(g["pairs_total"], 1)

        # 双方同分。
        data_both = [
            ("A", "JA", "P1", 8), ("B", "JA", "P1", 8),
            ("A", "JB", "P1", 5), ("B", "JB", "P1", 5),
        ]
        g2 = only_group(judge_disagreement(recs(data_both)))
        self.assertEqual(g2["counts"]["both_tie"], 1)
        self.assertEqual(g2["comparable_pairs"], 0)
        self.assertIsNone(g2["discordance_rate"])

    def test_mixed_categories_and_rate(self):
        # JA: C1>C2>C3；JB: C2>C3>C1 → (C1,C2) 逆向、(C1,C3) 逆向、(C2,C3) 同向。
        data = [
            ("C1", "JA", "P1", 9), ("C2", "JA", "P1", 8), ("C3", "JA", "P1", 7),
            ("C1", "JB", "P1", 7), ("C2", "JB", "P1", 9), ("C3", "JB", "P1", 8),
        ]
        g = only_group(judge_disagreement(recs(data)))
        self.assertEqual(g["counts"]["concordant"], 1)
        self.assertEqual(g["counts"]["discordant"], 2)
        self.assertEqual(g["comparable_pairs"], 3)
        self.assertAlmostEqual(g["discordance_rate"], 2 / 3)
        self.assertEqual(
            sorted((p["contestant_a"], p["contestant_b"]) for p in g["discordant_pairs"]),
            [("C1", "C2"), ("C1", "C3")],
        )

    def test_insufficient_sample_gives_no_rate(self):
        # JA/JB 只共同评过 1 名选手；B 仅 JA 评过，不能计入共评选手。
        data = [
            ("A", "JA", "P1", 8), ("B", "JA", "P1", 7),
            ("A", "JB", "P1", 6),
        ]
        dg = judge_disagreement(recs(data))
        g = only_group(dg)
        self.assertFalse(g["sufficient"])
        self.assertEqual(g["common_count"], 1)
        self.assertEqual(g["pairs_total"], 0)
        self.assertIsNone(g["comparable_pairs"])
        self.assertIsNone(g["discordance_rate"])
        self.assertEqual(g["discordant_pairs"], [])
        for v in g["counts"].values():
            self.assertEqual(v, 0)
        s = dg["summary"]
        self.assertEqual(s["groups"], 1)
        self.assertEqual(s["insufficient_groups"], 1)
        self.assertEqual(s["sufficient_groups"], 0)
        # 样本不足分组不计入任何类别与整体占比。
        self.assertEqual(s["common_contestant_pairs"], 0)
        self.assertEqual(s["comparable_pairs"], 0)
        self.assertIsNone(s["discordance_rate"])

    def test_grouping_by_item_and_judge_pair(self):
        data = [
            # I1: JA/JB 共评 A、B 且逆向；JC 只评过 A → (JA,JC)/(JB,JC) 样本不足。
            ("A", "JA", "I1", 8), ("B", "JA", "I1", 7),
            ("A", "JB", "I1", 6), ("B", "JB", "I1", 9),
            ("A", "JC", "I1", 5),
            # I2: JA/JB 共评 A、B 且同向。
            ("A", "JA", "I2", 8), ("B", "JA", "I2", 7),
            ("A", "JB", "I2", 9), ("B", "JB", "I2", 8),
        ]
        dg = judge_disagreement(recs(data))
        self.assertEqual(dg["summary"]["groups"], 4)
        keys = sorted((g["item"], g["judge_a"], g["judge_b"]) for g in dg["groups"])
        self.assertEqual(
            keys,
            [("I1", "JA", "JB"), ("I1", "JA", "JC"), ("I1", "JB", "JC"), ("I2", "JA", "JB")],
        )
        s = dg["summary"]
        self.assertEqual(s["sufficient_groups"], 2)
        self.assertEqual(s["insufficient_groups"], 2)
        self.assertEqual(s["counts"]["discordant"], 1)
        self.assertEqual(s["counts"]["concordant"], 1)
        self.assertEqual(s["comparable_pairs"], 2)
        self.assertAlmostEqual(s["discordance_rate"], 0.5)

    def test_empty_records(self):
        dg = judge_disagreement([])
        self.assertEqual(dg["groups"], [])
        self.assertEqual(dg["summary"]["groups"], 0)
        self.assertIsNone(dg["summary"]["discordance_rate"])

    def test_independent_of_scope_and_decisions(self):
        data = [
            ("A", "JA", "P1", 8), ("B", "JA", "P1", 7),
            ("A", "JB", "P1", 6), ("B", "JB", "P1", 9),
        ]
        records = recs(data)
        a_judge = analyze(records, {}, scope="judge")["judge_disagreement"]
        a_item = analyze(records, {}, scope="judge_item")["judge_disagreement"]
        # 对 A 的全部评分保留原分，不应改变分歧结果。
        decisions = {r["id"]: (True, "复核") for r in records if r["contestant"] == "A"}
        a_decided = analyze(records, decisions, scope="judge")["judge_disagreement"]
        self.assertEqual(a_judge, a_item)
        self.assertEqual(a_judge, a_decided)
        g = only_group(a_judge)
        self.assertEqual(g["counts"]["discordant"], 1)
        self.assertEqual(g["discordance_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
