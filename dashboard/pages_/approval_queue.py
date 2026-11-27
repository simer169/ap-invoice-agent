"""Approval queue: approve / reject / hold READY invoices, resolve exceptions.

Reads through data.py (ui_queries), writes only through data.submit_decision (decisions.py).
Nothing here starts a payment.
"""

from typing import Any

import data
import pandas as pd
import streamlit as st

from ap_agent import ui_queries as q
from ap_agent.decisions import DecisionError

CHIP_COLOR = {"info": "gray", "warning": "orange", "block": "red"}

st.header("Approval queue")

acting_as = str(st.session_state.get("acting_as", "")).strip()
if not acting_as:
    st.warning("Enter your name under 'Acting as' in the sidebar before deciding anything.")

flash = st.session_state.pop("flash", None)
if flash:
    st.success(flash)


def _money(card: q.ApprovalCard) -> str:
    return f"{card.total:,.2f} {card.currency or ''}".strip() if card.total is not None else "-"


def _due(card: q.ApprovalCard) -> str:
    if card.due_date is None or card.days_left is None:
        return "-"
    n = card.days_left
    when = f"{abs(n)} day(s) overdue" if n < 0 else "today" if n == 0 else f"{n} day(s) left"
    return f"{card.due_date.isoformat()} ({when})"


def _decide(card: q.ApprovalCard, decision: dict[str, Any], verb: str) -> None:
    try:
        status = data.submit_decision(card.thread_id, {**decision, "user": acting_as})
    except DecisionError as exc:
        st.error(str(exc))
        return
    except Exception as exc:  # noqa: BLE001 - show the class only, never connection details
        st.error(f"Could not {verb}: {type(exc).__name__}")
        return
    st.session_state["flash"] = f"{card.invoice_number or card.thread_id}: {verb} -> {status}"
    st.rerun()


def _flags(card: q.ApprovalCard) -> None:
    if card.flags:
        st.markdown(
            " ".join(
                f":{CHIP_COLOR[f.severity]}-badge[{f.severity.upper()} {f.code}]"
                for f in card.flags
            )
        )
        for f in card.flags:
            st.caption(f"{f.code}: {f.message}")
    else:
        st.caption("No flags.")
    if card.explanation:
        st.info(card.explanation, icon=":material/smart_toy:")
        st.caption("AI explanation. The flags above are decided by rules, not by the AI.")


def _lines_and_pdf(card: q.ApprovalCard) -> None:
    left, right = st.columns([5, 1])
    with left:
        lines = data.card_po_lines(card.po_number, card.billed_by_part)
        if lines:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Line": ln.line_no,
                            "Part": ln.part_no,
                            "Ordered": float(ln.ordered),
                            "Received": float(ln.received),
                            "Billed": float(ln.billed) if ln.billed is not None else None,
                        }
                        for ln in lines
                    ]
                ),
                hide_index=True,
                use_container_width=True,
            )
        else:
            st.caption("No PO lines to compare.")
    with right:
        if card.s3_key:
            try:
                st.link_button("View PDF", data.pdf_url(card.s3_key))
            except Exception as exc:  # noqa: BLE001
                st.caption(f"PDF link unavailable ({type(exc).__name__})")


def _header(card: q.ApprovalCard) -> None:
    st.subheader(f"{card.vendor_name or 'Unknown vendor'} - {card.invoice_number or '(no number)'}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("PO", card.po_number or "-")
    c2.metric("Total", _money(card))
    c3.metric("Due", _due(card))
    if card.po_method:
        conf = f" ({card.po_confidence:.0%})" if card.po_confidence is not None else ""
        c4.metric("PO match", f"{card.po_method}{conf}")
    if card.discount_deadline:
        st.caption(f"Early-payment discount ends {card.discount_deadline.isoformat()}")


def _ready_card(card: q.ApprovalCard) -> None:
    with st.container(border=True):
        _header(card)
        _flags(card)
        _lines_and_pdf(card)
        needs_note = "Override reason required (block flag)" if card.has_block_flag else "Note"
        note = st.text_area(
            needs_note + " - always required to reject", key=f"note-{card.thread_id}", height=70
        )
        a, r, h, _ = st.columns([1, 1, 1, 5])
        if a.button("Approve", key=f"approve-{card.thread_id}", type="primary"):
            _decide(card, {"decision": "approve", "note": note}, "approved")
        if r.button("Reject", key=f"reject-{card.thread_id}"):
            _decide(card, {"decision": "reject", "note": note}, "rejected")
        if h.button("Hold", key=f"hold-{card.thread_id}"):
            _decide(card, {"decision": "hold", "note": note}, "held")


def _exception_card(card: q.ApprovalCard) -> None:
    with st.container(border=True):
        _header(card)
        for reason in card.reasons:
            st.markdown(f":red-badge[EXCEPTION] {reason}")
        vendors = {c.id: c.label for c in card.vendor_candidates}
        for v in data.list_vendor_options():
            vendors.setdefault(v.id, v.name)
        vendor_ids: list[int | None] = [None, *vendors]
        vendor_id = st.selectbox(
            "Vendor",
            vendor_ids,
            index=vendor_ids.index(card.vendor_id) if card.vendor_id in vendor_ids else 0,
            format_func=lambda i: "(keep as is)" if i is None else vendors[i],
            key=f"vendor-{card.thread_id}",
        )
        pos = {c.id: c.label for c in card.po_candidates}
        po_ids: list[int | None] = [None, *pos]
        po_id = st.selectbox(
            "Purchase order (candidates)",
            po_ids,
            format_func=lambda i: "(none chosen)" if i is None else pos[i],
            key=f"po-{card.thread_id}",
        )
        note = st.text_area("Note (required to reject)", key=f"note-{card.thread_id}", height=70)
        a, b, _ = st.columns([1, 1, 6])
        if a.button("Resolve", key=f"resolve-{card.thread_id}", type="primary"):
            choice: dict[str, Any] = {"decision": "resolve", "note": note}
            if vendor_id is not None and vendor_id != card.vendor_id:
                choice["vendor_id"] = vendor_id
            elif vendor_id is not None and po_id is None:
                choice["vendor_id"] = vendor_id  # re-validate with the same, confirmed vendor
            if po_id is not None:
                choice["po_id"] = po_id
            _decide(card, choice, "resolved")
        if b.button("Reject", key=f"xreject-{card.thread_id}"):
            _decide(card, {"decision": "reject", "note": note}, "rejected")
        if card.s3_key:
            try:
                st.link_button("View PDF", data.pdf_url(card.s3_key))
            except Exception as exc:  # noqa: BLE001
                st.caption(f"PDF link unavailable ({type(exc).__name__})")


def _waiting_card(card: q.ApprovalCard) -> None:
    with st.container(border=True):
        _header(card)
        for reason in card.reasons:
            st.markdown(f":orange-badge[WAITING] {reason}")
        _lines_and_pdf(card)


def _decided_card(card: q.ApprovalCard) -> None:
    with st.container(border=True):
        _header(card)
        color = "green" if card.status == "ATTACHED" else "red"
        when = card.decided_at.strftime("%Y-%m-%d %H:%M") if card.decided_at else "?"
        st.markdown(f":{color}-badge[{card.status}] by {card.decided_by or '?'} at {when}")
        if card.decision_note:
            st.caption(f"Note: {card.decision_note}")
        _flags(card)


TABS: list[tuple[q.Tab, str, Any]] = [
    ("ready", "Ready for approval", _ready_card),
    ("exceptions", "Exceptions", _exception_card),
    ("waiting", "Waiting for receiving", _waiting_card),
    ("decided", "Decided (last 50)", _decided_card),
]

try:
    counts = data.approval_counts()
    tabs = st.tabs([f"{label} ({counts.get(key, 0)})" for key, label, _ in TABS])
    for tab, (key, _label, render) in zip(tabs, TABS, strict=True):
        with tab:
            cards = data.approval_cards(key)
            if not cards:
                st.caption("Nothing here.")
            for card in cards:
                render(card)
except Exception as exc:  # noqa: BLE001
    st.error(f"Could not load the queue: {type(exc).__name__}")
