"""Graph nodes: one function per step. Each appends to `history` and writes an audit_log row.

park / exception / ready record the outcome (status + agent.parked_invoices upsert) and flow into
the shared `wait` node, which calls interrupt(). The interrupt lives in its own node because a
node that interrupts does not commit its state update until it is resumed.
"""

import os
from collections.abc import Callable
from datetime import date
from typing import Any

from langgraph.types import interrupt

from ap_agent.graph.deps import Deps, ExtractionError
from ap_agent.graph.state import InvoiceState

Update = dict[str, Any]
NodeFn = Callable[[InvoiceState], Update]
Body = Callable[[InvoiceState], tuple[Update, str]]

NO_PO_NUMBER_REASON = "PO matched without PO number - confirm"
CRASH_ENV = "AP_CRASH_AFTER"


def _invoice(state: InvoiceState) -> dict[str, Any]:
    return dict(state.get("extraction") or {})


def _received_date(state: InvoiceState) -> date | None:
    raw = (state.get("email_meta") or {}).get("received_at")
    try:
        return date.fromisoformat(str(raw)[:10]) if raw else None
    except ValueError:
        return None


def _iso_date(value: Any) -> date | None:
    return date.fromisoformat(value) if value else None


def make_nodes(deps: Deps) -> dict[str, NodeFn]:
    def step(name: str, body: Body) -> NodeFn:
        """Wrap a body: add the history line, write the audit row, honour AP_CRASH_AFTER."""

        def run(state: InvoiceState) -> Update:
            update, note = body(state)
            update["history"] = [f"{name}: {note}"]
            deps.audit(state["thread_id"], f"NODE_{name}", {"note": note})
            if os.environ.get(CRASH_ENV) == name:  # dev only: demo crash + resume
                raise RuntimeError(f"{CRASH_ENV}={name}: simulated crash after {name}")
            return update

        return run

    def extract(state: InvoiceState) -> tuple[Update, str]:
        try:
            res = deps.extract(state["content_hash"], state["s3_key"], _received_date(state))
        except ExtractionError as exc:
            return {"extraction": {}, "reasons": [f"extraction failed: {exc}"]}, "failed"
        inv = {**res["invoice"], "date_ambiguous": bool(res.get("date_ambiguous"))}
        note = f"invoice {inv['invoice_number']} from {inv['vendor_name']}"
        if inv["date_ambiguous"]:
            note += " (date format ambiguous)"
        return {"extraction": inv}, note

    def validate_vendor(state: InvoiceState) -> tuple[Update, str]:
        inv = _invoice(state)
        vendor = deps.find_vendor(
            inv["vendor_name"], (state.get("email_meta") or {}).get("from_address")
        )
        update: Update = {"vendor": vendor}
        if vendor.get("vendor_id") is None:
            update["reasons"] = [f"Vendor not found: {inv['vendor_name']!r}"]
            return update, "vendor not found"
        terms = deps.load_terms(vendor["vendor_id"], date.fromisoformat(inv["invoice_date"]))
        update.update(due_date=terms["due_date"], discount_deadline=terms["discount_deadline"])
        return update, f"vendor {vendor['vendor_id']} via {vendor['method']}"

    def duplicate_check(state: InvoiceState) -> tuple[Update, str]:
        inv = _invoice(state)
        dup = deps.check_duplicate(
            (state.get("vendor") or {}).get("vendor_id"),
            inv["invoice_number"],
            state["content_hash"],
        )
        if dup["kind"] == "exact" and dup.get("existing_ref") == state["thread_id"]:
            dup = {"kind": "new", "existing_ref": None}  # a re-run must not reject itself
        update: Update = {"duplicate": dup}
        if dup["kind"] == "exact":
            update["reasons"] = [f"Exact duplicate of {dup['existing_ref']}"]
        elif dup["kind"] == "reissue":
            update["reasons"] = [
                (
                    f"Possible reissue of {dup['existing_ref']}: same vendor and invoice "
                    "number, different PDF"
                )
            ]
        return update, dup["kind"]

    def match_po(state: InvoiceState) -> tuple[Update, str]:
        inv = _invoice(state)
        subject = (state.get("email_meta") or {}).get("subject")
        po = deps.match_po(state["vendor"]["vendor_id"], subject, inv)
        update: Update = {"po": po}
        if po.get("po_id") is None:
            why = "; ".join(po.get("reasons") or []) or "no match"
            update["reasons"] = [f"PO not matched: {why}"]
            return update, "no PO"
        if po["method"] == "content_score":
            update["reasons"] = [NO_PO_NUMBER_REASON]
        return update, f"PO {po.get('po_number')} via {po['method']}"

    def check_receipt(state: InvoiceState) -> tuple[Update, str]:
        receipt = deps.receipt_status(state["po"]["po_id"])
        if receipt["fully_received"]:
            return {"receipt": receipt}, "fully received"
        open_lines = sum(1 for ln in receipt["lines"] if float(ln["qty_outstanding"]) > 0)
        return {
            "receipt": receipt,
            "reasons": [f"PO not fully received ({open_lines} line(s) outstanding)"],
        }, "not fully received"

    def price_check(state: InvoiceState) -> tuple[Update, str]:
        pricing = deps.price_variance(state["po"]["po_id"], _invoice(state))
        over = sum(1 for ln in pricing["lines"] if ln["exceeds_tolerance"])
        return {
            "pricing": pricing
        }, f"{len(pricing['lines'])} line(s) compared, {over} over tolerance"

    def anomaly_check(state: InvoiceState) -> tuple[Update, str]:
        report = deps.anomaly_check(state)
        flags = [f for f in report.get("flags") or [] if f["severity"] != "info"]
        blocks = sum(1 for f in flags if f["severity"] == "block")
        update: Update = {"anomaly_report": report}
        if flags:
            update["reasons"] = [
                f"Anomaly {f['code']} ({f['severity']}): {f['message']}" for f in flags
            ]
        return update, f"{len(report.get('flags') or [])} flag(s), {blocks} block"

    def reject(state: InvoiceState) -> tuple[Update, str]:
        return {"status": "REJECTED"}, f"rejected: {state['duplicate']['existing_ref']}"

    def _record(status: str) -> Body:
        def body(state: InvoiceState) -> tuple[Update, str]:
            inv = _invoice(state)
            reasons = list(state.get("reasons") or [])
            po_number = (state.get("po") or {}).get("po_number") or inv.get("po_number")
            deps.upsert_parked(
                {
                    "thread_id": state["thread_id"],
                    "content_hash": state["content_hash"],
                    "s3_key": state.get("s3_key"),
                    "status": status,
                    "vendor_id": (state.get("vendor") or {}).get("vendor_id"),
                    "po_number": po_number,
                    "invoice_number": inv.get("invoice_number"),
                    "invoice_date": inv.get("invoice_date"),
                    "due_date": state.get("due_date"),
                    "currency": inv.get("currency"),
                    "total": inv.get("total"),
                    "extracted_json": inv or None,
                    "anomalies": reasons,
                    "anomaly_report": state.get("anomaly_report"),
                }
            )
            return {"status": status}, f"recorded as {status}"

        return body

    def wait(state: InvoiceState) -> Update:
        status = state["status"]
        if status == "WAITING":
            payload = {
                "waiting_for": "po_fully_received",
                "po_number": (state.get("po") or {}).get("po_number"),
            }
        elif status == "EXCEPTION":
            payload = {"waiting_for": "human_review", "reasons": list(state.get("reasons") or [])}
        else:
            payload = {"waiting_for": "human_approval"}
        decision = interrupt(payload)
        return {"history": [f"wait: resumed with {decision!r}"]}

    return {
        "extract": step("extract", extract),
        "validate_vendor": step("validate_vendor", validate_vendor),
        "duplicate_check": step("duplicate_check", duplicate_check),
        "match_po": step("match_po", match_po),
        "check_receipt": step("check_receipt", check_receipt),
        "price_check": step("price_check", price_check),
        "anomaly_check": step("anomaly_check", anomaly_check),
        "reject": step("reject", reject),
        "exception": step("exception", _record("EXCEPTION")),
        "park": step("park", _record("WAITING")),
        "ready": step("ready", _record("READY")),
        "wait": wait,
    }
