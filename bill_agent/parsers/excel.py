"""Excel：读工作表/表头/费用明细行（.xlsx=openpyxl，.xls=xlrd）。"""
from __future__ import annotations

from pathlib import Path


def _cell_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _parse_xlsx(path: str) -> dict:
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True)
    tables: list[list[list[str]]] = []
    sheets: list[str] = []
    for ws in wb.worksheets:
        sheets.append(ws.title)
        tables.append([[_cell_str(c) for c in row] for row in ws.iter_rows(values_only=True)])
    wb.close()
    return {
        "type": "excel",
        "source_file": str(path),
        "raw_text": "",
        "tables": tables,
        "meta": {"sheets": sheets},
    }


def _parse_xls(path: str) -> dict:
    import xlrd

    wb = xlrd.open_workbook(path)
    tables: list[list[list[str]]] = []
    sheets: list[str] = []
    for ws in wb.sheets():
        sheets.append(ws.name)
        tables.append([[_cell_str(c) for c in row] for row in ws.get_rows()])
    return {
        "type": "excel",
        "source_file": str(path),
        "raw_text": "",
        "tables": tables,
        "meta": {"sheets": sheets},
    }


def parse_excel(path: str) -> dict:
    ext = Path(path).suffix.lower()
    if ext == ".xlsx":
        return _parse_xlsx(path)
    return _parse_xls(path)
