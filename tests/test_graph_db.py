"""Integration: real PostgresSaver (schema langgraph) + real agent.parked_invoices upsert.

Tools are fakes; only persistence is real. Rows are committed (the checkpointer needs autocommit
connections) and removed again in the fixture teardown.
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest

from ap_agent.graph.checkpointer import make_checkpointer, make_pool, setup_checkpointer
from ap_agent.graph.deps import prod_deps
from ap_agent.graph.graph import build_graph
from ap_agent.graph.runner import resume_thread, run_thread, summarize
from graph_fakes import FakeWorld, initial_state
from tools_seed import DATABASE_URL, integration

pytestmark = integration


@dataclass
class Env:
    url: str
    world: FakeWorld
    graph: Any
    thread_id: str


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    assert DATABASE_URL is not None
    setup_checkpointer(DATABASE_URL)
    code = uuid.uuid4().hex[:10].upper()
    thread_id = f"invoice-test{code.lower()}"
    with psycopg.connect(DATABASE_URL, autocommit=True) as c:
        row = c.execute(
            "INSERT INTO trex.vendors (vendor_code, name, payment_terms_days) "
            "VALUES (%s, %s, 30) RETURNING id",
            (f"T-{code}", f"Graph Test {code}"),
        ).fetchone()
        assert row
        vendor_id = int(row[0])
    world = FakeWorld()
    world.vendor = {"vendor_id": vendor_id, "method": "exact_name", "score": 100.0}
    world.fully_received = False
    deps = world.deps()
    deps.upsert_parked = prod_deps(DATABASE_URL).upsert_parked  # real DB write
    pool = make_pool(DATABASE_URL)
    saver = make_checkpointer(pool)
    graph = build_graph(deps, saver)
    try:
        yield Env(DATABASE_URL, world, graph, thread_id)
    finally:
        saver.delete_thread(thread_id)
        pool.close()
        with psycopg.connect(DATABASE_URL, autocommit=True) as c:
            c.execute("DELETE FROM agent.parked_invoices WHERE thread_id = %s", (thread_id,))
            c.execute("DELETE FROM trex.vendors WHERE id = %s", (vendor_id,))


def _parked(url: str, thread_id: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(url) as c:
        return c.execute(
            "SELECT status, po_number, invoice_number, anomalies FROM agent.parked_invoices "
            "WHERE thread_id = %s",
            (thread_id,),
        ).fetchall()


def test_checkpoint_tables_live_in_langgraph_schema(env: Env) -> None:
    with psycopg.connect(env.url) as c:
        rows = c.execute(
            "SELECT table_schema FROM information_schema.tables WHERE table_name = 'checkpoints'"
        ).fetchall()
    assert [r[0] for r in rows] == ["langgraph"]


def test_parked_thread_has_checkpoint_state_and_waiting_row(env: Env) -> None:
    s = run_thread(env.graph, {**initial_state(env.thread_id)})
    assert s.status == "WAITING" and s.waiting_for == "po_fully_received"

    # state survives in the checkpointer: a fresh summary reads it back from Postgres
    again = summarize(env.graph, env.thread_id)
    assert again.status == "WAITING" and again.invoice_number == "INV-1"
    snap = env.graph.get_state({"configurable": {"thread_id": env.thread_id}})
    assert snap.values["history"][-1].startswith("park")

    rows = _parked(env.url, env.thread_id)
    assert len(rows) == 1 and rows[0][0] == "WAITING" and rows[0][1] == "PO-1"


def test_rerunning_the_terminal_node_is_idempotent(env: Env) -> None:
    run_thread(env.graph, {**initial_state(env.thread_id)})
    assert resume_thread(env.graph, env.thread_id).status == "WAITING"  # no-op: it is paused
    deps_upsert = prod_deps(env.url).upsert_parked
    row = {
        "thread_id": env.thread_id,
        "content_hash": env.thread_id.removeprefix("invoice-"),
        "s3_key": None,
        "status": "WAITING",
        "vendor_id": env.world.vendor["vendor_id"],
        "po_number": "PO-1",
        "invoice_number": "INV-1",
        "invoice_date": "2026-11-01",
        "due_date": None,
        "currency": "CAD",
        "total": "113.00",
        "extracted_json": {},
        "anomalies": [],
    }
    deps_upsert(row)
    deps_upsert(row)
    assert len(_parked(env.url, env.thread_id)) == 1


def test_crash_after_match_po_resumes_without_extracting_again(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AP_CRASH_AFTER", "match_po")
    crashed = run_thread(env.graph, {**initial_state(env.thread_id)})
    assert crashed.error and crashed.next_nodes == ("match_po",)
    monkeypatch.delenv("AP_CRASH_AFTER")

    done = resume_thread(env.graph, env.thread_id)
    assert done.status == "WAITING"
    assert env.world.calls.count("extract") == 1
    assert _parked(env.url, env.thread_id)[0][0] == "WAITING"


def test_upsert_stores_anomaly_report_and_embedding(env: Env) -> None:
    row = {
        "thread_id": env.thread_id,
        "content_hash": env.thread_id.removeprefix("invoice-"),
        "s3_key": None,
        "status": "READY",
        "vendor_id": env.world.vendor["vendor_id"],
        "po_number": "PO-1",
        "invoice_number": "INV-1",
        "invoice_date": "2026-11-01",
        "due_date": None,
        "currency": "CAD",
        "total": "113.00",
        "extracted_json": {},
        "anomalies": ["x"],
        "anomaly_report": {"flags": [], "explanation": "e", "summary_text": "Acme | 1 x P | 1 CAD"},
    }
    upsert = prod_deps(env.url).upsert_parked
    upsert(row)
    upsert(row)
    with psycopg.connect(env.url) as c:
        got = c.execute(
            "SELECT anomaly_report ->> 'explanation', vector_dims(embedding) "
            "FROM agent.parked_invoices WHERE thread_id = %s",
            (env.thread_id,),
        ).fetchall()
    assert got == [("e", 384)]
