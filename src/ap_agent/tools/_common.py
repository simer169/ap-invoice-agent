"""Small shared helpers for the TREX validation tools. Pure functions, no DB."""

import re
from decimal import Decimal

from ap_agent.extract.normalize import clean_po_number

_NON_ALNUM = re.compile(r"[\W_]+", re.UNICODE)


def norm_key(value: str | None) -> str:
    """Case/space/punctuation-insensitive key: 'Acme, Inc.' -> 'acmeinc'."""
    return _NON_ALNUM.sub("", (value or "").casefold())


def norm_po(value: str | None) -> str | None:
    """Normalise a PO number like the extractor does ('PO# gen-po-0052' -> 'GEN-PO-0052')."""
    return clean_po_number(value)


def norm_invoice_no(value: str | None) -> str:
    """Invoice numbers compare ignoring case, spaces and punctuation ('inv-001' == 'INV 001')."""
    return norm_key(value).upper()


def within_pct(a: Decimal, b: Decimal, pct: Decimal) -> bool:
    """True when a is within pct (e.g. 0.02) of b. Zero only matches zero."""
    if b == 0:
        return a == 0
    return abs(a - b) / abs(b) <= pct
