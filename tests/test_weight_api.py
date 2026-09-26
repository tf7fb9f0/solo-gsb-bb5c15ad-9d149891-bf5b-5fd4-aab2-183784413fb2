"""API tests for the read-only item weight threshold analysis endpoint."""
import os
import sys
import tempfile
import unittest

_tmp = tempfile.mkdtemp(prefix="calib_weight_test_")
os.environ["CALIB_DATA_DIR"] = _tmp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import database as db  # noqa: E402
from app.main import app  # noqa: E402

db.init_db()
client = TestClient(app)
SID = "weight-session"

# C1: I1=7,I2=9；C2: I1=9,I2=5（单一评委，最终均分=原分）→ 调整 I2 在 w=0.5 相交。
CSV_BODY = (
    "选手,评委,项目,分数\n"
    "C1,J,I1,7.0\nC2,J,I1,9.0\nC1,J,I2,9.0\nC2,J,I2,5.0\n"
    # C3 仅有 I1=8：在 I2 分析中按 0 贡献（斜率 0 的水平线）。
    "C3,J,I1,8.0\n"
)


class TestWeightThresholdAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        client.delete(f"/api/sessions/{SID}")
        r = client.post(
            f"/api/sessions/{SID}/upload?scope=judge",
            files={"file": ("w.csv", CSV_BODY.encode("utf-8-sig"), "text/csv")},
        )
        assert r.status_code == 200, r.text

    def test_01_basic_thresholds(self):
        r = client.post(
            f"/api/sessions/{SID}/weight-thresholds",
            json={"item": "I2", "upper_bound": 3},
        )
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["item"], "I2")
        self.assertEqual(data["lower"], 0.0)
        self.assertEqual(data["upper_bound"], 3.0)
        self.assertEqual(data["scope"], "judge")
        # C1/C2 在 0.5 相交；C3 水平线还会分别与二者相交。
        crit = [e for e in data["entries"] if e["kind"] == "critical"]
        ws = sorted(e["w"] for e in crit)
        self.assertIn(0.5, [round(w, 6) for w in ws])
        self.assertEqual(data["contestants_missing_item"], ["C3"])
        # 每个临界/中点条目都带全员名次快照，且名次合法（竞赛排名）。
        for e in data["entries"]:
            self.assertEqual(len(e["ranking"]), 3)
            ranks = sorted(row["rank"] for row in e["ranking"])
            self.assertEqual(ranks[0], 1)
        # 交点临界条目标注并列选手；中点没有 ties 字段。
        main = next(e for e in crit if abs(e["w"] - 0.5) < 1e-9)
        self.assertEqual(main["ties"], [["C1", "C2"]])
        self.assertTrue(all(row["tied"] for row in main["ranking"] if row["contestant"] in ("C1", "C2")))
        self.assertFalse(any(row["tied"] for row in main["ranking"] if row["contestant"] == "C3"))
        for e in data["entries"]:
            if e["kind"] == "midpoint":
                self.assertNotIn("ties", e)
                self.assertTrue(e["left"] < e["w"] < e["right"])

    def test_02_readonly_does_not_mutate_session(self):
        before = client.get(f"/api/sessions/{SID}/analysis").json()
        client.post(f"/api/sessions/{SID}/weight-thresholds", json={"item": "I1", "upper_bound": 5})
        client.post(f"/api/sessions/{SID}/weight-thresholds", json={"item": "I2", "upper_bound": 1.5})
        after = client.get(f"/api/sessions/{SID}/analysis").json()
        self.assertEqual(
            [(b["contestant"], b["eff_total"]) for b in before["contestants"]],
            [(b["contestant"], b["eff_total"]) for b in after["contestants"]],
        )
        self.assertEqual(after["counts"]["decisions"], 0)

    def test_03_reflects_scope_switch(self):
        r1 = client.post(
            f"/api/sessions/{SID}/weight-thresholds", json={"item": "I2", "upper_bound": 3}
        ).json()
        client.post(f"/api/sessions/{SID}/scope", json={"scope": "judge_item"})
        r2 = client.post(
            f"/api/sessions/{SID}/weight-thresholds", json={"item": "I2", "upper_bound": 3}
        ).json()
        # 切换校准范围后响应标注新范围，且临界倍率随校准参数重算（本例二者不同）。
        self.assertEqual(r1["scope"], "judge")
        self.assertEqual(r2["scope"], "judge_item")
        self.assertNotEqual(
            [round(e["w"], 9) for e in r1["entries"]],
            [round(e["w"], 9) for e in r2["entries"]],
        )
        client.post(f"/api/sessions/{SID}/scope", json={"scope": "judge"})

    def test_04_reflects_decisions(self):
        # 载入内置示例（刻意设置了宽/严评委，存在校准分≠原分的评分），
        # 翻转一条裁决应改变最终均分并移动临界点。
        client.post(f"/api/sessions/{SID}/sample?scope=judge")
        analysis = client.get(f"/api/sessions/{SID}/analysis").json()
        item = analysis["items"][0]
        before = client.post(
            f"/api/sessions/{SID}/weight-thresholds",
            json={"item": item, "upper_bound": 3},
        ).json()
        target = next(
            s for b in analysis["contestants"] for ib in b["items"] for s in ib["scores"]
            if abs(s["calibrated"] - s["raw"]) > 1e-9
        )
        r = client.patch(
            f"/api/sessions/{SID}/scores/{target['id']}/decision",
            json={"use_raw": True, "reason": "权重接口应反映裁决"},
        )
        self.assertEqual(r.status_code, 200)
        after = client.post(
            f"/api/sessions/{SID}/weight-thresholds",
            json={"item": item, "upper_bound": 3},
        ).json()
        n = analysis["counts"]["contestants"]

        def snapshot_totals(payload):
            return {
                row["contestant"]: round(row["total"], 9)
                for e in payload["entries"]
                if e["kind"] == "midpoint"
                for row in e["ranking"]
            }

        # 裁决改写了该选手在该项目（或其余项目）的最终分：中点快照总分必然变化。
        self.assertNotEqual(snapshot_totals(before), snapshot_totals(after))
        for e in after["entries"]:
            self.assertEqual(len(e["ranking"]), n)
        client.post(f"/api/sessions/{SID}/decisions/reset")

    def test_05_errors(self):
        # 未知项目 → 400。
        r = client.post(
            f"/api/sessions/{SID}/weight-thresholds",
            json={"item": "不存在的项目", "upper_bound": 3},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("项目", r.json()["detail"])
        # 上限越界 → 422（pydantic 校验）。
        r2 = client.post(
            f"/api/sessions/{SID}/weight-thresholds", json={"item": "I1", "upper_bound": 6}
        )
        self.assertEqual(r2.status_code, 422)
        r3 = client.post(
            f"/api/sessions/{SID}/weight-thresholds", json={"item": "I1", "upper_bound": 0}
        )
        self.assertEqual(r3.status_code, 422)
        # 未知会话 → 404。
        r4 = client.post(
            "/api/sessions/nope/weight-thresholds", json={"item": "I1", "upper_bound": 3}
        )
        self.assertEqual(r4.status_code, 404)

    def test_06_empty_session_returns_404(self):
        client.delete(f"/api/sessions/empty-ws")
        r = client.post(
            "/api/sessions/empty-ws/weight-thresholds",
            json={"item": "I1", "upper_bound": 3},
        )
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
