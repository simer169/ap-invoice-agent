"""Graph routing, interrupts and crash/resume with fake tools (no DB, no LLM)."""

from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END
from langgraph.types import Command

from ap_agent.graph.graph import (
    build_graph,
    route_after_duplicate,
    route_after_po,
    route_after_receipt,
    route_after_vendor,
    route_after_wait,
)
from ap_agent.graph.nodes import NO_PO_NUMBER_REASON
from ap_agent.graph.runner import config_for, resume_thread, run_thread, summarize
from graph_fakes import FakeWorld, initial_state

TID = "invoice-abc"


def _run(world: FakeWorld) -> tuple[Any, Any]:
    graph = build_graph(world.deps(), InMemorySaver())
    return graph, run_thread(graph, initial_state(TID))


def _interrupt_payload(graph: Any) -> dict[str, Any]:
    snap = graph.get_state({"configurable": {"thread_id": TID}})
    return snap.tasks[0].interrupts[0].value


def test_exact_duplicate_rejects_and_ends() -> None:
    w = FakeWorld()
    w.duplicate = {"kind": "exact", "existing_ref": "invoice-zzz"}
    _, s = _run(w)
    assert s.status == "REJECTED" and s.next_nodes == ()
    assert w.parked == {}  # nothing parked: the original already holds the hash
    assert "match_po" not in w.calls


def test_exact_duplicate_of_itself_is_not_rejected() -> None:
    w = FakeWorld()
    w.duplicate = {"kind": "exact", "existing_ref": TID}
    _, s = _run(w)
    assert s.status == "READY"


def test_unknown_vendor_goes_to_exception() -> None:
    w = FakeWorld()
    w.vendor = {"vendor_id": None, "method": "none", "score": 0.0}
    graph, s = _run(w)
    assert s.status == "EXCEPTION" and s.waiting_for == "human_review"
    assert "Vendor not found" in s.reasons[0]
    assert "check_duplicate" not in w.calls
    assert w.parked[TID]["status"] == "EXCEPTION"
    assert _interrupt_payload(graph)["reasons"] == s.reasons


def test_reissue_goes_to_exception() -> None:
    w = FakeWorld()
    w.duplicate = {"kind": "reissue", "existing_ref": "trex_invoice:5"}
    _, s = _run(w)
    assert s.status == "EXCEPTION" and "reissue" in s.reasons[0]
    assert "match_po" not in w.calls


def test_unmatched_po_goes_to_exception() -> None:
    w = FakeWorld()
    w.po = {"po_id": None, "method": "none", "confidence": 0.0, "reasons": ["no open PO"]}
    _, s = _run(w)
    assert s.status == "EXCEPTION" and "PO not matched: no open PO" in s.reasons
    assert "receipt_status" not in w.calls


def test_not_fully_received_parks_with_payload() -> None:
    w = FakeWorld()
    w.fully_received = False
    graph, s = _run(w)
    assert s.status == "WAITING"
    assert _interrupt_payload(graph) == {"waiting_for": "po_fully_received", "po_number": "PO-1"}
    assert w.parked[TID]["status"] == "WAITING"
    assert "price_variance" not in w.calls


def test_fully_received_goes_through_price_check_to_ready() -> None:
    w = FakeWorld()
    graph, s = _run(w)
    assert s.status == "READY" and s.reasons == []
    payload = _interrupt_payload(graph)
    assert payload["waiting_for"] == "human_approval"
    assert payload["summary"]["invoice_number"] == "INV-1" and payload["flags"] == []
    assert len(w.notifications) == 1 and w.notifications[0]["kind"] == "PO_RECEIVED"
    row = w.parked[TID]
    assert row["status"] == "READY" and row["due_date"] == "2026-12-01"
    assert row["invoice_number"] == "INV-1" and row["po_number"] == "PO-1"
    assert w.calls.index("receipt_status") < w.calls.index("price_variance")


def test_content_scored_match_proceeds_with_confirm_reason() -> None:
    w = FakeWorld()
    w.po = {**w.po, "method": "content_score", "confidence": 0.9}
    _, s = _run(w)
    assert s.status == "READY" and s.reasons == [NO_PO_NUMBER_REASON]
    assert w.parked[TID]["anomalies"] == [NO_PO_NUMBER_REASON]


def test_extraction_failure_goes_to_exception() -> None:
    w = FakeWorld()
    w.extract_fails = True
    _, s = _run(w)
    assert s.status == "EXCEPTION" and s.reasons[0].startswith("extraction failed")
    assert "find_vendor" not in w.calls
    assert w.parked[TID]["invoice_number"] is None


def test_anomaly_flags_do_not_change_route_but_reach_parked_row() -> None:
    w = FakeWorld()
    w.anomaly = {
        "flags": [
            {"code": "PRICE_OVER_TOLERANCE", "severity": "block", "message": "x", "evidence": {}},
            {"code": "DATE_AMBIGUOUS", "severity": "info", "message": "y", "evidence": {}},
        ],
        "similar_invoices": [],
        "explanation": "Price is high.",
    }
    _, s = _run(w)
    assert s.status == "READY"
    row = w.parked[TID]
    assert row["anomaly_report"]["explanation"] == "Price is high."
    assert row["anomalies"] == ["Anomaly PRICE_OVER_TOLERANCE (block): x"]  # info flags not listed


def test_every_node_writes_history_and_audit() -> None:
    w = FakeWorld()
    graph, _ = _run(w)
    history = graph.get_state({"configurable": {"thread_id": TID}}).values["history"]
    ran = [h.split(":")[0] for h in history]
    assert ran == [
        "extract", "validate_vendor", "duplicate_check", "match_po",
        "check_receipt", "price_check", "anomaly_check", "ready",
    ]  # fmt: skip
    assert [a for t, a in w.audits] == [f"NODE_{n}" for n in ran]
    assert {t for t, _ in w.audits} == {TID}


def test_upsert_is_keyed_by_thread() -> None:
    w = FakeWorld()
    _run(w)
    w.deps().upsert_parked({**w.parked[TID]})
    assert len(w.parked) == 1


def test_crash_after_match_po_then_resume_does_not_extract_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = FakeWorld()
    graph = build_graph(w.deps(), InMemorySaver())
    monkeypatch.setenv("AP_CRASH_AFTER", "match_po")
    crashed = run_thread(graph, initial_state(TID))
    assert crashed.error and "simulated crash" in crashed.error
    assert crashed.next_nodes == ("match_po",)
    assert w.calls.count("extract") == 1

    monkeypatch.delenv("AP_CRASH_AFTER")
    done = resume_thread(graph, TID)
    assert done.status == "READY" and done.waiting_for == "human_approval"
    assert w.calls.count("extract") == 1  # never paid twice
    assert w.calls.count("find_vendor") == 1


def test_resume_leaves_waiting_and_unknown_threads_alone() -> None:
    w = FakeWorld()
    graph, _ = _run(w)
    calls = list(w.calls)
    assert resume_thread(graph, TID).status == "READY"
    assert w.calls == calls
    with pytest.raises(LookupError):
        resume_thread(graph, "invoice-nope")
    assert summarize(graph, TID).thread_id == TID


def test_pure_routers() -> None:
    assert route_after_vendor({"vendor": {"vendor_id": 1}}) == "duplicate_check"
    assert route_after_vendor({"vendor": {"vendor_id": None}}) == "exception"
    assert route_after_duplicate({"duplicate": {"kind": "exact"}}) == "reject"
    assert route_after_duplicate({"duplicate": {"kind": "reissue"}}) == "exception"
    assert route_after_duplicate({"duplicate": {"kind": "new"}}) == "match_po"
    assert route_after_po({"po": {"po_id": 0}}) == "check_receipt"
    assert route_after_po({"po": {"po_id": None}}) == "exception"
    assert route_after_receipt({"receipt": {"fully_received": False}}) == "park"
    assert route_after_receipt({"receipt": {"fully_received": True}}) == "price_check"


def test_route_after_wait_loops_only_for_waiting() -> None:
    assert route_after_wait({"status": "WAITING"}) == "check_receipt"
    assert route_after_wait({"status": "READY"}) == END
    assert route_after_wait({"status": "EXCEPTION"}) == END


def test_waiting_thread_resumed_loops_back_and_drops_the_stale_receipt_reason() -> None:
    w = FakeWorld()
    w.fully_received = False
    w.po = {**w.po, "method": "content_score"}  # adds NO_PO_NUMBER_REASON, which must survive
    graph, s = _run(w)
    assert s.status == "WAITING" and any("not fully received" in r for r in s.reasons)

    w.fully_received = True
    graph.invoke(Command(resume={"event": "po_received", "at": "t"}), config_for(TID))
    after = summarize(graph, TID)
    assert after.status == "READY" and after.waiting_for == "human_approval"
    assert after.reasons == [NO_PO_NUMBER_REASON]
    assert w.parked[TID]["status"] == "READY"
    assert w.calls.count("extract") == 1  # only the tail of the graph re-ran
