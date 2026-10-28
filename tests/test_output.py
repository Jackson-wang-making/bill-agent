"""output_node 组装 + UPDATE 字段级继承。"""
from bill_agent.graph.nodes.output import output_node


def _state(operation, charge, bill=None, rule=None):
    return {
        "match_results": [
            {"charge": charge, "matched_bill": bill, "rule": rule, "operation": operation}
        ],
        "document_info": {
            "document_type": "Debit Note",
            "reference_no": "INV-1",
            "document_date": "2026-01-01",
        },
        "transaction_party": {"issuer": "A", "recipient": "B"},
        "accounting_entity": "A",
        "posting_type": "AR",
        "ar_ap_reasoning": "记账主体是签发方",
        "parsed_documents": [{"source_file": "x.pdf", "raw_text": "Handling Fee USD 100"}],
    }


def _charge(**kw):
    base = {
        "charge_item": "Handling Charge",
        "original_charge_item": "Handling Fee",
        "amount": 100.0,
        "currency": "USD",
        "payment_term": "PP",
        "quantity": None,
        "unit_price": None,
    }
    base.update(kw)
    return base


def test_create_record():
    recs = output_node(_state("CREATE", _charge()))["output_records"]
    r = recs[0]
    assert r["operation"] == "CREATE"
    assert r["charge_details"]["charge_item"] == "Handling Charge"
    assert r["charge_details"]["original_charge_item"] == "Handling Fee"
    assert r["financial_attributes"]["posting_type"] == "AR"


def test_update_inherits_fields():
    """输入只改金额，quantity/unit_price 应保留旧账单值（字段级继承）。"""
    bill = {
        "charge_item": "Handling Charge",
        "original_charge_item": "Handling Fee",
        "amount": 100.0,
        "currency": "USD",
        "payment_term": "PP",
        "quantity": 2.0,
        "unit_price": 50.0,
    }
    recs = output_node(_state("UPDATE", _charge(amount=150.0), bill, "rule1"))["output_records"]
    cd = recs[0]["charge_details"]
    assert recs[0]["operation"] == "UPDATE"
    assert cd["amount"] == 150.0
    assert cd["quantity"] == 2.0  # 继承
    assert cd["unit_price"] == 50.0  # 继承


def test_no_change_record():
    bill = {"charge_item": "Handling Charge", "amount": 100.0, "currency": "USD", "payment_term": "PP"}
    recs = output_node(_state("NO_CHANGE", _charge(), bill, "rule1"))["output_records"]
    assert recs[0]["operation"] == "NO_CHANGE"


def test_audit_trail_has_reasoning():
    recs = output_node(_state("CREATE", _charge()))["output_records"]
    assert "AR/AP" in " ".join(recs[0]["audit_trail"]["ai_reasoning"])


def test_conflict_record_is_pending_review():
    """冲突命中：输出操作类型 PENDING_REVIEW，并保留候选账单供人工裁决。"""
    state = _state("PENDING_REVIEW", _charge())
    state["match_results"][0].update(
        {"matched_bill": None, "conflict": True, "rule": "rule1",
         "candidates": [{"reference_no": "INV-1"}, {"reference_no": "INV-2"}]}
    )
    r = output_node(state)["output_records"][0]

    assert r["operation"] == "PENDING_REVIEW"
    assert r["charge_details"]["amount"] == 100.0  # 提取值仍保留供复核
    assert "数据冲突" in " ".join(r["audit_trail"]["ai_reasoning"])


def test_one_record_per_charge():
    """一个单据多个费用项 → 每条记录一条（题目处理要求 5）。"""
    state = _state("CREATE", _charge())
    state["match_results"].append(
        {"charge": _charge(charge_item="Documentation Fee", original_charge_item="Doc Fee"),
         "matched_bill": None, "rule": None, "operation": "CREATE", "conflict": False,
         "candidates": []}
    )
    recs = output_node(state)["output_records"]
    assert len(recs) == 2
    assert {r["charge_details"]["charge_item"] for r in recs} == {
        "Handling Charge",
        "Documentation Fee",
    }
