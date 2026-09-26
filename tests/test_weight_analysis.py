"""Tests for item weight threshold analysis (read-only pure transform)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.scoring import analyze
from app.weight_analysis import (
    WeightValidationError,
    weight_threshold_analysis,
)


def make_records(rows):
    # rows: (contestant, judge, item, score)
    return [
        {"id": i + 1, "contestant": c, "judge": j, "item": it, "score": s}
        for i, (c, j, it, s) in enumerate(rows)
    ]


# 每个项目只有一位评委，使最终均分 == 原始分，便于手算断言；
# C1: I1=7, I2=9；C2: I1=9, I2=5。
# 调整 I2 倍率 w 时：总分 C1 = 9w + 7，C2 = 5w + 9 → 交点 w = 0.5。
BASE = [
    ("C1", "J", "I1", 7.0), ("C2", "J", "I1", 9.0),
    ("C1", "J", "I2", 9.0), ("C2", "J", "I2", 5.0),
]


def run(rows=BASE, item="I2", upper=3.0, decisions=None, scope="judge"):
    return weight_threshold_analysis(
        analyze(make_records(rows), decisions or {}, scope), item, upper
    )


def run_raw(rows, item="I2", upper=2.0, scope="judge"):
    # 全部评分裁决为「保留原分」，最终均分/总分即原始分本身，
    # 大数场景下可精确手算斜率、截距与交点。
    records = make_records(rows)
    decisions = {rec["id"]: (True, "精确手算") for rec in records}
    return weight_threshold_analysis(analyze(records, decisions, scope), item, upper)


def kinds(entries):
    return [e["kind"] for e in entries]


def critical_ws(result):
    return [e["w"] for e in result["entries"] if e["kind"] == "critical"]


class TestWeightThreshold(unittest.TestCase):
    def test_single_crossing_and_midpoints(self):
        r = run(upper=3.0)
        # 临界点 0.5 + 两个区间中点 (0,0.5) 与 (0.5,3)，端点 0/3 无并列不展开。
        self.assertEqual(kinds(r["entries"]), ["midpoint", "critical", "midpoint"])
        crit = next(e for e in r["entries"] if e["kind"] == "critical")
        self.assertAlmostEqual(crit["w"], 0.5, places=9)
        self.assertEqual(crit["ties"], [["C1", "C2"]])
        self.assertTrue(all(row["tied"] for row in crit["ranking"]))
        self.assertEqual({row["rank"] for row in crit["ranking"]}, {1})
        # 交点总分：9*0.5+7 = 5*0.5+9 = 11.5。
        for row in crit["ranking"]:
            self.assertAlmostEqual(row["total"], 11.5, places=9)
        # 中点处的相对次序：w=0.25 时 C2 领先，w=1.75 时 C1 领先。
        mids = [e for e in r["entries"] if e["kind"] == "midpoint"]
        self.assertAlmostEqual(mids[0]["w"], 0.25, places=9)
        self.assertAlmostEqual(mids[1]["w"], 1.75, places=9)
        rank_at = lambda e, n: next(x["rank"] for x in e["ranking"] if x["contestant"] == n)
        self.assertLess(rank_at(mids[0], "C2"), rank_at(mids[0], "C1"))
        self.assertLess(rank_at(mids[1], "C1"), rank_at(mids[1], "C2"))
        # 交点标记了 boundary=None（位于开区间内部）。
        self.assertIsNone(crit["boundary"])

    def test_crossing_at_lower_boundary(self):
        # C1/C2 在 I1 相同（截距相等），I2 均分不同 → 恰在 w=0 相交。
        rows = [
            ("C1", "J", "I1", 9.0), ("C2", "J", "I1", 9.0),
            ("C1", "J", "I2", 10.0), ("C2", "J", "I2", 8.0),
        ]
        r = run(rows, upper=3.0)
        crit = [e for e in r["entries"] if e["kind"] == "critical"]
        self.assertEqual(len(crit), 1)
        self.assertAlmostEqual(crit[0]["w"], 0.0)
        self.assertEqual(crit[0]["boundary"], "start")
        # 只有 (0,3) 一个开区间中点。
        self.assertEqual(kinds(r["entries"]), ["critical", "midpoint"])

    def test_crossing_at_upper_boundary(self):
        # C1: I1=10,I2=6 → 6w+10；C2: I1=4,I2=8 → 8w+4 → 交点 w=3。
        rows = [
            ("C1", "J", "I1", 10.0), ("C2", "J", "I1", 4.0),
            ("C1", "J", "I2", 6.0), ("C2", "J", "I2", 8.0),
        ]
        r = run(rows, upper=3.0)
        crit = [e for e in r["entries"] if e["kind"] == "critical"]
        self.assertEqual(len(crit), 1)
        self.assertAlmostEqual(crit[0]["w"], 3.0)
        self.assertEqual(crit[0]["boundary"], "end")
        self.assertEqual(kinds(r["entries"]), ["midpoint", "critical"])

    def test_root_outside_interval_has_no_critical(self):
        # C1: I1=0,I2=1 → w；C2: I1=-10,I2=2 → 2w-10；交点 w=10，超出上限 3。
        rows = [
            ("C1", "J", "I1", 0.0), ("C1", "J", "I2", 1.0),
            ("C2", "J", "I1", -10.0), ("C2", "J", "I2", 2.0),
        ]
        r = run(rows, upper=3.0)
        self.assertEqual(r["summary"]["crossing_pairs"], 0)
        self.assertEqual(r["summary"]["critical_points"], 0)
        self.assertEqual(kinds(r["entries"]), ["midpoint"])

    def test_identical_lines_always_tied_no_critical(self):
        rows = [
            ("C1", "J", "I1", 7.0), ("C2", "J", "I1", 7.0),
            ("C1", "J", "I2", 9.0), ("C2", "J", "I2", 9.0),
        ]
        r = run(rows, upper=3.0)
        # 直线完全重合：任何倍率都并列，不产生交点；区间内只有一个中点。
        self.assertEqual(critical_ws(r), [])
        self.assertEqual(r["always_tied_groups"], [["C1", "C2"]])
        self.assertEqual(r["summary"]["crossing_pairs"], 0)
        self.assertEqual(len([e for e in r["entries"] if e["kind"] == "midpoint"]), 1)
        # 中点处两人竞赛名次相同（1224 并列）。
        mid = next(e for e in r["entries"] if e["kind"] == "midpoint")
        ranks = {x["contestant"]: x["rank"] for x in mid["ranking"]}
        self.assertEqual(ranks, {"C1": 1, "C2": 1})

    def test_parallel_offset_lines_no_critical(self):
        # I2 均分相同（斜率相同），I1 差 2 分 → 平行不重合，永不相交。
        rows = [
            ("C1", "J", "I1", 9.0), ("C2", "J", "I1", 7.0),
            ("C1", "J", "I2", 5.0), ("C2", "J", "I2", 5.0),
        ]
        r = run(rows, upper=3.0)
        self.assertEqual(critical_ws(r), [])
        self.assertEqual(r["always_tied_groups"], [])
        mid = next(e for e in r["entries"] if e["kind"] == "midpoint")
        # C1 在任意 w 都领先 2 分。
        self.assertLess(next(x["rank"] for x in mid["ranking"] if x["contestant"] == "C1"),
                        next(x["rank"] for x in mid["ranking"] if x["contestant"] == "C2"))

    def test_triple_tie_merges_into_one_critical(self):
        # C1: w+3，C2: 2w+2，C3: 3w+1 → w=1 时三者都等于 4（3 对交点合并为 1 个）。
        rows = []
        for c, s, b in [("C1", 1.0, 3.0), ("C2", 2.0, 2.0), ("C3", 3.0, 1.0)]:
            rows.append((c, "J", "I1", b))
            rows.append((c, "J", "I2", s))
        r = run(rows, upper=3.0)
        crit = [e for e in r["entries"] if e["kind"] == "critical"]
        self.assertEqual(len(crit), 1)
        self.assertAlmostEqual(crit[0]["w"], 1.0)
        self.assertEqual(crit[0]["ties"], [["C1", "C2", "C3"]])
        self.assertEqual(r["summary"]["crossing_pairs"], 3)  # 3 对选手对都在此相交

    def test_missing_item_zero_contribution(self):
        # C3 只有 I1=8：斜率 0、截距 8，全程水平。
        rows = BASE + [("C3", "J", "I1", 8.0)]
        r = run(rows, upper=3.0)
        self.assertEqual(r["contestants_missing_item"], ["C3"])
        c3_rows = [
            row
            for e in r["entries"]
            for row in e["ranking"]
            if row["contestant"] == "C3"
        ]
        self.assertTrue(c3_rows)
        for row in c3_rows:
            self.assertFalse(row["has_item"])
            self.assertAlmostEqual(row["total"], 8.0, places=9)  # 缺失项目 0 贡献
            self.assertAlmostEqual(row["item_avg"], 0.0, places=9)

    def test_reflects_decisions_and_scope(self):
        # 给 C2 在 I2 的原始分（id=4）加「保留原分」裁决前先构造校准分≠原分的场景。
        # 两位评委打分会产生校准差异；裁决翻转后最终均分改变 → 交点随之移动。
        rows = [
            ("C1", "J1", "I1", 7.0), ("C2", "J1", "I1", 9.0),
            ("C1", "J2", "I1", 8.0), ("C2", "J2", "I1", 8.0),
            ("C1", "J1", "I2", 9.0), ("C2", "J1", "I2", 5.0),
            ("C1", "J2", "I2", 9.0), ("C2", "J2", "I2", 5.0),
        ]
        a0 = analyze(make_records(rows), {}, "judge_item")
        before = weight_threshold_analysis(a0, "I2", 3.0)
        # 找到 C2 在 I2 上校准分与原始分不同的评分并裁决保留原分。
        target = next(
            s["id"]
            for b in a0["contestants"] if b["contestant"] == "C2"
            for ib in b["items"] if ib["item"] == "I2"
            for s in ib["scores"]
            if abs(s["calibrated"] - s["raw"]) > 1e-9
        )
        a1 = analyze(make_records(rows), {target: (True, "保留")}, "judge_item")
        after = weight_threshold_analysis(a1, "I2", 3.0)
        self.assertNotAlmostEqual(critical_ws(before)[0], critical_ws(after)[0])
        self.assertEqual(after["scope"], "judge_item")

    def test_validation_errors(self):
        a = analyze(make_records(BASE), {}, "judge")
        with self.assertRaises(WeightValidationError):
            weight_threshold_analysis(a, "不存在", 3.0)
        with self.assertRaises(WeightValidationError):
            weight_threshold_analysis(a, "I2", 0.5)
        with self.assertRaises(WeightValidationError):
            weight_threshold_analysis(a, "I2", 5.1)
        with self.assertRaises(WeightValidationError):
            weight_threshold_analysis(a, "   ", 3.0)

    def test_ranking_totals_affine(self):
        # 任意快照处总分都等于「截距 + 斜率×w」，且名次为竞赛排名（1224）。
        r = run(upper=2.0)
        analysis = analyze(make_records(BASE), {}, "judge")
        avgs = {
            b["contestant"]: next(x["eff_avg"] for x in b["items"] if x["item"] == "I2")
            for b in analysis["contestants"]
        }
        for e in r["entries"]:
            for row in e["ranking"]:
                name = row["contestant"]
                base_total = next(
                    b["eff_total"] for b in analysis["contestants"]
                    if b["contestant"] == name
                )
                expect = base_total - avgs[name] + avgs[name] * e["w"]
                self.assertAlmostEqual(row["total"], expect, places=9)

    def test_default_upper_equals_one_allowed(self):
        # 上限恰为 1 也合法（0～1）。
        r = run(upper=1.0)
        self.assertEqual(r["upper_bound"], 1.0)
        self.assertTrue(all(0.0 <= e["w"] <= 1.0 for e in r["entries"]))


class TestWeightThresholdNumerics(unittest.TestCase):
    """交点/并列判定的数值稳健性：可表示的真实差异不得被随尺度放大的容差吞没。"""

    BIG = 1e12

    @staticmethod
    def rank_at(entry, name):
        return next(x["rank"] for x in entry["ranking"] if x["contestant"] == name)

    def test_large_scale_crossing_listed_with_rank_swap(self):
        # 大数反例回归：甲 总分(w)=1e12·w+0.5，乙 总分(w)=(1e12+0.5)·w，
        # 两线在 w=1 真实相交并交换领先，不得判为平行或始终并列。
        rows = [
            ("C1", "J", "I1", 0.5), ("C2", "J", "I1", 0.0),
            ("C1", "J", "I2", self.BIG), ("C2", "J", "I2", self.BIG + 0.5),
        ]
        r = run_raw(rows, upper=2.0)
        self.assertEqual(r["always_tied_groups"], [])
        self.assertEqual(r["summary"]["crossing_pairs"], 1)
        self.assertEqual(r["summary"]["critical_points"], 1)
        self.assertEqual(kinds(r["entries"]), ["midpoint", "critical", "midpoint"])
        crit = next(e for e in r["entries"] if e["kind"] == "critical")
        self.assertAlmostEqual(crit["w"], 1.0, places=12)
        self.assertIsNone(crit["boundary"])
        self.assertEqual(crit["ties"], [["C1", "C2"]])
        # 交点处两人总分相等（1e12+0.5）、并列第一。
        self.assertEqual({row["rank"] for row in crit["ranking"]}, {1})
        for row in crit["ranking"]:
            self.assertAlmostEqual(row["total"], self.BIG + 0.5, places=3)
        # 交点前后名次正确：w=0.5 时 C1 领先，w=1.5 时 C2 领先。
        mids = [e for e in r["entries"] if e["kind"] == "midpoint"]
        self.assertAlmostEqual(mids[0]["w"], 0.5, places=12)
        self.assertAlmostEqual(mids[1]["w"], 1.5, places=12)
        self.assertLess(self.rank_at(mids[0], "C1"), self.rank_at(mids[0], "C2"))
        self.assertLess(self.rank_at(mids[1], "C2"), self.rank_at(mids[1], "C1"))

    def test_large_scale_distinct_intercepts_not_always_tied(self):
        # 大数总分下截距不同的平行线（斜率同为 1e12，截距 0.5 vs 0）永不相交，
        # 不得因截距差相对总分尺度很小而误判为始终并列。
        rows = [
            ("C1", "J", "I1", 0.5), ("C2", "J", "I1", 0.0),
            ("C1", "J", "I2", self.BIG), ("C2", "J", "I2", self.BIG),
        ]
        r = run_raw(rows, upper=2.0)
        self.assertEqual(r["always_tied_groups"], [])
        self.assertEqual(critical_ws(r), [])
        self.assertEqual(r["summary"]["crossing_pairs"], 0)
        # 任意倍率下 C1 都恰好领先 0.5，名次不得并列。
        mid = next(e for e in r["entries"] if e["kind"] == "midpoint")
        totals = {row["contestant"]: row["total"] for row in mid["ranking"]}
        self.assertAlmostEqual(totals["C1"] - totals["C2"], 0.5, places=9)
        self.assertLess(self.rank_at(mid, "C1"), self.rank_at(mid, "C2"))

    def test_large_scale_truly_coincident_always_tied(self):
        # 只有两条总分线确实重合（斜率与截距完全相同）才标为始终并列。
        rows = [
            ("C1", "J", "I1", 0.5), ("C2", "J", "I1", 0.5),
            ("C1", "J", "I2", self.BIG), ("C2", "J", "I2", self.BIG),
        ]
        r = run_raw(rows, upper=2.0)
        self.assertEqual(r["always_tied_groups"], [["C1", "C2"]])
        self.assertEqual(critical_ws(r), [])
        self.assertEqual(r["summary"]["crossing_pairs"], 0)
        mid = next(e for e in r["entries"] if e["kind"] == "midpoint")
        self.assertEqual({row["rank"] for row in mid["ranking"]}, {1})

    def test_normal_scale_tiny_slope_difference(self):
        # 普通尺度回归：斜率差 2**-40 是可表示的真实差异，同样不得吞没——
        # C1: 10w+1；C2: (10+2^-40)·w+(1-2^-40) → 恰在 w=1 相交并交换总分先后。
        d = 2.0 ** -40
        rows = [
            ("C1", "J", "I1", 1.0), ("C2", "J", "I1", 1.0 - d),
            ("C1", "J", "I2", 10.0), ("C2", "J", "I2", 10.0 + d),
        ]
        r = run_raw(rows, upper=2.0)
        self.assertEqual(r["always_tied_groups"], [])
        crit = [e for e in r["entries"] if e["kind"] == "critical"]
        self.assertEqual(len(crit), 1)
        self.assertAlmostEqual(crit[0]["w"], 1.0, places=12)
        self.assertEqual(crit[0]["ties"], [["C1", "C2"]])
        # 交点前后总分方向交换（差值 4.5e-13 在排名同分容差内，名次仍并列）。
        mids = [e for e in r["entries"] if e["kind"] == "midpoint"]
        totals = [
            {row["contestant"]: row["total"] for row in e["ranking"]} for e in mids
        ]
        self.assertGreater(totals[0]["C1"], totals[0]["C2"])
        self.assertGreater(totals[1]["C2"], totals[1]["C1"])

    def test_large_scale_crossing_at_upper_boundary(self):
        # 端点交点回归（大数尺度）：C1: 1e12·w+0.5；C2: (1e12+0.25)·w
        # → 恰在 w=2（上限）相交，标记 boundary="end"。
        rows = [
            ("C1", "J", "I1", 0.5), ("C2", "J", "I1", 0.0),
            ("C1", "J", "I2", self.BIG), ("C2", "J", "I2", self.BIG + 0.25),
        ]
        r = run_raw(rows, upper=2.0)
        self.assertEqual(r["always_tied_groups"], [])
        crit = [e for e in r["entries"] if e["kind"] == "critical"]
        self.assertEqual(len(crit), 1)
        self.assertAlmostEqual(crit[0]["w"], 2.0, places=12)
        self.assertEqual(crit[0]["boundary"], "end")
        self.assertEqual(crit[0]["ties"], [["C1", "C2"]])
        self.assertEqual(kinds(r["entries"]), ["midpoint", "critical"])

    def test_large_scale_crossing_at_lower_boundary(self):
        # 端点交点回归（大数尺度）：截距相同（0.5）、斜率不同 → 恰在 w=0 相交；
        # 共享截距不等于两线重合，不得判为始终并列。
        rows = [
            ("C1", "J", "I1", 0.5), ("C2", "J", "I1", 0.5),
            ("C1", "J", "I2", self.BIG), ("C2", "J", "I2", self.BIG + 0.5),
        ]
        r = run_raw(rows, upper=2.0)
        self.assertEqual(r["always_tied_groups"], [])
        crit = [e for e in r["entries"] if e["kind"] == "critical"]
        self.assertEqual(len(crit), 1)
        self.assertAlmostEqual(crit[0]["w"], 0.0, places=12)
        self.assertEqual(crit[0]["boundary"], "start")
        self.assertEqual(crit[0]["ties"], [["C1", "C2"]])
        self.assertEqual(kinds(r["entries"]), ["critical", "midpoint"])

    def test_close_but_distinct_crossings_listed_separately(self):
        # 近邻真实交点回归：甲 (I1=2e12, I2=1e12)、乙 (1e12, 2e12)、丙 (0, 3e12-1000)，
        # 分析 I2 的 0～2 倍率。三对交点 w=1、w≈1.0000000005、w≈1.000000001
        # 可表示且真实不同，必须分别列出；w=1 的并列组只含甲、乙，不得错并入丙。
        rows = [
            ("甲", "J", "I1", 2e12), ("甲", "J", "I2", 1e12),
            ("乙", "J", "I1", 1e12), ("乙", "J", "I2", 2e12),
            ("丙", "J", "I1", 0.0), ("丙", "J", "I2", 3e12 - 1000),
        ]
        r = run_raw(rows, upper=2.0)
        self.assertEqual(r["always_tied_groups"], [])
        self.assertEqual(r["summary"]["crossing_pairs"], 3)
        self.assertEqual(r["summary"]["critical_points"], 3)
        crit = [e for e in r["entries"] if e["kind"] == "critical"]
        self.assertEqual(len(crit), 3)
        ws = [e["w"] for e in crit]
        self.assertAlmostEqual(ws[0], 1.0, places=15)
        self.assertAlmostEqual(ws[1], 1.0000000005, places=12)
        self.assertAlmostEqual(ws[2], 1.000000001, places=12)
        self.assertTrue(all(e["boundary"] is None for e in crit))
        # 每个交点的并列组只含该点真实相等的选手（组内按名字字典序）。
        self.assertEqual(crit[0]["ties"], [["乙", "甲"]])
        self.assertEqual(crit[1]["ties"], [["丙", "甲"]])
        self.assertEqual(crit[2]["ties"], [["丙", "乙"]])
        # w=1 处丙的总分为 3e12-1000，不与甲乙并列（名次 3，无并列标记）。
        at_one = crit[0]
        row_of = {row["contestant"]: row for row in at_one["ranking"]}
        self.assertAlmostEqual(row_of["丙"]["total"], 3e12 - 1000, places=3)
        self.assertEqual((row_of["甲"]["rank"], row_of["乙"]["rank"]), (1, 1))
        self.assertEqual(row_of["丙"]["rank"], 3)
        self.assertFalse(row_of["丙"]["tied"])
        # 交点处并列组的名次一致（吸收总分的浮点噪声）。
        for e in crit:
            for g in e["ties"]:
                self.assertEqual(
                    len({row["rank"] for row in e["ranking"] if row["contestant"] in g}), 1
                )
        # 每对相邻临界点（含端点）之间都有名次快照，且次序逐区间交换。
        self.assertEqual(
            kinds(r["entries"]),
            ["midpoint", "critical", "midpoint", "critical",
             "midpoint", "critical", "midpoint"],
        )
        mids = [e for e in r["entries"] if e["kind"] == "midpoint"]
        self.assertEqual(len(mids), 4)
        order = [
            [row["contestant"] for row in e["ranking"]] for e in mids
        ]
        self.assertEqual(order[0], ["甲", "乙", "丙"])  # (0, 1)
        self.assertEqual(order[1], ["乙", "甲", "丙"])  # (1, 1.0000000005)
        self.assertEqual(order[2], ["乙", "丙", "甲"])  # (1.0000000005, 1.000000001)
        self.assertEqual(order[3], ["丙", "乙", "甲"])  # (1.000000001, 2)

    def test_same_root_float_noise_still_merged(self):
        # 同一数学交点的浮点偏差仍归为一处：三线的真实交点都是 w=1/3，
        # 各对计算结果只有浮点误差级差异，不得拆成多个临界点。
        third = 1.0 / 3.0
        rows = []
        for c, s in [("C1", 1.0), ("C2", 2.0), ("C3", 4.0)]:
            # 截距 = 2 - s/3，使三线在 w=1/3 处同取总分 2。
            rows.append((c, "J", "I1", 2.0 - s * third))
            rows.append((c, "J", "I2", s))
        r = run_raw(rows, upper=2.0)
        crit = [e for e in r["entries"] if e["kind"] == "critical"]
        self.assertEqual(len(crit), 1)
        self.assertAlmostEqual(crit[0]["w"], third, places=12)
        self.assertEqual(crit[0]["ties"], [["C1", "C2", "C3"]])
        self.assertEqual(r["summary"]["crossing_pairs"], 3)


if __name__ == "__main__":
    unittest.main()
