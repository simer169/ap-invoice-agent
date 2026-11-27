"""Human decisions in the graph: approve / reject / hold / resolve, with fakes (no DB, no LLM)."""

from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from ap_agent.graph.decision_rules import validate_decision
from ap_agent.graph.graph import build_graph, route_after_exception_review, route_after_review
from ap_agent.graph.runner import config_for, run_thread, summarize
from graph_fakes import FakeWorld, initial_state

TID = "invoice-abc"
BLOCK = {"code": "PRICE_OVER_TOLERANCE", "severity": "block", "message": "x", "evidence": {}}


def _ready(world: FakeWorld) -> Any:
    graph = build_graph(world.deps(), InMemorySaver())
    run_thread(graph, initial_state(TID))
    return graph


def _decide(graph: Any, **decision: Any) -> Any:
    graph.invoke(Command(resume=decision), config_for(TID))
    return summarize(graph, TID)


def _state(graph: Any) -> dict[str, Any]:
    return dict(graph.get_state(config_for(TID)).values)


def test_approve_attaches_once_and_audits_the_user() -> None:
    w = FakeWorld()
    graph = _ready(w)
    s = _decide(graph, decision="approve", user="dana", note="ok")
    assert s.status == "ATTACHED" and s.next_nodes == ()
    assert w.attach_calls == 1
    assert w.trex[(7, "INV-1")]["status"] == "ATTACHED" and w.trex[(7, "INV-1")]["user"] == "dana"
    assert ("dana", TID, "DECISION_APPROVE") in w.decision_audits


def test_reject_records_note_and_ends() -> None:
    w = FakeWorld()
    graph = _ready(w)
    s = _decide(graph, decision="reject", user="dana", note="wrong vendor")
    assert s.status == "REJECTED" and s.next_nodes == ()
    assert w.attach_calls == 0
    assert w.decisions[TID] == {
        "status": "REJECTED",
        "decision": "reject",
        "user": "dana",
        "note": "wrong vendor",
    }
    assert "wrong vendor" in s.reasons[-1]
    assert ("dana", TID, "DECISION_REJECT") in w.decision_audits


def test_reject_without_note_is_refused() -> None:
    w = FakeWorld()
    graph = _ready(w)
    s = _decide(graph, decision="reject", user="dana", note="  ")
    assert s.status == "READY" and s.waiting_for == "human_approval"
    assert _state(graph)["decision_error"]


def test_hold_stays_ready_and_can_be_decided_later() -> None:
    w = FakeWorld()
    graph = _ready(w)
    s = _decide(graph, decision="hold", user="dana", note="ask vendor")
    assert s.status == "READY" and s.waiting_for == "human_approval"
    assert w.attach_calls == 0 and len(w.notifications) == 1  # no second notification
    assert ("dana", TID, "DECISION_HOLD") in w.decision_audits
    s = _decide(graph, decision="approve", user="lee", note="")
    assert s.status == "ATTACHED" and w.attach_calls == 1


def test_block_flag_approve_without_note_is_refused_in_the_graph() -> None:
    w = FakeWorld()
    w.anomaly = {"flags": [BLOCK], "similar_invoices": [], "explanation": "bad"}
    graph = _ready(w)
    s = _decide(graph, decision="approve", user="dana", note="")
    assert s.status == "READY" and s.waiting_for == "human_approval"
    assert w.attach_calls == 0 and w.trex == {}
    assert "override reason" in (_state(graph)["decision_error"] or "")
    assert ("dana", TID, "DECISION_REFUSED") in w.decision_audits

    s = _decide(graph, decision="approve", user="dana", note="vendor confirmed new price")
    assert s.status == "ATTACHED" and w.attach_calls == 1
    assert _state(graph)["decision_error"] is None


def test_unknown_decision_and_missing_user_are_refused() -> None:
    w = FakeWorld()
    graph = _ready(w)
    assert _decide(graph, decision="pay", user="dana", note="x").status == "READY"
    assert _decide(graph, decision="approve", user="", note="x").status == "READY"
    assert w.attach_calls == 0


def test_two_approvals_make_one_trex_row() -> None:
    w = FakeWorld()
    deps = w.deps()
    for _ in range(2):  # the attach dependency itself is an idempotent upsert
        deps.attach_invoice(TID, 7, 11, {"invoice_number": "INV-1"}, "dana", "")
    assert len(w.trex) == 1


def test_attach_refused_when_trex_invoice_is_paid_pauses_for_a_new_decision() -> None:
    w = FakeWorld()
    graph = _ready(w)
    w.attach_error = "trex invoice INV-1 is already paid or void"
    s = _decide(graph, decision="approve", user="dana", note="ok")
    assert s.status == "READY" and s.waiting_for == "human_approval"
    assert "already paid" in (_state(graph)["decision_error"] or "")
    assert ("dana", TID, "ATTACH_REFUSED") in w.decision_audits
    w.attach_error = None
    s = _decide(graph, decision="reject", user="dana", note="paid elsewhere")
    assert s.status == "REJECTED"


def _exception(world: FakeWorld) -> Any:
    world.vendor = {"vendor_id": None, "method": "none", "score": 0.0, "candidates": []}
    return _ready(world)


def test_exception_resolve_with_vendor_reruns_validation_to_ready() -> None:
    w = FakeWorld()
    graph = _exception(w)
    assert summarize(graph, TID).status == "EXCEPTION"
    w.vendor = {"vendor_id": 99, "method": "exact_name", "score": 100.0}  # must NOT be used
    s = _decide(graph, decision="resolve", vendor_id=7, user="dana", note="it is Acme")
    assert s.status == "READY" and s.waiting_for == "human_approval"
    st = _state(graph)
    assert st["vendor"]["vendor_id"] == 7 and st["vendor"]["method"] == "manual"
    assert not any("Vendor not found" in r for r in st["reasons"])
    assert ("dana", TID, "DECISION_RESOLVE") in w.decision_audits
    assert w.calls.count("extract") == 1  # only validation re-ran


def test_exception_resolve_with_po_uses_load_po() -> None:
    w = FakeWorld()
    w.po = {"po_id": None, "method": "none", "confidence": 0.0, "reasons": [], "candidates": []}
    graph = _ready(w)
    assert summarize(graph, TID).status == "EXCEPTION"
    s = _decide(graph, decision="resolve", po_id=12, user="dana", note="")
    assert s.status == "READY"
    assert _state(graph)["po"]["po_number"] == "PO-2"


def test_exception_resolve_with_foreign_po_returns_to_exception() -> None:
    w = FakeWorld()
    w.po = {"po_id": None, "method": "none", "confidence": 0.0, "reasons": [], "candidates": []}
    graph = _ready(w)
    s = _decide(graph, decision="resolve", po_id=555, user="dana", note="")
    assert s.status == "EXCEPTION" and s.waiting_for == "human_review"
    assert "not this vendor's" in s.reasons[-1]


def test_exception_reject_ends_rejected() -> None:
    w = FakeWorld()
    graph = _exception(w)
    s = _decide(graph, decision="reject", user="dana", note="not ours")
    assert s.status == "REJECTED" and s.next_nodes == ()
    assert w.decisions[TID]["note"] == "not ours"


def test_resolve_without_choice_or_after_failed_extraction_is_refused() -> None:
    w = FakeWorld()
    graph = _exception(w)
    assert _decide(graph, decision="resolve", user="dana", note="").status == "EXCEPTION"
    assert _state(graph)["decision_error"]

    w2 = FakeWorld()
    w2.extract_fails = True
    g2 = _ready(w2)
    s = _decide(g2, decision="resolve", vendor_id=7, user="dana", note="")
    assert s.status == "EXCEPTION" and "extraction failed" in (_state(g2)["decision_error"] or "")


def test_approval_decisions_are_not_valid_on_an_exception() -> None:
    w = FakeWorld()
    graph = _exception(w)
    assert _decide(graph, decision="approve", user="dana", note="x").status == "EXCEPTION"
    assert w.attach_calls == 0


@pytest.mark.parametrize(
    ("kind", "state", "decision", "ok"),
    [
        ("human_approval", {}, {"decision": "approve", "user": "a"}, True),
        ("human_approval", {}, {"decision": "hold", "user": "a"}, True),
        ("human_approval", {}, {"decision": "reject", "user": "a"}, False),
        ("human_approval", {}, {"decision": "reject", "user": "a", "note": "n"}, True),
        ("human_approval", {}, {"decision": "approve", "user": " "}, False),
        ("human_approval", {}, {"decision": "resolve", "user": "a", "vendor_id": 1}, False),
        ("human_approval", {}, "approve", False),
        (
            "human_approval",
            {"anomaly_report": {"flags": [BLOCK]}},
            {"decision": "approve", "user": "a"},
            False,
        ),
        (
            "human_approval",
            {"anomaly_report": {"flags": [{"severity": "warning"}]}},
            {"decision": "approve", "user": "a"},
            True,
        ),
        ("human_review", {"extraction": {"x": 1}}, {"decision": "resolve", "user": "a"}, False),
        (
            "human_review",
            {"extraction": {"x": 1}},
            {"decision": "resolve", "user": "a", "po_id": 3},
            True,
        ),
        ("human_review", {}, {"decision": "resolve", "user": "a", "po_id": 3}, False),
        ("human_review", {}, {"decision": "reject", "user": "a", "note": "n"}, True),
    ],
)
def test_validate_decision_table(kind: Any, state: Any, decision: Any, ok: bool) -> None:
    assert (validate_decision(state, kind, decision) is None) is ok


def test_routers() -> None:
    assert route_after_review({"decision": {"decision": "approve"}}) == "attach_to_trex"
    assert route_after_review({"decision": {"decision": "reject"}}) == "reject_decided"
    assert route_after_review({"decision": {"decision": "hold"}}) == "human_review"
    assert (
        route_after_review({"decision": {"decision": "approve"}, "decision_error": "no"})
        == "human_review"
    )
    assert route_after_exception_review({"decision": {"decision": "resolve"}}) == "validate_vendor"
    assert route_after_exception_review({"decision": {"decision": "reject"}}) == "reject_decided"
    assert (
        route_after_exception_review({"decision": {"decision": "resolve"}, "decision_error": "x"})
        == "exception_review"
    )
