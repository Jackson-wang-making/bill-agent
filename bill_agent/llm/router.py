"""模型路由：DeepSeek（默认 deepseek-chat，可选 deepseek-reasoner）。"""
from __future__ import annotations

from langchain_deepseek import ChatDeepSeek

from bill_agent.config import config


def get_llm(reasoner: bool = False, thinking: bool | None = None) -> ChatDeepSeek:
    """构造 ChatDeepSeek 实例。

    reasoner=True 用 reasoner 模型（ar_ap 等强推理）。
    thinking=False 显式关闭思考模式：DeepSeek 思考模式不支持 tool_choice，
    函数调用式结构化输出必须关闭它，否则会返回 400。
    """
    model = config.llm_reasoner if reasoner else config.llm_model
    kwargs = {}
    if thinking is False:
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    return ChatDeepSeek(
        model_name=model,
        api_key=config.llm_api_key or None,
        api_base=config.llm_api_base,
        temperature=config.llm_temperature,
        max_retries=2,
        **kwargs,
    )
