# -*- coding: utf-8 -*-
"""段落复序求解器测试：拟合范围、约束、最优性、平局、冲突与零相似度。"""
from __future__ import annotations

import pytest

from app.reorder import (
    ReorderInputError,
    diagnose_conflicts,
    solve_reorder,
    validate_inputs,
)

# ---- 构造词汇链段落：唯一正相似度只出现在相邻链节上 ----
# P1 纯苹果 / P2 苹果香蕉 / P3 香蕉樱桃 / P4 樱桃葡萄 / P5 纯葡萄
# 任意非链相邻对（如 P1-P3、P2-P5）字符二元组完全不相交，相似度为 0。
CHAIN = [
    "苹果" * 15,                                 # P1
    ("苹果香蕉" * 8),                            # P2
    ("香蕉樱桃" * 8),                            # P3
    ("樱桃葡萄" * 8),                            # P4
    "葡萄" * 15,                                 # P5
]
# 打乱后的粘贴顺序：P3, P5, P1, P4, P2
SCRAMBLED = [CHAIN[2], CHAIN[4], CHAIN[0], CHAIN[3], CHAIN[1]]
# 各原段在打乱序列中的序号：P1->2, P2->4, P3->0, P4->3, P5->1


def test_recovers_chain_order_from_scrambled_input():
    res = solve_reorder(SCRAMBLED, first=2, last=1)
    assert res.ok, res.reason
    # 唯一全部衔接为正分的顺序：P1 P2 P3 P4 P5
    assert res.order == [2, 4, 0, 3, 1]
    assert len(res.links) == 4
    assert all(l["score"] > 0 for l in res.links)
    assert res.total_score == pytest.approx(sum(l["score"] for l in res.links), abs=1e-6)
    assert res.feature_count > 0


def test_each_paragraph_used_exactly_once():
    res = solve_reorder(SCRAMBLED, first=2, last=1)
    assert res.ok
    assert sorted(res.order) == list(range(5))


def test_adjacent_mark_is_enforced_even_at_score_cost():
    # 紧接 (P2, P4) 且 先于 (P2, P3)：唯一可行顺序 P1 P2 P4 P3 P5，
    # 得分 s12+s34 低于无标记时的链式最优，但标记必须被满足
    res = solve_reorder(CHAIN, first=0, last=4, adjacent=[(1, 3)], before=[(1, 2)])
    assert res.ok, res.reason
    assert res.order == [0, 1, 3, 2, 4]
    pos = {p: i for i, p in enumerate(res.order)}
    assert pos[3] == pos[1] + 1
    assert pos[1] < pos[2]
    marked = [(l["from"], l["to"]) for l in res.links if l["marked"]]
    assert marked == [(1, 3)]


def test_before_mark_is_enforced():
    # 先于 (P3, P2)：P3 必须排在 P2 之前（不必相邻）
    res = solve_reorder(CHAIN, first=0, last=4, before=[(2, 1)])
    assert res.ok, res.reason
    pos = {p: i for i, p in enumerate(res.order)}
    assert pos[2] < pos[1]


def test_tie_break_picks_smallest_index_sequence():
    # 四段完全相同：所有排列同分，取原输入序号序列字典序最小者
    same = ["春眠不觉晓处处闻啼鸟夜来风雨声花落知多少。"] * 4
    res = solve_reorder(same, first=2, last=0)
    assert res.ok
    assert res.order == [2, 1, 3, 0]
    res2 = solve_reorder(same, first=0, last=3)
    assert res2.order == [0, 1, 2, 3]


def test_all_zero_similarity_gives_reason_not_order():
    disjoint = ["甲" * 30, "乙" * 30, "丙" * 30, "丁" * 30]
    res = solve_reorder(disjoint, first=0, last=3)
    assert not res.ok
    assert "相似度均为零" in res.reason
    assert res.order == []


def test_adjacent_cycle_conflict():
    res = solve_reorder(CHAIN, first=0, last=4, adjacent=[(1, 2), (2, 1)])
    assert not res.ok
    assert "回环" in res.reason or "循环" in res.reason


def test_duplicate_adjacent_source_conflict():
    res = solve_reorder(CHAIN, first=0, last=4, adjacent=[(1, 2), (1, 3)])
    assert not res.ok
    assert "只能紧接一个段落" in res.reason


def test_adjacent_into_first_conflict():
    res = solve_reorder(CHAIN, first=0, last=4, adjacent=[(2, 0)])
    assert not res.ok
    assert "首段" in res.reason


def test_adjacent_after_last_conflict():
    res = solve_reorder(CHAIN, first=0, last=4, adjacent=[(4, 2)])
    assert not res.ok
    assert "末段" in res.reason


def test_before_cycle_conflict():
    res = solve_reorder(CHAIN, first=0, last=4, before=[(1, 2), (2, 1)])
    assert not res.ok
    assert "循环" in res.reason


def test_before_into_first_conflict():
    res = solve_reorder(CHAIN, first=0, last=4, before=[(2, 0)])
    assert not res.ok
    assert "首段" in res.reason


def test_first_last_same_chain_conflict():
    # 链 首->X->末 长度 3 < 5：首段固定在开头时末段落不到末尾
    res = solve_reorder(CHAIN, first=0, last=2, adjacent=[(0, 1), (1, 2)])
    assert not res.ok
    assert "链" in res.reason


def test_mixed_adjacent_before_cycle_conflict():
    # 紧接 P1->P2 且 先于 P2->P1：混合成环
    res = solve_reorder(CHAIN, first=0, last=4, adjacent=[(1, 2)], before=[(2, 1)])
    assert not res.ok
    assert "循环" in res.reason


# ---------------- 输入形状校验（对应 HTTP 422） ----------------


def _ok_paras(n=4):
    return ["段落内容" * 10] * n


def test_paragraph_count_bounds():
    with pytest.raises(ReorderInputError, match="4~8"):
        validate_inputs(_ok_paras(3), 0, 2, [], [])
    with pytest.raises(ReorderInputError, match="4~8"):
        validate_inputs(_ok_paras(9), 0, 8, [], [])
    validate_inputs(_ok_paras(4), 0, 3, [], [])  # 不抛异常


def test_paragraph_length_bounds():
    paras = _ok_paras(4)
    paras[1] = "太短了"
    with pytest.raises(ReorderInputError, match="少于下限"):
        validate_inputs(paras, 0, 3, [], [])
    paras = _ok_paras(4)
    paras[2] = "长" * 401
    with pytest.raises(ReorderInputError, match="超过上限"):
        validate_inputs(paras, 0, 3, [], [])


def test_first_last_must_differ():
    with pytest.raises(ReorderInputError, match="不同"):
        validate_inputs(_ok_paras(4), 1, 1, [], [])


def test_mark_count_bounds():
    with pytest.raises(ReorderInputError, match="最多 2 组"):
        validate_inputs(_ok_paras(5), 0, 4, [(0, 1), (1, 2), (2, 3)], [])
    with pytest.raises(ReorderInputError, match="最多 2 组"):
        validate_inputs(_ok_paras(5), 0, 4, [], [(0, 1), (1, 2), (2, 3)])


def test_self_mark_rejected():
    with pytest.raises(ReorderInputError, match="同一段落"):
        validate_inputs(_ok_paras(4), 0, 3, [(1, 1)], [])
    with pytest.raises(ReorderInputError, match="同一段落"):
        validate_inputs(_ok_paras(4), 0, 3, [], [(2, 2)])


def test_mark_index_range():
    with pytest.raises(ReorderInputError, match="无效段落序号"):
        validate_inputs(_ok_paras(4), 0, 3, [(0, 9)], [])


def test_diagnose_returns_empty_for_consistent_marks():
    assert diagnose_conflicts(5, 0, 4, [(0, 1)], [(2, 3)]) == []


def test_solver_raises_input_error_on_bad_shape():
    with pytest.raises(ReorderInputError):
        solve_reorder(["太短"] * 4, 0, 3)
