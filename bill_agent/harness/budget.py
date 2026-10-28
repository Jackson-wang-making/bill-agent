"""全局 + 每节点 token/OCR 预算，超预算触发降级。"""
from __future__ import annotations

import contextvars
import threading

from bill_agent.config import config


class BudgetTracker:
    def __init__(self, max_tokens: int, max_ocr_calls: int) -> None:
        self.max_tokens = max_tokens
        self.max_ocr_calls = max_ocr_calls
        self.tokens = 0
        self.ocr_calls = 0
        # 并行分支线程共享同一实例，计数必须原子：
        # 并发分支的 check-then-record 会突破上限
        self._lock = threading.Lock()

    def check_tokens(self, estimated: int = 0) -> bool:
        """前置检查：预估消费后是否仍在预算内。"""
        with self._lock:
            return self.tokens + estimated <= self.max_tokens

    def record_tokens(self, n: int) -> None:
        with self._lock:
            self.tokens += n

    def reserve_ocr(self) -> None:
        """原子占位：先占用一次 OCR 额度再发起网络调用。

        不能 check 后再 record：并行分支线程共享计数器时，两个分支会同时通过检查。
        """
        with self._lock:
            if self.ocr_calls >= self.max_ocr_calls:
                raise BudgetExceeded(f"OCR 调用次数超过预算上限 {self.max_ocr_calls}")
            self.ocr_calls += 1

    def release_ocr(self) -> None:
        """回滚未真正发起的 OCR 占位（如 staging 失败）：额度不能被配置/网络错误吃掉。"""
        with self._lock:
            if self.ocr_calls > 0:
                self.ocr_calls -= 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {"tokens": self.tokens, "ocr_calls": self.ocr_calls}


# 运行上下文级预算（contextvar，不是线程局部）：LangGraph 的 Send 并行分支在线程池
# 里执行，contextvar 随上下文副本传递 —— 同一任务的所有分支共享一个上限；
# 并发任务（Web 线程池）各自独立，计数器不会跨任务累计。
_CURRENT: contextvars.ContextVar[BudgetTracker | None] = contextvars.ContextVar(
    "bill_budget", default=None
)


def get_budget() -> BudgetTracker:
    """当前运行上下文的预算实例（懒加载，读取当前配置）。"""
    budget = _CURRENT.get()
    if budget is None:
        budget = BudgetTracker(config.max_total_tokens, config.max_ocr_calls)
        _CURRENT.set(budget)
    return budget


def reset_budget() -> None:
    """新任务/测试开始时重置：为当前上下文（及其并行分支）创建全新预算。"""
    _CURRENT.set(BudgetTracker(config.max_total_tokens, config.max_ocr_calls))


def snapshot_budget() -> dict[str, int] | None:
    """当前上下文预算快照；未初始化返回 None（不隐式创建预算实例）。"""
    tracker = _CURRENT.get()
    return tracker.snapshot() if tracker is not None else None


class BudgetExceeded(RuntimeError):
    """预算耗尽：调用方应降级而非重试。"""
