"""Pure helpers behind the approval cards (no DB)."""

from datetime import date
from decimal import Decimal
from typing import Any

from ap_agent import ui_queries as q


def _row(**over: Any) -> tuple[Any, ...]:
    base: dict[str, Any] = {
        "thread_id": "invoice-a",
        "status": "READY",
        "s3_key": "raw/a.pdf",
        "vendor_id": 7,
        "vendor_name": "Acme",
        "invoice_number": "INV-1",
        "invoice_date": date(2026, 11, 1),
        "po_id": 11,
        "po_number": "PO-1",
        "currency": "CAD",
        "total": Decimal("113.00"),
        "due_date": date(2026, 12, 1),
        "discount_deadline": None,
        "anomalies": ["Anomaly X (block): m"],
        "anomaly_report": {
            "flags": [
                {"code": "X", "severity": "block", "message": "m"},
                {"code": "Y", "severity": "info", "message": "n"},
            ],
            "explanation": "because",
        },
        "review_json": {
            "vendor": {
                "method": "fuzzy",
                "candidates": [{"vendor_id": 7, "name": "Acme", "score": 90}],
            },
            "po": {
                "method": "content_score",
                "confidence": 0.9,
                "candidates": [{"po_id": 11, "po_number": "PO-1", "score": 0.9}],
            },
        },
        "extracted_json": {
            "line_items": [{"part_no": "P1", "quantity": "2"}, {"part_no": "P1", "quantity": "3"}]
        },
        "decided_by": None,
        "decided_at": None,
        "decision": None,
        "decision_note": None,
    }
    base.update(over)
    return tuple(base.values())


def test_card_days_left_flags_and_candidates() -> None:
    card = q._card(_row(), today=date(2026, 11, 26))
    assert card.days_left == 5
    assert card.has_block_flag and [f.severity for f in card.flags] == ["block", "info"]
    assert card.explanation == "because" and card.po_method == "content_score"
    assert [(c.id, c.label) for c in card.vendor_candidates] == [(7, "Acme")]
    assert [(c.id, c.label) for c in card.po_candidates] == [(11, "PO-1")]
    assert card.billed_by_part == {"P1": Decimal(5)}


def test_card_overdue_and_missing_data() -> None:
    card = q._card(
        _row(
            due_date=date(2026, 11, 20), anomaly_report=None, review_json=None, extracted_json=None
        ),
        today=date(2026, 11, 26),
    )
    assert card.days_left == -6
    assert not card.has_block_flag and card.flags == [] and card.po_candidates == []
    assert q._card(_row(due_date=None), today=date(2026, 11, 26)).days_left is None


def test_billed_quantities_ignores_lines_without_part_or_bad_quantity() -> None:
    items = {
        "line_items": [
            {"part_no": None, "quantity": "1"},
            {"part_no": "A", "quantity": "x"},
            {"part_no": "A", "quantity": "1.5"},
        ]
    }
    assert q.billed_quantities(items) == {"A": Decimal("1.5")}
    assert q.billed_quantities(None) == {}


def test_merge_card_lines_ordered_received_billed() -> None:
    lines = [
        q.POLine(
            line_no=1, part_no="P1", description=None, unit_price=Decimal(1),
            qty_ordered=Decimal(10), qty_received=Decimal(10), fully_received=True,
        ),
        q.POLine(
            line_no=2, part_no="P2", description=None, unit_price=Decimal(1),
            qty_ordered=Decimal(5), qty_received=Decimal(2), fully_received=False,
        ),
    ]  # fmt: skip
    out = q.merge_card_lines(lines, {"P1": Decimal(10)})
    assert [(c.ordered, c.received, c.billed) for c in out] == [
        (Decimal(10), Decimal(10), Decimal(10)),
        (Decimal(5), Decimal(2), None),
    ]
