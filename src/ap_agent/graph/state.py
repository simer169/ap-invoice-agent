"""Graph state. Nested tool results are stored as JSON-safe dicts (model_dump(mode='json'))."""

import operator
from typing import Annotated, Any, Literal, TypedDict

Status = Literal["NEW", "WAITING", "READY", "EXCEPTION", "REJECTED", "ATTACHED"]

RESET = "__reset__"  # first element of a reasons update: replace the list instead of appending


def add_or_reset(left: list[str], right: list[str]) -> list[str]:
    """Reducer for `reasons`: append, unless the update starts with RESET (then replace)."""
    if right and right[0] == RESET:
        return list(right[1:])
    return [*left, *right]


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
    overrides: dict[str, Any]  # human choices on an exception: vendor_id, po_id
    decision: dict[str, Any]  # last human decision (accepted or refused)
    decision_error: str | None  # set when the last decision was refused
    status: Status
    reasons: Annotated[list[str], add_or_reset]  # human readable
    due_date: str | None  # ISO date
    discount_deadline: str | None  # ISO date
    history: Annotated[list[str], operator.add]  # audit trail
