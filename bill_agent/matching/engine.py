"""四规则匹配引擎。

规则优先级（命中即停）：
  ① 单据编号 + 费用名
  ② 提单号 + 费用名 + 币种
  ③ 订舱号 + 费用名 + 币种
  ④ 箱号 + 交易双方 + 日期 + 金额

同一规则命中多张账单 → 数据冲突，转 PENDING_REVIEW（不静默取第一条）。
"""
from __future__ import annotations

from typing import Any, Callable


def _eq(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return False
    return str(a).strip().lower() == str(b).strip().lower()


def _amount_eq(a: Any, b: Any) -> bool:
    try:
        return abs(float(a) - float(b)) < 0.01
    except (TypeError, ValueError):
        return _eq(a, b)


def _identical(charge: dict, bill: dict) -> bool:
    """匹配到账单且关键字段完全一致 → NO_CHANGE（幂等）。"""
    return (
        _amount_eq(charge.get("amount"), bill.get("amount"))
        and _eq(charge.get("currency"), bill.get("currency"))
        and _eq(charge.get("payment_term"), bill.get("payment_term"))
    )


def _summary(bill: dict[str, Any]) -> dict[str, Any]:
    """冲突时给人工看的最小账单摘要。"""
    return {
        "reference_no": bill.get("reference_no"),
        "charge_item": bill.get("charge_item"),
        "amount": bill.get("amount"),
        "currency": bill.get("currency"),
        "document_date": bill.get("document_date"),
    }


def _rules(
    charge: dict[str, Any],
    doc_info: dict[str, Any],
    party: dict[str, Any],
) -> list[tuple[str, Callable[[dict[str, Any]], bool]]]:
    """按优先级生成四个匹配谓词（键缺失时该规则整体不适用）。"""
    rules: list[tuple[str, Callable[[dict[str, Any]], bool]]] = []

    if doc_info.get("reference_no"):
        rules.append(
            (
                "rule1",
                lambda b: _eq(b.get("reference_no"), doc_info.get("reference_no"))
                and _eq(b.get("charge_item"), charge.get("charge_item")),
            )
        )
    if doc_info.get("bl_no"):
        rules.append(
            (
                "rule2",
                lambda b: _eq(b.get("bl_no"), doc_info.get("bl_no"))
                and _eq(b.get("charge_item"), charge.get("charge_item"))
                and _eq(b.get("currency"), charge.get("currency")),
            )
        )
    if doc_info.get("booking_no"):
        rules.append(
            (
                "rule3",
                lambda b: _eq(b.get("booking_no"), doc_info.get("booking_no"))
                and _eq(b.get("charge_item"), charge.get("charge_item"))
                and _eq(b.get("currency"), charge.get("currency")),
            )
        )
    if doc_info.get("container_no"):
        rules.append(
            (
                "rule4",
                lambda b: _eq(b.get("container_no"), doc_info.get("container_no"))
                and _eq(b.get("issuer"), party.get("issuer"))
                and _eq(b.get("recipient"), party.get("recipient"))
                and _eq(b.get("document_date"), doc_info.get("document_date"))
                and _amount_eq(b.get("amount"), charge.get("amount")),
            )
        )
    return rules


def _match_one(
    charge: dict[str, Any],
    doc_info: dict[str, Any],
    party: dict[str, Any],
    existing_bills: list[dict[str, Any]],
) -> dict[str, Any]:
    hit_rule: str | None = None
    hits: list[dict[str, Any]] = []

    for rule, pred in _rules(charge, doc_info, party):
        hits = [b for b in existing_bills if pred(b)]
        if hits:
            hit_rule = rule
            break

    if not hits:
        return {
            "charge": charge,
            "matched_bill": None,
            "rule": None,
            "operation": "CREATE",
            "conflict": False,
            "candidates": [],
        }

    if len(hits) > 1:
        return {
            "charge": charge,
            "matched_bill": None,
            "rule": hit_rule,
            "operation": "PENDING_REVIEW",
            "conflict": True,
            "candidates": [_summary(b) for b in hits],
        }

    bill = hits[0]
    operation = "NO_CHANGE" if _identical(charge, bill) else "UPDATE"
    return {
        "charge": charge,
        "matched_bill": bill,
        "rule": hit_rule,
        "operation": operation,
        "conflict": False,
        "candidates": [_summary(bill)],
    }


def _disambiguate(
    results: list[dict[str, Any]],
    document_info: dict[str, Any],
    transaction_party: dict[str, Any],
    existing_bills: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """同一单据的多条费用争用同一张已有账单时的消歧。

    典型场景：一张 Debit Note 有两行 "Handling Charge"（65 / 35），
    规则①只按「单据号 + 费用名」匹配，两行都会命中同一张旧账单。
    处理方式：金额与旧账单唯一一致的那行拥有该账单，其余行视为另一条费用重新匹配；
    无法唯一确定（零条或多条金额一致）→ 全部转人工，不猜。
    """
    for _ in range(3):  # 重新匹配可能产生新的争用，迭代到稳定
        groups: dict[int, list[int]] = {}
        for i, r in enumerate(results):
            if r.get("matched_bill") is not None and not r.get("conflict"):
                groups.setdefault(id(r["matched_bill"]), []).append(i)

        changed = False
        for idxs in groups.values():
            if len(idxs) < 2:
                continue
            bill = results[idxs[0]]["matched_bill"]
            exact = [
                i
                for i in idxs
                if _amount_eq(results[i]["charge"].get("amount"), bill.get("amount"))
            ]
            if len(exact) == 1:
                keep = exact[0]
                for i in idxs:
                    if i == keep:
                        continue
                    results[i] = _match_one(
                        results[i]["charge"],
                        document_info,
                        transaction_party,
                        [b for b in existing_bills if b is not bill],
                    )
                    results[i]["notes"] = [
                        "同一单据存在同名费用，本行金额与旧账单不一致，判为另一条费用"
                    ]
            else:
                for i in idxs:
                    results[i] = {
                        **results[i],
                        "operation": "PENDING_REVIEW",
                        "matched_bill": None,
                        "conflict": True,
                        "candidates": [_summary(bill)],
                        "conflict_reason": "多条费用争用同一张已有账单，金额无法唯一匹配",
                    }
            changed = True
        if not changed:
            break
    return results


def match_charges(
    normalized_charges: list[dict[str, Any]],
    document_info: dict[str, Any],
    transaction_party: dict[str, Any],
    existing_bills: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """对每个标准化费用项执行四规则匹配，并消解同名费用争用。"""
    results = [
        _match_one(c, document_info, transaction_party, existing_bills)
        for c in normalized_charges
    ]
    return _disambiguate(results, document_info, transaction_party, existing_bills)
