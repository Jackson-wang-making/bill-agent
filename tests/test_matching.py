"""四规则匹配 + CREATE/UPDATE/NO_CHANGE 判定。"""
from bill_agent.matching.engine import match_charges


def make_charge(**kw):
    base = {
        "charge_item": "Handling Charge",
        "original_charge_item": "Handling Fee",
        "amount": 100.0,
        "currency": "USD",
        "payment_term": "PP",
        "quantity": 1.0,
        "unit_price": 100.0,
        "confidence": 1.0,
    }
    base.update(kw)
    return base


def make_bill(**kw):
    base = {
        "reference_no": "INV-001",
        "bl_no": "BL001",
        "booking_no": "BK001",
        "container_no": "CNTR1",
        "issuer": "A",
        "recipient": "B",
        "document_date": "2026-01-01",
        "charge_item": "Handling Charge",
        "amount": 100.0,
        "currency": "USD",
        "payment_term": "PP",
    }
    base.update(kw)
    return base


def test_create_when_no_match():
    r = match_charges([make_charge()], {"reference_no": "NEW-1"}, {}, [make_bill()])
    assert r[0]["operation"] == "CREATE"
    assert r[0]["matched_bill"] is None


def test_update_rule1_reference_no():
    r = match_charges([make_charge(amount=150.0)], {"reference_no": "INV-001"}, {}, [make_bill()])
    assert r[0]["operation"] == "UPDATE"
    assert r[0]["rule"] == "rule1"


def test_no_change_when_identical():
    r = match_charges([make_charge()], {"reference_no": "INV-001"}, {}, [make_bill()])
    assert r[0]["operation"] == "NO_CHANGE"


def test_update_rule2_bl_no():
    r = match_charges([make_charge(amount=150.0)], {"bl_no": "BL001"}, {}, [make_bill(reference_no=None)])
    assert r[0]["operation"] == "UPDATE"
    assert r[0]["rule"] == "rule2"


def test_update_rule3_booking_no():
    r = match_charges([make_charge(amount=150.0)], {"booking_no": "BK001"}, {}, [make_bill(reference_no=None, bl_no=None)])
    assert r[0]["rule"] == "rule3"


def test_update_rule4_container_no():
    r = match_charges([make_charge(amount=150.0)], {"container_no": "CNTR1", "document_date": "2026-01-01"},
                      {"issuer": "A", "recipient": "B"},
                      [make_bill(reference_no=None, bl_no=None, booking_no=None, amount=150.0)])
    assert r[0]["rule"] == "rule4"


def test_rule_priority():
    """命中规则①后不再降级到②③④。"""
    r = match_charges([make_charge()], {"reference_no": "INV-001", "bl_no": "BL001"}, {},
                      [make_bill()])
    assert r[0]["rule"] == "rule1"


def test_conflict_when_multiple_bills_match():
    """同一规则命中多张账单 → 冲突转人工，而不是静默取第一条。"""
    bills = [make_bill(), make_bill(amount=999.0, bl_no="BL002")]
    r = match_charges([make_charge(amount=150.0)], {"reference_no": "INV-001"}, {}, bills)

    assert r[0]["operation"] == "PENDING_REVIEW"
    assert r[0]["conflict"] is True
    assert len(r[0]["candidates"]) == 2
    assert r[0]["matched_bill"] is None


def test_no_conflict_when_single_bill_matches():
    r = match_charges([make_charge(amount=150.0)], {"reference_no": "INV-001"}, {}, [make_bill()])
    assert r[0]["conflict"] is False
    assert len(r[0]["candidates"]) == 1


def test_duplicate_charge_name_exact_amount_wins():
    """同一单据两行同名费用：金额与旧账单一致的那行命中，另一行判为独立费用。"""
    bills = [make_bill(amount=65.0)]
    r = match_charges(
        [make_charge(amount=65.0), make_charge(amount=35.0)],
        {"reference_no": "INV-001"},
        {},
        bills,
    )
    assert [x["operation"] for x in r] == ["NO_CHANGE", "CREATE"]
    assert r[1]["notes"]


def test_duplicate_charge_name_without_unique_amount_goes_to_review():
    r = match_charges(
        [make_charge(amount=50.0), make_charge(amount=15.0)],
        {"reference_no": "INV-001"},
        {},
        [make_bill(amount=100.0)],
    )
    assert [x["operation"] for x in r] == ["PENDING_REVIEW", "PENDING_REVIEW"]
    assert all("争用" in x["conflict_reason"] for x in r)


def test_duplicate_charge_name_same_amount_ambiguous():
    r = match_charges(
        [make_charge(amount=100.0), make_charge(amount=100.0)],
        {"reference_no": "INV-001"},
        {},
        [make_bill(amount=100.0)],
    )
    assert [x["operation"] for x in r] == ["PENDING_REVIEW", "PENDING_REVIEW"]
