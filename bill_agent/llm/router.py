"""模型路由：DeepSeek（默认 deepseek-chat，可选 deepseek-reasoner）。"""
from __future__ import annotations

from langchain_deepseek import ChatDeepSeek

from bill_agent.config import config


def get_llm(reasoner: bool = False) -> ChatDeepSeek:
    """构造 ChatDeepSeek 实例。reasoner=True 用 deepseek-reasoner（ar_ap 等强推理）。"""
    model = config.llm_reasoner if reasoner else config.llm_model
    return ChatDeepSeek(
        model_name=model,
        api_key=config.llm_api_key or None,
        api_base=config.llm_api_base,
        temperature=config.llm_temperature,
        max_retries=2,
    )
