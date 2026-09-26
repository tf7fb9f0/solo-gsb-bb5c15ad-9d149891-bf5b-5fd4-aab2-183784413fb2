"""项目权重临界点分析（只读纯函数）。

在当前校准范围与逐项裁决形成的**项目最终均分**基础上，选定一个项目，令其
倍率 w 从 0 连续变化到上限（1～5），其余项目倍率恒为 1。每位选手的总分即
关于 w 的仿射线性函数：

    总分_c(w) = 该项目最终均分_c × w + (最终总分_c − 该项目最终均分_c)
    缺失该项目的选手：斜率为 0（按 0 贡献），截距即其最终总分。

枚举两两选手直线在闭区间 [0, 上限] 内的交点（临界倍率）。斜率与截距按
浮点计算结果**精确比较**：斜率完全相等才视为平行，斜率与截距都完全相等
（两线确实重合）才视为始终并列——不使用随分数尺度放大的相对容差，否则
大数总分下真实的斜率差会被判成平行、不同的截距会被判成始终并列，可表示
且落在区间内的真实交点就被漏报。

交点的端点吸附、同位置归并与区间切分共用 1e-12 级相对容差（约机器精度
量级）：同一数学交点因浮点运算产生的微小偏差仍归为一处；但可表示且真实
不同的交点——哪怕相邻间距只有 1e-10 量级——分别列出，并在每对相邻
临界点（含端点 0、上限）构成的开区间中点给出名次快照。每个临界点的
并列组只含在该点真实相等的选手；并列组的名次按组内均值整体排列（吸收
总分的浮点噪声），行内总分仍展示各自的仿射计算值。对每个临界点给出该
倍率下的竞赛名次、总分与交点处并列的选手。

整个计算只读，不触碰评分、裁决或会话；评分、裁决或校准范围变化后由调用
方重新计算。
"""
from __future__ import annotations

from typing import Any, Mapping

from .scoring import SCOPE_LABELS, competition_rank

WEIGHT_LOWER = 0.0
WEIGHT_UPPER_MIN = 1.0
WEIGHT_UPPER_MAX = 5.0

# 交点归并与端点吸附共用的相对容差（约机器精度量级）：两个倍率 w1、w2 仅当
# |w1 − w2| ≤ _TOL_REL × max(|w1|, |w2|) 时视为同一点。量级只够吸收同一数学
# 交点因浮点运算产生的微小偏差：可表示且真实不同的交点（哪怕相邻间距只有
# 1e-10 量级）必须分别列出，不得合并——否则近邻真实交点会被并为一处，在
# 该点并不相等的选手也会被错并入交点并列组。
_TOL_REL = 1e-12


class WeightValidationError(ValueError):
    """请求参数不合法（项目不存在、倍率上限越界等）。"""


class _DSU:
    """极简并查集：用于把同位置交点合并为并列组。"""

    def __init__(self) -> None:
        self.parent: dict[Any, Any] = {}

    def find(self, x: Any) -> Any:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: Any, b: Any) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # 字典序较小者作根，保证输出稳定。
            if str(ra) < str(rb):
                self.parent[rb] = ra
            else:
                self.parent[ra] = rb

    def groups(self) -> list[list]:
        out: dict[Any, list[Any]] = {}
        for x in list(self.parent):
            out.setdefault(self.find(x), []).append(x)
        return [sorted(members, key=str) for members in out.values()]


def weight_threshold_analysis(
    analysis: Mapping[str, Any],
    item: str,
    upper_bound: float,
) -> dict[str, Any]:
    """计算单个项目倍率连续变化时的全部总分相等临界点与区间中点名次。

    ``analysis`` 为 :func:`app.scoring.analyze` 的输出（只使用其中的
    ``scope``、``items`` 与每位选手的 ``eff_total`` / 逐项目 ``eff_avg``）。
    """
    if isinstance(item, str):
        item = item.strip()
    if not item:
        raise WeightValidationError("请选择要分析的项目。")
    if (
        not isinstance(upper_bound, (int, float))
        or isinstance(upper_bound, bool)
        or upper_bound != upper_bound  # NaN
    ):
        raise WeightValidationError("倍率上限必须是 1～5 之间的数字。")
    upper_bound = float(upper_bound)
    if not (WEIGHT_UPPER_MIN <= upper_bound <= WEIGHT_UPPER_MAX):
        raise WeightValidationError("倍率上限必须在 1～5 之间。")

    items = list(analysis["items"])
    if item not in items:
        avail = "、".join(items) if items else "（当前没有项目）"
        raise WeightValidationError(f"项目「{item}」不存在，可选：{avail}。")

    blocks = list(analysis["contestants"])
    names = [b["contestant"] for b in blocks]
    # 总分_c(w) = slope_c * w + intercept_c。
    slope: dict[str, float] = {}
    intercept: dict[str, float] = {}
    has_item: dict[str, bool] = {}
    for b in blocks:
        name = b["contestant"]
        ib = next((x for x in b["items"] if x["item"] == item), None)
        d = float(ib["eff_avg"]) if ib is not None else 0.0  # 缺失项目按 0 贡献
        slope[name] = d
        intercept[name] = float(b["eff_total"]) - d
        has_item[name] = ib is not None

    # 同一点判定（相对容差）：只吸收同一数学交点的浮点计算偏差。
    def same_point(w1: float, w2: float) -> bool:
        return abs(w1 - w2) <= _TOL_REL * max(abs(w1), abs(w2))

    # 平行判定对斜率的浮点计算结果精确比较：完全相等才算平行。若按随分数
    # 尺度放大的相对容差判定，大数总分下真实的斜率差（如 1e12 与 1e12+0.5）
    # 会被容差吞没，可表示且落在区间内的真实交点就被漏报。
    def parallel(i: str, j: str) -> bool:
        return slope[i] == slope[j]

    # 始终并列（直线确实重合）：斜率与截距的浮点计算结果都完全相等——这些
    # 选手在任何倍率下都并列，按需求不产生交点，仅在汇总中提示。截距不同
    # 的平行线永不相交，即使截距差相对总分尺度很小也不得误判为始终并列。
    always_dsu = _DSU()
    for x, i in enumerate(names):
        for j in names[x + 1:]:
            if parallel(i, j) and intercept[i] == intercept[j]:
                always_dsu.union(i, j)
    always_tied = sorted(
        (g for g in always_dsu.groups() if len(g) >= 2),
        key=lambda g: (-intercept[g[0]], "、".join(g)),
    )

    # 枚举所有非平行选手对的交点 w。数学上恰为 0 的交点（截距完全相等）计算
    # 结果恰为 0.0，故负值交点一定真实落在区间外，直接排除；略超出上限的交点
    # 只在上限的相对容差内才钳到上限，视为端点处相交。
    crossings: list[tuple[float, str, str]] = []
    for x, i in enumerate(names):
        for j in names[x + 1:]:
            if parallel(i, j):
                continue
            w0 = (intercept[j] - intercept[i]) / (slope[i] - slope[j])
            if w0 < 0.0 or w0 > upper_bound + _TOL_REL * max(upper_bound, abs(w0)):
                continue
            crossings.append((min(w0, upper_bound), i, j))
    crossings.sort(key=lambda t: (t[0], t[1], t[2]))

    # 同位置交点顺序聚类：相对容差只吸收同一数学交点的浮点计算偏差，可表示
    # 且真实不同的近邻交点各自成类、分别列出；类内用并查集合并选手，因此每个
    # 临界点的并列组只含在该点真实相等的选手。同位置的数学根只有浮点误差级
    # 差异，直接以首条交点为代表（类中若含恰在端点的交点则以端点为代表），
    # 避免平均后轻微漂移离开端点或把不同交点越并越宽。
    clusters: list[dict[str, Any]] = []
    for w0, i, j in crossings:
        if clusters and same_point(w0, clusters[-1]["w"]):
            cluster = clusters[-1]
        else:
            cluster = {"w": w0, "dsu": _DSU(), "at_start": False, "at_end": False}
            clusters.append(cluster)
        cluster["dsu"].union(i, j)
        cluster["at_start"] = cluster["at_start"] or w0 == WEIGHT_LOWER
        cluster["at_end"] = cluster["at_end"] or w0 == upper_bound
    for cluster in clusters:
        if cluster["at_start"]:
            cluster["w"] = WEIGHT_LOWER  # 与端点分隔点使用完全相同的浮点键
        elif cluster["at_end"]:
            cluster["w"] = upper_bound

    def boundary_flag(w: float) -> str | None:
        if w == WEIGHT_LOWER:
            return "start"
        if w == upper_bound:
            return "end"
        return None

    # 临界点（带并列组）；端点 0 / 上限即使无并列也作为区间分隔点参与中点计算。
    cut_points: list[float] = [WEIGHT_LOWER, upper_bound]
    tie_at: dict[float, list[list[str]]] = {}
    for cluster in clusters:
        w = cluster["w"]
        groups = sorted(
            (g for g in cluster["dsu"].groups() if len(g) >= 2),
            key=lambda g: (
                -sum(intercept[m] + slope[m] * w for m in g) / len(g),
                "、".join(g),
            ),
        )
        if w not in cut_points:
            cut_points.append(w)
        if groups:
            tie_at.setdefault(w, [])
            for g in groups:
                if g not in tie_at[w]:
                    tie_at[w].append(g)
    cut_points = sorted(set(cut_points))

    def ranking_at(w: float, tie_groups: list[list[str]] | None = None) -> list[dict[str, Any]]:
        totals = {n: intercept[n] + slope[n] * w for n in names}
        tied_members = {m for g in tie_groups or () for m in g}
        # 交点并列组在该倍率下的总分数学上相等，浮点计算可能留下微小差异：
        # 名次按组内均值整体排列，保证同组选手名次一致（竞赛排名 1224）；
        # 行内展示的总分仍是各自的仿射计算值。
        rank_totals = dict(totals)
        for g in tie_groups or ():
            if len(g) >= 2:
                avg = sum(totals[m] for m in g) / len(g)
                for m in g:
                    rank_totals[m] = avg
        ranks = competition_rank(rank_totals.items())
        rows = [
            {
                "contestant": n,
                "rank": ranks[n],
                "total": totals[n],
                "item_avg": slope[n],
                "has_item": has_item[n],
                "tied": n in tied_members,
            }
            for n in names
        ]
        rows.sort(key=lambda r: (r["rank"], r["contestant"]))
        return rows

    entries: list[dict[str, Any]] = []
    critical_count = 0
    for idx, w in enumerate(cut_points):
        groups = tie_at.get(w)
        if groups:
            entries.append(
                {
                    "kind": "critical",
                    "w": w,
                    "boundary": boundary_flag(w),
                    "ties": groups,
                    "ranking": ranking_at(w, groups),
                }
            )
            critical_count += 1
        if idx + 1 < len(cut_points):
            w_next = cut_points[idx + 1]
            mid = (w + w_next) / 2.0
            # 每对相邻切分点之间给出名次快照（浮点可表示中点时）。
            if w < mid < w_next:
                entries.append(
                    {
                        "kind": "midpoint",
                        "w": mid,
                        "left": w,
                        "right": w_next,
                        "ranking": ranking_at(mid),
                    }
                )

    scope = analysis.get("scope", "")
    return {
        "scope": scope,
        "scope_label": analysis.get("scope_label") or SCOPE_LABELS.get(scope, scope),
        "item": item,
        "lower": WEIGHT_LOWER,
        "upper_bound": upper_bound,
        "contestants_missing_item": [n for n in names if not has_item[n]],
        "always_tied_groups": always_tied,
        "summary": {
            "contestants": len(names),
            "crossing_pairs": len(crossings),
            "critical_points": critical_count,
            "interval_midpoints": sum(1 for e in entries if e["kind"] == "midpoint"),
        },
        "entries": entries,
    }
