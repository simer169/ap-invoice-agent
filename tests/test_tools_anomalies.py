"""Anomaly rules (pure, no DB/LLM), explanation guard, and the DB-backed check with fakes."""

import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ap_agent.embeddings import line_summary, summary_text, to_vector_literal
from ap_agent.tools.anomalies import (
    AnomalyReport,
    ExplainResult,
    Flag,
    HistoryStats,
    SimilarInvoice,
    add_similar_evidence,
    check_anomalies,
    evaluate_rules,
    unsupported_numbers,
)
from ap_agent.tools.pricing import LineVariance, PriceVarianceResult, load_po_lines, price_variance
from tools_seed import connect, integration, seed_vendor

D = Decimal
NO_STATS = HistoryStats(n=0, mean=None, std=None)


def _line(po_price: str, inv_price: str, billed: str = "10", received: str = "10") -> LineVariance:
    p, i = D(po_price), D(inv_price)
    diff = i - p
    return LineVariance(
        invoice_line_index=0, po_line_id=1, part_no="P-1", matched_by="part_no",
        unit_price_invoice=i, unit_price_po=p, variance_abs=diff,
        variance_pct=diff / p if p else None, exceeds_tolerance=abs(diff / p) > D("0.02"),
        qty_billed=D(billed), qty_ordered=D("10"), qty_received=D(received),
        billed_over_received=max(D(0), D(billed) - D(received)),
    )  # fmt: skip


def _pricing(*lines: LineVariance, unmatched: list[int] | None = None) -> PriceVarianceResult:
    return PriceVarianceResult(
        tolerance=D("0.02"), lines=list(lines),
        unmatched_invoice_lines=unmatched or [], unbilled_po_line_ids=[],
    )  # fmt: skip


INVOICE: dict[str, Any] = {
    "currency": "CAD", "total": "1000.00", "invoice_date": "2026-11-01",
    "line_items": [{"part_no": "P-1", "description": "Widget", "quantity": "10"}],
}  # fmt: skip
PO: dict[str, Any] = {"method": "po_number", "po_number": "PO-1"}
META = {"received_at": "2026-11-02T10:00:00"}


def _codes(flags: list[Flag]) -> dict[str, str]:
    return {f.code: f.severity for f in flags}


def _rules(pricing: PriceVarianceResult | None = None, **kw: Any) -> list[Flag]:
    args: dict[str, Any] = {
        "invoice": INVOICE, "po": PO, "pricing": pricing or _pricing(_line("100", "100")),
        "email_meta": META, "vendor_currency": "CAD", "stats": NO_STATS,
    }  # fmt: skip
    args.update(kw)
    return evaluate_rules(**args)


def test_clean_invoice_has_no_flags() -> None:
    assert _rules() == []


@pytest.mark.parametrize(
    ("inv_price", "expected"),
    [
        ("102.00", None),  # exactly 2% is within tolerance
        ("102.01", "warning"),
        ("110.00", "warning"),  # exactly 10% is not a block
        ("110.01", "block"),
        ("90.00", None),  # cheaper than PO is not an overcharge
    ],
)
def test_price_over_tolerance_thresholds(inv_price: str, expected: str | None) -> None:
    flags = _rules(_pricing(_line("100", inv_price)))
    assert _codes(flags).get("PRICE_OVER_TOLERANCE") == expected


def test_price_flag_takes_worst_line_and_lists_evidence() -> None:
    flags = _rules(_pricing(_line("100", "103"), _line("100", "120")))
    f = next(f for f in flags if f.code == "PRICE_OVER_TOLERANCE")
    assert f.severity == "block" and len(f.evidence["lines"]) == 2


def test_qty_over_received_blocks() -> None:
    flags = _rules(_pricing(_line("100", "100", billed="10", received="7")))
    assert _codes(flags) == {"QTY_OVER_RECEIVED": "block"}
    assert _rules(_pricing(_line("100", "100", billed="7", received="7"))) == []


def test_line_not_on_po_blocks() -> None:
    inv = {**INVOICE, "line_items": [{"part_no": "X", "description": "Fee", "line_total": "9"}]}
    flags = _rules(_pricing(unmatched=[0]), invoice=inv)
    f = next(f for f in flags if f.code == "LINE_NOT_ON_PO")
    assert f.severity == "block" and f.evidence["lines"][0]["part_no"] == "X"


def test_currency_mismatch_warns() -> None:
    assert _codes(_rules(vendor_currency="USD")) == {"CURRENCY_MISMATCH": "warning"}
    assert _rules(vendor_currency=None) == []


def test_future_date_warns_same_day_ok() -> None:
    assert _codes(_rules(invoice={**INVOICE, "invoice_date": "2026-11-03"})) == {
        "FUTURE_DATE": "warning"
    }
    assert _rules(invoice={**INVOICE, "invoice_date": "2026-11-02"}) == []
    assert _rules(email_meta={}) == []  # no received date: cannot judge


def test_info_flags() -> None:
    flags = _rules(invoice={**INVOICE, "date_ambiguous": True}, po={"method": "content_score"})
    assert _codes(flags) == {"DATE_AMBIGUOUS": "info", "PO_MATCHED_WITHOUT_NUMBER": "info"}
    assert not AnomalyReport(flags=flags).needs_explanation


@pytest.mark.parametrize(
    ("total", "stats", "flagged"),
    [
        ("1300.00", HistoryStats(10, D("1000"), D("100")), False),  # exactly 3 sigma
        ("1300.01", HistoryStats(10, D("1000"), D("100")), True),
        ("700.00", HistoryStats(10, D("1000"), D("100")), False),
        ("699.99", HistoryStats(10, D("1000"), D("100")), True),  # unusually low also flags
        ("9999.00", HistoryStats(4, D("1000"), D("100")), False),  # too little history
        ("9999.00", HistoryStats(10, D("1000"), D("0")), False),  # no spread
        ("9999.00", NO_STATS, False),
    ],
)
def test_amount_outlier(total: str, stats: HistoryStats, flagged: bool) -> None:
    flags = _rules(invoice={**INVOICE, "total": total}, stats=stats)
    assert ("AMOUNT_OUTLIER" in _codes(flags)) is flagged


def _similar(*totals: str) -> list[SimilarInvoice]:
    return [
        SimilarInvoice(invoice_number=f"H{i}", invoice_date="2026-01-01", total=D(t),
                       currency="CAD", distance=0.1)
        for i, t in enumerate(totals)
    ]  # fmt: skip


def test_similar_invoices_add_evidence_only_when_far_below() -> None:
    stats = HistoryStats(10, D("1000"), D("100"))
    flags = _rules(invoice={**INVOICE, "total": "5000"}, stats=stats)
    add_similar_evidence(flags, D("5000"), _similar("900", "1000", "1100"))
    ev = flags[0].evidence
    assert ev["similar_median_total"] == "1000.00" and len(ev["similar_totals"]) == 3

    near = _rules(invoice={**INVOICE, "total": "5000"}, stats=stats)
    add_similar_evidence(near, D("5000"), _similar("4000", "4500", "5200"))
    assert "similar_median_total" not in near[0].evidence
    add_similar_evidence(near, D("5000"), [])  # no neighbours: no change, no crash


def test_unsupported_numbers_guard() -> None:
    ctx = {"flags": [{"evidence": {"total": "6,839.04", "pct_over": "5.44"}}]}
    assert unsupported_numbers("The total of 6839.04 is 5.44% over.", ctx) == set()
    assert unsupported_numbers("That is 12% over.", ctx) == {"12"}


def test_summary_text_formats() -> None:
    lines = line_summary([("BRG-1", "Bearing cap", D("80")), (None, "Fee", D("1.5"))])
    assert lines == "80 x BRG-1 Bearing cap, 1.5 x Fee"
    assert summary_text("Hill", lines, D("12.5"), "CAD") == f"Hill | {lines} | 12.50 CAD"
    assert to_vector_literal([0.5, 1.0]) == "[0.5,1]"


# ------------------------------------------------------------------------ DB-backed (fakes)
def _fake_embed(texts: list[str]) -> list[list[float]]:
    return [[1.0] + [0.0] * 383 for _ in texts]


class Explainer:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, context: dict[str, Any]) -> ExplainResult:
        self.calls.append(context)
        return ExplainResult(text="Mocked explanation.", model="m", tokens_in=1, tokens_out=1)


def _state(vendor_id: int, thread_id: str, pricing: PriceVarianceResult) -> dict[str, Any]:
    return {
        "thread_id": thread_id, "extraction": INVOICE | {"vendor_name": "V"},
        "vendor": {"vendor_id": vendor_id}, "po": PO, "email_meta": META,
        "pricing": pricing.model_dump(mode="json"),
    }  # fmt: skip


def _history(c: Any, vendor_id: int, totals: list[str]) -> None:
    for i, t in enumerate(totals):
        c.execute(
            "INSERT INTO trex.vendor_invoice_history (vendor_id, invoice_number, invoice_date, "
            "currency, total, line_summary, embedding) VALUES (%s, %s, '2026-01-01', 'CAD', %s, "
            "%s, %s::vector)",
            (vendor_id, f"H-{i}", t, "10 x P-1 Widget", to_vector_literal(_fake_embed([""])[0])),
        )


@pytest.fixture
def conn() -> Iterator[Any]:
    yield from connect()  # rolled back on teardown


@integration[0]
@integration[1]
def test_check_anomalies_explains_once_and_caches(conn: Any) -> None:
    c = conn
    vid = seed_vendor(c, "Anomaly Test Vendor")
    _history(c, vid, ["900", "1000", "1100", "1000", "950", "1050"])
    ex = Explainer()
    st = _state(vid, "invoice-anomtest", _pricing(_line("100", "120")))
    inv_total = {**st["extraction"], "total": "9000.00"}
    st["extraction"] = inv_total
    rep = check_anomalies(st, c, embed=_fake_embed, explain=ex)
    codes = {f.code for f in rep.flags}
    assert {"PRICE_OVER_TOLERANCE", "AMOUNT_OUTLIER"} <= codes
    assert rep.explanation == "Mocked explanation." and len(ex.calls) == 1
    assert len(rep.similar_invoices) == 5
    amount = next(f for f in rep.flags if f.code == "AMOUNT_OUTLIER")
    assert "similar_median_total" in amount.evidence
    # same thread + same flags: served from the cache, no second LLM call
    rep2 = check_anomalies(st, c, embed=_fake_embed, explain=ex)
    assert rep2.explanation == "Mocked explanation." and len(ex.calls) == 1


@integration[0]
@integration[1]
def test_no_explain_call_without_warning_or_block(conn: Any) -> None:
    c = conn
    vid = seed_vendor(c, "Anomaly Quiet Vendor")
    ex = Explainer()
    rep = check_anomalies(
        _state(vid, "invoice-quiet", _pricing(_line("100", "100"))),
        c, embed=_fake_embed, explain=ex,
    )  # fmt: skip
    assert rep.flags == [] and rep.explanation is None and ex.calls == []


@integration[0]
@integration[1]
def test_embedding_failure_degrades_to_rules_only(conn: Any) -> None:
    c = conn
    vid = seed_vendor(c, "Anomaly NoEmbed Vendor")

    def boom(_: list[str]) -> list[list[float]]:
        raise RuntimeError("model unavailable")

    st = _state(vid, "invoice-noembed", _pricing(_line("100", "120")))
    rep = check_anomalies(st, c, embed=boom, explain=Explainer())
    assert rep.similar_invoices == [] and "PRICE_OVER_TOLERANCE" in {f.code for f in rep.flags}


# ----------------------------------------------------- ground-truth dev invoices (seeded GEN data)
GROUND_TRUTH = Path(__file__).resolve().parent.parent / "evals" / "ground_truth.json"


def _dev_entry(outcome: str) -> dict[str, Any]:
    data = json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))
    return next(
        e for e in data["invoices"] if e["split"] == "dev" and e["expected_outcome"] == outcome
    )


@integration[0]
@integration[1]
@pytest.mark.parametrize(
    ("outcome", "code"),
    [("ANOMALY_PRICE", "PRICE_OVER_TOLERANCE"), ("ANOMALY_AMOUNT", "AMOUNT_OUTLIER")],
)
def test_ground_truth_dev_invoices_get_expected_flag(conn: Any, outcome: str, code: str) -> None:
    from ap_agent.models import ExtractedInvoice

    e = _dev_entry(outcome)
    c = conn
    row = c.execute(
        "SELECT id, vendor_id FROM trex.purchase_orders WHERE po_number = %s",
        (e["source_po"],),
    ).fetchone()
    if row is None:
        pytest.skip("GEN demo data is not loaded (run data_gen)")
    po_id, vendor_id = row
    f = e["fields"]
    inv = ExtractedInvoice.model_validate(
        {**f, "line_items": [
            {"part_no": ln["part_no"], "description": ln["description"],
             "quantity": ln["qty"], "unit_price": ln["unit_price"],
             "line_total": ln["line_total"]} for ln in f["line_items"]
        ]}
    )  # fmt: skip
    pricing = price_variance(inv.line_items, load_po_lines(c, po_id))
    state = {
        "thread_id": f"invoice-gt-{e['id']}", "vendor": {"vendor_id": vendor_id},
        "extraction": inv.model_dump(mode="json"), "po": PO,
        "email_meta": {"received_at": e["received"]},
        "pricing": pricing.model_dump(mode="json"),
    }  # fmt: skip
    rep = check_anomalies(state, c, embed=_fake_embed, explain=Explainer())
    assert code in {fl.code for fl in rep.flags}
    assert rep.explanation == "Mocked explanation."
