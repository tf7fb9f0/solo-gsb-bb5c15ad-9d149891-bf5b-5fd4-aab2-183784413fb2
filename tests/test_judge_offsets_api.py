"""API tests for the judge offset diagnosis endpoint and analysis integration."""
import os
import sys
import tempfile
import unittest

_tmp = tempfile.mkdtemp(prefix="calib_offsets_test_")
os.environ["CALIB_DATA_DIR"] = _tmp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import database as db  # noqa: E402
from app.main import app  # noqa: E402

db.init_db()
client = TestClient(app)
SID = "offsets-session"

HEADER = "选手,评委,项目,分数\n"
# 与 tests/test_judge_offsets.py 的三角形数据一致：
# JA-JB 中位分差 2（权重 3）、JA-JC 中位分差 1.5（权重 2）、JB-JC 中位分差 0（权重 3）。
INITIAL = (
    HEADER
    + "C1,JA,P1,9\nC1,JB,P1,7\nC1,JC,P1,8\n"
    + "C2,JA,P1,8\nC2,JB,P1,6\nC2,JC,P1,6\n"
    + "C3,JA,P1,7\nC3,JB,P1,5\n"
    + "C4,JB,P1,4\nC4,JC,P1,4\n"
)
# 合并后新增评委 JD（与 JA 在 P2 共评两名选手），JA/JB/JC 保持不动。
MERGED = INITIAL + "C1,JA,P2,8\nC1,JD,P2,6\nC2,JA,P2,7\nC2,JD,P2,4\n"


def upload(csv_text, sid=SID):
    return client.post(
        f"/api/sessions/{sid}/upload?scope=judge",
        files={"file": ("s.csv", csv_text.encode("utf-8-sig"), "text/csv")},
    )


class TestJudgeOffsetsAPI(unittest.TestCase):
    def test_01_unknown_session_404(self):
        r = client.get("/api/sessions/offsets-missing/judge-offsets")
        self.assertEqual(r.status_code, 404)
        self.assertIn("会话不存在", r.json()["detail"])

    def test_02_session_without_scores_404(self):
        sid = "offsets-empty"
        r = client.post(f"/api/sessions/{sid}/scope", json={"scope": "judge"})
        self.assertEqual(r.status_code, 200)
        r = client.get(f"/api/sessions/{sid}/judge-offsets")
        self.assertEqual(r.status_code, 404)
        self.assertIn("没有评分数据", r.json()["detail"])

    def test_03_upload_response_contains_diagnosis(self):
        r = upload(INITIAL)
        self.assertEqual(r.status_code, 200, r.text)
        jo = r.json()["judge_offsets"]
        s = jo["summary"]
        self.assertEqual(s["judges"], 3)
        self.assertEqual(s["estimable_judges"], 3)
        self.assertEqual(s["non_estimable_judges"], 0)
        self.assertEqual(s["components"], 1)
        self.assertEqual(s["pairs"], 3)
        self.assertEqual(s["common_records"], 8)
        judges = {j["judge"]: j for j in jo["judges"]}
        self.assertAlmostEqual(judges["JA"]["offset"], 25 / 21)
        self.assertAlmostEqual(judges["JB"]["offset"], -2 / 3)
        self.assertAlmostEqual(judges["JC"]["offset"], -11 / 21)
        self.assertEqual(judges["JA"]["coverage"], 5)
        pairs = {(p["judge_a"], p["judge_b"]): p for p in jo["pairs"]}
        self.assertAlmostEqual(pairs[("JA", "JB")]["observed_diff"], 2.0)
        self.assertAlmostEqual(pairs[("JA", "JB")]["fitted_diff"], 13 / 7)
        self.assertAlmostEqual(pairs[("JA", "JB")]["residual"], 1 / 7)
        self.assertAlmostEqual(s["weighted_residual_ss"], 3 / 14)

    def test_04_endpoint_matches_analysis_and_is_readonly(self):
        analysis = client.get(f"/api/sessions/{SID}/analysis").json()
        r = client.get(f"/api/sessions/{SID}/judge-offsets")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), analysis["judge_offsets"])
        # 只读：再次获取分析，裁决数与评分数不变。
        again = client.get(f"/api/sessions/{SID}/analysis").json()
        self.assertEqual(again["counts"], analysis["counts"])

    def test_05_scope_and_decisions_do_not_change_result(self):
        before = client.get(f"/api/sessions/{SID}/judge-offsets").json()
        r = client.post(f"/api/sessions/{SID}/scope", json={"scope": "judge_item"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["judge_offsets"], before)
        # 单条裁决（保留原分）不改变诊断。
        analysis = client.get(f"/api/sessions/{SID}/analysis").json()
        score_id = analysis["contestants"][0]["items"][0]["scores"][0]["id"]
        r = client.patch(
            f"/api/sessions/{SID}/scores/{score_id}/decision",
            json={"use_raw": True, "reason": "复核"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["judge_offsets"], before)
        self.assertEqual(
            client.get(f"/api/sessions/{SID}/judge-offsets").json(), before
        )
        # 恢复默认范围与裁决，避免影响后续用例。
        client.post(f"/api/sessions/{SID}/scope", json={"scope": "judge"})
        client.post(f"/api/sessions/{SID}/decisions/reset")

    def test_06_merge_confirm_refreshes_diagnosis(self):
        r = client.post(
            f"/api/sessions/{SID}/merge/preview",
            files={"file": ("m.csv", MERGED.encode("utf-8-sig"), "text/csv")},
        )
        self.assertEqual(r.status_code, 200, r.text)
        p = r.json()
        self.assertEqual(p["counts"]["added"], 4)
        r = client.post(
            f"/api/sessions/{SID}/merge/confirm",
            json={"records": p["records"], "basis": p["basis"], "scope": "judge"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        jo = r.json()["judge_offsets"]
        s = jo["summary"]
        self.assertEqual(s["judges"], 4)
        self.assertEqual(s["estimable_judges"], 4)
        # JD 与 JA 共评 → 同一分量；JD 与 JB/JC 无共评 → 不产生新评委对。
        self.assertEqual(s["components"], 1)
        self.assertEqual(s["pairs"], 4)
        judges = {j["judge"]: j for j in jo["judges"]}
        self.assertTrue(judges["JD"]["estimable"])
        self.assertEqual(judges["JD"]["component"], judges["JA"]["component"])
        self.assertEqual(judges["JD"]["coverage"], 2)
        pairs = {(p["judge_a"], p["judge_b"]): p for p in jo["pairs"]}
        ja_jd = pairs[("JA", "JD")]
        self.assertEqual(ja_jd["common_count"], 2)
        self.assertAlmostEqual(ja_jd["observed_diff"], 2.5)  # (8-6, 7-4) → 中位数 2.5
        # 与独立接口结果一致。
        self.assertEqual(client.get(f"/api/sessions/{SID}/judge-offsets").json(), jo)

    def test_07_replace_upload_refreshes_diagnosis(self):
        # 替换导入一份 JD 不在其中的数据 → JD 消失，回到三评委三角形。
        r = upload(INITIAL)
        self.assertEqual(r.status_code, 200, r.text)
        jo = r.json()["judge_offsets"]
        self.assertEqual(jo["summary"]["judges"], 3)
        self.assertEqual([j["judge"] for j in jo["judges"]], ["JA", "JB", "JC"])
        self.assertEqual(
            client.get(f"/api/sessions/{SID}/judge-offsets").json(), jo
        )


if __name__ == "__main__":
    unittest.main()
