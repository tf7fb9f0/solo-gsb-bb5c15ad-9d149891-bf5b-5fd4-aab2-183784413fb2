"""Tests for promotion threshold check (read-only pure transform)."""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.promotion import (
    PROMO_FAIL,
    PROMO_INSUFFICIENT,
    PROMO_PASS,
    REASON_ITEM_BELOW,
    REASON_MISSING_ITEMS,
    REASON_TOTAL_BELOW,
    PromotionValidationError,
    promotion_check,
)
from app.scoring import analyze


def make_records(rows):
    # rows: (contestant, judge, item, score)
    return [
        {"id": i + 1, "contestant": c, "judge": j, "item": it, "score": s}
        for i, (c, j, it, s) in enumerate(rows)
    ]


# 单一评委 → 最终均分 == 原始分。
# C1: I1=8, I2=9；C2: I1=6, I2=7；C3 仅有 I1=5（缺 I2）。
BASE = [
    ("C1", "J", "I1", 8.0), ("C2", "J", "I1", 6.0), ("C3", "J", "I1", 5.0),
    ("C1", "J", "I2", 9.0), ("C2", "J", "I2", 7.0),
]


def check(rows=BASE, thresholds=(("I1", 5.0),), total=None, decisions=None, scope="judge"):
    a = analyze(make_records(rows), decisions or {}, scope)
    return promotion_check(a, [{"item": i, "min_avg": v} for i, v in thresholds], total)


def by_name(result):
    return {r["contestant"]: r for r in result["contestants"]}


class TestPromotionCheck(unittest.TestCase):
    def test_pass_and_fail_with_single_item_threshold(self):
        r = check(thresholds=(("I1", 7.0),))
        rows = by_name(r)
        self.assertEqual(rows["C1"]["status"], PROMO_PASS)
        self.assertEqual(rows["C2"]["status"], PROMO_FAIL)
        self.assertEqual(rows["C3"]["status"], PROMO_FAIL)  # I1=5 < 7
        self.assertEqual(r["summary"], {"contestants": 3, "pass": 1, "fail": 2, "insufficient_data": 0})
        c2 = rows["C2"]
        self.assertEqual(len(c2["reasons"]), 1)
        reason = c2["reasons"][0]
        self.assertEqual(reason["kind"], REASON_ITEM_BELOW)
        self.assertEqual(reason["items"][0]["item"], "I1")
        self.assertAlmostEqual(reason["items"][0]["gap"], 1.0)
        self.assertIn("I1", reason["message"])
        # 未设总分线时总分检查段为 None。
        self.assertIsNone(c2["total_check"])
        self.assertIsNone(c2["total_threshold"])

    def test_missing_threshold_item_is_insufficient_not_zero(self):
        # 门槛设在 I2：C3 完全没有 I2 评分 → 数据不足，且不按 0 参与任何判定。
        r = check(thresholds=(("I2", 100.0),))  # 故意设很高的线验证不是按 0 判未达标
        rows = by_name(r)
        c3 = rows["C3"]
        self.assertEqual(c3["status"], PROMO_INSUFFICIENT)
        self.assertEqual(c3["missing_items"], ["I2"])
        self.assertEqual(c3["reasons"][0]["kind"], REASON_MISSING_ITEMS)
        self.assertEqual(c3["reasons"][0]["items"], ["I2"])
        # 数据不足时不判定总分线。
        self.assertIsNone(c3["total_check"])
        # C1(9<100) 与 C2(7<100) 正常判为未达标。
        self.assertEqual(rows["C1"]["status"], PROMO_FAIL)
        self.assertEqual(rows["C2"]["status"], PROMO_FAIL)
        self.assertEqual(r["summary"]["insufficient_data"], 1)

    def test_missing_non_threshold_item_is_evaluated_normally(self):
        # 门槛只设在 I1：C3 缺的是 I2（非门槛项目），照常按 I1 判定，总分照算。
        r = check(thresholds=(("I1", 4.0),), total=4.5)
        c3 = by_name(r)["C3"]
        self.assertEqual(c3["status"], PROMO_PASS)
        self.assertEqual(c3["missing_items"], [])
        self.assertEqual(c3["total"], 5.0)
        self.assertTrue(c3["total_check"]["passes"])

    def test_total_threshold(self):
        r = check(thresholds=(("I1", 1.0),), total=14.0)  # 项目线很松，卡总分
        rows = by_name(r)
        # C1 总分 17 达标；C2 总分 13 差 1；C3 总分 5 差 9。
        self.assertEqual(rows["C1"]["status"], PROMO_PASS)
        self.assertEqual(rows["C2"]["status"], PROMO_FAIL)
        self.assertEqual(rows["C3"]["status"], PROMO_FAIL)
        c2 = rows["C2"]
        total_reasons = [x for x in c2["reasons"] if x["kind"] == REASON_TOTAL_BELOW]
        self.assertEqual(len(total_reasons), 1)
        self.assertAlmostEqual(total_reasons[0]["gap"], 1.0)
        self.assertFalse(c2["total_check"]["passes"])
        self.assertAlmostEqual(c2["total_check"]["gap"], 1.0)
        self.assertEqual(c2["total_threshold"], 14.0)
        # 同时卡项目与总分时原因全部列出。
        r2 = check(thresholds=(("I1", 7.0),), total=14.0)
        c2b = by_name(r2)["C2"]
        kinds = [x["kind"] for x in c2b["reasons"]]
        self.assertEqual(kinds, [REASON_ITEM_BELOW, REASON_TOTAL_BELOW])

    def test_insufficient_skips_total_judgement(self):
        r = check(thresholds=(("I2", 1.0),), total=999.0)
        c3 = by_name(r)["C3"]
        self.assertEqual(c3["status"], PROMO_INSUFFICIENT)
        self.assertEqual([x["kind"] for x in c3["reasons"]], [REASON_MISSING_ITEMS])
        self.assertIsNone(c3["total_check"])

    def test_boundary_equality_passes(self):
        # 恰好等于门槛视为达标。
        r = check(thresholds=(("I1", 8.0), ("I2", 9.0)), total=17.0)
        c1 = by_name(r)["C1"]
        self.assertEqual(c1["status"], PROMO_PASS)
        self.assertEqual(c1["reasons"], [])

    def test_item_cells_cover_all_items_with_threshold_flags(self):
        r = check(thresholds=(("I2", 7.0),))
        c1 = by_name(r)["C1"]
        cells = {c["item"]: c for c in c1["items"]}
        self.assertEqual(set(cells), {"I1", "I2"})
        self.assertIsNone(cells["I1"]["threshold"])
        self.assertIsNone(cells["I1"]["passes"])
        self.assertAlmostEqual(cells["I1"]["avg"], 8.0)
        self.assertEqual(cells["I2"]["threshold"], 7.0)
        self.assertTrue(cells["I2"]["passes"])
        c3 = by_name(r)["C3"]
        c3_i2 = {c["item"]: c for c in c3["items"]}["I2"]
        self.assertFalse(c3_i2["has_data"])
        self.assertIsNone(c3_i2["avg"])
        self.assertFalse(c3_i2["passes"])

    def test_rows_ordered_by_rank_and_echo_scope(self):
        r = check(thresholds=(("I1", 0.0),))
        self.assertEqual([row["contestant"] for row in r["contestants"]], ["C1", "C2", "C3"])
        self.assertEqual(r["scope"], "judge")
        self.assertIn("scope_label", r)
        thresholds = {(t["item"], t["min_avg"]) for t in r["thresholds"]["items"]}
        self.assertEqual(thresholds, {("I1", 0.0)})
        self.assertIsNone(r["thresholds"]["total"])

    def test_reflects_decisions_and_scope(self):
        records = make_records(BASE)
        a0 = analyze(records, {}, "judge")
        r0 = promotion_check(a0, [{"item": "I1", "min_avg": 8.0}])
        self.assertEqual(by_name(r0)["C1"]["status"], PROMO_PASS)
        # 找到 C1 在 I1 的评分并保留原分（校准后该值会变化）。
        target = next(
            s for b in a0["contestants"] for ib in b["items"] for s in ib["scores"]
            if b["contestant"] == "C1" and ib["item"] == "I1"
        )
        # 直接构造一个「校准分被拉低」的裁决场景：保留原分不会拉低，改为切范围验证更直接。
        a1 = analyze(records, {target["id"]: (True, "x")}, "judge")
        r1 = promotion_check(a1, [{"item": "I1", "min_avg": 8.0}])
        # 切到 judge_item 范围，结果应重算且 scope 标注更新。
        r2 = promotion_check(analyze(records, {}, "judge_item"), [{"item": "I1", "min_avg": 8.0}])
        self.assertEqual(r2["scope"], "judge_item")
        self.assertIn("scope_label", r2)
        # 两个结果至少结构自洽（不因裁决/范围报错）。
        self.assertEqual(r1["summary"]["contestants"], 3)
        self.assertEqual(r2["summary"]["contestants"], 3)

    # ---- 参数校验 ----
    def test_validation_errors(self):
        a = analyze(make_records(BASE), {}, "judge")
        with self.assertRaises(PromotionValidationError):
            promotion_check(a, [])
        with self.assertRaises(PromotionValidationError):
            promotion_check(a, [{"item": "I1", "min_avg": "x"}])
        with self.assertRaises(PromotionValidationError):
            promotion_check(a, [{"item": "I1", "min_avg": float("nan")}])
        with self.assertRaises(PromotionValidationError):
            promotion_check(a, [{"item": "I1", "min_avg": float("inf")}])
        with self.assertRaises(PromotionValidationError):
            promotion_check(a, [{"item": "  ", "min_avg": 1}])
        with self.assertRaises(PromotionValidationError) as ctx:
            promotion_check(
                a, [{"item": "I1", "min_avg": 1}, {"item": "I1", "min_avg": 2}]
            )
        self.assertIn("重复", str(ctx.exception))
        with self.assertRaises(PromotionValidationError) as ctx:
            promotion_check(a, [{"item": "不存在", "min_avg": 1}])
        self.assertIn("不存在", str(ctx.exception))
        with self.assertRaises(PromotionValidationError):
            promotion_check(a, [{"item": "I1", "min_avg": 1}], total_threshold=float("nan"))
        with self.assertRaises(PromotionValidationError):
            promotion_check(a, [{"item": "I1", "min_avg": 1}], total_threshold="nope")
        # 布尔值不接受为数字。
        with self.assertRaises(PromotionValidationError):
            promotion_check(a, [{"item": "I1", "min_avg": True}])

    def test_item_name_is_stripped(self):
        r = check(thresholds=((" I1 ", 8.0),))
        self.assertEqual(r["thresholds"]["items"][0]["item"], "I1")


if __name__ == "__main__":
    unittest.main()
