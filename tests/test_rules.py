"""Tests for batch adjudication rule matching and priority resolution."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.rules import (
    RuleValidationError,
    evaluate_rules,
    normalize_rule,
    normalize_rules,
    render_reason,
    rule_matches,
    score_rows_from_analysis,
)
from app.scoring import analyze


def rows(data):
    # data: (id, contestant, judge, item, raw, calibrated)
    return [
        {"id": i, "contestant": c, "judge": j, "item": it, "raw": raw, "calibrated": cal}
        for i, (c, j, it, raw, cal) in enumerate(data, start=1)
    ]


SAMPLE = rows([
    ("C1", "JA", "P1", 5.0, 9.0),
    ("C2", "JA", "P1", 9.0, 5.0),
    ("C1", "JB", "P1", 7.0, 7.0),
    ("C2", "JB", "P1", 8.0, 8.0),
    ("C1", "JA", "P2", 10.0, 10.0),
])


def rule(**kw):
    base = {
        "name": "", "judge": None, "item": None,
        "raw_min": None, "raw_max": None, "cal_min": None, "cal_max": None,
        "use_raw": False, "reason_template": "", "priority": 100,
    }
    base.update(kw)
    return base


class TestNormalize(unittest.TestCase):
    def test_blank_strings_become_none(self):
        r = normalize_rule({"judge": "  ", "item": "P1", "priority": "5"}, 1)
        self.assertIsNone(r["judge"])
        self.assertEqual(r["item"], "P1")
        self.assertEqual(r["priority"], 5)

    def test_bad_interval(self):
        with self.assertRaises(RuleValidationError):
            normalize_rule({"raw_min": 9, "raw_max": 8}, 1)
        with self.assertRaises(RuleValidationError):
            normalize_rule({"cal_min": 9, "cal_max": 8}, 2)

    def test_bad_number_and_priority(self):
        with self.assertRaises(RuleValidationError):
            normalize_rule({"raw_min": "abc"}, 1)
        with self.assertRaises(RuleValidationError):
            normalize_rule({"priority": "x"}, 1)
        with self.assertRaises(RuleValidationError):
            normalize_rule({"priority": -1}, 1)
        # NaN / inf rejected
        with self.assertRaises(RuleValidationError):
            normalize_rule({"raw_min": float("inf")}, 1)

    def test_too_many_rules(self):
        with self.assertRaises(RuleValidationError):
            normalize_rules([{"judge": "J"} for _ in range(201)])


class TestMatching(unittest.TestCase):
    def test_match_by_judge_item_ranges(self):
        r = rule(judge="JA", item="P1", raw_min=4, raw_max=6, cal_min=8, cal_max=10)
        self.assertTrue(rule_matches(r, SAMPLE[0]))   # 5.0 / 9.0
        self.assertFalse(rule_matches(r, SAMPLE[1]))  # raw out
        self.assertFalse(rule_matches(r, SAMPLE[2]))  # wrong judge
        self.assertFalse(rule_matches(r, SAMPLE[4]))  # wrong item

    def test_inclusive_boundaries(self):
        r = rule(raw_min=5.0, raw_max=9.0, cal_min=5.0, cal_max=9.0)
        self.assertTrue(rule_matches(r, SAMPLE[0]))
        self.assertTrue(rule_matches(r, SAMPLE[1]))

    def test_empty_rule_matches_all(self):
        r = rule()
        self.assertTrue(all(rule_matches(r, s) for s in SAMPLE))


class TestEvaluate(unittest.TestCase):
    def test_hits_misses_and_wins(self):
        rules = [rule(judge="JA"), rule(judge="JB")]
        res = evaluate_rules(SAMPLE, rules)
        self.assertEqual(res["total_scores"], 5)
        self.assertEqual(res["matched"], 5)
        self.assertEqual(res["unmatched"], 0)
        self.assertEqual(res["rules"][0]["hits"], 3)
        self.assertEqual(res["rules"][0]["wins"], 3)
        self.assertEqual(res["rules"][0]["misses"], 2)
        self.assertEqual(res["rules"][1]["hits"], 2)
        self.assertEqual(res["conflicted"], 0)

    def test_priority_resolves_conflict_smaller_wins(self):
        # Both match score 1 (JA, raw 5). Rule B with priority 1 must win.
        rules = [
            rule(name="A", judge="JA", use_raw=True, reason_template="A", priority=10),
            rule(name="B", raw_max=6, use_raw=False, reason_template="B", priority=1),
        ]
        res = evaluate_rules(SAMPLE, rules)
        self.assertEqual(res["conflicted"], 1)
        a0 = next(a for a in res["assignments"] if a["score_id"] == 1)
        self.assertEqual(a0["matched_rule_indexes"], [0, 1])
        self.assertEqual(a0["winner_index"], 1)
        self.assertFalse(a0["use_raw"])
        self.assertEqual(a0["reason"], "B")
        # Each conflicting rule reports one conflict; only winner gets the win.
        self.assertEqual(res["rules"][0]["conflicts"], 1)
        self.assertEqual(res["rules"][1]["conflicts"], 1)
        self.assertEqual(res["rules"][0]["wins"], 2)  # JA rows 1,5
        self.assertEqual(res["rules"][1]["wins"], 1)

    def test_tie_priority_first_in_list_wins(self):
        rules = [
            rule(use_raw=True, reason_template="first", priority=5),
            rule(use_raw=False, reason_template="second", priority=5),
        ]
        res = evaluate_rules(SAMPLE, rules)
        self.assertEqual(res["conflicted"], 5)
        self.assertTrue(all(a["winner_index"] == 0 for a in res["assignments"]))
        self.assertTrue(all(a["reason"] == "first" for a in res["assignments"]))

    def test_unmatched_kept_and_would_change(self):
        current = {1: (False, "r1"), 2: (True, "keep")}
        rules = [rule(judge="JA", use_raw=False, reason_template="r1")]
        res = evaluate_rules(SAMPLE, rules, current)
        # score 1 already matches the intended verdict -> not a change;
        # score 2 currently raw but rule says calibrated -> a change;
        # score 5 rule calibrated with reason "r1", no current decision -> change.
        self.assertEqual(res["matched"], 3)
        self.assertEqual(res["unmatched"], 2)
        self.assertEqual(res["would_change"], 2)
        unmatched_ids = {a["score_id"] for a in res["assignments"] if not a["matched"]}
        self.assertEqual(unmatched_ids, {3, 4})
        # unmatched assignments carry no verdict
        none_a = next(a for a in res["assignments"] if a["score_id"] == 3)
        self.assertIsNone(none_a["use_raw"])
        self.assertIsNone(none_a["winner_index"])

    def test_empty_rules(self):
        res = evaluate_rules(SAMPLE, [])
        self.assertEqual(res["unmatched"], 5)
        self.assertEqual(res["matched"], 0)
        self.assertEqual(res["rules"], [])


class TestReasonTemplate(unittest.TestCase):
    def test_placeholders(self):
        out = render_reason(
            "{judge}在{item}给出{raw}，校准{calibrated}", SAMPLE[0]
        )
        self.assertEqual(out, "JA在P1给出5.00，校准9.00")

    def test_unknown_placeholder_kept(self):
        out = render_reason("{judge}-{unknown}", SAMPLE[0])
        self.assertEqual(out, "JA-{unknown}")

    def test_empty_template(self):
        self.assertEqual(render_reason("", SAMPLE[0]), "")


class TestFromAnalysis(unittest.TestCase):
    def test_rows_carry_item_and_contestant(self):
        raw = [
            {"id": 1, "contestant": "C1", "judge": "JA", "item": "P1", "score": 9.0},
            {"id": 2, "contestant": "C1", "judge": "JB", "item": "P1", "score": 6.0},
        ]
        a = analyze(raw, {}, "judge")
        rows_out = score_rows_from_analysis(a)
        self.assertEqual({r["item"] for r in rows_out}, {"P1"})
        self.assertEqual({r["contestant"] for r in rows_out}, {"C1"})
        ids = {r["id"]: r for r in rows_out}
        self.assertAlmostEqual(ids[1]["raw"], 9.0)
        self.assertIn("calibrated", ids[1])


if __name__ == "__main__":
    unittest.main()
