"""Payment terms: due date and early-payment discount deadline. Vendor columns are authoritative."""

import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import psycopg
from pydantic import BaseModel

_DISCOUNT_NET = re.compile(
    r"(\d+(?:\.\d+)?)\s*/\s*(\d+)\s*,?\s*net\s*(\d+)", re.IGNORECASE
)  # 2/10 Net 30
_NET = re.compile(r"net\s*(\d+)", re.IGNORECASE)


class VendorTerms(BaseModel):
    payment_terms_days: int
    discount_pct: Decimal = Decimal(0)
    discount_days: int = 0


class TermsResult(BaseModel):
    due_date: date
    discount_deadline: date | None
    discount_pct: Decimal


def parse_terms(text: str) -> VendorTerms | None:
    """'2/10 Net 30' -> (30, 2, 10); 'Net 45' -> (45, 0, 0); anything else -> None."""
    if m := _DISCOUNT_NET.search(text):
        return VendorTerms(
            payment_terms_days=int(m[3]), discount_pct=Decimal(m[1]), discount_days=int(m[2])
        )
    if m := _NET.search(text):
        return VendorTerms(payment_terms_days=int(m[1]))
    return None


def load_vendor_terms(conn: psycopg.Connection[Any], vendor_id: int) -> VendorTerms | None:
    row = conn.execute(
        "SELECT payment_terms_days, discount_pct, discount_days FROM trex.vendors WHERE id = %s",
        (vendor_id,),
    ).fetchone()
    if row is None:
        return None
    return VendorTerms(payment_terms_days=row[0], discount_pct=row[1], discount_days=row[2])


def due_date(invoice_date: date, terms: VendorTerms) -> date:
    return invoice_date + timedelta(days=terms.payment_terms_days)


def discount_deadline(invoice_date: date, terms: VendorTerms) -> date | None:
    """Last day to pay for the discount; None when the vendor offers none."""
    if terms.discount_pct <= 0 or terms.discount_days <= 0:
        return None
    return invoice_date + timedelta(days=terms.discount_days)


def terms_result(invoice_date: date, terms: VendorTerms) -> TermsResult:
    return TermsResult(
        due_date=due_date(invoice_date, terms),
        discount_deadline=discount_deadline(invoice_date, terms),
        discount_pct=terms.discount_pct,
    )
