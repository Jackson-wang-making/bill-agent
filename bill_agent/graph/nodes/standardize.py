"""standardize_node：费用名/币种/付款方式/日期标准化，保留原始费用名。"""
from __future__ import annotations

from typing import Any

from bill_agent.standardize.normalizer import (
    normalize_charge_name,
    normalize_currency,
    normalize_date,
    normalize_payment_term,
    suggest_charge_name,
)


def standardize_node(state: dict[str, Any]) -> dict[str, Any]:
    raw_charges = state.get("extracted_charges", [])
    normalized = []
    for c in raw_charges:
        std, orig = normalize_charge_name(c.get("charge_item", ""))
        item = {
            "charge_item": std,
            "original_charge_item": orig,
            "amount": c.get("amount"),
            "currency": normalize_currency(c.get("currency", "")),
            "payment_term": normalize_payment_term(c.get("payment_term")),
            "quantity": c.get("quantity"),
            "unit_price": c.get("unit_price"),
            "confidence": c.get("confidence", 1.0),
        }
        if std == orig:
            suggestion = suggest_charge_name(c.get("charge_item", ""))
            if suggestion:
                item["suggested_charge_item"] = suggestion
        normalized.append(item)

    # 日期归一化必须在 match_node 之前（规则④按日期相等比较）
    doc_info = dict(state.get("document_info") or {})
    if doc_info.get("document_date"):
        doc_info["document_date"] = normalize_date(doc_info["document_date"])

    return {"normalized_charges": normalized, "document_info": doc_info}
