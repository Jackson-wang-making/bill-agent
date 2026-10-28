"""生成演示样例：debit_note.xlsx（账单）+ existing_bills.json（已有账单）。

用法：python samples/make_samples.py
生成后可跑：
  python -m bill_agent samples/debit_note.xlsx --bills samples/existing_bills.json --entity "Shanghai Ocean Freight Forwarding Co." --out output/result.json --pretty
"""
from __future__ import annotations

import json
from pathlib import Path

import openpyxl

SAMPLES = Path(__file__).resolve().parent


def make_xlsx() -> Path:
    path = SAMPLES / "debit_note.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "DebitNote"
    rows = [
        ["Document Type", "Debit Note", "Reference No", "DN-2026-001", "Date", "2026-10-20"],
        ["Issuer", "Shanghai Ocean Freight Forwarding Co., Ltd.", "Recipient", "ABC Trading Co., Ltd."],
        ["BL No", "BL-8842", "Booking No", "BK-1188", "Container No", "MSKU-1234567"],
        [],
        ["Charge Item", "Quantity", "Unit Price", "Amount", "Currency", "Payment Term"],
        ["Handling Fee", 1, 150.00, 150.00, "USD", "PP"],
        ["Documentation Fee", 1, 50.00, 50.00, "USD", "PP"],
        ["Terminal Handling Charge", 1, 200.00, 200.00, "USD", "CC"],
        ["Subtotal", "", "", 400.00, "USD", ""],
        ["Tax", "", "", 0.00, "USD", ""],
        ["Total", "", "", 400.00, "USD", ""],
    ]
    for row in rows:
        ws.append(row)
    wb.save(path)
    return path


def make_existing_bills() -> Path:
    path = SAMPLES / "existing_bills.json"
    bills = [
        {
            "reference_no": "DN-2026-001",
            "bl_no": "BL-8842",
            "booking_no": "BK-1188",
            "container_no": "MSKU-1234567",
            "issuer": "Shanghai Ocean Freight Forwarding Co., Ltd.",
            "recipient": "ABC Trading Co., Ltd.",
            "document_date": "2026-10-20",
            "charge_item": "Handling Charge",
            "original_charge_item": "Handling Fee",
            "amount": 120.00,
            "currency": "USD",
            "payment_term": "PP",
            "quantity": 1.0,
            "unit_price": 120.00,
        }
    ]
    path.write_text(json.dumps(bills, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


if __name__ == "__main__":
    print("xlsx ->", make_xlsx())
    print("json ->", make_existing_bills())
