"""Integration tests for the Octopos mock DB. Skipped when DATABASE_URL is unset.

Schema creation is committed (migrations are meant to persist); all test rows live in a
transaction that is rolled back at the end.
"""

import sys
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from ap_agent.config import get_database_url

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from migrate import MIGRATIONS_DIR, migrate

DATABASE_URL = get_database_url()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(DATABASE_URL is None, reason="DATABASE_URL not set"),
]


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    assert DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL, connect_timeout=10) as c:
        migrate(c)  # commits itself; afterwards psycopg opens a new transaction implicitly
        try:
            yield c
        finally:
            c.rollback()


def _vendor(c: psycopg.Connection) -> int:
    row = c.execute(
        "INSERT INTO trex.vendors (vendor_code, name) VALUES ('TEST-V1', 'Test Vendor') "
        "RETURNING id"
    ).fetchone()
    assert row
    return int(row[0])


def test_migrations_run_twice(conn: psycopg.Connection) -> None:
    assert migrate(conn) == []  # everything already recorded
    # Each file is also safe to execute again directly.
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        conn.execute(path.read_text(encoding="utf-8"))  # type: ignore[arg-type]
    recorded = {r[0] for r in conn.execute("SELECT filename FROM agent.schema_migrations")}
    assert {p.name for p in MIGRATIONS_DIR.glob("*.sql")} <= recorded


def test_duplicate_vendor_invoice_rejected(conn: psycopg.Connection) -> None:
    vid = _vendor(conn)
    sql = (
        "INSERT INTO trex.invoices (vendor_id, invoice_number, invoice_date, total) "
        "VALUES (%s, 'INV-1', '2026-01-01', 100.00)"
    )
    conn.execute(sql, (vid,))
    # The inner transaction is a savepoint, so the outer transaction stays usable.
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        conn.execute(sql, (vid,))


def _status(conn: psycopg.Connection, po_id: int) -> tuple[int, int, bool]:
    row = conn.execute(
        "SELECT lines_total, lines_fully_received, fully_received "
        "FROM trex.po_receipt_status WHERE po_id = %s",
        (po_id,),
    ).fetchone()
    assert row
    return int(row[0]), int(row[1]), bool(row[2])


def test_receipt_view_fully_received_only_after_all_lines(conn: psycopg.Connection) -> None:
    vid = _vendor(conn)
    row = conn.execute(
        "INSERT INTO trex.purchase_orders (po_number, vendor_id, order_date) "
        "VALUES ('TEST-PO-1', %s, '2026-01-01') RETURNING id",
        (vid,),
    ).fetchone()
    assert row
    po_id = int(row[0])
    line_ids = []
    for n, qty in ((1, "10"), (2, "5")):
        r = conn.execute(
            "INSERT INTO trex.po_lines (po_id, line_no, part_no, qty_ordered, unit_price) "
            "VALUES (%s, %s, %s, %s, 1.00) RETURNING id",
            (po_id, n, f"P{n}", Decimal(qty)),
        ).fetchone()
        assert r
        line_ids.append(int(r[0]))
    r = conn.execute(
        "INSERT INTO trex.receiving_reports (rr_number, po_id) VALUES ('TEST-RR-1', %s) "
        "RETURNING id",
        (po_id,),
    ).fetchone()
    assert r
    rr_id = int(r[0])

    def receive(line_id: int, qty: str) -> None:
        conn.execute(
            "INSERT INTO trex.receiving_lines (rr_id, po_line_id, qty_received) "
            "VALUES (%s, %s, %s)",
            (rr_id, line_id, Decimal(qty)),
        )

    assert _status(conn, po_id) == (2, 0, False)  # nothing received
    receive(line_ids[0], "4")
    assert _status(conn, po_id) == (2, 0, False)  # partial line 1
    receive(line_ids[0], "6")
    assert _status(conn, po_id) == (2, 1, False)  # line 1 complete, line 2 untouched
    receive(line_ids[1], "5")
    assert _status(conn, po_id) == (2, 2, True)  # all lines received

    per_line = conn.execute(
        "SELECT line_no, qty_ordered, qty_received FROM trex.po_line_receipt "
        "WHERE po_id = %s ORDER BY line_no",
        (po_id,),
    ).fetchall()
    assert per_line == [
        (1, Decimal("10.000"), Decimal("10.000")),
        (2, Decimal("5.000"), Decimal("5.000")),
    ]


def test_po_without_lines_is_not_fully_received(conn: psycopg.Connection) -> None:
    vid = _vendor(conn)
    row = conn.execute(
        "INSERT INTO trex.purchase_orders (po_number, vendor_id, order_date) "
        "VALUES ('TEST-PO-2', %s, '2026-01-01') RETURNING id",
        (vid,),
    ).fetchone()
    assert row
    assert _status(conn, int(row[0])) == (0, 0, False)
