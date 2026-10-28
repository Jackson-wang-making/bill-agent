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


_COUNTED_KEYS = (
    ("source_files", "files"),
    ("parsed_documents", "docs"),
    ("parse_errors", "parse_errors"),
    ("extracted_charges", "charges"),
    ("normalized_charges", "norm"),
    ("match_results", "matches"),
    ("output_records", "records"),
)


def _state_summary(state) -> str:
    """输入摘要：只记列表长度，不序列化任何值（凭据/正文不得进日志）。"""
    if not isinstance(state, dict):
        return ""
    parts = []
    for key, label in _COUNTED_KEYS:
        value = state.get(key)
        if isinstance(value, (list, tuple)):
            parts.append(f"{label}={len(value)}")
    return ",".join(parts)


def _live_budget():
    """当前运行上下文的 token/OCR 用量快照；未初始化则返回 None。"""
    try:
        from bill_agent.harness.budget import _CURRENT

        tracker = _CURRENT.get()
        return tracker.snapshot() if tracker is not None else None
    except Exception:  # noqa: BLE001 - 日志绝不能影响主流程
        return None


def trace_node(name: str, fn):
    """给图节点加轻量耗时追踪（架构 §4.6 可观测性的最小落地）。

    记录进入时的长度摘要、退出时的耗时、输出键名与预算快照；不序列化 state，
    避免把正文/凭据再次带进日志。
    """

    @functools.wraps(fn)
    def wrapper(state):
        log = get_logger(f"bill_agent.node.{name}")
        started = time.perf_counter()
        log.info("enter %s", _state_summary(state))
        result = None
        try:
            result = fn(state)
            return result
        finally:
            out_keys = ",".join(sorted(result)) if isinstance(result, dict) else "-"
            log.info(
                "exit elapsed=%.3fs out=[%s] budget=%s",
                time.perf_counter() - started,
                out_keys,
                _live_budget(),
            )

    return wrapper
