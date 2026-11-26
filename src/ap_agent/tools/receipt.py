"""Receipt status of a PO from trex.po_line_receipt / trex.po_receipt_status. Read-only."""

from decimal import Decimal
from typing import Any

import psycopg
from pydantic import BaseModel


class ReceiptLine(BaseModel):
    po_line_id: int
    part_no: str
    qty_ordered: Decimal
    qty_received: Decimal
    qty_outstanding: Decimal


class ReceiptStatus(BaseModel):
    po_id: int
    fully_received: bool
    lines: list[ReceiptLine]


def receipt_status(conn: psycopg.Connection[Any], po_id: int) -> ReceiptStatus:
    head = conn.execute(
        "SELECT fully_received FROM trex.po_receipt_status WHERE po_id = %s", (po_id,)
    ).fetchone()
    rows = conn.execute(
        "SELECT po_line_id, part_no, qty_ordered, qty_received FROM trex.po_line_receipt "
        "WHERE po_id = %s ORDER BY line_no",
        (po_id,),
    ).fetchall()
    return ReceiptStatus(
        po_id=po_id,
        fully_received=bool(head[0]) if head else False,
        lines=[
            ReceiptLine(
                po_line_id=r[0],
                part_no=r[1],
                qty_ordered=r[2],
                qty_received=r[3],
                qty_outstanding=max(Decimal(0), r[2] - r[3]),
            )
            for r in rows
        ],
    )
