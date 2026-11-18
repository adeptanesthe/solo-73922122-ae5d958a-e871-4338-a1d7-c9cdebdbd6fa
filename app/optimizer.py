# -*- coding: utf-8 -*-
"""队列全局最优求解器。

问题定义
========
从 30 篇文章中选出 6 篇放入位置 1..6，要求：

1. 每个主题的入选篇数精确等于给定配额（6 个配额之和恒为 6）；
2. 同一作者在队列中不得重复；
3. 已揭示前缀（位置 1..k）永久固定，只能在其后的位置重新优化；
4. 被排除的文章不可入选；
5. 最大化目标  Σ_{pos=1..6} (7 - pos) * relevance(article_at_pos)。

关键结论
--------
* 给定一组入选文章，按相关度从高到低排列（重排不等式）即为最优排列；
  但存在固定前缀时，前缀不参与重排，只对剩余位置上的候选做降序排列。
* 数据规模极小（每主题最多 5 篇候选），用“逐主题枚举组合 + 分支定界”
  即可精确求出全局最优，无需启发式近似。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .articles_data import ARTICLES, TOPICS

POSITION_WEIGHTS = {pos: 7 - pos for pos in range(1, 7)}  # 位置 1..6 -> 权重 6..1


@dataclass
class SolveResult:
    feasible: bool
    queue: list[dict] = field(default_factory=list)  # 长度 6，元素含 article_id/topic/author/relevance/position
    score: float = 0.0
    reason: str | None = None  # 不可行时的原因说明


def _recompute_topic_quotas(    quotas: dict[str, int], prefix: list[dict]
) -> tuple[dict[str, int], str | None]:
    """扣除已揭示前缀占用的主题配额。返回 (剩余配额, 错误原因)。"""
    remaining = dict(quotas)
    for item in prefix:
        t = item["topic"]
        if t not in remaining:
            return remaining, f"已揭示文章的主题 {t} 不在当前主题集合中"
        remaining[t] -= 1
        if remaining[t] < 0:
            return remaining, f"主题 {t} 的配额不足以容纳已揭示前缀"
    return remaining, None


def solve_queue(
    relevance: dict[int, float],
    quotas: dict[str, int],
    prefix: list[dict] | None = None,
    excluded_ids: set[int] | None = None,
) -> SolveResult:
    """求满足全部约束的全局最优队列。

    参数
    ----
    relevance: {文章id: 兴趣余弦相关度}
    quotas: {主题: 该主题精确入选篇数}，总和须为 6
    prefix: 已揭示前缀，元素至少含 article_id/topic/author，按位置顺序
    excluded_ids: 被排除的文章 id 集合
    """
    prefix = prefix or []
    excluded_ids = set(excluded_ids or set())

    # ---- 输入校验 ----
    if sum(quotas.values()) != 6:
        return SolveResult(False, reason="六个主题配额之和必须等于 6")
    if any(q < 0 for q in quotas.values()):
        return SolveResult(False, reason="主题配额不能为负数")
    if len(prefix) > 6:
        return SolveResult(False, reason="已揭示前缀长度不能超过 6")

    # ---- 前缀合法性：排除集不得命中已揭示文章 ----
    prefix_ids = [p["article_id"] for p in prefix]
    conflict = excluded_ids.intersection(prefix_ids)
    if conflict:
        return SolveResult(False, reason=f"不能排除已揭示文章: {sorted(conflict)}")
    if len(set(prefix_ids)) != len(prefix_ids):
        return SolveResult(False, reason="已揭示前缀内部存在重复文章")
    prefix_authors = [p["author"] for p in prefix]
    if len(set(prefix_authors)) != len(prefix_authors):
        return SolveResult(False, reason="已揭示前缀内部存在重复作者")

    remaining_quotas, err = _recompute_topic_quotas(quotas, prefix)
    if err:
        return SolveResult(False, reason=err)

    free_slots = 6 - len(prefix)
    if sum(remaining_quotas.values()) != free_slots:
        return SolveResult(False, reason="剩余配额与未揭示位置数不一致")

    # ---- 按主题构造可选候选（排除已用文章/作者/排除集）----
    used_authors = set(prefix_authors)
    used_ids = set(prefix_ids)
    candidates: dict[str, list[dict]] = {}
    for topic in TOPICS:
        cands = []
        for a in ARTICLES:
            if a["topic"] != topic:
                continue
            if a["id"] in used_ids or a["id"] in excluded_ids:
                continue
            if a["author"] in used_authors:
                continue
            cands.append(
                {
                    "article_id": a["id"],
                    "topic": topic,
                    "author": a["author"],
                    "relevance": float(relevance.get(a["id"], 0.0)),
                }
            )
        # 相关度降序、id 升序，保证枚举与平局处理确定
        cands.sort(key=lambda c: (-c["relevance"], c["article_id"]))
        candidates[topic] = cands
        if len(cands) < remaining_quotas[topic]:
            return SolveResult(
                False,
                reason=(
                    f"主题 {topic} 需要 {remaining_quotas[topic]} 篇，"
                    f"但满足排除/作者约束的候选只有 {len(cands)} 篇"
                ),
            )

    # ---- 前缀部分的固定得分（位置权重不变）----
    prefix_score = sum(
        POSITION_WEIGHTS[pos] * float(p["relevance"])
        for pos, p in enumerate(prefix, start=1)
    )

    if free_slots == 0:
        queue = [
            {
                "article_id": p["article_id"],
                "topic": p["topic"],
                "author": p["author"],
                "relevance": float(p["relevance"]),
                "position": pos,
            }
            for pos, p in enumerate(prefix, start=1)
        ]
        return SolveResult(True, queue=queue, score=prefix_score)

    # 未揭示位置的权重（位置 k+1 .. 6），降序：6-k, 6-k-1, ..., 1
    free_weights = [POSITION_WEIGHTS[pos] for pos in range(len(prefix) + 1, 7)]

    # 参与分配的主题（剩余配额 > 0），按配额降序、主题名升序，先搜约束最紧的主题
    active_topics = sorted(
        [t for t in TOPICS if remaining_quotas[t] > 0],
        key=lambda t: (-remaining_quotas[t], t),
    )

    # 每个主题候选按相关度降序；松弛上界用“该主题前 q 个最大相关度”
    top_q = {
        t: [c["relevance"] for c in candidates[t][: remaining_quotas[t]]]
        for t in active_topics
    }

    best_score: float | None = None
    best_selection: list[dict] | None = None
    best_key: tuple | None = None

    # DFS：逐主题选 q 篇，同时保证作者不重复
    def dfs(
        idx: int,
        chosen: list[dict],
        authors: set[str],
    ) -> None:
        nonlocal best_score, best_selection, best_key

        # ---- 分支定界：合法且较紧的乐观上界 ----
        # 汇总后续每个主题忽略作者冲突时的 top-q 相关度，整体降序后与
        # 剩余位置的降序权重配对（重排不等式给出该松弛问题的最优值）。
        # 忽略跨主题作者冲突只会高估真实最优，因此上界永远不低于真实值。
        remaining_count = free_slots - len(chosen)
        if remaining_count > 0:
            relaxed = []
            for j in range(idx, len(active_topics)):
                relaxed.extend(top_q[active_topics[j]])
            relaxed.sort(reverse=True)
            ub_weights = sorted(free_weights, reverse=True)[:remaining_count]
            # 已选部分的乐观值：把它们按降序放到当前已占用的最高权重位置上
            chosen_rels = sorted((c["relevance"] for c in chosen), reverse=True)
            used_weights = sorted(free_weights, reverse=True)[: len(chosen)]
            chosen_ub = sum(w * r for w, r in zip(used_weights, chosen_rels))
            upper_bound = (
                prefix_score
                + chosen_ub
                + sum(w * r for w, r in zip(ub_weights, relaxed))
            )
            if best_score is not None and upper_bound < best_score - 1e-12:
                return

        if idx == len(active_topics):
            if len(chosen) != free_slots:
                return
            # 重排不等式：入选文章按相关度降序放到权重降序的位置上即最优排列
            ordered = sorted(chosen, key=lambda c: (-c["relevance"], c["article_id"]))
            score = sum(
                free_weights[i] * ordered[i]["relevance"] for i in range(free_slots)
            )
            total = prefix_score + score
            key = tuple(c["article_id"] for c in ordered)
            if (
                best_score is None
                or total > best_score + 1e-12
                or (abs(total - best_score) <= 1e-12 and key < best_key)
            ):
                best_score = total
                best_selection = ordered
                best_key = key
            return

        topic = active_topics[idx]
        need = remaining_quotas[topic]
        cands = candidates[topic]

        def pick(start: int, picked: list[dict], picked_authors: set[str]) -> None:
            if len(picked) == need:
                dfs(idx + 1, chosen + picked, authors | picked_authors)
                return
            # 剪枝：剩余候选不够凑齐
            for i in range(start, len(cands)):
                c = cands[i]
                if c["author"] in authors or c["author"] in picked_authors:
                    continue
                if len(cands) - i < need - len(picked):
                    break
                pick(i + 1, picked + [c], picked_authors | {c["author"]})

        pick(0, [], set())

    dfs(0, [], set())

    if best_selection is None:
        return SolveResult(False, reason="作者不重复约束下不存在可行组合")

    # ---- 组装完整队列：前缀 + 最优自由排列 ----
    queue: list[dict] = []
    for pos, p in enumerate(prefix, start=1):
        queue.append(
            {
                "article_id": p["article_id"],
                "topic": p["topic"],
                "author": p["author"],
                "relevance": float(p["relevance"]),
                "position": pos,
            }
        )
    for i, c in enumerate(best_selection):
        pos = len(prefix) + 1 + i
        queue.append(
            {
                "article_id": c["article_id"],
                "topic": c["topic"],
                "author": c["author"],
                "relevance": c["relevance"],
                "position": pos,
            }
        )

    return SolveResult(True, queue=queue, score=float(best_score))
