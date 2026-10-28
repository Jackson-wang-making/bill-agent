"""route_node：按文件类型路由，用 Send 并行扇出到各 parse 节点。

作为 `add_conditional_edges(START, route_node, ...)` 的条件边函数返回 list[Send]。
"""
from __future__ import annotations

from typing import Any

from langgraph.types import Send

from bill_agent.parsers.dispatcher import classify

_TARGET = {
    "eml": "parse_eml",
    "pdf_text": "parse_pdf_text",
    "pdf_ocr": "parse_ocr",
    "image": "parse_ocr",
    "excel": "parse_excel",
    "word": "parse_word",
}


def route_node(state: dict[str, Any]) -> list[Send]:
    source_files = state.get("source_files", [])
    sends: list[Send] = []
    for f in source_files:
        route = classify(f.get("path", ""), f.get("mime"))
        target = _TARGET.get(route, "parse_unknown")
        sends.append(Send(target, {"source_file": {**f, "route": route}}))
    return sends
