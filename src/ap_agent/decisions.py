"""Human decisions: the only way the UI (and, in Phase 8, the API) changes anything.

`submit_decision` resumes a paused invoice thread with Command(resume=...). The rules live in
graph.decision_rules and are enforced again inside the graph; checking here too gives a clear error
before anything runs. Nothing here initiates a payment.
"""

import threading
from collections.abc import Sequence
from typing import Any

import psycopg
from langgraph.types import Command

from ap_agent.config import get_database_url
from ap_agent.graph.decision_rules import validate_decision
from ap_agent.graph.runner import Graph, config_for

PAUSES = ("human_approval", "human_review")

_lock = threading.Lock()
_graph: Graph | None = None


class DecisionError(ValueError):
    """The decision was refused (unknown thread, not waiting, or it breaks a rule)."""


def _default_graph() -> Graph:
    """Real graph on DATABASE_URL, built once per process (pool + checkpointer)."""
    global _graph
    with _lock:
        if _graph is None:
            url = get_database_url()
            if not url:
                raise RuntimeError("DATABASE_URL is not set; see .env.example.")
            from ap_agent.graph.checkpointer import make_checkpointer, make_pool
            from ap_agent.graph.deps import prod_deps
            from ap_agent.graph.graph import build_graph

            _graph = build_graph(prod_deps(url), make_checkpointer(make_pool(url)))
        return _graph


def submit_decision(thread_id: str, decision: dict[str, Any], *, graph: Graph | None = None) -> str:
    """Resume `thread_id` with a decision and return its new status.

    approve | reject | hold for a READY invoice;
    resolve (vendor_id?, po_id?) | reject for an exception. Always with `user` and `note`.
    Raises DecisionError when the thread is unknown or not waiting for a decision, or when the
    decision breaks a rule (for example approving past a block flag without a note).
    """
    g = graph or _default_graph()
    snap = g.get_state(config_for(thread_id))
    if not snap.created_at:
        raise DecisionError(f"unknown thread {thread_id}")
    values = dict(snap.values or {})
    waiting = next((i.value.get("waiting_for") for t in snap.tasks for i in t.interrupts), None)
    if waiting not in PAUSES:
        raise DecisionError(
            f"thread {thread_id} is not waiting for a decision "
            f"(status {values.get('status', 'NEW')}, waiting_for {waiting})"
        )
    error = validate_decision(values, waiting, decision)
    if error:
        raise DecisionError(error)
    g.invoke(Command(resume=decision), config_for(thread_id))
    after = dict(g.get_state(config_for(thread_id)).values or {})
    if after.get("decision_error"):  # the graph refused it (state changed under us)
        raise DecisionError(str(after["decision_error"]))
    return str(after.get("status", "NEW"))


def mark_notifications_read(ids: Sequence[int] | None = None) -> int:
    """Mark notifications read (all unread when `ids` is None). Returns how many changed."""
    url = get_database_url()
    if not url:
        raise RuntimeError("DATABASE_URL is not set; see .env.example.")
    with psycopg.connect(url, autocommit=True, connect_timeout=10) as conn:
        cur = conn.execute(
            "UPDATE agent.notifications SET read_at = now() "
            "WHERE read_at IS NULL AND (%(ids)s::bigint[] IS NULL OR id = ANY(%(ids)s))",
            {"ids": list(ids) if ids is not None else None},
        )
        return cur.rowcount
