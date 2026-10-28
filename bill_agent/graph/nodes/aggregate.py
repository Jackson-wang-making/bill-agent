"""aggregate_node：并行 fan-in 后统一入口。

parsed_documents 已由 reducer 去重；完整性判定放在 builder 的条件边
（无文档且有错误 → 直接 PENDING_REVIEW，不再空跑 LLM 重试）。
"""
from __future__ import annotations

from typing import Any


def aggregate_node(state: dict[str, Any]) -> dict[str, Any]:
    return {}
