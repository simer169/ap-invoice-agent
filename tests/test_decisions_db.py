"""attach_invoice and the approval-card queries against the real schema.

attach_invoice commits on its own connection, so rows are committed and removed in teardown.
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from ap_agent import ui_queries as q
from ap_agent.graph.deps import AttachError, prod_deps
from tools_seed import DATABASE_URL, integration, migrate

pytestmark = integration

INV = {
    "invoice_number": "ATT-1",
    "invoice_date": "2026-11-01",
    "currency": "CAD",
    "total": "113.00",
}


@dataclass
class Env:
    vendor_id: int
    thread_id: str
    code: str


@pytest.fixture
def env() -> Iterator[Env]:
    assert DATABASE_URL is not None
    code = uuid.uuid4().hex[:8]
    thread_id = f"invoice-att{code}"
    with psycopg.connect(DATABASE_URL, autocommit=True) as c:
        migrate(c)
        vendor_id = c.execute(
            "INSERT INTO trex.vendors (vendor_code, name) VALUES (%s, %s) RETURNING id",
            (f"ATT-{code}", f"Attach Test {code}"),
        ).fetchone()[0]  # type: ignore[index]
        c.execute(
            "INSERT INTO agent.parked_invoices (thread_id, content_hash, vendor_id, status, "
            "invoice_number, total, currency, due_date, anomaly_report) "
            "VALUES (%s, %s, %s, 'READY', 'ATT-1', 113, 'CAD', '2026-12-01', %s)",
            (thread_id, code * 8, vendor_id, Jsonb({"flags": [], "explanation": "fine"})),
        )
    try:
        yield Env(vendor_id, thread_id, code)
    finally:
        with psycopg.connect(DATABASE_URL, autocommit=True) as c:
            c.execute("DELETE FROM trex.invoices WHERE vendor_id = %s", (vendor_id,))
            c.execute("DELETE FROM agent.parked_invoices WHERE thread_id = %s", (thread_id,))
            c.execute("DELETE FROM trex.vendors WHERE id = %s", (vendor_id,))


def _rows(vendor_id: int) -> list[tuple[Any, ...]]:
    assert DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL, autocommit=True) as c:
        return c.execute(
            "SELECT status, attached_at, total FROM trex.invoices WHERE vendor_id = %s",
            (vendor_id,),
        ).fetchall()


def test_attach_twice_creates_one_trex_row_and_marks_parked_attached(env: Env) -> None:
    assert DATABASE_URL is not None
    attach = prod_deps(DATABASE_URL).attach_invoice
    first = attach(env.thread_id, env.vendor_id, None, INV, "dana", "ok")
    second = attach(env.thread_id, env.vendor_id, None, INV, "dana", "ok")
    assert first == second
    rows = _rows(env.vendor_id)
    assert len(rows) == 1 and rows[0][0] == "ATTACHED" and rows[0][1] is not None
    with psycopg.connect(DATABASE_URL, autocommit=True) as c:
        status, by, decision = c.execute(
            "SELECT status, decided_by, decision FROM agent.parked_invoices WHERE thread_id = %s",
            (env.thread_id,),
        ).fetchone()  # type: ignore[misc]
    assert (status, by, decision) == ("ATTACHED", "dana", "approve")


def test_attach_adopts_a_pending_invoice_but_never_touches_a_paid_one(env: Env) -> None:
    assert DATABASE_URL is not None
    attach = prod_deps(DATABASE_URL).attach_invoice
    with psycopg.connect(DATABASE_URL, autocommit=True) as c:
        c.execute(
            "INSERT INTO trex.invoices (vendor_id, invoice_number, invoice_date, total, status) "
            "VALUES (%s, 'ATT-1', '2026-11-01', 100, 'PENDING')",
            (env.vendor_id,),
        )
    attach(env.thread_id, env.vendor_id, None, INV, "dana", "")
    assert [r[0] for r in _rows(env.vendor_id)] == ["ATTACHED"]

    with psycopg.connect(DATABASE_URL, autocommit=True) as c:
        c.execute("UPDATE trex.invoices SET status = 'PAID' WHERE vendor_id = %s", (env.vendor_id,))
    with pytest.raises(AttachError):
        attach(env.thread_id, env.vendor_id, None, INV, "dana", "")
    assert [r[0] for r in _rows(env.vendor_id)] == ["PAID"]


def test_cards_per_tab_counts_and_bell(env: Env) -> None:
    assert DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL, autocommit=True) as c:
        ready = [x for x in q.list_approval_cards(c, "ready", date(2026, 11, 26))]
        mine = next(x for x in ready if x.thread_id == env.thread_id)
        assert mine.days_left == 5 and mine.explanation == "fine"
        assert mine.vendor_name == f"Attach Test {env.code}"
        assert q.approval_counts(c)["ready"] >= 1
        assert len(q.list_approval_cards(c, "decided", date(2026, 11, 26))) <= q.DECIDED_LIMIT
        before = q.unread_notification_count(c)
        c.execute(
            "INSERT INTO agent.notifications (thread_id, kind, title, body) "
            "VALUES (%s, 'NEAR_DUE', 't', 'b')",
            (env.thread_id,),
        )
        try:
            assert q.unread_notification_count(c) == before + 1
            assert any(n.thread_id == env.thread_id and not n.read for n in q.list_notifications(c))
        finally:
            c.execute("DELETE FROM agent.notifications WHERE thread_id = %s", (env.thread_id,))
