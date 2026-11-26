"""Seed helpers for the tools integration tests. Everything is inserted inside the caller's
transaction (rolled back by the fixture), with unique codes so demo data never collides."""

import sys
import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest

from ap_agent.config import get_database_url

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from migrate import migrate

DATABASE_URL = get_database_url()

integration = [
    pytest.mark.integration,
    pytest.mark.skipif(DATABASE_URL is None, reason="DATABASE_URL not set"),
]

# (part_no, description, qty_ordered, unit_price)
SeedLine = tuple[str, str, str, str]


def connect() -> Iterator[psycopg.Connection[Any]]:
    assert DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL, connect_timeout=10) as c:
        migrate(c)
        try:
            yield c
        finally:
            c.rollback()


def uid() -> str:
    return uuid.uuid4().hex[:10].upper()


def seed_vendor(
    c: psycopg.Connection[Any],
    name: str,
    domain: str | None = None,
    days: int = 30,
    discount_pct: str = "0",
    discount_days: int = 0,
) -> int:
    row = c.execute(
        "INSERT INTO trex.vendors (vendor_code, name, email_domain, payment_terms_days, "
        "discount_pct, discount_days) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (f"T-{uid()}", name, domain, days, discount_pct, discount_days),
    ).fetchone()
    assert row is not None
    return int(row[0])


def seed_po(
    c: psycopg.Connection[Any],
    vendor_id: int,
    lines: list[SeedLine],
    po_number: str | None = None,
    order_date: date = date(2026, 10, 1),
    status: str = "OPEN",
) -> tuple[int, list[int]]:
    total = sum((Decimal(q) * Decimal(p) for _, _, q, p in lines), Decimal(0))
    row = c.execute(
        "INSERT INTO trex.purchase_orders (po_number, vendor_id, order_date, status, total) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (po_number or f"TPO-{uid()}", vendor_id, order_date, status, total),
    ).fetchone()
    assert row is not None
    po_id = int(row[0])
    line_ids: list[int] = []
    for n, (part, desc, qty, price) in enumerate(lines, start=1):
        r = c.execute(
            "INSERT INTO trex.po_lines (po_id, line_no, part_no, description, qty_ordered, "
            "unit_price) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (po_id, n, part, desc, qty, price),
        ).fetchone()
        assert r is not None
        line_ids.append(int(r[0]))
    return po_id, line_ids


def receive(c: psycopg.Connection[Any], po_id: int, line_id: int, qty: str) -> None:
    row = c.execute(
        "INSERT INTO trex.receiving_reports (rr_number, po_id) VALUES (%s, %s) RETURNING id",
        (f"RR-{uid()}", po_id),
    ).fetchone()
    assert row is not None
    c.execute(
        "INSERT INTO trex.receiving_lines (rr_id, po_line_id, qty_received) VALUES (%s, %s, %s)",
        (row[0], line_id, qty),
    )
