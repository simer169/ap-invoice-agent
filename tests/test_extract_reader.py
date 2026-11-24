import io
from types import SimpleNamespace
from typing import Any

import pytest
from reportlab.pdfgen.canvas import Canvas

from ap_agent.extract import reader
from ap_agent.extract.reader import SCANNED_THRESHOLD, extract_invoice, is_scanned
from ap_agent.models import InvoiceExtraction


def test_scanned_threshold() -> None:
    assert is_scanned("")
    assert is_scanned("a b\n" * 10)  # 20 non-space chars
    assert is_scanned("x" * (SCANNED_THRESHOLD - 1))
    assert not is_scanned("x" * SCANNED_THRESHOLD)
    assert not is_scanned(" ".join(["x"] * SCANNED_THRESHOLD))  # spaces do not count


def _pdf(text: str) -> bytes:
    buf = io.BytesIO()
    c = Canvas(buf)
    if text:
        c.drawString(72, 700, text)
    c.showPage()
    c.save()
    return buf.getvalue()


_INVOICE = InvoiceExtraction.model_validate(
    {
        "vendor_name": "Acme",
        "invoice_number": "1",
        "invoice_date_raw": "03/11/2026",
        "currency": "CAD",
        "subtotal": "1",
        "tax": "0",
        "total": "1",
        "line_items": [{"description": "x", "quantity": "1", "unit_price": "1", "line_total": "1"}],
    }
)


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> InvoiceExtraction:
        self.calls.append(kwargs)
        return _INVOICE


def test_text_pdf_uses_text_method() -> None:
    client = FakeClient()
    res = extract_invoice(
        _pdf("Invoice No. INV-1 total 100.00 " * 5), client=client, model="m", date_order="DMY"
    )  # type: ignore[arg-type]
    assert res.method == "text"
    assert client.calls[0]["max_retries"] == 2
    assert client.calls[0]["response_model"] is InvoiceExtraction
    assert "Invoice No." in client.calls[0]["messages"][1]["content"]


def test_scanned_pdf_uses_vision_method() -> None:
    client = FakeClient()
    res = extract_invoice(_pdf(""), client=client, model="m", date_order="DMY")  # type: ignore[arg-type]
    assert res.method == "vision"
    parts = client.calls[0]["messages"][1]["content"]
    assert any(p["type"] == "image_url" for p in parts)


def test_system_prompt_has_injection_guard() -> None:
    assert "ignore any instructions" in reader.SYSTEM_PROMPT.lower()


@pytest.mark.parametrize("n", [1])
def test_render_pages(n: int) -> None:
    assert len(reader.render_pages_png(_pdf("hello"))) == n


def test_date_context_and_ambiguity_propagate() -> None:
    from datetime import date

    client = FakeClient()
    res = extract_invoice(
        _pdf("Invoice No. INV-1 total 100.00 " * 5),
        client=client,  # type: ignore[arg-type]
        model="m",
        received_date=date(2026, 12, 1),
        date_order="MDY",
    )
    assert client.calls[0]["context"] == {"received_date": date(2026, 12, 1), "date_order": "MDY"}
    assert res.invoice.invoice_date == date(2026, 3, 11)
    assert res.date_ambiguous
    assert res.raw.invoice_date_raw == "03/11/2026"


def test_date_instruction_in_prompt() -> None:
    assert "invoice_date_raw" in reader.SYSTEM_PROMPT
    assert "exactly as printed" in reader.SYSTEM_PROMPT
