"""Tests for data_gen: determinism (no DB) and idempotent rebuild/simulate (DB, rolled back)."""

import sys
from collections import Counter
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import psycopg
import pytest

from ap_agent.config import get_database_url
from data_gen import db
from data_gen.build import build
from data_gen.generate import (
    DEMO_TODAY,
    GEN_PREFIX,
    SCHEDULE_FIRST,
    SCHEDULE_LAST,
    Dataset,
    generate,
)
from data_gen.simulate import apply_day

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from migrate import migrate

DATABASE_URL = get_database_url()
needs_db = pytest.mark.skipif(DATABASE_URL is None, reason="DATABASE_URL not set")


@pytest.fixture(scope="module")
def ds() -> Dataset:
    return generate(42)


# ---------------------------------------------------------------- pure generation (no DB)
def test_same_seed_same_data(ds: Dataset) -> None:
    again = generate(42)
    assert again == ds
    assert again.model_dump_json() == ds.model_dump_json()
    assert generate(43) != ds


def test_vendors(ds: Dataset) -> None:
    assert len(ds.vendors) == 25
    assert Counter(v.currency for v in ds.vendors) == {"CAD": 22, "USD": 3}
    assert all(v.email_domain.endswith(".example") for v in ds.vendors)
    assert len({v.email_domain for v in ds.vendors}) == 25
    terms = {(v.payment_terms_days, v.discount_pct, v.discount_days) for v in ds.vendors}
    assert {(30, 0, 0), (45, 0, 0), (30, 2, 10)} == terms


def test_all_codes_prefixed(ds: Dataset) -> None:
    codes = [v.vendor_code for v in ds.vendors] + [p.po_number for p in ds.purchase_orders]
    codes += [r.rr_number for p in ds.purchase_orders for r in (*p.receipts, *p.scheduled)]
    codes += [h.invoice_number for h in ds.history]
    assert all(c.startswith(GEN_PREFIX) for c in codes)
    assert len(codes) == len(set(codes))


def test_purchase_orders(ds: Dataset) -> None:
    pos = ds.purchase_orders
    assert len(pos) == 80
    assert all(2 <= len(p.lines) <= 8 for p in pos)
    assert all(date(2026, 8, 1) <= p.order_date <= DEMO_TODAY for p in pos)
    vendor_ccy = {v.vendor_code: v.currency for v in ds.vendors}
    assert all(p.currency == vendor_ccy[p.vendor_code] for p in pos)
    for p in pos:
        assert p.total == sum(ln.unit_price * ln.qty_ordered for ln in p.lines)
        assert len({ln.part_no for ln in p.lines}) == len(p.lines)
    assert Counter(p.status for p in pos) == {"RECEIVED": 32, "PARTIAL": 32, "OPEN": 16}


def test_receiving_story(ds: Dataset) -> None:
    sched_dates = []
    never = 0
    for p in ds.purchase_orders:
        ordered = {ln.line_no: ln.qty_ordered for ln in p.lines}
        got = Counter[int]()
        for r in p.receipts:
            assert r.received_at.date() <= DEMO_TODAY
            assert r.received_at.date() >= p.order_date
            for rl in r.lines:
                got[rl.line_no] += rl.qty
        later = Counter[int]()
        for r in p.scheduled:
            assert SCHEDULE_FIRST <= r.received_at.date() <= SCHEDULE_LAST
            sched_dates.append(r.received_at.date())
            for rl in r.lines:
                later[rl.line_no] += rl.qty
        if p.status == "OPEN":
            assert not p.receipts and not p.scheduled
        elif p.status == "RECEIVED":
            assert dict(got) == ordered and not p.scheduled
        else:
            assert any(got[n] > 0 for n in ordered)
            assert any(got[n] < q for n, q in ordered.items())  # some line is short
            assert all(got[n] <= q for n, q in ordered.items())
            if p.scheduled:  # scheduled deliveries exactly cover the shortfall
                assert {n: got[n] + later[n] for n in ordered} == ordered
            else:
                never += 1
    assert never == 4
    assert sched_dates


def test_history(ds: Dataset) -> None:
    by_vendor = Counter(h.vendor_code for h in ds.history)
    assert set(by_vendor) == {v.vendor_code for v in ds.vendors}
    assert all(8 <= n <= 20 for n in by_vendor.values())
    assert all(date(2025, 11, 5) < h.invoice_date <= DEMO_TODAY for h in ds.history)
    assert all(h.total > 0 and h.line_summary for h in ds.history)


# ------------------------------------------------------------- database (always rolled back)
@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    assert DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL, connect_timeout=10) as c:
        migrate(c)
        try:
            yield c
        finally:
            c.rollback()


def _fingerprint(c: psycopg.Connection) -> tuple[dict[str, int], list[tuple[str, str]]]:
    rows = c.execute(
        "SELECT po_number, status FROM trex.purchase_orders "
        "WHERE starts_with(po_number, 'GEN-') ORDER BY po_number"
    ).fetchall()
    return db.counts(c), [(str(a), str(b)) for a, b in rows]


@pytest.mark.integration
@needs_db
def test_rebuild_does_not_duplicate_and_keeps_other_data(conn: psycopg.Connection) -> None:
    conn.execute("INSERT INTO trex.vendors (vendor_code, name) VALUES ('TEST-KEEP', 'Keep Me')")
    _, first = build(conn, 42)
    _, second = build(conn, 42)
    assert first == second
    assert first == {
        "vendors": 25,
        "purchase_orders": 80,
        "po_lines": sum(len(p.lines) for p in generate(42).purchase_orders),
        "receiving_reports": sum(len(p.receipts) for p in generate(42).purchase_orders),
        "receiving_lines": sum(
            len(r.lines) for p in generate(42).purchase_orders for r in p.receipts
        ),
        "vendor_invoice_history": len(generate(42).history),
    }
    kept = conn.execute("SELECT count(*) FROM trex.vendors WHERE vendor_code = 'TEST-KEEP'")
    assert kept.fetchone() == (1,)


@pytest.mark.integration
@needs_db
def test_simulate_twice_same_day_is_noop(conn: psycopg.Connection) -> None:
    dataset, _ = build(conn, 42)
    schedule = dataset.schedule_entries()
    before = _fingerprint(conn)

    first = apply_day(conn, date(2026, 11, 15), schedule)
    after_first = _fingerprint(conn)
    assert first  # something was delivered
    assert after_first != before

    second = apply_day(conn, date(2026, 11, 15), schedule)
    assert second == []
    assert _fingerprint(conn) == after_first

    # Deliveries after the cutoff day are untouched; running to month end completes the rest.
    apply_day(conn, SCHEDULE_LAST, schedule)
    statuses = dict(db.status_counts(conn))
    assert statuses == {"OPEN": 16, "PARTIAL": 4, "RECEIVED": 60}
    assert apply_day(conn, SCHEDULE_LAST, schedule) == []
