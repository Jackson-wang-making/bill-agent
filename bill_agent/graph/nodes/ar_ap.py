"""ar_ap_node：AR/AP 判定。

题目硬性要求：不能仅凭 Debit Note / Credit Note 字面判断，
必须结合「当前记账主体 + 签发方 + 接收方 + 收费方向」。

实现分三层：
1. 确定性方向：先判断记账主体在单据中的角色（签发方 / 接收方），
   再结合单据类型推出方向（签发方开 Debit Note 请求付款 → AR）。
2. LLM 佐证：把角色、双方、单据类型、原文片段给模型，要求一行
   `AR|AP|UNCERTAIN|理由`，用于处理特殊文本（如 "BALANCE DUE TO ..."）。
3. 兜底：记账主体不是交易双方 → 无法判定 → posting_type=None（转人工）；
   LLM 与确定性方向冲突 → 同样转人工，不赌。
"""
from __future__ import annotations

from typing import Any

from bill_agent.config import config
from bill_agent.extraction.extractor import doc_snippet
from bill_agent.llm.router import get_llm

_PROMPT = """你是航运财务专家。判断这笔费用从【记账主体】的视角是应收(AR)还是应付(AP)。

判定规则（必须同时满足，不能只看单据类型）：
- 记账主体是签发方：发出 Debit Note（要求对方付款）→ AR；发出 Credit Note（需退款/冲销）→ AP。
- 记账主体是接收方：收到 Debit Note → AP；收到 Credit Note → AR。
- 若原文有 "BALANCE DUE TO X"、"BENEFICIARY: X" 等表述，以此确认收款方。
- 信息不足以判断时回答 UNCERTAIN，不要猜。

输入：
记账主体: {entity}
记账主体在单据中的角色: {role}
签发方: {issuer}
接收方: {recipient}
单据类型: {doc_type}
费用项: {charges}
原文片段:
{doc}

只输出一行：<AR|AP|UNCERTAIN>|<一句理由>"""


def _norm(s: str | None) -> str:
    return "".join(ch for ch in str(s or "").lower() if ch.isalnum())


def _role(entity: str, issuer: str | None, recipient: str | None) -> str | None:
    """记账主体在单据中的角色：issuer / recipient / None。"""
    e = _norm(entity)
    if not e:
        return None
    if issuer and (e in _norm(issuer) or _norm(issuer) in e):
        return "issuer"
    if recipient and (e in _norm(recipient) or _norm(recipient) in e):
        return "recipient"
    return None


def _direction(doc_type: str | None, role: str) -> str:
    """由角色 + 单据类型推出方向。"""
    is_credit = "credit" in str(doc_type or "").lower()
    if role == "issuer":
        return "AP" if is_credit else "AR"
    return "AR" if is_credit else "AP"


def _ask_llm(entity, role, issuer, recipient, doc_type, charges, doc) -> tuple[str | None, str]:
    prompt = _PROMPT.format(
        entity=entity,
        role=role,
        issuer=issuer,
        recipient=recipient,
        doc_type=doc_type,
        charges=[c.get("charge_item") for c in charges],
        doc=(doc or "")[:3000],
    )
    text = str(get_llm(reasoner=config.ar_ap_reasoner).invoke(prompt).content).strip()
    head, _, reason = text.partition("|")
    answer = head.strip().upper()
    if answer.startswith("AR"):
        answer = "AR"
    elif answer.startswith("AP"):
        answer = "AP"
    else:
        answer = None
    return answer, (reason.strip() or text)


def ar_ap_node(state: dict[str, Any]) -> dict[str, Any]:
    entity = state.get("accounting_entity") or config.accounting_entity or ""
    party = state.get("transaction_party", {})
    issuer = party.get("issuer")
    recipient = party.get("recipient")
    doc_type = (state.get("document_info") or {}).get("document_type")
    charges = state.get("normalized_charges", [])
    doc = doc_snippet(state.get("parsed_documents", []), limit=3000)

    if not entity:
        return {"posting_type": None, "ar_ap_reasoning": "未配置记账主体，无法判定 AR/AP"}

    role = _role(entity, issuer, recipient)
    if role is None:
        return {
            "posting_type": None,
            "ar_ap_reasoning": (
                f"记账主体「{entity}」不是单据签发方或接收方"
                f"（签发方={issuer}，接收方={recipient}），posting_type 未定，需人工补录"
            ),
        }

    expected = _direction(doc_type, role)

    try:
        answer, llm_reason = _ask_llm(entity, role, issuer, recipient, doc_type, charges, doc)
    except Exception:  # noqa: BLE001 - LLM 不可用：确定性方向仍成立
        return {
            "posting_type": expected,
            "ar_ap_reasoning": f"记账主体为{role}，按方向规则判定 {expected}（LLM 不可用，未做语义复核）",
        }

    if answer is None:
        return {
            "posting_type": None,
            "ar_ap_reasoning": f"模型无法确定 AR/AP（{llm_reason}），posting_type 未定，需人工补录",
        }

    if answer != expected:
        return {
            "posting_type": None,
            "ar_ap_reasoning": (
                f"规则判定 {expected}（记账主体为{role}）与模型判定 {answer} 冲突，"
                f"模型理由：{llm_reason}；posting_type 未定，需人工裁决补录"
            ),
        }

    return {"posting_type": expected, "ar_ap_reasoning": f"{expected}（记账主体为{role}）：{llm_reason}"}
