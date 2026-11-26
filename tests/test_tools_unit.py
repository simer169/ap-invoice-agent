"""Unit tests for the pure parts of the TREX tools (no database)."""

from datetime import date
from decimal import Decimal

from ap_agent.models import LineItem
from ap_agent.tools.pricing import POLineInfo, price_variance
from ap_agent.tools.purchase_orders import (
    POCandidate,
    POInfo,
    decide,
    po_from_subject,
    score_po,
)
from ap_agent.tools.terms import (
    VendorTerms,
    discount_deadline,
    due_date,
    parse_terms,
    terms_result,
)
from ap_agent.tools.vendors import VendorRef, pick_vendor, sender_domain

V = [
    VendorRef(id=1, name="Rhodes Draft Systems Inc.", email_domain="rhodesdraft.example"),
    VendorRef(id=2, name="Walker Industrial Coatings", email_domain="walkercoatings.example"),
    VendorRef(id=3, name="Walker Industrial Couplings", email_domain="walkercouplings.example"),
]


# --- vendors ---------------------------------------------------------------------------------


def test_exact_name_ignores_case_space_punctuation() -> None:
    m = pick_vendor(V, "RHODES  draft systems, inc", None)
    assert (m.vendor_id, m.method) == (1, "exact_name")


def test_email_domain_when_name_differs() -> None:
    m = pick_vendor(V, "Totally Different Name", "Billing <ar@rhodesdraft.example>")
    assert (m.vendor_id, m.method) == (1, "email_domain")


def test_subdomain_matches_vendor_domain() -> None:
    assert sender_domain("a@mail.Acme.example") == "mail.acme.example"
    m = pick_vendor(V, "x", "ar@mail.rhodesdraft.example")
    assert m.vendor_id == 1


def test_fuzzy_90_plus_matches() -> None:
    m = pick_vendor(V, "Rhodes Draft System", None)
    assert (m.vendor_id, m.method) == (1, "fuzzy")
    assert m.score >= 90


def test_fuzzy_80_to_89_lists_candidates_only() -> None:
    m = pick_vendor(V, "Walker Industrial", None)  # equally close to two vendors
    assert m.vendor_id is None
    assert {c.vendor_id for c in m.candidates} >= {2, 3}


def test_no_match() -> None:
    m = pick_vendor(V, "Zzz Qqq Unknown", "x@nowhere.example")
    assert (m.vendor_id, m.method, m.candidates) == (None, "none", [])


# --- PO helpers ------------------------------------------------------------------------------


def test_subject_po_regex() -> None:
    assert po_from_subject("Billing for PO GEN-PO-0052") == "GEN-PO-0052"
    assert po_from_subject("Invoice 123 re: P.O. # 4471-A") == "4471-A"
    assert po_from_subject("Purchase Order no. GEN-PO-0001 invoice") == "GEN-PO-0001"
    assert po_from_subject("Invoice 20901") is None
    assert po_from_subject("PO attached") is None
    assert po_from_subject(None) is None


def _line(part: str, qty: str, price: str) -> LineItem:
    return LineItem(
        description=part,
        part_no=part,
        quantity=Decimal(qty),
        unit_price=Decimal(price),
        line_total=Decimal(qty) * Decimal(price),
    )


def _po(po_id: int, lines: list[tuple[str, str, str]], d: date = date(2026, 10, 1)) -> POInfo:
    pl = [
        POLineInfo(
            po_line_id=po_id * 100 + i,
            line_no=i,
            part_no=p,
            qty_ordered=Decimal(q),
            unit_price=Decimal(u),
        )
        for i, (p, q, u) in enumerate(lines, start=1)
    ]
    total = sum((x.qty_ordered * x.unit_price for x in pl), Decimal(0))
    return POInfo(po_id=po_id, po_number=f"P{po_id}", order_date=d, total=total, lines=pl)


def test_score_perfect_and_poor() -> None:
    rows = [("A-1", "10", "5.00"), ("B-2", "4", "20.00")]
    inv = [_line(p, q, u) for p, q, u in rows]
    total = Decimal(130) * Decimal("1.13")
    good, _ = score_po(inv, total, date(2026, 11, 1), _po(1, rows))
    bad, _ = score_po(inv, total, date(2026, 11, 1), _po(2, [("Z-9", "1", "1.00")]))
    assert good == 1.0
    assert bad < 0.3


def test_score_penalises_po_dated_after_invoice() -> None:
    rows = [("A-1", "10", "5.00")]
    inv = [_line("A-1", "10", "5.00")]
    on_time, _ = score_po(inv, Decimal(50), date(2026, 11, 1), _po(1, rows))
    late, _ = score_po(inv, Decimal(50), date(2026, 9, 1), _po(1, rows))
    assert round(on_time - late, 2) == 0.10


def test_decide_threshold_and_lead() -> None:
    c = POCandidate
    assert decide([]) is None
    assert decide([c(po_id=1, po_number="a", score=0.84)]) is None
    assert decide([c(po_id=1, po_number="a", score=0.85)]).po_id == 1  # type: ignore[union-attr]
    close = [c(po_id=1, po_number="a", score=0.95), c(po_id=2, po_number="b", score=0.80)]
    assert decide(close) is None  # lead only 0.15
    clear = [c(po_id=1, po_number="a", score=0.95), c(po_id=2, po_number="b", score=0.70)]
    assert decide(clear).po_id == 1  # type: ignore[union-attr]


# --- pricing ---------------------------------------------------------------------------------


def _pl(i: int, part: str, desc: str | None, q: str, recv: str, price: str) -> POLineInfo:
    return POLineInfo(
        po_line_id=i,
        line_no=i,
        part_no=part,
        description=desc,
        qty_ordered=Decimal(q),
        qty_received=Decimal(recv),
        unit_price=Decimal(price),
    )


def test_price_variance_by_part_and_description_and_unmatched() -> None:
    po = [
        _pl(1, "A-1", "Zinc primer 20L", "10", "10", "100.00"),
        _pl(2, "B-2", "Thinner 20L", "5", "2", "50.00"),
        _pl(3, "C-3", "Never billed", "1", "0", "9.00"),
    ]
    inv = [
        _line("A-1", "10", "105.00"),  # +5% over tolerance
        LineItem(
            description="thinner 20L",
            part_no=None,
            quantity=Decimal(5),
            unit_price=Decimal("50.50"),
            line_total=Decimal("252.50"),
        ),  # fuzzy match, +1% within tolerance, billed 5 vs received 2
        _line("X-9", "1", "1.00"),  # not on the PO
    ]
    r = price_variance(inv, po)
    by_idx = {x.invoice_line_index: x for x in r.lines}
    assert by_idx[0].matched_by == "part_no"
    assert by_idx[0].variance_pct == Decimal("0.05")
    assert by_idx[0].exceeds_tolerance is True
    assert by_idx[1].matched_by == "description"
    assert by_idx[1].exceeds_tolerance is False
    assert by_idx[1].billed_over_received == Decimal(3)
    assert r.unmatched_invoice_lines == [2]
    assert r.unbilled_po_line_ids == [3]


# --- terms -----------------------------------------------------------------------------------


def test_two_ten_net_thirty_dates() -> None:
    t = parse_terms("2/10 Net 30")
    assert t == VendorTerms(payment_terms_days=30, discount_pct=Decimal(2), discount_days=10)
    inv = date(2026, 11, 4)
    r = terms_result(inv, t)
    assert r.discount_deadline == date(2026, 11, 14)
    assert r.due_date == date(2026, 12, 4)
    assert r.discount_pct == Decimal(2)


def test_net_terms_without_discount_and_month_rollover() -> None:
    t = parse_terms("Net 45")
    assert t is not None
    assert due_date(date(2026, 12, 20), t) == date(2027, 2, 3)
    assert discount_deadline(date(2026, 12, 20), t) is None
    assert parse_terms("due on receipt") is None
