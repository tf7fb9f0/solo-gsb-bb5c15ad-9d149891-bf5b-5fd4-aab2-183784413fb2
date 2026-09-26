"""Tests for the scoring math: calibration, totals, ranking and reversals."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.scoring import (
    MAD_K,
    analyze,
    compare_contestants,
    competition_rank,
    robust_params,
    stability_experiment,
)


def recs(data):
    # data: list of (contestant, judge, item, score)
    return [
        {"id": i + 1, "contestant": c, "judge": j, "item": it, "score": s}
        for i, (c, j, it, s) in enumerate(data)
    ]


class TestRobustParams(unittest.TestCase):
    def test_median_and_mad(self):
        med, raw, scaled = robust_params([1, 2, 3, 4, 100])
        self.assertEqual(med, 3)
        # deviations: 2,1,0,1,97 -> median 1
        self.assertEqual(raw, 1)
        self.assertAlmostEqual(scaled, MAD_K)

    def test_zero_spread(self):
        med, raw, scaled = robust_params([5, 5, 5])
        self.assertEqual((med, raw, scaled), (5, 0, 0))


class TestRank(unittest.TestCase):
    def test_competition_ranking_ties(self):
        ranks = competition_rank([("a", 10.0), ("b", 10.0), ("c", 9.0), ("d", 8.0)])
        self.assertEqual(ranks["a"], 1)
        self.assertEqual(ranks["b"], 1)
        self.assertEqual(ranks["c"], 3)
        self.assertEqual(ranks["d"], 4)

    def test_tie_anchored_to_group_high_no_chain_diffusion(self):
        # 相邻两人差都在容差内，但第三人与组最高分（锚点）的差已超出容差：
        # 旧的逐对相邻比较会链式扩散，把三人误排为并列第一。
        eps = 1e-9
        pairs = [
            ("a", 90.0),
            ("b", 90.0 - 0.6 * eps),
            ("c", 90.0 - 1.2 * eps),
        ]
        ranks = competition_rank(pairs)
        self.assertEqual(ranks, {"a": 1, "b": 1, "c": 3})

    def test_anchor_chains_only_within_eps_of_group_high(self):
        # 五人依次相差 0.9e-9：相对各组锚点超界即开启下一组 -> 1,1,3,3,5。
        eps = 1e-9
        pairs = [("p%d" % k, 90.0 - k * 0.9 * eps) for k in range(5)]
        self.assertEqual(
            competition_rank(pairs),
            {"p0": 1, "p1": 1, "p2": 3, "p3": 3, "p4": 5},
        )

    def test_within_eps_group_ranks_one_one_three(self):
        # 全部落在锚点容差内才是并列；下一组名次按累计人数跳过（1、1、3）。
        eps = 1e-9
        ranks = competition_rank(
            [("a", 90.0), ("b", 90.0 - 0.4 * eps), ("c", 89.0)]
        )
        self.assertEqual(ranks, {"a": 1, "b": 1, "c": 3})

    def test_tie_member_order_stable_by_name(self):
        # 同分时按键名稳定排序：无论输入次序如何，同组名次一致且并列块内
        # 名称次序确定（此处仅验证名次结果与降序分组）。
        ranks = competition_rank([("c", 10.0), ("a", 10.0), ("b", 9.5)])
        self.assertEqual(ranks, {"a": 1, "c": 1, "b": 3})

    def test_empty(self):
        self.assertEqual(competition_rank([]), {})


class TestCalibration(unittest.TestCase):
    def setUp(self):
        # Two judges with offset/scale differences, two items, two contestants.
        # Judge A tight low range; judge B wide high range.
        self.data = [
            ("C1", "JA", "I1", 8.0),
            ("C2", "JA", "I1", 7.0),
            ("C1", "JA", "I2", 9.0),
            ("C2", "JA", "I2", 6.0),
            ("C1", "JB", "I1", 12.0),
            ("C2", "JB", "I1", 10.0),
            ("C1", "JB", "I2", 13.0),
            ("C2", "JB", "I2", 9.0),
        ]

    def test_zscore_centered(self):
        a = analyze(recs(self.data), {}, "judge")
        by_judge = {g["judge"]: g for g in a["groups"]}
        # JB: values 12,10,13,9 -> median 11, deviations 1,1,2,2 -> raw MAD 1.5
        self.assertAlmostEqual(by_judge["JB"]["median"], 11)
        self.assertAlmostEqual(by_judge["JB"]["raw_mad"], 1.5)
        # Every judge's mean robust z across their own scores should be ~0 here
        # because symmetric deviations around the median.
        for block in a["contestants"]:
            for ib in block["items"]:
                for s in ib["scores"]:
                    if s["judge"] == "JB" and s["raw"] == 11:
                        self.assertAlmostEqual(s["z"], 0.0)

    def test_totals_and_ranks(self):
        a = analyze(recs(self.data), {}, "judge")
        blocks = {b["contestant"]: b for b in a["contestants"]}
        # C1 wins every item under both judges -> rank 1 raw and calibrated
        self.assertEqual(blocks["C1"]["raw_rank"], 1)
        self.assertEqual(blocks["C1"]["cal_rank"], 1)
        self.assertEqual(blocks["C1"]["eff_rank"], 1)
        self.assertEqual(blocks["C2"]["raw_rank"], 2)

    def test_override_raw_changes_effective_only(self):
        records = recs(self.data)
        first_id = records[0]["id"]
        a = analyze(records, {first_id: (True, "test reason")}, "judge")
        for block in a["contestants"]:
            for ib in block["items"]:
                for s in ib["scores"]:
                    if s["id"] == first_id:
                        self.assertTrue(s["use_raw"])
                        self.assertEqual(s["effective"], s["raw"])
                        self.assertNotEqual(s["effective"], s["calibrated"])
                        self.assertEqual(s["reason"], "test reason")
                    else:
                        self.assertFalse(s["use_raw"])
                        self.assertEqual(s["effective"], s["calibrated"])
        # calibrated totals/ranks must be unaffected by decisions
        blocks = {b["contestant"]: b for b in a["contestants"]}
        self.assertEqual(blocks["C1"]["cal_rank"], 1)
        self.assertEqual(a["counts"]["decisions"], 1)

    def test_judge_item_scope(self):
        a = analyze(recs(self.data), {}, "judge_item")
        self.assertEqual(a["scope"], "judge_item")
        # 2 judges x 2 items = 4 groups
        self.assertEqual(len(a["groups"]), 4)
        self.assertIn("item", a["groups"][0])

    def test_reversal_detected(self):
        # Two tight judges rank C1 first; one wide-spread judge ranks C2 far
        # above C1. Raw average crowns C2; robust calibration downweights the
        # wide judge and crowns C1.
        data = [
            ("C1", "JA", "I1", 8.30), ("C2", "JA", "I1", 8.00),
            ("C3", "JA", "I1", 7.90), ("C4", "JA", "I1", 7.80),
            ("C1", "JB", "I1", 8.30), ("C2", "JB", "I1", 8.00),
            ("C3", "JB", "I1", 7.85), ("C4", "JB", "I1", 7.75),
            ("C1", "JC", "I1", 5.0), ("C2", "JC", "I1", 9.5),
            ("C3", "JC", "I1", 7.2), ("C4", "JC", "I1", 6.8),
        ]
        a = analyze(recs(data), {}, "judge")
        self.assertGreaterEqual(len(a["reversals"]), 1)
        top_reversal = a["reversals"][0]
        self.assertEqual(top_reversal["winner_before"], "C2")
        self.assertEqual(top_reversal["winner_after"], "C1")
        blocks = {b["contestant"]: b for b in a["contestants"]}
        self.assertEqual(blocks["C1"]["cal_rank"], 1)
        self.assertEqual(blocks["C2"]["raw_rank"], 1)

    def test_rank_tie_chain_not_diffused_across_all_three_kinds(self):
        # 单一评委、单一项目，全部「保留原分」=> 原分/校准分/最终分同构。
        # 相邻分差 0.6e-9 在容差内，但 C3 与最高分锚点差 1.2e-9 已超界，
        # 三个名次口径都必须是 C1=C2 第 1、C3 第 3，且不产生反转。
        eps = 1e-9
        data = [
            ("C1", "J", "I", 90.0),
            ("C2", "J", "I", 90.0 - 0.6 * eps),
            ("C3", "J", "I", 90.0 - 1.2 * eps),
        ]
        records = recs(data)
        a = analyze(records, {r["id"]: (True, "raw") for r in records}, "judge")
        blocks = {b["contestant"]: b for b in a["contestants"]}
        for kind in ("raw_rank", "cal_rank", "eff_rank"):
            self.assertEqual(blocks["C1"][kind], 1, kind)
            self.assertEqual(blocks["C2"][kind], 1, kind)
            self.assertEqual(blocks["C3"][kind], 3, kind)
        # 同组两人名称稳定排序：C1 在 C2 前；C3 另起一组随后。
        self.assertEqual(
            [b["contestant"] for b in a["contestants"]], ["C1", "C2", "C3"]
        )
        # C3 与 C1 并非两侧都互有明确优胜方向（C1/C2 在原始分并列），无反转。
        self.assertEqual(a["reversals"], [])

    def test_empty(self):
        a = analyze([], {}, "judge")
        self.assertEqual(a["counts"]["scores"], 0)
        self.assertEqual(a["reversals"], [])
        self.assertEqual(a["contestants"], [])

    def test_bad_scope(self):
        with self.assertRaises(ValueError):
            analyze(recs(self.data), {}, "bogus")


class TestCompareContestants(unittest.TestCase):
    def setUp(self):
        # C1: I1+I2; C2: I1+I2; C3: only I1 (missing I2 -> 无数据).
        data = [
            ("C1", "JA", "I1", 8.0), ("C2", "JA", "I1", 7.0), ("C3", "JA", "I1", 9.0),
            ("C1", "JB", "I1", 8.0), ("C2", "JB", "I1", 7.0), ("C3", "JB", "I1", 9.0),
            ("C1", "JA", "I2", 6.0), ("C2", "JA", "I2", 8.0),
            ("C1", "JB", "I2", 6.0), ("C2", "JB", "I2", 8.0),
        ]
        self.records = recs(data)

    def test_total_and_item_deltas(self):
        a = analyze(self.records, {}, "judge")
        cmp_ = compare_contestants(a, "C1", "C2")
        blocks = {b["contestant"]: b for b in a["contestants"]}
        for kind in ("raw", "cal", "eff"):
            key = f"{kind}_total"
            self.assertAlmostEqual(
                cmp_["totals"]["delta"][kind],
                blocks["C1"][key] - blocks["C2"][key],
            )
        # Every known item appears in output order, with per-judge-average deltas.
        self.assertEqual([r["item"] for r in cmp_["items"]], a["items"])
        i1 = next(r for r in cmp_["items"] if r["item"] == "I1")
        self.assertAlmostEqual(i1["delta"]["raw"], 1.0)  # 8.0 vs 7.0
        self.assertEqual(i1["lead"], "a")
        i2 = next(r for r in cmp_["items"] if r["item"] == "I2")
        self.assertAlmostEqual(i2["delta"]["raw"], -2.0)  # 6.0 vs 8.0
        self.assertEqual(i2["lead"], "b")
        # Ranks are echoed for both sides.
        self.assertEqual(cmp_["ranks"]["a"]["eff"], blocks["C1"]["eff_rank"])
        self.assertEqual(cmp_["ranks"]["b"]["eff"], blocks["C2"]["eff_rank"])

    def test_lead_tie_counts(self):
        a = analyze(self.records, {}, "judge")
        cmp_ = compare_contestants(a, "C1", "C2")
        self.assertEqual(cmp_["summary"]["compared"], 2)
        self.assertEqual(cmp_["summary"]["a_only"], 0)
        self.assertEqual(cmp_["summary"]["b_only"], 0)
        for kind in ("raw", "cal", "eff"):
            c = cmp_["summary"][kind]
            self.assertEqual(c["a_leads"] + c["b_leads"] + c["ties"], 2)
        # Symmetric swap flips leads and the sign of every delta.
        swapped = compare_contestants(a, "C2", "C1")
        self.assertAlmostEqual(
            swapped["totals"]["delta"]["eff"], -cmp_["totals"]["delta"]["eff"]
        )
        self.assertEqual(swapped["summary"]["eff"]["a_leads"], cmp_["summary"]["eff"]["b_leads"])

    def test_missing_item_is_no_data_not_zero(self):
        a = analyze(self.records, {}, "judge")
        cmp_ = compare_contestants(a, "C1", "C3")
        i2 = next(r for r in cmp_["items"] if r["item"] == "I2")
        self.assertIsNone(i2["b"])
        self.assertIsNotNone(i2["a"])
        self.assertIsNone(i2["delta"])
        self.assertIsNone(i2["lead"])
        # I1 is the only comparable item; I2 is one-side-only.
        self.assertEqual(cmp_["summary"]["compared"], 1)
        self.assertEqual(cmp_["summary"]["a_only"], 1)
        self.assertEqual(cmp_["summary"]["b_only"], 0)
        eff = cmp_["summary"]["eff"]
        self.assertEqual(eff["a_leads"] + eff["b_leads"] + eff["ties"], 1)

    def test_item_missing_for_both_sides(self):
        # Add an item neither C1 nor C2 ever scored.
        data = [
            ("C1", "JA", "I1", 8.0), ("C2", "JA", "I1", 7.0),
            ("C1", "JB", "I1", 8.0), ("C2", "JB", "I1", 7.0),
            ("C3", "JA", "I3", 9.0), ("C3", "JB", "I3", 9.0),
        ]
        a = analyze(recs(data), {}, "judge")
        cmp_ = compare_contestants(a, "C1", "C2")
        self.assertEqual(cmp_["summary"]["items_total"], 2)
        self.assertEqual(cmp_["summary"]["compared"], 1)
        i3 = next(r for r in cmp_["items"] if r["item"] == "I3")
        self.assertIsNone(i3["a"])
        self.assertIsNone(i3["b"])
        self.assertIsNone(i3["delta"])

    def test_exact_tie(self):
        data = [
            ("C1", "JA", "I1", 8.0), ("C2", "JA", "I1", 8.0),
            ("C1", "JB", "I1", 7.0), ("C2", "JB", "I1", 7.0),
        ]
        a = analyze(recs(data), {}, "judge")
        cmp_ = compare_contestants(a, "C1", "C2")
        self.assertEqual(cmp_["items"][0]["lead"], "tie")
        self.assertEqual(cmp_["summary"]["eff"]["ties"], 1)
        self.assertEqual(cmp_["summary"]["eff"]["a_leads"], 0)
        self.assertEqual(cmp_["totals"]["delta"]["eff"], 0)

    def test_decisions_change_eff_only(self):
        # Three-judge reversal fixture: JC is a wild outlier, so calibrated
        # scores are guaranteed to differ from raw ones.
        records = recs([
            ("C1", "JA", "I1", 8.30), ("C2", "JA", "I1", 8.00),
            ("C3", "JA", "I1", 7.90), ("C4", "JA", "I1", 7.80),
            ("C1", "JB", "I1", 8.30), ("C2", "JB", "I1", 8.00),
            ("C3", "JB", "I1", 7.85), ("C4", "JB", "I1", 7.75),
            ("C1", "JC", "I1", 5.0), ("C2", "JC", "I1", 9.5),
            ("C3", "JC", "I1", 7.2), ("C4", "JC", "I1", 6.8),
        ])
        a0 = analyze(records, {}, "judge")
        before = compare_contestants(a0, "C1", "C3")
        # Pick one of C3's scores whose calibrated value actually moves.
        target = next(
            s
            for blk in a0["contestants"]
            if blk["contestant"] == "C3"
            for ib in blk["items"]
            for s in ib["scores"]
            if abs(s["calibrated"] - s["raw"]) > 1e-9
        )
        a1 = analyze(records, {target["id"]: (True, "复核")}, "judge")
        after = compare_contestants(a1, "C1", "C3")
        self.assertAlmostEqual(
            after["totals"]["delta"]["raw"], before["totals"]["delta"]["raw"]
        )
        self.assertAlmostEqual(
            after["totals"]["delta"]["cal"], before["totals"]["delta"]["cal"]
        )
        self.assertNotAlmostEqual(
            after["totals"]["delta"]["eff"], before["totals"]["delta"]["eff"]
        )

    def test_errors(self):
        a = analyze(self.records, {}, "judge")
        with self.assertRaises(ValueError):
            compare_contestants(a, "C1", "C1")
        with self.assertRaises(LookupError):
            compare_contestants(a, "C1", "NOBODY")
        with self.assertRaises(LookupError):
            compare_contestants(a, "NOBODY", "C1")


class TestStabilityExperiment(unittest.TestCase):
    def setUp(self):
        # 4 contestants x 3 judges x 1 item; JC is a wild outlier judge.
        data = [
            ("C1", "JA", "P1", 8.30), ("C2", "JA", "P1", 8.00),
            ("C3", "JA", "P1", 7.90), ("C4", "JA", "P1", 7.80),
            ("C1", "JB", "P1", 8.30), ("C2", "JB", "P1", 8.00),
            ("C3", "JB", "P1", 7.85), ("C4", "JB", "P1", 7.75),
            ("C1", "JC", "P1", 5.0), ("C2", "JC", "P1", 9.5),
            ("C3", "JC", "P1", 7.2), ("C4", "JC", "P1", 6.8),
        ]
        self.records = recs(data)
        # Add a second item so item exclusion leaves partial data.
        self.records += recs([
            ("C1", "JA", "P2", 8.2), ("C2", "JA", "P2", 8.1),
            ("C3", "JA", "P2", 7.8), ("C4", "JA", "P2", 7.7),
            ("C1", "JB", "P2", 8.1), ("C2", "JB", "P2", 8.0),
            ("C3", "JB", "P2", 7.9), ("C4", "JB", "P2", 7.8),
        ])

    def test_exclude_judge_baseline_fields(self):
        res = stability_experiment(self.records, {}, "judge", "judge", "JC")
        self.assertEqual(res["exclusion"]["type"], "judge")
        self.assertEqual(res["exclusion"]["name"], "JC")
        self.assertEqual(res["removed_scores"], 4)
        self.assertEqual(res["baseline_counts"]["scores"], 20)
        self.assertEqual(res["scenario_counts"]["scores"], 16)
        self.assertEqual(res["scenario_counts"]["judges"], 2)
        # Every contestant loses exactly one score (JC's).
        self.assertTrue(all(r["affected_scores"] == 1 for r in res["contestants"]))

    def test_deltas_match_independent_rerun(self):
        baseline = analyze(self.records, {}, "judge")
        res = stability_experiment(self.records, {}, "judge", "judge", "JC")
        kept = [r for r in self.records if r["judge"] != "JC"]
        scenario = analyze(kept, {}, "judge")

        b_map = {b["contestant"]: b for b in baseline["contestants"]}
        s_map = {b["contestant"]: b for b in scenario["contestants"]}
        self.assertEqual(len(res["contestants"]), len(baseline["contestants"]))
        for row in res["contestants"]:
            name = row["contestant"]
            self.assertTrue(row["present"])
            self.assertAlmostEqual(
                row["raw_total_delta"],
                s_map[name]["raw_total"] - b_map[name]["raw_total"],
            )
            self.assertEqual(
                row["eff_rank_delta"],
                b_map[name]["eff_rank"] - s_map[name]["eff_rank"],
            )
            self.assertEqual(row["baseline"]["eff_rank"], b_map[name]["eff_rank"])
            self.assertEqual(row["scenario"]["eff_rank"], s_map[name]["eff_rank"])

    def test_excluding_outlier_judge_changes_rank(self):
        # With JC removed the raw ranking must lose the reversal (C1 leads C2).
        res = stability_experiment(self.records, {}, "judge", "judge", "JC")
        rows = {r["contestant"]: r for r in res["contestants"]}
        # JC gives C2 9.5 vs C1 5.0 -> removing it moves C1 up / C2 down.
        self.assertGreaterEqual(rows["C1"]["raw_rank_delta"], 0)
        self.assertLess(rows["C2"]["raw_rank_delta"], 0)
        self.assertEqual(res["summary"]["eff_moved_up"] + res["summary"]["eff_moved_down"],
                         res["summary"]["eff_rank_changed"])

    def test_exclude_item_leaves_partial_data(self):
        res = stability_experiment(self.records, {}, "judge_item", "item", "P1")
        self.assertEqual(res["exclusion"]["type_label"], "项目")
        self.assertEqual(res["removed_scores"], 12)
        self.assertEqual(res["scenario_counts"]["items"], 1)
        # Everyone still scored P2, so all remain present.
        self.assertTrue(all(r["present"] for r in res["contestants"]))
        # Baseline totals contain two items, scenario only P2.
        for row in res["contestants"]:
            self.assertNotAlmostEqual(
                row["scenario"]["eff_total"], row["baseline"]["eff_total"]
            )

    def test_excluded_only_contestant_goes_missing(self):
        # C1 is only ever scored by judge JA -> removing JA removes C1 entirely.
        data = [
            ("C1", "JA", "P1", 9.0),
            ("C2", "JA", "P1", 7.0), ("C2", "JB", "P1", 6.0),
        ]
        records = recs(data)
        res = stability_experiment(records, {}, "judge", "judge", "JA")
        rows = {r["contestant"]: r for r in res["contestants"]}
        self.assertFalse(rows["C1"]["present"])
        self.assertIsNone(rows["C1"]["scenario"])
        self.assertIsNone(rows["C1"]["eff_rank_delta"])
        self.assertEqual(rows["C1"]["affected_scores"], 1)
        self.assertTrue(rows["C2"]["present"])
        self.assertEqual(rows["C2"]["affected_scores"], 1)

    def test_inputs_not_mutated_and_decisions_follow_removed_scores(self):
        records = recs([
            ("C1", "JA", "P1", 8.0), ("C2", "JA", "P1", 7.0),
            ("C1", "JB", "P1", 9.0), ("C2", "JB", "P1", 6.0),
        ])
        before = [dict(r) for r in records]
        decisions = {1: (True, "keep")}  # id 1 belongs to judge JA
        res = stability_experiment(records, decisions, "judge", "judge", "JB")
        self.assertEqual(records, before)
        self.assertEqual(len(decisions), 1)
        # JB excluded: its two scores are gone, JA's decision stays in effect.
        self.assertEqual(res["removed_scores"], 2)
        self.assertEqual(res["scenario_counts"]["decisions"], 1)

    def test_unknown_exclusion(self):
        with self.assertRaises(LookupError):
            stability_experiment(self.records, {}, "judge", "judge", "NOBODY")
        with self.assertRaises(ValueError):
            stability_experiment(self.records, {}, "judge", "round", "P1")


if __name__ == "__main__":
    unittest.main()
