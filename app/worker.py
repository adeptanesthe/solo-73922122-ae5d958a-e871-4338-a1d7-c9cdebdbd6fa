# -*- coding: utf-8 -*-
"""后台重算 worker。

* 守护线程轮询 pending 任务，模拟慢重算（可由 compute_delay_ms 控制）；
* 计算前快照条件，写回前再次核对世代号，迟到结果一律拒绝落库；
* 计算异常只把任务标记 failed，绝不修改已揭示前缀（失败不推进进度）。
"""
from __future__ import annotations

import logging
import threading
import time

from . import database
from .optimizer import solve_queue
from .tfidf_model import get_model

log = logging.getLogger("recommender.worker")


class ComputeWorker:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._busy = threading.Event()  # 正在执行某个任务（含模拟延迟）

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="recompute-worker", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """等待 worker 处理完当前已领取的任务，返回是否已空闲。"""
        deadline = time.monotonic() + timeout
        while self._busy.is_set() and time.monotonic() < deadline:
            time.sleep(0.02)
        return not self._busy.is_set()

    def _run(self) -> None:
        # 模型在后台线程首次构建，避免拖累启动；get_model 内部有锁。
        while not self._stop.is_set():
            try:
                job = database.claim_pending_job()
            except Exception:  # pragma: no cover - 数据库临时故障重试
                log.exception("领取任务失败，稍后重试")
                time.sleep(0.5)
                continue
            if job is None:
                self._stop.wait(0.25)
                continue
            self._busy.set()
            try:
                self._process(job)
            finally:
                self._busy.clear()

    def _process(self, job: dict) -> None:
        generation = job["generation"]
        snapshot = job["condition_snapshot"]
        delay = max(0, int(snapshot.get("compute_delay_ms", 0))) / 1000.0
        try:
            # 慢重算窗口：此期间用户可以继续修改条件，产生新世代。
            # 分段等待以便及时响应停止信号。
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                if self._stop.is_set():
                    return
                time.sleep(min(0.1, deadline - time.monotonic()))

            state = database.get_state()
            # 双重检查：开始计算前确认自己仍是最新世代
            if state["generation"] != generation:
                database.fail_job(generation, "计算开始前条件已变更，任务作废")
                return

            model = get_model()
            relevance = model.relevance_vector(snapshot["interest"])
            result = solve_queue(
                relevance=relevance,
                quotas=snapshot["quotas"],
                prefix=state["revealed"],
                excluded_ids=set(snapshot.get("excluded_ids", state["excluded_ids"])),
            )
            payload = {
                "feasible": result.feasible,
                "queue": result.queue,
                "score": result.score,
                "reason": result.reason,
            }
            accepted = database.complete_job(generation, payload)
            if not accepted:
                log.info("世代 %s 的迟到结果已被丢弃", generation)
        except Exception as exc:  # 失败不推进进度，只记录任务错误
            log.exception("世代 %s 重算失败", generation)
            try:
                database.fail_job(generation, f"{type(exc).__name__}: {exc}")
            except Exception:  # pragma: no cover
                log.exception("写入失败状态时出错")


_worker = ComputeWorker()


def get_worker() -> ComputeWorker:
    return _worker
