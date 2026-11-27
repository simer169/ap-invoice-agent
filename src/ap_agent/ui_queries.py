"""All SQL the UI needs. Read-only, typed, one connection argument.

Pages must not contain SQL; they call these functions (Phase 8 puts FastAPI in front of the same
functions). Every function only SELECTs, so a read-only connection or role is enough.
"""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import psycopg
from pydantic import BaseModel

from ap_agent.extract.normalize import DateOrder
from ap_agent.models import ExtractedInvoice, InvoiceExtraction

# --- models ----------------------------------------------------------------------------------


class InboxRow(BaseModel):
    message_id: str
    received_at: datetime | None
    from_address: str | None
    subject: str | None
    outcome: str
    error_message: str | None
    content_hash: str | None


class InvoiceRow(BaseModel):
    """One extracted invoice, normalised on read (no LLM call)."""

    content_hash: str
    s3_key: str | None
    email_from: str | None
    email_subject: str | None
    received_at: datetime | None
    vendor_name: str | None
    invoice_number: str | None
    invoice_date: date | None
    invoice_date_raw: str | None
    po_number: str | None
    currency: str | None
    total: Decimal | None
    method: str
    model: str
    cost_usd: float | None
    tokens_in: int
    tokens_out: int
    retries: int
    extracted_at: datetime
    date_ambiguous: bool
    missing_po: bool
    invoice: ExtractedInvoice | None
    error: str | None = None


class POSummary(BaseModel):
    po_number: str
    vendor_id: int
    vendor_name: str
    order_date: date
    status: str
    currency: str
    total: Decimal
    fully_received: bool
    lines_total: int
    lines_fully_received: int


class POLine(BaseModel):
    line_no: int
    part_no: str
    description: str | None
    unit_price: Decimal
    qty_ordered: Decimal
    qty_received: Decimal
    fully_received: bool


class ReceivedLine(BaseModel):
    line_no: int
    part_no: str
    qty_received: Decimal


class ReceivingReport(BaseModel):
    rr_number: str
    received_at: datetime
    received_by: str | None
    lines: list[ReceivedLine]


class PODetail(BaseModel):
    summary: POSummary
    lines: list[POLine]
    receiving_reports: list[ReceivingReport]
    invoices: list[InvoiceRow]


class VendorOption(BaseModel):
    id: int
    name: str


class VendorRow(BaseModel):
    id: int
    vendor_code: str
    name: str
    currency: str
    payment_terms_days: int
    discount_pct: Decimal
    discount_days: int
    active: bool
    invoice_count: int
    avg_total: Decimal | None
    min_total: Decimal | None
    max_total: Decimal | None


# --- inbox -----------------------------------------------------------------------------------


def inbox_counts(conn: psycopg.Connection[Any]) -> dict[str, int]:
    rows = conn.execute(
        "SELECT outcome, COUNT(*) FROM agent.processed_emails GROUP BY outcome ORDER BY outcome"
    ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def list_inbox(
    conn: psycopg.Connection[Any], outcome: str | None = None, limit: int = 1000
) -> list[InboxRow]:
    rows = conn.execute(
        "SELECT message_id, received_at, from_address, subject, outcome, error_message, "
        "content_hash FROM agent.processed_emails "
        "WHERE (%(o)s::text IS NULL OR outcome = %(o)s) "
        "ORDER BY received_at DESC NULLS LAST, message_id LIMIT %(n)s",
        {"o": outcome, "n": limit},
    ).fetchall()
    return [
        InboxRow(
            message_id=r[0],
            received_at=r[1],
            from_address=r[2],
            subject=r[3],
            outcome=r[4],
            error_message=r[5],
            content_hash=r[6],
        )
        for r in rows
    ]


# --- extracted invoices ----------------------------------------------------------------------

_INVOICE_SQL = """
SELECT e.content_hash, e.model, e.method, e.extracted_json, e.tokens_in, e.tokens_out,
       e.cost_usd, e.retries, e.created_at,
       a.details ->> 's3_key', a.details -> 'email'
FROM agent.extractions e
LEFT JOIN LATERAL (
    SELECT details FROM agent.audit_log
    WHERE action = 'RUN_REQUESTED' AND ref = 'invoice-' || e.content_hash
    ORDER BY id LIMIT 1
) a ON true
"""


def _received(email: dict[str, Any] | None) -> datetime | None:
    value = (email or {}).get("received_at")
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def _invoice_row(r: tuple[Any, ...], date_order: DateOrder) -> InvoiceRow:
    email: dict[str, Any] = r[10] or {}
    received_at = _received(email)
    common: dict[str, Any] = {
        "content_hash": r[0],
        "model": r[1],
        "method": r[2],
        "tokens_in": r[4],
        "tokens_out": r[5],
        "cost_usd": float(r[6]) if r[6] is not None else None,
        "retries": r[7],
        "extracted_at": r[8],
        "s3_key": r[9],
        "email_from": email.get("from"),
        "email_subject": email.get("subject"),
        "received_at": received_at,
    }
    empty: dict[str, Any] = dict.fromkeys(
        (
            "vendor_name",
            "invoice_number",
            "invoice_date",
            "invoice_date_raw",
            "po_number",
            "currency",
            "total",
            "invoice",
        )
    )
    try:
        raw = InvoiceExtraction.model_validate(r[3])
    except ValueError as exc:  # stored JSON no longer fits the schema
        return InvoiceRow(
            **common,
            **empty,
            date_ambiguous=False,
            missing_po=False,
            error=f"stored extraction is invalid: {str(exc).splitlines()[0]}",
        )
    try:
        inv, ambiguous = raw.to_invoice(
            received_date=received_at.date() if received_at else None, date_order=date_order
        )
    except ValueError as exc:  # e.g. date rules reject the printed date
        return InvoiceRow(
            **common,
            **{**empty, "vendor_name": raw.vendor_name, "invoice_number": raw.invoice_number},
            invoice_date_raw=raw.invoice_date_raw,
            po_number=raw.po_number,
            currency=raw.currency,
            total=raw.total,
            date_ambiguous=False,
            missing_po=raw.po_number is None,
            error=str(exc),
        )
    return InvoiceRow(
        **common,
        vendor_name=inv.vendor_name,
        invoice_number=inv.invoice_number,
        invoice_date=inv.invoice_date,
        invoice_date_raw=raw.invoice_date_raw,
        po_number=inv.po_number,
        currency=inv.currency,
        total=inv.total,
        date_ambiguous=ambiguous,
        missing_po=inv.po_number is None,
        invoice=inv,
    )


def list_extracted_invoices(
    conn: psycopg.Connection[Any], date_order: DateOrder = "DMY"
) -> list[InvoiceRow]:
    rows = conn.execute(_INVOICE_SQL + " ORDER BY e.created_at DESC, e.content_hash").fetchall()
    return [_invoice_row(r, date_order) for r in rows]


def get_invoice(
    conn: psycopg.Connection[Any], content_hash: str, date_order: DateOrder = "DMY"
) -> InvoiceRow | None:
    row = conn.execute(_INVOICE_SQL + " WHERE e.content_hash = %s", (content_hash,)).fetchone()
    return _invoice_row(row, date_order) if row else None


def invoices_for_po(
    conn: psycopg.Connection[Any], po_number: str, date_order: DateOrder = "DMY"
) -> list[InvoiceRow]:
    """Extracted invoices whose (normalised) PO number matches. Demo scale, filtered in Python."""
    wanted = po_number.strip().upper()
    return [r for r in list_extracted_invoices(conn, date_order) if r.po_number == wanted]


# --- purchase orders -------------------------------------------------------------------------

_PO_SQL = """
SELECT po.po_number, po.vendor_id, v.name, po.order_date, po.status, po.currency, po.total,
       COALESCE(s.fully_received, false), COALESCE(s.lines_total, 0),
       COALESCE(s.lines_fully_received, 0)
FROM trex.purchase_orders po
JOIN trex.vendors v ON v.id = po.vendor_id
LEFT JOIN trex.po_receipt_status s ON s.po_id = po.id
"""


def _po_summary(r: tuple[Any, ...]) -> POSummary:
    return POSummary(
        po_number=r[0],
        vendor_id=r[1],
        vendor_name=r[2],
        order_date=r[3],
        status=r[4],
        currency=r[5],
        total=r[6],
        fully_received=bool(r[7]),
        lines_total=int(r[8]),
        lines_fully_received=int(r[9]),
    )


def list_po_statuses(conn: psycopg.Connection[Any]) -> list[str]:
    rows = conn.execute("SELECT DISTINCT status FROM trex.purchase_orders ORDER BY status")
    return [str(r[0]) for r in rows.fetchall()]


def list_vendor_options(conn: psycopg.Connection[Any]) -> list[VendorOption]:
    rows = conn.execute("SELECT id, name FROM trex.vendors ORDER BY name").fetchall()
    return [VendorOption(id=r[0], name=r[1]) for r in rows]


def list_purchase_orders(
    conn: psycopg.Connection[Any], status: str | None = None, vendor_id: int | None = None
) -> list[POSummary]:
    rows = conn.execute(
        _PO_SQL + " WHERE (%(s)s::text IS NULL OR po.status = %(s)s) "
        "AND (%(v)s::bigint IS NULL OR po.vendor_id = %(v)s) "
        "ORDER BY po.order_date DESC, po.po_number",
        {"s": status, "v": vendor_id},
    ).fetchall()
    return [_po_summary(r) for r in rows]


def get_po_detail(
    conn: psycopg.Connection[Any], po_number: str, date_order: DateOrder = "DMY"
) -> PODetail | None:
    head = conn.execute(_PO_SQL + " WHERE po.po_number = %s", (po_number,)).fetchone()
    if head is None:
        return None
    lines = conn.execute(
        "SELECT plr.line_no, plr.part_no, pl.description, pl.unit_price, plr.qty_ordered, "
        "plr.qty_received, plr.fully_received "
        "FROM trex.po_line_receipt plr JOIN trex.po_lines pl ON pl.id = plr.po_line_id "
        "JOIN trex.purchase_orders po ON po.id = plr.po_id "
        "WHERE po.po_number = %s ORDER BY plr.line_no",
        (po_number,),
    ).fetchall()
    rr_rows = conn.execute(
        "SELECT rr.id, rr.rr_number, rr.received_at, rr.received_by, pl.line_no, pl.part_no, "
        "rl.qty_received "
        "FROM trex.receiving_reports rr JOIN trex.purchase_orders po ON po.id = rr.po_id "
        "LEFT JOIN trex.receiving_lines rl ON rl.rr_id = rr.id "
        "LEFT JOIN trex.po_lines pl ON pl.id = rl.po_line_id "
        "WHERE po.po_number = %s ORDER BY rr.received_at, rr.id, pl.line_no",
        (po_number,),
    ).fetchall()
    reports: dict[int, ReceivingReport] = {}
    for r in rr_rows:
        rep = reports.setdefault(
            r[0],
            ReceivingReport(rr_number=r[1], received_at=r[2], received_by=r[3], lines=[]),
        )
        if r[4] is not None:
            rep.lines.append(ReceivedLine(line_no=r[4], part_no=r[5], qty_received=r[6]))
    return PODetail(
        summary=_po_summary(head),
        lines=[
            POLine(
                line_no=r[0],
                part_no=r[1],
                description=r[2],
                unit_price=r[3],
                qty_ordered=r[4],
                qty_received=r[5],
                fully_received=bool(r[6]),
            )
            for r in lines
        ],
        receiving_reports=list(reports.values()),
        invoices=invoices_for_po(conn, po_number, date_order),
    )


# --- vendors ---------------------------------------------------------------------------------


def list_vendors(conn: psycopg.Connection[Any]) -> list[VendorRow]:
    rows = conn.execute(
        "SELECT v.id, v.vendor_code, v.name, v.currency, v.payment_terms_days, v.discount_pct, "
        "v.discount_days, v.active, COUNT(h.id), AVG(h.total)::numeric(12,2), MIN(h.total), "
        "MAX(h.total) "
        "FROM trex.vendors v LEFT JOIN trex.vendor_invoice_history h ON h.vendor_id = v.id "
        "GROUP BY v.id ORDER BY v.name"
    ).fetchall()
    return [
        VendorRow(
            id=r[0],
            vendor_code=r[1],
            name=r[2],
            currency=r[3],
            payment_terms_days=r[4],
            discount_pct=r[5],
            discount_days=r[6],
            active=r[7],
            invoice_count=int(r[8]),
            avg_total=r[9],
            min_total=r[10],
            max_total=r[11],
        )
        for r in rows
    ]


# --- approval queue --------------------------------------------------------------------------

Tab = Literal["ready", "exceptions", "waiting", "decided"]
DECIDED_LIMIT = 50

_TAB_SQL: dict[str, tuple[tuple[str, ...], str]] = {
    "ready": (("READY",), "p.ready_at, p.id"),
    "exceptions": (("EXCEPTION",), "p.parked_at, p.id"),
    "waiting": (("WAITING",), "p.due_date NULLS LAST, p.id"),
    "decided": (("ATTACHED", "REJECTED"), "p.decided_at DESC NULLS LAST, p.id DESC"),
}

_CARD_SQL = """
SELECT p.thread_id, p.status, p.s3_key, p.vendor_id, v.name, p.invoice_number, p.invoice_date,
       p.po_id, p.po_number, p.currency, p.total, p.due_date, p.discount_deadline, p.anomalies,
       p.anomaly_report, p.review_json, p.extracted_json, p.decided_by, p.decided_at, p.decision,
       p.decision_note
FROM agent.parked_invoices p LEFT JOIN trex.vendors v ON v.id = p.vendor_id
WHERE p.status = ANY(%(statuses)s)
ORDER BY {order}
LIMIT %(n)s
"""


class FlagChip(BaseModel):
    code: str
    severity: Literal["info", "warning", "block"]
    message: str


class Candidate(BaseModel):
    id: int
    label: str
    score: float | None = None


class ApprovalCard(BaseModel):
    thread_id: str
    status: str
    s3_key: str | None
    vendor_id: int | None
    vendor_name: str | None
    invoice_number: str | None
    invoice_date: date | None
    po_id: int | None
    po_number: str | None
    currency: str | None
    total: Decimal | None
    due_date: date | None
    days_left: int | None
    discount_deadline: date | None
    reasons: list[str]
    flags: list[FlagChip]
    explanation: str | None
    vendor_method: str | None
    po_method: str | None
    po_confidence: float | None
    vendor_candidates: list[Candidate]
    po_candidates: list[Candidate]
    billed_by_part: dict[str, Decimal]
    decided_by: str | None
    decided_at: datetime | None
    decision: str | None
    decision_note: str | None

    @property
    def has_block_flag(self) -> bool:
        return any(f.severity == "block" for f in self.flags)


class CardLine(BaseModel):
    line_no: int
    part_no: str
    description: str | None
    ordered: Decimal
    received: Decimal
    billed: Decimal | None  # None: the invoice has no line with this part number


class Notification(BaseModel):
    id: int
    created_at: datetime
    thread_id: str | None
    kind: str
    title: str
    body: str
    read: bool


def billed_quantities(extracted: dict[str, Any] | None) -> dict[str, Decimal]:
    """Invoice quantity per part number (lines without a part number are not matched)."""
    out: dict[str, Decimal] = {}
    for item in (extracted or {}).get("line_items") or []:
        part = str(item.get("part_no") or "").strip()
        if not part:
            continue
        try:
            qty = Decimal(str(item.get("quantity")))
        except InvalidOperation:
            continue
        out[part] = out.get(part, Decimal(0)) + qty
    return out


def merge_card_lines(po_lines: list[POLine], billed: dict[str, Decimal]) -> list[CardLine]:
    """PO lines ordered vs received vs billed."""
    return [
        CardLine(
            line_no=ln.line_no,
            part_no=ln.part_no,
            description=ln.description,
            ordered=ln.qty_ordered,
            received=ln.qty_received,
            billed=billed.get(ln.part_no),
        )
        for ln in po_lines
    ]


def _candidates(
    items: list[dict[str, Any]] | None, id_key: str, label_keys: tuple[str, ...]
) -> list[Candidate]:
    out: list[Candidate] = []
    for it in items or []:
        if it.get(id_key) is None:
            continue
        label = next((str(it[k]) for k in label_keys if it.get(k)), str(it[id_key]))
        score = it.get("score", it.get("confidence"))
        out.append(
            Candidate(
                id=int(it[id_key]), label=label, score=float(score) if score is not None else None
            )
        )
    return out


def _card(r: tuple[Any, ...], today: date) -> ApprovalCard:
    report: dict[str, Any] = r[14] or {}
    review: dict[str, Any] = r[15] or {}
    vendor: dict[str, Any] = review.get("vendor") or {}
    po: dict[str, Any] = review.get("po") or {}
    due: date | None = r[11]
    return ApprovalCard(
        thread_id=r[0],
        status=r[1],
        s3_key=r[2],
        vendor_id=r[3],
        vendor_name=r[4],
        invoice_number=r[5],
        invoice_date=r[6],
        po_id=r[7],
        po_number=r[8],
        currency=r[9],
        total=r[10],
        due_date=due,
        days_left=(due - today).days if due else None,
        discount_deadline=r[12],
        reasons=[str(x) for x in (r[13] or [])],
        flags=[FlagChip.model_validate(f) for f in report.get("flags") or []],
        explanation=report.get("explanation"),
        vendor_method=vendor.get("method"),
        po_method=po.get("method"),
        po_confidence=po.get("confidence"),
        vendor_candidates=_candidates(
            vendor.get("candidates"), "vendor_id", ("name", "vendor_name")
        ),
        po_candidates=_candidates(po.get("candidates"), "po_id", ("po_number",)),
        billed_by_part=billed_quantities(r[16]),
        decided_by=r[17],
        decided_at=r[18],
        decision=r[19],
        decision_note=r[20],
    )


def list_approval_cards(conn: psycopg.Connection[Any], tab: Tab, today: date) -> list[ApprovalCard]:
    """Parked invoices for one queue tab. `today` is passed in (demo date or real date)."""
    statuses, order = _TAB_SQL[tab]
    sql = _CARD_SQL.format(order=order)
    rows = conn.execute(
        sql, {"statuses": list(statuses), "n": DECIDED_LIMIT if tab == "decided" else 500}
    ).fetchall()
    return [_card(r, today) for r in rows]


def approval_counts(conn: psycopg.Connection[Any]) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM agent.parked_invoices GROUP BY status"
    ).fetchall()
    by = {str(r[0]): int(r[1]) for r in rows}
    return {
        "ready": by.get("READY", 0),
        "exceptions": by.get("EXCEPTION", 0),
        "waiting": by.get("WAITING", 0),
        "decided": min(by.get("ATTACHED", 0) + by.get("REJECTED", 0), DECIDED_LIMIT),
    }


def card_po_lines(
    conn: psycopg.Connection[Any], po_number: str | None, billed: dict[str, Decimal]
) -> list[CardLine]:
    """The card's PO lines: ordered vs received (from TREX) vs billed (from the invoice)."""
    if not po_number:
        return []
    rows = conn.execute(
        "SELECT plr.line_no, plr.part_no, pl.description, pl.unit_price, plr.qty_ordered, "
        "plr.qty_received, plr.fully_received "
        "FROM trex.po_line_receipt plr JOIN trex.po_lines pl ON pl.id = plr.po_line_id "
        "JOIN trex.purchase_orders po ON po.id = plr.po_id "
        "WHERE po.po_number = %s ORDER BY plr.line_no",
        (po_number,),
    ).fetchall()
    lines = [
        POLine(
            line_no=r[0],
            part_no=r[1],
            description=r[2],
            unit_price=r[3],
            qty_ordered=r[4],
            qty_received=r[5],
            fully_received=bool(r[6]),
        )
        for r in rows
    ]
    return merge_card_lines(lines, billed)


# --- notifications ---------------------------------------------------------------------------


def unread_notification_count(conn: psycopg.Connection[Any]) -> int:
    row = conn.execute("SELECT COUNT(*) FROM agent.notifications WHERE read_at IS NULL").fetchone()
    return int(row[0]) if row else 0


def list_notifications(conn: psycopg.Connection[Any], limit: int = 20) -> list[Notification]:
    rows = conn.execute(
        "SELECT id, created_at, thread_id, kind, title, body, read_at IS NOT NULL "
        "FROM agent.notifications ORDER BY created_at DESC, id DESC LIMIT %s",
        (limit,),
    ).fetchall()
    return [
        Notification(
            id=r[0], created_at=r[1], thread_id=r[2], kind=r[3], title=r[4], body=r[5], read=r[6]
        )
        for r in rows
    ]


def presigned_pdf_url(s3_key: str, expires: int = 600) -> str:
    """Short-lived presigned URL for an invoice PDF (no DB; lives here so pages use one layer)."""
    from ap_agent.mail.storage import default_storage

    return default_storage().presigned_url(s3_key, expires=expires)
