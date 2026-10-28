"""parse_* 节点：薄封装，调用确定性解析器，返回 parsed_documents / parse_errors。"""
from __future__ import annotations

from typing import Any

from bill_agent.harness.redact import clean_error as _clean_error
from bill_agent.harness.redact import redact_temp_paths as _redact_temp_paths
from bill_agent.parsers.eml import parse_eml as _parse_eml
from bill_agent.parsers.excel import parse_excel as _parse_excel
from bill_agent.parsers.ocr import parse_ocr as _parse_ocr
from bill_agent.parsers.pdf_text import parse_pdf_text as _parse_pdf_text
from bill_agent.parsers.word import parse_word as _parse_word


def _path(state: dict[str, Any]) -> str:
    return state.get("source_file", {}).get("path", "")


def _error(state: dict[str, Any], e: Exception) -> dict[str, Any]:
    return {"parse_errors": [{"file": _path(state), "error": _clean_error(str(e))}]}


def _promote_error_docs(state: dict[str, Any], docs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """错误 doc（EML 附件解析失败等）提升为 parse_errors：错误与文件名必须可追溯。

    file 用附件文件名而不是临时目录路径，本机路径不进入审计轨迹；
    被提升的 doc 不再进入提取流程（空文本喂 LLM 只会产生幻觉费用）。
    """
    errors: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for d in docs:
        meta = d.get("meta") or {}
        if d.get("type") == "error" or meta.get("error"):
            fname = meta.get("attachment_filename") or d.get("source_file") or _path(state)
            errors.append(
                {"file": str(fname), "error": _clean_error(str(meta.get("error") or "解析器返回错误文档"))}
            )
        else:
            kept.append(d)
    return kept, errors


def _single(state: dict[str, Any], docs: list[dict[str, Any]]) -> dict[str, Any]:
    """单文档解析器的统一收口：meta.error 的 doc（如 .doc 无 LibreOffice）提升为
    parse_errors，真实原因进审计轨迹，不能只剩「文件无内容可提取」的泛泛原因。"""
    kept, errors = _promote_error_docs(state, docs)
    return {"parsed_documents": kept, "parse_errors": errors}


def parse_eml(state: dict[str, Any]) -> dict[str, Any]:
    try:
        docs = _parse_eml(_path(state))
    except Exception as e:  # noqa: BLE001
        return _error(state, e)
    kept, errors = _promote_error_docs(state, docs)
    return {"parsed_documents": kept, "parse_errors": errors}


def parse_pdf_text(state: dict[str, Any]) -> dict[str, Any]:
    try:
        return _single(state, [_parse_pdf_text(_path(state))])
    except Exception as e:  # noqa: BLE001
        return _error(state, e)


def parse_ocr(state: dict[str, Any]) -> dict[str, Any]:
    try:
        return _single(state, _parse_ocr(_path(state)))
    except Exception as e:  # noqa: BLE001
        return _error(state, e)


def parse_excel(state: dict[str, Any]) -> dict[str, Any]:
    try:
        return _single(state, [_parse_excel(_path(state))])
    except Exception as e:  # noqa: BLE001
        return _error(state, e)


def parse_word(state: dict[str, Any]) -> dict[str, Any]:
    try:
        return _single(state, [_parse_word(_path(state))])
    except Exception as e:  # noqa: BLE001
        return _error(state, e)


def parse_unknown(state: dict[str, Any]) -> dict[str, Any]:
    return {"parse_errors": [{"file": _path(state), "error": "无法识别的文件类型"}]}
