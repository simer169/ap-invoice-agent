"""Duplicate / reissue detection against parked invoices and TREX invoices. Read-only."""

from typing import Any, Literal

import psycopg
from pydantic import BaseModel

from ap_agent.tools._common import norm_invoice_no


class DuplicateResult(BaseModel):
    kind: Literal["new", "exact", "reissue"]
    existing_ref: str | None = None


def check_duplicate(
    conn: psycopg.Connection[Any],
    vendor_id: int | None,
    invoice_number: str,
    content_hash: str,
) -> DuplicateResult:
    """exact = same PDF hash already parked; reissue = same vendor + invoice number, other hash."""
    row = conn.execute(
        "SELECT thread_id FROM agent.parked_invoices WHERE content_hash = %s", (content_hash,)
    ).fetchone()
    if row:
        return DuplicateResult(kind="exact", existing_ref=str(row[0]))
    if vendor_id is None:
        return DuplicateResult(kind="new")

    wanted = norm_invoice_no(invoice_number)
    regex = r"[\W_]+"  # same normalisation as norm_invoice_no, done in SQL
    parked = conn.execute(
        "SELECT thread_id FROM agent.parked_invoices "
        "WHERE vendor_id = %(v)s AND upper(regexp_replace(invoice_number, %(re)s, '', 'g')) = %(n)s "
        "AND content_hash <> %(h)s ORDER BY id LIMIT 1",
        {"v": vendor_id, "re": regex, "n": wanted, "h": content_hash},
    ).fetchone()
    if parked:
        return DuplicateResult(kind="reissue", existing_ref=f"thread:{parked[0]}")
    booked = conn.execute(
        "SELECT id FROM trex.invoices "
        "WHERE vendor_id = %(v)s AND upper(regexp_replace(invoice_number, %(re)s, '', 'g')) = %(n)s "
        "ORDER BY id LIMIT 1",
        {"v": vendor_id, "re": regex, "n": wanted},
    ).fetchone()
    if booked:
        return DuplicateResult(kind="reissue", existing_ref=f"trex_invoice:{booked[0]}")
    return DuplicateResult(kind="new")
