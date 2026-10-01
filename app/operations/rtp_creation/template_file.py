"""Generates the downloadable starter .xlsx template for this operation's
Stage A upload step (same decision as Private App Import's template: a
downloadable template AND an in-UI explainer for every operation's
upload step)."""
from __future__ import annotations

import io

import openpyxl


def build_template_workbook_bytes() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Users"
    ws.append(["SAM Account Name", "Display Name"])
    ws.append(["jdoe10340", "Jane Doe"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
