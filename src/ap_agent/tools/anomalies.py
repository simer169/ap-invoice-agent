"""Anomaly check (Agent 4). Rules decide; the LLM only explains.

`evaluate_rules` is pure (no DB, no LLM). `check_anomalies` loads history stats and similar past
invoices, runs the rules, and makes ONE LLM call to explain warning/block flags in plain English
(cached by thread_id). The explanation never changes a flag or a decision.
"""

import hashlib
import json
import re
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import psycopg
from pydantic import BaseModel, Field

from ap_agent.embeddings import embed_texts, line_summary, summary_text, to_vector_literal
from ap_agent.tools.pricing import PriceVarianceResult

Severity = Literal["info", "warning", "block"]

PRICE_WARN_PCT = Decimal("0.02")
PRICE_BLOCK_PCT = Decimal("0.10")
OUTLIER_SIGMAS = Decimal(3)
MIN_HISTORY = 5  # fewer past invoices than this: no outlier test
SIMILAR_K = 5
SIMILAR_FAR_BELOW = Decimal("0.5")  # neighbours' median total below 50% of this total

_SEVERITY_ORDER: dict[str, int] = {"info": 0, "warning": 1, "block": 2}


class Flag(BaseModel):
    code: str
    severity: Severity
    message: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class SimilarInvoice(BaseModel):
    invoice_number: str
    invoice_date: date
    total: Decimal
    currency: str
    distance: float


class AnomalyReport(BaseModel):
    flags: list[Flag] = Field(default_factory=list)
    similar_invoices: list[SimilarInvoice] = Field(default_factory=list)
    explanation: str | None = None
    summary_text: str | None = None  # what was embedded; the parked row embeds the same text

    @property
    def needs_explanation(self) -> bool:
        return any(f.severity in ("warning", "block") for f in self.flags)


@dataclass(frozen=True)
class HistoryStats:
    n: int
    mean: Decimal | None
    std: Decimal | None


@dataclass(frozen=True)
class ExplainResult:
    text: str
    model: str
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None


ExplainFn = Callable[[dict[str, Any]], ExplainResult]


# ------------------------------------------------------------------------------------- rules
def _dec(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except InvalidOperation:
        return None


def _received_date(email_meta: dict[str, Any] | None) -> date | None:
    raw = (email_meta or {}).get("received_at")
    try:
        return date.fromisoformat(str(raw)[:10]) if raw else None
    except ValueError:
        return None


def _worst(severities: list[Severity]) -> Severity:
    return max(severities, key=lambda s: _SEVERITY_ORDER[s])


def _price_flag(pricing: PriceVarianceResult) -> Flag | None:
    hits: list[tuple[Severity, dict[str, Any]]] = []
    for ln in pricing.lines:
        pct = ln.variance_pct
        if pct is None:  # PO price is zero: any positive price is over
            if ln.variance_abs <= 0:
                continue
            sev: Severity = "block"
        elif pct > PRICE_BLOCK_PCT:
            sev = "block"
        elif pct > PRICE_WARN_PCT:
            sev = "warning"
        else:
            continue
        hits.append(
            (
                sev,
                {
                    "line_index": ln.invoice_line_index,
                    "part_no": ln.part_no,
                    "unit_price_invoice": str(ln.unit_price_invoice),
                    "unit_price_po": str(ln.unit_price_po),
                    "pct_over": str((pct * 100).quantize(Decimal("0.01")))
                    if pct is not None
                    else None,
                },
            )
        )
    if not hits:
        return None
    severity = _worst([h[0] for h in hits])
    return Flag(
        code="PRICE_OVER_TOLERANCE",
        severity=severity,
        message=f"{len(hits)} line(s) priced above the PO price by more than "
        f"{int(PRICE_WARN_PCT * 100)}%",
        evidence={"lines": [h[1] for h in hits]},
    )


def evaluate_rules(
    invoice: dict[str, Any],
    po: dict[str, Any],
    pricing: PriceVarianceResult,
    email_meta: dict[str, Any] | None,
    vendor_currency: str | None,
    stats: HistoryStats,
) -> list[Flag]:
    """All rule flags for one invoice. Pure."""
    flags: list[Flag] = []
    if (f := _price_flag(pricing)) is not None:
        flags.append(f)

    over = [ln for ln in pricing.lines if ln.billed_over_received > 0]
    if over:
        flags.append(
            Flag(
                code="QTY_OVER_RECEIVED",
                severity="block",
                message=f"{len(over)} line(s) billed for more than was received",
                evidence={
                    "lines": [
                        {
                            "line_index": ln.invoice_line_index,
                            "part_no": ln.part_no,
                            "qty_billed": str(ln.qty_billed),
                            "qty_received": str(ln.qty_received),
                        }
                        for ln in over
                    ]
                },
            )
        )

    if pricing.unmatched_invoice_lines:
        lines = invoice.get("line_items") or []
        flags.append(
            Flag(
                code="LINE_NOT_ON_PO",
                severity="block",
                message=f"{len(pricing.unmatched_invoice_lines)} invoice line(s) not on the PO",
                evidence={
                    "lines": [
                        {
                            "line_index": i,
                            "part_no": lines[i].get("part_no") if i < len(lines) else None,
                            "description": lines[i].get("description") if i < len(lines) else None,
                            "line_total": str(lines[i].get("line_total"))
                            if i < len(lines)
                            else None,
                        }
                        for i in pricing.unmatched_invoice_lines
                    ]
                },
            )
        )

    currency = invoice.get("currency")
    if vendor_currency and currency and currency != vendor_currency:
        flags.append(
            Flag(
                code="CURRENCY_MISMATCH",
                severity="warning",
                message=f"Invoice is in {currency} but the vendor is set up in {vendor_currency}",
                evidence={"invoice_currency": currency, "vendor_currency": vendor_currency},
            )
        )

    received = _received_date(email_meta)
    inv_date = invoice.get("invoice_date")
    if received and inv_date and date.fromisoformat(str(inv_date)) > received:
        flags.append(
            Flag(
                code="FUTURE_DATE",
                severity="warning",
                message="Invoice date is after the day the email was received",
                evidence={"invoice_date": str(inv_date), "received_date": received.isoformat()},
            )
        )

    if invoice.get("date_ambiguous"):
        flags.append(
            Flag(
                code="DATE_AMBIGUOUS",
                severity="info",
                message="The invoice date format is ambiguous (day/month order)",
                evidence={"invoice_date": str(inv_date)},
            )
        )

    if po.get("method") == "content_score":
        flags.append(
            Flag(
                code="PO_MATCHED_WITHOUT_NUMBER",
                severity="info",
                message="PO was matched by content because the invoice has no usable PO number",
                evidence={"po_number": po.get("po_number"), "confidence": po.get("confidence")},
            )
        )

    total = _dec(invoice.get("total"))
    if (
        total is not None
        and stats.n >= MIN_HISTORY
        and stats.mean is not None
        and stats.std is not None
        and stats.std > 0
        and abs(total - stats.mean) > OUTLIER_SIGMAS * stats.std
    ):
        sigmas = abs(total - stats.mean) / stats.std
        flags.append(
            Flag(
                code="AMOUNT_OUTLIER",
                severity="warning",
                message=f"Total is {sigmas:.1f} standard deviations from this vendor's usual amount",
                evidence={
                    "total": str(total),
                    "history_mean": f"{stats.mean:.2f}",
                    "history_std": f"{stats.std:.2f}",
                    "history_count": stats.n,
                    "sigmas": f"{sigmas:.1f}",
                },
            )
        )
    return flags


def add_similar_evidence(
    flags: list[Flag], total: Decimal | None, similar: list[SimilarInvoice]
) -> None:
    """If the nearest neighbours' totals are far below this total, say so on AMOUNT_OUTLIER."""
    if total is None or not similar:
        return
    median = statistics.median(s.total for s in similar)
    if median >= total * SIMILAR_FAR_BELOW:
        return
    for f in flags:
        if f.code == "AMOUNT_OUTLIER":
            f.evidence["similar_median_total"] = f"{median:.2f}"
            f.evidence["similar_totals"] = [f"{s.total:.2f}" for s in similar]


# --------------------------------------------------------------------------------- explanation
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _canon(token: str) -> str:
    t = token.replace(",", "")
    try:
        return str(Decimal(t).normalize())
    except InvalidOperation:
        return t


def numbers_in(text: str) -> set[str]:
    return {_canon(m) for m in _NUMBER.findall(text)}


def unsupported_numbers(explanation: str, context: dict[str, Any]) -> set[str]:
    """Numbers in the explanation that appear nowhere in the evidence it was given."""
    allowed = numbers_in(json.dumps(context, default=str))
    return numbers_in(explanation) - allowed


def _flags_hash(report: AnomalyReport) -> str:
    payload = {
        "flags": [f.model_dump(mode="json") for f in report.flags],
        "similar": [s.model_dump(mode="json") for s in report.similar_invoices],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:32]


def _explain_context(
    invoice: dict[str, Any], vendor_name: str, report: AnomalyReport
) -> dict[str, Any]:
    return {
        "vendor": vendor_name,
        "invoice_number": invoice.get("invoice_number"),
        "total": f"{invoice.get('total')} {invoice.get('currency')}",
        "flags": [f.model_dump(mode="json") for f in report.flags if f.severity != "info"],
        "similar_invoices": [s.model_dump(mode="json") for s in report.similar_invoices],
    }


def llm_explain(context: dict[str, Any]) -> ExplainResult:
    """ONE call to STRONG_MODEL. Retries (instructor) if it invents a number."""
    from pydantic import ValidationInfo, field_validator

    from ap_agent.config import get_settings
    from ap_agent.llm import USAGE_EXTRA_BODY, get_client, track_usage

    class Explanation(BaseModel):
        text: str = Field(description="1-3 plain-English sentences for the AP clerk")

        @field_validator("text")
        @classmethod
        def _no_new_numbers(cls, v: str, info: ValidationInfo) -> str:
            bad = unsupported_numbers(v, (info.context or {}).get("evidence", {}))
            if bad:
                raise ValueError(f"numbers not in the evidence: {sorted(bad)}; use only given ones")
            return v

    model = get_settings().STRONG_MODEL
    with track_usage() as usage:
        result = get_client().chat.completions.create(
            model=model,
            response_model=Explanation,
            temperature=0,
            max_retries=2,
            extra_body=USAGE_EXTRA_BODY,
            validation_context={"evidence": context},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You explain accounts-payable anomaly flags to a clerk in 1-3 plain "
                        "sentences. Use ONLY the facts and numbers in the evidence. Do not "
                        "recommend approving, paying or rejecting, and do not add new numbers."
                    ),
                },
                {"role": "user", "content": json.dumps(context, default=str)},
            ],
        )
    return ExplainResult(
        text=result.text.strip(),
        model=model,
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
        cost_usd=usage.cost_usd,
    )


# ------------------------------------------------------------------------------------ DB layer
def load_history_stats(
    conn: psycopg.Connection[Any], vendor_id: int, currency: str | None
) -> HistoryStats:
    row = conn.execute(
        "SELECT count(*), avg(total), stddev_samp(total) FROM trex.vendor_invoice_history "
        "WHERE vendor_id = %s AND currency = %s",
        (vendor_id, currency),
    ).fetchone()
    assert row is not None
    return HistoryStats(n=int(row[0]), mean=row[1], std=row[2])


def load_similar(
    conn: psycopg.Connection[Any], vendor_id: int, currency: str | None, vec: list[float]
) -> list[SimilarInvoice]:
    lit = to_vector_literal(vec)
    rows = conn.execute(
        "SELECT invoice_number, invoice_date, total, currency, embedding <=> %s::vector AS dist "
        "FROM trex.vendor_invoice_history "
        "WHERE vendor_id = %s AND currency = %s AND embedding IS NOT NULL "
        "ORDER BY dist LIMIT %s",
        (lit, vendor_id, currency, SIMILAR_K),
    ).fetchall()
    return [
        SimilarInvoice(
            invoice_number=r[0], invoice_date=r[1], total=r[2], currency=r[3], distance=float(r[4])
        )
        for r in rows
    ]


def _cached_explanation(
    conn: psycopg.Connection[Any], thread_id: str, flags_hash: str
) -> str | None:
    row = conn.execute(
        "SELECT flags_hash, explanation FROM agent.anomaly_explanations WHERE thread_id = %s",
        (thread_id,),
    ).fetchone()
    return row[1] if row and row[0] == flags_hash else None


def _store_explanation(
    conn: psycopg.Connection[Any], thread_id: str, flags_hash: str, res: ExplainResult
) -> None:
    conn.execute(
        "INSERT INTO agent.anomaly_explanations "
        "(thread_id, flags_hash, explanation, model, tokens_in, tokens_out, cost_usd) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (thread_id) DO UPDATE SET flags_hash = EXCLUDED.flags_hash, "
        "explanation = EXCLUDED.explanation, model = EXCLUDED.model, "
        "tokens_in = EXCLUDED.tokens_in, tokens_out = EXCLUDED.tokens_out, "
        "cost_usd = EXCLUDED.cost_usd, created_at = now()",
        (thread_id, flags_hash, res.text, res.model, res.tokens_in, res.tokens_out, res.cost_usd),
    )


def check_anomalies(
    state: dict[str, Any],
    conn: psycopg.Connection[Any],
    *,
    embed: Callable[[list[str]], list[list[float]]] = embed_texts,
    explain: ExplainFn = llm_explain,
) -> AnomalyReport:
    """Run all anomaly rules for the graph state and explain warning/block flags."""
    invoice: dict[str, Any] = dict(state.get("extraction") or {})
    vendor_id = int(state["vendor"]["vendor_id"])
    pricing = PriceVarianceResult.model_validate(
        state.get("pricing")
        or {
            "tolerance": "0.02",
            "lines": [],
            "unmatched_invoice_lines": [],
            "unbilled_po_line_ids": [],
        }
    )

    vrow = conn.execute(
        "SELECT name, currency FROM trex.vendors WHERE id = %s", (vendor_id,)
    ).fetchone()
    vendor_name, vendor_currency = (
        (vrow[0], vrow[1]) if vrow else (invoice.get("vendor_name", ""), None)
    )
    currency = invoice.get("currency")

    stats = load_history_stats(conn, vendor_id, currency)
    flags = evaluate_rules(
        invoice,
        dict(state.get("po") or {}),
        pricing,
        state.get("email_meta"),
        vendor_currency,
        stats,
    )

    lines_text = line_summary(
        (ln.get("part_no"), ln.get("description") or "", ln.get("quantity") or 0)
        for ln in invoice.get("line_items") or []
    )
    summary = summary_text(vendor_name, lines_text, invoice.get("total") or 0, currency or "")
    similar: list[SimilarInvoice] = []
    try:
        vec: list[float] | None = embed([summary])[0]
    except Exception:  # noqa: BLE001  embeddings are an aid only; rules must still run without them
        vec = None
    if vec is not None:
        similar = load_similar(conn, vendor_id, currency, vec)
    add_similar_evidence(flags, _dec(invoice.get("total")), similar)

    report = AnomalyReport(flags=flags, similar_invoices=similar, summary_text=summary)
    if report.needs_explanation:
        thread_id = state["thread_id"]
        key = _flags_hash(report)
        text = _cached_explanation(conn, thread_id, key)
        if text is None:
            res = explain(_explain_context(invoice, vendor_name, report))
            _store_explanation(conn, thread_id, key, res)
            text = res.text
        report.explanation = text
    return report
