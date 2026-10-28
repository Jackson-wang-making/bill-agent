"""结构化输出工厂。

实测（deepseek-chat）：
- method="function_calling" / "json_schema" → 严格按 schema 返回，Pydantic 直接校验通过
- method="json_mode" → 只保证「合法 JSON」，字段名由模型自造（document_no / parties ...），
  Pydantic 校验必然失败。因此不能用于本项目的强类型提取。

默认走 function_calling（兼容性最好）；deepseek-reasoner 不支持工具调用，
需要 reasoner 的场景（AR/AP 判定）本身就是纯文本推理，不走结构化输出。
"""
from __future__ import annotations

from typing import Type, TypeVar

from pydantic import BaseModel

from bill_agent.llm.router import get_llm

T = TypeVar("T", bound=BaseModel)


def structured_llm(schema: Type[T], reasoner: bool = False):
    """返回可调用 Runnable：invoke(messages) -> schema 实例（解析失败抛异常）。"""
    llm = get_llm(reasoner=reasoner)
    return llm.with_structured_output(schema, method="function_calling")
