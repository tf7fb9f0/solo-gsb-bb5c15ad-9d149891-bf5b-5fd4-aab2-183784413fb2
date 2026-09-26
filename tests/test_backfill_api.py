"""补评分派只读预览接口的 API 测试。"""
import os
import sys
import tempfile
import unittest

_tmp = tempfile.mkdtemp(prefix="calib_backfill_test_")
os.environ["CALIB_DATA_DIR"] = _tmp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import database as db  # noqa: E402
from app.main import app  # noqa: E402

db.init_db()
client = TestClient(app)
SID = "backfill-session"

HEADER = "选手,评委,项目,分数\n"
# P1：A 有 JA/JB/JC，B 只有 JA；P2：A 只有 JA。
CSV_BODY = (
    HEADER
    + "A,JA,P1,8.0\nA,JB,P1,9.0\nA,JC,P1,7.0\n"
    + "B,JA,P1,8.0\n"
    + "A,JA,P2,8.0\n"
)


def upload(body=CSV_BODY):
    return client.post(
        f"/api/sessions/{SID}/upload?scope=judge",
        files={"file": ("s.csv", body.encode("utf-8-sig"), "text/csv")},
    )


def preview(payload):
    return client.post(f"/api/sessions/{SID}/backfill/preview", json=payload)


class TestBackfillAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        r = upload()
        assert r.status_code == 200, r.text

    def test_01_plan_structure_and_order(self):
        # target 3：(A,P1) 已满；(B,P1) 缺 2，合格 JB/JC；(A,P2) 缺 2，无合格评委。
        r = preview({"target": 3, "capacities": {"JB": 2, "JC": 1}})
        self.assertEqual(r.status_code, 200, r.text)
        p = r.json()
        self.assertEqual(p["scope"], "judge")
        self.assertIn("scope_label", p)
        # 单元固定按 项目→选手 排序。
        self.assertEqual(
            [(u["item"], u["contestant"]) for u in p["units"]],
            [("P1", "A"), ("P1", "B"), ("P2", "A")],
        )
        # 分派固定按 评委→项目→选手 排序：JB 在 JC 之前。
        self.assertEqual(
            p["assignments"],
            [
                {"judge": "JB", "contestant": "B", "item": "P1"},
                {"judge": "JC", "contestant": "B", "item": "P1"},
            ],
        )
        s = p["summary"]
        self.assertEqual(s["units"], 3)
        self.assertEqual(s["gap_units"], 2)
        self.assertEqual(s["total_gap"], 4)
        self.assertEqual(s["assigned_total"], 2)
        self.assertEqual(s["unmet_total"], 2)
        self.assertEqual(s["optimal_total"], 2)
        # 评委容量：缺省的 JA 按 0 回显；JB 已派 1、剩 1。
        judges = {j["judge"]: j for j in p["judges"]}
        self.assertEqual(judges["JA"]["capacity"], 0)
        self.assertEqual(judges["JB"]["assigned"], 1)
        self.assertEqual(judges["JB"]["remaining"], 1)
        # 单元明细的合格评委列表。
        b1 = next(u for u in p["units"] if u["item"] == "P1" and u["contestant"] == "B")
        self.assertEqual(b1["eligible_judges"], ["JB", "JC"])
        a2 = next(u for u in p["units"] if u["item"] == "P2" and u["contestant"] == "A")
        self.assertEqual(a2["eligible_judges"], [])
        self.assertEqual(a2["unmet"], 2)

    def test_02_capacity_binds(self):
        # 只给 JB 1 个名额：(B,P1) 只能补 1 个，另一缺口与 (A,P2) 两缺口共 3 个未满足。
        r = preview({"target": 3, "capacities": {"JB": 1}})
        self.assertEqual(r.status_code, 200, r.text)
        p = r.json()
        self.assertEqual(p["assignments"], [{"judge": "JB", "contestant": "B", "item": "P1"}])
        self.assertEqual(p["summary"]["assigned_total"], 1)
        self.assertEqual(p["summary"]["unmet_total"], 3)
        self.assertEqual(p["summary"]["capacity_total"], 1)

    def test_03_missing_capacity_defaults_zero(self):
        r = preview({"target": 3})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["summary"]["assigned_total"], 0)
        self.assertEqual(r.json()["summary"]["unmet_total"], 4)

    def test_04_readonly_does_not_write(self):
        # 多次预览不得改变评分/裁决。
        before = client.get(f"/api/sessions/{SID}/analysis").json()
        for _ in range(3):
            preview({"target": 3, "capacities": {"JB": 9}})
        after = client.get(f"/api/sessions/{SID}/analysis").json()
        self.assertEqual(after["counts"]["scores"], before["counts"]["scores"])
        self.assertEqual(after["counts"]["decisions"], 0)
        self.assertEqual(after["counts"]["scores"], 5)

    def test_05_unknown_judge_returns_400(self):
        r = preview({"target": 3, "capacities": {"不存在的评委": 1}})
        self.assertEqual(r.status_code, 400)
        self.assertIn("评委", r.json()["detail"])

    def test_06_validation_status_codes(self):
        self.assertEqual(preview({"target": 0}).status_code, 422)
        self.assertEqual(preview({"target": 3, "capacities": {"JB": -1}}).status_code, 400)
        self.assertEqual(preview({"target": 3, "capacities": [1]}).status_code, 422)
        self.assertEqual(preview({"target": "x"}).status_code, 422)

    def test_07_unknown_or_empty_session_404(self):
        self.assertEqual(
            client.post("/api/sessions/no-such-session/backfill/preview", json={"target": 3}).status_code,
            404,
        )
        client.delete(f"/api/sessions/{SID}")
        self.assertEqual(preview({"target": 3}).status_code, 404)

    def test_08_merging_new_scores_refreshes_eligibility(self):
        # 重新上传后合并：给 (B,P1) 追加 JB 的评分 → (B,P1) 已满，无分派。
        upload(
            HEADER
            + "A,JA,P1,8.0\nA,JB,P1,9.0\nA,JC,P1,7.0\n"
            + "B,JA,P1,8.0\nB,JB,P1,8.0\n"
            + "A,JA,P2,8.0\n"
        )
        p = preview({"target": 2, "capacities": {"JC": 5}}).json()
        # target 2：(B,P1) 现有 2 满；(A,P2) 现有 1，但评过 P2 的只有 JA → 无合格评委。
        self.assertEqual(p["summary"]["assigned_total"], 0)
        self.assertEqual(p["summary"]["gap_units"], 1)
        self.assertEqual(p["summary"]["unmet_total"], 1)

    def test_09_sample_data_deterministic(self):
        client.post(f"/api/sessions/{SID}/sample?scope=judge")
        # 完整 6×4×3 矩阵：无任何「评过项目却未评单元」的候选。
        p = preview({"target": 5, "capacities": {j: 9 for j in ["评委甲", "评委乙", "评委丙", "评委丁"]}}).json()
        self.assertEqual(p["summary"]["units"], 18)
        self.assertEqual(p["summary"]["eligible_edges"], 0)
        self.assertEqual(p["summary"]["assigned_total"], 0)
        # 重复请求结果一致。
        again = preview(
            {"target": 5, "capacities": {j: 9 for j in ["评委甲", "评委乙", "评委丙", "评委丁"]}}
        ).json()
        self.assertEqual(again["assignments"], p["assignments"])


if __name__ == "__main__":
    unittest.main()
