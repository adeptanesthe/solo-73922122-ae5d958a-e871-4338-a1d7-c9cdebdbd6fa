# -*- coding: utf-8 -*-
"""段落复序求解器（排版故障恢复）。

问题定义
========
技术编辑拿到被排版故障打散的稿件（4~8 段，每段 20~400 字），需要恢复
可供校对的段落顺序。编辑可以提供的线索：

* 首段与末段（必须是不同的段落）；
* 至多两组“紧接”标记：(a, b) 表示 b 必须紧跟在 a 之后；
* 至多两组“先于”标记：(a, b) 表示 a 必须排在 b 之前（不要求相邻）。

算法
====
1. 字符 2~3 元 TF-IDF **只在这批段落上拟合**（每次请求独立 fit，
   不复用文章语料模型，也不让这批段落之外的文本影响特征空间）；
2. 以段落两两余弦相似度为衔接得分，枚举全部满足标记约束的排列
   （首末固定、每段恰用一次），取“相邻段落余弦相似度之和”的最高分；
3. 同分按原输入序号序列的字典序取最小（枚举本身按字典序进行，
   严格更优才替换，因此同分自然保留序号序列最小者）；
4. 约束冲突、或全部段间相似度为零时，只说明原因，不给出无依据的顺序。
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

MIN_PARAGRAPHS = 4
MAX_PARAGRAPHS = 8
MIN_CHARS = 20
MAX_CHARS = 400
MAX_ADJACENT_MARKS = 2
MAX_BEFORE_MARKS = 2

# 分数比较容差：浮点噪声量级远小于它，真实分差远大于它
_EPS = 1e-9


class ReorderInputError(ValueError):
    """输入形状不合法（段落数量/字数、标记数量、序号越界等，对应 HTTP 422）。"""


@dataclass
class ReorderResult:
    ok: bool
    order: list[int] = field(default_factory=list)  # 原输入序号（0 基）按新顺序排列
    links: list[dict] = field(default_factory=list)  # [{from, to, score, marked}]
    total_score: float = 0.0
    feature_count: int = 0
    reason: str | None = None  # 无法给出顺序时的原因说明


def _label(i: int) -> str:
    return f"段{i + 1}"


# ---------------- 输入形状校验 ----------------


def validate_inputs(
    paragraphs: list[str],
    first: int,
    last: int,
    adjacent: list[tuple[int, int]],
    before: list[tuple[int, int]],
) -> list[str]:
    """校验输入形状，返回去除首尾空白后的段落列表；不合法抛 ReorderInputError。"""
    n = len(paragraphs)
    if not (MIN_PARAGRAPHS <= n <= MAX_PARAGRAPHS):
        raise ReorderInputError(
            f"段落数量须为 {MIN_PARAGRAPHS}~{MAX_PARAGRAPHS} 段，当前为 {n} 段"
        )
    cleaned = [(p or "").strip() for p in paragraphs]
    for i, p in enumerate(cleaned):
        if not p:
            raise ReorderInputError(f"{_label(i)}为空，请粘贴完整段落")
        if len(p) < MIN_CHARS:
            raise ReorderInputError(
                f"{_label(i)}只有 {len(p)} 字，少于下限 {MIN_CHARS} 字"
            )
        if len(p) > MAX_CHARS:
            raise ReorderInputError(
                f"{_label(i)}有 {len(p)} 字，超过上限 {MAX_CHARS} 字"
            )

    for name, idx in (("首段", first), ("末段", last)):
        if not isinstance(idx, int) or not (0 <= idx < n):
            raise ReorderInputError(f"{name}序号无效，应在 1~{n} 之间")
    if first == last:
        raise ReorderInputError("首段与末段必须是不同的段落")

    if len(adjacent) > MAX_ADJACENT_MARKS:
        raise ReorderInputError(f"“紧接”标记最多 {MAX_ADJACENT_MARKS} 组，当前 {len(adjacent)} 组")
    if len(before) > MAX_BEFORE_MARKS:
        raise ReorderInputError(f"“先于”标记最多 {MAX_BEFORE_MARKS} 组，当前 {len(before)} 组")

    for kind, pairs in (("紧接", adjacent), ("先于", before)):
        for a, b in pairs:
            if not (0 <= a < n) or not (0 <= b < n):
                raise ReorderInputError(f"“{kind}”标记引用了无效段落序号（应在 1~{n} 之间）")
            if a == b:
                raise ReorderInputError(f"“{kind}”标记不能指向同一段落（{_label(a)}）")
    return cleaned


# ---------------- 约束冲突诊断 ----------------


def _find_cycle(edges: list[tuple[int, int]]) -> list[int] | None:
    """在有向图中找一个环，返回环节点序列（首尾相同）；无环返回 None。"""
    adj: dict[int, list[int]] = {}
    for a, b in edges:
        adj.setdefault(a, []).append(b)
    color: dict[int, int] = {}  # 0=未访问 1=在栈上 2=已完成
    stack: list[int] = []

    def dfs(u: int) -> list[int] | None:
        color[u] = 1
        stack.append(u)
        for v in adj.get(u, []):
            c = color.get(v, 0)
            if c == 1:
                return stack[stack.index(v):] + [v]
            if c == 0:
                r = dfs(v)
                if r:
                    return r
        stack.pop()
        color[u] = 2
        return None

    for node in list(adj):
        if color.get(node, 0) == 0:
            r = dfs(node)
            if r:
                return r
    return None


def diagnose_conflicts(
    n: int,
    first: int,
    last: int,
    adjacent: list[tuple[int, int]],
    before: list[tuple[int, int]],
) -> list[str]:
    """对形状合法的标记做可满足性诊断，返回具体冲突说明列表（空列表=未发现冲突）。"""
    problems: list[str] = []

    # 1) 紧接：同一前驱/同一后继被占用两次
    src: dict[int, int] = {}
    for a, b in adjacent:
        if a in src and src[a] != b:
            problems.append(
                f"{_label(a)}被指定紧接{_label(src[a])}和{_label(b)}两个不同段落，"
                "一个段落之后只能紧接一个段落"
            )
        src[a] = b
    tgt: dict[int, int] = {}
    for a, b in adjacent:
        if b in tgt and tgt[b] != a:
            problems.append(
                f"{_label(b)}被指定同时紧接在{_label(tgt[b])}和{_label(a)}之后，"
                "一个段落之前只能紧接一个段落"
            )
        tgt[b] = a

    # 2) 紧接与首末段的矛盾
    for a, b in adjacent:
        if a == last:
            problems.append(f"末段（{_label(a)}）之后不能再紧接任何段落")
        if b == first:
            problems.append(f"首段（{_label(b)}）之前不能再有任何段落紧接它")

    # 3) 先于与首末段的矛盾
    for a, b in before:
        if a == last:
            problems.append(f"末段（{_label(a)}）不可能先于{_label(b)}，它之后没有段落")
        if b == first:
            problems.append(f"首段（{_label(b)}）不可能晚于{_label(a)}，它之前没有段落")

    # 4) 紧接关系自身成环
    adj_cycle = _find_cycle(adjacent)
    if adj_cycle:
        problems.append(
            "“紧接”标记构成回环：" + " → ".join(_label(i) for i in adj_cycle) + "，无法排成一行"
        )

    # 5) 紧接 + 先于混合后的先后关系成环（与纯紧接回环相同则不重复报告）
    cyc = _find_cycle(list(adjacent) + list(before))
    if cyc and cyc != adj_cycle:
        problems.append(
            "标记合起来构成循环：" + " → ".join(_label(i) for i in cyc) + "，先后关系互相矛盾"
        )

    # 6) 首末段被同一紧接链锁住：链含首段与末段但长度 < n，末段到不了末尾
    if not problems:
        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for a, b in adjacent:
            parent[find(a)] = find(b)
        if find(first) == find(last):
            chain_len = sum(1 for i in range(n) if find(i) == find(first))
            if chain_len < n:
                problems.append(
                    f"首段（{_label(first)}）与末段（{_label(last)}）被“紧接”标记锁进"
                    f"同一条长度为 {chain_len} 的链，但全文共 {n} 段，"
                    "首段固定在开头时末段无法落到末尾"
                )
    return problems


# ---------------- 求解 ----------------


def _fit_similarity(paragraphs: list[str]):
    """只在这批段落上拟合字符 2~3 元 TF-IDF，返回 (相似度矩阵, 特征数)。"""
    vectorizer = TfidfVectorizer(
        analyzer="char",
        ngram_range=(2, 3),
        min_df=1,
        sublinear_tf=True,
        norm="l2",
    )
    matrix = vectorizer.fit_transform(paragraphs)
    sims = cosine_similarity(matrix)
    return sims, len(vectorizer.vocabulary_)


def solve_reorder(
    paragraphs: list[str],
    first: int,
    last: int,
    adjacent: list[tuple[int, int]] | None = None,
    before: list[tuple[int, int]] | None = None,
) -> ReorderResult:
    """求满足全部标记、每段恰用一次的最高衔接得分顺序。"""
    adjacent = [tuple(p) for p in (adjacent or [])]
    before = [tuple(p) for p in (before or [])]
    cleaned = validate_inputs(paragraphs, first, last, adjacent, before)
    n = len(cleaned)

    # ---- 约束冲突：只说明原因，不给顺序 ----
    problems = diagnose_conflicts(n, first, last, adjacent, before)
    if problems:
        return ReorderResult(ok=False, reason="；".join(problems))

    # ---- 只在这批段落上拟合 TF-IDF ----
    sims, feature_count = _fit_similarity(cleaned)

    # ---- 全部段间相似度为零：没有任何衔接依据 ----
    off_diag = [
        float(sims[i, j]) for i in range(n) for j in range(n) if i != j
    ]
    if all(s <= 0.0 for s in off_diag):
        return ReorderResult(
            ok=False,
            feature_count=feature_count,
            reason=(
                "全部段间余弦相似度均为零：这些段落没有任何共同的字符 2~3 元特征，"
                "相似度无法为排序提供任何依据，因此不给出顺序"
            ),
        )

    # ---- 枚举全部满足约束的排列（首末固定，每段恰用一次）----
    middle = [i for i in range(n) if i != first and i != last]
    best_order: tuple[int, ...] | None = None
    best_score = 0.0
    for perm in itertools.permutations(middle):  # 字典序生成
        order = (first,) + perm + (last,)
        pos = {p: i for i, p in enumerate(order)}
        if any(pos[b] != pos[a] + 1 for a, b in adjacent):
            continue
        if any(pos[a] >= pos[b] for a, b in before):
            continue
        score = 0.0
        for i in range(n - 1):
            score += float(sims[order[i], order[i + 1]])
        # 严格更优才替换：同分保留字典序更小的原输入序号序列
        if best_order is None or score > best_score + _EPS:
            best_order = order
            best_score = score

    if best_order is None:
        return ReorderResult(
            ok=False,
            feature_count=feature_count,
            reason=(
                "不存在同时满足首段、末段与全部“紧接/先于”标记的排列，"
                "请检查标记之间是否互相矛盾"
            ),
        )

    if best_score <= _EPS:
        return ReorderResult(
            ok=False,
            feature_count=feature_count,
            reason=(
                "在全部标记约束下，任何可行顺序的相邻相似度之和都为零"
                "（存在相似度的段落对被标记隔开），没有依据给出顺序"
            ),
        )

    marked = set(adjacent)
    links = [
        {
            "from": best_order[i],
            "to": best_order[i + 1],
            "score": round(float(sims[best_order[i], best_order[i + 1]]), 6),
            "marked": (best_order[i], best_order[i + 1]) in marked,
        }
        for i in range(n - 1)
    ]
    return ReorderResult(
        ok=True,
        order=list(best_order),
        links=links,
        total_score=round(best_score, 6),
        feature_count=feature_count,
    )
