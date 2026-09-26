"""End-to-end API tests with a temporary data directory."""
import os
import sys
import tempfile
import unittest

_tmp = tempfile.mkdtemp(prefix="calib_test_")
os.environ["CALIB_DATA_DIR"] = _tmp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import database as db  # noqa: E402
from app.main import app  # noqa: E402

db.init_db()
client = TestClient(app)
SID = "test-session"

CSV_BODY = (
    "选手,评委,项目,分数\n"
    "C1,JA,P1,8.30\nC2,JA,P1,8.00\nC3,JA,P1,7.90\nC4,JA,P1,7.80\n"
    "C1,JB,P1,8.30\nC2,JB,P1,8.00\nC3,JB,P1,7.85\nC4,JB,P1,7.75\n"
    "C1,JC,P1,5.0\nC2,JC,P1,9.5\nC3,JC,P1,7.2\nC4,JC,P1,6.8\n"
)


class TestAPI(unittest.TestCase):
    def test_01_health_and_empty(self):
        self.assertEqual(client.get("/api/health").json()["status"], "ok")
        r = client.get(f"/api/sessions/{SID}/analysis")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["counts"]["scores"], 0)

    def test_02_upload_errors(self):
        r = client.post(
            f"/api/sessions/{SID}/upload?scope=judge",
            files={"file": ("bad.csv", "选手,评委,分数\nA,J,9".encode(), "text/csv")},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("项目", r.json()["detail"])

    def test_03_upload_ok(self):
        r = client.post(
            f"/api/sessions/{SID}/upload?scope=judge",
            files={"file": ("s.csv", CSV_BODY.encode("utf-8-sig"), "text/csv")},
        )
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["counts"]["scores"], 12)
        self.assertGreaterEqual(len(data["reversals"]), 1)

    def test_04_persists_across_requests(self):
        r = client.get(f"/api/sessions/{SID}/analysis")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["counts"]["scores"], 12)

    def test_05_decision_and_rerank(self):
        # Override C1's first raw score (id 1) and verify effective totals change.
        before = client.get(f"/api/sessions/{SID}/analysis").json()
        r = client.patch(
            f"/api/sessions/{SID}/scores/1/decision",
            json={"use_raw": True, "reason": "复核确认"},
        )
        self.assertEqual(r.status_code, 200)
        after = r.json()
        self.assertEqual(after["counts"]["decisions"], 1)
        self.assertNotEqual(
            after["contestants"][0]["eff_total"], before["contestants"][0]["eff_total"]
        )

    def test_06_scope_switch(self):
        r = client.post(f"/api/sessions/{SID}/scope", json={"scope": "judge_item"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["scope"], "judge_item")
        self.assertEqual(len(r.json()["groups"]), 3)
        # switch back
        client.post(f"/api/sessions/{SID}/scope", json={"scope": "judge"})

    def test_07_reset(self):
        r = client.post(f"/api/sessions/{SID}/decisions/reset")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["counts"]["decisions"], 0)

    def test_08_rules_preview_is_readonly(self):
        analysis = client.get(f"/api/sessions/{SID}/analysis").json()
        n_scores = analysis["counts"]["scores"]
        judge = analysis["judges"][0]
        rules = [
            {"name": "保留低分", "judge": judge, "raw_max": 8.0, "use_raw": True,
             "reason_template": "{judge}低分复核{raw}", "priority": 10},
            {"name": "全部校准", "use_raw": False, "reason_template": "默认校准", "priority": 100},
        ]
        r = client.post(f"/api/sessions/{SID}/rules/preview", json={"rules": rules})
        self.assertEqual(r.status_code, 200, r.text)
        p = r.json()
        self.assertEqual(p["scope"], "judge")
        self.assertEqual(p["total_scores"], n_scores)
        self.assertEqual(p["rules_submitted"], 2)
        self.assertEqual(p["rules_saved"], 0)
        # The catch-all rule (index 1) hits everything; rule 0 hits a subset.
        catch_all = p["rules"][1]
        self.assertEqual(catch_all["hits"], n_scores)
        self.assertEqual(catch_all["wins"], n_scores - p["rules"][0]["hits"])
        # Low scores are claimed by both rules -> conflicts resolved by priority.
        self.assertGreater(p["conflicted"], 0)
        self.assertEqual(p["matched"], n_scores)
        self.assertEqual(p["unmatched"], 0)
        # Reason placeholder substitution happened.
        winner0 = next(a for a in p["assignments"] if a["winner_index"] == 0)
        self.assertIn(judge, winner0["reason"])
        self.assertTrue(winner0["use_raw"])

        # Nothing was persisted and no decisions changed.
        again = client.get(f"/api/sessions/{SID}/analysis").json()
        self.assertEqual(again["counts"]["decisions"], 0)
        self.assertEqual(client.get(f"/api/sessions/{SID}/rules").json()["rules"], [])

    def test_09_rules_preview_validation(self):
        analysis = client.get(f"/api/sessions/{SID}/analysis").json()
        # Unknown judge -> 400.
        r = client.post(
            f"/api/sessions/{SID}/rules/preview",
            json={"rules": [{"judge": "不存在的评委", "use_raw": True}]},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("评委", r.json()["detail"])
        # Inverted interval -> 400.
        r2 = client.post(
            f"/api/sessions/{SID}/rules/preview",
            json={"rules": [{"raw_min": 9, "raw_max": 1}]},
        )
        self.assertEqual(r2.status_code, 400)
        self.assertIn("原始分区间", r2.json()["detail"])
        # Unknown session -> 404; still 12 scores untouched.
        r3 = client.post("/api/sessions/nope/rules/preview", json={"rules": []})
        self.assertEqual(r3.status_code, 404)
        self.assertEqual(
            client.get(f"/api/sessions/{SID}/analysis").json()["counts"]["scores"], 12
        )

    def test_10_rules_apply_batch_transaction(self):
        analysis = client.get(f"/api/sessions/{SID}/analysis").json()
        judge = analysis["judges"][0]
        target = next(
            s for b in analysis["contestants"] for ib in b["items"] for s in ib["scores"]
            if s["judge"] == judge and s["raw"] <= 8.0
        )
        rules = [
            {"judge": judge, "raw_max": 8.0, "use_raw": True,
             "reason_template": "复核", "priority": 10},
            {"use_raw": False, "reason_template": "校准默认", "priority": 100},
        ]
        r = client.post(f"/api/sessions/{SID}/rules/apply", json={"rules": rules})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        # Every score matched (catch-all), ranks refreshed in same response.
        self.assertEqual(data["matched"], 12)
        self.assertEqual(data["unmatched"], 0)
        self.assertEqual(data["written"], 12)
        self.assertEqual(data["rules_saved"], 2)
        self.assertGreaterEqual(data["changed"], 1)
        self.assertIn("contestants", data)
        # The target score now keeps its raw value with the rendered reason.
        applied = next(
            s for b in data["contestants"] for ib in b["items"] for s in ib["scores"]
            if s["id"] == target["id"]
        )
        self.assertTrue(applied["use_raw"])
        self.assertEqual(applied["reason"], "复核")
        self.assertEqual(applied["effective"], applied["raw"])
        # Rules persisted and survive a new request.
        saved = client.get(f"/api/sessions/{SID}/rules").json()["rules"]
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[0]["priority"], 10)
        self.assertTrue(saved[0]["use_raw"])
        self.assertIsNone(saved[1]["judge"])

    def test_11_rules_saved_preview_uses_persisted_count(self):
        # Preview of the now-stored rules without resubmitting them (empty body)
        # still reports saved rules count; applying an empty set writes nothing.
        p = client.post(f"/api/sessions/{SID}/rules/preview", json={"rules": []}).json()
        self.assertEqual(p["rules_saved"], 2)
        self.assertEqual(p["unmatched"], p["total_scores"])

    def test_12_save_rules_without_apply(self):
        r = client.put(
            f"/api/sessions/{SID}/rules",
            json={"rules": [{"use_raw": True, "item": "P1", "priority": 5}]},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["saved"], 1)
        # Decisions from the previous apply remain (save does not touch them).
        analysis = client.get(f"/api/sessions/{SID}/analysis").json()
        self.assertEqual(analysis["counts"]["scores"], 12)
        saved_rules = client.get(f"/api/sessions/{SID}/rules").json()["rules"]
        self.assertEqual(saved_rules[0]["item"], "P1")
        # Clear rules via PUT []; decisions must remain.
        decisions_before = client.get(f"/api/sessions/{SID}/analysis").json()["counts"]["decisions"]
        r2 = client.put(f"/api/sessions/{SID}/rules", json={"rules": []})
        self.assertEqual(r2.json()["saved"], 0)
        decisions_after = client.get(f"/api/sessions/{SID}/analysis").json()["counts"]["decisions"]
        self.assertEqual(decisions_before, decisions_after)

    def test_13_export_includes_rules_section(self):
        # Re-save one rule then export.
        client.put(f"/api/sessions/{SID}/rules", json={"rules": [{"name": "审计规则", "use_raw": False}]})
        r = client.get(f"/api/sessions/{SID}/export")
        self.assertEqual(r.status_code, 200)
        self.assertIn("批处理裁决规则".encode(), r.content)
        self.assertIn("审计规则".encode(), r.content)

    def test_14_upload_wipes_rules(self):
        # Uploading fresh data must remove rules (cascade with scores).
        client.put(f"/api/sessions/{SID}/rules", json={"rules": [{"use_raw": True}]})
        r = client.post(
            f"/api/sessions/{SID}/upload?scope=judge",
            files={"file": ("s.csv", CSV_BODY.encode("utf-8-sig"), "text/csv")},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(client.get(f"/api/sessions/{SID}/rules").json()["rules"], [])

    def test_15_stability_experiment(self):
        # Load the sample fixture (6 contestants x 4 judges x 3 items).
        client.post(f"/api/sessions/{SID}/sample?scope=judge")
        analysis = client.get(f"/api/sessions/{SID}/analysis").json()
        judge = analysis["judges"][0]
        n_scores = analysis["counts"]["scores"]

        r = client.post(
            f"/api/sessions/{SID}/stability",
            json={"exclude_type": "judge", "exclude_name": judge},
        )
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["exclusion"]["type"], "judge")
        self.assertEqual(data["exclusion"]["name"], judge)
        self.assertEqual(data["baseline_counts"]["scores"], n_scores)
        # Every contestant x item has one score per judge -> 6 * 3 removed.
        self.assertEqual(data["removed_scores"], 18)
        self.assertEqual(data["scenario_counts"]["scores"], n_scores - 18)
        self.assertEqual(len(data["contestants"]), analysis["counts"]["contestants"])

        # Baseline totals are echoed and every row carries deltas.
        base_blocks = {b["contestant"]: b for b in analysis["contestants"]}
        for row in data["contestants"]:
            self.assertTrue(row["present"])
            self.assertEqual(row["baseline"]["eff_rank"], base_blocks[row["contestant"]]["eff_rank"])
            for kind in ("raw", "cal", "eff"):
                self.assertIn(f"{kind}_rank_delta", row)
                self.assertIn(f"{kind}_total_delta", row)
            self.assertGreaterEqual(row["affected_scores"], 1)

        # Item exclusion too.
        item = analysis["items"][0]
        r2 = client.post(
            f"/api/sessions/{SID}/stability",
            json={"exclude_type": "item", "exclude_name": item},
        )
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["exclusion"]["type"], "item")
        self.assertEqual(r2.json()["removed_scores"], 24)  # 6 contestants x 4 judges

        # The experiment must not mutate scores/decisions: baseline is unchanged.
        again = client.get(f"/api/sessions/{SID}/analysis").json()
        self.assertEqual(again["counts"]["scores"], n_scores)
        self.assertEqual(len(again["judges"]), 4)

    def test_16_stability_errors(self):
        # Unknown judge name -> 400.
        r = client.post(
            f"/api/sessions/{SID}/stability",
            json={"exclude_type": "judge", "exclude_name": "不存在的评委"},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("评委", r.json()["detail"])

        # Bad payload -> 422 from pydantic.
        r2 = client.post(
            f"/api/sessions/{SID}/stability",
            json={"exclude_type": "round", "exclude_name": "X"},
        )
        self.assertEqual(r2.status_code, 422)

        # Unknown session -> 404.
        r3 = client.post(
            "/api/sessions/no-such-session/stability",
            json={"exclude_type": "judge", "exclude_name": "J"},
        )
        self.assertEqual(r3.status_code, 404)

    def test_17_clear(self):
        r = client.delete(f"/api/sessions/{SID}")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["cleared"])
        r2 = client.get(f"/api/sessions/{SID}/analysis")
        self.assertEqual(r2.json()["counts"]["scores"], 0)
        # Rules are gone with the session.
        self.assertEqual(client.get(f"/api/sessions/{SID}/rules").json()["rules"], [])
        # Export after clearing must fail
        self.assertEqual(client.get(f"/api/sessions/{SID}/export").status_code, 404)
        # Stability experiment on an empty session must also fail
        r3 = client.post(
            f"/api/sessions/{SID}/stability",
            json={"exclude_type": "judge", "exclude_name": "J"},
        )
        self.assertEqual(r3.status_code, 404)


    def test_18_compare_endpoint(self):
        # Session was cleared in test_17; reload the sample fixture.
        client.post(f"/api/sessions/{SID}/sample?scope=judge")
        analysis = client.get(f"/api/sessions/{SID}/analysis").json()
        names = [b["contestant"] for b in analysis["contestants"]]
        a, b = names[0], names[1]
        r = client.get(f"/api/sessions/{SID}/compare", params={"a": a, "b": b})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["a"], a)
        self.assertEqual(data["b"], b)
        self.assertEqual(data["scope"], "judge")
        self.assertEqual(len(data["items"]), analysis["counts"]["items"])
        # Total deltas are consistent with the analysis blocks.
        blocks = {blk["contestant"]: blk for blk in analysis["contestants"]}
        for kind in ("raw", "cal", "eff"):
            self.assertAlmostEqual(
                data["totals"]["delta"][kind],
                blocks[a][f"{kind}_total"] - blocks[b][f"{kind}_total"],
            )
        # Summary counts add up; sample data has no missing items.
        s = data["summary"]
        self.assertEqual(
            s["compared"] + s["a_only"] + s["b_only"], analysis["counts"]["items"]
        )
        self.assertEqual(s["a_only"], 0)
        self.assertEqual(s["b_only"], 0)
        for kind in ("raw", "cal", "eff"):
            c = s[kind]
            self.assertEqual(c["a_leads"] + c["b_leads"] + c["ties"], s["compared"])

    def test_19_compare_reflects_decision_and_scope(self):
        analysis = client.get(f"/api/sessions/{SID}/analysis").json()
        names = [b["contestant"] for b in analysis["contestants"]]
        a, b = names[0], names[1]
        before = client.get(f"/api/sessions/{SID}/compare", params={"a": a, "b": b}).json()
        # Flip one of A's scores whose calibrated value differs from raw.
        target = next(
            s
            for blk in analysis["contestants"]
            if blk["contestant"] == a
            for ib in blk["items"]
            for s in ib["scores"]
            if abs(s["calibrated"] - s["raw"]) > 1e-9
        )
        client.patch(
            f"/api/sessions/{SID}/scores/{target['id']}/decision",
            json={"use_raw": True, "reason": "对比测试"},
        )
        after = client.get(f"/api/sessions/{SID}/compare", params={"a": a, "b": b}).json()
        self.assertNotAlmostEqual(
            after["totals"]["delta"]["eff"], before["totals"]["delta"]["eff"]
        )
        self.assertAlmostEqual(
            after["totals"]["delta"]["raw"], before["totals"]["delta"]["raw"]
        )
        self.assertAlmostEqual(
            after["totals"]["delta"]["cal"], before["totals"]["delta"]["cal"]
        )
        # Scope switch recomputes calibrated values in the comparison too.
        client.post(f"/api/sessions/{SID}/scope", json={"scope": "judge_item"})
        scoped = client.get(f"/api/sessions/{SID}/compare", params={"a": a, "b": b}).json()
        self.assertEqual(scoped["scope"], "judge_item")
        self.assertNotAlmostEqual(
            scoped["totals"]["delta"]["cal"], after["totals"]["delta"]["cal"]
        )
        # Restore baseline state for later tests.
        client.post(f"/api/sessions/{SID}/scope", json={"scope": "judge"})
        client.post(f"/api/sessions/{SID}/decisions/reset")

    def test_20_compare_errors_and_readonly(self):
        analysis = client.get(f"/api/sessions/{SID}/analysis").json()
        names = [b["contestant"] for b in analysis["contestants"]]
        # Same contestant on both sides -> 400.
        r = client.get(
            f"/api/sessions/{SID}/compare", params={"a": names[0], "b": names[0]}
        )
        self.assertEqual(r.status_code, 400)
        # Unknown contestant -> 400.
        r2 = client.get(
            f"/api/sessions/{SID}/compare", params={"a": names[0], "b": "查无此人"}
        )
        self.assertEqual(r2.status_code, 400)
        self.assertIn("查无此人", r2.json()["detail"])
        # Unknown session -> 404.
        r3 = client.get("/api/sessions/nope/compare", params={"a": "x", "b": "y"})
        self.assertEqual(r3.status_code, 404)
        # Read-only: no decisions were created by any of the comparisons.
        again = client.get(f"/api/sessions/{SID}/analysis").json()
        self.assertEqual(again["counts"]["decisions"], 0)

    def test_21_compare_missing_item_is_no_data(self):
        csv_body = (
            "选手,评委,项目,分数\n"
            "A,J1,I1,8\nA,J2,I1,9\nB,J1,I1,7\nB,J2,I1,6\n"
            "A,J1,I2,8\nA,J2,I2,8\n"  # B 在 I2 无评分
        )
        r = client.post(
            f"/api/sessions/{SID}/upload?scope=judge",
            files={"file": ("m.csv", csv_body.encode("utf-8-sig"), "text/csv")},
        )
        self.assertEqual(r.status_code, 200, r.text)
        data = client.get(f"/api/sessions/{SID}/compare", params={"a": "A", "b": "B"}).json()
        i2 = next(row for row in data["items"] if row["item"] == "I2")
        self.assertIsNotNone(i2["a"])
        self.assertIsNone(i2["b"])
        self.assertIsNone(i2["delta"])
        self.assertIsNone(i2["lead"])
        s = data["summary"]
        self.assertEqual(s["compared"], 1)
        self.assertEqual(s["a_only"], 1)
        self.assertEqual(s["b_only"], 0)
        # Only the shared item participates in lead/tie counts.
        for kind in ("raw", "cal", "eff"):
            c = s[kind]
            self.assertEqual(c["a_leads"] + c["b_leads"] + c["ties"], 1)

    def test_22_judge_disagreement_in_analysis(self):
        # JA/JB 同向；JA/JC 仅 C1↔C2 一对逆向（JC 给 C2 最高分、C1 次高）。
        csv_body = (
            "选手,评委,项目,分数\n"
            "C1,JA,P1,8.30\nC2,JA,P1,8.00\nC3,JA,P1,7.90\nC4,JA,P1,7.80\n"
            "C1,JB,P1,8.30\nC2,JB,P1,8.00\nC3,JB,P1,7.85\nC4,JB,P1,7.75\n"
            "C1,JC,P1,8.0\nC2,JC,P1,9.5\nC3,JC,P1,7.2\nC4,JC,P1,6.8\n"
        )
        r = client.post(
            f"/api/sessions/{SID}/upload?scope=judge",
            files={"file": ("s.csv", csv_body.encode("utf-8-sig"), "text/csv")},
        )
        self.assertEqual(r.status_code, 200, r.text)
        dg = r.json()["judge_disagreement"]
        self.assertEqual(dg["summary"]["groups"], 3)  # 3 judge pairs, one item
        by_pair = {(g["judge_a"], g["judge_b"]): g for g in dg["groups"]}
        g_ajc = by_pair[("JA", "JC")]
        self.assertTrue(g_ajc["sufficient"])
        self.assertEqual(g_ajc["common_count"], 4)
        self.assertEqual(g_ajc["pairs_total"], 6)
        self.assertEqual(g_ajc["counts"]["discordant"], 1)  # C1 vs C2 reversed
        self.assertEqual(g_ajc["comparable_pairs"], 6)
        self.assertAlmostEqual(g_ajc["discordance_rate"], 1 / 6)
        rev = g_ajc["discordant_pairs"]
        self.assertEqual(len(rev), 1)
        self.assertEqual({rev[0]["contestant_a"], rev[0]["contestant_b"]}, {"C1", "C2"})
        self.assertEqual(rev[0]["judge_a_prefers"], "C1")
        self.assertEqual(rev[0]["judge_b_prefers"], "C2")
        g_ajb = by_pair[("JA", "JB")]
        self.assertEqual(g_ajb["counts"]["concordant"], 6)
        self.assertEqual(g_ajb["discordance_rate"], 0.0)

        # 与校准范围、裁决无关：切范围、写裁决后原始分歧保持不变。
        scoped = client.post(f"/api/sessions/{SID}/scope", json={"scope": "judge_item"}).json()
        self.assertEqual(scoped["judge_disagreement"], dg)
        target = next(
            s for b in scoped["contestants"] for ib in b["items"] for s in ib["scores"]
            if s["judge"] == "JC"
        )
        decided = client.patch(
            f"/api/sessions/{SID}/scores/{target['id']}/decision",
            json={"use_raw": True, "reason": "分歧面板不应变化"},
        ).json()
        self.assertEqual(decided["judge_disagreement"], dg)
        client.post(f"/api/sessions/{SID}/scope", json={"scope": "judge"})
        client.post(f"/api/sessions/{SID}/decisions/reset")

    def test_23_judge_disagreement_refreshes_with_merge(self):
        # 两个评委在 P1 对 A、B 评价相反（逆向 1 对）。
        csv_before = (
            "选手,评委,项目,分数\n"
            "A,J1,P1,8\nB,J1,P1,7\nA,J2,P1,6\nB,J2,P1,9\n"
        )
        client.post(
            f"/api/sessions/{SID}/upload?scope=judge",
            files={"file": ("b.csv", csv_before.encode("utf-8-sig"), "text/csv")},
        )
        before = client.get(f"/api/sessions/{SID}/analysis").json()["judge_disagreement"]
        self.assertEqual(before["summary"]["counts"]["discordant"], 1)

        # 合并导入：把 J2 对 A 的分数改为 9，A/B 在两位评委处都同向。
        csv_after = (
            "选手,评委,项目,分数\n"
            "A,J1,P1,8\nB,J1,P1,7\nA,J2,P1,9\nB,J2,P1,9\n"
        )
        preview = client.post(
            f"/api/sessions/{SID}/merge/preview?scope=judge",
            files={"file": ("a.csv", csv_after.encode("utf-8-sig"), "text/csv")},
        ).json()
        self.assertEqual(preview["counts"]["changed"], 1)
        # 预览不落库：分歧尚未变化。
        still = client.get(f"/api/sessions/{SID}/analysis").json()["judge_disagreement"]
        self.assertEqual(still["summary"]["counts"]["discordant"], 1)

        confirmed = client.post(
            f"/api/sessions/{SID}/merge/confirm",
            json={"records": preview["records"], "basis": preview["basis"], "scope": "judge"},
        ).json()
        dg = confirmed["judge_disagreement"]
        g = dg["groups"][0]
        self.assertEqual(g["counts"]["discordant"], 0)
        # J2 给 A、B 同分 → 唯一选手对为单方同分，不计入可比较对数，不给分歧率。
        self.assertEqual(g["pairs_total"], 1)
        self.assertEqual(g["counts"]["one_tie"], 1)
        self.assertEqual(g["comparable_pairs"], 0)
        self.assertIsNone(g["discordance_rate"])

    def test_24_common_judge_endpoint_readonly(self):
        # 构造同评委可比对照数据：A/B 在 I1 有共同评委 J1/J2，在 I2 无共同评委。
        csv_body = (
            "选手,评委,项目,分数\n"
            "A,J1,I1,8\nA,J2,I1,6\nA,J1,I2,10\n"
            "B,J1,I1,7\nB,J2,I1,5\nB,J2,I2,9\n"
            "C,J3,I1,5\n"
        )
        client.post(
            f"/api/sessions/{SID}/upload?scope=judge",
            files={"file": ("cj.csv", csv_body.encode("utf-8-sig"), "text/csv")},
        )
        r = client.get(f"/api/sessions/{SID}/common-judge")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["scope"], "judge")
        s = data["summary"]
        self.assertEqual(s["pairs"], 3)
        self.assertEqual(s["comparable"], 1)
        self.assertEqual(s["incomparable"], 2)
        ab = next(p for p in data["pairs"] if {p["a"], p["b"]} == {"A", "B"})
        self.assertTrue(ab["comparable"])
        i1 = next(it for it in ab["items"] if it["item"] == "I1")
        self.assertEqual(i1["common_judges"], ["J1", "J2"])
        # 接口默认采用校准分：I1 上 A 的校准均分 7.0、B 的校准均分 5.0。
        self.assertAlmostEqual(i1["a_avg"], 7.0)
        self.assertAlmostEqual(i1["b_avg"], 5.0)
        i2 = next(it for it in ab["items"] if it["item"] == "I2")
        self.assertFalse(i2["comparable"])
        self.assertEqual(i2["reason"], "no_common_judge")
        # 只读：不改变任何裁决与总分。
        again = client.get(f"/api/sessions/{SID}/analysis").json()
        self.assertEqual(again["counts"]["decisions"], 0)
        # 未知会话 → 404。
        self.assertEqual(client.get("/api/sessions/nope/common-judge").status_code, 404)
        # 范围切换反映在响应上。
        client.post(f"/api/sessions/{SID}/scope", json={"scope": "judge_item"})
        scoped = client.get(f"/api/sessions/{SID}/common-judge").json()
        self.assertEqual(scoped["scope"], "judge_item")
        client.post(f"/api/sessions/{SID}/scope", json={"scope": "judge"})


if __name__ == "__main__":
    unittest.main()
