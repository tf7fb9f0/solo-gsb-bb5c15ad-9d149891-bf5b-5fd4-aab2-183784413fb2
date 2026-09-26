"""评委严宽差异诊断：基于原始分的评委相对偏移量加权最小二乘拟合（纯函数）。

仅使用**原始分**，与校准范围及人工裁决无关：

- 对每对评委，取两人共同评过的「选手 × 项目」记录（共评记录），
  计算原始分差（评委甲 − 评委乙）的**中位数**作为该评委对的观测分差，
  共评记录数作为拟合权重；
- 评委关系图（节点 = 评委，边 = 有共评记录的评委对）按**连通分量**分别做
  加权最小二乘拟合，使各分量内评委相对偏移量的**均值为零**
  （偏移量为分量内相对值，**不跨分量比较**）；
- 没有任何共评边的评委标记为「不可估计」，偏移量为 ``None``。

拟合模型：对每条边 (a, b)，观测分差 d_ab ≈ offset_a − offset_b，
以共评数 w_ab 为权重最小化 Σ w_ab · (d_ab − (offset_a − offset_b))²，
并施加分量内零均值约束。法方程 L·x = b（L 为加权拉普拉斯矩阵）在
固定分量内第一个评委为 0 后化为正定的约化方程组，解出后再平移到零均值。
"""
from __future__ import annotations

from collections import defaultdict
from statistics import median
from typing import Any, Mapping, Sequence

# 偏移量绝对值不超过该阈值时视为「居中」（既不偏宽也不偏严）。
OFFSET_EPS = 1e-9


def _solve_anchored(lap: list[list[float]], rhs: list[float]) -> list[float]:
    """Solve L·x = b with x[0] anchored to 0 (Gaussian elimination).

    ``lap`` is the weighted Laplacian of one connected component, so the
    reduced system (dropping row/col 0) is positive definite and partial
    pivoting is well-defined. Returns the anchored solution (x[0] == 0).
    """
    n = len(lap)
    x = [0.0] * n
    if n == 1:
        return x
    m = n - 1
    mat = [[lap[r][c] for c in range(1, n)] for r in range(1, n)]
    vec = [rhs[r] for r in range(1, n)]
    for col in range(m):
        piv = max(range(col, m), key=lambda r: abs(mat[r][col]))
        if abs(mat[piv][col]) <= 1e-12:
            raise ArithmeticError("连通分量的法方程奇异，无法拟合评委偏移量。")
        if piv != col:
            mat[col], mat[piv] = mat[piv], mat[col]
            vec[col], vec[piv] = vec[piv], vec[col]
        for r in range(col + 1, m):
            factor = mat[r][col] / mat[col][col]
            if factor == 0.0:
                continue
            for c in range(col, m):
                mat[r][c] -= factor * mat[col][c]
            vec[r] -= factor * vec[col]
    for row in range(m - 1, -1, -1):
        rest = sum(mat[row][j] * x[j + 1] for j in range(row + 1, m))
        x[row + 1] = (vec[row] - rest) / mat[row][row]
    return x


def judge_offset_diagnosis(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """评委严宽差异诊断（只读，仅基于原始分）。

    返回评委偏移量（逐连通分量、分量内均值零）、每位评委的共评覆盖量，
    以及每对评委的观测分差（共评记录原始分差的中位数）、拟合分差与残差。
    无共评边的评委 ``estimable`` 为 ``False``、``offset`` 为 ``None``。
    """
    # 单元 = (选手, 项目)；同一单元同一评委出现多条时以首条为准（与排序分歧一致）。
    judge_order: list[str] = []
    judges_seen: set[str] = set()
    units: dict[tuple[str, str], dict[str, float]] = {}
    for rec in records:
        judge = rec["judge"]
        if judge not in judges_seen:
            judges_seen.add(judge)
            judge_order.append(judge)
        unit = units.setdefault((rec["contestant"], rec["item"]), {})
        unit.setdefault(judge, float(rec["score"]))

    jidx = {j: i for i, j in enumerate(judge_order)}

    # 评委对（按下标有序）-> 共评记录上的原始分差（甲 − 乙）列表。
    pair_diffs: dict[tuple[int, int], list[float]] = defaultdict(list)
    for unit in units.values():
        idxs = sorted(jidx[j] for j in unit)
        for p, ia in enumerate(idxs):
            score_a = unit[judge_order[ia]]
            for ib in idxs[p + 1:]:
                pair_diffs[(ia, ib)].append(score_a - unit[judge_order[ib]])

    # 共评覆盖量（该评委参与的共评记录条数）与共评评委数。
    coverage = [0] * len(judge_order)
    partners = [0] * len(judge_order)
    adjacency: dict[int, list[int]] = defaultdict(list)
    for (ia, ib), diffs in pair_diffs.items():
        w = len(diffs)
        coverage[ia] += w
        coverage[ib] += w
        partners[ia] += 1
        partners[ib] += 1
        adjacency[ia].append(ib)
        adjacency[ib].append(ia)

    # 连通分量：按评委首次出现顺序发现，分量编号从 1 开始。
    comp_of: dict[int, int] = {}
    components: list[list[int]] = []
    for i in range(len(judge_order)):
        if i in comp_of or i not in adjacency:
            continue
        cid = len(components) + 1
        stack = [i]
        comp_of[i] = cid
        members: list[int] = []
        while stack:
            node = stack.pop()
            members.append(node)
            for nb in adjacency[node]:
                if nb not in comp_of:
                    comp_of[nb] = cid
                    stack.append(nb)
        members.sort()
        components.append(members)

    # 逐分量加权最小二乘：anchor 首位评委为 0 求解，再平移到分量内零均值。
    offsets: dict[int, float] = {}
    for cid, members in enumerate(components, start=1):
        k = len(members)
        pos = {node: r for r, node in enumerate(members)}
        lap = [[0.0] * k for _ in range(k)]
        rhs = [0.0] * k
        for (ia, ib), diffs in pair_diffs.items():
            if comp_of[ia] != cid:
                continue
            w = float(len(diffs))
            d = median(diffs)
            ra, rb = pos[ia], pos[ib]
            lap[ra][ra] += w
            lap[rb][rb] += w
            lap[ra][rb] -= w
            lap[rb][ra] -= w
            rhs[ra] += w * d
            rhs[rb] -= w * d
        solved = _solve_anchored(lap, rhs)
        shift = sum(solved) / k
        for node, value in zip(members, solved):
            offsets[node] = value - shift

    # 评委对结果：按（分量, 评委甲, 评委乙）排序，给出观测/拟合/残差。
    pairs_out: list[dict[str, Any]] = []
    weighted_ss = 0.0
    max_abs_residual: float | None = None
    for ia, ib in sorted(pair_diffs, key=lambda p: (comp_of[p[0]], p[0], p[1])):
        diffs = pair_diffs[(ia, ib)]
        observed = median(diffs)
        fitted = offsets[ia] - offsets[ib]
        residual = observed - fitted
        w = len(diffs)
        weighted_ss += w * residual * residual
        abs_r = abs(residual)
        if max_abs_residual is None or abs_r > max_abs_residual:
            max_abs_residual = abs_r
        pairs_out.append(
            {
                "judge_a": judge_order[ia],
                "judge_b": judge_order[ib],
                "component": comp_of[ia],
                "common_count": w,
                "observed_diff": observed,
                "fitted_diff": fitted,
                "residual": residual,
            }
        )

    judges_out: list[dict[str, Any]] = []
    for i, name in enumerate(judge_order):
        cid = comp_of.get(i)
        judges_out.append(
            {
                "judge": name,
                "estimable": cid is not None,
                "component": cid,
                "offset": offsets.get(i) if cid is not None else None,
                "coverage": coverage[i],
                "partners": partners[i],
            }
        )

    components_out: list[dict[str, Any]] = []
    for cid, members in enumerate(components, start=1):
        comp_pairs = [p for p in pair_diffs if comp_of[p[0]] == cid]
        components_out.append(
            {
                "id": cid,
                "judges": [judge_order[m] for m in members],
                "pairs": len(comp_pairs),
                "common_records": sum(len(pair_diffs[p]) for p in comp_pairs),
            }
        )

    estimable_count = sum(1 for j in judges_out if j["estimable"])
    return {
        "summary": {
            "judges": len(judge_order),
            "estimable_judges": estimable_count,
            "non_estimable_judges": len(judge_order) - estimable_count,
            "components": len(components),
            "pairs": len(pair_diffs),
            "common_records": sum(len(d) for d in pair_diffs.values()),
            "weighted_residual_ss": weighted_ss,
            "max_abs_residual": max_abs_residual,
        },
        "judges": judges_out,
        "components": components_out,
        "pairs": pairs_out,
    }
