"""Generates the downloadable starter .xlsx template for this operation's
upload step (CLAUDE.md decision, 2026-08: a downloadable template AND an
in-UI explainer for every operation's upload step). CSV is also accepted
at upload time (Section 7 step 5), but one .xlsx template is offered here,
same as Operations 1 and 2 - the header-name-driven parser reads either
format identically, so a single template shape is enough to demonstrate
the expected column."""
from __future__ import annotations

import io

import openpyxl


def build_template_workbook_bytes() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Users"
    ws.append(["Email"])
    ws.append(["jane.doe@example.com"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
