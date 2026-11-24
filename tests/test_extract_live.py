"""Live extraction tests: call the real model via OpenRouter and cost money.

Skipped by default. Run with: uv run pytest -m llm
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

from ap_agent.extract.reader import extract_invoice, extract_text

ROOT = Path(__file__).resolve().parent.parent
TRUTH = json.loads((ROOT / "evals" / "ground_truth.json").read_text(encoding="utf-8"))

pytestmark = pytest.mark.llm

# Layout is a per-vendor property; identify it from the label each layout prints.
LAYOUT_MARKERS = {0: "Invoice No.", 1: "Inv #", 2: "Invoice Number"}


def _dev_by_layout() -> dict[int, dict]:
    found: dict[int, dict] = {}
    for inv in TRUTH["invoices"]:
        if inv["split"] != "dev" or inv["scanned"]:
            continue
        text = extract_text((ROOT / "sample_inbox" / "pdfs" / inv["pdf_file"]).read_bytes())
        for layout, marker in LAYOUT_MARKERS.items():
            if marker in text and layout not in found:
                found[layout] = inv
    return found


@pytest.mark.parametrize("layout", [0, 1, 2])
def test_extract_dev_invoice(layout: int) -> None:
    inv = _dev_by_layout().get(layout)
    if inv is None:
        pytest.skip(f"no non-scanned dev invoice with layout {layout}")
    pdf = (ROOT / "sample_inbox" / "pdfs" / inv["pdf_file"]).read_bytes()
    received = datetime.fromisoformat(inv["received"]).date()
    res = extract_invoice(pdf, received_date=received)
    got, want = res.invoice, inv["fields"]
    assert got.vendor_name == want["vendor_name"]
    assert got.invoice_number == want["invoice_number"].upper()
    assert got.invoice_date.isoformat() == want["invoice_date"]
    assert got.po_number == (want["po_number"] or None)
    assert got.currency == want["currency"]
    assert str(got.total) == want["total"]
    assert len(got.line_items) == len(want["line_items"])
    assert res.method == "text"
