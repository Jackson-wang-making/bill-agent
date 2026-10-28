"""extract_node 实现：LLM 结构化提取（DeepSeek JSON mode + Pydantic）。"""
from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from bill_agent.extraction.prompts import SYSTEM_PROMPT, build_user_prompt
from bill_agent.extraction.schemas import ExtractionResult
from bill_agent.harness.budget import BudgetExceeded, get_budget
from bill_agent.harness.redact import clean_error
from bill_agent.llm.structured import structured_llm

# 粗估 token：中文约 1.5 字符/token，取保守的 2 字符/token（只用于预算闸门，不用于计费）
_CHARS_PER_TOKEN = 2
# 单条提示上限：提示与真实正文共享 build_user_prompt 的 30k 截断，
# 一封含 4k 日历部件的邮件曾产生 56k 提示，把真实附件挤出 prompt
_HINT_MAX = 300


def _label(value: Any, limit: int = 80) -> str:
    """提示里的文件名/类型必须单行限长：RFC2231 文件名可带 %0A，能向 prompt 注入新行。"""
    s = re.sub(r"[\s\x00-\x1f\x7f]+", " ", str(value or "")).strip()
    return s[:limit]


def _join_limited(items: list[str], limit: int = 5, total: int | None = None) -> str:
    shown = items[:limit]
    text = "、".join(shown)
    if len(items) > limit:
        text += f" 等 {total if total is not None else len(items)} 个"
    return text


def _hint_text(value: Any, limit: int = 200) -> str:
    """meta 错误文本进提示前必须单行：网关 message 可带 \\n，会向 prompt 注入新行。"""
    return _label(clean_error(str(value)), limit)


def _doc_hints(d: dict[str, Any]) -> list[str]:
    """单文档的解析层提示：降级/跳过/失败都必须在 LLM 上下文与审计轨迹里可见。"""
    meta = d.get("meta") or {}
    hints: list[str] = []
    # meta 提示是 parse_errors 之外的第二条审计通道：同样按 URL+临时路径脱敏
    if meta.get("ocr_error"):
        hints.append(f"OCR 失败，已降级为本地 PDF 文本提取：{_hint_text(meta['ocr_error'])}")
    elif meta.get("ocr_skipped"):
        # 原文已含「未启用/降级」与具体配置缺口（OSS 凭据 / 公网地址）：原样进提示
        hints.append(_hint_text(meta["ocr_skipped"]))
    if meta.get("table_error"):
        hints.append(f"PDF 表格解析失败（正文仍提取）：{_hint_text(meta['table_error'])}")
    skipped = meta.get("ocr_skipped_items")
    if skipped:
        hints.append(f"OCR 响应中有 {skipped} 个条目无法结构化，已跳过")
    inline = meta.get("inline_cid_parts")
    if inline:
        # 文件名来自邮件头（不可信）：与 meta 错误同一脱敏标准，URL query 与
        # 临时路径不得借 hint 元数据绕过 clean_error 进入 prompt/审计轨迹
        names = _join_limited([_hint_text(p.get("filename") or "未命名", 80) for p in inline])
        # 总数写在前缀：300 字截断可能截掉名字列表，聚合信息不能跟着消失
        hints.append(f"邮件正文以 cid 引用的内联图片未作为附件解析（共 {len(inline)} 个）：{names}")
    ignored = meta.get("ignored_parts")
    if ignored:
        counts: dict[str, int] = {}
        for p in ignored:
            t = _hint_text(p.get("content_type") or "未知", 60)
            counts[t] = counts.get(t, 0) + 1
        types = _join_limited(
            [f"{t}×{n}" if n > 1 else t for t, n in counts.items()], total=len(ignored)
        )
        hints.append(f"邮件中的非单据部件未解析（签名/日历等，共 {len(ignored)} 个）：{types}")
    return [h[:_HINT_MAX] for h in hints]


def parse_hints(parsed_documents: list[dict[str, Any]]) -> list[str]:
    """所有文档的解析层提示（去重保序、条数封顶）。

    original_text 有 500 字截断，长正文时提示会被截掉：成功记录的
    ai_reasoning 也追加这些提示，故障不因截断而无声。
    """
    hints = list(dict.fromkeys(h for d in parsed_documents for h in _doc_hints(d)))
    if len(hints) > 50:
        hints = hints[:50] + [f"另有 {len(hints) - 50} 条解析提示未展示"]
    return hints


def docs_to_text(parsed_documents: list[dict[str, Any]]) -> str:
    """把 parsed_documents 拼成单一文本上下文（正文 + 表格 + 解析提示）。"""
    chunks: list[str] = []
    for d in parsed_documents:
        src = d.get("source_file", "")
        t = d.get("type", "")
        chunks.append(f"=== [{t}] {src} ===")
        if d.get("raw_text"):
            chunks.append(d["raw_text"])
        for i, table in enumerate(d.get("tables") or []):
            chunks.append(f"--- 表格 {i + 1} ---")
            for row in table:
                chunks.append(" | ".join(str(c) for c in row))
        for hint in _doc_hints(d):
            chunks.append(f"（解析提示：{hint}）")
    return "\n".join(chunks)


def doc_snippet(parsed_documents: list[dict[str, Any]], limit: int = 500) -> str | None:
    """正文 + 表格短片段，用于审计轨迹 original_text（xlsx 内容在 tables 中）。"""
    if not parsed_documents:
        return None
    return docs_to_text(parsed_documents)[:limit]


def extract(
    parsed_documents: list[dict[str, Any]],
    feedback: str | None = None,
    reasoner: bool = False,
) -> ExtractionResult:
    """单次结构化提取。解析失败抛异常，由调用方决定重试。"""
    doc_text = docs_to_text(parsed_documents)
    # 预算必须按真正发出去的 prompt 估算：build_user_prompt 会把文档截断到
    # 30000 字，用未截断的全文估算会让大文档在真正调用前就误报预算不足
    user_prompt = build_user_prompt(doc_text, feedback)

    budget = get_budget()
    estimated = max(1, (len(SYSTEM_PROMPT) + len(user_prompt)) // _CHARS_PER_TOKEN)
    if not budget.check_tokens(estimated):
        raise BudgetExceeded(
            f"token 预算不足：已用 {budget.tokens}，本次预估 {estimated}，上限 {budget.max_tokens}"
        )

    chain = structured_llm(ExtractionResult, reasoner=reasoner)
    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ]
    result = chain.invoke(messages)
    if result is None:
        # 防御：解析器/模型返回空结果时给出明确错误，交给上层重试或转人工，
        # 不能带着 None 去访问 model_dump_json。
        raise RuntimeError("模型未返回结构化提取结果（空响应）")
    budget.record_tokens(estimated + max(1, len(result.model_dump_json()) // _CHARS_PER_TOKEN))
    return result
