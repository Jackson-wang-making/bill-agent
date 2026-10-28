"""费用名/币种/付款方式标准化。"""
from bill_agent.standardize.normalizer import (
    normalize_charge_name,
    normalize_currency,
    normalize_date,
    normalize_payment_term,
)


def test_alias_map():
    std, orig = normalize_charge_name("Handling Fee")
    assert std == "Handling Charge"
    assert orig == "Handling Fee"


def test_alias_substring():
    std, _ = normalize_charge_name("H/D Fee")
    assert std == "Handling Charge"


def test_unknown_keeps_original():
    std, orig = normalize_charge_name("Some New Fee")
    assert std == "Some New Fee"
    assert orig == "Some New Fee"


def test_currency_upper():
    assert normalize_currency("usd") == "USD"
    assert normalize_currency("cny") == "CNY"


def test_payment_term():
    assert normalize_payment_term("Prepaid") == "PP"
    assert normalize_payment_term("COLLECT") == "CC"
    assert normalize_payment_term("到付") == "CC"
    assert normalize_payment_term(None) is None


def test_date_iso_passthrough():
    assert normalize_date("2026-10-24") == "2026-10-24"


def test_date_us_style():
    """真实样例 PDF 里就是 10-24-2026 这种美式写法。"""
    assert normalize_date("10-24-2026") == "2026-10-24"


def test_date_day_first_disambiguated():
    assert normalize_date("24/10/2026") == "2026-10-24"
    assert normalize_date("2026/10/24") == "2026-10-24"


def test_date_chinese_and_rfc2822():
    assert normalize_date("2026年10月24日") == "2026-10-24"
    assert normalize_date("Tue, 20 Oct 2026 15:46:44 +0800") == "2026-10-20"


def test_date_unparseable_kept():
    assert normalize_date("N/A") == "N/A"
    assert normalize_date(None) is None
