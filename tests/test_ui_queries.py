"""Integration tests for ap_agent.ui_queries. Skipped when DATABASE_URL is unset.

All rows are created inside a transaction that is rolled back; assertions only look at the
rows seeded here, so existing demo data does not matter.
"""

import sys
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from ap_agent import ui_queries as q
from ap_agent.config import get_database_url
from ap_agent.extract.batch import pending_pdfs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from migrate import migrate

DATABASE_URL = get_database_url()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(DATABASE_URL is None, reason="DATABASE_URL not set"),
]

H1, H2, H3 = "a" * 64, "b" * 64, "c" * 64


@pytest.fixture
def conn() -> Iterator[psycopg.Connection[Any]]:
    assert DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL, connect_timeout=10) as c:
        migrate(c)
        try:
            yield c
        finally:
            c.rollback()


def _raw(po: str | None, date_raw: str, **over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "vendor_name": "UI Test Vendor",
        "invoice_number": "UI-1",
        "invoice_date_raw": date_raw,
        "po_number": po,
        "currency": "CAD",
        "payment_terms": "Net 30",
        "subtotal": "100.00",
        "tax": "13.00",
        "total": "113.00",
        "line_items": [
            {"description": "x", "quantity": "1", "unit_price": "100.00", "line_total": "100.00"}
        ],
    }
    data.update(over)
    return data


def _extraction(c: psycopg.Connection[Any], h: str, js: Any, method: str = "text") -> None:
    c.execute(
        "INSERT INTO agent.extractions (content_hash, model, method, extracted_json, tokens_in, "
        "tokens_out, cost_usd) VALUES (%s, 'm', %s, %s, 10, 5, 0.001)",
        (h, method, Jsonb(js)),
    )


def _run_requested(
    c: psycopg.Connection[Any], h: str, received: str = "2026-11-10T09:00:00"
) -> None:
    c.execute(
        "INSERT INTO agent.audit_log (actor, action, ref, details) VALUES "
        "('test', 'RUN_REQUESTED', %s, %s)",
        (
            f"invoice-{h}",
            Jsonb(
                {
                    "s3_key": f"raw/2026/11/{h}.pdf",
                    "email": {"from": "ap@ui.example", "subject": "Inv", "received_at": received},
                }
            ),
        ),
    )


def _po(c: psycopg.Connection[Any]) -> tuple[int, str]:
    v = c.execute(
        "INSERT INTO trex.vendors (vendor_code, name, payment_terms_days) "
        "VALUES ('UI-V1', 'UI Test Vendor', 45) RETURNING id"
    ).fetchone()
    assert v
    po = c.execute(
        "INSERT INTO trex.purchase_orders (po_number, vendor_id, order_date, status, total) "
        "VALUES ('UI-PO-1', %s, '2026-10-01', 'PARTIAL', 500) RETURNING id",
        (v[0],),
    ).fetchone()
    assert po
    for no, qty in ((1, 10), (2, 5)):
        c.execute(
            "INSERT INTO trex.po_lines (po_id, line_no, part_no, description, qty_ordered, "
            "unit_price) VALUES (%s, %s, %s, 'desc', %s, 10)",
            (po[0], no, f"P{no}", qty),
        )
    rr = c.execute(
        "INSERT INTO trex.receiving_reports (rr_number, po_id, received_at, received_by) "
        "VALUES ('UI-RR-1', %s, '2026-10-20 10:00+00', 'clerk') RETURNING id",
        (po[0],),
    ).fetchone()
    assert rr
    c.execute(
        "INSERT INTO trex.receiving_lines (rr_id, po_line_id, qty_received) "
        "SELECT %s, id, 10 FROM trex.po_lines WHERE po_id = %s AND line_no = 1",
        (rr[0], po[0]),
    )
    return int(v[0]), "UI-PO-1"


def test_inbox_filter_and_counts(conn: psycopg.Connection[Any]) -> None:
    before = q.inbox_counts(conn)
    for i, outcome in enumerate(["PROCESSED", "ERROR", "ERROR"]):
        conn.execute(
            "INSERT INTO agent.processed_emails (message_id, received_at, from_address, subject, "
            "outcome, error_message) VALUES (%s, now(), 'x@ui.example', 'S', %s, %s)",
            (f"<ui-test-{i}@x>", outcome, "boom" if outcome == "ERROR" else None),
        )
    after = q.inbox_counts(conn)
    assert after.get("ERROR", 0) - before.get("ERROR", 0) == 2
    assert after.get("PROCESSED", 0) - before.get("PROCESSED", 0) == 1
    errs = [r for r in q.list_inbox(conn, "ERROR") if r.message_id.startswith("<ui-test-")]
    assert len(errs) == 2 and all(r.error_message == "boom" for r in errs)
    assert all(r.outcome == "ERROR" for r in q.list_inbox(conn, "ERROR"))


def test_invoice_rows_flags_and_bad_json(conn: psycopg.Connection[Any]) -> None:
    _extraction(conn, H1, _raw("PO# ui-po-1", "03/11/2026"))  # ambiguous, PO cleaned
    _extraction(conn, H2, _raw(None, "Nov 3, 2026"), method="vision")  # unambiguous, no PO
    _extraction(conn, H3, {"vendor_name": "old shape"})  # no longer validates
    _run_requested(conn, H1)
    rows = {r.content_hash: r for r in q.list_extracted_invoices(conn)}

    a, b, bad = rows[H1], rows[H2], rows[H3]
    assert a.date_ambiguous and not a.missing_po and a.po_number == "UI-PO-1"
    assert a.invoice_date is not None and a.invoice_date.isoformat() == "2026-11-03"
    assert a.s3_key == f"raw/2026/11/{H1}.pdf" and a.email_from == "ap@ui.example"
    assert not b.date_ambiguous and b.missing_po and b.method == "vision"
    assert bad.error and bad.invoice is None
    assert q.get_invoice(conn, H1) is not None
    assert q.get_invoice(conn, "f" * 64) is None


def test_date_order_changes_reading(conn: psycopg.Connection[Any]) -> None:
    _extraction(conn, H1, _raw("UI-PO-1", "03/11/2026"))
    _run_requested(conn, H1, received="2026-12-01T00:00:00")
    mdy = next(r for r in q.list_extracted_invoices(conn, "MDY") if r.content_hash == H1)
    assert mdy.invoice_date is not None and mdy.invoice_date.isoformat() == "2026-03-11"


def test_po_list_filters_and_detail(conn: psycopg.Connection[Any]) -> None:
    vendor_id, po_number = _po(conn)
    _extraction(conn, H1, _raw("PO: ui-po-1", "2026-11-03"))
    _run_requested(conn, H1)

    mine = [p for p in q.list_purchase_orders(conn, "PARTIAL", vendor_id)]
    assert [p.po_number for p in mine] == [po_number]
    assert q.list_purchase_orders(conn, "CLOSED", vendor_id) == []
    assert "PARTIAL" in q.list_po_statuses(conn)
    assert any(v.id == vendor_id for v in q.list_vendor_options(conn))

    d = q.get_po_detail(conn, po_number)
    assert d is not None
    assert not d.summary.fully_received and d.summary.lines_total == 2
    got = {ln.line_no: (ln.qty_ordered, ln.qty_received, ln.fully_received) for ln in d.lines}
    assert got == {1: (Decimal(10), Decimal(10), True), 2: (Decimal(5), Decimal(0), False)}
    assert [r.rr_number for r in d.receiving_reports] == ["UI-RR-1"]
    assert d.receiving_reports[0].lines[0].qty_received == Decimal(10)
    assert [i.content_hash for i in d.invoices] == [H1]
    assert q.get_po_detail(conn, "NOPE") is None


def test_vendor_stats(conn: psycopg.Connection[Any]) -> None:
    vendor_id, _ = _po(conn)
    for n, total in (("A", 100), ("B", 300)):
        conn.execute(
            "INSERT INTO trex.vendor_invoice_history (vendor_id, invoice_number, invoice_date, "
            "total) VALUES (%s, %s, '2026-09-01', %s)",
            (vendor_id, n, total),
        )
    v = next(v for v in q.list_vendors(conn) if v.id == vendor_id)
    assert v.payment_terms_days == 45 and v.invoice_count == 2
    assert (v.avg_total, v.min_total, v.max_total) == (Decimal("200.00"), 100, 300)


def test_pending_pdfs_excludes_extracted(conn: psycopg.Connection[Any]) -> None:
    _run_requested(conn, H1)
    _run_requested(conn, H2)
    _extraction(conn, H2, _raw("UI-PO-1", "2026-11-03"))
    mine = {p.content_hash for p in pending_pdfs(conn)} & {H1, H2}
    assert mine == {H1}


def test_readonly_role_privileges(conn: psycopg.Connection[Any]) -> None:
    def has(table: str, priv: str) -> bool:
        row = conn.execute(
            "SELECT has_table_privilege('ap_ui_readonly', %s, %s)", (table, priv)
        ).fetchone()
        return bool(row and row[0])

    for table in ("agent.processed_emails", "agent.extractions", "trex.purchase_orders"):
        assert has(table, "SELECT")
        assert not has(table, "INSERT")
        assert not has(table, "UPDATE")
        assert not has(table, "DELETE")
