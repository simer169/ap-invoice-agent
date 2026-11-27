"""Graph wiring.

extract -> validate_vendor -> duplicate_check -> match_po -> check_receipt -> price_check ->
anomaly_check -> ready -> human_review -> attach_to_trex | reject_decided (both END), hold loops
with exits to reject (END), exception and park.
park pauses in `wait`; a resumed WAITING thread loops back to check_receipt.
exception pauses in `exception_review`: resolve re-runs from validate_vendor with the chosen
vendor/PO, reject ends. Hold and refused decisions pause again.
"""

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from ap_agent.graph.deps import Deps
from ap_agent.graph.nodes import make_nodes
from ap_agent.graph.state import InvoiceState


def route_after_extract(state: InvoiceState) -> str:
    return "validate_vendor" if state.get("extraction") else "exception"


def route_after_vendor(state: InvoiceState) -> str:
    return "duplicate_check" if (state.get("vendor") or {}).get("vendor_id") else "exception"


def route_after_duplicate(state: InvoiceState) -> str:
    kind = (state.get("duplicate") or {}).get("kind", "new")
    return {"exact": "reject", "reissue": "exception"}.get(kind, "match_po")


def route_after_po(state: InvoiceState) -> str:
    return "check_receipt" if (state.get("po") or {}).get("po_id") is not None else "exception"


def route_after_receipt(state: InvoiceState) -> str:
    return "price_check" if (state.get("receipt") or {}).get("fully_received") else "park"


def route_after_wait(state: InvoiceState) -> str:
    """A WAITING thread woken by the poller re-checks the receipt; every other pause just ends."""
    return "check_receipt" if state.get("status") == "WAITING" else END


def route_after_review(state: InvoiceState) -> str:
    """approve -> attach, reject -> reject; hold or a refused decision pauses again."""
    if state.get("decision_error"):
        return "human_review"
    return {"approve": "attach_to_trex", "reject": "reject_decided"}.get(
        (state.get("decision") or {}).get("decision", ""), "human_review"
    )


def route_after_attach(state: InvoiceState) -> str:
    """A refused attach (trex invoice already paid/void) pauses for a new decision."""
    return "human_review" if state.get("decision_error") else END


def route_after_exception_review(state: InvoiceState) -> str:
    """resolve -> re-validate from validate_vendor, reject -> reject; refused pauses again."""
    if state.get("decision_error"):
        return "exception_review"
    return {"resolve": "validate_vendor", "reject": "reject_decided"}.get(
        (state.get("decision") or {}).get("decision", ""), "exception_review"
    )


def build_graph(
    deps: Deps, checkpointer: BaseCheckpointSaver[Any] | None
) -> CompiledStateGraph[InvoiceState, Any, Any, Any]:
    nodes = make_nodes(deps)
    g = StateGraph(InvoiceState)
    for name, fn in nodes.items():
        g.add_node(name, fn)
    g.add_edge(START, "extract")
    g.add_conditional_edges("extract", route_after_extract, ["validate_vendor", "exception"])
    g.add_conditional_edges("validate_vendor", route_after_vendor, ["duplicate_check", "exception"])
    g.add_conditional_edges(
        "duplicate_check", route_after_duplicate, ["reject", "exception", "match_po"]
    )
    g.add_conditional_edges("match_po", route_after_po, ["check_receipt", "exception"])
    g.add_conditional_edges("check_receipt", route_after_receipt, ["price_check", "park"])
    g.add_edge("price_check", "anomaly_check")
    g.add_edge("anomaly_check", "ready")
    g.add_edge("reject", END)
    g.add_edge("park", "wait")
    g.add_conditional_edges("wait", route_after_wait, ["check_receipt", END])
    g.add_edge("ready", "human_review")
    g.add_conditional_edges(
        "human_review", route_after_review, ["attach_to_trex", "reject_decided", "human_review"]
    )
    g.add_edge("exception", "exception_review")
    g.add_conditional_edges(
        "exception_review",
        route_after_exception_review,
        ["validate_vendor", "reject_decided", "exception_review"],
    )
    g.add_conditional_edges("attach_to_trex", route_after_attach, [END, "human_review"])
    g.add_edge("reject_decided", END)
    return g.compile(checkpointer=checkpointer)
