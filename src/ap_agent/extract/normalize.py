"""Plain-code normalisers applied to LLM output. No LLM calls here."""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal

DateOrder = Literal["DMY", "MDY"]

_CURRENCY_CHARS = re.compile(r"[$€£¥]|\b(?:CAD|USD|CDN)\b", re.IGNORECASE)
_PO_PREFIX = re.compile(
    r"^\s*(?:purchase\s+order|p\.?o\.?)\s*(?:number|no\.?|#|:)*\s*", re.IGNORECASE
)


def parse_money(value: object) -> Decimal:
    """'$1,234.50' / '1 234.50' / '(12.00)' -> Decimal. Raises ValueError if unparseable."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError(f"not a number: {value!r}")  # noqa: TRY004 (pydantic needs ValueError)
    if isinstance(value, int | float):
        return Decimal(str(value))
    if not isinstance(value, str):
        raise ValueError(f"not a number: {value!r}")  # noqa: TRY004 (pydantic needs ValueError)
    s = _CURRENCY_CHARS.sub("", value).strip()
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "").replace(" ", "").replace(" ", "")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"not a number: {value!r}") from None
    return -d if negative else d


# --- dates -----------------------------------------------------------------------------------

MIN_YEAR = 2000
MAX_YEAR = 2100

_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_NUMERIC = re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})$")
_NAMED_FORMATS = (
    "%b %d, %Y",
    "%B %d, %Y",
    "%b %d %Y",
    "%B %d %Y",
    "%d %b %Y",
    "%d %B %Y",
    "%d-%b-%Y",
)


@dataclass(frozen=True)
class ParsedDate:
    value: date
    ambiguous: bool = False  # True when both DD/MM and MM/DD were valid and the default was used


def _reject_reason(y: int, m: int, d: int, received_date: date | None) -> tuple[date | None, str]:
    """Return (date, "") if this reading is acceptable, else (None, why not)."""
    if not MIN_YEAR <= y <= MAX_YEAR:
        return None, f"year {y} is outside {MIN_YEAR}-{MAX_YEAR}"
    try:
        value = date(y, m, d)
    except ValueError:
        return None, f"{y:04d}-{m:02d}-{d:02d} is not a real calendar date"
    if received_date is not None and value > received_date + timedelta(days=1):
        return None, f"{value} is after the email received date {received_date}"
    return value, ""


def parse_invoice_date(
    raw: str, *, order: DateOrder = "DMY", received_date: date | None = None
) -> ParsedDate:
    """Parse an invoice date exactly as printed. Raises ValueError with a clear reason.

    - ISO (2026-11-03) and month-name dates (Nov 3, 2026 / 3 Nov 2026) are unambiguous.
    - Numeric NN/NN/YYYY is ambiguous. Both DD/MM and MM/DD readings are tried; readings that
      are not real dates, fall outside 2000-2100, or are later than received_date + 1 day are
      dropped. One survivor wins. If both survive, `order` (DMY default) wins and
      `ambiguous=True` is set so a clerk can be shown the doubt.
    """
    if not isinstance(raw, str):
        raise ValueError(f"invoice date must be text as printed, got {raw!r}")  # noqa: TRY004
    s = re.sub(r"\s+", " ", raw.strip())

    if m := _ISO.match(s):
        readings = {"ISO": (int(m[1]), int(m[2]), int(m[3]))}
    elif m := _NUMERIC.match(s):
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        readings = {"DMY": (y, b, a), "MDY": (y, a, b)}
    else:
        for fmt in _NAMED_FORMATS:
            try:
                dt = datetime.strptime(s, fmt)
            except ValueError:
                continue
            readings = {"NAMED": (dt.year, dt.month, dt.day)}
            break
        else:
            raise ValueError(
                f"invoice date {raw!r}: unrecognised format. Copy the date exactly as printed "
                "on the invoice."
            )

    valid: dict[str, date] = {}
    reasons: list[str] = []
    for label, (y, mo, d) in readings.items():
        value, why = _reject_reason(y, mo, d, received_date)
        if value is None:
            reasons.append(f"{label}: {why}" if len(readings) > 1 else why)
        else:
            valid[label] = value

    if not valid:
        raise ValueError(
            f"invoice date {raw!r} is not valid ({'; '.join(reasons)}). "
            "Copy the date exactly as printed on the invoice."
        )
    distinct = set(valid.values())
    if len(distinct) == 1:
        return ParsedDate(next(iter(distinct)))
    return ParsedDate(valid[order], ambiguous=True)


def clean_invoice_number(value: str) -> str:
    return value.strip().upper()


def clean_po_number(value: str | None) -> str | None:
    """Strip prefixes like 'PO#', 'PO:', 'Purchase Order'; uppercase; empty -> None."""
    if value is None:
        return None
    s = _PO_PREFIX.sub("", value.strip()).strip().upper()
    return s or None
