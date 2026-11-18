# -*- coding: utf-8 -*-
"""SQLite 持久化层：会话状态、异步重算任务、排除集。

设计要点
========
* 单会话全局状态（对应“两个浏览器窗口共享同一进度”的演示模型）；
* WAL 模式 + BEGIN IMMEDIATE 写事务，配合进程内写锁串行化所有变更，
  保证并发请求下“同条件同进度只推进一次”；
* 已揭示前缀、条件、世代号(generation)全部落盘，重启后完整恢复；
* 后台任务只在“仍是最新世代且条件未变”时才允许写回结果，迟到结果被丢弃。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from .articles_data import ARTICLES, TOPICS

_DB_PATH = os.environ.get("RECOMMENDER_DB", "/data/recommender.db")
_init_lock = threading.Lock()
# 揭示并发批次宽限窗口（秒）。窗口内同世代的揭示请求（两个浏览器窗口同时
# 点击、客户端重试、锁上排队的姊妹线程）只复用首个请求的结果；窗口过后顺序
# 发起的“揭示下一组”正常推进。2 秒远小于人工连续点击间隔，又足以覆盖
# 线程调度与网络重试。可用环境变量 REVEAL_CLAIM_TTL 调整。
_CLAIM_TTL_SECONDS = float(os.environ.get("REVEAL_CLAIM_TTL", "2"))
# 进程内写锁：SQLite 文件锁跨进程有效，但进程内多线程的极短事务在 GIL
# 交错下可能连续提交；可重入锁把所有写事务在进程内串行化，双保险。
_write_lock = threading.RLock()
# 揭示并发批次状态：_reveal_generation 记录最近一次揭示所属世代，
# _reveal_batch_deadline 是该批次的宽限截止时间；同一并发批次内，
# 在写锁上排队的后续请求直接复用首个请求的结果而不再推进。
_reveal_generation: int | None = None
_reveal_batch_deadline: float = 0.0
_reveal_batch_outcome: dict[str, Any] | None = None
_reveal_batch_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ts() -> float:
    """单调时钟基准统一的 POSIX 时间戳（UTC 秒），用于票据过期比较。"""
    return datetime.now(timezone.utc).timestamp()


def get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def _write_tx():
    """串行化的立即写事务上下文。"""
    with _write_lock:
        conn = get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


@contextmanager
def _read_tx():
    conn = get_conn()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with _init_lock:
        with _write_tx() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    author TEXT NOT NULL
                );

                -- 全局单会话状态（id 固定为 1）
                CREATE TABLE IF NOT EXISTS state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    interest TEXT NOT NULL,
                    quotas_json TEXT NOT NULL,
                    generation INTEGER NOT NULL DEFAULT 1,
                    revealed_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS exclusions (
                    article_id INTEGER PRIMARY KEY REFERENCES articles(id)
                );

                -- 异步重算任务：每个世代至多一条
                CREATE TABLE IF NOT EXISTS jobs (
                    generation INTEGER PRIMARY KEY,
                    status TEXT NOT NULL,              -- pending/running/done/failed/stale
                    condition_snapshot_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    finished_at TEXT
                );

                -- 揭示票据：并发去重。同世代同进度点的第一个请求生成票据，
                -- 并发窗口内的重复请求命中同票据、只返回结果而不再次推进；
                -- TTL 过期后顺序的“揭示下一组”请求可以正常推进到下一位置。
                CREATE TABLE IF NOT EXISTS reveal_claims (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    generation INTEGER NOT NULL,
                    served_length INTEGER NOT NULL,
                    outcome_json TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                """
            )
            # 轻量迁移：早期版本 reveal_claims.expires_at 为 TEXT，
            # 与 POSIX 时间戳比较不兼容，发现旧结构时重建。
            cols = {
                r[1] for r in conn.execute("PRAGMA table_info(reveal_claims)")
            }
            if cols and "expires_at" in cols:
                col_type = conn.execute(
                    "SELECT type FROM pragma_table_info('reveal_claims') "
                    "WHERE name = 'expires_at'"
                ).fetchone()
                if col_type and col_type[0].upper() != "REAL":
                    conn.execute("DROP TABLE reveal_claims")
                    conn.execute(
                        "CREATE TABLE reveal_claims ("
                        "id INTEGER PRIMARY KEY CHECK (id = 1), "
                        "generation INTEGER NOT NULL, served_length INTEGER NOT NULL, "
                        "outcome_json TEXT NOT NULL, expires_at REAL NOT NULL)"
                    )
            # 文章种子（幂等）
            existing = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
            if existing == 0:
                conn.executemany(
                    "INSERT INTO articles(id, title, body, topic, author) "
                    "VALUES (:id, :title, :body, :topic, :author)",
                    ARTICLES,
                )
            exists = conn.execute("SELECT 1 FROM state WHERE id = 1").fetchone()
            if not exists:
                default_quotas = {t: 1 for t in TOPICS}
                default_interest = "我想了解人工智能模型训练与数据驱动的分析方法"
                conn.execute(
                    "INSERT INTO state(id, interest, quotas_json, generation, "
                    "revealed_json, updated_at) VALUES (1, ?, ?, 1, '[]', ?)",
                    (
                        default_interest,
                        json.dumps(default_quotas, ensure_ascii=False),
                        _now(),
                    ),
                )
                # 首个世代立即登记 pending 任务，启动后自动产出推荐
                snapshot = {
                    "interest": default_interest,
                    "quotas": default_quotas,
                    "excluded_ids": [],
                    "compute_delay_ms": 0,
                }
                conn.execute(
                    "INSERT INTO jobs(generation, status, condition_snapshot_json, "
                    "created_at) VALUES (1, 'pending', ?, ?)",
                    (json.dumps(snapshot, ensure_ascii=False), _now()),
                )


# ---------------- 状态读取 ----------------


def get_state() -> dict[str, Any]:
    with _read_tx() as conn:
        row = conn.execute("SELECT * FROM state WHERE id = 1").fetchone()
        exclusions = {
            r[0] for r in conn.execute("SELECT article_id FROM exclusions")
        }
        return {
            "interest": row["interest"],
            "quotas": json.loads(row["quotas_json"]),
            "generation": row["generation"],
            "revealed": json.loads(row["revealed_json"]),
            "excluded_ids": sorted(exclusions),
            "updated_at": row["updated_at"],
        }


def get_latest_job() -> dict[str, Any] | None:
    with _read_tx() as conn:
        row = conn.execute(
            "SELECT * FROM jobs ORDER BY generation DESC LIMIT 1"
        ).fetchone()
        return _job_row_to_dict(row) if row else None


def get_job(generation: int) -> dict[str, Any] | None:
    with _read_tx() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE generation = ?", (generation,)
        ).fetchone()
        return _job_row_to_dict(row) if row else None


def _job_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "generation": row["generation"],
        "status": row["status"],
        "condition_snapshot": json.loads(row["condition_snapshot_json"]),
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "error": row["error"],
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
    }


def _excluded_ids_locked(conn: sqlite3.Connection) -> list[int]:
    return [
        r[0]
        for r in conn.execute("SELECT article_id FROM exclusions ORDER BY article_id")
    ]


def _stale_running_jobs(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'stale', finished_at = ? "
        "WHERE status IN ('pending', 'running')",
        (_now(),),
    )


# ---------------- 条件变更（开启新世代） ----------------


def begin_new_generation(
    interest: str | None,
    quotas: dict[str, int] | None,
    compute_delay_ms: int,
) -> dict[str, Any]:
    """修改兴趣/配额，世代号 +1，并登记一条 pending 任务。"""
    with _write_tx() as conn:
        row = conn.execute("SELECT * FROM state WHERE id = 1").fetchone()
        cur_interest = row["interest"]
        cur_quotas = json.loads(row["quotas_json"])
        new_interest = interest if interest is not None else cur_interest
        new_quotas = quotas if quotas is not None else cur_quotas
        new_gen = row["generation"] + 1
        conn.execute(
            "UPDATE state SET interest = ?, quotas_json = ?, generation = ?, "
            "updated_at = ? WHERE id = 1",
            (
                new_interest,
                json.dumps(new_quotas, ensure_ascii=False),
                new_gen,
                _now(),
            ),
        )
        _stale_running_jobs(conn)
        snapshot = {
            "interest": new_interest,
            "quotas": new_quotas,
            "compute_delay_ms": compute_delay_ms,
        }
        conn.execute(
            "INSERT INTO jobs(generation, status, condition_snapshot_json, "
            "created_at) VALUES (?, 'pending', ?, ?)",
            (new_gen, json.dumps(snapshot, ensure_ascii=False), _now()),
        )
    return get_state()


# ---------------- 排除集变更（同样开启新世代） ----------------


def add_exclusion(article_id: int, compute_delay_ms: int) -> dict[str, Any]:
    with _write_tx() as conn:
        state_row = conn.execute("SELECT * FROM state WHERE id = 1").fetchone()
        revealed = json.loads(state_row["revealed_json"])
        if any(r["article_id"] == article_id for r in revealed):
            raise ValueError("该文章已揭示，不能排除")
        exists = conn.execute(
            "SELECT 1 FROM exclusions WHERE article_id = ?", (article_id,)
        ).fetchone()
        if exists:
            raise ValueError("该文章已在排除列表中")
        conn.execute(
            "INSERT INTO exclusions(article_id) VALUES (?)", (article_id,)
        )
        new_gen = state_row["generation"] + 1
        quotas = json.loads(state_row["quotas_json"])
        conn.execute(
            "UPDATE state SET generation = ?, updated_at = ? WHERE id = 1",
            (new_gen, _now()),
        )
        _stale_running_jobs(conn)
        snapshot = {
            "interest": state_row["interest"],
            "quotas": quotas,
            "excluded_ids": _excluded_ids_locked(conn),
            "compute_delay_ms": compute_delay_ms,
        }
        conn.execute(
            "INSERT INTO jobs(generation, status, condition_snapshot_json, "
            "created_at) VALUES (?, 'pending', ?, ?)",
            (new_gen, json.dumps(snapshot, ensure_ascii=False), _now()),
        )
    return get_state()


def remove_exclusion(article_id: int, compute_delay_ms: int) -> dict[str, Any]:
    with _write_tx() as conn:
        cur = conn.execute(
            "SELECT 1 FROM exclusions WHERE article_id = ?", (article_id,)
        ).fetchone()
        if not cur:
            raise ValueError("该文章不在排除列表中")
        conn.execute("DELETE FROM exclusions WHERE article_id = ?", (article_id,))
        state_row = conn.execute("SELECT * FROM state WHERE id = 1").fetchone()
        new_gen = state_row["generation"] + 1
        quotas = json.loads(state_row["quotas_json"])
        conn.execute(
            "UPDATE state SET generation = ?, updated_at = ? WHERE id = 1",
            (new_gen, _now()),
        )
        _stale_running_jobs(conn)
        snapshot = {
            "interest": state_row["interest"],
            "quotas": quotas,
            "excluded_ids": _excluded_ids_locked(conn),
            "compute_delay_ms": compute_delay_ms,
        }
        conn.execute(
            "INSERT INTO jobs(generation, status, condition_snapshot_json, "
            "created_at) VALUES (?, 'pending', ?, ?)",
            (new_gen, json.dumps(snapshot, ensure_ascii=False), _now()),
        )
    return get_state()


# ---------------- 后台任务领取与写回 ----------------


def claim_pending_job() -> dict[str, Any] | None:
    """原子领取一条 pending 任务；没有则返回 None。"""
    with _write_tx() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE status = 'pending' "
            "ORDER BY generation DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        generation = row["generation"]
        conn.execute(
            "UPDATE jobs SET status = 'running' WHERE generation = ?",
            (generation,),
        )
    return get_job(generation)


def complete_job(generation: int, result: dict[str, Any]) -> bool:
    """写回成功结果。若世代已过期则拒绝，返回 False。"""
    with _write_tx() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE generation = ?", (generation,)
        ).fetchone()
        current_gen = conn.execute(
            "SELECT generation FROM state WHERE id = 1"
        ).fetchone()[0]
        if not row or row["status"] != "running" or current_gen != generation:
            # 迟到结果：旧条件推荐不得落库揭示
            conn.execute(
                "UPDATE jobs SET status = 'stale', finished_at = ? "
                "WHERE generation = ? AND status = 'running'",
                (_now(), generation),
            )
            return False
        conn.execute(
            "UPDATE jobs SET status = 'done', result_json = ?, "
            "finished_at = ? WHERE generation = ?",
            (json.dumps(result, ensure_ascii=False), _now(), generation),
        )
        return True


def fail_job(generation: int, error: str) -> bool:
    """记录失败。失败不推进进度；过期任务只标记 stale。"""
    with _write_tx() as conn:
        row = conn.execute(
            "SELECT status FROM jobs WHERE generation = ?", (generation,)
        ).fetchone()
        if not row or row["status"] != "running":
            return False
        current_gen = conn.execute(
            "SELECT generation FROM state WHERE id = 1"
        ).fetchone()[0]
        new_status = "failed" if current_gen == generation else "stale"
        conn.execute(
            "UPDATE jobs SET status = ?, error = ?, finished_at = ? "
            "WHERE generation = ?",
            (new_status, error[:500], _now(), generation),
        )
        return current_gen == generation


# ---------------- 揭示推进 ----------------


def _reset_reveal_batch() -> None:
    global _reveal_generation, _reveal_batch_deadline, _reveal_batch_outcome
    with _reveal_batch_lock:
        _reveal_generation = None
        _reveal_batch_deadline = 0.0
        _reveal_batch_outcome = None


def _peek_reveal_batch(generation: int) -> dict[str, Any] | None:
    """若当前请求命中有效并发批次，返回可复用的结果副本，否则 None。

    独立函数封装，避免 reveal_next_two 内 global 声明把预读取变成局部变量。
    """
    with _reveal_batch_lock:
        if (
            _reveal_batch_outcome is not None
            and _reveal_generation == generation
            and _now_ts() < _reveal_batch_deadline
        ):
            return json.loads(json.dumps(_reveal_batch_outcome))
    return None


def reveal_next_two() -> dict[str, Any]:
    """依据当前世代的已完成推荐，揭示下一组（最多两篇）。

    并发与幂等
    ----------
    所有请求在进程内写锁上排队。第一个进入临界区的请求执行揭示并开启一个
    “并发批次”（宽限 TTL）；同一世代、仍在批次窗口内拿到锁的后续请求
    （两个浏览器窗口同时点击、客户端重试、在锁上排队的姊妹线程）直接复用
    首个请求的结果而不再推进。窗口过后顺序发起的“揭示下一组”开启新批次、
    正常推进，因此 6 篇队列可分三轮揭示完毕。
    """
    with _write_lock:
        with _write_tx() as conn:
            state = conn.execute("SELECT * FROM state WHERE id = 1").fetchone()
            generation = state["generation"]
            revealed = json.loads(state["revealed_json"])
            current_len = len(revealed)

            # 命中并发批次：同世代重复请求只返回上次结果，不重复推进
            cached = _peek_reveal_batch(generation)
            if cached is not None:
                cached["advanced"] = False
                cached["idempotent"] = True
                cached["reason"] = "同条件同进度点的重复请求，下一组只推进一次"
                return cached

            job_row = conn.execute(
                "SELECT * FROM jobs WHERE generation = ?", (generation,)
            ).fetchone()
            if job_row is None:
                raise RuntimeError("当前世代没有重算任务")
            if job_row["status"] in ("pending", "running"):
                raise RuntimeError("推荐仍在计算中，请稍后再试")
            if job_row["status"] == "stale":
                raise RuntimeError("计算结果已过期，请使用最新条件的结果")
            if job_row["status"] == "failed":
                raise RuntimeError(f"上一轮重算失败，未推进进度: {job_row['error']}")

            result = json.loads(job_row["result_json"])
            if not result.get("feasible"):
                # 无解：保留已揭示内容并停止揭示
                outcome = {
                    "advanced": False,
                    "reason": result.get("reason") or "无可行解，停止揭示",
                    "revealed": revealed,
                    "generation": generation,
                }
                _open_batch(generation, outcome)
                _save_claim(conn, generation, current_len, outcome)
                return outcome

            queue = result["queue"]
            if current_len >= len(queue):
                outcome = {
                    "advanced": False,
                    "reason": "队列已全部揭示",
                    "revealed": revealed,
                    "generation": generation,
                }
                _open_batch(generation, outcome)
                _save_claim(conn, generation, current_len, outcome)
                return outcome

            new_items = queue[current_len:current_len + 2]
            revealed = revealed + new_items
            conn.execute(
                "UPDATE state SET revealed_json = ?, updated_at = ? WHERE id = 1",
                (json.dumps(revealed, ensure_ascii=False), _now()),
            )
            outcome = {
                "advanced": True,
                "revealed": revealed,
                "generation": generation,
                "new_positions": [item["position"] for item in new_items],
            }
            _open_batch(generation, outcome)
            _save_claim(conn, generation, current_len, outcome)
            return outcome


def _open_batch(generation: int, outcome: dict[str, Any]) -> None:
    global _reveal_generation, _reveal_batch_deadline, _reveal_batch_outcome
    with _reveal_batch_lock:
        _reveal_generation = generation
        _reveal_batch_deadline = _now_ts() + max(0.0, _claim_ttl_seconds())
        _reveal_batch_outcome = json.loads(json.dumps(outcome))


def _claim_ttl_seconds() -> float:
    # 间接读取以便测试调整；默认 10 秒并发去重窗口
    return _CLAIM_TTL_SECONDS


def _save_claim(
    conn: sqlite3.Connection,
    generation: int,
    served_length: int,
    outcome: dict[str, Any],
) -> None:
    """把最近一次揭示落库（审计/重启可查），去重判定以内存批次为准。"""
    expires = _now_ts() + max(0.0, _claim_ttl_seconds())
    conn.execute(
        "INSERT INTO reveal_claims(id, generation, served_length, outcome_json, "
        "expires_at) VALUES (1, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET generation = excluded.generation, "
        "served_length = excluded.served_length, outcome_json = excluded.outcome_json, "
        "expires_at = excluded.expires_at",
        (generation, served_length, json.dumps(outcome, ensure_ascii=False), expires),
    )


# ---------------- 演示重置 ----------------


def reset_all(
    default_interest: str,
    default_quotas: dict[str, int],
    compute_delay_ms: int = 0,
) -> None:
    """清空揭示、排除与任务，状态回到世代 1 并登记首条 pending 任务。"""
    _reset_reveal_batch()
    with _write_tx() as conn:
        conn.execute("DELETE FROM exclusions")
        conn.execute("DELETE FROM jobs")
        conn.execute("DELETE FROM reveal_claims")
        conn.execute(
            "UPDATE state SET interest = ?, quotas_json = ?, generation = 1, "
            "revealed_json = '[]', updated_at = ? WHERE id = 1",
            (
                default_interest,
                json.dumps(default_quotas, ensure_ascii=False),
                _now(),
            ),
        )
        snapshot = {
            "interest": default_interest,
            "quotas": default_quotas,
            "excluded_ids": [],
            "compute_delay_ms": compute_delay_ms,
        }
        conn.execute(
            "INSERT INTO jobs(generation, status, condition_snapshot_json, "
            "created_at) VALUES (1, 'pending', ?, ?)",
            (json.dumps(snapshot, ensure_ascii=False), _now()),
        )
