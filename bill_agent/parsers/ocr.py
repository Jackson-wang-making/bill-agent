"""OCR：调用指定 OCR API（先 stage 成可下载 URL）。

实测响应结构（gateway.pltplt.com/ai/process）：
  {"data": {"list": [{fileType, mblNumber, shipper, consignee, invoiceNumber,
                      chargeList, ctrList, hold, page_index, ...}], ...},
   "success": true, "message": "...", "error_code": ..., "task_id": ...}

因此不能把响应当纯文本读：必须逐条 doc 渲染字段 + 把 chargeList/ctrList 转成表格。
"""
from __future__ import annotations

import copy
import hashlib
import os
import tempfile
import threading
import time
from typing import Any, Iterable

import requests

from bill_agent.config import config
from bill_agent.harness.budget import BudgetExceeded, get_budget
from bill_agent.harness.error_handler import with_retry
from bill_agent.harness.redact import clean_error, redact_urls as _redact_urls
from bill_agent.parsers.stage import ocr_delivery_blocker, stage_local_file

# 期望是 True / 0 的组合；其余视为失败
_OK_ERROR_CODES = (None, 0, "0", 200, "200")
# 网关 message/list 先截窗口再扫描：窗口必须远大于任何真实本地路径
# （Windows 扩展路径上限 32,767，POSIX PATH_MAX 4,096）。窗口太小会把
# bill_att_/bill_doc_ marker 切到窗口外，路径前缀就成了「没有 marker 的普通
# 文本」漏过脱敏（round 13：2,100 字组件把 marker 推到 2000 字外，泄漏
# C:\Users\<用户>）。窗口只用于限制不可信文本的扫描成本。
_SCAN_MAX = 200_000

# OCR 结果缓存：键 = (文件内容哈希, 路径)，值 = (写入时间, 解析文档深拷贝)。
# 命中缓存不调网关、不扣 OCR 预算；TTL 过期自动淘汰。完整内容参与哈希，
# 同名不同内容的文件不会互相污染。
_ocr_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_ocr_cache_lock = threading.Lock()


def _clean_excerpt(value: Any, limit: int) -> str:
    """不可信响应片段的审计摘要：截窗口（保住 marker）→ 脱敏 → 限长。"""
    return clean_error(str(value)[:_SCAN_MAX])[:limit]


def _file_content_hash(path: str) -> str:
    """文件完整内容哈希：缓存键必须覆盖全文，不能只用路径或前 N 字节。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _ocr_cache_get(path: str) -> list[dict[str, Any]] | None:
    """缓存命中返回解析文档深拷贝；未命中或过期返回 None。"""
    if not getattr(config, "ocr_cache_enabled", True):
        return None
    key = (_file_content_hash(path), str(path))
    with _ocr_cache_lock:
        entry = _ocr_cache.get(key)
        if entry is None:
            return None
        stored_at, docs = entry
        if time.time() - stored_at > getattr(config, "ocr_cache_ttl_seconds", 3600):
            _ocr_cache.pop(key, None)
            return None
        return copy.deepcopy(docs)


def _ocr_cache_put(path: str, docs: list[dict[str, Any]]) -> None:
    """缓存一次成功的 OCR 解析结果（只缓存成功，不缓存降级/失败）。"""
    if not getattr(config, "ocr_cache_enabled", True):
        return
    key = (_file_content_hash(path), str(path))
    with _ocr_cache_lock:
        _ocr_cache[key] = (time.time(), copy.deepcopy(docs))


def _docs_from_items(items: Any, path: str, source_url: str) -> list[dict[str, Any]]:
    """把网关 data.list 映射为解析文档，跳过非 dict 与无可提取内容的条目。"""
    if not isinstance(items, list):
        return []
    docs: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        doc = _item_to_doc(item, str(path), source_url)
        if _doc_has_content(doc):
            docs.append(doc)
    return docs


def _render_downscaled(path: str) -> str | None:
    """把 PDF/图片渲染成降采样 PNG，供二次 OCR；失败返回 None。"""
    try:
        import pymupdf

        scale = float(getattr(config, "ocr_degrade_scale", 0.5) or 0.5)
        doc = pymupdf.open(path)
        try:
            pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(scale, scale))
        finally:
            doc.close()
        fd, out = tempfile.mkstemp(prefix="bill_ocr_degrade_", suffix=".png")
        os.close(fd)
        pix.save(out)
        return out
    except Exception:  # noqa: BLE001 - 降级失败由调用方保持原错误
        return None


def _degraded_retry(path: str, original_error: str) -> list[dict[str, Any]] | None:
    """扫描件/图片 OCR 失败后降采样二次识别；失败返回 None，调用方保持原错误。"""
    if not getattr(config, "ocr_degrade_enabled", True):
        return None
    budget = get_budget()
    try:
        budget.reserve_ocr()
    except BudgetExceeded:
        return None

    degraded_path = _render_downscaled(path)
    if degraded_path is None:
        budget.release_ocr()
        return None
    try:
        try:
            file_url = stage_local_file(degraded_path)
        except Exception:  # noqa: BLE001 - staging 失败没有发起 OCR，回滚额度
            budget.release_ocr()
            return None
        try:
            result = call_ocr(file_url)
        except Exception:  # noqa: BLE001 - call_ocr 已消耗尝试，不回滚（与原语义一致）
            return None
    finally:
        try:
            os.unlink(degraded_path)
        except OSError:
            pass

    if not _is_success(result):
        return None
    data = result.get("data") if isinstance(result, dict) else None
    items = data.get("list") if isinstance(data, dict) else None
    docs = _docs_from_items(items, str(path), file_url)
    if not docs:
        return None
    for d in docs:
        d.setdefault("meta", {})["ocr_degraded_retry"] = True
        d["meta"]["ocr_degraded_reason"] = clean_error(original_error)[:300]
    return docs


def _recover(path: str, meta: dict[str, Any]) -> list[dict[str, Any]] | None:
    """降级链：PDF 文本层 → 降采样二次 OCR。返回 None 表示无法恢复。"""
    doc = _fallback_pdf_text(path, meta)
    if doc is not None:
        return [doc]
    reason = meta.get("ocr_error") or meta.get("ocr_skipped") or "OCR 失败"
    return _degraded_retry(path, reason)


def _supplement_pdf_text(path: str, docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OCR 成功但 PDF 有文本层时，追加本地全文作为补充上下文。

    网关可能只返回部分页/部分字段（真实样例漏掉了 CUR/USD）；文本层能补回
    币种与后续页内容。OCR 仍是主结果，补充 doc 单独标注 ocr_supplement。
    """
    if not str(path).lower().endswith(".pdf"):
        return docs
    if not getattr(config, "ocr_pdf_text_supplement", True):
        return docs
    from bill_agent.parsers.dispatcher import _inspect_pdf
    from bill_agent.parsers.pdf_text import parse_pdf_text

    has_text, err = _inspect_pdf(path)
    if err is not None or not has_text:
        return docs
    try:
        supplement = parse_pdf_text(path)
    except Exception:  # noqa: BLE001 - 补充失败不影响 OCR 主结果
        return docs
    if not (supplement.get("raw_text") or "").strip() and not supplement.get("tables"):
        return docs
    supplement.setdefault("meta", {})["ocr_supplement"] = True
    return [*docs, supplement]


def _is_transient_ocr_error(e: Exception) -> bool:
    """只有网关瞬时故障才值得重试：5xx / 连接失败 / 超时；4xx 是客户端错误。"""
    if isinstance(e, requests.exceptions.HTTPError):
        resp = getattr(e, "response", None)
        return resp is not None and resp.status_code >= 500
    return isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout))


def call_ocr(file_url: str, task: str | None = None) -> dict:
    """调用 gateway.pltplt.com/ai/process，file 为服务端可访问的下载地址。

    瞬时错误（5xx/连接/超时）按配置指数退避重试；4xx 客户端错误立即上抛，
    不把次数浪费在不可能成功的请求上。
    """
    url = config.ocr_url
    data = {
        "file": file_url,
        "task": task or config.ocr_task,
        "callBack": "",
        "taskType": "",
    }

    def _post() -> dict:
        resp = requests.post(url, json=data, timeout=config.ocr_timeout)
        resp.raise_for_status()
        return resp.json()

    return with_retry(
        _post,
        max_retries=config.max_ocr_retries,
        backoff=config.ocr_retry_backoff,
        retry_on=(requests.exceptions.RequestException,),
        should_retry=_is_transient_ocr_error,
    )


def _is_success(result: dict) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("success") is False:
        return False
    return result.get("error_code") in _OK_ERROR_CODES


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return str(v)
    return str(v)


def _rows_to_table(rows: Any) -> list[list[str]] | None:
    """把 chargeList/ctrList 这类异构列表转成二维表（首行表头）；非 dict 行不丢弃。"""
    if not isinstance(rows, list) or not rows:
        return None
    if isinstance(rows[0], dict):
        headers: list[str] = []
        for r in rows:
            if isinstance(r, dict):
                for k in r:
                    if k not in headers:
                        headers.append(k)
        out = [headers]
        for r in rows:
            if isinstance(r, dict):
                out.append([_cell(r.get(h)) for h in headers])
            else:
                # 非 dict 行并入表格（单列原文），内容不得静默消失
                out.append([f"（无法结构化）{_cell(r)}"])
        return out
    out: list[list[str]] = []
    for r in rows:
        if isinstance(r, (list, tuple)):
            out.append([_cell(c) for c in r])
        else:
            out.append([_cell(r)])
    return out


# 网关把检索标记编码成 Y/N 或 True/False，这些字段本身不构成单据内容
_MARKER_KEYS = frozenset({"suspicious", "isDetention", "isDemurrage", "requiresReview"})
# fileType 只是单据类型标签：没有其它字段时送 LLM 也提不出任何数据，
# 反而会遮蔽 PDF 自带文本层（有文本层应降级本地提取）
_CONTENT_IGNORED_KEYS = frozenset({"fileType"})
# chargeList/ctrList 的行只按真实字段判断内容：标记、类型标签、页码都不是费用数据
_ROW_IGNORED_KEYS = _MARKER_KEYS | _CONTENT_IGNORED_KEYS | {"page_index"}
_FLAG_VALUES = frozenset({"y", "n", "yes", "no", "true", "false"})


def _meaningful(value: Any) -> bool:
    """字段值是否有真实内容：空串/空容器/叶子全空的 dict（如 hold）都算空。"""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_meaningful(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_meaningful(v) for v in value)
    return True


def _meaningful_content(value: Any) -> bool:
    """内容判定（比 _meaningful 严格）：布尔与 Y/N 检索标记不算单据内容。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        s = value.strip()
        return bool(s) and s.lower() not in _FLAG_VALUES
    if isinstance(value, dict):
        return any(_meaningful_content(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_meaningful_content(v) for v in value)
    return value is not None


def _item_has_content(item: dict[str, Any]) -> bool:
    """条目是否有可提取内容。

    必须按真实网关结构判断（tests/fixtures/ocr_response_real.json）：
    suspicious/isDetention/isDemurrage 是 'N' 字符串，hold 是
    {'holdType': '', ...} 这类叶子全空的 dict；isReefer 等布尔/Y-N 标记与
    fileType 标签本身也不构成内容。只含这些的条目送 LLM 只会产生噪音，
    应视为「网关未识别出内容」。
    """
    for k, v in item.items():
        if (
            k in ("chargeList", "ctrList", "page_index")
            or k in _MARKER_KEYS
            or k in _CONTENT_IGNORED_KEYS
        ):
            continue
        if _meaningful_content(v):
            return True
    for key in ("chargeList", "ctrList"):
        rows = item.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict):
                if any(_meaningful_content(v) for k, v in row.items() if k not in _ROW_IGNORED_KEYS):
                    return True
            elif _meaningful_content(row):
                return True
    return False


def _scalar_lines(item: dict[str, Any]) -> Iterable[str]:
    """渲染单据级字段（跳过空值；chargeList/ctrList 单独转表格）。"""
    for k, v in item.items():
        if k in ("chargeList", "ctrList", "page_index"):
            continue
        if not _meaningful(v):
            continue
        # other/hold 等非空 dict/list 保留原文（LLM 上下文与审计轨迹都必须可见）
        yield f"{k}: {v}"


def _doc_has_content(doc: dict[str, Any]) -> bool:
    """条目是否含真实内容（优先按原始条目判断，缺失时退回渲染文本）。"""
    item = (doc.get("meta") or {}).get("ocr_raw")
    if isinstance(item, dict):
        return _item_has_content(item)
    for line in doc["raw_text"].splitlines():
        s = line.strip()
        if not s or s.startswith("---"):
            continue
        key, _, value = s.partition(":")
        if key.strip() in _MARKER_KEYS or key.strip() in _CONTENT_IGNORED_KEYS:
            continue
        if value.strip().lower() in ("true", "false", "y", "n", "yes", "no"):
            continue
        return True
    # 表头行不算内容：只有数据行（第 2 行起）出现非空单元格才算
    return any(
        any(str(c).strip() for c in row)
        for table in doc["tables"]
        for row in table[1:]
    )


def _item_to_doc(item: dict[str, Any], path: str, source_url: str) -> dict[str, Any]:
    charges = item.get("chargeList") or []
    containers = item.get("ctrList") or []
    tables = [t for t in (_rows_to_table(charges), _rows_to_table(containers)) if t]

    lines = list(_scalar_lines(item))
    if containers:
        lines.append("--- 箱信息 ---")
        for row in _rows_to_table(containers) or []:
            lines.append(" | ".join(row))
    if charges:
        lines.append("--- 费用明细 ---")
        for row in _rows_to_table(charges) or []:
            lines.append(" | ".join(row))

    return {
        "type": "ocr",
        "source_file": path,
        "raw_text": "\n".join(lines),
        "tables": tables,
        "meta": {
            "ocr_file_type": item.get("fileType"),
            "ocr_suspicious": item.get("suspicious"),
            # 页码参与 fan-in 去重签名：同文件多页的前 200 字符前缀相同时不得被丢弃
            "ocr_page_index": item.get("page_index"),
            "ocr_raw": item,
            "file_url": source_url,
        },
    }


def _fallback_pdf_text(path: str, meta: dict[str, Any]) -> dict[str, Any] | None:
    """OCR 不可用时，含文本层的 PDF 降级为本地文本提取（非 OCR 结果，同样过校验门）。"""
    if not str(path).lower().endswith(".pdf"):
        return None
    from bill_agent.parsers.dispatcher import _inspect_pdf
    from bill_agent.parsers.pdf_text import parse_pdf_text

    has_text, err = _inspect_pdf(path)
    if err is not None:
        # 打不开的 PDF 是真实根因：不能因为 OCR 不可用就改报「staging 不可达」
        raise RuntimeError(f"PDF 无法打开：{err}")
    if not has_text:
        return None
    doc = parse_pdf_text(path)
    doc.setdefault("meta", {}).update(meta)
    return doc


def parse_ocr(path: str) -> list[dict]:
    """扫描 PDF / 图片：stage 成 URL → 调 OCR → 按真实响应结构映射。

    返回 parsed_documents 列表（响应 data.list 可能有多条）。
    """
    blocker = ocr_delivery_blocker()
    if blocker is not None:
        # 配置不可用不发网络请求，也不占 OCR 预算（否则多次纯配置失败会耗尽额度、
        # 把可操作的 staging 提示替换成误导的「预算上限」错误）。错误消息带上具体
        # 缺口（缺凭据 / 缺公网地址），不能一律说「地址不可达」。
        hint = f"OCR 未启用：{blocker}"
        doc = _fallback_pdf_text(path, {"ocr_skipped": hint})
        if doc is not None:
            return [doc]
        raise RuntimeError(hint)

    cached = _ocr_cache_get(path)
    if cached is not None:
        # 命中缓存：不 stage、不调网关、不扣预算。审计里明确标注缓存命中，
        # 且不携带上一轮的预签名 URL（已过期，只会误导审计）。
        for d in cached:
            d["source_file"] = str(path)
            d.setdefault("meta", {})["ocr_cached"] = True
            d["meta"]["file_url"] = None
        return cached

    budget = get_budget()
    # 预算内原子占位后再发起网络调用：并行分支共享计数器时，
    # check-then-record 会让多个分支同时通过检查
    try:
        budget.reserve_ocr()
    except BudgetExceeded as e:
        # 预算耗尽的 PDF 仍可用本地文本层（图片没有文本层，照常上抛）
        doc = _fallback_pdf_text(path, {"ocr_error": clean_error(str(e)), "ocr_fallback": "pdf_text"})
        if doc is not None:
            return [doc]
        raise

    try:
        file_url = stage_local_file(path)
    except Exception as e:  # noqa: BLE001 - staging 失败：PDF 可降级，图片转人工
        # staging 失败没有发起 OCR 调用：回滚额度，否则后面的文件会误报
        # 「预算上限」，把真正的 staging/配置错误完全遮蔽
        budget.release_ocr()
        # meta 提示与 parse_errors 同标准脱敏：这条分支不再经过 _clean_error，
        # 临时路径与 URL 凭据必须在源头清掉
        recovered = _recover(path, {"ocr_error": clean_error(str(e)), "ocr_fallback": "pdf_text"})
        if recovered is not None:
            return recovered
        raise

    try:
        result = call_ocr(file_url)
    except Exception as e:  # noqa: BLE001 - 网络/网关失败：PDF 可降级，图片转人工
        recovered = _recover(path, {"ocr_error": clean_error(str(e)), "ocr_fallback": "pdf_text"})
        if recovered is not None:
            return recovered
        raise

    if not _is_success(result):
        # 网关可能返回非 dict（如 JSON 数组）：不能因 .get 抛出误导的 AttributeError
        raw = result.get("message") or result if isinstance(result, dict) else str(result)
        # 网关 message 长度不可控：只扫描有界窗口，但窗口必须保住 marker（见 _SCAN_MAX）
        msg = f"OCR 返回失败: {_clean_excerpt(raw, 500)}"
        recovered = _recover(path, {"ocr_error": msg, "ocr_fallback": "pdf_text"})
        if recovered is not None:
            return recovered
        raise RuntimeError(msg)

    data = result.get("data") if isinstance(result, dict) else None
    items = data.get("list") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        hint = "OCR 成功但未返回 list 内容（网关未识别出单据）"
        recovered = _recover(path, {"ocr_error": hint, "ocr_fallback": "pdf_text"})
        if recovered is not None:
            return recovered
        raise RuntimeError(hint)

    docs = _docs_from_items(items, str(path), file_url)
    if not docs:
        # data.list 非空但无任何有效条目：PDF 有文本层则降级本地提取，
        # 不能因为网关返回垃圾条目就丢掉真实内容；无文本层才报错转人工
        hint = (
            f"OCR 响应 data.list 无有效单据条目（{len(items)} 个条目均无可提取内容）: "
            f"{_clean_excerpt(items, 200)}"
        )
        recovered = _recover(path, {"ocr_error": hint, "ocr_fallback": "pdf_text"})
        if recovered is not None:
            return recovered
        raise RuntimeError(hint)
    skipped = len(items) - len(docs)
    if skipped:
        # 混合 list：非 dict / 空条目可能是网关未能结构化的单据，打标记进审计轨迹，不静默丢弃
        docs[0].setdefault("meta", {})["ocr_skipped_items"] = skipped
    docs = _supplement_pdf_text(str(path), docs)
    _ocr_cache_put(path, docs)
    return docs
