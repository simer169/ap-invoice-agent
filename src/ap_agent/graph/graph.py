"""Graph wiring.

extract -> validate_vendor -> duplicate_check -> match_po -> check_receipt -> price_check ->
anomaly_check -> ready
with exits to reject (END), exception and park; exception/park/ready all pause in `wait`.
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
    for name in ("exception", "park", "ready"):
        g.add_edge(name, "wait")
    g.add_edge("wait", END)
    return g.compile(checkpointer=checkpointer)
