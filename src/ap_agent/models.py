"""Pydantic v2 domain models.

Two invoice shapes:
- InvoiceExtraction: what the LLM fills. The date is a raw string exactly as printed; code (not
  the model) turns it into a date.
- ExtractedInvoice: the normalised invoice the rest of the app uses.
"""

from datetime import date
from decimal import Decimal
from typing import Any, Literal, Self

from pydantic import BaseModel, ValidationInfo, field_validator, model_validator

from ap_agent.extract.normalize import (
    DateOrder,
    clean_invoice_number,
    clean_po_number,
    parse_invoice_date,
    parse_money,
)

TOLERANCE = Decimal("0.01")


class LineItem(BaseModel):
    description: str
    part_no: str | None = None
    quantity: Decimal
    unit_price: Decimal
    line_total: Decimal

    @field_validator("quantity", "unit_price", "line_total", mode="before")
    @classmethod
    def _money(cls, v: Any) -> Any:
        return parse_money(v)


class _InvoiceBase(BaseModel):
    """Fields and checks shared by the LLM-facing and the normalised invoice."""

    vendor_name: str
    invoice_number: str
    po_number: str | None = None
    currency: Literal["CAD", "USD"]
    payment_terms: str | None = None
    subtotal: Decimal
    tax: Decimal
    total: Decimal
    line_items: list[LineItem]

    @field_validator("subtotal", "tax", "total", mode="before")
    @classmethod
    def _money(cls, v: Any) -> Any:
        return parse_money(v)

    @field_validator("invoice_number", mode="after")
    @classmethod
    def _inv_no(cls, v: str) -> str:
        return clean_invoice_number(v)

    @field_validator("po_number", mode="after")
    @classmethod
    def _po_no(cls, v: str | None) -> str | None:
        return clean_po_number(v)

    @model_validator(mode="after")
    def _check_totals(self) -> Self:
        """Arithmetic must reconcile. Messages name the failed check so an LLM retry can fix it."""
        for i, li in enumerate(self.line_items, start=1):
            calc = li.quantity * li.unit_price
            diff = abs(calc - li.line_total)
            if diff > TOLERANCE:
                raise ValueError(
                    f"line {i} ({li.description!r}): quantity * unit_price = {calc} but "
                    f"line_total = {li.line_total} (off by {diff}). Re-read the printed "
                    "quantity, unit price and line total for this line."
                )
        line_sum = sum((li.line_total for li in self.line_items), Decimal(0))
        diff = abs(line_sum - self.subtotal)
        if diff > TOLERANCE:
            raise ValueError(
                f"sum of line totals = {line_sum} but subtotal = {self.subtotal} "
                f"(off by {diff}). A line may be missing or misread."
            )
        diff = abs(self.subtotal + self.tax - self.total)
        if diff > TOLERANCE:
            raise ValueError(
                f"subtotal + tax = {self.subtotal + self.tax} but total = {self.total} "
                f"(off by {diff}). Re-read subtotal, tax and total as printed."
            )
        return self


class ExtractedInvoice(_InvoiceBase):
    """Normalised invoice used by the rest of the app."""

    invoice_date: date


class InvoiceExtraction(_InvoiceBase):
    """LLM-facing schema: the invoice date is the raw string as printed."""

    invoice_date_raw: str

    @field_validator("invoice_date_raw", mode="after")
    @classmethod
    def _date_is_sane(cls, v: str, info: ValidationInfo) -> str:
        """Fail (so the LLM can retry) on unparseable dates or impossible years."""
        ctx = info.context or {}
        parse_invoice_date(
            v, order=ctx.get("date_order", "DMY"), received_date=ctx.get("received_date")
        )
        return v.strip()

    def to_invoice(
        self, *, received_date: date | None = None, date_order: DateOrder = "DMY"
    ) -> tuple[ExtractedInvoice, bool]:
        """Normalise to ExtractedInvoice. Returns (invoice, date_ambiguous)."""
        parsed = parse_invoice_date(
            self.invoice_date_raw, order=date_order, received_date=received_date
        )
        data = self.model_dump(exclude={"invoice_date_raw"})
        return ExtractedInvoice.model_validate({**data, "invoice_date": parsed.value}), (
            parsed.ambiguous
        )


class ExtractionResult(BaseModel):
    """An extracted invoice plus how it was produced."""

    invoice: ExtractedInvoice
    raw: InvoiceExtraction
    date_ambiguous: bool = False
    method: Literal["text", "vision"]
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None
    retries: int = 0
    duration_ms: int = 0
    cached: bool = False
