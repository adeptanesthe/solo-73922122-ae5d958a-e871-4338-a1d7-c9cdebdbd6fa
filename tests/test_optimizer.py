# -*- coding: utf-8 -*-
"""求解器单元测试：约束、最优性、前缀固定、不可行场景与暴力对拍。"""
from __future__ import annotations

import itertools

import pytest

from app.articles_data import ARTICLES, TOPICS
from app.optimizer import POSITION_WEIGHTS, solve_queue

EQUAL_QUOTAS = {t: 1 for t in TOPICS}


def make_relevance(fill: float = 0.5) -> dict[int, float]:
    return {a["id"]: fill for a in ARTICLES}


def test_data_layout():
    assert len(ARTICLES) == 30
    from collections import Counter

    topic_count = Counter(a["topic"] for a in ARTICLES)
    assert all(topic_count[t] == 5 for t in TOPICS)
    author_topics: dict[str, set] = {}
    for a in ARTICLES:
        author_topics.setdefault(a["author"], set()).add(a["topic"])
    assert all(len(v) == 3 for v in author_topics.values())


def test_basic_solution_constraints():
    rel = make_relevance()
    res = solve_queue(rel, EQUAL_QUOTAS)
    assert res.feasible, res.reason
    assert len(res.queue) == 6
    topics = [q["topic"] for q in res.queue]
    assert sorted(topics) == sorted(TOPICS)
    authors = [q["author"] for q in res.queue]
    assert len(set(authors)) == 6
    assert [q["position"] for q in res.queue] == [1, 2, 3, 4, 5, 6]
    # 得分与目标函数一致
    expected = sum(POSITION_WEIGHTS[q["position"]] * q["relevance"] for q in res.queue)
    assert res.score == pytest.approx(expected)


def test_relevance_descending_order_without_prefix():
    # 无固定前缀时，全局最优队列必然按相关度降序排列
    rel = {a["id"]: (a["id"] * 37 % 101) / 101.0 for a in ARTICLES}
    res = solve_queue(rel, EQUAL_QUOTAS)
    assert res.feasible
    rels = [q["relevance"] for q in res.queue]
    assert rels == sorted(rels, reverse=True)


def test_bruteforce_optimal_without_prefix():
    # 与全暴力枚举对拍（随机相关度，多次）
    import random

    rng = random.Random(42)
    by_topic = {t: [a for a in ARTICLES if a["topic"] == t] for t in TOPICS}
    for trial in range(8):
        rel = {a["id"]: rng.random() for a in ARTICLES}
        res = solve_queue(rel, EQUAL_QUOTAS)
        assert res.feasible

        best = -1.0
        for choices in itertools.product(*(by_topic[t] for t in TOPICS)):
            if len({c["author"] for c in choices}) < 6:
                continue
            ordered = sorted(choices, key=lambda c: (-rel[c["id"]], c["id"]))
            score = sum(POSITION_WEIGHTS[i + 1] * rel[c["id"]] for i, c in enumerate(ordered))
            best = max(best, score)
        assert res.score == pytest.approx(best, abs=1e-12), f"trial {trial}"


def test_bruteforce_with_custom_quotas_and_prefix():
    # 自定义配额（2,2,1,1,0,0）+ 2 篇固定前缀，对拍剩余位置的最优解
    import random

    rng = random.Random(7)
    rel = {a["id"]: rng.random() for a in ARTICLES}
    quotas = {
        "人工智能": 2,
        "医疗健康": 2,
        "金融投资": 1,
        "旅游地理": 1,
        "美食烹饪": 0,
        "体育竞技": 0,
    }
    # 先求一个完整最优解，取前两篇作为“已揭示前缀”
    full = solve_queue(rel, quotas)
    assert full.feasible
    prefix = [
        {"article_id": full.queue[i]["article_id"],
         "topic": full.queue[i]["topic"],
         "author": full.queue[i]["author"],
         "relevance": full.queue[i]["relevance"]}
        for i in range(2)
    ]
    res = solve_queue(rel, quotas, prefix=prefix)
    assert res.feasible
    # 前缀位置与内容不变
    for i in range(2):
        assert res.queue[i]["article_id"] == prefix[i]["article_id"]

    # 暴力：固定前两篇，枚举满足配额与作者约束的其余 4 篇（按配额做组合枚举）
    used_authors = {p["author"] for p in prefix}
    free_quotas = dict(quotas)
    for p in prefix:
        free_quotas[p["topic"]] -= 1
    topic_pools = {
        t: [a for a in ARTICLES if a["topic"] == t and a["author"] not in used_authors]
        for t in TOPICS if free_quotas[t] > 0
    }
    combo_lists = [list(itertools.combinations(topic_pools[t], free_quotas[t]))
                   for t in TOPICS if free_quotas[t] > 0]
    best = -1.0
    for combo in itertools.product(*combo_lists):
        flat = [c for group in combo for c in group]
        if len({c["author"] for c in flat}) < len(flat):
            continue
        ordered = sorted(flat, key=lambda c: (-rel[c["id"]], c["id"]))
        free_weights = [POSITION_WEIGHTS[pos] for pos in range(3, 7)]
        score = sum(free_weights[i] * rel[c["id"]] for i, c in enumerate(ordered))
        best = max(best, score)
    prefix_score = sum(POSITION_WEIGHTS[i + 1] * prefix[i]["relevance"] for i in range(2))
    assert res.score == pytest.approx(prefix_score + best, abs=1e-12)


def test_prefix_is_locked_even_when_relevance_changes():
    # 兴趣变化导致相关度重排，已揭示前缀仍保持原位置与文章
    rel1 = {a["id"]: r for a, r in zip(ARTICLES, [0.9 - i * 0.01 for i in range(30)])}
    res1 = solve_queue(rel1, EQUAL_QUOTAS)
    prefix = [
        {"article_id": res1.queue[0]["article_id"],
         "topic": res1.queue[0]["topic"],
         "author": res1.queue[0]["author"],
         "relevance": res1.queue[0]["relevance"]},
        {"article_id": res1.queue[1]["article_id"],
         "topic": res1.queue[1]["topic"],
         "author": res1.queue[1]["author"],
         "relevance": res1.queue[1]["relevance"]},
    ]
    rng_rel = {a["id"]: (a["id"] * 13 % 97) / 100.0 for a in ARTICLES}
    res2 = solve_queue(rng_rel, EQUAL_QUOTAS, prefix=prefix)
    assert res2.feasible
    assert res2.queue[0]["article_id"] == prefix[0]["article_id"]
    assert res2.queue[1]["article_id"] == prefix[1]["article_id"]
    # 剩余位置按新相关度做全局最优
    free_rels = [q["relevance"] for q in res2.queue[2:]]
    assert free_rels == sorted(free_rels, reverse=True)


def test_quota_sum_validation():
    bad = dict(EQUAL_QUOTAS)
    bad["人工智能"] = 2
    bad["医疗健康"] = 0  # 总和仍是 6，但主题覆盖变化本身允许；构造真正非法的
    res = solve_queue(make_relevance(), bad)
    assert res.feasible  # 总和为 6 的其它配额是允许的
    bad2 = dict(EQUAL_QUOTAS)
    bad2["人工智能"] = 3  # 总和变 7
    res2 = solve_queue(make_relevance(), bad2)
    assert not res2.feasible and "配额" in res2.reason


def test_excluded_revealed_article_rejected():
    res = solve_queue(make_relevance(), EQUAL_QUOTAS)
    prefix = [
        {"article_id": res.queue[0]["article_id"],
         "topic": res.queue[0]["topic"],
         "author": res.queue[0]["author"],
         "relevance": 0.5},
    ]
    blocked = solve_queue(
        make_relevance(), EQUAL_QUOTAS,
        prefix=prefix, excluded_ids={prefix[0]["article_id"]},
    )
    assert not blocked.feasible
    assert "已揭示" in blocked.reason


def test_infeasible_when_exhausting_topic():
    # 某主题配额 5 篇，排除其中 1 篇后候选不足
    rel = make_relevance()
    quotas = {t: 0 for t in TOPICS}
    quotas["人工智能"] = 6  # 先制造总和非法
    assert not solve_queue(rel, quotas).feasible
    # 合法总和但人为把某主题候选耗尽：AI 取 5、其余 1 篇来自医疗 -> 总和 6
    quotas = {t: 0 for t in TOPICS}
    quotas["人工智能"] = 5
    quotas["医疗健康"] = 1
    ai_ids = [a["id"] for a in ARTICLES if a["topic"] == "人工智能"]
    res = solve_queue(rel, quotas, excluded_ids=set(ai_ids[:2]))
    assert not res.feasible
    assert "人工智能" in res.reason


def test_author_conflict_makes_infeasible():
    # 通过配额 2 + 排除，构造“某主题仅剩与已占用作者冲突的候选”这一不可行情形。
    rel = make_relevance()
    base = solve_queue(rel, EQUAL_QUOTAS)
    occupied_author = base.queue[0]["author"]

    # 选一个包含该作者文章、且与前缀文章主题不同的目标主题
    target_topic = next(
        a["topic"] for a in ARTICLES
        if a["author"] == occupied_author and a["topic"] != base.queue[0]["topic"]
    )
    # 该主题中其它作者的文章全部排除，只留下 occupied_author 的那一篇
    others = [
        a["id"] for a in ARTICLES
        if a["topic"] == target_topic and a["author"] != occupied_author
    ]
    quotas = {t: 1 for t in TOPICS}
    quotas[target_topic] = 2
    # 总和保持 6：在另一个与前缀、目标都不同的主题上减配额
    donor = next(
        t for t in TOPICS
        if t != target_topic and t != base.queue[0]["topic"]
    )
    quotas[donor] = 0
    prefix = [{
        "article_id": base.queue[0]["article_id"],
        "topic": base.queue[0]["topic"],
        "author": occupied_author,
        "relevance": 0.5,
    }]
    out = solve_queue(
        rel, quotas, prefix=prefix, excluded_ids=set(others)
    )
    # 目标主题需要 2 篇，但只剩 1 篇候选且其作者与前缀冲突 -> 不可行
    assert not out.feasible
    assert target_topic in out.reason


def test_deterministic_tie_breaking():
    # 全部相关度相同时，重复求解结果必须一致（按 article_id 平局）
    r1 = solve_queue(make_relevance(0.42), EQUAL_QUOTAS)
    r2 = solve_queue(make_relevance(0.42), EQUAL_QUOTAS)
    ids1 = [q["article_id"] for q in r1.queue]
    ids2 = [q["article_id"] for q in r2.queue]
    assert ids1 == ids2
