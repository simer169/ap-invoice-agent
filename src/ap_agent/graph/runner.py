"""Run, resume and inspect invoice threads. Intake (the watcher) and processing are decoupled:
the watcher only writes RUN_REQUESTED audit rows; this module turns them into graph runs."""

from dataclasses import dataclass, field
from typing import Any

import psycopg
from langgraph.graph.state import CompiledStateGraph

from ap_agent.graph.state import InvoiceState

_PENDING_SQL = """
SELECT a.ref, a.details ->> 's3_key', a.details -> 'email'
FROM agent.audit_log a
WHERE a.action = 'RUN_REQUESTED' AND a.ref LIKE 'invoice-%%'
  AND NOT EXISTS (SELECT 1 FROM langgraph.checkpoints c WHERE c.thread_id = a.ref)
ORDER BY a.id
"""

Graph = CompiledStateGraph[InvoiceState, Any, Any, Any]


@dataclass
class ThreadSummary:
    thread_id: str
    vendor: str = ""
    invoice_number: str = ""
    po_number: str = ""
    status: str = "NEW"
    reasons: list[str] = field(default_factory=list)
    waiting_for: str | None = None
    next_nodes: tuple[str, ...] = ()
    error: str | None = None


def config_for(thread_id: str) -> Any:
    return {"configurable": {"thread_id": thread_id}}


def pending_threads(conn: psycopg.Connection[Any]) -> list[InvoiceState]:
    """Initial state for every RUN_REQUESTED content hash that has no graph thread yet."""
    out: list[InvoiceState] = []
    for ref, s3_key, email in conn.execute(_PENDING_SQL).fetchall():
        if not s3_key:
            continue
        email = email or {}
        out.append(
            {
                "thread_id": ref,
                "content_hash": ref.removeprefix("invoice-"),
                "s3_key": s3_key,
                "email_meta": {
                    "message_id": email.get("message_id"),
                    "from_address": email.get("from"),
                    "subject": email.get("subject"),
                    "received_at": email.get("received_at"),
                },
                "status": "NEW",
                "reasons": [],
                "history": [],
            }
        )
    return out


def summarize(graph: Graph, thread_id: str) -> ThreadSummary:
    snap = graph.get_state(config_for(thread_id))
    v = snap.values or {}
    waiting = next((i.value.get("waiting_for") for t in snap.tasks for i in t.interrupts), None)
    inv = v.get("extraction") or {}
    return ThreadSummary(
        thread_id=thread_id,
        vendor=inv.get("vendor_name", ""),
        invoice_number=inv.get("invoice_number", ""),
        po_number=(v.get("po") or {}).get("po_number") or inv.get("po_number") or "",
        status=v.get("status", "NEW"),
        reasons=list(v.get("reasons") or []),
        waiting_for=waiting,
        next_nodes=tuple(snap.next),
    )


def run_thread(graph: Graph, state: InvoiceState) -> ThreadSummary:
    """Run a new thread until it ends or interrupts. A crash is reported, not raised."""
    thread_id = state["thread_id"]
    try:
        graph.invoke(state, config_for(thread_id))
    except Exception as exc:  # noqa: BLE001 - one bad thread must not stop the batch
        s = summarize(graph, thread_id)
        s.error = f"{type(exc).__name__}: {str(exc)[:200]}"
        return s
    return summarize(graph, thread_id)


def resume_thread(graph: Graph, thread_id: str) -> ThreadSummary:
    """Continue a thread that crashed mid-run (invoke with None). Waiting threads are left alone."""
    snap = graph.get_state(config_for(thread_id))
    if not snap.created_at:
        raise LookupError(f"no saved state for {thread_id}")
    if not snap.next:
        return summarize(graph, thread_id)  # already finished
    if any(t.interrupts for t in snap.tasks):
        return summarize(graph, thread_id)  # paused for a human / receipt, not crashed
    graph.invoke(None, config_for(thread_id))
    return summarize(graph, thread_id)
