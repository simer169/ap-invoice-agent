"""Fake Deps for graph tests: no DB, no LLM, no S3. Behaviour is set through plain attributes."""

from datetime import date
from typing import Any

from ap_agent.graph.deps import AttachError, Deps, ExtractionError
from ap_agent.graph.state import InvoiceState

INVOICE: dict[str, Any] = {
    "vendor_name": "Acme Ltd",
    "invoice_number": "INV-1",
    "po_number": "PO-1",
    "currency": "CAD",
    "payment_terms": "Net 30",
    "subtotal": "100.00",
    "tax": "13.00",
    "total": "113.00",
    "line_items": [],
    "invoice_date": "2026-11-01",
}


class FakeWorld:
    """Holds scripted tool answers and records every call."""

    def __init__(self) -> None:
        self.vendor: dict[str, Any] = {"vendor_id": 7, "method": "exact_name", "score": 100.0}
        self.duplicate: dict[str, Any] = {"kind": "new", "existing_ref": None}
        self.po: dict[str, Any] = {
            "po_id": 11,
            "po_number": "PO-1",
            "method": "po_number",
            "confidence": 1.0,
            "reasons": [],
        }
        self.fully_received = True
        self.extract_fails = False
        self.anomaly: dict[str, Any] = {"flags": [], "similar_invoices": [], "explanation": None}
        self.calls: list[str] = []
        self.audits: list[tuple[str, str]] = []
        self.parked: dict[str, dict[str, Any]] = {}
        self.notifications: list[dict[str, Any]] = []
        self.decision_audits: list[tuple[str, str, str]] = []  # actor, thread_id, action
        self.po_numbers: dict[tuple[int, int], str] = {(7, 11): "PO-1", (7, 12): "PO-2"}
        self.trex: dict[tuple[int, str], dict[str, Any]] = {}  # (vendor_id, number) -> row
        self.attach_calls = 0
        self.attach_error: str | None = None  # set to make attach_invoice raise AttachError
        self.decisions: dict[str, dict[str, str]] = {}  # thread_id -> last recorded decision

    def deps(self) -> Deps:
        w = self

        def extract(h: str, key: str, received: date | None) -> dict[str, Any]:
            w.calls.append("extract")
            if w.extract_fails:
                raise ExtractionError("ValidationError: totals do not add up")
            return {"invoice": dict(INVOICE), "date_ambiguous": False}

        def find_vendor(name: str, sender: str | None) -> dict[str, Any]:
            w.calls.append("find_vendor")
            return dict(w.vendor)

        def check_duplicate(vid: int | None, number: str, h: str) -> dict[str, Any]:
            w.calls.append("check_duplicate")
            return dict(w.duplicate)

        def load_terms(vid: int, d: date) -> dict[str, str | None]:
            return {"due_date": "2026-12-01", "discount_deadline": None}

        def match_po(vid: int, subject: str | None, inv: dict[str, Any]) -> dict[str, Any]:
            w.calls.append("match_po")
            return dict(w.po)

        def receipt_status(po_id: int) -> dict[str, Any]:
            w.calls.append("receipt_status")
            qty = "0" if w.fully_received else "3"
            return {
                "po_id": po_id,
                "fully_received": w.fully_received,
                "lines": [{"qty_outstanding": qty}],
            }

        def price_variance(po_id: int, inv: dict[str, Any]) -> dict[str, Any]:
            w.calls.append("price_variance")
            return {"tolerance": "0.02", "lines": [], "unmatched_invoice_lines": []}

        def anomaly_check(state: InvoiceState) -> dict[str, Any]:
            w.calls.append("anomaly_check")
            return dict(w.anomaly)

        def audit(thread_id: str, action: str, details: dict[str, Any]) -> None:
            w.audits.append((thread_id, action))

        def upsert_parked(row: dict[str, Any]) -> None:
            w.parked[row["thread_id"]] = row

        def notify(event: dict[str, Any]) -> None:
            if not any(n["thread_id"] == event["thread_id"] for n in w.notifications):
                w.notifications.append(event)  # like the real unique (thread, kind) index

        def audit_as(actor: str, thread_id: str, action: str, details: dict[str, Any]) -> None:
            w.decision_audits.append((actor, thread_id, action))

        def load_po(vendor_id: int, po_id: int) -> dict[str, Any] | None:
            number = w.po_numbers.get((vendor_id, po_id))
            if number is None:
                return None
            return {
                "po_id": po_id,
                "po_number": number,
                "method": "manual",
                "confidence": 1.0,
                "reasons": [],
                "candidates": [],
            }

        def attach_invoice(
            thread_id: str,
            vendor_id: int,
            po_id: int | None,
            inv: dict[str, Any],
            user: str,
            note: str,
        ) -> int:
            w.attach_calls += 1
            if w.attach_error:
                raise AttachError(w.attach_error)
            key = (vendor_id, inv["invoice_number"])
            w.trex.setdefault(key, {"id": len(w.trex) + 1})  # upsert: never a second row
            w.trex[key].update(status="ATTACHED", po_id=po_id, user=user, note=note)
            if thread_id in w.parked:
                w.parked[thread_id]["status"] = "ATTACHED"
            return int(w.trex[key]["id"])

        def record_decision(
            thread_id: str, status: str, decision: str, user: str, note: str
        ) -> None:
            w.decisions[thread_id] = {
                "status": status,
                "decision": decision,
                "user": user,
                "note": note,
            }
            if thread_id in w.parked:
                w.parked[thread_id]["status"] = status

        return Deps(
            extract=extract,
            find_vendor=find_vendor,
            check_duplicate=check_duplicate,
            load_terms=load_terms,
            match_po=match_po,
            receipt_status=receipt_status,
            price_variance=price_variance,
            anomaly_check=anomaly_check,
            audit=audit,
            upsert_parked=upsert_parked,
            notify=notify,
            audit_as=audit_as,
            load_po=load_po,
            attach_invoice=attach_invoice,
            record_decision=record_decision,
        )


def initial_state(thread_id: str = "invoice-abc") -> InvoiceState:
    return {
        "thread_id": thread_id,
        "content_hash": thread_id.removeprefix("invoice-"),
        "s3_key": "raw/2026/11/abc.pdf",
        "email_meta": {
            "message_id": "<m1>",
            "from_address": "ar@acme.example",
            "subject": "Invoice",
            "received_at": "2026-11-02T10:00:00",
        },
        "status": "NEW",
        "reasons": [],
        "history": [],
    }
