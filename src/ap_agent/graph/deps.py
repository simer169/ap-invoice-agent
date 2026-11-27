"""Everything the graph nodes need from the outside world, as plain callables.

Nodes only see `Deps`, so tests swap in fakes (no DB, no LLM, no S3). `prod_deps` wires the real
tools. All values crossing this boundary are JSON-safe dicts.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ap_agent.graph.state import InvoiceState
from ap_agent.models import ExtractedInvoice

ACTOR = "invoice_graph"


class ExtractionError(Exception):
    """The PDF could not be turned into a valid invoice (LLM output failed validation, etc.)."""


@dataclass
class Deps:
    # content_hash, s3_key, received_date -> {"invoice": <ExtractedInvoice dump>, "date_ambiguous"}
    extract: Callable[[str, str, date | None], dict[str, Any]]
    # vendor_name, from_address -> VendorMatch dump
    find_vendor: Callable[[str, str | None], dict[str, Any]]
    # vendor_id, invoice_number, content_hash -> DuplicateResult dump
    check_duplicate: Callable[[int | None, str, str], dict[str, Any]]
    # vendor_id, invoice_date -> {"due_date", "discount_deadline"} (ISO strings or None)
    load_terms: Callable[[int, date], dict[str, str | None]]
    # vendor_id, subject, invoice dump, invoice_date -> POMatch dump plus "po_number"
    match_po: Callable[[int, str | None, dict[str, Any]], dict[str, Any]]
    # po_id -> ReceiptStatus dump
    receipt_status: Callable[[int], dict[str, Any]]
    # po_id, invoice dump -> PriceVarianceResult dump
    price_variance: Callable[[int, dict[str, Any]], dict[str, Any]]
    # full graph state -> AnomalyReport dump (rules + LLM explanation)
    anomaly_check: Callable[[InvoiceState], dict[str, Any]]
    # thread_id, action, details
    audit: Callable[[str, str, dict[str, Any]], None]
    # one agent.parked_invoices row as a dict (keyed by thread_id); idempotent upsert
    upsert_parked: Callable[[dict[str, Any]], None]
    # NotifyEvent fields as JSON-safe dict; idempotent per (thread, kind)
    notify: Callable[[dict[str, Any]], None]
    # actor, thread_id, action, details: audit row written on behalf of a human
    audit_as: Callable[[str, str, str, dict[str, Any]], None]
    # vendor_id, po_id -> POMatch-like dict with po_number, or None when the PO is not the vendor's
    load_po: Callable[[int, int], dict[str, Any] | None]
    # thread_id, vendor_id, po_id, invoice dump, user, note -> trex.invoices id (idempotent)
    attach_invoice: Callable[[str, int, int | None, dict[str, Any], str, str], int]
    # thread_id, status, decision, user, note: marks the parked row decided (idempotent)
    record_decision: Callable[[str, str, str, str, str], None]


class AttachError(Exception):
    """The trex invoice exists but is no longer attachable (already PAID or VOID)."""


_UPSERT_PARKED = """
INSERT INTO agent.parked_invoices
    (thread_id, content_hash, s3_key, status, vendor_id, po_id, po_number, invoice_number,
     invoice_date, due_date, discount_deadline, currency, total, extracted_json, anomalies,
     anomaly_report, review_json, embedding, ready_at)
VALUES
    (%(thread_id)s, %(content_hash)s, %(s3_key)s, %(status)s, %(vendor_id)s, %(po_id)s,
     %(po_number)s,
     %(invoice_number)s, %(invoice_date)s, %(due_date)s, %(discount_deadline)s, %(currency)s,
     %(total)s,
     %(extracted_json)s, %(anomalies)s, %(anomaly_report)s, %(review_json)s,
     %(embedding)s::vector,
     CASE WHEN %(status)s = 'READY' THEN now() END)
ON CONFLICT (thread_id) DO UPDATE SET
    po_id = EXCLUDED.po_id, review_json = EXCLUDED.review_json,
    status = EXCLUDED.status, vendor_id = EXCLUDED.vendor_id, po_number = EXCLUDED.po_number,
    invoice_number = EXCLUDED.invoice_number, invoice_date = EXCLUDED.invoice_date,
    due_date = EXCLUDED.due_date, discount_deadline = EXCLUDED.discount_deadline, currency = EXCLUDED.currency, total = EXCLUDED.total,
    extracted_json = EXCLUDED.extracted_json, anomalies = EXCLUDED.anomalies,
    anomaly_report = EXCLUDED.anomaly_report, embedding = EXCLUDED.embedding,
    ready_at = CASE WHEN EXCLUDED.status = 'READY'
                    THEN COALESCE(agent.parked_invoices.ready_at, now()) END
"""

_ATTACH_INVOICE = """
INSERT INTO trex.invoices
    (vendor_id, po_id, invoice_number, invoice_date, currency, total, status, attached_at)
VALUES (%(vendor_id)s, %(po_id)s, %(invoice_number)s, %(invoice_date)s, %(currency)s, %(total)s,
        'ATTACHED', now())
ON CONFLICT (vendor_id, invoice_number) DO UPDATE SET
    status = 'ATTACHED', po_id = EXCLUDED.po_id, invoice_date = EXCLUDED.invoice_date,
    currency = EXCLUDED.currency, total = EXCLUDED.total,
    attached_at = COALESCE(trex.invoices.attached_at, now())
WHERE trex.invoices.status IN ('PENDING', 'ATTACHED')
RETURNING id
"""

_MARK_DECIDED = """
UPDATE agent.parked_invoices
SET status = %(status)s, decided_by = %(user)s, decided_at = COALESCE(decided_at, now()),
    decision = %(decision)s, decision_note = %(note)s
WHERE thread_id = %(thread_id)s
"""


def prod_deps(url: str) -> Deps:
    """Real tools. Each call uses its own short autocommit connection (pooler friendly)."""
    from ap_agent.embeddings import embed_texts, to_vector_literal
    from ap_agent.extract.cache import extract_and_cache
    from ap_agent.mail.storage import default_storage
    from ap_agent.tools.anomalies import check_anomalies
    from ap_agent.tools.duplicates import check_duplicate
    from ap_agent.tools.pricing import load_po_lines, price_variance
    from ap_agent.tools.purchase_orders import match_po
    from ap_agent.tools.receipt import receipt_status
    from ap_agent.tools.terms import load_vendor_terms, terms_result
    from ap_agent.tools.vendors import find_vendor

    @contextmanager
    def db() -> Iterator[psycopg.Connection[Any]]:
        with psycopg.connect(url, autocommit=True, connect_timeout=10) as conn:
            yield conn

    def extract(content_hash: str, s3_key: str, received: date | None) -> dict[str, Any]:
        pdf = default_storage().read(s3_key)  # infrastructure errors propagate (crash + resume)
        with db() as conn:
            try:
                res = extract_and_cache(conn, content_hash, pdf, received_date=received)
            except psycopg.Error:
                raise
            except Exception as exc:
                raise ExtractionError(f"{type(exc).__name__}: {str(exc)[:200]}") from exc
        return {
            "invoice": res.invoice.model_dump(mode="json"),
            "date_ambiguous": res.date_ambiguous,
        }

    def _vendor(name: str, sender: str | None) -> dict[str, Any]:
        with db() as conn:
            return find_vendor(conn, name, sender).model_dump(mode="json")

    def _dup(vendor_id: int | None, number: str, h: str) -> dict[str, Any]:
        with db() as conn:
            return check_duplicate(conn, vendor_id, number, h).model_dump(mode="json")

    def _terms(vendor_id: int, invoice_date: date) -> dict[str, str | None]:
        with db() as conn:
            terms = load_vendor_terms(conn, vendor_id)
        if terms is None:
            return {"due_date": None, "discount_deadline": None}
        r = terms_result(invoice_date, terms)
        return {
            "due_date": r.due_date.isoformat(),
            "discount_deadline": r.discount_deadline.isoformat() if r.discount_deadline else None,
        }

    def _match(vendor_id: int, subject: str | None, inv: dict[str, Any]) -> dict[str, Any]:
        invoice = ExtractedInvoice.model_validate(inv)
        with db() as conn:
            m = match_po(
                conn,
                vendor_id,
                invoice.po_number,
                subject,
                invoice.line_items,
                invoice.total,
                invoice.invoice_date,
            )
            out = m.model_dump(mode="json")
            row = (
                conn.execute(
                    "SELECT po_number FROM trex.purchase_orders WHERE id = %s", (m.po_id,)
                ).fetchone()
                if m.po_id is not None
                else None
            )
        out["po_number"] = row[0] if row else None
        return out

    def _receipt(po_id: int) -> dict[str, Any]:
        with db() as conn:
            return receipt_status(conn, po_id).model_dump(mode="json")

    def _pricing(po_id: int, inv: dict[str, Any]) -> dict[str, Any]:
        invoice = ExtractedInvoice.model_validate(inv)
        with db() as conn:
            lines = load_po_lines(conn, po_id)
        return price_variance(invoice.line_items, lines).model_dump(mode="json")

    def _anomalies(state: InvoiceState) -> dict[str, Any]:
        with db() as conn:
            return check_anomalies(dict(state), conn).model_dump(mode="json")

    def _audit(thread_id: str, action: str, details: dict[str, Any]) -> None:
        with db() as conn:
            conn.execute(
                "INSERT INTO agent.audit_log (actor, action, ref, details) VALUES (%s, %s, %s, %s)",
                (ACTOR, action, thread_id, Jsonb(details)),
            )

    def _upsert(row: dict[str, Any]) -> None:
        report = row.get("anomaly_report")
        text = (report or {}).get("summary_text")
        vec = embed_texts([text])[0] if text else None  # local model; same text as the check
        params = {
            "discount_deadline": None,
            "po_id": None,
            **row,
            "review_json": Jsonb(row["review_json"]) if row.get("review_json") else None,
            "anomaly_report": Jsonb(report) if report is not None else None,
            "embedding": to_vector_literal(vec) if vec is not None else None,
            "total": Decimal(row["total"]) if row.get("total") is not None else None,
            "extracted_json": Jsonb(row.get("extracted_json")),
            "anomalies": Jsonb(row.get("anomalies") or []),
        }
        with db() as conn:
            conn.execute(_UPSERT_PARKED, params)  # type: ignore[arg-type]

    def _notify(event: dict[str, Any]) -> None:
        from ap_agent.notify import NotifyEvent, notify

        with db() as conn:
            notify(conn, NotifyEvent.model_validate(event))

    def _audit_as(actor: str, thread_id: str, action: str, details: dict[str, Any]) -> None:
        with db() as conn:
            conn.execute(
                "INSERT INTO agent.audit_log (actor, action, ref, details) VALUES (%s, %s, %s, %s)",
                (actor, action, thread_id, Jsonb(details)),
            )

    def _load_po(vendor_id: int, po_id: int) -> dict[str, Any] | None:
        with db() as conn:
            row = conn.execute(
                "SELECT po_number FROM trex.purchase_orders WHERE id = %s AND vendor_id = %s",
                (po_id, vendor_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "po_id": po_id,
            "po_number": row[0],
            "method": "manual",
            "confidence": 1.0,
            "reasons": ["chosen by a reviewer"],
            "candidates": [],
        }

    def _attach(
        thread_id: str,
        vendor_id: int,
        po_id: int | None,
        inv: dict[str, Any],
        user: str,
        note: str,
    ) -> int:
        """Upsert trex.invoices and mark the parked row ATTACHED in one transaction."""
        params = {
            "vendor_id": vendor_id,
            "po_id": po_id,
            "invoice_number": inv["invoice_number"],
            "invoice_date": date.fromisoformat(inv["invoice_date"]),
            "currency": inv["currency"],
            "total": Decimal(str(inv["total"])),
        }
        with psycopg.connect(url, connect_timeout=10) as conn, conn.transaction():
            row = conn.execute(_ATTACH_INVOICE, params).fetchone()  # type: ignore[arg-type]
            if row is None:
                raise AttachError(
                    f"trex invoice {params['invoice_number']} is already paid or void"
                )
            conn.execute(
                _MARK_DECIDED,
                {
                    "status": "ATTACHED",
                    "user": user,
                    "decision": "approve",
                    "note": note,
                    "thread_id": thread_id,
                },
            )
            return int(row[0])

    def _record_decision(thread_id: str, status: str, decision: str, user: str, note: str) -> None:
        with db() as conn:
            conn.execute(
                _MARK_DECIDED,
                {
                    "status": status,
                    "user": user,
                    "decision": decision,
                    "note": note,
                    "thread_id": thread_id,
                },
            )

    return Deps(
        extract=extract,
        find_vendor=_vendor,
        check_duplicate=_dup,
        load_terms=_terms,
        match_po=_match,
        receipt_status=_receipt,
        price_variance=_pricing,
        anomaly_check=_anomalies,
        audit=_audit,
        upsert_parked=_upsert,
        notify=_notify,
        audit_as=_audit_as,
        load_po=_load_po,
        attach_invoice=_attach,
        record_decision=_record_decision,
    )
