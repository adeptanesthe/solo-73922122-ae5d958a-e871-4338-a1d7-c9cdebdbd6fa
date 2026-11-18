# -*- coding: utf-8 -*-
"""TF-IDF 模型测试：仅文章拟合、字符 2~3 元、余弦相关度语义合理性。"""
from app.articles_data import ARTICLES
from app.tfidf_model import get_model


def test_model_fitted_on_articles_only():
    model = get_model()
    # 特征数量由 30 篇正文决定，兴趣文本不参与 fit
    assert model.feature_count > 100
    assert len(model.article_ids) == 30
    # 分析器是纯字符级 2~3 元
    assert model.vectorizer.analyzer == "char"
    assert model.vectorizer.ngram_range == (2, 3)
    # 词表里所有特征的字符长度都必须恰好是 2 或 3
    lengths = {len(term) for term in model.vectorizer.vocabulary_}
    assert lengths <= {2, 3} and lengths


def test_interest_transform_uses_same_vocabulary():
    model = get_model()
    q = model.transform_interest("深度学习训练神经网络")
    # 变换后的维度必须等于文章矩阵的特征维度
    assert q.shape[1] == model.article_matrix.shape[1]


def test_relevance_range_and_ranking():
    model = get_model()
    rel = model.relevance_vector("神经网络训练、梯度下降与深度学习模型优化")
    assert len(rel) == 30
    assert all(0.0 <= v <= 1.0 for v in rel.values())
    # AI 主题文章的平均相关度应高于体育主题
    ai = [rel[a["id"]] for a in ARTICLES if a["topic"] == "人工智能"]
    sport = [rel[a["id"]] for a in ARTICLES if a["topic"] == "体育竞技"]
    assert sum(ai) / len(ai) > sum(sport) / len(sport)


def test_unrelated_interest_low_scores():
    model = get_model()
    rel = model.relevance_vector("量子纠缠弦理论黑洞宇宙学暗物质星系")
    # 语料里几乎没有这些词汇，整体相关度应当很低
    assert max(rel.values()) < 0.15


def test_known_article_text_ranks_itself_high():
    model = get_model()
    target = next(a for a in ARTICLES if a["id"] == 22)  # 家常炖煮
    rel = model.relevance_vector(target["body"])
    top_id = max(rel, key=rel.get)
    assert top_id == target["id"]
    assert rel[target["id"]] > 0.9
