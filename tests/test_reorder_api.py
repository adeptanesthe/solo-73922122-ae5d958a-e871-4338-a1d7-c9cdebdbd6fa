# -*- coding: utf-8 -*-
"""段落复序 API 测试：正常复序、输入校验、冲突说明、指纹回显与无状态性。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

CHAIN = [
    "苹果" * 15,
    "苹果香蕉" * 8,
    "香蕉樱桃" * 8,
    "樱桃葡萄" * 8,
    "葡萄" * 15,
]
SCRAMBLED = [CHAIN[2], CHAIN[4], CHAIN[0], CHAIN[3], CHAIN[1]]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app import database

    database._DB_PATH = str(tmp_path / "reorder_test.db")
    monkeypatch.setenv("RECOMMENDER_DB", database._DB_PATH)
    with TestClient(app) as c:
        yield c


def test_reorder_happy_path(client):
    r = client.post("/api/reorder", json={
        "paragraphs": SCRAMBLED,
        "first": 2,
        "last": 1,
        "adjacent": [[0, 3]],   # 段1(原P3) 紧接 段4(原P4)，与链一致
        "before": [[2, 4]],     # 段3(原P1) 先于 段5(原P2)
        "fingerprint": "fp-abc",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["order"] == [2, 4, 0, 3, 1]
    assert data["fingerprint"] == "fp-abc"
    assert len(data["links"]) == 4
    assert all(0 <= l["score"] <= 1 for l in data["links"])
    assert data["total_score"] > 0
    assert data["feature_count"] > 0
    # 返回的重排正文与原输入一致（按新顺序）
    assert [p["index"] for p in data["paragraphs"]] == data["order"]
    assert data["paragraphs"][0]["text"] == SCRAMBLED[2].strip()
    # 紧接标记被满足并被标注
    pos = {p: i for i, p in enumerate(data["order"])}
    assert pos[3] == pos[0] + 1
    assert any(l["marked"] for l in data["links"])


def test_reorder_conflict_returns_explanation_not_order(client):
    r = client.post("/api/reorder", json={
        "paragraphs": CHAIN,
        "first": 0,
        "last": 4,
        "adjacent": [[1, 2], [2, 1]],   # 回环
    })
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False
    assert data["reason"]
    assert "order" not in data


def test_reorder_zero_similarity_explained(client):
    r = client.post("/api/reorder", json={
        "paragraphs": ["甲" * 30, "乙" * 30, "丙" * 30, "丁" * 30],
        "first": 0,
        "last": 3,
    })
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False
    assert "相似度均为零" in data["reason"]


def test_reorder_validation_422(client):
    base = {"paragraphs": CHAIN, "first": 0, "last": 4}
    # 段落数不足
    r = client.post("/api/reorder", json={**base, "paragraphs": CHAIN[:3]})
    assert r.status_code == 422
    # 首末相同
    r = client.post("/api/reorder", json={**base, "last": 0})
    assert r.status_code == 422
    # 过短段落
    r = client.post("/api/reorder", json={**base, "paragraphs": ["短"] * 5})
    assert r.status_code == 422
    # 三组紧接标记
    r = client.post("/api/reorder", json={**base, "adjacent": [[0, 1], [1, 2], [2, 3]]})
    assert r.status_code == 422
    # 序号越界
    r = client.post("/api/reorder", json={**base, "before": [[0, 9]]})
    assert r.status_code == 422


def test_reorder_is_stateless_and_recommender_untouched(client):
    # 两次不同输入互不影响
    r1 = client.post("/api/reorder", json={
        "paragraphs": SCRAMBLED, "first": 2, "last": 1, "fingerprint": "v1",
    }).json()
    r2 = client.post("/api/reorder", json={
        "paragraphs": SCRAMBLED, "first": 2, "last": 1,
        "adjacent": [[0, 3]], "fingerprint": "v2",
    }).json()
    assert r1["fingerprint"] == "v1" and r2["fingerprint"] == "v2"
    assert r1["ok"] and r2["ok"]
    # 原阅读推荐接口仍可用
    s = client.get("/api/status")
    assert s.status_code == 200
    assert "topics" in s.json() and "articles" in s.json()
