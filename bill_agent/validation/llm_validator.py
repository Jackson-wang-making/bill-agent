"""LLM 语义校验（补充）：合计语义、单据类型合理性、是否虚构字段。"""
from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from bill_agent.llm.router import get_llm

_PROMPT = """你是航运账单审计员。检查下面提取结果是否存在语义问题：
- 单据类型(document_type)是否合理（Debit Note / Credit Note / Invoice 等）
- 合计/小计/税额的语义是否自洽
- 是否有明显虚构或与原文矛盾的字段
只输出 "OK" 或一句简短的问题描述。"""


def semantic_check(result: dict, doc_text: str) -> str | None:
    """返回 None 表示无问题，否则返回问题描述。"""
    llm = get_llm()
    msg = [
        SystemMessage(content=_PROMPT),
        HumanMessage(content=f"提取结果：\n{result}\n\n原文片段：\n{doc_text[:6000]}"),
    ]
    resp = llm.invoke(msg)
    text = str(resp.content).strip()
    if not text:
        # 空/空白响应不等于「复核通过」：second_confirm 会据此转人工，
        # 否则中置信度单据会产出「从未复核」的 CREATE
        return "语义复核不可用：模型未返回任何内容"
    if text.lower() != "ok":
        return text
    return None
