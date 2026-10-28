"""确定性校验（零 LLM）：金额/币种/合计/OCR 纠错/重复。"""
from __future__ import annotations

import re
from typing import Any

# 常用 + 航运相关 ISO 4217 三位代码（子集足够覆盖业务）
ISO_4217: frozenset[str] = frozenset(
    """
    USD CNY HKD EUR GBP JPY KRW SGD AUD CAD CHF TWD MOP MYR THB VND IDR PHP INR
    AED SAR QAR KWD BHD OMR JOD ILS TRY RUB ZAR NGN KES EGP MAD NOK SEK DKK PLN
    CZK HUF RON BGN ISK MXN BRL ARS CLP COP PEN UYU NZD FJD XPF
    """.split()
)

_AMOUNT_RE = re.compile(r"^\d+(\.\d+)?$")
_OCR_SUSPICIOUS = re.compile(r"[OoIl]")


def is_valid_amount(v: Any) -> bool:
    """金额是否为有效数字（int/float 或可转 float 的纯数字串）。"""
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    if isinstance(v, str):
        return bool(_AMOUNT_RE.match(v.strip()))
    return False


def is_valid_currency(code: Any) -> bool:
    if code is None:
        # 题目要求：无法提取的字段使用 null。null 不是「非法币种」，
        # 不能因为缺失币种就逼模型在重试中编造一个币种。
        return True
    return isinstance(code, str) and code.upper() in ISO_4217


def has_ocr_ambiguity(v: Any) -> bool:
    """检测数字/编号字段里是否残留 OCR 的 0/O、1/l 混淆字符。"""
    if isinstance(v, str):
        # 含字母 O/l/I 且整体像数字/编号（含数字）时判定可疑
        if _OCR_SUSPICIOUS.search(v) and re.search(r"\d", v):
            return True
    return False


def _fuzzy_eq(a: Any, b: Any, tol: float = 0.01) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return str(a).strip().lower() == str(b).strip().lower()


def validate_totals(
    charges: list[dict[str, Any]], subtotal: Any, tax: Any, total: Any
) -> list[str]:
    """明细=小计、小计+税=总计 两条独立校验。"""
    issues: list[str] = []
    detail_sum = 0.0
    for c in charges:
        try:
            detail_sum += float(c.get("amount") or 0)
        except (TypeError, ValueError):
            continue  # 非法金额已由 is_valid_amount 单独报错，这里跳过
    if subtotal is not None and not _fuzzy_eq(detail_sum, subtotal):
        issues.append(f"明细合计 {detail_sum:.2f} != 小计 {subtotal}")
    if subtotal is not None and tax is not None and total is not None:
        if not _fuzzy_eq(float(subtotal) + float(tax), total):
            issues.append(f"小计 {subtotal} + 税 {tax} != 总计 {total}")
    return issues


def detect_duplicates(charges: list[dict[str, Any]]) -> list[str]:
    """检测同一费用是否被重复提取。"""
    seen: dict[str, int] = {}
    dups: list[str] = []
    for i, c in enumerate(charges):
        key = f"{c.get('charge_item')}|{c.get('currency')}|{c.get('amount')}"
        if key in seen:
            dups.append(f"费用项重复：{c.get('charge_item')} {c.get('currency')} {c.get('amount')}（第 {seen[key]+1} 与第 {i+1} 项）")
        else:
            seen[key] = i
    return dups


def validate_extraction(result: dict[str, Any]) -> tuple[list[str], str]:
    """校验提取结果，返回 (issues, status)。status: pass | fail。"""
    issues: list[str] = []

    charges = result.get("charges", [])
    for i, c in enumerate(charges):
        if not is_valid_amount(c.get("amount")):
            issues.append(f"费用项 {i+1} 金额非法: {c.get('amount')!r}")
        if not is_valid_currency(c.get("currency")):
            issues.append(f"费用项 {i+1} 币种非 ISO 4217: {c.get('currency')!r}")
        for field in ("amount", "quantity", "unit_price"):
            if has_ocr_ambiguity(c.get(field)):
                issues.append(f"费用项 {i+1} {field} 疑似 OCR 混淆: {c.get(field)!r}")

    # 注意：编号/提单号/订舱号/箱号字段本身可含字母（如 INV-001、BL123），
    # 自动 0/O、1/l 纠错会破坏合法编号，故只对纯数字字段（金额/数量/单价）做 OCR 检查。
    issues.extend(
        validate_totals(charges, result.get("subtotal"), result.get("tax"), result.get("total"))
    )
    issues.extend(detect_duplicates(charges))

    return issues, ("fail" if issues else "pass")
