"""Price / quantity variance between invoice lines and PO lines. Computes only; no decisions."""

from decimal import Decimal
from typing import Any

import psycopg
from pydantic import BaseModel
from rapidfuzz import fuzz

from ap_agent.models import LineItem
from ap_agent.tools._common import norm_key

DEFAULT_TOLERANCE = Decimal("0.02")
DESCRIPTION_MATCH = 80


class POLineInfo(BaseModel):
    po_line_id: int
    line_no: int
    part_no: str
    description: str | None = None
    qty_ordered: Decimal
    qty_received: Decimal = Decimal(0)
    unit_price: Decimal


class LineVariance(BaseModel):
    invoice_line_index: int  # 0-based position in the invoice's line_items
    po_line_id: int
    part_no: str
    matched_by: str  # "part_no" | "description"
    unit_price_invoice: Decimal
    unit_price_po: Decimal
    variance_abs: Decimal  # invoice - PO
    variance_pct: Decimal | None  # None when the PO price is zero
    exceeds_tolerance: bool
    qty_billed: Decimal
    qty_ordered: Decimal
    qty_received: Decimal
    billed_over_received: Decimal  # max(0, billed - received)


class PriceVarianceResult(BaseModel):
    tolerance: Decimal
    lines: list[LineVariance]
    unmatched_invoice_lines: list[int]  # indexes of invoice lines with no PO line
    unbilled_po_line_ids: list[int]  # PO lines nothing on the invoice matched


def load_po_lines(conn: psycopg.Connection[Any], po_id: int) -> list[POLineInfo]:
    rows = conn.execute(
        "SELECT pl.id, pl.line_no, pl.part_no, pl.description, pl.qty_ordered, "
        "COALESCE(plr.qty_received, 0), pl.unit_price "
        "FROM trex.po_lines pl LEFT JOIN trex.po_line_receipt plr ON plr.po_line_id = pl.id "
        "WHERE pl.po_id = %s ORDER BY pl.line_no",
        (po_id,),
    ).fetchall()
    return [
        POLineInfo(
            po_line_id=r[0],
            line_no=r[1],
            part_no=r[2],
            description=r[3],
            qty_ordered=r[4],
            qty_received=r[5],
            unit_price=r[6],
        )
        for r in rows
    ]


def _match_lines(
    invoice_lines: list[LineItem], po_lines: list[POLineInfo]
) -> dict[int, tuple[POLineInfo, str]]:
    """Invoice line index -> (PO line, how). Each PO line is used at most once."""
    used: set[int] = set()
    out: dict[int, tuple[POLineInfo, str]] = {}
    by_part = {norm_key(p.part_no): p for p in po_lines}
    for i, li in enumerate(invoice_lines):
        p = by_part.get(norm_key(li.part_no)) if li.part_no else None
        if p is not None and p.po_line_id not in used:
            out[i] = (p, "part_no")
            used.add(p.po_line_id)
    for i, li in enumerate(invoice_lines):
        if i in out:
            continue
        best: tuple[float, POLineInfo] | None = None
        for p in po_lines:
            if p.po_line_id in used or not p.description:
                continue
            score = fuzz.token_set_ratio(li.description.casefold(), p.description.casefold())
            if score >= DESCRIPTION_MATCH and (best is None or score > best[0]):
                best = (score, p)
        if best is not None:
            out[i] = (best[1], "description")
            used.add(best[1].po_line_id)
    return out


def price_variance(
    invoice_lines: list[LineItem],
    po_lines: list[POLineInfo],
    tolerance: Decimal = DEFAULT_TOLERANCE,
) -> PriceVarianceResult:
    matches = _match_lines(invoice_lines, po_lines)
    lines: list[LineVariance] = []
    for i, (p, how) in sorted(matches.items()):
        li = invoice_lines[i]
        diff = li.unit_price - p.unit_price
        pct = diff / p.unit_price if p.unit_price != 0 else None
        exceeds = abs(pct) > tolerance if pct is not None else diff != 0
        lines.append(
            LineVariance(
                invoice_line_index=i,
                po_line_id=p.po_line_id,
                part_no=p.part_no,
                matched_by=how,
                unit_price_invoice=li.unit_price,
                unit_price_po=p.unit_price,
                variance_abs=diff,
                variance_pct=pct,
                exceeds_tolerance=exceeds,
                qty_billed=li.quantity,
                qty_ordered=p.qty_ordered,
                qty_received=p.qty_received,
                billed_over_received=max(Decimal(0), li.quantity - p.qty_received),
            )
        )
    matched_po = {p.po_line_id for p, _ in matches.values()}
    return PriceVarianceResult(
        tolerance=tolerance,
        lines=lines,
        unmatched_invoice_lines=[i for i in range(len(invoice_lines)) if i not in matches],
        unbilled_po_line_ids=[p.po_line_id for p in po_lines if p.po_line_id not in matched_po],
    )
