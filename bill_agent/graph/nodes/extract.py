"""extract_node：LLM 结构化提取（DeepSeek JSON mode + Pydantic）。"""
from __future__ import annotations

from typing import Any

from bill_agent.extraction.extractor import extract as _extract


def extract_node(state: dict[str, Any]) -> dict[str, Any]:
    docs = state.get("parsed_documents", [])
    issues = state.get("extraction_issues", [])
    feedback = "\n".join(i.get("msg", str(i)) for i in issues) if issues else None

    try:
        result = _extract(docs, feedback=feedback)
    except Exception as e:  # noqa: BLE001 - LLM 调用/解析失败，交由校验门重试
        msg = f"LLM 提取异常: {e}"
        # 归属到紧随其后的校验轮次，转人工时只呈现最终轮次的问题
        issue = {"msg": msg, "iteration": state.get("iteration", 0) + 1}
        return {
            "extracted_charges": [],
            "document_info": {},
            "transaction_party": {},
            "extraction_confidence": {"overall": 0.0, "per_charge": {}},
            "subtotal": None,
            "tax": None,
            "total": None,
            "extraction_issues": [issue],
            # extraction_issues 会在校验节点被重试反馈覆盖，异常原因会随之消失；
            # validation_issues 是 add 累加器，把异常原因持久化进 PENDING_REVIEW 审计轨迹
            "validation_issues": [issue],
        }

    charges = [c.model_dump() for c in result.charges]
    return {
        "document_info": result.document_info.model_dump(),
        "transaction_party": result.transaction_party.model_dump(),
        "extracted_charges": charges,
        "extraction_confidence": {
            "overall": result.overall_confidence,
            "per_charge": {str(i): c["confidence"] for i, c in enumerate(charges)},
        },
        "subtotal": result.subtotal,
        "tax": result.tax,
        "total": result.total,
    }
