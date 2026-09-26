"""FastAPI application: multi-judge score calibration."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import database as db
from .backfill import BackfillValidationError, plan_backfill
from .common_judge import common_judge_comparison
from .csv_io import CSVParseError, build_audit_csv, parse_scores
from .judge_offsets import judge_offset_diagnosis
from .merge import (
    STATUS_CHANGED,
    STATUS_LABELS as MERGE_STATUS_LABELS,
    STATUS_REMOVED,
    MergeValidationError,
    basis_matches,
    build_merge_plan,
    current_basis,
    normalize_incoming_records,
    valid_basis_shape,
)
from .pending_rank import PendingRankValidationError, pending_rank_bounds
from .promotion import PromotionValidationError, promotion_check
from .rules import (
    RuleValidationError,
    evaluate_rules,
    normalize_rules,
    score_rows_from_analysis,
)
from .scoring import (
    SCOPES,
    SCOPE_JUDGE,
    SCOPE_LABELS,
    analyze,
    compare_contestants,
    stability_experiment,
)
from .weight_analysis import WeightValidationError, weight_threshold_analysis

SAMPLE_PATH = os.environ.get(
    "CALIB_SAMPLE_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples", "sample_scores.csv"),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="多评委评分校准", version="1.0.0", lifespan=lifespan)


def _build_analysis(conn, session_id: str, scope: str) -> dict[str, Any]:
    records = db.load_scores(conn, session_id)
    decisions = db.load_decisions(conn, session_id)
    result = analyze(records, decisions, scope=scope)
    result["batch_rules"] = db.load_rules(conn, session_id)
    return result


def _resolve_scope(conn, session_id: str, override: str | None) -> str:
    if override is not None:
        if override not in SCOPES:
            raise HTTPException(status_code=400, detail=f"不支持的校准范围: {override}")
        return override
    stored = db.get_scope(conn, session_id)
    return stored or SCOPE_JUDGE


class ScopeBody(BaseModel):
    scope: Literal["judge", "judge_item"]


class DecisionBody(BaseModel):
    use_raw: bool
    reason: str = Field(default="", max_length=2000)


class StabilityBody(BaseModel):
    exclude_type: Literal["judge", "item"]
    exclude_name: str = Field(min_length=1, max_length=200)


class BackfillBody(BaseModel):
    # 每个需求单元（已有评分的选手×项目）统一的目标评委人数。
    target: int = Field(ge=1, le=1_000_000)
    # 评委 -> 本轮可接任务数；未提供的评委按 0 处理。
    capacities: dict[str, int] = Field(default_factory=dict)


class WeightBody(BaseModel):
    # 要连续调整倍率的项目；基于当前校准范围与逐项裁决形成的项目最终均分。
    item: str = Field(min_length=1, max_length=200)
    # 倍率上限（1～5），倍率始终从 0 连续变化到该上限，其余项目恒为 1。
    upper_bound: float = Field(ge=1.0, le=5.0)


class ItemThresholdBody(BaseModel):
    # 单个项目的最终项目均分最低线（项目名在 pure 层校验是否存在）。
    item: str = Field(min_length=1, max_length=200)
    min_avg: float


class PromotionBody(BaseModel):
    # 一个或多个项目的最终项目均分最低线（至少一条，项目名不允许重复）。
    item_thresholds: list[ItemThresholdBody] = Field(min_length=1)
    # 可选的最终总分最低线；不填则只检查项目门槛。
    total_threshold: float | None = None


class PendingRankBody(BaseModel):
    # 待补评分所在项目；基于当前校准范围与逐项裁决形成的最终分。
    item: str = Field(min_length=1, max_length=200)
    # 该项目每位选手补足到的目标评委人数。
    target: int = Field(ge=1, le=1_000_000)
    # 每条待补评分（最终分）可取的闭区间上下限。
    score_min: float
    score_max: float


class RuleBody(BaseModel):
    name: str = Field(default="", max_length=200)
    judge: str | None = None
    item: str | None = None
    raw_min: float | None = None
    raw_max: float | None = None
    cal_min: float | None = None
    cal_max: float | None = None
    use_raw: bool = False
    reason_template: str = Field(default="", max_length=2000)
    priority: int = Field(default=100, ge=0, le=1_000_000)


class RuleSetBody(BaseModel):
    rules: list[RuleBody] = Field(default_factory=list)


class MergeConfirmBody(BaseModel):
    # 客户端原样回传预览时解析出的记录与基线快照；
    # 服务端重新校验并核对基线，避免「过期预览」覆盖新数据。
    records: list[dict[str, Any]] = Field(min_length=1)
    basis: list[dict[str, Any]] = Field(min_length=0)
    # 仅在会话尚不存在（向空会话合并）时用于创建会话；已有会话保持原校准范围。
    scope: Literal["judge", "judge_item"] | None = None


def _normalize_session_rules(body: RuleSetBody, analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate/normalize submitted rules against the session's judge/item names."""
    try:
        rules = normalize_rules([r.model_dump() for r in body.rules])
    except RuleValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    judges = set(analysis["judges"])
    items = set(analysis["items"])
    for i, rule in enumerate(rules, start=1):
        if rule["judge"] is not None and rule["judge"] not in judges:
            raise HTTPException(
                status_code=400,
                detail=f"第 {i} 条规则引用的评委「{rule['judge']}」不存在，可选：{', '.join(analysis['judges']) or '(无)'}。",
            )
        if rule["item"] is not None and rule["item"] not in items:
            raise HTTPException(
                status_code=400,
                detail=f"第 {i} 条规则引用的项目「{rule['item']}」不存在，可选：{', '.join(analysis['items']) or '(无)'}。",
            )
    return rules


def _rule_preview(analysis: dict[str, Any], rules: list[dict[str, Any]]) -> dict[str, Any]:
    """Run read-only rule matching on top of an analysis result."""
    rows = score_rows_from_analysis(analysis)
    decisions = {
        s["id"]: (s["use_raw"], s["reason"])
        for block in analysis["contestants"]
        for ib in block["items"]
        for s in ib["scores"]
    }
    preview = evaluate_rules(rows, rules, decisions)
    preview["scope"] = analysis["scope"]
    preview["scope_label"] = analysis["scope_label"]
    return preview


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/sessions/{session_id}/analysis")
def get_analysis(session_id: str, scope: str | None = None) -> dict[str, Any]:
    with db.get_conn() as conn:
        effective_scope = _resolve_scope(conn, session_id, scope)
        return _build_analysis(conn, session_id, effective_scope)


@app.post("/api/sessions/{session_id}/scope")
def update_scope(session_id: str, body: ScopeBody) -> dict[str, Any]:
    with db.get_conn() as conn:
        db.ensure_session(conn, session_id, body.scope)
        db.set_scope(conn, session_id, body.scope)
        return _build_analysis(conn, session_id, body.scope)


@app.post("/api/sessions/{session_id}/upload")
async def upload_scores(
    session_id: str,
    file: UploadFile = File(...),
    scope: str = SCOPE_JUDGE,
) -> dict[str, Any]:
    if scope not in SCOPES:
        raise HTTPException(status_code=400, detail=f"不支持的校准范围: {scope}")
    raw = await file.read()
    try:
        records = parse_scores(raw)
    except CSVParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    with db.get_conn() as conn:
        db.ensure_session(conn, session_id, scope)
        db.replace_scores(conn, session_id, records)
        return _build_analysis(conn, session_id, scope)


@app.post("/api/sessions/{session_id}/sample")
def load_sample(session_id: str, scope: str = SCOPE_JUDGE) -> dict[str, Any]:
    if scope not in SCOPES:
        raise HTTPException(status_code=400, detail=f"不支持的校准范围: {scope}")
    try:
        with open(SAMPLE_PATH, "rb") as fh:
            records = parse_scores(fh.read())
    except OSError:
        raise HTTPException(status_code=404, detail="示例数据文件不可用。")
    except CSVParseError as exc:
        raise HTTPException(status_code=500, detail=f"示例数据格式错误: {exc}")
    with db.get_conn() as conn:
        db.ensure_session(conn, session_id, scope)
        db.replace_scores(conn, session_id, records)
        return _build_analysis(conn, session_id, scope)


@app.post("/api/sessions/{session_id}/merge/preview")
async def merge_preview(
    session_id: str,
    file: UploadFile = File(...),
    scope: str | None = None,
) -> dict[str, Any]:
    """合并导入的只读预览：解析上传文件并与当前会话数据逐条比对。

    按「选手+评委+项目」识别 新增/未变/分数变更/仅存在于当前会话 四类记录，
    逐行给出新旧分数与受影响的旧裁决。不落库、不创建会话、不改变任何裁决。
    """
    raw = await file.read()
    try:
        records = parse_scores(raw)
    except CSVParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    with db.get_conn() as conn:
        stored_scope = db.get_scope(conn, session_id)
        if scope is not None:
            if scope not in SCOPES:
                raise HTTPException(status_code=400, detail=f"不支持的校准范围: {scope}")
            effective_scope = scope
        else:
            effective_scope = stored_scope or SCOPE_JUDGE
        current = db.load_scores(conn, session_id)
        decisions = db.load_decisions(conn, session_id)
        plan = build_merge_plan(current, records, decisions)
        # 受影响评分（分数变更/将删除）的裁决状态一并记入基线，确认时核对。
        decision_snapshots = {
            row["score_id"]: (
                {"use_raw": row["old_use_raw"], "reason": row["old_reason"]}
                if row["has_decision"]
                else None
            )
            for row in plan["rows"]
            if row["status"] in (STATUS_CHANGED, STATUS_REMOVED)
        }
    return {
        "scope": effective_scope,
        "scope_label": SCOPE_LABELS[effective_scope],
        "session_existed": stored_scope is not None,
        "status_labels": MERGE_STATUS_LABELS,
        "rows": plan["rows"],
        "counts": plan["counts"],
        "total_current": plan["total_current"],
        "total_incoming": plan["total_incoming"],
        "will_modify": plan["will_modify"],
        "decisions_cleared": plan["decisions_cleared"],
        "records": records,
        "basis": current_basis(current, decision_snapshots),
    }


@app.post("/api/sessions/{session_id}/merge/confirm")
def merge_confirm(session_id: str, body: MergeConfirmBody) -> dict[str, Any]:
    """确认合并：在同一个数据库事务中落库预览结果。

    分数变更/删除记录的旧裁决在事务内自动清除；未变记录及其裁决、已保存的
    批处理规则均不触碰。确认时在同一事务内先核对预览基线：除全部评分外，
    还包括受影响评分（分数变更/将删除）的裁决状态（存在性、采用原分、
    理由）；预览后这些数据被其他操作改动（含新增/修改/删除相关裁决）时
    返回 409 要求重新预览，且不写入任何合并结果。
    """
    try:
        records = normalize_incoming_records(body.records)
    except MergeValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not valid_basis_shape(body.basis):
        raise HTTPException(status_code=400, detail="basis 基线快照格式不正确，请重新预览。")
    with db.get_conn() as conn:
        current = db.load_scores(conn, session_id)
        decisions = db.load_decisions(conn, session_id)
        stored_scope = db.get_scope(conn, session_id)
        plan = build_merge_plan(current, records, decisions)
        affected_ids = {
            row["score_id"]
            for row in plan["rows"]
            if row["status"] in (STATUS_CHANGED, STATUS_REMOVED)
        }
        # 核对先于一切写入，且与写入处于同一事务；不匹配则整体放弃。
        if not basis_matches(body.basis, current, decisions, affected_ids):
            raise HTTPException(
                status_code=409,
                detail="预览后当前会话数据已变化，请重新预览后再确认合并。",
            )
        if stored_scope is None:
            db.ensure_session(conn, session_id, body.scope or SCOPE_JUDGE)
        # 全部变更随本事务一起提交；任何失败整体回滚。
        result = db.merge_scores(conn, session_id, plan["rows"])
        scope = _resolve_scope(conn, session_id, None)
        analysis = _build_analysis(conn, session_id, scope)
    analysis["merge_applied"] = {
        "counts": result["counts"],
        "decisions_cleared": result["decisions_cleared"],
        "rules_touched": 0,
    }
    return analysis


@app.patch("/api/sessions/{session_id}/scores/{score_id}/decision")
def patch_decision(session_id: str, score_id: int, body: DecisionBody) -> dict[str, Any]:
    with db.get_conn() as conn:
        try:
            db.upsert_decision(conn, session_id, score_id, body.use_raw, body.reason)
        except db.NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        scope = _resolve_scope(conn, session_id, None)
        return _build_analysis(conn, session_id, scope)


@app.post("/api/sessions/{session_id}/decisions/reset")
def reset_decisions(session_id: str) -> dict[str, Any]:
    with db.get_conn() as conn:
        if db.get_scope(conn, session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在。")
        removed = db.reset_decisions(conn, session_id)
        scope = _resolve_scope(conn, session_id, None)
        analysis = _build_analysis(conn, session_id, scope)
        analysis["reset_count"] = removed
        return analysis


@app.get("/api/sessions/{session_id}/rules")
def get_rules(session_id: str) -> dict[str, Any]:
    with db.get_conn() as conn:
        return {"rules": db.load_rules(conn, session_id)}


@app.put("/api/sessions/{session_id}/rules")
def save_rules(session_id: str, body: RuleSetBody) -> dict[str, Any]:
    """Persist a rule set without applying it to any scores."""
    with db.get_conn() as conn:
        if db.get_scope(conn, session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在，请先上传数据。")
        scope = _resolve_scope(conn, session_id, None)
        analysis = _build_analysis(conn, session_id, scope)
        if analysis["counts"]["scores"] == 0:
            raise HTTPException(status_code=400, detail="当前会话没有评分数据，无法保存规则。")
        rules = _normalize_session_rules(body, analysis)
        saved = db.replace_rules(conn, session_id, rules)
    return {"saved": saved, "rules": rules}


@app.post("/api/sessions/{session_id}/rules/preview")
def preview_rules(session_id: str, body: RuleSetBody) -> dict[str, Any]:
    """Read-only dry run: hit/miss/conflict counts and priority resolution.

    Never persists rules and never modifies decisions.
    """
    with db.get_conn() as conn:
        if db.get_scope(conn, session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在，请先上传数据。")
        scope = _resolve_scope(conn, session_id, None)
        analysis = _build_analysis(conn, session_id, scope)
        if analysis["counts"]["scores"] == 0:
            raise HTTPException(status_code=400, detail="当前会话没有评分数据，无法预览规则。")
        rules = _normalize_session_rules(body, analysis)
        preview = _rule_preview(analysis, rules)
    preview["rules_submitted"] = len(rules)
    preview["rules_saved"] = len(analysis["batch_rules"])
    return preview


@app.post("/api/sessions/{session_id}/rules/apply")
def apply_rules(session_id: str, body: RuleSetBody) -> dict[str, Any]:
    """Persist rules and write all adjudications in a single transaction."""
    with db.get_conn() as conn:
        if db.get_scope(conn, session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在，请先上传数据。")
        scope = _resolve_scope(conn, session_id, None)
        pre_analysis = _build_analysis(conn, session_id, scope)
        if pre_analysis["counts"]["scores"] == 0:
            raise HTTPException(status_code=400, detail="当前会话没有评分数据，无法应用规则。")
        rules = _normalize_session_rules(body, pre_analysis)
        preview = _rule_preview(pre_analysis, rules)
        # Everything below commits together; any failure rolls the whole batch back.
        db.replace_rules(conn, session_id, rules)
        written = db.apply_batch_decisions(conn, session_id, preview["assignments"])
        analysis = _build_analysis(conn, session_id, scope)
    analysis["rules_saved"] = len(rules)
    analysis["written"] = written
    analysis["matched"] = preview["matched"]
    analysis["unmatched"] = preview["unmatched"]
    analysis["conflicted"] = preview["conflicted"]
    analysis["changed"] = preview["would_change"]
    analysis["rule_preview"] = {
        k: preview[k]
        for k in ("total_scores", "matched", "unmatched", "conflicted", "would_change", "rules")
    }
    return analysis


@app.post("/api/sessions/{session_id}/stability")
def stability_check(session_id: str, body: StabilityBody) -> dict[str, Any]:
    """Temporary what-if run: exclude one judge/item and diff ranks/totals.

    Pure read-only computation — no scores, decisions or session state are
    modified. The client discards the result when returning to baseline.
    """
    with db.get_conn() as conn:
        if db.get_scope(conn, session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在，请先上传数据。")
        scope = _resolve_scope(conn, session_id, None)
        records = db.load_scores(conn, session_id)
        decisions = db.load_decisions(conn, session_id)
    if not records:
        raise HTTPException(status_code=404, detail="当前会话没有评分数据，无法进行实验。")
    try:
        return stability_experiment(
            records, decisions, scope, body.exclude_type, body.exclude_name.strip()
        )
    except LookupError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/sessions/{session_id}/compare")
def compare_two_contestants(session_id: str, a: str, b: str) -> dict[str, Any]:
    """Read-only head-to-head comparison, recomputed from current data.

    The comparison is derived from the same analysis pipeline on every call,
    so it always reflects the current calibration scope and manual
    decisions. Nothing is persisted and no scores are duplicated.
    """
    with db.get_conn() as conn:
        if db.get_scope(conn, session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在，请先上传数据。")
        scope = _resolve_scope(conn, session_id, None)
        analysis = _build_analysis(conn, session_id, scope)
    if analysis["counts"]["scores"] == 0:
        raise HTTPException(status_code=404, detail="当前会话没有评分数据，无法对比。")
    try:
        return compare_contestants(analysis, a, b)
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/sessions/{session_id}/common-judge")
def common_judge(session_id: str) -> dict[str, Any]:
    """同评委可比对照（只读）。

    在当前最终分上，对每对选手逐项目只取双方共同评分的评委，分别求同一
    评委集合的均分，再汇总各可比项目的差值；没有共同评委或一方缺项目的
    项目列为不可比、不按零分；全部项目不可比的选手对整对不可比。结果给出
    各项目共同评委覆盖与差值、可比领先者，并与现有全量最终总分方向对照，
    标出反向的选手对。每次请求实时重算，反映最新校准范围与逐项裁决，
    不写入评分、裁决或会话。
    """
    with db.get_conn() as conn:
        if db.get_scope(conn, session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在，请先上传数据。")
        scope = _resolve_scope(conn, session_id, None)
        analysis = _build_analysis(conn, session_id, scope)
    if analysis["counts"]["scores"] == 0:
        raise HTTPException(status_code=404, detail="当前会话没有评分数据，无法进行同评委可比对照。")
    return common_judge_comparison(analysis)


@app.get("/api/sessions/{session_id}/judge-offsets")
def judge_offsets(session_id: str) -> dict[str, Any]:
    """评委严宽差异诊断（只读，仅基于原始分）。

    仅用原始分：在每对评委共同评过的「选手 × 项目」记录上计算原始分差
    （评委甲 − 评委乙）的中位数与共评数，以共评数为权重对评委关系图
    逐连通分量做加权最小二乘拟合（各分量内偏移量均值为零，不跨分量比较）。
    无共评边的评委标为不可估计。每次请求实时重算，不受校准范围与裁决影响，
    不写入任何数据。
    """
    with db.get_conn() as conn:
        if db.get_scope(conn, session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在，请先上传数据。")
        records = db.load_scores(conn, session_id)
    if not records:
        raise HTTPException(status_code=404, detail="当前会话没有评分数据，无法进行严宽差异诊断。")
    return judge_offset_diagnosis(records)


@app.post("/api/sessions/{session_id}/backfill/preview")
def backfill_preview(session_id: str, body: BackfillBody) -> dict[str, Any]:
    """补评分派只读预览：求评委容量与单元缺口约束下的最大分派方案。

    以已有评分的「选手 × 项目」为需求单元，仅允许给该项目打过分、但尚未给
    该单元打分的评委接单。纯内存计算，**不写入评分或裁决**，不创建/修改会话。
    """
    with db.get_conn() as conn:
        if db.get_scope(conn, session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在，请先上传数据。")
        scope = _resolve_scope(conn, session_id, None)
        records = db.load_scores(conn, session_id)
    if not records:
        raise HTTPException(status_code=404, detail="当前会话没有评分数据，无法生成补评分派方案。")
    try:
        plan = plan_backfill(records, body.target, body.capacities)
    except BackfillValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    plan["scope"] = scope
    plan["scope_label"] = SCOPE_LABELS[scope]
    return plan


@app.post("/api/sessions/{session_id}/weight-thresholds")
def weight_thresholds(session_id: str, body: WeightBody) -> dict[str, Any]:
    """项目权重临界点分析（只读）。

    以当前校准范围与逐项裁决形成的**项目最终均分**为基础，令指定项目倍率
    从 0 连续变化到上限，其余项目倍率恒为 1，枚举区间内所有选手总分相等的
    临界倍率（合并同位置交点；始终并列的选手不产生交点），并给出每个临界
    点及相邻开区间中点处的竞赛名次与总分。缺失该项目的选手按 0 贡献。
    结果每次请求实时重算，不写入评分、裁决或会话。
    """
    with db.get_conn() as conn:
        if db.get_scope(conn, session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在，请先上传数据。")
        scope = _resolve_scope(conn, session_id, None)
        analysis = _build_analysis(conn, session_id, scope)
    if analysis["counts"]["scores"] == 0:
        raise HTTPException(status_code=404, detail="当前会话没有评分数据，无法分析权重临界点。")
    try:
        return weight_threshold_analysis(analysis, body.item, body.upper_bound)
    except WeightValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/sessions/{session_id}/promotion-check")
def promotion_threshold_check(session_id: str, body: PromotionBody) -> dict[str, Any]:
    """晋级门槛检查（只读）。

    用户为一个或多个项目填写最终项目均分最低线，并可选择填写最终总分线；
    基于当前校准范围与逐项裁决实时计算每位选手是否达标。缺少任一被设门槛
    项目的选手标为「数据不足」并列出缺失项目，**不按 0 分处理**。
    结果每次请求实时重算，不写入评分、裁决或会话。
    """
    with db.get_conn() as conn:
        if db.get_scope(conn, session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在，请先上传数据。")
        scope = _resolve_scope(conn, session_id, None)
        analysis = _build_analysis(conn, session_id, scope)
    if analysis["counts"]["scores"] == 0:
        raise HTTPException(status_code=404, detail="当前会话没有评分数据，无法检查晋级门槛。")
    try:
        return promotion_check(
            analysis,
            [t.model_dump() for t in body.item_thresholds],
            body.total_threshold,
        )
    except PromotionValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/sessions/{session_id}/pending-rank")
def pending_rank(session_id: str, body: PendingRankBody) -> dict[str, Any]:
    """待补评分名次边界（只读）。

    选择一个项目、目标评委人数与待补评分的最终分上下限：沿用当前校准范围与
    逐项裁决形成的已有最终分，将每位选手在该项目的评分补足至目标人数（待补
    分可独立取闭区间内任意值），重算该项目均分区间与最终总分区间（其余项目
    保持现值），并分别用「本人总分上限 vs 他人下限」「本人下限 vs 他人上限」
    求可达到的最好 / 最差竞赛名次（同分容差与主排名一致）。任一选手在该项目
    的已有评分数超过目标人数时整体拒绝。结果每次请求实时重算，不写入评分、
    裁决或会话。
    """
    with db.get_conn() as conn:
        if db.get_scope(conn, session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在，请先上传数据。")
        scope = _resolve_scope(conn, session_id, None)
        analysis = _build_analysis(conn, session_id, scope)
    if analysis["counts"]["scores"] == 0:
        raise HTTPException(status_code=404, detail="当前会话没有评分数据，无法计算名次边界。")
    try:
        return pending_rank_bounds(
            analysis, body.item, body.target, body.score_min, body.score_max
        )
    except PendingRankValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/sessions/{session_id}")
def clear_session(session_id: str) -> dict[str, Any]:
    with db.get_conn() as conn:
        existed = db.delete_session(conn, session_id)
    return {"cleared": existed}


@app.get("/api/sessions/{session_id}/export")
def export_audit(session_id: str) -> Response:
    with db.get_conn() as conn:
        if db.get_scope(conn, session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在，请先上传数据。")
        scope = _resolve_scope(conn, session_id, None)
        analysis = _build_analysis(conn, session_id, scope)
    if analysis["counts"]["scores"] == 0:
        raise HTTPException(status_code=400, detail="当前会话没有可导出的数据。")
    payload = build_audit_csv(analysis)
    headers = {"Content-Disposition": 'attachment; filename="calibration_audit.csv"'}
    return Response(content=payload, media_type="text/csv; charset=utf-8", headers=headers)


# Frontend (served last so /api routes take precedence).
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
