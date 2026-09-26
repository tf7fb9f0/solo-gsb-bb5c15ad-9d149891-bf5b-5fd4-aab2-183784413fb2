"""评分数据合并导入：按「选手+评委+项目」三元组计算差异。

上传 CSV 走的是整体替换（``replace_scores``，会清空裁决与批处理规则）；
合并导入则在保留未变记录的前提下，把新文件并入当前会话：

- ``added``     新文件有、当前会话没有 → 新增
- ``unchanged`` 两边都有且分数相同    → 保持不动（裁决/规则均不受影响）
- ``changed``   两边都有但分数不同    → 原地更新分数，旧裁决自动清除
- ``removed``   仅存在于当前会话      → 删除记录，其旧裁决一并清除

差异计算是纯函数，预览不触碰数据库；确认时由
``database.merge_scores`` 在单个事务内落库。

预览基线（``current_basis``）除评分快照外，还为「分数变更/将删除」
两类受影响评分记录裁决状态（是否存在、采用原分、理由）；确认时由
``basis_matches`` 连同评分一起核对，任何变化都判为过期预览（409），
避免预览后新增的裁决被静默清除。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

# 状态固定展示顺序：变更、新增、仅当前会话（删除）、未变。
STATUS_CHANGED = "changed"
STATUS_ADDED = "added"
STATUS_REMOVED = "removed"
STATUS_UNCHANGED = "unchanged"

STATUSES = (STATUS_CHANGED, STATUS_ADDED, STATUS_REMOVED, STATUS_UNCHANGED)
STATUS_LABELS = {
    STATUS_CHANGED: "分数变更",
    STATUS_ADDED: "新增",
    STATUS_REMOVED: "仅当前会话（将删除）",
    STATUS_UNCHANGED: "未变",
}
_STATUS_ORDER = {s: i for i, s in enumerate(STATUSES)}

# 决策动作用于预览逐行展示裁决受影响方式。
DECISION_CLEAR = "clear"   # 变更/删除：旧裁决（如有）将被清除
DECISION_KEEP = "keep"     # 未变：现有裁决保持不动
DECISION_NONE = "none"     # 该记录当前没有裁决


class MergeValidationError(ValueError):
    """合并提交的 JSON 记录不合法（返回 400）。"""


def _triple(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (row["contestant"], row["judge"], row["item"])


def _decision_info(
    decisions: Mapping[int, Any], score_id: int
) -> tuple[bool, bool | None, str]:
    """Return (exists, use_raw, reason) for a current score id."""
    verdict = decisions.get(score_id)
    if verdict is None:
        return False, None, ""
    return True, bool(verdict[0]), verdict[1] or ""


def build_merge_plan(
    current: Sequence[Mapping[str, Any]],
    incoming: Sequence[Mapping[str, Any]],
    decisions: Mapping[int, Any] | None = None,
) -> dict[str, Any]:
    """对比当前记录与新文件记录，生成逐条合并计划（纯函数）。

    current: 库内评分记录（含 id/score），决定 rows 的基础排序。
    incoming: parse_scores 解析出的新记录（无 id）。
    decisions: score_id -> 裁决，用于标记哪些行会清掉旧裁决。

    返回 {"rows": [...], "counts": {...}}；rows 按
    变更→新增→仅当前会话→未变排列，组内沿用当前行顺序 / 新文件行顺序。
    """
    decisions = decisions or {}
    current_by_key: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for rec in current:
        current_by_key[_triple(rec)] = rec
    incoming_by_key: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for rec in incoming:
        incoming_by_key[_triple(rec)] = rec

    rows: list[dict[str, Any]] = []
    counts = {s: 0 for s in STATUSES}
    decisions_cleared = 0

    # 当前会话侧：先按当前顺序产出 变更/未变/仅当前会话。
    for rec in current:
        key = _triple(rec)
        new = incoming_by_key.get(key)
        if new is None:
            status = STATUS_REMOVED
        elif float(new["score"]) == float(rec["score"]):
            status = STATUS_UNCHANGED
        else:
            status = STATUS_CHANGED
        has_decision, old_use_raw, old_reason = _decision_info(decisions, rec["id"])
        affected = status != STATUS_UNCHANGED and has_decision
        if affected:
            decisions_cleared += 1
        if status == STATUS_CHANGED or status == STATUS_REMOVED:
            decision_action = DECISION_CLEAR if affected else DECISION_NONE
        else:
            decision_action = DECISION_KEEP if has_decision else DECISION_NONE
        rows.append(
            {
                "status": status,
                "contestant": rec["contestant"],
                "judge": rec["judge"],
                "item": rec["item"],
                "score_id": rec["id"],
                "old_score": rec["score"],
                "new_score": None if new is None else float(new["score"]),
                "incoming_order": None,
                "has_decision": has_decision,
                "old_use_raw": old_use_raw,
                "old_reason": old_reason,
                "decision_action": decision_action,
            }
        )
        counts[status] += 1

    # 新增侧：沿用新文件中的出现顺序。
    for order, rec in enumerate(incoming):
        if _triple(rec) in current_by_key:
            continue
        rows.append(
            {
                "status": STATUS_ADDED,
                "contestant": rec["contestant"],
                "judge": rec["judge"],
                "item": rec["item"],
                "score_id": None,
                "old_score": None,
                "new_score": float(rec["score"]),
                "incoming_order": order,
                "has_decision": False,
                "decision_action": DECISION_NONE,
            }
        )
        counts[STATUS_ADDED] += 1

    # 稳定排序：只按状态分桶，桶内保持插入顺序
    # （当前侧=库内行序，新增侧=新文件行序）。
    rows.sort(key=lambda r: _STATUS_ORDER[r["status"]])

    return {
        "rows": rows,
        "counts": counts,
        "total_current": len(current),
        "total_incoming": len(incoming),
        "decisions_cleared": decisions_cleared,
        "will_modify": counts[STATUS_CHANGED]
        + counts[STATUS_ADDED]
        + counts[STATUS_REMOVED],
    }


def current_basis(
    records: Sequence[Mapping[str, Any]],
    decision_snapshots: Mapping[int, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """生成确认时回传的「预览基线」快照。

    每条记录含 选手/评委/项目/分数；对合并时将被清除裁决的受影响评分
    （分数变更/将删除），另附 ``decision`` 字段记录其裁决状态
    （``None`` 表示无裁决，否则为 {"use_raw", "reason"}），供确认时核对。
    """
    decision_snapshots = decision_snapshots or {}
    basis: list[dict[str, Any]] = []
    for r in records:
        entry: dict[str, Any] = {
            "contestant": r["contestant"],
            "judge": r["judge"],
            "item": r["item"],
            "score": float(r["score"]),
        }
        if r["id"] in decision_snapshots:
            snap = decision_snapshots[r["id"]]
            entry["decision"] = (
                None
                if snap is None
                else {"use_raw": bool(snap["use_raw"]), "reason": str(snap["reason"])}
            )
        basis.append(entry)
    return basis


def valid_basis_shape(raw: Any) -> bool:
    """校验客户端回传的基线快照格式（含受影响行的裁决快照字段）。"""
    if not isinstance(raw, list):
        return False
    for item in raw:
        if not (
            isinstance(item, Mapping)
            and isinstance(item.get("contestant"), str)
            and isinstance(item.get("judge"), str)
            and isinstance(item.get("item"), str)
            and isinstance(item.get("score"), (int, float))
            and not isinstance(item.get("score"), bool)
        ):
            return False
        if "decision" in item:
            snap = item["decision"]
            if snap is not None and not (
                isinstance(snap, Mapping)
                and isinstance(snap.get("use_raw"), bool)
                and isinstance(snap.get("reason"), str)
            ):
                return False
    return True


def _decision_snapshot_matches(snapshot: Any, verdict: Any) -> bool:
    """比对基线中的裁决快照与库内裁决（verdict 为 (use_raw, reason) 或 None）。"""
    if snapshot is None:
        return verdict is None
    if verdict is None or not isinstance(snapshot, Mapping):
        return False
    try:
        return (
            snapshot["use_raw"] is bool(verdict[0])
            and str(snapshot["reason"]) == (verdict[1] or "")
        )
    except (KeyError, TypeError):
        return False


def basis_matches(
    basis: Sequence[Mapping[str, Any]],
    current: Sequence[Mapping[str, Any]],
    decisions: Mapping[int, Any] | None = None,
    affected_ids: set[int] | None = None,
) -> bool:
    """核对客户端回传的基线是否仍与库内数据一致。

    逐条比对评分（选手/评委/项目/分数）；对 ``affected_ids`` 中的受影响
    评分（合并将清除其裁决的行），还要求基线携带 ``decision`` 快照且与
    库内裁决状态（存在性、采用原分、理由）完全一致——预览后裁决被新增、
    修改或删除都会判为不匹配。未受影响行不得携带 ``decision`` 字段。
    未传 ``decisions``/``affected_ids`` 时退化为仅核对评分（旧行为）。
    """
    if len(basis) != len(current):
        return False
    check_decisions = decisions is not None and affected_ids is not None
    for b, rec in zip(basis, current):
        try:
            if (
                b["contestant"] != rec["contestant"]
                or b["judge"] != rec["judge"]
                or b["item"] != rec["item"]
                or float(b["score"]) != float(rec["score"])
            ):
                return False
        except (KeyError, TypeError, ValueError):
            return False
        if check_decisions:
            affected = rec["id"] in affected_ids
            if affected != ("decision" in b):
                return False
            if affected and not _decision_snapshot_matches(
                b["decision"], decisions.get(rec["id"])
            ):
                return False
    return True


def normalize_incoming_records(raw: Any) -> list[dict[str, Any]]:
    """校验确认请求里回传的新记录（与 parse_scores 同样严格）。"""
    if not isinstance(raw, list):
        raise MergeValidationError("records 必须是数组。")
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for i, row in enumerate(raw, start=1):
        where = f"第 {i} 条记录"
        if not isinstance(row, Mapping):
            raise MergeValidationError(f"{where}必须是对象。")

        def text(field: str, label: str) -> str:
            value = row.get(field)
            if not isinstance(value, str) or not value.strip():
                raise MergeValidationError(f"{where}的「{label}」不能为空。")
            return value.strip()

        contestant = text("contestant", "选手")
        judge = text("judge", "评委")
        item = text("item", "项目")
        score_raw = row.get("score")
        if isinstance(score_raw, bool) or not isinstance(score_raw, (int, float, str)):
            raise MergeValidationError(f"{where}的分数必须是数字。")
        score_text = str(score_raw).strip()
        if not score_text:
            raise MergeValidationError(f"{where}的「分数」不能为空。")
        try:
            score = float(score_text)
        except ValueError:
            raise MergeValidationError(f"{where}的分数「{score_text}」不是数字。")
        if score != score or score in (float("inf"), float("-inf")):
            raise MergeValidationError(f"{where}的分数无效。")

        triple = (contestant, judge, item)
        if triple in seen:
            raise MergeValidationError(
                f"{where}存在重复评分（选手={contestant}，评委={judge}，项目={item}）。"
            )
        seen.add(triple)
        records.append(
            {"contestant": contestant, "judge": judge, "item": item, "score": score}
        )

    if not records:
        raise MergeValidationError("合并导入至少需要一条评分记录。")
    return records
