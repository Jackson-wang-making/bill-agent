"""output_node：把 match_results 组装成标准输出 JSON。

- CREATE：一条费用一条记录
- UPDATE：字段级继承（仅用输入中非空且变化的字段覆盖旧值，其余保留）
- NO_CHANGE：原样输出已有账单（幂等）
- PENDING_REVIEW：多账单命中同一费用的冲突，输出提取值供人工裁决
"""
from __future__ import annotations

from typing import Any

from bill_agent.config import config
from bill_agent.extraction.extractor import doc_snippet, parse_hints

_FIELDS = (
    "charge_item",
    "original_charge_item",
    "amount",
    "currency",
    "payment_term",
    "quantity",
    "unit_price",
)


def _pick(charge: dict[str, Any]) -> dict[str, Any]:
    return {k: charge.get(k) for k in _FIELDS}


def _merge_update(charge: dict[str, Any], bill: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """字段级继承：输入非空且与旧值不同才覆盖，其余保留旧值。"""
    merged = {k: bill.get(k) for k in _FIELDS}
    changed: list[str] = []
    for k in _FIELDS:
        if charge.get(k) is not None and charge.get(k) != bill.get(k):
            merged[k] = charge[k]
            changed.append(k)
    return merged, changed


def _eval_reasoning(
    operation: str, mr: dict[str, Any], charge: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """返回 (charge_details, ai_reasoning)。"""
    bill = mr.get("matched_bill") or {}

    if operation == "NO_CHANGE":
        return _pick(bill), [
            f"通过{mr.get('rule')}匹配到已有账单，关键字段一致，无变化（幂等）"
        ]
    if operation == "UPDATE":
        details, changed = _merge_update(charge, bill)
        changed_str = "、".join(changed) if changed else "无"
        return details, [
            f"通过{mr.get('rule')}匹配到已有账单",
            f"更新字段：{changed_str}；其余字段保留原值（字段级继承）",
        ]
    if operation == "PENDING_REVIEW":
        reason = mr.get("conflict_reason") or (
            f"数据冲突：{mr.get('rule')}命中 {len(mr.get('candidates') or [])} 张已有账单，无法唯一确定"
        )
        return _pick(charge), [
            reason,
            f"候选账单：{mr.get('candidates')}",
            "已输出本次提取值，转人工裁决",
        ]
    # CREATE
    return _pick(charge), [
        "未匹配到已有账单，新增记录",
        f"费用名标准化：{charge.get('original_charge_item')} → {charge.get('charge_item')}",
    ]


def output_node(state: dict[str, Any]) -> dict[str, Any]:
    match_results = state.get("match_results", [])
    doc_info = state.get("document_info", {})
    party = state.get("transaction_party", {})
    entity = state.get("accounting_entity") or config.accounting_entity or None
    posting_type = state.get("posting_type")
    ar_ap_reasoning = state.get("ar_ap_reasoning", "")
    docs = state.get("parsed_documents", [])
    source_file = docs[0].get("source_file") if docs else None
    original_text = doc_snippet(docs)
    # original_text 有 500 字截断：解析降级/跳过提示同时进 ai_reasoning，审计不因截断失真
    hints = parse_hints(docs)

    records: list[dict[str, Any]] = []
    for mr in match_results:
        operation = mr["operation"]
        charge = mr["charge"]  # 标准化后的费用项（含 original_charge_item）

        charge_details, reasoning = _eval_reasoning(operation, mr, charge)
        reasoning.extend(mr.get("notes") or [])
        if hints:
            reasoning.extend(hints)
        if ar_ap_reasoning:
            reasoning.append(f"AR/AP：{ar_ap_reasoning}")

        records.append(
            {
                "operation": operation,
                "document_info": {
                    "document_type": doc_info.get("document_type"),
                    "reference_no": doc_info.get("reference_no"),
                    "document_date": doc_info.get("document_date"),
                },
                "transaction_party": {
                    "issuer": party.get("issuer"),
                    "recipient": party.get("recipient"),
                },
                "charge_details": charge_details,
                "financial_attributes": {
                    "accounting_entity": entity,
                    "posting_type": posting_type,
                },
                "audit_trail": {
                    "source_file": source_file,
                    "original_text": original_text,
                    "ai_reasoning": reasoning,
                },
            }
        )

    # 部分文件解析失败（其余正常出单）：失败文件补一条 PENDING_REVIEW，错误不可吞掉
    parse_errors = state.get("parse_errors") or []
    if parse_errors:
        from bill_agent.graph.nodes.pending_review import (
            _parse_error_lines,
            make_pending_record,
        )

        for err in parse_errors:
            records.append(
                make_pending_record(
                    state,
                    source_file=err.get("file"),
                    reasoning=_parse_error_lines([err]),
                    original_text=None,
                )
            )

    return {"output_records": records}
