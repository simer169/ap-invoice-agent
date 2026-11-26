"""PO matching: explicit PO number, PO hint in the email subject, else content scoring. Read-only."""

import re
from datetime import date
from decimal import Decimal
from typing import Any, Literal

import psycopg
from pydantic import BaseModel

from ap_agent.models import LineItem
from ap_agent.tools._common import norm_key, norm_po, within_pct
from ap_agent.tools.pricing import POLineInfo, load_po_lines

# Content-score weights (sum to 1.0).
W_PARTS, W_QTY, W_PRICE, W_TOTAL, W_DATE = (
    Decimal("0.45"),
    Decimal("0.15"),
    Decimal("0.15"),
    Decimal("0.15"),
    Decimal("0.10"),
)
LINE_TOLERANCE = Decimal("0.02")
TOTAL_TOLERANCE = Decimal("0.02")
# PO totals are pre-tax while invoice totals may include tax, so both readings are tried.
TAX_READINGS = (Decimal(0), Decimal("0.13"))
ACCEPT_SCORE = 0.85
ACCEPT_LEAD = 0.2
OPEN_STATUSES = ("OPEN", "PARTIAL", "RECEIVED")

_SUBJECT_PO = re.compile(
    r"(?:\bP\.?O\.?|\bpurchase\s+order)\s*(?:number|no\.?|#|:)*\s*([A-Z0-9][A-Z0-9\-]*\d[A-Z0-9\-]*)",
    re.IGNORECASE,
)


class POCandidate(BaseModel):
    po_id: int
    po_number: str
    score: float


class POMatch(BaseModel):
    po_id: int | None
    method: Literal["po_number", "subject_hint", "content_score", "none"]
    confidence: float = 0.0
    reasons: list[str] = []
    candidates: list[POCandidate] = []


class POInfo(BaseModel):
    po_id: int
    po_number: str
    order_date: date
    total: Decimal
    lines: list[POLineInfo]


def po_from_subject(subject: str | None) -> str | None:
    """'Billing for PO GEN-PO-0052' -> 'GEN-PO-0052'. Requires a digit so 'PO attached' is ignored."""
    if not subject:
        return None
    m = _SUBJECT_PO.search(subject)
    return norm_po(m.group(1)) if m else None


def score_po(
    lines: list[LineItem], total: Decimal, invoice_date: date | None, po: POInfo
) -> tuple[float, list[str]]:
    """Pure content score 0-1 for one candidate PO, with human-readable reasons."""
    reasons: list[str] = []
    by_part = {norm_key(p.part_no): p for p in po.lines}
    inv_parts = {norm_key(li.part_no): li for li in lines if li.part_no}
    shared = [k for k in inv_parts if k in by_part]
    union = len(set(inv_parts) | set(by_part))
    score = Decimal(0)
    if union:
        overlap = Decimal(len(shared)) / Decimal(union)
        score += W_PARTS * overlap
        reasons.append(f"part_no overlap {len(shared)}/{union}")
    if shared:
        qty_ok = sum(
            1
            for k in shared
            if within_pct(inv_parts[k].quantity, by_part[k].qty_ordered, LINE_TOLERANCE)
        )
        price_ok = sum(
            1
            for k in shared
            if within_pct(inv_parts[k].unit_price, by_part[k].unit_price, LINE_TOLERANCE)
        )
        score += W_QTY * Decimal(qty_ok) / len(shared) + W_PRICE * Decimal(price_ok) / len(shared)
        reasons.append(
            f"qty agrees on {qty_ok}/{len(shared)}, unit price on {price_ok}/{len(shared)}"
        )
    line_sum = sum((p.qty_ordered * p.unit_price for p in po.lines), Decimal(0))
    targets = {po.total, line_sum} - {Decimal(0)}
    if any(
        within_pct(total, t * (1 + tax), TOTAL_TOLERANCE) for t in targets for tax in TAX_READINGS
    ):
        score += W_TOTAL
        reasons.append("total within 2% of PO")
    if invoice_date is not None and po.order_date <= invoice_date:
        score += W_DATE
        reasons.append("PO date on/before invoice date")
    elif invoice_date is not None:
        reasons.append("PO dated after the invoice")
    return float(min(score, Decimal(1))), reasons


def decide(candidates: list[POCandidate]) -> POCandidate | None:
    """Accept the top candidate only if it is strong and clearly ahead of the runner-up."""
    if not candidates:
        return None
    top = candidates[0]
    if top.score < ACCEPT_SCORE:
        return None
    if len(candidates) > 1 and top.score - candidates[1].score < ACCEPT_LEAD:
        return None
    return top


def _lookup(
    conn: psycopg.Connection[Any], vendor_id: int, po_number: str
) -> tuple[int | None, str]:
    """Verify a PO number belongs to this vendor. Returns (po_id|None, reason)."""
    row = conn.execute(
        "SELECT id, vendor_id FROM trex.purchase_orders WHERE po_number = %s", (po_number,)
    ).fetchone()
    if row is None:
        return None, f"PO {po_number} not found"
    if row[1] != vendor_id:
        return None, "PO belongs to another vendor"
    return int(row[0]), f"PO {po_number} found for this vendor"


_CANDIDATE_SQL = """
SELECT po.id, po.po_number, po.order_date, po.total FROM trex.purchase_orders po
WHERE po.vendor_id = %(v)s AND po.status = ANY(%(st)s)
  AND NOT EXISTS (SELECT 1 FROM trex.invoices i WHERE i.po_id = po.id AND i.status <> 'VOID')
  AND NOT EXISTS (SELECT 1 FROM agent.parked_invoices pi
                  WHERE pi.po_number = po.po_number AND pi.status <> 'REJECTED')
ORDER BY po.id
"""


def match_po(
    conn: psycopg.Connection[Any],
    vendor_id: int,
    po_number: str | None,
    email_subject: str | None,
    lines: list[LineItem],
    total: Decimal,
    invoice_date: date | None = None,
) -> POMatch:
    reasons: list[str] = []

    explicit = norm_po(po_number)
    if explicit:
        po_id, why = _lookup(conn, vendor_id, explicit)
        if po_id is not None:
            return POMatch(po_id=po_id, method="po_number", confidence=1.0, reasons=[why])
        return POMatch(po_id=None, method="none", reasons=[why])

    hint = po_from_subject(email_subject)
    if hint:
        po_id, why = _lookup(conn, vendor_id, hint)
        if po_id is not None:
            return POMatch(
                po_id=po_id, method="subject_hint", confidence=0.95, reasons=[f"subject: {why}"]
            )
        reasons.append(f"subject hint {hint}: {why}")

    infos: list[POInfo] = []
    for r in conn.execute(_CANDIDATE_SQL, {"v": vendor_id, "st": list(OPEN_STATUSES)}).fetchall():
        infos.append(
            POInfo(
                po_id=r[0],
                po_number=r[1],
                order_date=r[2],
                total=r[3],
                lines=load_po_lines(conn, r[0]),
            )
        )
    scored: list[tuple[POCandidate, list[str]]] = []
    for info in infos:
        s, why_list = score_po(lines, total, invoice_date, info)
        scored.append(
            (POCandidate(po_id=info.po_id, po_number=info.po_number, score=round(s, 3)), why_list)
        )
    scored.sort(key=lambda t: (-t[0].score, t[0].po_number))
    cands = [c for c, _ in scored[:3]]

    best = decide([c for c, _ in scored])
    if best is not None:
        why_best = next(w for c, w in scored if c.po_id == best.po_id)
        return POMatch(
            po_id=best.po_id,
            method="content_score",
            confidence=best.score,
            reasons=[*reasons, *why_best],
            candidates=cands,
        )
    if not cands:
        reasons.append("no open PO for this vendor")
    else:
        reasons.append("no single PO clearly ahead on content score")
    return POMatch(po_id=None, method="none", reasons=reasons, candidates=cands)
