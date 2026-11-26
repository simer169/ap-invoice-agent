"""Graph state. Nested tool results are stored as JSON-safe dicts (model_dump(mode='json'))."""

import operator
from typing import Annotated, Any, Literal, TypedDict

Status = Literal["NEW", "WAITING", "READY", "EXCEPTION", "REJECTED"]


class EmailMeta(TypedDict, total=False):
    message_id: str | None
    from_address: str | None
    subject: str | None
    received_at: str | None


class InvoiceState(TypedDict, total=False):
    thread_id: str
    content_hash: str
    s3_key: str
    email_meta: EmailMeta
    extraction: dict[str, Any]  # ExtractedInvoice dump, plus "date_ambiguous"
    vendor: dict[str, Any]  # VendorMatch
    po: dict[str, Any]  # POMatch
    duplicate: dict[str, Any]  # DuplicateResult
    receipt: dict[str, Any]  # ReceiptStatus
    pricing: dict[str, Any]  # PriceVarianceResult
    anomaly_report: dict[str, Any]  # AnomalyReport dump (flags, similar invoices, explanation)
    status: Status
    reasons: Annotated[list[str], operator.add]  # human readable
    due_date: str | None  # ISO date
    discount_deadline: str | None  # ISO date
    history: Annotated[list[str], operator.add]  # audit trail
