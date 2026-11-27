"""Poller SQL against the real database. Everything runs in a transaction that is rolled back."""

from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import psycopg
import pytest

from ap_agent.notify import NotifyEvent, notify
from ap_agent.poller import (
    ParkedRow,
    _alert,
    find_discount_expiring,
    find_near_due,
    find_received,
)
from data_gen.simulate import apply_day
from tools_seed import connect, integration, seed_po, seed_vendor, uid

pytestmark = integration

TODAY = date(2026, 11, 25)


@pytest.fixture
def conn() -> Iterator[psycopg.Connection[Any]]:
    yield from connect()


def _park(
    c: psycopg.Connection[Any],
    vendor_id: int,
    po_number: str,
    status: str = "WAITING",
    due: date | None = None,
    discount: date | None = None,
) -> str:
    thread = f"invoice-t{uid().lower()}"
    c.execute(
        "INSERT INTO agent.parked_invoices (thread_id, content_hash, status, vendor_id, po_number, "
        "invoice_number, due_date, discount_deadline, currency, total) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'CAD', 100)",
        (
            thread,
            thread.removeprefix("invoice-"),
            status,
            vendor_id,
            po_number,
            f"INV-{uid()}",
            due,
            discount,
        ),
    )
    return thread


def _delivery(po_number: str, line_no: int, qty: str) -> dict[str, object]:
    return {
        "po_number": po_number,
        "rr_number": f"RR-{uid()}",
        "delivery_date": "2026-11-20",
        "received_at": "2026-11-20T10:00:00+00:00",
        "received_by": "test",
        "lines": [{"line_no": line_no, "qty": qty}],
    }


def test_find_received_returns_only_fully_received_pos(conn: psycopg.Connection[Any]) -> None:
    vid = seed_vendor(conn, f"Poller {uid()}")
    lines = [("P-1", "a", "5", "10.00"), ("P-2", "b", "3", "20.00")]
    _, _ = seed_po(conn, vid, lines, po_number=(partial := f"TPO-{uid()}"))
    seed_po(conn, vid, lines, po_number=(full := f"TPO-{uid()}"))
    t_partial, t_full = _park(conn, vid, partial), _park(conn, vid, full)
    t_ready = _park(conn, vid, full, status="READY")  # not WAITING -> never resumed

    schedule = [
        _delivery(partial, 1, "5"),  # one of two lines
        _delivery(full, 1, "5"),
        _delivery(full, 2, "3"),
    ]
    assert apply_day(conn, date(2026, 11, 20), schedule)
    found = {r.thread_id for r in find_received(conn)}
    assert t_full in found
    assert t_partial not in found and t_ready not in found

    assert apply_day(conn, date(2026, 11, 20), schedule) == []  # simulator is idempotent
    assert {r.thread_id for r in find_received(conn)} == found

    apply_day(conn, date(2026, 11, 21), [_delivery(partial, 2, "3")])  # finish the partial PO
    assert t_partial in {r.thread_id for r in find_received(conn)}


def test_find_received_ignores_a_po_number_that_belongs_to_another_vendor(
    conn: psycopg.Connection[Any],
) -> None:
    v1, v2 = seed_vendor(conn, f"Poller {uid()}"), seed_vendor(conn, f"Poller {uid()}")
    po = f"TPO-{uid()}"
    seed_po(conn, v1, [("P-1", "a", "1", "10.00")], po_number=po)
    thread = _park(conn, v2, po)
    apply_day(conn, date(2026, 11, 20), [_delivery(po, 1, "1")])
    assert thread not in {r.thread_id for r in find_received(conn)}


def _rows(rows: list[ParkedRow]) -> set[str]:
    return {r.thread_id for r in rows}


def test_near_due_window_and_status(conn: psycopg.Connection[Any]) -> None:
    vid = seed_vendor(conn, f"Poller {uid()}")
    inside = _park(conn, vid, "X", due=TODAY + timedelta(days=7))
    overdue = _park(conn, vid, "X", due=TODAY - timedelta(days=2))
    outside = _park(conn, vid, "X", due=TODAY + timedelta(days=8))
    ready = _park(conn, vid, "X", status="READY", due=TODAY + timedelta(days=1))
    found = _rows(find_near_due(conn, TODAY))
    assert {inside, overdue} <= found
    assert outside not in found and ready not in found


def test_near_due_alert_is_sent_once_per_day(conn: psycopg.Connection[Any]) -> None:
    vid = seed_vendor(conn, f"Poller {uid()}")
    thread = _park(conn, vid, "X", due=TODAY + timedelta(days=3))
    sent: list[NotifyEvent] = []

    def send(today: date) -> int:
        n = 0
        for row in find_near_due(conn, today):
            if row.thread_id == thread:
                _alert(
                    conn,
                    row,
                    "NEAR_DUE",
                    "NEAR_DUE_ALERT",
                    today,
                    lambda c, e: sent.append(e) or True,
                )
                n += 1
        return n

    assert send(TODAY) == 1
    assert send(TODAY) == 0  # same day: already alerted
    assert send(TODAY + timedelta(days=1)) == 1  # next day: alerted again
    assert len(sent) == 2
    audit = conn.execute(
        "SELECT count(*) FROM agent.audit_log WHERE action = 'NEAR_DUE_ALERT' AND ref = %s",
        (thread,),
    ).fetchone()
    assert audit == (2,)


def test_discount_window_covers_ready_and_waiting(conn: psycopg.Connection[Any]) -> None:
    vid = seed_vendor(conn, f"Poller {uid()}")
    edge = _park(conn, vid, "X", status="READY", discount=TODAY + timedelta(days=3))
    waiting = _park(conn, vid, "X", discount=TODAY)
    late = _park(conn, vid, "X", status="READY", discount=TODAY + timedelta(days=4))
    gone = _park(conn, vid, "X", status="READY", discount=TODAY - timedelta(days=1))
    exception = _park(conn, vid, "X", status="EXCEPTION", discount=TODAY + timedelta(days=1))
    found = _rows(find_discount_expiring(conn, TODAY))
    assert {edge, waiting} <= found
    assert not ({late, gone, exception} & found)


def test_notify_writes_inbox_row_once_for_po_received(conn: psycopg.Connection[Any]) -> None:
    thread = f"invoice-n{uid().lower()}"
    event = NotifyEvent(kind="PO_RECEIVED", thread_id=thread, invoice_number="INV-1", today=TODAY)
    no_slack = {"webhook_url": lambda: None}
    assert notify(conn, event, **no_slack) is True
    assert notify(conn, event, **no_slack) is False  # woken once, announced once
    row = conn.execute(
        "SELECT kind, title, read_at FROM agent.notifications WHERE thread_id = %s", (thread,)
    ).fetchall()
    assert row == [("PO_RECEIVED", "Invoice INV-1 ready for review", None)]
