from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from ap_agent.models import InvoiceExtraction


def _invoice(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "vendor_name": "Acme",
        "invoice_number": " inv-1 ",
        "invoice_date_raw": "03/11/2026",
        "po_number": "PO# gen-po-1",
        "currency": "CAD",
        "payment_terms": "Net 30",
        "subtotal": "$300.00",
        "tax": "39.00",
        "total": "339.00",
        "line_items": [
            {
                "description": "A",
                "part_no": "P1",
                "quantity": "2",
                "unit_price": "100.00",
                "line_total": "200.00",
            },
            {
                "description": "B",
                "part_no": None,
                "quantity": "1",
                "unit_price": "100.00",
                "line_total": "100.00",
            },
        ],
    }
    base.update(over)
    return base


def test_good_invoice_is_normalised() -> None:
    raw = InvoiceExtraction.model_validate(_invoice())
    inv, ambiguous = raw.to_invoice()
    assert inv.invoice_number == "INV-1"
    assert inv.po_number == "GEN-PO-1"
    assert inv.invoice_date == date(2026, 11, 3)
    assert ambiguous


def test_to_invoice_respects_date_order() -> None:
    raw = InvoiceExtraction.model_validate(_invoice())
    inv, ambiguous = raw.to_invoice(date_order="MDY")
    assert inv.invoice_date == date(2026, 3, 11)
    assert ambiguous


def test_unambiguous_date_not_flagged() -> None:
    raw = InvoiceExtraction.model_validate(_invoice(invoice_date_raw="Nov 3, 2026"))
    inv, ambiguous = raw.to_invoice()
    assert inv.invoice_date == date(2026, 11, 3)
    assert not ambiguous


def test_impossible_year_fails_validation_with_retry_hint() -> None:
    with pytest.raises(ValidationError, match=r"year 311 is outside 2000-2100.*exactly as printed"):
        InvoiceExtraction.model_validate(_invoice(invoice_date_raw="0311-03-11"))


def test_validation_context_applies_received_date() -> None:
    ctx = {"received_date": date(2026, 1, 1), "date_order": "DMY"}
    with pytest.raises(ValidationError, match="received date"):
        InvoiceExtraction.model_validate(_invoice(), context=ctx)


def test_within_tolerance_passes() -> None:
    InvoiceExtraction.model_validate(_invoice(total="339.01"))


def test_bad_line_total() -> None:
    items = _invoice()["line_items"]
    items[0]["line_total"] = "205.00"
    with pytest.raises(ValidationError, match=r"line 1.*quantity \* unit_price = 200.00.*5.00"):
        InvoiceExtraction.model_validate(_invoice(line_items=items))


def test_bad_subtotal() -> None:
    with pytest.raises(ValidationError, match=r"sum of line totals = 300.00.*subtotal = 310.00"):
        InvoiceExtraction.model_validate(_invoice(subtotal="310.00", total="349.00"))


def test_bad_total() -> None:
    with pytest.raises(ValidationError, match=r"subtotal \+ tax = 339.00.*total = 400.00.*61.00"):
        InvoiceExtraction.model_validate(_invoice(total="400.00"))


def test_bad_currency() -> None:
    with pytest.raises(ValidationError):
        InvoiceExtraction.model_validate(_invoice(currency="EUR"))
