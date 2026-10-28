"""BillState：LangGraph 全局状态（TypedDict）。

并行 fan-in 的 parsed_documents 用自定义 reducer 合并去重（避免 Send 结果重复累加）；
parse_errors / validation_issues 用 add 累加；extraction_issues 为当前轮反馈（替换）。
"""
from __future__ import annotations

import hashlib
from operator import add
from typing import Annotated, Any, Dict, List, Literal, TypedDict


def _tables_have_content(tables: Any) -> bool:
    """表格是否有任何非空单元格；空工作簿/全空表格（tables=[[]]）不算内容。"""
    return any(
        any(c is not None and str(c).strip() for c in (row or []))
        for table in (tables or [])
        for row in (table or [])
    )


def doc_has_content(doc: Dict[str, Any]) -> bool:
    """doc 是否有可提取内容（正文或非空表格）；空 doc 喂 LLM 只会产生幻觉费用。"""
    return bool((doc.get("raw_text") or "").strip()) or _tables_have_content(doc.get("tables"))


def _content_sig(d: Dict[str, Any]) -> str:
    """doc 的完整内容签名（raw_text + tables + OCR 页码）。

    去重键不能用 raw_text[:200] 前缀：excel doc 的 raw_text 恒为空串、多页 OCR 的
    前 200 字符可能相同，都会让内容不同的文档撞键被静默丢弃。表格与页码也参与签名。
    """
    h = hashlib.sha256()
    h.update((d.get("raw_text") or "").encode("utf-8", errors="replace"))
    h.update(repr(d.get("tables") or []).encode("utf-8", errors="replace"))
    page = (d.get("meta") or {}).get("ocr_page_index")
    if page is not None:
        h.update(f"|page:{page}".encode("utf-8"))
    return h.hexdigest()


def _merge_unique(a: List[Dict[str, Any]], b: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """并行 fan-in 时合并 parsed_documents，按 (source_file, 完整内容签名) 去重。

    只有「同文件 + 完全相同内容」才是真重复；内容不同的文档（跨 EML 的同名附件、
    同一文件的多页 OCR）必须全部保留，否则内容会从 LLM prompt 与审计轨迹中静默消失。
    """
    seen = set()
    out: List[Dict[str, Any]] = []
    for d in list(a or []) + list(b or []):
        key = (d.get("source_file"), _content_sig(d))
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


class BillState(TypedDict, total=False):
    # ---- 输入 ----
    source_files: List[Dict[str, str]]          # [{path, mime, type}]
    existing_bills: List[Dict[str, Any]]        # 已有账单 JSON
    accounting_entity: str                      # 记账主体（AR/AP 视角基准）
    config: Dict[str, Any]                      # 阈值/重试/模型/预算（运行时快照）

    # ---- 解析层（Routing 输出 + 并行 fan-in） ----
    route: str
    source_file: Dict[str, Any]                  # 单个文件（Send 扇出 payload）
    parsed_documents: Annotated[List[Dict[str, Any]], _merge_unique]
    parse_errors: Annotated[List[Dict[str, Any]], add]

    # ---- 提取层 ----
    document_info: Dict[str, Any]               # 单据类型/编号/日期 + bl_no/booking_no/container_no
    transaction_party: Dict[str, Any]           # issuer / recipient
    extracted_charges: List[Dict[str, Any]]     # 原始费用项（保留原始名），含 confidence
    extraction_confidence: Dict[str, Any]       # {"overall": 0.9, "per_charge": {...}}
    extraction_issues: List[Dict[str, Any]]     # 当前轮校验缺陷（反馈给 extract 重试，替换）
    subtotal: float
    tax: float
    total: float

    # ---- 标准化 ----
    normalized_charges: List[Dict[str, Any]]    # 标准名 + 原始名 并存

    # ---- 匹配 ----
    match_results: List[Dict[str, Any]]         # [{charge, matched_bill, rule, operation}]

    # ---- AR/AP ----
    posting_type: str                           # AR / AP / None
    ar_ap_reasoning: str

    # ---- 校验 ----
    validation_issues: Annotated[List[Dict[str, Any]], add]
    validation_status: Literal["pass", "fail"]
    second_confirm_status: Literal["pass", "fail"]

    # ---- 输出 ----
    output_records: List[Dict[str, Any]]
    final_status: Literal["success", "pending_review", "failed"]

    # ---- 控制（harness） ----
    iteration: int                              # 当前校验-重试轮次
    retry_count: int
    budget_consumed: Dict[str, int]             # {"tokens": n, "ocr_calls": n}
