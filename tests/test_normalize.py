from datetime import date
from decimal import Decimal

import pytest

from ap_agent.extract.normalize import (
    clean_invoice_number,
    clean_po_number,
    parse_invoice_date,
    parse_money,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$1,234.50", "1234.50"),
        ("CAD 62,559.37", "62559.37"),
        ("1 234.50", "1234.50"),
        ("(12.00)", "-12.00"),
        (62, "62"),
        (Decimal("1.10"), "1.10"),
    ],
)
def test_parse_money(raw: object, expected: str) -> None:
    assert parse_money(raw) == Decimal(expected)


@pytest.mark.parametrize("bad", ["abc", "", None, True])
def test_parse_money_rejects(bad: object) -> None:
    with pytest.raises(ValueError):
        parse_money(bad)


# --- dates ---


def test_ambiguous_numeric_defaults_day_first() -> None:
    p = parse_invoice_date("03/11/2026")
    assert p.value == date(2026, 11, 3)
    assert p.ambiguous


def test_ambiguous_numeric_month_first_when_configured() -> None:
    p = parse_invoice_date("03/11/2026", order="MDY")
    assert p.value == date(2026, 3, 11)
    assert p.ambiguous


@pytest.mark.parametrize("order", ["DMY", "MDY"])
def test_day_above_12_is_unambiguous(order: str) -> None:
    p = parse_invoice_date("13/11/2026", order=order)  # type: ignore[arg-type]
    assert p.value == date(2026, 11, 13)
    assert not p.ambiguous


def test_same_day_and_month_is_not_ambiguous() -> None:
    p = parse_invoice_date("05/05/2026")
    assert p.value == date(2026, 5, 5)
    assert not p.ambiguous


@pytest.mark.parametrize(
    "raw", ["2026-11-03", "Nov 3, 2026", "November 3, 2026", "3 Nov 2026", " 3  November 2026 "]
)
def test_unambiguous_formats(raw: str) -> None:
    p = parse_invoice_date(raw)
    assert p.value == date(2026, 11, 3)
    assert not p.ambiguous


@pytest.mark.parametrize("raw", ["0311-03-11", "03/11/0311", "1160-11-01", "03/11/1999"])
def test_impossible_year_rejected(raw: str) -> None:
    with pytest.raises(ValueError, match=r"year \d+ is outside 2000-2100"):
        parse_invoice_date(raw)


def test_received_date_resolves_ambiguity() -> None:
    # Received 12 Mar 2026: 3 Nov 2026 is in the future, so 11 March must be meant.
    p = parse_invoice_date("03/11/2026", received_date=date(2026, 3, 12))
    assert p.value == date(2026, 3, 11)
    assert not p.ambiguous


def test_received_date_allows_one_day_ahead() -> None:
    p = parse_invoice_date("2026-11-04", received_date=date(2026, 11, 3))
    assert p.value == date(2026, 11, 4)


def test_date_after_received_rejected() -> None:
    with pytest.raises(ValueError, match="after the email received date"):
        parse_invoice_date("2026-11-20", received_date=date(2026, 11, 3))
    with pytest.raises(ValueError, match="DMY.*MDY"):
        parse_invoice_date("03/11/2026", received_date=date(2026, 1, 1))


def test_both_valid_with_received_date_still_flags() -> None:
    p = parse_invoice_date("03/11/2026", received_date=date(2026, 12, 1))
    assert p.value == date(2026, 11, 3)
    assert p.ambiguous


@pytest.mark.parametrize("raw", ["sometime in fall", "31/31/2026", "03/11/26", ""])
def test_unparseable_rejected(raw: str) -> None:
    with pytest.raises(ValueError, match="exactly as printed"):
        parse_invoice_date(raw)


def test_clean_invoice_number() -> None:
    assert clean_invoice_number("  inv-31722 ") == "INV-31722"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PO#GEN-PO-0039", "GEN-PO-0039"),
        ("po: gen-po-0039", "GEN-PO-0039"),
        ("Purchase Order GEN-PO-0039", "GEN-PO-0039"),
        ("P.O. # 123", "123"),
        ("  GEN-PO-0039 ", "GEN-PO-0039"),
        ("PO#", None),
        (None, None),
    ],
)
def test_clean_po_number(raw: str | None, expected: str | None) -> None:
    assert clean_po_number(raw) == expected
