"""validate_extract_node：代码校验 + 置信度路由；second_confirm_node：中等置信度二次确认。"""
from __future__ import annotations

from typing import Any

from bill_agent.extraction.extractor import docs_to_text
from bill_agent.validation.code_validators import validate_extraction
from bill_agent.validation.llm_validator import semantic_check


def validate_extract_node(state: dict[str, Any]) -> dict[str, Any]:
    charges = state.get("extracted_charges", [])
    result = {
        "document_info": state.get("document_info", {}),
        "charges": charges,
        "subtotal": state.get("subtotal"),
        "tax": state.get("tax"),
        "total": state.get("total"),
    }
    issues, status = validate_extraction(result)
    if not charges:
        issues.append("未提取到任何费用项")
        status = "fail"

    iteration = state.get("iteration", 0) + 1
    issue_dicts = [{"msg": i, "iteration": iteration} for i in issues]
    return {
        "validation_issues": issue_dicts,
        "validation_status": status,
        "extraction_issues": issue_dicts,  # 反馈给 extract 重试
        "iteration": iteration,
    }


def second_confirm_node(state: dict[str, Any]) -> dict[str, Any]:
    """中等置信度费用项做 LLM 语义复核；复核本身不可用时转人工，不得静默放行。"""
    doc_text = docs_to_text(state.get("parsed_documents", []))
    charges = state.get("extracted_charges", [])
    try:
        issue = semantic_check({"charges": charges}, doc_text)
        status = "pass" if issue is None else "fail"
    except Exception as e:  # noqa: BLE001 - 复核不可用不能当成通过：
        # 否则中等置信度单据会产出「看起来自动通过、实际从未复核」的 CREATE，
        # 审计轨迹上没有任何痕迹。转人工并写明真实原因
        issue, status = f"语义复核不可用: {e}", "fail"
    result: dict[str, Any] = {"second_confirm_status": status}
    if issue:
        # 复核发现的问题不能丢：validation_issues（add 累加器）直达 PENDING_REVIEW
        # 审计轨迹，否则只剩「低置信度或校验失败」的泛泛原因
        result["validation_issues"] = [{"msg": issue, "iteration": state.get("iteration", 0)}]
    return result
