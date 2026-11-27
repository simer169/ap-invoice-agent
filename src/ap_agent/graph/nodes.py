"""Graph nodes: one function per step. Each appends to `history` and writes an audit_log row.

park / exception / ready record the outcome (status + agent.parked_invoices upsert). Each then
flows into a node that calls interrupt(): `wait` (park; a WAITING thread resumed by the poller
loops back to check_receipt, see graph.route_after_wait), `human_review` (ready) or
`exception_review` (exception). The interrupt lives in its own node because a node that interrupts
does not commit its state update until it is resumed, and is re-run from the top on resume.

Human decisions are validated here (decision_rules), so the rules hold whatever the caller is.
A refused decision never raises: it is audited, stored as `decision_error` and the node pauses
again. Nothing here pays anything.
"""

import logging
import os
import re
from collections.abc import Callable
from datetime import date
from typing import Any

from langgraph.types import interrupt

from ap_agent.graph.decision_rules import PauseKind, validate_decision
from ap_agent.graph.deps import AttachError, Deps, ExtractionError
from ap_agent.graph.state import RESET, InvoiceState

log = logging.getLogger(__name__)

Update = dict[str, Any]
NodeFn = Callable[[InvoiceState], Update]
Body = Callable[[InvoiceState], tuple[Update, str]]

NO_PO_NUMBER_REASON = "PO matched without PO number - confirm"
RECEIPT_REASON = "PO not fully received"
CRASH_ENV = "AP_CRASH_AFTER"
_ANOMALY_PREFIX = re.compile(r"^Anomaly \S+ \(\w+\): ")


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
        chosen = (state.get("overrides") or {}).get("vendor_id")
        if chosen is not None:  # a reviewer picked the vendor on an exception
            vendor: dict[str, Any] = {
                "vendor_id": int(chosen),
                "method": "manual",
                "score": 100.0,
                "candidates": (state.get("vendor") or {}).get("candidates") or [],
            }
        else:
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
        chosen = (state.get("overrides") or {}).get("po_id")
        if chosen is not None:  # a reviewer picked the PO on an exception
            po = deps.load_po(state["vendor"]["vendor_id"], int(chosen))
            if po is None:
                prev = (state.get("po") or {}).get("candidates") or []
                none_po = {
                    "po_id": None,
                    "method": "none",
                    "confidence": 0.0,
                    "reasons": [],
                    "candidates": prev,
                }
                return {
                    "po": none_po,
                    "reasons": [f"PO not matched: chosen PO {chosen} is not this vendor's"],
                }, "no PO"
        else:
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
            "reasons": [f"{RECEIPT_REASON} ({open_lines} line(s) outstanding)"],
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

    def _notify_ready(state: InvoiceState) -> None:
        """In-app (and Slack) 'ready for review'. Deduplicated per thread; never breaks the run."""
        inv = _invoice(state)
        event = {
            "kind": "PO_RECEIVED",
            "thread_id": state["thread_id"],
            "vendor": inv.get("vendor_name"),
            "invoice_number": inv.get("invoice_number"),
            "po_number": (state.get("po") or {}).get("po_number") or inv.get("po_number"),
            "currency": inv.get("currency"),
            "total": inv.get("total"),
            "due_date": state.get("due_date"),
            "discount_deadline": state.get("discount_deadline"),
            "warnings": [_ANOMALY_PREFIX.sub("", str(r)) for r in state.get("reasons") or []],
            "today": date.today().isoformat(),
        }
        try:
            deps.notify(event)
        except Exception as exc:  # noqa: BLE001 - a notification must not stop the invoice
            log.warning("notify failed for %s: %s", state["thread_id"], type(exc).__name__)

    def _record(status: str) -> Body:
        def body(state: InvoiceState) -> tuple[Update, str]:
            inv = _invoice(state)
            reasons = list(state.get("reasons") or [])
            po = state.get("po") or {}
            po_number = po.get("po_number") or inv.get("po_number")
            deps.upsert_parked(
                {
                    "thread_id": state["thread_id"],
                    "content_hash": state["content_hash"],
                    "s3_key": state.get("s3_key"),
                    "status": status,
                    "vendor_id": (state.get("vendor") or {}).get("vendor_id"),
                    "po_id": po.get("po_id"),
                    "review_json": {
                        "vendor": state.get("vendor"),
                        "po": state.get("po"),
                        "receipt": state.get("receipt"),
                    },
                    "po_number": po_number,
                    "invoice_number": inv.get("invoice_number"),
                    "invoice_date": inv.get("invoice_date"),
                    "due_date": state.get("due_date"),
                    "discount_deadline": state.get("discount_deadline"),
                    "currency": inv.get("currency"),
                    "total": inv.get("total"),
                    "extracted_json": inv or None,
                    "anomalies": reasons,
                    "anomaly_report": state.get("anomaly_report"),
                }
            )
            if status == "READY":
                _notify_ready(state)
            return {"status": status}, f"recorded as {status}"

        return body

    def wait(state: InvoiceState) -> Update:
        """Parked until the PO is fully received (the poller resumes it)."""
        decision = interrupt(
            {
                "waiting_for": "po_fully_received",
                "po_number": (state.get("po") or {}).get("po_number"),
            }
        )
        update: Update = {"history": [f"wait: resumed with {decision!r}"]}
        # Looping back to check_receipt: drop the stale receipt reason, keep the others.
        kept = [r for r in state.get("reasons") or [] if not r.startswith(RECEIPT_REASON)]
        update["reasons"] = [RESET, *kept]
        return update

    def _apply_decision(state: InvoiceState, kind: PauseKind, decision: Any) -> Update:
        """Validate a resume payload, audit it (actor = the user) and store it in state."""
        thread_id = state["thread_id"]
        d: dict[str, Any] = decision if isinstance(decision, dict) else {}
        user = str(d.get("user") or "").strip() or "unknown"
        action = str(d.get("decision") or "")
        note = str(d.get("note") or "").strip()
        error = validate_decision(dict(state), kind, decision)
        if error:
            deps.audit_as(
                user,
                thread_id,
                "DECISION_REFUSED",
                {"decision": action, "note": note, "error": error},
            )
            return {
                "decision": {"decision": action, "user": user, "note": note},
                "decision_error": error,
                "history": [f"{kind}: refused {action or '?'} by {user}: {error}"],
            }
        stored: dict[str, Any] = {"decision": action, "user": user, "note": note}
        update: Update = {"decision": stored, "decision_error": None}
        if action == "resolve":
            new = {k: int(d[k]) for k in ("vendor_id", "po_id") if d.get(k) is not None}
            merged = {**(state.get("overrides") or {}), **new}
            if "vendor_id" in new and "po_id" not in new:
                merged.pop("po_id", None)  # a new vendor invalidates the old PO choice
            update["overrides"] = merged
            update["reasons"] = [RESET]  # validation re-runs and re-derives them
            stored.update(new)
        deps.audit_as(user, thread_id, f"DECISION_{action.upper()}", {"note": note, **stored})
        update["history"] = [f"{kind}: {action} by {user}"]
        return update

    def human_review(state: InvoiceState) -> Update:
        inv = _invoice(state)
        report = state.get("anomaly_report") or {}
        decision = interrupt(
            {
                "waiting_for": "human_approval",
                "summary": {
                    "vendor": inv.get("vendor_name"),
                    "invoice_number": inv.get("invoice_number"),
                    "po_number": (state.get("po") or {}).get("po_number"),
                    "currency": inv.get("currency"),
                    "total": inv.get("total"),
                    "due_date": state.get("due_date"),
                },
                "flags": list(report.get("flags") or []),
                "explanation": report.get("explanation"),
            }
        )
        return _apply_decision(state, "human_approval", decision)

    def exception_review(state: InvoiceState) -> Update:
        decision = interrupt(
            {
                "waiting_for": "human_review",
                "reasons": list(state.get("reasons") or []),
                "vendor_candidates": list((state.get("vendor") or {}).get("candidates") or []),
                "po_candidates": list((state.get("po") or {}).get("candidates") or []),
            }
        )
        return _apply_decision(state, "human_review", decision)

    def attach_to_trex(state: InvoiceState) -> tuple[Update, str]:
        d = state.get("decision") or {}
        po_id = (state.get("po") or {}).get("po_id")
        user = d.get("user", "unknown")
        try:
            trex_id = deps.attach_invoice(
                state["thread_id"],
                state["vendor"]["vendor_id"],
                po_id,
                _invoice(state),
                user,
                d.get("note", ""),
            )
        except AttachError as exc:  # already PAID/VOID in trex: refuse, pause for a new decision
            error = str(exc)
            deps.audit_as(user, state["thread_id"], "ATTACH_REFUSED", {"error": error})
            return {"decision_error": error}, f"attach refused: {error}"
        return {"status": "ATTACHED", "decision_error": None}, f"attached as trex invoice {trex_id}"

    def reject_decided(state: InvoiceState) -> tuple[Update, str]:
        d = state.get("decision") or {}
        user, note = d.get("user", "unknown"), d.get("note", "")
        deps.record_decision(state["thread_id"], "REJECTED", "reject", user, note)
        return {
            "status": "REJECTED",
            "reasons": [f"Rejected by {user}: {note}"],
        }, f"rejected by {user}"

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
        "human_review": human_review,
        "exception_review": exception_review,
        "attach_to_trex": step("attach_to_trex", attach_to_trex),
        "reject_decided": step("reject_decided", reject_decided),
    }
