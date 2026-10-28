"""pending_review_node：低置信度/校验失败/解析失败时转人工，产出 PENDING_REVIEW 记录。

解析失败（parse_errors）必须把真实错误与文件名带进审计轨迹——
吞掉错误会让 OCR/OSS 配置故障伪装成「成功转人工」（验收：审计可追溯）。
"""
from __future__ import annotations

from typing import Any

from bill_agent.extraction.extractor import doc_snippet, parse_hints
from bill_agent.graph.nodes.parse import _clean_error
from bill_agent.graph.state import doc_has_content

_BLANK_CHARGE = {
    "charge_item": None,
    "original_charge_item": None,
    "amount": None,
    "currency": None,
    "payment_term": None,
    "quantity": None,
    "unit_price": None,
}


def _parse_error_lines(parse_errors: list[dict[str, Any]]) -> list[str]:
    return [f"解析失败（{e.get('file')}）：{e.get('error')}" for e in parse_errors]


def make_pending_record(
    state: dict[str, Any],
    source_file: str | None,
    reasoning: list[str],
    original_text: str | None,
) -> dict[str, Any]:
    """PENDING_REVIEW 记录的统一形状（output_node 部分失败时也复用）。"""
    doc_info = state.get("document_info", {})
    party = state.get("transaction_party", {})
    return {
        "operation": "PENDING_REVIEW",
        "document_info": {
            "document_type": doc_info.get("document_type"),
            "reference_no": doc_info.get("reference_no"),
            "document_date": doc_info.get("document_date"),
        },
        "transaction_party": {
            "issuer": party.get("issuer"),
            "recipient": party.get("recipient"),
        },
        "charge_details": dict(_BLANK_CHARGE),
        "financial_attributes": {
            # 未配置记账主体时按题目要求输出 null，而不是空串
            "accounting_entity": state.get("accounting_entity") or None,
            "posting_type": None,
        },
        "audit_trail": {
            "source_file": source_file,
            "original_text": original_text,
            "ai_reasoning": reasoning,
        },
    }


def pending_review_node(state: dict[str, Any]) -> dict[str, Any]:
    parse_errors = state.get("parse_errors") or []
    issues = state.get("validation_issues") or state.get("extraction_issues") or []
    docs = state.get("parsed_documents", [])

    if parse_errors and not docs:
        # aggregate 短路路径：全部文件解析失败，错误与文件名必须可追溯
        record = make_pending_record(
            state,
            source_file=parse_errors[0].get("file"),
            reasoning=_parse_error_lines(parse_errors),
            original_text=None,
        )
        return {"output_records": [record], "final_status": "pending_review"}

    src = docs[0].get("source_file") if docs else None

    if not issues and not parse_errors and not any(doc_has_content(d) for d in docs):
        # 无解析错误但也没有任何可提取内容：空文本不喂 LLM，转人工并写明真实原因。
        # 解析层的降级/跳过提示（如纯内联 CID 图片邮件）放在最前，避免只剩
        # 「正文与附件均为空」而看不出为什么
        record = make_pending_record(
            state,
            source_file=src,
            reasoning=parse_hints(docs) + ["文件无内容可提取（正文与附件均为空），转人工复核"],
            original_text=None,
        )
        return {"output_records": [record], "final_status": "pending_review"}

    if state.get("validation_status") == "pass" and state.get("second_confirm_status") != "fail":
        # 校验已通过（低置信度转人工）：历史重试轮次的旧问题不代表最终状态
        issues = []
    elif issues:
        # 重试耗尽/复核失败：只保留最后一轮（最大 iteration）的问题，
        # 旧轮次的原因不得冒充最终原因
        latest = max((i.get("iteration") or 0) for i in issues)
        issues = [i for i in issues if (i.get("iteration") or 0) == latest]

    if issues:
        # extraction_issues/validation_issues 可能携带 LLM 异常原文（含 URL query/token），
        # 与 parse_errors 同收口：进审计轨迹前先脱敏；重复消息去重保序
        reasoning = list(dict.fromkeys(
            _clean_error(str(i.get("msg", str(i)))) for i in issues
        ))
    else:
        conf = (state.get("extraction_confidence") or {}).get("overall")
        if conf is not None:
            # 低置信度转人工：写明真实数值，不猜
            reasoning = [f"提取置信度 {conf:.2f} 未达自动通过阈值，转人工复核"]
        else:
            reasoning = ["低置信度或校验失败，转人工复核"]
    if parse_errors:
        # 校验失败路径且部分文件解析失败：错误行放最前，其余保留
        reasoning = _parse_error_lines(parse_errors) + reasoning

    record = make_pending_record(
        state,
        source_file=src,
        reasoning=reasoning,
        original_text=doc_snippet(docs),
    )
    return {"output_records": [record], "final_status": "pending_review"}
