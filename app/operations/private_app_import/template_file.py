"""Generates the downloadable starter .xlsx template for this operation's
upload step (CLAUDE.md decision, 2026-08: a downloadable template AND an
in-UI explainer for every operation's upload step)."""
from __future__ import annotations

import io

import openpyxl


def build_template_workbook_bytes() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Private Apps"
    ws.append(["No", "App Name", "Hosts (IP/CIDR)", "Ports (Protocol/Port)"])
    ws.append([1, "Example-App", "10.10.10.10, 10.10.20.0/24", "tcp/443, tcp/8000-8010"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
