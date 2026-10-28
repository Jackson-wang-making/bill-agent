"""端到端 happy path：mock 掉 extract 的 LLM，验证整条图接线正确。"""
from docx import Document

from bill_agent.extraction.schemas import (
    ChargeItem,
    DocumentInfo,
    ExtractionResult,
    TransactionParty,
)
from bill_agent.main import run_bill_agent


class _FakeChain:
    def __init__(self, result):
        self._result = result

    def invoke(self, messages):
        return self._result


def _mock_extract(monkeypatch, result):
    import bill_agent.extraction.extractor as ext

    monkeypatch.setattr(ext, "structured_llm", lambda *a, **k: _FakeChain(result))


def _write_docx(tmp_path, name="a.docx"):
    p = tmp_path / name
    doc = Document()
    doc.add_paragraph("Handling Fee USD 100 PP")
    doc.save(p)
    return str(p)


def _result(charge_item="Handling Fee", amount=100.0, confidence=0.95):
    return ExtractionResult(
        document_info=DocumentInfo(
            document_type="Debit Note", reference_no="INV-001", document_date="2026-01-01"
        ),
        transaction_party=TransactionParty(issuer="A Shipping", recipient="B Trading"),
        charges=[
            ChargeItem(charge_item=charge_item, amount=amount, currency="USD", payment_term="PP", confidence=confidence)
        ],
        subtotal=amount,
        tax=0.0,
        total=amount,
        overall_confidence=confidence,
    )


def test_e2e_create(monkeypatch, tmp_path):
    _mock_extract(monkeypatch, _result())
    path = _write_docx(tmp_path)

    records = run_bill_agent([path], [], "A Shipping")

    assert records[0]["operation"] == "CREATE"
    assert records[0]["charge_details"]["charge_item"] == "Handling Charge"
    assert records[0]["charge_details"]["original_charge_item"] == "Handling Fee"
    assert records[0]["financial_attributes"]["posting_type"] == "AR"


def test_e2e_multi_charge_creates_one_record_each(monkeypatch, tmp_path):
    """一个单据多个费用项 → 每个费用一条记录（题目处理要求 5）。"""
    result = _result()
    result.charges = [
        result.charges[0],
        type(result.charges[0])(
            charge_item="Doc Fee", amount=50.0, currency="USD", payment_term="PP", confidence=0.95
        ),
    ]
    result.subtotal = 150.0
    result.total = 150.0
    _mock_extract(monkeypatch, result)
    path = _write_docx(tmp_path)

    records = run_bill_agent([path], [], "A Shipping")

    assert len(records) == 2
    assert {r["charge_details"]["charge_item"] for r in records} == {
        "Handling Charge",
        "Documentation Fee",
    }


def test_e2e_update_with_inheritance(monkeypatch, tmp_path):
    _mock_extract(monkeypatch, _result(amount=150.0))
    path = _write_docx(tmp_path)
    existing = [
        {
            "reference_no": "INV-001",
            "charge_item": "Handling Charge",
            "amount": 100.0,
            "currency": "USD",
            "payment_term": "PP",
            "quantity": 2.0,
            "unit_price": 50.0,
        }
    ]

    records = run_bill_agent([path], existing, "A Shipping")

    assert records[0]["operation"] == "UPDATE"
    cd = records[0]["charge_details"]
    assert cd["amount"] == 150.0  # 更新
    assert cd["quantity"] == 2.0  # 继承
    assert cd["unit_price"] == 50.0  # 继承
