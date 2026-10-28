"""错误处理：指数退避重试（retry）。

用于可恢复的瞬时错误（网络抖动、网关 5xx）。调用方必须同时给出重试上限，
避免与图内的 Evaluator-Optimizer 循环叠加成无限重试。
"""
from __future__ import annotations

import time
from typing import Callable, TypeVar

T = TypeVar("T")


def with_retry(
    fn: Callable[[], T],
    max_retries: int = 3,
    backoff: float = 2.0,
    retry_on: tuple[type[Exception], ...] = (Exception,),
    should_retry: Callable[[Exception], bool] | None = None,
) -> T:
    """调用 fn，失败指数退避重试；超限则抛出最后一次异常。

    `should_retry` 可选：对命中 `retry_on` 的异常做二次过滤。返回 False 的异常
    立即上抛（如 OCR 的 4xx 客户端错误不应被当成瞬时错误重试），返回 True 才
    进入退避重试。
    """
    last: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except retry_on as e:  # type: ignore[misc]
            if should_retry is not None and not should_retry(e):
                raise
            last = e
            if attempt < max_retries:
                time.sleep(backoff**attempt)
    assert last is not None
    raise last
