"""End to end: park a thread, receive the PO, run_once wakes it. Needs committed rows (the
checkpointer uses autocommit connections), so everything is removed again in teardown."""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from ap_agent.graph.checkpointer import make_checkpointer, make_pool, setup_checkpointer
from ap_agent.graph.deps import prod_deps
from ap_agent.graph.graph import build_graph
from ap_agent.graph.runner import run_thread
from ap_agent.notify import notify
from ap_agent.poller import run_once
from graph_fakes import FakeWorld, initial_state
from tools_seed import DATABASE_URL, integration

pytestmark = integration


@dataclass
class Env:
    graph: Any
    world: FakeWorld
    thread_id: str
    po_number: str
    po_id: int
    line_id: int


@pytest.fixture
def env() -> Iterator[Env]:
    assert DATABASE_URL is not None
    setup_checkpointer(DATABASE_URL)
    code = uuid.uuid4().hex[:10].upper()
    thread_id = f"invoice-poll{code.lower()}"
    po_number = f"TPO-{code}"
    with psycopg.connect(DATABASE_URL, autocommit=True) as c:
        vendor_id = c.execute(
            "INSERT INTO trex.vendors (vendor_code, name, payment_terms_days) "
            "VALUES (%s, %s, 30) RETURNING id",
            (f"T-{code}", f"Poll Test {code}"),
        ).fetchone()[0]  # type: ignore[index]
        po_id = c.execute(
            "INSERT INTO trex.purchase_orders (po_number, vendor_id, order_date, status, total) "
            "VALUES (%s, %s, '2026-10-01', 'OPEN', 10) RETURNING id",
            (po_number, vendor_id),
        ).fetchone()[0]  # type: ignore[index]
        line_id = c.execute(
            "INSERT INTO trex.po_lines (po_id, line_no, part_no, qty_ordered, unit_price) "
            "VALUES (%s, 1, 'P-1', 5, 2) RETURNING id",
            (po_id,),
        ).fetchone()[0]  # type: ignore[index]
    world = FakeWorld()
    world.vendor = {"vendor_id": vendor_id, "method": "exact_name", "score": 100.0}
    world.po = {**world.po, "po_id": po_id, "po_number": po_number}
    world.fully_received = False
    deps = world.deps()
    deps.upsert_parked = prod_deps(DATABASE_URL).upsert_parked
    pool = make_pool(DATABASE_URL)
    saver = make_checkpointer(pool)
    try:
        yield Env(build_graph(deps, saver), world, thread_id, po_number, po_id, line_id)
    finally:
        saver.delete_thread(thread_id)
        pool.close()
        with psycopg.connect(DATABASE_URL, autocommit=True) as c:
            c.execute("DELETE FROM agent.notifications WHERE thread_id = %s", (thread_id,))
            c.execute("DELETE FROM agent.audit_log WHERE ref = %s", (thread_id,))
            c.execute("DELETE FROM agent.parked_invoices WHERE thread_id = %s", (thread_id,))
            c.execute("DELETE FROM trex.receiving_reports WHERE po_id = %s", (po_id,))
            c.execute("DELETE FROM trex.po_lines WHERE po_id = %s", (po_id,))
            c.execute("DELETE FROM trex.purchase_orders WHERE id = %s", (po_id,))
            c.execute("DELETE FROM trex.vendors WHERE id = %s", (vendor_id,))


def _receive(env: Env) -> None:
    assert DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL, autocommit=True) as c:
        rr = c.execute(
            "INSERT INTO trex.receiving_reports (rr_number, po_id) VALUES (%s, %s) RETURNING id",
            (f"RR-{uuid.uuid4().hex[:8]}", env.po_id),
        ).fetchone()
        assert rr
        c.execute(
            "INSERT INTO trex.receiving_lines (rr_id, po_line_id, qty_received) VALUES (%s, %s, 5)",
            (rr[0], env.line_id),
        )


def test_run_once_wakes_only_after_the_po_is_fully_received(env: Env) -> None:
    assert DATABASE_URL is not None
    s = run_thread(env.graph, {**initial_state(env.thread_id)})
    assert s.status == "WAITING"
    now = datetime(2026, 11, 25, 9, 0, tzinfo=UTC)

    def poll() -> Any:
        with psycopg.connect(DATABASE_URL, autocommit=True) as c:
            return run_once(
                env.graph,
                c,
                now=now,
                notifier=lambda c2, e: notify(c2, e, webhook_url=lambda: None),
            )

    assert poll().resumed == 0  # nothing received yet: left alone
    with psycopg.connect(DATABASE_URL) as c:
        assert c.execute(
            "SELECT status FROM agent.parked_invoices WHERE thread_id = %s", (env.thread_id,)
        ).fetchone() == ("WAITING",)

    env.world.fully_received = True  # the fake tool mirrors the real receipt now
    _receive(env)
    counts = poll()
    assert counts.resumed == 1 and counts.errors == 0

    with psycopg.connect(DATABASE_URL) as c:
        status, ready_at, anomalies = c.execute(
            "SELECT status, ready_at, anomalies FROM agent.parked_invoices WHERE thread_id = %s",
            (env.thread_id,),
        ).fetchone()  # type: ignore[misc]
        # (the fake invoice is due within 7 days of `now`, so a NEAR_DUE alert is expected too)
        notes = c.execute(
            "SELECT body FROM agent.notifications WHERE thread_id = %s AND kind = 'PO_RECEIVED'",
            (env.thread_id,),
        ).fetchall()
    assert status == "READY" and ready_at is not None
    assert not any("not fully received" in a for a in anomalies)  # stale reason dropped
    assert len(notes) == 1 and "fully received" in notes[0][0]

    assert poll().resumed == 0  # READY now: not woken or announced twice
    with psycopg.connect(DATABASE_URL) as c:
        n = c.execute(
            "SELECT count(*) FROM agent.notifications WHERE thread_id = %s AND kind = 'PO_RECEIVED'",
            (env.thread_id,),
        ).fetchone()
    assert n == (1,)
