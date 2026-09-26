"""API tests for the merge-import flow.

预览把「待合并记录 + 预览基线」作为会话专属快照存入服务端并返回一次性
标识；确认只携带该标识，在同一事务内取回快照、核对基线并原子消耗标识。
"""
import os
import sys
import tempfile
import unittest

_tmp = tempfile.mkdtemp(prefix="calib_merge_test_")
os.environ["CALIB_DATA_DIR"] = _tmp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import database as db  # noqa: E402
from app.main import app  # noqa: E402

db.init_db()
client = TestClient(app)
SID = "merge-session"

HEADER = "选手,评委,项目,分数\n"
INITIAL = (
    HEADER
    + "C1,JA,P1,8.0\n"   # -> changed to 8.5
    + "C1,JB,P1,7.0\n"   # -> unchanged
    + "C2,JA,P1,9.0\n"   # -> removed (only in session)
    + "C2,JB,P1,6.0\n"   # -> changed to 6.5, had no decision
)
MERGED = (
    HEADER
    + "C1,JA,P1,8.5\n"
    + "C1,JB,P1,7.0\n"
    + "C2,JB,P1,6.5\n"
    + "C3,JA,P1,5.0\n"   # -> added
    + "C3,JB,P1,4.0\n"   # -> added
)


def upload(csv_text, sid=SID):
    return client.post(
        f"/api/sessions/{sid}/upload?scope=judge",
        files={"file": ("s.csv", csv_text.encode("utf-8-sig"), "text/csv")},
    )


def preview(csv_text, sid=SID, scope=None):
    url = f"/api/sessions/{sid}/merge/preview"
    if scope is not None:
        url += f"?scope={scope}"
    return client.post(
        url,
        files={"file": ("m.csv", csv_text.encode("utf-8-sig"), "text/csv")},
    )


def confirm(payload, sid=SID):
    return client.post(f"/api/sessions/{sid}/merge/confirm", json=payload)


def confirm_token(token, sid=SID):
    return confirm({"token": token}, sid=sid)


def analysis(sid=SID):
    return client.get(f"/api/sessions/{sid}/analysis").json()


def score_map(data):
    return {
        (b["contestant"], s["judge"], ib["item"]): s
        for b in data["contestants"]
        for ib in b["items"]
        for s in ib["scores"]
    }


def id_map(data):
    return {
        (b["contestant"], s["judge"], ib["item"]): s["id"]
        for b in data["contestants"]
        for ib in b["items"]
        for s in ib["scores"]
    }


def put_decision(score_id, use_raw, reason):
    return client.patch(
        f"/api/sessions/{SID}/scores/{score_id}/decision",
        json={"use_raw": use_raw, "reason": reason},
    )


class TestMergeAPI(unittest.TestCase):
    def test_01_preview_readonly_and_classification(self):
        self.assertEqual(upload(INITIAL).status_code, 200)
        # Put a decision on C1/JA/P1 (changed row) and C1/JB/P1 (unchanged row).
        ids = id_map(analysis())
        put_decision(ids[("C1", "JA", "P1")], True, "人工保留甲")
        put_decision(ids[("C1", "JB", "P1")], True, "校准乙")

        r = preview(MERGED)
        self.assertEqual(r.status_code, 200, r.text)
        p = r.json()
        self.assertEqual(p["counts"], {"changed": 2, "added": 2, "removed": 1, "unchanged": 1})
        self.assertEqual(p["will_modify"], 5)
        self.assertEqual(p["total_current"], 4)
        self.assertEqual(p["total_incoming"], 5)
        # Only C1/JA/P1 loses a decision; C2/JB changed row had none.
        self.assertEqual(p["decisions_cleared"], 1)
        rows = {(x["contestant"], x["judge"], x["item"]): x for x in p["rows"]}
        ch = rows[("C1", "JA", "P1")]
        self.assertEqual(ch["old_score"], 8.0)
        self.assertEqual(ch["new_score"], 8.5)
        self.assertEqual(ch["decision_action"], "clear")
        self.assertTrue(ch["has_decision"])
        self.assertTrue(ch["old_use_raw"])
        self.assertEqual(ch["old_reason"], "人工保留甲")
        un = rows[("C1", "JB", "P1")]
        self.assertEqual(un["status"], "unchanged")
        self.assertEqual(un["decision_action"], "keep")
        rm = rows[("C2", "JA", "P1")]
        self.assertEqual(rm["status"], "removed")
        self.assertIsNone(rm["new_score"])
        self.assertEqual(rm["decision_action"], "none")
        ad = rows[("C3", "JA", "P1")]
        self.assertEqual(ad["status"], "added")
        self.assertIsNone(ad["old_score"])
        # 确认凭一次性标识进行；记录与基线不再回传（由服务端快照保管）。
        self.assertIsInstance(p["token"], str)
        self.assertGreaterEqual(len(p["token"]), 32)
        self.assertNotIn("records", p)
        self.assertNotIn("basis", p)
        # Bucket order: changed -> added -> removed -> unchanged.
        statuses = [x["status"] for x in p["rows"]]
        self.assertEqual(statuses, sorted(statuses, key={
            "changed": 0, "added": 1, "removed": 2, "unchanged": 3,
        }.__getitem__))

        # Preview must not touch scores or decisions.
        again = analysis()
        self.assertEqual(again["counts"]["scores"], 4)
        self.assertEqual(again["counts"]["decisions"], 2)

    def test_02_preview_bad_csv_is_400(self):
        r = client.post(
            f"/api/sessions/{SID}/merge/preview",
            files={"file": ("m.csv", "选手,评委,分数\nA,J,9".encode(), "text/csv")},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("项目", r.json()["detail"])
        # Duplicate triple inside the merge file -> 400.
        dup = HEADER + "A,J,P,1\nA,J,P,2\n"
        self.assertEqual(preview(dup).status_code, 400)

    def test_03_confirm_stale_basis_conflicts(self):
        self.assertEqual(upload(INITIAL).status_code, 200)
        token = preview(MERGED).json()["token"]
        # 预览后当前会话数据被其它操作改动 → 基线不再匹配。
        self.assertEqual(upload(HEADER + "C1,JA,P1,8.1\n").status_code, 200)
        r = confirm_token(token)
        self.assertEqual(r.status_code, 409)
        self.assertIn("重新预览", r.json()["detail"])
        # Nothing was applied.
        self.assertEqual(analysis()["counts"]["scores"], 1)

    def test_04_confirm_token_validation(self):
        self.assertEqual(upload(INITIAL).status_code, 200)
        token = preview(MERGED).json()["token"]
        # Missing/empty token -> pydantic 422.
        self.assertEqual(confirm({}).status_code, 422)
        self.assertEqual(confirm({"token": ""}).status_code, 422)
        # Unknown token -> 404, 提示重新预览。
        r = confirm_token("伪造的标识")
        self.assertEqual(r.status_code, 404)
        self.assertIn("重新预览", r.json()["detail"])
        # 其它会话的标识不可用（标识是会话专属的）。
        r = confirm_token(token, sid="other-session")
        self.assertEqual(r.status_code, 404)
        # 以上失败都不写数据，也不消耗合法标识。
        self.assertEqual(analysis()["counts"]["scores"], 4)
        r = confirm_token(token)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["counts"]["scores"], 5)

    def test_05_confirm_ignores_client_supplied_records(self):
        """回归：确认请求里篡改的记录/基线被忽略，只应用预览快照。"""
        self.assertEqual(upload(INITIAL).status_code, 200)
        token = preview(MERGED).json()["token"]
        # 攻击场景：预览显示 8.0→8.5，确认时把回传记录改成 99。
        r = confirm({
            "token": token,
            "records": [{"contestant": "C1", "judge": "JA", "item": "P1", "score": 99}],
            "basis": [],
            "scope": "judge_item",
        })
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        m = score_map(data)
        # 写入的是预览时的 8.5 而不是未预览的 99；其余差异也来自预览快照。
        self.assertEqual(m[("C1", "JA", "P1")]["raw"], 8.5)
        self.assertEqual(data["counts"]["scores"], 5)
        self.assertEqual(m[("C2", "JB", "P1")]["raw"], 6.5)
        self.assertIn(("C3", "JA", "P1"), m)
        # 会话已存在，校准范围保持原样（忽略请求里的 scope）。
        self.assertEqual(data["scope"], "judge")

    def test_06_token_is_single_use(self):
        self.assertEqual(upload(INITIAL).status_code, 200)
        token = preview(MERGED).json()["token"]
        r = confirm_token(token)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["counts"]["scores"], 5)
        # 同一标识再次确认 → 409，且不重复写入。
        r = confirm_token(token)
        self.assertEqual(r.status_code, 409)
        self.assertIn("重新预览", r.json()["detail"])
        self.assertEqual(analysis()["counts"]["scores"], 5)

    def test_07_new_preview_invalidates_previous_token(self):
        self.assertEqual(upload(INITIAL).status_code, 200)
        old_token = preview(MERGED).json()["token"]
        new_token = preview(MERGED).json()["token"]
        self.assertNotEqual(old_token, new_token)
        # 同一会话只保留最新预览：旧标识作废。
        r = confirm_token(old_token)
        self.assertEqual(r.status_code, 404)
        self.assertIn("重新预览", r.json()["detail"])
        self.assertEqual(analysis()["counts"]["scores"], 4)
        # 新标识仍可确认。
        r = confirm_token(new_token)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["counts"]["scores"], 5)

    def test_08_confirm_applies_in_transaction(self):
        self.assertEqual(upload(INITIAL).status_code, 200)
        ids = id_map(analysis())
        put_decision(ids[("C1", "JA", "P1")], True, "人工保留甲")
        put_decision(ids[("C1", "JB", "P1")], True, "校准乙")
        token = preview(MERGED).json()["token"]
        r = confirm_token(token)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        applied = data["merge_applied"]
        self.assertEqual(applied["counts"],
                         {"changed": 2, "added": 2, "removed": 1, "unchanged": 1})
        self.assertEqual(applied["decisions_cleared"], 1)
        self.assertEqual(applied["rules_touched"], 0)
        # Refreshed analysis returned in the same response.
        self.assertEqual(data["counts"]["scores"], 5)
        # C1/JA/P1 decision cleared; C1/JB/P1 unchanged decision kept.
        self.assertEqual(data["counts"]["decisions"], 1)

        m = score_map(data)
        self.assertEqual(m[("C1", "JA", "P1")]["raw"], 8.5)
        self.assertFalse(m[("C1", "JA", "P1")]["use_raw"])
        self.assertEqual(m[("C1", "JA", "P1")]["reason"], "")
        self.assertEqual(m[("C2", "JB", "P1")]["raw"], 6.5)
        self.assertNotIn(("C2", "JA", "P1"), m)  # removed row is gone
        self.assertIn(("C3", "JA", "P1"), m)
        self.assertIn(("C3", "JB", "P1"), m)
        keep = m[("C1", "JB", "P1")]
        self.assertTrue(keep["use_raw"])
        self.assertEqual(keep["reason"], "校准乙")
        self.assertEqual(keep["raw"], 7.0)

    def test_09_unchanged_rows_keep_ids_and_remerge_is_all_unchanged(self):
        # Re-preview the just-merged state with the same file: nothing to do.
        p = preview(MERGED).json()
        self.assertEqual(p["counts"], {"changed": 0, "added": 0, "removed": 0, "unchanged": 5})
        self.assertEqual(p["will_modify"], 0)
        self.assertEqual(p["decisions_cleared"], 0)
        # Confirming a no-op merge still succeeds and changes nothing.
        r = confirm_token(p["token"])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["counts"]["scores"], 5)
        self.assertEqual(r.json()["counts"]["decisions"], 1)

    def test_10_batch_rules_are_not_touched_by_merge(self):
        judge = analysis()["judges"][0]
        client.put(f"/api/sessions/{SID}/rules", json={"rules": [
            {"name": "合并后规则保留", "judge": judge, "use_raw": True, "priority": 5},
        ]})
        next_csv = HEADER + "C1,JA,P1,9.9\nC1,JB,P1,7.0\nC2,JB,P1,6.5\nC3,JA,P1,5.0\nC3,JB,P1,4.0\n"
        p = preview(next_csv).json()
        self.assertEqual(p["counts"]["changed"], 1)
        r = confirm_token(p["token"])
        self.assertEqual(r.status_code, 200, r.text)
        rules = client.get(f"/api/sessions/{SID}/rules").json()["rules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["name"], "合并后规则保留")
        self.assertEqual(rules[0]["judge"], judge)

    def test_11_preview_on_brand_new_session_is_all_added_and_creates_nothing(self):
        fresh = "brand-new-merge-session"
        p = preview(MERGED, sid=fresh, scope="judge_item").json()
        self.assertFalse(p["session_existed"])
        self.assertEqual(p["scope"], "judge_item")
        self.assertEqual(p["counts"]["added"], 5)
        self.assertEqual(p["total_current"], 0)
        # Preview must not create the session.
        self.assertEqual(analysis(fresh)["counts"]["scores"], 0)
        # 确认只带标识；会话按预览时选定的校准范围创建。
        r = confirm_token(p["token"], sid=fresh)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["counts"]["scores"], 5)
        self.assertEqual(data["scope"], "judge_item")

    def test_12_cancel_is_nothing(self):
        # Issuing a preview and walking away (no confirm) leaves data intact.
        before = analysis()
        p = preview(HEADER + "C1,JA,P1,1.0\n").json()
        self.assertEqual(p["will_modify"], 5)
        after = analysis()
        self.assertEqual(before["counts"], after["counts"])
        self.assertEqual(score_map(after)[("C1", "JA", "P1")]["raw"], 9.9)

    def test_13_confirm_rolls_back_on_failure(self):
        before = analysis()
        token = preview(HEADER + "C1,JA,P1,2.2\n").json()["token"]

        original_merge = db.merge_scores

        def boom(conn, session_id, rows):
            # Simulate a failure after the UPDATE/DELETE statements would run:
            # the surrounding transaction must roll every change back.
            conn.execute("UPDATE scores SET score = -999 WHERE session_id = ?", (session_id,))
            raise RuntimeError("simulated mid-transaction failure")

        db.merge_scores = boom
        quiet_client = TestClient(app, raise_server_exceptions=False)
        try:
            r = quiet_client.post(
                f"/api/sessions/{SID}/merge/confirm",
                json={"token": token},
            )
        finally:
            db.merge_scores = original_merge
        self.assertEqual(r.status_code, 500)

        after = analysis()
        self.assertEqual(after["counts"], before["counts"])
        m = score_map(after)
        self.assertEqual(m[("C1", "JA", "P1")]["raw"], 9.9)  # no phantom -999
        self.assertEqual(len(m), before["counts"]["scores"])  # no added/removed leaked
        # 事务回滚连同标识消耗一并撤销：同一标识可重试并成功。
        r = confirm_token(token)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(score_map(r.json())[("C1", "JA", "P1")]["raw"], 2.2)

    # --- 预览基线同时核对受影响评分的裁决状态 ---

    def test_14_decision_added_after_preview_conflicts(self):
        self.assertEqual(upload(INITIAL).status_code, 200)
        # C2/JB/P1 是分数变更行且本无裁决；预览后给它新增裁决。
        token = preview(MERGED).json()["token"]
        ids = id_map(analysis())
        put_decision(ids[("C2", "JB", "P1")], True, "预览后新增")

        r = confirm_token(token)
        self.assertEqual(r.status_code, 409)
        self.assertIn("重新预览", r.json()["detail"])
        # 未写入任何合并结果：分数与裁决原样保留。
        after = analysis()
        self.assertEqual(after["counts"]["scores"], 4)
        self.assertEqual(after["counts"]["decisions"], 1)
        m = score_map(after)
        self.assertEqual(m[("C2", "JB", "P1")]["raw"], 6.0)
        self.assertTrue(m[("C2", "JB", "P1")]["use_raw"])
        self.assertEqual(m[("C2", "JB", "P1")]["reason"], "预览后新增")

    def test_15_decision_modified_or_removed_after_preview_conflicts(self):
        self.assertEqual(upload(INITIAL).status_code, 200)
        ids = id_map(analysis())
        # C1/JA/P1 是分数变更行，预览前已有裁决。
        put_decision(ids[("C1", "JA", "P1")], True, "原始裁决")
        token = preview(MERGED).json()["token"]

        # 预览后修改该裁决 → 409，且裁决保持修改后的样子（未被合并清除）。
        put_decision(ids[("C1", "JA", "P1")], False, "预览后修改")
        r = confirm_token(token)
        self.assertEqual(r.status_code, 409)
        after = analysis()
        self.assertEqual(after["counts"]["scores"], 4)
        m = score_map(after)
        self.assertFalse(m[("C1", "JA", "P1")]["use_raw"])
        self.assertEqual(m[("C1", "JA", "P1")]["reason"], "预览后修改")

        # 预览后删除该裁决 → 同样 409。
        token2 = preview(MERGED).json()["token"]
        client.post(f"/api/sessions/{SID}/decisions/reset")
        r = confirm_token(token2)
        self.assertEqual(r.status_code, 409)
        after = analysis()
        self.assertEqual(after["counts"]["scores"], 4)
        self.assertEqual(after["counts"]["decisions"], 0)

    def test_16_unchanged_row_decision_change_does_not_block(self):
        self.assertEqual(upload(INITIAL).status_code, 200)
        ids = id_map(analysis())
        # 变更行 C1/JA/P1 与未变行 C1/JB/P1 各置一条裁决。
        put_decision(ids[("C1", "JA", "P1")], True, "将被清除")
        put_decision(ids[("C1", "JB", "P1")], True, "未变行原裁决")
        p = preview(MERGED).json()
        self.assertEqual(p["decisions_cleared"], 1)

        # 预览后只改未变行的裁决 → 不阻塞合并。
        put_decision(ids[("C1", "JB", "P1")], False, "预览后改过的未变行裁决")
        r = confirm_token(p["token"])
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["merge_applied"]["decisions_cleared"], 1)
        # counts["decisions"] 只统计「保留原分」的裁决；唯一存活的未变行裁决
        # 已被改成 use_raw=False，故计数为 0。
        self.assertEqual(data["counts"]["decisions"], 0)
        # 未变行的裁决按预览后的新状态原样保留（理由仍在即裁决行未被删）。
        keep = score_map(data)[("C1", "JB", "P1")]
        self.assertFalse(keep["use_raw"])
        self.assertEqual(keep["reason"], "预览后改过的未变行裁决")

    def test_17_cleared_session_forgets_pending_tokens(self):
        self.assertEqual(upload(INITIAL).status_code, 200)
        token = preview(MERGED).json()["token"]
        client.delete(f"/api/sessions/{SID}")
        # 会话清空后，其待合并标识一并失效。
        r = confirm_token(token)
        self.assertEqual(r.status_code, 404)
        self.assertIn("重新预览", r.json()["detail"])
        self.assertEqual(analysis()["counts"]["scores"], 0)


if __name__ == "__main__":
    unittest.main()
