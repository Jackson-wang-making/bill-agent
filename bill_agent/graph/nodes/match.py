"""match_node：四规则匹配已有账单，定 CREATE/UPDATE/NO_CHANGE。"""
from __future__ import annotations

from typing import Any

from bill_agent.matching.engine import match_charges


def match_node(state: dict[str, Any]) -> dict[str, Any]:
    normalized = state.get("normalized_charges", [])
    doc_info = state.get("document_info", {})
    party = state.get("transaction_party", {})
    existing = state.get("existing_bills", [])
    results = match_charges(normalized, doc_info, party, existing)
    return {"match_results": results}
