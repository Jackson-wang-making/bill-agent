"""确定性校验：金额/币种/合计/OCR 纠错/重复。"""
from bill_agent.validation.code_validators import (
    detect_duplicates,
    is_valid_amount,
    is_valid_currency,
    validate_extraction,
)


def test_valid_amount():
    assert is_valid_amount(100.5)
    assert is_valid_amount("100.5")
    assert not is_valid_amount("abc")
    assert not is_valid_amount(True)


def test_valid_currency():
    assert is_valid_currency("USD")
    assert is_valid_currency("usd")
    assert not is_valid_currency("US")
    assert not is_valid_currency(123)


def test_validate_fail_bad_amount():
    issues, status = validate_extraction({"charges": [{"amount": "abc", "currency": "USD"}]})
    assert status == "fail"
    assert any("金额非法" in i for i in issues)


def test_validate_totals_pass():
    issues, status = validate_extraction(
        {"charges": [{"amount": 100.0, "currency": "USD"}], "subtotal": 100.0, "tax": 0.0, "total": 100.0}
    )
    assert status == "pass"


def test_validate_totals_mismatch():
    issues, _ = validate_extraction(
        {"charges": [{"amount": 100.0, "currency": "USD"}], "subtotal": 90.0, "tax": 0.0, "total": 90.0}
    )
    assert any("明细合计" in i for i in issues)


def test_detect_duplicates():
    dups = detect_duplicates(
        [
            {"charge_item": "a", "currency": "USD", "amount": 1},
            {"charge_item": "a", "currency": "USD", "amount": 1},
        ]
    )
    assert len(dups) == 1
