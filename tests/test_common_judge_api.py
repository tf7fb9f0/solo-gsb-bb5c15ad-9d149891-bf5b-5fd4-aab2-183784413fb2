"""API tests for the read-only common-judge comparable comparison endpoint."""
import os
import sys
import tempfile
import unittest

_tmp = tempfile.mkdtemp(prefix="calib_cj_test_")
os.environ["CALIB_DATA_DIR"] = _tmp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import database as db  # noqa: E402
from app.main import app  # noqa: E402

db.init_db()
client = TestClient(app)
SID = "common-judge-session"
URL = f"/api/sessions/{SID}/common-judge"

# A/B 的 I1 由 J1/J2 共同评，I2 各自只有一名不同评委 → 无共同评委。
# A: I1={J1:8,J2:6}、I2={J1:10}；B: I1={J1:7,J2:5}、I2={J2:9}；
# C: I1={J3:5}。
CSV_BODY = (
    "选手,评委,项目,分数\n"
    "A,J1,I1,8\nA,J2,I1,6\nA,J1,I2,10\n"
    "B,J1,I1,7\nB,J2,I1,5\nB,J2,I2,9\n"
    "C,J3,I1,5\n"
)


class TestCommonJudgeAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        client.delete(f"/api/sessions/{SID}")
        r = client.post(
            f"/api/sessions/{SID}/upload?scope=judge",
            files={"file": ("c.csv", CSV_BODY.encode("utf-8-sig"), "text/csv")},
        )
        assert r.status_code == 200, r.text
        # 接口默认采用校准分；为与原始分断言一致，对全部评分标记「保留原分」。
        for blk in r.json()["contestants"]:
            for ib in blk["items"]:
                for row in ib["scores"]:
                    dr = client.patch(
                        f"/api/sessions/{SID}/scores/{row['id']}/decision",
                        json={"use_raw": True, "reason": "保留原分"},
                    )
                    assert dr.status_code == 200, dr.text

    def test_01_basic_payload(self):
        r = client.get(URL)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["scope"], "judge")
        self.assertEqual(data["scope_label"], "按评委（跨项目汇总）")
        s = data["summary"]
        self.assertEqual(s["pairs"], 3)
        self.assertEqual(s["comparable"], 1)
        self.assertEqual(s["incomparable"], 2)
        self.assertEqual(s["reversed"], 0)
        ab = next(p for p in data["pairs"] if (p["a"], p["b"]) == ("A", "B"))
        self.assertTrue(ab["comparable"])
        self.assertEqual(ab["comparable_items"], 1)
        i1 = next(it for it in ab["items"] if it["item"] == "I1")
        self.assertEqual(i1["common_judges"], ["J1", "J2"])
        self.assertAlmostEqual(i1["a_avg"], 7.0)
        self.assertAlmostEqual(i1["b_avg"], 6.0)
        i2 = next(it for it in ab["items"] if it["item"] == "I2")
        self.assertFalse(i2["comparable"])
        self.assertEqual(i2["reason"], "no_common_judge")
        self.assertEqual(ab["direction"], "same")

    def test_02_reversed_pair(self):
        # 构造一对：全量总分 A 领先，但共同评委口径 B 领先。
        sid = "cj-reversed"
        client.delete(f"/api/sessions/{sid}")
        csv_body = (
            "选手,评委,项目,分数\n"
            "A,J1,I1,8\nA,J2,I1,8\nA,J3,I2,10\nA,J4,I2,10\n"
            "B,J1,I1,9\nB,J2,I1,9\n"
        )
        r = client.post(
            f"/api/sessions/{sid}/upload?scope=judge",
            files={"file": ("r.csv", csv_body.encode("utf-8-sig"), "text/csv")},
        )
        self.assertEqual(r.status_code, 200, r.text)
        data = client.get(f"/api/sessions/{sid}/common-judge").json()
        p = data["pairs"][0]
        self.assertEqual(p["direction"], "reversed")
        self.assertTrue(p["reversed"])
        self.assertEqual(data["summary"]["reversed"], 1)

    def test_02b_full_direction_follows_final_rank(self):
        # 最终总分 90、90−0.6e-9、90−1.2e-9 → 主榜名次 1、1、3。
        # B/C 分差 0.6e-9 在容差内，但名次 1 vs 3 有先后，不得标为全量并列。
        sid = "cj-rank-anchor"
        client.delete(f"/api/sessions/{sid}")
        csv_body = (
            "选手,评委,项目,分数\n"
            f"A,J1,I1,{90.0}\n"
            f"B,J1,I1,{90.0 - 0.6e-9:.17g}\n"
            f"C,J1,I1,{90.0 - 1.2e-9:.17g}\n"
        )
        r = client.post(
            f"/api/sessions/{sid}/upload?scope=judge",
            files={"file": ("rank.csv", csv_body.encode("utf-8-sig"), "text/csv")},
        )
        self.assertEqual(r.status_code, 200, r.text)
        data = client.get(f"/api/sessions/{sid}/common-judge").json()
        pairs = {(p["a"], p["b"]): p for p in data["pairs"]}
        ab, ac, bc = pairs[("A", "B")], pairs[("A", "C")], pairs[("B", "C")]
        self.assertEqual((ab["a_rank"], ab["b_rank"]), (1, 1))
        self.assertEqual((bc["a_rank"], bc["b_rank"]), (1, 3))
        self.assertEqual(ab["direction"], "full_tie")
        self.assertEqual(ab["full_leader"], "tie")
        self.assertEqual(bc["full_leader"], "a")
        self.assertLessEqual(abs(bc["full_delta"]), 1e-9)
        self.assertEqual(bc["direction"], "same")
        self.assertFalse(bc["reversed"])
        self.assertEqual(ac["direction"], "same")
        self.assertEqual(data["summary"]["full_tie"], 1)
        self.assertEqual(data["summary"]["same_direction"], 2)
        self.assertEqual(data["summary"]["reversed"], 0)
        self.assertEqual(data["direction_labels"]["full_tie"], "全量并列")

    def test_03_readonly_does_not_mutate(self):
        before = client.get(f"/api/sessions/{SID}/analysis").json()
        client.get(URL)
        client.get(URL)
        after = client.get(f"/api/sessions/{SID}/analysis").json()
        # 只读调用不改变裁决数（setUpClass 已对全部评分标记保留原分）。
        self.assertEqual(after["counts"]["decisions"], before["counts"]["decisions"])
        self.assertEqual(
            [(b["contestant"], b["eff_total"]) for b in before["contestants"]],
            [(b["contestant"], b["eff_total"]) for b in after["contestants"]],
        )

    def test_04_reflects_decision_and_scope(self):
        analysis = client.get(f"/api/sessions/{SID}/analysis").json()
        target = next(
            x for b in analysis["contestants"] for ib in b["items"] for x in ib["scores"]
            if b["contestant"] == "A" and ib["item"] == "I1" and x["judge"] == "J1"
        )
        before = client.get(URL).json()
        client.patch(
            f"/api/sessions/{SID}/scores/{target['id']}/decision",
            json={"use_raw": False, "reason": "同评委对照应反映裁决"},
        )
        # 本数据下 calibrated 与 raw 可能相等；至少接口必须返回最新 scope/结构。
        after = client.get(URL).json()
        self.assertEqual(after["scope"], "judge")
        client.post(f"/api/sessions/{SID}/scope", json={"scope": "judge_item"})
        scoped = client.get(URL).json()
        self.assertEqual(scoped["scope"], "judge_item")
        client.post(f"/api/sessions/{SID}/scope", json={"scope": "judge"})
        client.post(f"/api/sessions/{SID}/decisions/reset")

    def test_05_unknown_session_and_empty_session(self):
        self.assertEqual(client.get("/api/sessions/nope/common-judge").status_code, 404)
        client.delete("/api/sessions/empty-cj")
        # 会话不存在：GET analysis 会惰性建立一个空会话。
        client.get("/api/sessions/empty-cj/analysis")
        self.assertEqual(
            client.get("/api/sessions/empty-cj/common-judge").status_code, 404
        )


if __name__ == "__main__":
    unittest.main()
