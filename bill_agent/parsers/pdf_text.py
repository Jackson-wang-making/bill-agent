"""含文本层 PDF：直接提取文字与表格（免 OCR）。"""
from __future__ import annotations

import pymupdf

from bill_agent.harness.redact import clean_error


def parse_pdf_text(path: str) -> dict:
    doc = pymupdf.open(path)
    pages: list[str] = []
    tables: list[list[list[str]]] = []
    table_errors: list[str] = []
    try:
        for page in doc:
            pages.append(page.get_text())
            try:
                for table in page.find_tables().tables:
                    rows = table.extract()
                    if rows:
                        tables.append([[str(c) if c is not None else "" for c in row] for row in rows])
            except Exception as e:  # noqa: BLE001 - 表格失败不阻断正文，但必须留痕
                table_errors.append(str(e))
    finally:
        doc.close()

    meta: dict = {}
    if table_errors:
        # table_error 会进 LLM prompt 与 ai_reasoning（不经 parse_errors 收口），
        # 临时路径/URL 凭据必须在源头脱敏
        meta["table_error"] = clean_error("; ".join(table_errors))[:300]
    return {
        "type": "pdf_text",
        "source_file": str(path),
        "raw_text": "\n".join(pages),
        "tables": tables,
        "meta": meta,
    }
