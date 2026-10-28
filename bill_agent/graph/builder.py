"""StateGraph 组装：五模式（Chaining + Routing + Parallelization + Evaluator-Optimizer）。

路由（条件边）：
  aggregate 无文档且有错误 → PENDING_REVIEW
  validate_extract → 重试 extract / 高置信直通 / 中置信二次确认 / 低置信转人工
  second_confirm → 通过标准化 / 失败转人工
  validate_final → 通过 END / 失败转人工
"""
from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from bill_agent.config import config
from bill_agent.graph.state import BillState, doc_has_content
from bill_agent.graph.nodes import (
    aggregate_node,
    ar_ap_node,
    extract_node,
    match_node,
    output_node,
    parse_eml,
    parse_excel,
    parse_ocr,
    parse_pdf_text,
    parse_unknown,
    parse_word,
    pending_review_node,
    route_node,
    second_confirm_node,
    standardize_node,
    validate_extract_node,
    validate_final_node,
)
from bill_agent.harness.logger import trace_node

_PARSE_NODES = (
    "parse_eml",
    "parse_pdf_text",
    "parse_ocr",
    "parse_excel",
    "parse_word",
    "parse_unknown",
)


def route_after_aggregate(state: dict[str, Any]) -> str:
    """没有任何可提取内容（无文档/文档全空）→ 直接转人工，不空跑 LLM（幻觉风险）。"""
    docs = state.get("parsed_documents") or []
    if not any(doc_has_content(d) for d in docs):
        return "pending_review_node"
    return "extract_node"


def route_after_validate_extract(state: dict[str, Any]) -> str:
    status = state.get("validation_status", "fail")
    conf = state.get("extraction_confidence", {}).get("overall", 0.0)
    iteration = state.get("iteration", 0)

    if status == "fail":
        return "extract_node" if iteration < config.max_extract_retries else "pending_review_node"
    if conf >= config.t_high:
        return "standardize_node"
    if conf >= config.t_low:
        return "second_confirm_node"
    return "pending_review_node"


def route_after_confirm(state: dict[str, Any]) -> str:
    return "standardize_node" if state.get("second_confirm_status") == "pass" else "pending_review_node"


def route_after_validate_final(state: dict[str, Any]) -> str:
    return END if state.get("validation_status") == "pass" else "pending_review_node"


def build_graph() -> StateGraph:
    g = StateGraph(BillState)

    # 主链 + 并行 fan-in：START 经条件边按类型扇出 Send 到 parse_*
    g.add_conditional_edges(START, route_node, list(_PARSE_NODES))
    g.add_node("parse_eml", trace_node("parse_eml", parse_eml))
    g.add_node("parse_pdf_text", trace_node("parse_pdf_text", parse_pdf_text))
    g.add_node("parse_ocr", trace_node("parse_ocr", parse_ocr))
    g.add_node("parse_excel", trace_node("parse_excel", parse_excel))
    g.add_node("parse_word", trace_node("parse_word", parse_word))
    g.add_node("parse_unknown", trace_node("parse_unknown", parse_unknown))
    g.add_node("aggregate_node", trace_node("aggregate_node", aggregate_node))
    g.add_node("extract_node", trace_node("extract_node", extract_node))
    g.add_node("validate_extract_node", trace_node("validate_extract_node", validate_extract_node))
    g.add_node("second_confirm_node", trace_node("second_confirm_node", second_confirm_node))
    g.add_node("standardize_node", trace_node("standardize_node", standardize_node))
    g.add_node("match_node", trace_node("match_node", match_node))
    g.add_node("ar_ap_node", trace_node("ar_ap_node", ar_ap_node))
    g.add_node("output_node", trace_node("output_node", output_node))
    g.add_node("validate_final_node", trace_node("validate_final_node", validate_final_node))
    g.add_node("pending_review_node", trace_node("pending_review_node", pending_review_node))

    for name in _PARSE_NODES:
        g.add_edge(name, "aggregate_node")
    g.add_conditional_edges("aggregate_node", route_after_aggregate)
    g.add_edge("extract_node", "validate_extract_node")
    g.add_conditional_edges("validate_extract_node", route_after_validate_extract)
    g.add_conditional_edges("second_confirm_node", route_after_confirm)
    g.add_edge("standardize_node", "match_node")
    g.add_edge("match_node", "ar_ap_node")
    g.add_edge("ar_ap_node", "output_node")
    g.add_edge("output_node", "validate_final_node")
    g.add_conditional_edges("validate_final_node", route_after_validate_final)
    g.add_edge("pending_review_node", END)

    return g
