"""结构化日志 + 全链路 correlation_id。"""
from __future__ import annotations

import functools
import logging
import time
import uuid
from contextvars import ContextVar

_cid: ContextVar[str] = ContextVar("correlation_id", default="-")


def new_correlation_id() -> str:
    cid = uuid.uuid4().hex[:12]
    _cid.set(cid)
    return cid


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                f"[%(levelname)s] [cid={_cid.get()}] [%(name)s] %(message)s"
            )
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


def trace_node(name: str, fn):
    """给图节点加轻量耗时追踪（架构 §4.6 可观测性的最小落地）。

    只记录节点名与耗时，不序列化 state：把输入/输出整段打进日志会再次引入
    凭据泄漏面，且高频节点日志会淹没可读性。
    """

    @functools.wraps(fn)
    def wrapper(state):
        log = get_logger(f"bill_agent.node.{name}")
        started = time.perf_counter()
        log.info("enter")
        try:
            return fn(state)
        finally:
            log.info("exit elapsed=%.3fs", time.perf_counter() - started)

    return wrapper
