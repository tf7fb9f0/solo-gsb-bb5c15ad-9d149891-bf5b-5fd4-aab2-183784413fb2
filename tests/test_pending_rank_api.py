"""API tests for the read-only pending-score rank bounds endpoint."""
import os
import sys
import tempfile
import unittest

_tmp = tempfile.mkdtemp(prefix="calib_pending_test_")
os.environ["CALIB_DATA_DIR"] = _tmp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import database as db  # noqa: E402
from app.main import app  # noqa: E402

db.init_db()
client = TestClient(app)
SID = "pending-session"

# C1: I1={J1:8, J2:6}、I2={J1:10}；C2: I1={J1:5}、I2={J1:9, J2:7}；C3: 仅 I2={J1:4}。
CSV_BODY = (
    "选手,评委,项目,分数\n"
    "C1,J1,I1,8\nC1,J2,I1,6\nC1,J1,I2,10\n"
    "C2,J1,I1,5\nC2,J1,I2,9\nC2,J2,I2,7\n"
    "C3,J1,I2,4\n"
)

URL = f"/api/sessions/{SID}/pending-rank"
BODY = {"item": "I1", "target": 3, "score_min": 0, "score_max": 10}


def analysis_blocks():
    data = client.get(f"/api/sessions/{SID}/analysis").json()
    return {b["contestant"]: b for b in data["contestants"]}


class TestPendingRankAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        client.delete(f"/api/sessions/{SID}")
        r = client.post(
            f"/api/sessions/{SID}/upload?scope=judge",
            files={"file": ("p.csv", CSV_BODY.encode("utf-8-sig"), "text/csv")},
        )
        assert r.status_code == 200, r.text

    def test_01_basic_bounds(self):
        r = client.post(URL, json=BODY)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["scope"], "judge")
        self.assertEqual(data["item"], "I1")
        self.assertEqual(data["target"], 3)
        self.assertEqual(data["score_min"], 0.0)
        self.assertEqual(data["score_max"], 10.0)
        self.assertEqual(
            data["summary"],
            {"contestants": 3, "contestants_with_gap": 3, "gap_total": 6, "missing_item": 1},
        )
        rows = {row["contestant"]: row for row in data["contestants"]}
        self.assertEqual([row["contestant"] for row in data["contestants"]], ["C1", "C2", "C3"])
        # 与当前分析的最终分一致：缺口、均分区间与总分区间由最终分重算。
        blocks = analysis_blocks()
        for name, row in rows.items():
            blk = blocks[name]
            ib = next((x for x in blk["items"] if x["item"] == "I1"), None)
            n = len(ib["scores"]) if ib else 0
            exp_sum = sum(s["effective"] for s in ib["scores"]) if ib else 0.0
            base = blk["eff_total"] - (ib["eff_avg"] if ib else 0.0)
            self.assertEqual(row["existing_count"], n)
            self.assertEqual(row["gap"], 3 - n)
            self.assertAlmostEqual(row["existing_sum"], exp_sum)
            self.assertAlmostEqual(row["item_avg_min"], (exp_sum + row["gap"] * 0.0) / 3)
            self.assertAlmostEqual(row["item_avg_max"], (exp_sum + row["gap"] * 10.0) / 3)
            self.assertAlmostEqual(row["total_min"], base + row["item_avg_min"])
            self.assertAlmostEqual(row["total_max"], base + row["item_avg_max"])
            self.assertEqual(row["rank"], blk["eff_rank"])
            self.assertLessEqual(row["best_rank"], row["worst_rank"])
        self.assertIsNone(rows["C3"]["current_item_avg"])
        self.assertFalse(rows["C3"]["has_item"])

    def test_02_rank_bounds_match_anchor_ranking(self):
        from app.scoring import competition_rank

        data = client.post(URL, json=BODY).json()
        rows = data["contestants"]

        def anchored_rank(target, own_key, other_key):
            # 与主排名一致的锚定分组：按极端情形总分降序，成员资格相对本组
            # 最高分锚定（而非逐对相邻比较），名次取并列组首个位置（1224）。
            vec = {
                r["contestant"]: (r[own_key] if r is target else r[other_key])
                for r in rows
            }
            return competition_rank(vec.items())[target["contestant"]]

        for row in rows:
            self.assertEqual(
                row["best_rank"], anchored_rank(row, "total_max", "total_min")
            )
            self.assertEqual(
                row["worst_rank"], anchored_rank(row, "total_min", "total_max")
            )

    def test_03_overflow_rejected(self):
        # C1 在 I1 已有 2 条评分，目标 1 < 2 → 400。
        r = client.post(URL, json={"item": "I1", "target": 1, "score_min": 0, "score_max": 10})
        self.assertEqual(r.status_code, 400)
        self.assertIn("C1", r.json()["detail"])
        # 目标 2 恰好等于 C1 已有数 → 允许，C1 缺口为 0、区间退化。
        r2 = client.post(URL, json={"item": "I1", "target": 2, "score_min": 0, "score_max": 10})
        self.assertEqual(r2.status_code, 200, r2.text)
        c1 = next(row for row in r2.json()["contestants"] if row["contestant"] == "C1")
        self.assertEqual(c1["gap"], 0)
        self.assertAlmostEqual(c1["item_avg_min"], c1["item_avg_max"])
        self.assertAlmostEqual(c1["total_min"], c1["total_max"])

    def test_04_readonly_does_not_mutate_session(self):
        before = client.get(f"/api/sessions/{SID}/analysis").json()
        client.post(URL, json=BODY)
        client.post(URL, json={"item": "I2", "target": 5, "score_min": -3, "score_max": 20})
        after = client.get(f"/api/sessions/{SID}/analysis").json()
        self.assertEqual(after["counts"]["decisions"], 0)
        self.assertEqual(
            [(b["contestant"], b["eff_total"]) for b in before["contestants"]],
            [(b["contestant"], b["eff_total"]) for b in after["contestants"]],
        )

    def test_05_reflects_decisions(self):
        before = client.post(URL, json=BODY).json()
        blocks = analysis_blocks()
        # 找一条校准分 ≠ 原分的 I1 评分，改为保留原分 → 已有最终分之和与区间必须变化。
        target = next(
            s
            for b in blocks.values()
            for ib in b["items"] if ib["item"] == "I1"
            for s in ib["scores"]
            if abs(s["calibrated"] - s["raw"]) > 1e-9
        )
        r = client.patch(
            f"/api/sessions/{SID}/scores/{target['id']}/decision",
            json={"use_raw": True, "reason": "名次边界应反映裁决"},
        )
        self.assertEqual(r.status_code, 200)
        after = client.post(URL, json=BODY).json()

        def sums(payload):
            return {row["contestant"]: round(row["existing_sum"], 9) for row in payload["contestants"]}

        self.assertNotEqual(sums(before), sums(after))
        client.post(f"/api/sessions/{SID}/decisions/reset")
        restored = client.post(URL, json=BODY).json()
        self.assertEqual(sums(before), sums(restored))

    def test_06_reflects_scope_switch(self):
        r1 = client.post(URL, json=BODY).json()
        client.post(f"/api/sessions/{SID}/scope", json={"scope": "judge_item"})
        r2 = client.post(URL, json=BODY).json()
        self.assertEqual(r1["scope"], "judge")
        self.assertEqual(r2["scope"], "judge_item")
        # 校准参数变化 → 最终分变化 → 总分区间随之重算（本例二者不同）。
        self.assertNotEqual(
            [round(row["total_min"], 9) for row in r1["contestants"]],
            [round(row["total_min"], 9) for row in r2["contestants"]],
        )
        client.post(f"/api/sessions/{SID}/scope", json={"scope": "judge"})

    def test_07_validation_errors(self):
        # 未知项目 → 400。
        r = client.post(URL, json={"item": "Nope", "target": 3, "score_min": 0, "score_max": 10})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Nope", r.json()["detail"])
        # 下限 > 上限 → 400。
        r = client.post(URL, json={"item": "I1", "target": 3, "score_min": 11, "score_max": 10})
        self.assertEqual(r.status_code, 400)
        self.assertIn("下限", r.json()["detail"])
        # 目标人数越界 / 非整数 → 422（pydantic 校验）。
        r = client.post(URL, json={"item": "I1", "target": 0, "score_min": 0, "score_max": 10})
        self.assertEqual(r.status_code, 422)
        r = client.post(URL, json={"item": "I1", "target": 2.5, "score_min": 0, "score_max": 10})
        self.assertEqual(r.status_code, 422)
        # NaN / Infinity → 400（纯函数层拒绝；用裸 JSON 字面量绕过客户端序列化）。
        headers = {"Content-Type": "application/json"}
        r = client.post(
            URL,
            content='{"item":"I1","target":3,"score_min":NaN,"score_max":10}',
            headers=headers,
        )
        self.assertEqual(r.status_code, 400)
        r = client.post(
            URL,
            content='{"item":"I1","target":3,"score_min":0,"score_max":Infinity}',
            headers=headers,
        )
        self.assertEqual(r.status_code, 400)

    def test_08_unknown_session_and_empty_session(self):
        r = client.post("/api/sessions/nope/pending-rank", json=BODY)
        self.assertEqual(r.status_code, 404)
        client.delete("/api/sessions/empty-pd")
        r2 = client.post("/api/sessions/empty-pd/pending-rank", json=BODY)
        self.assertEqual(r2.status_code, 404)


if __name__ == "__main__":
    unittest.main()
