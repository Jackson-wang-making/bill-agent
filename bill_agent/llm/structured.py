"""结构化输出工厂。

DeepSeek V4 系列在思考模式下不支持 tool_choice；即使关闭思考，tool call 仍偶发
返回空结果。因此结构化提取改走 JSON mode：显式把 Pydantic schema 写进 prompt，
要求返回 json_object，再由 PydanticOutputParser 做强类型校验。解析失败会抛异常，
交给图的 Evaluator-Optimizer 重试；不会静默产出空结果。

需要 reasoner 的场景（AR/AP 判定）本身就是纯文本推理，不走这里。
"""
from __future__ import annotations

from typing import Type, TypeVar

from langchain_core.messages import SystemMessage
from langchain_core.output_parsers import PydanticOutputParser, StrOutputParser
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel

from bill_agent.config import config
from bill_agent.llm.router import get_llm

T = TypeVar("T", bound=BaseModel)


def structured_llm(schema: Type[T], reasoner: bool = False):
    """返回可调用 Runnable：invoke(messages) -> schema 实例（解析失败抛异常）。

    thinking=False：思考模式不支持 tool_choice；JSON mode 也不依赖工具调用。
    """
    llm = get_llm(reasoner=reasoner, thinking=config.llm_structured_thinking)
    parser = PydanticOutputParser(pydantic_object=schema)
    instruction = parser.get_format_instructions()

    def _with_schema(messages):
        """把 schema 格式指令附加到系统消息（无系统消息则插到最前）。"""
        out = list(messages)
        for i, message in enumerate(out):
            if isinstance(message, SystemMessage):
                content = message.content if isinstance(message.content, str) else str(message.content)
                out[i] = SystemMessage(content=f"{content}\n\n{instruction}")
                return out
        out.insert(0, SystemMessage(content=instruction))
        return out

    return (
        RunnableLambda(_with_schema)
        | llm.bind(response_format={"type": "json_object"})
        | StrOutputParser()
        | parser
    )
