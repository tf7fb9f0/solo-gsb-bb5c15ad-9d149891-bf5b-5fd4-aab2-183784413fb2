"""Tests for merge-import diffing: added/unchanged/changed/removed classification."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.merge import (
    DECISION_CLEAR,
    DECISION_KEEP,
    DECISION_NONE,
    STATUS_ADDED,
    STATUS_CHANGED,
    STATUS_REMOVED,
    STATUS_UNCHANGED,
    MergeValidationError,
    basis_matches,
    build_merge_plan,
    current_basis,
    normalize_incoming_records,
    valid_basis_shape,
)


def cur(rows):
    # rows: (id, contestant, judge, item, score)
    return [
        {"id": i, "contestant": c, "judge": j, "item": it, "score": s}
        for i, (c, j, it, s) in enumerate(rows, start=1)
    ]


def inc(rows):
    # rows: (contestant, judge, item, score)
    return [
        {"contestant": c, "judge": j, "item": it, "score": s}
        for c, j, it, s in rows
    ]


def by_key(plan):
    return {(r["contestant"], r["judge"], r["item"]): r for r in plan["rows"]}


class TestBuildMergePlan(unittest.TestCase):
    def test_classifies_all_four_statuses(self):
        current = cur([
            ("C1", "JA", "P1", 8.0),   # changed
            ("C1", "JB", "P1", 7.0),   # unchanged
            ("C2", "JA", "P1", 9.0),   # removed
        ])
        incoming = inc([
            ("C1", "JA", "P1", 8.5),
            ("C1", "JB", "P1", 7.0),
            ("C3", "JA", "P1", 6.0),   # added
        ])
        plan = build_merge_plan(current, incoming, {})
        self.assertEqual(plan["counts"], {
            STATUS_CHANGED: 1, STATUS_ADDED: 1, STATUS_REMOVED: 1, STATUS_UNCHANGED: 1,
        })
        self.assertEqual(plan["will_modify"], 3)
        self.assertEqual(plan["total_current"], 3)
        self.assertEqual(plan["total_incoming"], 3)
        rows = by_key(plan)
        ch = rows[("C1", "JA", "P1")]
        self.assertEqual(ch["status"], STATUS_CHANGED)
        self.assertEqual(ch["old_score"], 8.0)
        self.assertEqual(ch["new_score"], 8.5)
        self.assertEqual(ch["score_id"], 1)
        self.assertEqual(rows[("C1", "JB", "P1")]["status"], STATUS_UNCHANGED)
        rm = rows[("C2", "JA", "P1")]
        self.assertEqual(rm["status"], STATUS_REMOVED)
        self.assertIsNone(rm["new_score"])
        self.assertEqual(rm["old_score"], 9.0)
        ad = rows[("C3", "JA", "P1")]
        self.assertEqual(ad["status"], STATUS_ADDED)
        self.assertIsNone(ad["old_score"])
        self.assertEqual(ad["new_score"], 6.0)
        self.assertIsNone(ad["score_id"])

    def test_row_order_buckets_then_preserves_inner_order(self):
        current = cur([
            ("U1", "J", "P", 1.0),   # unchanged (current order 1)
            ("X1", "J", "P", 1.0),   # changed   (current order 2)
            ("R1", "J", "P", 1.0),   # removed   (current order 3)
            ("X2", "J", "P", 2.0),   # changed   (current order 4)
            ("U2", "J", "P", 3.0),   # unchanged (current order 5)
        ])
        incoming = inc([
            ("X1", "J", "P", 9.0),
            ("X2", "J", "P", 8.0),
            ("A1", "J", "P", 5.0),   # added first
            ("A2", "J", "P", 5.0),   # added second
            ("U1", "J", "P", 1.0),
            ("U2", "J", "P", 3.0),
        ])
        plan = build_merge_plan(current, incoming, {})
        statuses = [r["status"] for r in plan["rows"]]
        self.assertEqual(
            statuses,
            [STATUS_CHANGED, STATUS_CHANGED,
             STATUS_ADDED, STATUS_ADDED,
             STATUS_REMOVED,
             STATUS_UNCHANGED, STATUS_UNCHANGED],
        )
        # Within buckets: current order for existing rows...
        changed_ids = [r["score_id"] for r in plan["rows"] if r["status"] == STATUS_CHANGED]
        self.assertEqual(changed_ids, [2, 4])
        # ...and incoming file order for added rows.
        added_names = [r["contestant"] for r in plan["rows"] if r["status"] == STATUS_ADDED]
        self.assertEqual(added_names, ["A1", "A2"])

    def test_decisions_clear_keep_none_actions(self):
        current = cur([
            ("C1", "JA", "P1", 8.0),   # changed + had decision -> clear
            ("C2", "JA", "P1", 7.0),   # removed + had decision -> clear
            ("C3", "JA", "P1", 6.0),   # changed, no decision  -> none
            ("C4", "JA", "P1", 5.0),   # unchanged + decision -> keep
            ("C5", "JA", "P1", 4.0),   # unchanged, no decision -> none
        ])
        decisions = {
            1: (True, "人工保留"),
            2: (False, "校准理由"),
            4: (True, ""),
        }
        incoming = inc([
            ("C1", "JA", "P1", 8.8),
            ("C3", "JA", "P1", 6.6),
            ("C4", "JA", "P1", 5.0),
            ("C5", "JA", "P1", 4.0),
        ])
        plan = build_merge_plan(current, incoming, decisions)
        self.assertEqual(plan["decisions_cleared"], 2)
        rows = by_key(plan)
        self.assertEqual(rows[("C1", "JA", "P1")]["decision_action"], DECISION_CLEAR)
        self.assertTrue(rows[("C1", "JA", "P1")]["has_decision"])
        self.assertTrue(rows[("C1", "JA", "P1")]["old_use_raw"])
        self.assertEqual(rows[("C1", "JA", "P1")]["old_reason"], "人工保留")
        self.assertEqual(rows[("C2", "JA", "P1")]["decision_action"], DECISION_CLEAR)
        self.assertEqual(rows[("C3", "JA", "P1")]["decision_action"], DECISION_NONE)
        self.assertEqual(rows[("C4", "JA", "P1")]["decision_action"], DECISION_KEEP)
        keep = rows[("C4", "JA", "P1")]
        self.assertTrue(keep["has_decision"])
        self.assertEqual(rows[("C5", "JA", "P1")]["decision_action"], DECISION_NONE)
        # Added rows never carry an old decision.
        extra = inc([("C9", "JA", "P1", 1.0)])
        plan2 = build_merge_plan(current, incoming + extra, decisions)
        added = next(r for r in plan2["rows"] if r["status"] == STATUS_ADDED)
        self.assertEqual(added["decision_action"], DECISION_NONE)
        self.assertFalse(added["has_decision"])

    def test_identical_files_all_unchanged(self):
        data = cur([("A", "J", "P", 5.0), ("B", "J", "P", 6.0)])
        incoming = inc([("A", "J", "P", 5.0), ("B", "J", "P", 6.0)])
        plan = build_merge_plan(data, incoming, {})
        self.assertEqual(plan["counts"][STATUS_UNCHANGED], 2)
        self.assertEqual(plan["will_modify"], 0)
        self.assertEqual(plan["decisions_cleared"], 0)

    def test_empty_current_all_added_and_empty_incoming_all_removed(self):
        data = cur([("A", "J", "P", 5.0)])
        all_added = build_merge_plan([], inc([("A", "J", "P", 5.0)]), {})
        self.assertEqual(all_added["counts"][STATUS_ADDED], 1)
        all_removed = build_merge_plan(data, [], {})
        self.assertEqual(all_removed["counts"][STATUS_REMOVED], 1)


class TestBasis(unittest.TestCase):
    def setUp(self):
        self.current = cur([("A", "J", "P", 5.0), ("B", "J", "P", 6.0)])

    def test_roundtrip_matches(self):
        basis = current_basis(self.current)
        self.assertTrue(basis_matches(basis, self.current))
        # Order matters: shuffled basis does not match.
        self.assertFalse(basis_matches(list(reversed(basis)), self.current))

    def test_score_change_detected(self):
        basis = current_basis(self.current)
        basis[1]["score"] = 6.5
        self.assertFalse(basis_matches(basis, self.current))

    def test_length_and_key_mismatch(self):
        basis = current_basis(self.current)
        self.assertFalse(basis_matches(basis + [{"contestant": "C", "judge": "J", "item": "P", "score": 1}], self.current))
        bad = [dict(b) for b in basis]
        bad[0]["judge"] = "JX"
        self.assertFalse(basis_matches(bad, self.current))
        # Malformed basis entries do not raise but never match.
        self.assertFalse(basis_matches([{"contestant": "A"}], self.current))
        self.assertFalse(basis_matches(["nope"], self.current))


class TestBasisDecisionSnapshots(unittest.TestCase):
    """受影响评分（分数变更/将删除）的裁决状态进入基线并参与核对。"""

    def setUp(self):
        self.current = cur([("A", "J", "P", 5.0), ("B", "J", "P", 6.0)])
        self.decisions = {1: (True, "保留理由")}
        self.affected = {1}
        self.snapshots = {1: {"use_raw": True, "reason": "保留理由"}}
        self.basis = current_basis(self.current, self.snapshots)

    def test_basis_carries_snapshot_only_for_affected_rows(self):
        self.assertEqual(
            self.basis[0]["decision"], {"use_raw": True, "reason": "保留理由"}
        )
        self.assertNotIn("decision", self.basis[1])
        # 受影响但无裁决：显式记录 None（存在性也要核对）。
        none_basis = current_basis(self.current, {1: None})
        self.assertIn("decision", none_basis[0])
        self.assertIsNone(none_basis[0]["decision"])

    def test_roundtrip_matches(self):
        self.assertTrue(
            basis_matches(self.basis, self.current, self.decisions, self.affected)
        )

    def test_decision_added_after_preview_conflicts(self):
        basis = current_basis(self.current, {1: None})
        self.assertTrue(basis_matches(basis, self.current, {}, self.affected))
        # 预览后新增裁决 → 不匹配。
        self.assertFalse(
            basis_matches(basis, self.current, self.decisions, self.affected)
        )

    def test_decision_modified_or_removed_conflicts(self):
        self.assertFalse(
            basis_matches(
                self.basis, self.current, {1: (False, "保留理由")}, self.affected
            )
        )
        self.assertFalse(
            basis_matches(
                self.basis, self.current, {1: (True, "改了")}, self.affected
            )
        )
        self.assertFalse(basis_matches(self.basis, self.current, {}, self.affected))

    def test_unaffected_rows_decision_not_checked(self):
        # 未受影响行（B）的裁决变化不影响核对结果。
        self.assertTrue(
            basis_matches(
                self.basis,
                self.current,
                {1: (True, "保留理由"), 2: (False, "随便改")},
                self.affected,
            )
        )

    def test_marker_mismatch_conflicts(self):
        # 受影响行缺少 decision 快照。
        bare = [dict(b) for b in self.basis]
        del bare[0]["decision"]
        self.assertFalse(basis_matches(bare, self.current, self.decisions, self.affected))
        # 未受影响行多带 decision 快照。
        extra = [dict(b) for b in self.basis]
        extra[1]["decision"] = None
        self.assertFalse(
            basis_matches(extra, self.current, self.decisions, self.affected)
        )

    def test_malformed_snapshot_never_matches(self):
        bad = [dict(b) for b in self.basis]
        bad[0]["decision"] = {"use_raw": "yes", "reason": 1}
        self.assertFalse(basis_matches(bad, self.current, self.decisions, self.affected))

    def test_without_decision_args_keeps_score_only_behavior(self):
        self.assertTrue(basis_matches(self.basis, self.current))
        self.assertTrue(basis_matches(self.basis, self.current, self.decisions))
        self.assertTrue(basis_matches(self.basis, self.current, None, self.affected))


class TestValidBasisShape(unittest.TestCase):
    def test_accepts_score_only_and_snapshot_entries(self):
        self.assertTrue(valid_basis_shape([]))
        self.assertTrue(
            valid_basis_shape(
                [
                    {"contestant": "A", "judge": "J", "item": "P", "score": 5.0},
                    {
                        "contestant": "B",
                        "judge": "J",
                        "item": "P",
                        "score": 6,
                        "decision": {"use_raw": True, "reason": "r"},
                    },
                    {
                        "contestant": "C",
                        "judge": "J",
                        "item": "P",
                        "score": 7.0,
                        "decision": None,
                    },
                ]
            )
        )

    def test_rejects_malformed_entries(self):
        self.assertFalse(valid_basis_shape("nope"))
        self.assertFalse(valid_basis_shape([{"contestant": "A"}]))
        self.assertFalse(
            valid_basis_shape(
                [{"contestant": "A", "judge": "J", "item": "P", "score": True}]
            )
        )
        for bad_decision in ({"use_raw": "yes", "reason": "r"}, {"use_raw": True}, "x"):
            self.assertFalse(
                valid_basis_shape(
                    [
                        {
                            "contestant": "A",
                            "judge": "J",
                            "item": "P",
                            "score": 1.0,
                            "decision": bad_decision,
                        }
                    ]
                )
            )


class TestNormalizeIncomingRecords(unittest.TestCase):
    def test_accepts_valid_records(self):
        out = normalize_incoming_records([
            {"contestant": " A ", "judge": "J", "item": "P", "score": "8.5"},
            {"contestant": "B", "judge": "J", "item": "P", "score": 9},
        ])
        self.assertEqual(out[0]["contestant"], "A")
        self.assertEqual(out[0]["score"], 8.5)
        self.assertEqual(out[1]["score"], 9.0)

    def test_rejects_bad_shapes(self):
        with self.assertRaises(MergeValidationError):
            normalize_incoming_records("nope")
        with self.assertRaises(MergeValidationError):
            normalize_incoming_records(["nope"])
        with self.assertRaises(MergeValidationError):
            normalize_incoming_records([])

    def test_rejects_missing_fields_and_bad_numbers(self):
        cases = [
            [{"judge": "J", "item": "P", "score": 1}],          # missing contestant
            [{"contestant": "A", "judge": " ", "item": "P", "score": 1}],  # blank judge
            [{"contestant": "A", "judge": "J", "item": "P", "score": ""}],
            [{"contestant": "A", "judge": "J", "item": "P", "score": "好"}],
            [{"contestant": "A", "judge": "J", "item": "P", "score": float("inf")}],
            [{"contestant": "A", "judge": "J", "item": "P"}],  # score missing
        ]
        for payload in cases:
            with self.assertRaises(MergeValidationError):
                normalize_incoming_records(payload)

    def test_rejects_boolean_score_and_duplicate_triple(self):
        with self.assertRaises(MergeValidationError):
            normalize_incoming_records([{"contestant": "A", "judge": "J", "item": "P", "score": True}])
        with self.assertRaises(MergeValidationError) as ctx:
            normalize_incoming_records([
                {"contestant": "A", "judge": "J", "item": "P", "score": 1},
                {"contestant": "A", "judge": "J", "item": "P", "score": 2},
            ])
        self.assertIn("重复", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
