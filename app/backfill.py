"""补评分派：在评委容量与每单元缺口约束下，求分派任务总数最大的方案。

需求单元（unit）= 当前会话中**已有评分**的「选手 × 项目」组合。设用户统一
指定的每单元目标评委人数为 ``target``，则单元缺口::

    gap(unit) = max(0, target - 该单元已有评分数)

评委 ``j`` 可以接单元 ``(contestant, item)`` 当且仅当：

1. ``j`` 给**该项目**（任意选手）打过分；并且
2. ``j`` 尚未给**该单元**（该选手的该项目）打分。

分派是一个二分图 b-匹配（最大流）问题：

    source → 评委        容量 = 评委本轮可接任务数（容量）
    评委   → 单元        容量 1，仅当该评委有资格接该单元
    单元   → sink        容量 = 单元缺口

所有容量与流量均为整数，最大流即「分派任务总数最大」的方案。

最优方案可能不止一个（并列）。为稳定输出，按固定的
**评委 → 项目 → 选手** 次序逐条候选边做「强制纳入」可行性测试：在已确定
选择的前提下，强制该边为 1 后仍能补满最优总数，则纳入，否则永久放弃该边。
最终得到的边集在所有最优方案中按该次序字典序最大，因此同一份输入永远产出
同一份结果（与并列情况无关）。

本模块是纯函数，不读写任何存储；API 层据此提供只读预览。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping, Sequence

MAX_TARGET = 1_000_000
MAX_CAPACITY = 1_000_000


class BackfillValidationError(ValueError):
    """补评分派请求参数不合法（API 层转为 400）。"""

    def __str__(self) -> str:  # noqa: D401 - Chinese message is the payload
        return self.args[0] if self.args else super().__str__()


# ---------- Max flow (Dinic, integral) ----------

class _Dinic:
    """最小够用的 Dinic 最大流；边用可变列表 [to, cap, rev_index] 表示。"""

    def __init__(self, n: int) -> None:
        self.n = n
        self.g: list[list[list[Any]]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, cap: int) -> None:
        forward = [v, cap, len(self.g[v])]
        reverse = [u, 0, len(self.g[u])]
        self.g[u].append(forward)
        self.g[v].append(reverse)

    def _bfs(self, s: int, t: int) -> list[int] | None:
        level = [-1] * self.n
        level[s] = 0
        queue = [s]
        head = 0
        while head < len(queue):
            u = queue[head]
            head += 1
            for edge in self.g[u]:
                v, cap, _ = edge
                if cap > 0 and level[v] < 0:
                    level[v] = level[u] + 1
                    queue.append(v)
        return level if level[t] >= 0 else None

    def _dfs(self, u: int, t: int, pushed: int, level: list[int], it: list[int]) -> int:
        if u == t:
            return pushed
        while it[u] < len(self.g[u]):
            edge = self.g[u][it[u]]
            v, cap, rev = edge
            if cap > 0 and level[v] == level[u] + 1:
                d = self._dfs(v, t, min(pushed, cap), level, it)
                if d:
                    edge[1] -= d
                    self.g[v][rev][1] += d
                    return d
            it[u] += 1
        return 0

    def max_flow(self, s: int, t: int, limit: int | None = None) -> int:
        flow = 0
        budget = limit if limit is not None else MAX_CAPACITY
        while budget > 0:
            level = self._bfs(s, t)
            if level is None:
                break
            it = [0] * self.n
            while budget > 0:
                pushed = self._dfs(s, t, budget, level, it)
                if not pushed:
                    break
                flow += pushed
                budget -= pushed
        return flow


def _flow_value(
    judge_index: Mapping[str, int],
    unit_index: Mapping[tuple[str, str], int],
    capacities: Mapping[str, int],
    gaps: Mapping[tuple[str, str], int],
    edges: Sequence[tuple[str, str, str]],
) -> int:
    """在给定评委容量、单元缺口与候选边上求最大流值。

    节点布局：0=source，1..m=评委，其后为单元，最后一个为 sink。
    """
    m = len(judge_index)
    k = len(unit_index)
    source = 0
    sink = 1 + m + k
    dinic = _Dinic(sink + 1)
    for judge, idx in judge_index.items():
        cap = capacities.get(judge, 0)
        if cap > 0:
            dinic.add_edge(source, 1 + idx, cap)
    for (contestant, item), idx in unit_index.items():
        gap = gaps.get((contestant, item), 0)
        if gap > 0:
            dinic.add_edge(1 + m + idx, sink, gap)
    for judge, contestant, item in edges:
        j = judge_index.get(judge)
        u = unit_index.get((contestant, item))
        if j is None or u is None:
            continue
        if capacities.get(judge, 0) > 0 and gaps.get((contestant, item), 0) > 0:
            dinic.add_edge(1 + j, 1 + m + u, 1)
    return dinic.max_flow(source, sink)


# ---------- Input validation ----------

def _coerce_int(value: Any, label: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BackfillValidationError(f"{label}必须是整数。")
    if value < 0:
        raise BackfillValidationError(f"{label}不能为负数。")
    if value > maximum:
        raise BackfillValidationError(f"{label}不能超过 {maximum}。")
    return value


def normalize_request(
    judges: Sequence[str],
    target: Any,
    capacities: Any,
) -> tuple[int, dict[str, int]]:
    """校验 API/调用方传入的目标人数与评委容量。

    未出现在 ``capacities`` 中的评委容量按 0 处理；引用会话中不存在的评委、
    空字符串键名、布尔值或负数一律拒绝。
    """
    goal = _coerce_int(target, "每单元目标评委人数", maximum=MAX_TARGET)
    if goal < 1:
        raise BackfillValidationError("每单元目标评委人数至少为 1。")

    if capacities is None:
        caps = {}
    else:
        if not isinstance(capacities, Mapping):
            raise BackfillValidationError("评委容量必须是以评委名为键的对象。")
        caps = {}
        for key, value in capacities.items():
            if not isinstance(key, str) or not key.strip():
                raise BackfillValidationError("评委名称不能为空。")
            name = key.strip()
            if name not in judges:
                raise BackfillValidationError(
                    f"评委「{name}」不存在，可选：{', '.join(judges) or '(无)'}。"
                )
            caps[name] = _coerce_int(value, f"评委「{name}」本轮可接任务数", maximum=MAX_CAPACITY)

    return goal, {judge: caps.get(judge, 0) for judge in judges}


# ---------- Core planning ----------

def plan_backfill(
    records: Sequence[Mapping[str, Any]],
    target: Any,
    capacities: Any,
) -> dict[str, Any]:
    """计算补评分派方案（纯函数，只读）。

    records: 与 ``database.load_scores`` 同构的 dict（含
    contestant/judge/item），顺序即首次出现顺序，决定评委/项目/选手的固定次序。
    target:  每单元统一目标评委人数（≥1 的整数）。
    capacities: 评委 -> 本轮可接任务数；缺失按 0。

    返回方案 dict（assignments 已按 评委→项目→选手 固定次序排好）。
    """
    # 首次出现顺序（records 来自库内 row_order，天然稳定）。
    judges: list[str] = list(dict.fromkeys(r["judge"] for r in records))
    items: list[str] = list(dict.fromkeys(r["item"] for r in records))
    contestants: list[str] = list(dict.fromkeys(r["contestant"] for r in records))
    goal, caps = normalize_request(judges, target, capacities)

    judge_pos = {j: i for i, j in enumerate(judges)}
    item_pos = {it: i for i, it in enumerate(items)}
    contestant_pos = {c: i for i, c in enumerate(contestants)}

    judges_by_item: dict[str, set[str]] = defaultdict(set)
    judges_by_unit: dict[tuple[str, str], set[str]] = defaultdict(set)
    for rec in records:
        judges_by_item[rec["item"]].add(rec["judge"])
        judges_by_unit[(rec["contestant"], rec["item"])].add(rec["judge"])

    # 需求单元 = 已有评分的选手×项目；按 项目→选手 的固定次序排列。
    unit_keys = sorted(
        judges_by_unit.keys(),
        key=lambda ci: (item_pos[ci[1]], contestant_pos[ci[0]]),
    )
    existing = {u: len(js) for u, js in judges_by_unit.items()}
    gaps = {u: max(0, goal - n) for u, n in existing.items()}

    unit_pos = {u: i for i, u in enumerate(unit_keys)}

    # 候选边（评委有资格接该单元），按 评委→项目→选手 的固定次序生成。
    all_edges: list[tuple[str, str, str]] = []
    eligible_per_unit: dict[tuple[str, str], list[str]] = {u: [] for u in unit_keys}
    for judge in judges:
        for contestant, item in unit_keys:
            scored_item = judge in judges_by_item[item]
            scored_unit = judge in judges_by_unit[(contestant, item)]
            if scored_item and not scored_unit:
                all_edges.append((judge, contestant, item))
                eligible_per_unit[(contestant, item)].append(judge)

    # 最优总数（不受并列影响）。
    optimal = _flow_value(judge_pos, unit_pos, caps, gaps, all_edges)

    # 逐条候选边做「强制纳入」测试，得到按固定次序字典序最大的最优边集。
    remaining_cap = dict(caps)
    remaining_gap = dict(gaps)
    undecided = set(all_edges)
    forced: list[tuple[str, str, str]] = []
    for edge in all_edges:
        if len(forced) == optimal:
            break
        judge, contestant, item = edge
        # 局部容量/缺口已耗尽，不必跑最大流即可排除。
        if remaining_cap[judge] <= 0 or remaining_gap[(contestant, item)] <= 0:
            undecided.discard(edge)
            continue
        trial_cap = dict(remaining_cap)
        trial_gap = dict(remaining_gap)
        trial_cap[judge] -= 1
        trial_gap[(contestant, item)] -= 1
        need = optimal - len(forced) - 1
        trial_edges = [e for e in undecided if e != edge]
        achieved = _flow_value(judge_pos, unit_pos, trial_cap, trial_gap, trial_edges)
        if achieved >= need:
            # 强制该边后仍可达最优 → 纳入。
            forced.append(edge)
            remaining_cap = trial_cap
            remaining_gap = trial_gap
        undecided.discard(edge)

    assigned_per_judge = {j: 0 for j in judges}
    assigned_per_unit: dict[tuple[str, str], int] = {u: 0 for u in unit_keys}
    for judge, contestant, item in forced:
        assigned_per_judge[judge] += 1
        assigned_per_unit[(contestant, item)] += 1

    units_out: list[dict[str, Any]] = []
    unmet_total = 0
    gap_units = 0
    fully_met = 0
    for contestant, item in unit_keys:
        n_existing = existing[(contestant, item)]
        gap = gaps[(contestant, item)]
        assigned = assigned_per_unit[(contestant, item)]
        unmet = gap - assigned
        unmet_total += unmet
        if gap > 0:
            gap_units += 1
            if unmet == 0:
                fully_met += 1
        units_out.append(
            {
                "item": item,
                "contestant": contestant,
                "existing": n_existing,
                "target": goal,
                "gap": gap,
                "assigned": assigned,
                "unmet": unmet,
                "eligible_judges": eligible_per_unit[(contestant, item)],
                "eligible_count": len(eligible_per_unit[(contestant, item)]),
            }
        )

    return {
        "target": goal,
        "judges": [
            {
                "judge": judge,
                "capacity": caps[judge],
                "assigned": assigned_per_judge[judge],
                "remaining": caps[judge] - assigned_per_judge[judge],
            }
            for judge in judges
        ],
        "units": units_out,
        "assignments": [
            {"judge": judge, "contestant": contestant, "item": item}
            for judge, contestant, item in forced
        ],
        "summary": {
            "judges": len(judges),
            "units": len(unit_keys),
            "gap_units": gap_units,
            "fully_met_units": fully_met,
            "total_gap": sum(gaps.values()),
            "assigned_total": len(forced),
            "unmet_total": unmet_total,
            "capacity_total": sum(caps.values()),
            "eligible_edges": len(all_edges),
            "optimal_total": optimal,
        },
        "order": {"judges": judges, "items": items, "contestants": contestants},
    }
