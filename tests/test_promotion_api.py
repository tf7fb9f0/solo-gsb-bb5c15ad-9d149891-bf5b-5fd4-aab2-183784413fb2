"""API tests for the read-only promotion threshold check endpoint."""
import os
import sys
import tempfile
import unittest

_tmp = tempfile.mkdtemp(prefix="calib_promo_test_")
os.environ["CALIB_DATA_DIR"] = _tmp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import database as db  # noqa: E402
from app.main import app  # noqa: E402

db.init_db()
client = TestClient(app)
SID = "promo-session"

# 单一评委 → 最终均分 == 原始分。
# C1: I1=8,I2=9；C2: I1=6,I2=7；C3 仅有 I1=5（缺 I2）。
CSV_BODY = (
    "选手,评委,项目,分数\n"
    "C1,J,I1,8\nC2,J,I1,6\nC3,J,I1,5\n"
    "C1,J,I2,9\nC2,J,I2,7\n"
)

URL = f"/api/sessions/{SID}/promotion-check"


class TestPromotionAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        client.delete(f"/api/sessions/{SID}")
        r = client.post(
            f"/api/sessions/{SID}/upload?scope=judge",
            files={"file": ("p.csv", CSV_BODY.encode("utf-8-sig"), "text/csv")},
        )
        assert r.status_code == 200, r.text

    def test_01_basic_check(self):
        r = client.post(URL, json={"item_thresholds": [{"item": "I1", "min_avg": 7}]})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["scope"], "judge")
        self.assertIsNone(data["thresholds"]["total"])
        status = {row["contestant"]: row["status"] for row in data["contestants"]}
        self.assertEqual(status, {"C1": "pass", "C2": "fail", "C3": "fail"})
        self.assertEqual(data["summary"], {"contestants": 3, "pass": 1, "fail": 2, "insufficient_data": 0})
        labels = data["status_labels"]
        self.assertEqual(labels["pass"], "达标")

    def test_02_missing_item_is_insufficient(self):
        r = client.post(URL, json={"item_thresholds": [{"item": "I2", "min_avg": 100}]})
        self.assertEqual(r.status_code, 200, r.text)
        c3 = next(row for row in r.json()["contestants"] if row["contestant"] == "C3")
        self.assertEqual(c3["status"], "insufficient_data")
        self.assertEqual(c3["missing_items"], ["I2"])
        self.assertEqual(c3["reasons"][0]["kind"], "missing_items")
        self.assertIn("I2", c3["reasons"][0]["message"])
        self.assertIsNone(c3["total_check"])

    def test_03_total_threshold_and_reasons(self):
        r = client.post(
            URL,
            json={"item_thresholds": [{"item": "I1", "min_avg": 1}], "total_threshold": 14},
        )
        data = r.json()
        rows = {row["contestant"]: row for row in data["contestants"]}
        self.assertEqual(rows["C1"]["status"], "pass")
        self.assertTrue(rows["C1"]["total_check"]["passes"])
        c2 = rows["C2"]
        self.assertEqual(c2["status"], "fail")
        self.assertFalse(c2["total_check"]["passes"])
        self.assertAlmostEqual(c2["total_check"]["gap"], 1.0)
        kinds = [x["kind"] for x in c2["reasons"]]
        self.assertEqual(kinds, ["total_below"])

    def test_04_multiple_item_thresholds(self):
        r = client.post(
            URL,
            json={
                "item_thresholds": [
                    {"item": "I2", "min_avg": 8},
                    {"item": "I1", "min_avg": 7},
                ],
                "total_threshold": 20,
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        rows = {row["contestant"]: row for row in r.json()["contestants"]}
        # C1 项目均达标（I1=8、I2=9）但总分 17 < 20 → 未达标（仅总分原因）；
        # C2 I2=7 < 8 且总分 13 < 20 → 未达标；C3 缺门槛项目 I2 → 数据不足。
        self.assertEqual(rows["C1"]["status"], "fail")
        c1 = rows["C1"]
        self.assertEqual([x["kind"] for x in c1["reasons"]], ["total_below"])
        self.assertEqual(rows["C2"]["status"], "fail")
        c2 = rows["C2"]
        self.assertEqual(
            [x["kind"] for x in c2["reasons"]], ["item_below", "total_below"]
        )
        self.assertEqual(rows["C3"]["status"], "insufficient_data")

    def test_05_readonly_does_not_mutate_session(self):
        before = client.get(f"/api/sessions/{SID}/analysis").json()
        client.post(URL, json={"item_thresholds": [{"item": "I1", "min_avg": 9}], "total_threshold": 99})
        after = client.get(f"/api/sessions/{SID}/analysis").json()
        self.assertEqual(after["counts"]["decisions"], 0)
        self.assertEqual(
            [(b["contestant"], b["eff_total"]) for b in before["contestants"]],
            [(b["contestant"], b["eff_total"]) for b in after["contestants"]],
        )

    def test_06_reflects_decisions_and_scope(self):
        before = client.post(
            URL, json={"item_thresholds": [{"item": "I1", "min_avg": 7}]}
        ).json()
        analysis = client.get(f"/api/sessions/{SID}/analysis").json()
        target = next(
            s for b in analysis["contestants"] for ib in b["items"] for s in ib["scores"]
            if b["contestant"] == "C2" and ib["item"] == "I1"
        )
        client.patch(
            f"/api/sessions/{SID}/scores/{target['id']}/decision",
            json={"use_raw": True, "reason": "门槛接口应反映裁决"},
        )
        after = client.post(
            URL, json={"item_thresholds": [{"item": "I1", "min_avg": 7}]}
        ).json()
        # 本例单评委下保留原分不改变均分，但接口必须返回最新分析结构。
        self.assertEqual(after["scope"], "judge")
        c2 = next(row for row in after["contestants"] if row["contestant"] == "C2")
        self.assertAlmostEqual(
            next(c for c in c2["items"] if c["item"] == "I1")["avg"], 6.0
        )
        client.post(f"/api/sessions/{SID}/decisions/reset")
        # 范围切换反映在响应上。
        client.post(f"/api/sessions/{SID}/scope", json={"scope": "judge_item"})
        scoped = client.post(
            URL, json={"item_thresholds": [{"item": "I1", "min_avg": 7}]}
        ).json()
        self.assertEqual(scoped["scope"], "judge_item")
        client.post(f"/api/sessions/{SID}/scope", json={"scope": "judge"})

    def test_07_validation_errors(self):
        # 空门槛数组 → 422（pydantic min_length）。
        r = client.post(URL, json={"item_thresholds": []})
        self.assertEqual(r.status_code, 422)
        # 缺少字段 → 422。
        r = client.post(URL, json={"total_threshold": 10})
        self.assertEqual(r.status_code, 422)
        # 未知项目 → 400。
        r = client.post(URL, json={"item_thresholds": [{"item": "Nope", "min_avg": 1}]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Nope", r.json()["detail"])
        # 重复项目 → 400。
        r = client.post(
            URL,
            json={
                "item_thresholds": [
                    {"item": "I1", "min_avg": 1},
                    {"item": "I1", "min_avg": 2},
                ]
            },
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("重复", r.json()["detail"])
        # NaN / Infinity → 400（纯函数层拒绝；用裸 JSON 字面量绕过客户端序列化）。
        headers = {"Content-Type": "application/json"}
        r = client.post(
            URL,
            content='{"item_thresholds":[{"item":"I1","min_avg":NaN}]}',
            headers=headers,
        )
        self.assertEqual(r.status_code, 400)
        r = client.post(
            URL,
            content='{"item_thresholds":[{"item":"I1","min_avg":1}],"total_threshold":Infinity}',
            headers=headers,
        )
        self.assertEqual(r.status_code, 400)

    def test_08_unknown_session_and_empty_session(self):
        r = client.post(
            "/api/sessions/nope/promotion-check",
            json={"item_thresholds": [{"item": "I1", "min_avg": 1}]},
        )
        self.assertEqual(r.status_code, 404)
        client.delete("/api/sessions/empty-pr")
        r2 = client.post(
            "/api/sessions/empty-pr/promotion-check",
            json={"item_thresholds": [{"item": "I1", "min_avg": 1}]},
        )
        self.assertEqual(r2.status_code, 404)


if __name__ == "__main__":
    unittest.main()
