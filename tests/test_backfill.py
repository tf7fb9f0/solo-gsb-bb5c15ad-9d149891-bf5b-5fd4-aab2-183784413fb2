"""补评分派（plan_backfill）纯算法测试。

覆盖：单元缺口与评委资格、容量/缺口约束下的最大分派、目标人数扣减、
固定「评委 → 项目 → 选手」次序的稳定（并列）输出、每单元未满足汇总、
参数校验。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backfill import (  # noqa: E402
    BackfillValidationError,
    plan_backfill,
)


def recs(data):
    # data: list of (contestant, judge, item[, score])
    out = []
    for i, row in enumerate(data):
        contestant, judge, item = row[0], row[1], row[2]
        score = row[3] if len(row) > 3 else 8.0
        out.append(
            {"id": i + 1, "contestant": contestant, "judge": judge, "item": item, "score": score}
        )
    return out


def triples(plan):
    return [(a["judge"], a["contestant"], a["item"]) for a in plan["assignments"]]


class TestGapAndEligibility(unittest.TestCase):
    def test_full_matrix_has_no_eligible_edges(self):
        data = [
            ("A", "J1", "P"), ("A", "J2", "P"),
            ("B", "J1", "P"), ("B", "J2", "P"),
        ]
        # 每个单元都已被所有（两位）评委评过，target 再高也没有合格评委。
        plan = plan_backfill(recs(data), 3, {"J1": 9, "J2": 9})
        s = plan["summary"]
        self.assertEqual(s["units"], 2)
        self.assertEqual(s["total_gap"], 2)
        self.assertEqual(s["eligible_edges"], 0)
        self.assertEqual(s["assigned_total"], 0)
        self.assertEqual(s["unmet_total"], 2)
        for u in plan["units"]:
            self.assertEqual(u["existing"], 2)
            self.assertEqual(u["gap"], 1)
            self.assertEqual(u["eligible_judges"], [])

    def test_target_below_existing_has_no_gap(self):
        data = [
            ("A", "J1", "P"), ("A", "J2", "P"),
            ("B", "J1", "P"),
        ]
        # target 1：两个单元都至少有 1 条评分 → 无缺口。
        plan = plan_backfill(recs(data), 1, {"J2": 5})
        self.assertEqual(plan["summary"]["gap_units"], 0)
        self.assertEqual(plan["summary"]["total_gap"], 0)
        self.assertEqual(plan["assignments"], [])

    def test_eligibility_requires_scoring_same_item(self):
        # J2 评过 P2，但没评过 P1；不能接 P1 的单元。
        data = [
            ("A", "J1", "P1"),
            ("B", "J1", "P1"),
            ("A", "J2", "P2"),
        ]
        plan = plan_backfill(recs(data), 2, {"J2": 9})
        eligible = {
            (u["contestant"], u["item"]): u["eligible_judges"] for u in plan["units"]
        }
        # (B,P1) 缺口 1，但唯一评过 P1 的另一位评委不存在（J2 只评过 P2）。
        self.assertEqual(eligible[("B", "P1")], [])
        # (A,P2) 缺口 1，但只有 J2 评过 P2（且已评该单元），J1 没评过 P2。
        self.assertEqual(eligible[("A", "P2")], [])
        self.assertEqual(plan["summary"]["assigned_total"], 0)
        self.assertEqual(plan["summary"]["unmet_total"], 3)

    def test_scored_item_but_not_unit_is_eligible(self):
        # J2 给 A 的 P1 打过分（评过该项目），尚未给 B 的 P1 打分 → 可接 (B,P1)。
        data = [
            ("A", "J1", "P1"), ("A", "J2", "P1"),
            ("B", "J1", "P1"),
        ]
        plan = plan_backfill(recs(data), 2, {"J2": 5})
        self.assertEqual(triples(plan), [("J2", "B", "P1")])
        self.assertEqual(plan["summary"]["unmet_total"], 0)
        unit = next(u for u in plan["units"] if (u["contestant"], u["item"]) == ("B", "P1"))
        self.assertEqual(unit["eligible_judges"], ["J2"])
        self.assertEqual(unit["existing"], 1)
        self.assertEqual(unit["gap"], 1)
        self.assertEqual(unit["assigned"], 1)
        self.assertEqual(unit["unmet"], 0)


class TestOptimalAssignment(unittest.TestCase):
    def test_capacity_and_gap_bounds(self):
        # P1：(A) 有 J1,J2；(B) 只有 J1。P2：(A) 只有 J1。
        data = [
            ("A", "J1", "P1"), ("A", "J2", "P1"),
            ("B", "J1", "P1"),
            ("A", "J1", "P2"),
        ]
        # target 3：(A,P1) 缺 1（J1/J2 均已评该单元，无人合格）、
        # (B,P1) 缺 2（J2 评过 P1 未评该单元，合格）、(A,P2) 缺 2（无人合格）。
        plan = plan_backfill(recs(data), 3, {"J2": 1})
        s = plan["summary"]
        self.assertEqual(s["total_gap"], 5)
        self.assertEqual(s["assigned_total"], 1)
        self.assertEqual(s["unmet_total"], 4)
        self.assertEqual(triples(plan), [("J2", "B", "P1")])
        j2 = next(j for j in plan["judges"] if j["judge"] == "J2")
        self.assertEqual((j2["capacity"], j2["assigned"], j2["remaining"]), (1, 1, 0))

    def test_lexicographic_tie_break_picks_earlier_contestant(self):
        # J1 评过 P1（在 C 身上），A/B 均由 J2,J3 评过；只有 J1 有容量。
        data = [
            ("C", "J1", "P1"),
            ("A", "J2", "P1"), ("A", "J3", "P1"),
            ("B", "J2", "P1"), ("B", "J3", "P1"),
        ]
        plan = plan_backfill(recs(data), 3, {"J1": 1})
        # 两个最优并列（J1→A 或 J1→B），固定次序取 A。
        self.assertEqual(triples(plan), [("J1", "A", "P1")])
        self.assertEqual(plan["summary"]["assigned_total"], 1)

    def test_reroute_scenario_uses_full_capacity(self):
        # C 仅 J1 评过；A/B 由 J2,J3 评过。target 3。
        data = [
            ("C", "J1", "P1"),
            ("A", "J2", "P1"), ("A", "J3", "P1"),
            ("B", "J2", "P1"), ("B", "J3", "P1"),
        ]
        plan = plan_backfill(recs(data), 3, {"J1": 1, "J2": 1, "J3": 1})
        # 最优 3：J1 补 A（固定次序优先），J2/J3 补 C 的两个缺口。
        self.assertEqual(
            triples(plan),
            [("J1", "A", "P1"), ("J2", "C", "P1"), ("J3", "C", "P1")],
        )
        self.assertEqual(plan["summary"]["assigned_total"], 3)
        self.assertEqual(plan["summary"]["unmet_total"], 1)  # B 的缺口无人可接

    def test_stable_output_is_deterministic(self):
        data = [
            ("C", "J1", "P1"),
            ("A", "J2", "P1"), ("A", "J3", "P1"),
            ("B", "J2", "P1"), ("B", "J3", "P1"),
        ]
        first = plan_backfill(recs(data), 3, {"J1": 1, "J2": 1, "J3": 1})
        for _ in range(5):
            again = plan_backfill(recs(data), 3, {"J1": 1, "J2": 1, "J3": 1})
            self.assertEqual(again["assignments"], first["assignments"])

    def test_assignment_order_judge_item_contestant(self):
        # 记录使项目首次出现顺序为 P2 先于 P1，选手顺序为 C,B,A。
        data = [
            ("C", "J1", "P2"), ("C", "J2", "P1"),
            ("B", "J1", "P1"), ("A", "J2", "P2"),
        ]
        plan = plan_backfill(recs(data), 2, {"J1": 2, "J2": 2})
        # 单元（项目→选手）：(C,P2),(A,P2),(C,P1),(B,P1)
        self.assertEqual(
            triples(plan),
            [
                ("J1", "A", "P2"),  # J1 评过 P2（在 C），未评 A
                ("J1", "C", "P1"),  # J1 评过 P1（在 B），未评 C
                ("J2", "C", "P2"),
                ("J2", "B", "P1"),
            ],
        )
        self.assertEqual(
            [(u["item"], u["contestant"]) for u in plan["units"]],
            [("P2", "C"), ("P2", "A"), ("P1", "C"), ("P1", "B")],
        )

    def test_unmet_summary_per_unit(self):
        # 5 个缺口单元都只缺同一位评委 J4；J4 容量 3 → 前 3 个单元满足，后 2 个未满足。
        data = [
            ("S1", "J4", "P"), ("S1", "J1", "P"), ("S1", "J2", "P"), ("S1", "J3", "P"),
            ("S2", "J1", "P"), ("S2", "J2", "P"), ("S2", "J3", "P"),
            ("S3", "J1", "P"), ("S3", "J2", "P"), ("S3", "J3", "P"),
            ("S4", "J1", "P"), ("S4", "J2", "P"), ("S4", "J3", "P"),
            ("S5", "J1", "P"), ("S5", "J2", "P"), ("S5", "J3", "P"),
            ("S6", "J1", "P"), ("S6", "J2", "P"), ("S6", "J3", "P"),
        ]
        plan = plan_backfill(recs(data), 4, {"J4": 3})
        s = plan["summary"]
        self.assertEqual(s["gap_units"], 5)
        self.assertEqual(s["assigned_total"], 3)
        self.assertEqual(s["unmet_total"], 2)
        self.assertEqual(s["fully_met_units"], 3)
        # J4 首次出现于第一条 → 选手顺序里 S1 在最前；按选手固定次序取前 3 个缺口单元。
        self.assertEqual(
            [a["contestant"] for a in plan["assignments"]],
            ["S2", "S3", "S4"],
        )
        unmet = [(u["contestant"], u["unmet"]) for u in plan["units"] if u["unmet"] > 0]
        self.assertEqual(unmet, [("S5", 1), ("S6", 1)])


class TestValidation(unittest.TestCase):
    def test_unknown_judge_rejected(self):
        with self.assertRaises(BackfillValidationError) as ctx:
            plan_backfill(recs([("A", "J1", "P")]), 2, {"X": 1})
        self.assertIn("X", str(ctx.exception))

    def test_negative_and_non_int_rejected(self):
        data = recs([("A", "J1", "P")])
        with self.assertRaises(BackfillValidationError):
            plan_backfill(data, -1, {})
        with self.assertRaises(BackfillValidationError):
            plan_backfill(data, 2, {"J1": -1})
        with self.assertRaises(BackfillValidationError):
            plan_backfill(data, 2.5, {})
        with self.assertRaises(BackfillValidationError):
            plan_backfill(data, True, {})
        with self.assertRaises(BackfillValidationError):
            plan_backfill(data, 2, {"J1": 1.5})

    def test_missing_capacity_defaults_zero(self):
        data = recs([("A", "J1", "P"), ("A", "J2", "P"), ("B", "J1", "P")])
        plan = plan_backfill(data, 2, None)
        self.assertEqual(plan["summary"]["capacity_total"], 0)
        self.assertEqual(plan["summary"]["assigned_total"], 0)
        for j in plan["judges"]:
            self.assertEqual(j["capacity"], 0)
        # 空 dict 与缺省等价。
        self.assertEqual(
            plan_backfill(data, 2, {})["assignments"],
            plan["assignments"],
        )


if __name__ == "__main__":
    unittest.main()
