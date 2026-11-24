from types import SimpleNamespace
from typing import Any

import pytest

from ap_agent.extract import cache
from ap_agent.models import ExtractionResult, InvoiceExtraction

RAW = InvoiceExtraction.model_validate(
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


class FakeConn:
    """In-memory stand-in for the agent.extractions table."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[Any, ...]] = {}

    def execute(self, sql: str, params: tuple[Any, ...]) -> Any:
        if sql.lstrip().startswith("SELECT"):
            row = self.rows.get(params[0])
            return SimpleNamespace(fetchone=lambda: row)
        h, model, method, js, tin, tout, cost, retries, dur = params
        self.rows[h] = (model, method, js.obj, tin, tout, cost, retries, dur)
        return SimpleNamespace()


@pytest.fixture(autouse=True)
def _model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cache, "get_settings", lambda: SimpleNamespace(EXTRACT_MODEL="m1", DATE_ORDER="DMY")
    )


def _fake_extract(calls: list[str]) -> Any:
    def fake(
        pdf: bytes,
        client: Any = None,
        model: str | None = None,
        received_date: Any = None,
        date_order: Any = "DMY",
    ) -> ExtractionResult:
        calls.append(model or "")
        invoice, amb = RAW.to_invoice(received_date=received_date, date_order=date_order)
        return ExtractionResult(
            invoice=invoice,
            raw=RAW,
            date_ambiguous=amb,
            method="text",
            model=model or "",
            tokens_in=10,
            tokens_out=5,
            cost_usd=0.001,
        )

    return fake


def test_miss_extracts_and_stores_then_hit_skips_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cache, "extract_invoice", _fake_extract(calls))
    conn = FakeConn()

    first = cache.extract_and_cache(conn, "h1", b"pdf")  # type: ignore[arg-type]
    assert calls == ["m1"] and not first.cached

    second = cache.extract_and_cache(conn, "h1", b"pdf")  # type: ignore[arg-type]
    assert calls == ["m1"]  # no second LLM call
    assert second.cached
    assert second.invoice.invoice_date.isoformat() == "2026-11-03"
    assert second.tokens_in == 10


def test_different_model_re_extracts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cache, "extract_invoice", _fake_extract(calls))
    conn = FakeConn()
    cache.extract_and_cache(conn, "h1", b"pdf")  # type: ignore[arg-type]
    monkeypatch.setattr(
        cache, "get_settings", lambda: SimpleNamespace(EXTRACT_MODEL="m2", DATE_ORDER="DMY")
    )
    cache.extract_and_cache(conn, "h1", b"pdf")  # type: ignore[arg-type]
    assert calls == ["m1", "m2"]


def test_cached_raw_is_renormalised_with_new_date_order(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cache, "extract_invoice", _fake_extract(calls))
    conn = FakeConn()
    cache.extract_and_cache(conn, "h1", b"pdf")  # type: ignore[arg-type]
    hit = cache.get_cached(conn, "h1", "m1", date_order="MDY")  # type: ignore[arg-type]
    assert hit is not None
    assert hit.invoice.invoice_date.isoformat() == "2026-03-11"
    assert calls == ["m1"]


def test_old_shape_row_is_a_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cache, "extract_invoice", _fake_extract(calls))
    conn = FakeConn()
    conn.rows["h1"] = (
        "m1",
        "text",
        {"vendor_name": "Old", "invoice_date": "2026-11-05"},
        1,
        1,
        0,
        0,
        1,
    )
    res = cache.extract_and_cache(conn, "h1", b"pdf")  # type: ignore[arg-type]
    assert calls == ["m1"] and not res.cached


def test_refresh_skips_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cache, "extract_invoice", _fake_extract(calls))
    conn = FakeConn()
    cache.extract_and_cache(conn, "h1", b"pdf")  # type: ignore[arg-type]
    cache.extract_and_cache(conn, "h1", b"pdf", refresh=True)  # type: ignore[arg-type]
    assert calls == ["m1", "m1"]
