"""submit_decision: the single write path. Fakes only (no DB, no LLM)."""

from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from ap_agent.decisions import DecisionError, submit_decision
from ap_agent.graph.graph import build_graph
from ap_agent.graph.runner import run_thread
from graph_fakes import FakeWorld, initial_state

TID = "invoice-abc"
BLOCK = {"code": "X", "severity": "block", "message": "m", "evidence": {}}


def _graph(world: FakeWorld) -> Any:
    graph = build_graph(world.deps(), InMemorySaver())
    run_thread(graph, initial_state(TID))
    return graph


def test_approve_returns_new_status() -> None:
    w = FakeWorld()
    g = _graph(w)
    assert submit_decision(TID, {"decision": "approve", "user": "dana", "note": ""}, graph=g) == (
        "ATTACHED"
    )
    assert w.attach_calls == 1


def test_hold_and_reject_statuses() -> None:
    w = FakeWorld()
    g = _graph(w)
    assert submit_decision(TID, {"decision": "hold", "user": "d", "note": ""}, graph=g) == "READY"
    assert (
        submit_decision(TID, {"decision": "reject", "user": "d", "note": "no"}, graph=g)
        == "REJECTED"
    )


def test_second_approve_is_refused_and_creates_no_second_row() -> None:
    w = FakeWorld()
    g = _graph(w)
    submit_decision(TID, {"decision": "approve", "user": "dana", "note": ""}, graph=g)
    with pytest.raises(DecisionError, match="not waiting for a decision"):
        submit_decision(TID, {"decision": "approve", "user": "dana", "note": ""}, graph=g)
    assert w.attach_calls == 1 and len(w.trex) == 1


def test_block_flag_without_note_is_refused_and_nothing_runs() -> None:
    w = FakeWorld()
    w.anomaly = {"flags": [BLOCK], "similar_invoices": [], "explanation": None}
    g = _graph(w)
    with pytest.raises(DecisionError, match="override reason"):
        submit_decision(TID, {"decision": "approve", "user": "dana", "note": " "}, graph=g)
    assert w.attach_calls == 0
    assert (
        submit_decision(TID, {"decision": "approve", "user": "dana", "note": "ok"}, graph=g)
        == "ATTACHED"
    )


def test_waiting_for_receipt_thread_is_not_decidable() -> None:
    w = FakeWorld()
    w.fully_received = False
    g = _graph(w)
    with pytest.raises(DecisionError, match=r"status WAITING, waiting_for po_fully_received"):
        submit_decision(TID, {"decision": "approve", "user": "d", "note": ""}, graph=g)


def test_finished_and_unknown_threads_give_clear_errors() -> None:
    w = FakeWorld()
    w.duplicate = {"kind": "exact", "existing_ref": "invoice-zzz"}
    g = _graph(w)  # rejected as an exact duplicate: finished
    with pytest.raises(DecisionError, match="status REJECTED"):
        submit_decision(TID, {"decision": "approve", "user": "d", "note": ""}, graph=g)
    with pytest.raises(DecisionError, match="unknown thread"):
        submit_decision("invoice-nope", {"decision": "approve", "user": "d"}, graph=g)


def test_exception_resolve_through_submit_decision() -> None:
    w = FakeWorld()
    w.vendor = {"vendor_id": None, "method": "none", "score": 0.0, "candidates": []}
    g = _graph(w)
    assert (
        submit_decision(
            TID, {"decision": "resolve", "vendor_id": 7, "user": "d", "note": ""}, graph=g
        )
        == "READY"
    )
    with pytest.raises(DecisionError):  # approve is not a valid exception decision
        w2 = FakeWorld()
        w2.vendor = {"vendor_id": None, "method": "none", "score": 0.0, "candidates": []}
        submit_decision(TID, {"decision": "approve", "user": "d", "note": "x"}, graph=_graph(w2))
