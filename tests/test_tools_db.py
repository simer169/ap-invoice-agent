"""Integration tests for the TREX tools. Rows are seeded in a rolled-back transaction."""

import json
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest

from ap_agent.models import LineItem
from ap_agent.tools.duplicates import check_duplicate
from ap_agent.tools.purchase_orders import match_po
from ap_agent.tools.receipt import receipt_status
from ap_agent.tools.terms import load_vendor_terms, terms_result
from ap_agent.tools.vendors import find_vendor
from tests.tools_seed import connect, integration, receive, seed_po, seed_vendor, uid

pytestmark = integration

GROUND_TRUTH = Path(__file__).resolve().parent.parent / "evals" / "ground_truth.json"


@pytest.fixture
def conn() -> Iterator[psycopg.Connection[Any]]:
    yield from connect()


def _items(rows: list[tuple[str, str, str]]) -> list[LineItem]:
    return [
        LineItem(
            description=p,
            part_no=p,
            quantity=Decimal(q),
            unit_price=Decimal(u),
            line_total=Decimal(q) * Decimal(u),
        )
        for p, q, u in rows
    ]


ROWS = [("QX-1", "10", "5.00"), ("QX-2", "4", "20.00"), ("QX-3", "2", "50.00")]
SEED = [(p, f"desc {p}", q, u) for p, q, u in ROWS]
TOTAL = Decimal("230.00") * Decimal("1.13")


# --- vendors ---------------------------------------------------------------------------------


def test_find_vendor_exact_domain_fuzzy_and_none(conn: psycopg.Connection[Any]) -> None:
    tag = uid()
    name = f"Zyqx{tag} Holdings Ltd"
    vid = seed_vendor(conn, name, f"zyqx{tag.lower()}.example")
    m = find_vendor(conn, name.upper().replace(" ", "  "), None)
    assert (m.vendor_id, m.method) == (vid, "exact_name")
    m = find_vendor(conn, "Other Name", f"AR <ar@zyqx{tag.lower()}.example>")
    assert (m.vendor_id, m.method) == (vid, "email_domain")
    m = find_vendor(conn, f"Zyqx{tag} Holding", None)
    assert (m.vendor_id, m.method) == (vid, "fuzzy")
    m = find_vendor(conn, f"Qqq{tag} Wwwwww Nnnnn", "x@nowhere.example")
    assert m.vendor_id is None


def test_find_vendor_ignores_inactive(conn: psycopg.Connection[Any]) -> None:
    tag = uid()
    vid = seed_vendor(conn, f"Inactive{tag} Co")
    conn.execute("UPDATE trex.vendors SET active = false WHERE id = %s", (vid,))
    assert find_vendor(conn, f"Inactive{tag} Co", None).vendor_id is None


# --- purchase orders -------------------------------------------------------------------------


def test_match_by_po_number_normalised(conn: psycopg.Connection[Any]) -> None:
    v = seed_vendor(conn, f"A{uid()}")
    po_id, _ = seed_po(conn, v, SEED, po_number="T-PO-0001")
    m = match_po(conn, v, "po# t-po-0001", None, _items(ROWS), TOTAL, date(2026, 11, 1))
    assert (m.po_id, m.method, m.confidence) == (po_id, "po_number", 1.0)


def test_po_belongs_to_another_vendor(conn: psycopg.Connection[Any]) -> None:
    v1, v2 = seed_vendor(conn, f"A{uid()}"), seed_vendor(conn, f"B{uid()}")
    num = f"T-{uid()}"
    seed_po(conn, v2, SEED, po_number=num)
    m = match_po(conn, v1, num, None, _items(ROWS), TOTAL, date(2026, 11, 1))
    assert m.po_id is None
    assert m.method == "none"
    assert "PO belongs to another vendor" in m.reasons


def test_explicit_unknown_po_does_not_fall_back_to_content(conn: psycopg.Connection[Any]) -> None:
    v = seed_vendor(conn, f"A{uid()}")
    seed_po(conn, v, SEED)
    m = match_po(conn, v, "NOPE-1", None, _items(ROWS), TOTAL, date(2026, 11, 1))
    assert m.po_id is None
    assert m.method == "none"


def test_subject_hint_match(conn: psycopg.Connection[Any]) -> None:
    v = seed_vendor(conn, f"A{uid()}")
    num = f"T-{uid()}"
    po_id, _ = seed_po(conn, v, SEED, po_number=num)
    m = match_po(conn, v, None, f"Billing for PO {num}", [], Decimal(1), None)
    assert (m.po_id, m.method) == (po_id, "subject_hint")


def test_subject_hint_for_other_vendor_falls_through(conn: psycopg.Connection[Any]) -> None:
    v1, v2 = seed_vendor(conn, f"A{uid()}"), seed_vendor(conn, f"B{uid()}")
    num = f"T-{uid()}"
    seed_po(conn, v2, SEED, po_number=num)
    own, _ = seed_po(conn, v1, SEED)
    m = match_po(conn, v1, None, f"PO {num}", _items(ROWS), TOTAL, date(2026, 11, 1))
    assert (m.po_id, m.method) == (own, "content_score")
    assert any("another vendor" in r for r in m.reasons)


def test_content_score_picks_right_po(conn: psycopg.Connection[Any]) -> None:
    v = seed_vendor(conn, f"A{uid()}")
    right, _ = seed_po(conn, v, SEED)
    seed_po(conn, v, [("QX-1", "d", "99", "5.00"), ("ZZ-8", "d", "7", "3.00")])
    m = match_po(conn, v, None, "Invoice 77", _items(ROWS), TOTAL, date(2026, 11, 1))
    assert (m.po_id, m.method) == (right, "content_score")
    assert m.confidence >= 0.85
    assert len(m.candidates) == 2


def test_content_score_ambiguous_twins_returns_none(conn: psycopg.Connection[Any]) -> None:
    v = seed_vendor(conn, f"A{uid()}")
    seed_po(conn, v, SEED)
    seed_po(conn, v, SEED)
    m = match_po(conn, v, None, None, _items(ROWS), TOTAL, date(2026, 11, 1))
    assert m.po_id is None
    assert m.method == "none"
    assert len(m.candidates) == 2


def test_fully_invoiced_and_closed_pos_are_not_candidates(conn: psycopg.Connection[Any]) -> None:
    v = seed_vendor(conn, f"A{uid()}")
    invoiced, _ = seed_po(conn, v, SEED)
    conn.execute(
        "INSERT INTO trex.invoices (vendor_id, po_id, invoice_number, invoice_date, total) "
        "VALUES (%s, %s, 'OLD-1', '2026-10-20', 1)",
        (v, invoiced),
    )
    seed_po(conn, v, SEED, status="CLOSED")
    m = match_po(conn, v, None, None, _items(ROWS), TOTAL, date(2026, 11, 1))
    assert m.po_id is None
    assert m.candidates == []


@pytest.mark.parametrize("inv_id", ["INV-048", "INV-003", "INV-005", "INV-016"])
def test_content_scoring_on_ground_truth_no_po_invoices(
    conn: psycopg.Connection[Any], inv_id: str
) -> None:
    """INV-048 is the dev EXCEPTION_NO_PO invoice; the others are eval-split no-PO cases."""
    gt = json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))
    inv = next(i for i in gt["invoices"] if i["id"] == inv_id)
    f = inv["fields"]
    assert f["po_number"] is None
    v = seed_vendor(conn, f["vendor_name"] + uid())
    seed_lines = [
        (li["part_no"], li["description"], str(li["qty"]), li["unit_price"])
        for li in f["line_items"]
    ]
    right, _ = seed_po(conn, v, seed_lines, po_number=f"T-{inv['source_po']}-{uid()}")
    # decoy: shares one part number but different quantity and price
    seed_po(conn, v, [(seed_lines[0][0], "decoy", "1", "999.00"), ("ZZ-1", "x", "3", "4.00")])
    lines = [
        LineItem(
            description=li["description"],
            part_no=li["part_no"],
            quantity=Decimal(str(li["qty"])),
            unit_price=Decimal(li["unit_price"]),
            line_total=Decimal(li["line_total"]),
        )
        for li in f["line_items"]
    ]
    m = match_po(
        conn,
        v,
        f["po_number"],
        None,
        lines,
        Decimal(f["total"]),
        date.fromisoformat(f["invoice_date"]),
    )
    assert (m.po_id, m.method) == (right, "content_score")


# --- duplicates ------------------------------------------------------------------------------


def _park(conn: psycopg.Connection[Any], vendor_id: int, number: str, h: str) -> str:
    thread = f"th-{uid()}"
    conn.execute(
        "INSERT INTO agent.parked_invoices (thread_id, content_hash, vendor_id, invoice_number) "
        "VALUES (%s, %s, %s, %s)",
        (thread, h, vendor_id, number),
    )
    return thread


def test_duplicate_new_exact_reissue(conn: psycopg.Connection[Any]) -> None:
    v = seed_vendor(conn, f"A{uid()}")
    h1, h2 = uid().ljust(64, "a"), uid().ljust(64, "b")
    assert check_duplicate(conn, v, "INV-1", h1).kind == "new"
    thread = _park(conn, v, "INV-1", h1)
    exact = check_duplicate(conn, v, "INV-1", h1)
    assert (exact.kind, exact.existing_ref) == ("exact", thread)
    reissue = check_duplicate(conn, v, "inv 1", h2)  # same number, different PDF
    assert (reissue.kind, reissue.existing_ref) == ("reissue", f"thread:{thread}")
    assert check_duplicate(conn, v, "INV-2", h2).kind == "new"


def test_exact_duplicate_found_even_when_vendor_unknown(conn: psycopg.Connection[Any]) -> None:
    v = seed_vendor(conn, f"A{uid()}")
    h = uid().ljust(64, "c")
    _park(conn, v, "INV-9", h)
    assert check_duplicate(conn, None, "INV-9", h).kind == "exact"


def test_reissue_of_booked_trex_invoice(conn: psycopg.Connection[Any]) -> None:
    v = seed_vendor(conn, f"A{uid()}")
    row = conn.execute(
        "INSERT INTO trex.invoices (vendor_id, invoice_number, invoice_date, total) "
        "VALUES (%s, 'BK-7', '2026-10-01', 5) RETURNING id",
        (v,),
    ).fetchone()
    assert row is not None
    r = check_duplicate(conn, v, "bk-7", uid().ljust(64, "d"))
    assert (r.kind, r.existing_ref) == ("reissue", f"trex_invoice:{row[0]}")


# --- receipt ---------------------------------------------------------------------------------


def test_receipt_partial_then_full(conn: psycopg.Connection[Any]) -> None:
    v = seed_vendor(conn, f"A{uid()}")
    po_id, ids = seed_po(conn, v, SEED)
    r = receipt_status(conn, po_id)
    assert r.fully_received is False
    assert [x.qty_outstanding for x in r.lines] == [Decimal(10), Decimal(4), Decimal(2)]
    receive(conn, po_id, ids[0], "10")
    receive(conn, po_id, ids[1], "1")
    r = receipt_status(conn, po_id)
    assert r.fully_received is False
    assert [x.qty_received for x in r.lines] == [Decimal(10), Decimal(1), Decimal(0)]
    assert [x.qty_outstanding for x in r.lines] == [Decimal(0), Decimal(3), Decimal(2)]
    receive(conn, po_id, ids[1], "3")
    receive(conn, po_id, ids[2], "2")
    r = receipt_status(conn, po_id)
    assert r.fully_received is True
    assert all(x.qty_outstanding == 0 for x in r.lines)


def test_receipt_over_received_outstanding_floors_at_zero(
    conn: psycopg.Connection[Any],
) -> None:
    v = seed_vendor(conn, f"A{uid()}")
    po_id, ids = seed_po(conn, v, [SEED[0]])
    receive(conn, po_id, ids[0], "12")
    r = receipt_status(conn, po_id)
    assert r.lines[0].qty_outstanding == 0
    assert r.fully_received is True


# --- terms -----------------------------------------------------------------------------------


def test_vendor_terms_two_ten_net_thirty(conn: psycopg.Connection[Any]) -> None:
    v = seed_vendor(conn, f"A{uid()}", days=30, discount_pct="2.00", discount_days=10)
    t = load_vendor_terms(conn, v)
    assert t is not None
    r = terms_result(date(2026, 11, 4), t)
    assert (r.discount_deadline, r.due_date) == (date(2026, 11, 14), date(2026, 12, 4))
    assert load_vendor_terms(conn, -1) is None
