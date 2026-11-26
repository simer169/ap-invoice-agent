"""Tests for the invoice PDFs / emails / ground truth generator (no database needed)."""

import email
import json
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from email import policy
from pathlib import Path

import pymupdf
import pytest

from data_gen.artifacts import build_artifacts
from data_gen.generate import Dataset, generate
from data_gen.scenarios import COUNTS, OUTCOMES


@dataclass
class Out:
    root: Path
    pdfs: Path
    emls: Path
    truth: Path
    data: dict


def _run(ds: Dataset, seed: int, root: Path) -> Out:
    pdfs, emls, truth = root / "pdfs", root / "eml", root / "out" / "ground_truth.json"
    build_artifacts(ds, seed, pdfs, emls, truth)
    return Out(root, pdfs, emls, truth, json.loads(truth.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def ds() -> Dataset:
    return generate(42)


@pytest.fixture(scope="module")
def out(ds: Dataset, tmp_path_factory: pytest.TempPathFactory) -> Out:
    return _run(ds, 42, tmp_path_factory.mktemp("a"))


def _inv(out: Out) -> list[dict]:
    return out.data["invoices"]


def test_ground_truth_is_byte_identical_for_same_seed(
    ds: Dataset, out: Out, tmp_path: Path
) -> None:
    again = _run(ds, 42, tmp_path)
    assert again.truth.read_bytes() == out.truth.read_bytes()
    # every artifact is byte-stable too, not just the answer key
    for p in sorted(out.pdfs.iterdir()):
        assert (again.pdfs / p.name).read_bytes() == p.read_bytes()
    for p in sorted(out.emls.iterdir()):
        assert (again.emls / p.name).read_bytes() == p.read_bytes()


def test_different_seed_differs(ds: Dataset, out: Out, tmp_path: Path) -> None:
    other = _run(generate(7), 7, tmp_path)
    assert other.truth.read_bytes() != out.truth.read_bytes()


def test_scenario_counts_match_list(out: Out) -> None:
    got = Counter(i["scenario"] for i in _inv(out))
    assert got == COUNTS
    assert sum(COUNTS.values()) == len(_inv(out)) == 60
    for i in _inv(out):
        base = i["underlying_scenario"] or i["scenario"]
        assert i["expected_outcome"] == OUTCOMES[base]
        assert i["scanned"] == (i["scenario"] == "scanned")
    assert Counter(i["expected_outcome"] for i in _inv(out)).get("READY") == 14 + 1


def test_every_referenced_file_exists(out: Out) -> None:
    for i in _inv(out):
        assert (out.pdfs / i["pdf_file"]).is_file()
        assert (out.emls / i["eml_file"]).is_file()
    for n in out.data["non_invoice_emails"]:
        assert (out.emls / n["eml_file"]).is_file()
    assert len(list(out.pdfs.glob("*.pdf"))) == 60
    assert len(list(out.emls.glob("*.eml"))) == 65


def test_split(out: Out) -> None:
    split = Counter(i["split"] for i in _inv(out))
    assert split == {"dev": 10, "eval": 50}
    dev = [i for i in _inv(out) if i["split"] == "dev"]
    assert {i["scenario"] for i in dev} >= set(OUTCOMES)
    assert any(i["scanned"] for i in dev)


def test_emails(out: Out) -> None:
    ids = []
    for i in _inv(out):
        msg = email.message_from_bytes(
            (out.emls / i["eml_file"]).read_bytes(), policy=policy.default
        )
        ids.append(msg["Message-ID"])
        assert msg["Message-ID"] == i["message_id"]
        assert str(msg["From"]).split("@")[-1].rstrip(">").endswith(".example")
        assert date(2026, 11, 1) <= msg["Date"].datetime.date() <= date(2026, 11, 5)
        pdfs = [a for a in msg.iter_attachments() if a.get_content_type() == "application/pdf"]
        assert len(pdfs) == 1
        assert pdfs[0].get_content() == (out.pdfs / i["pdf_file"]).read_bytes()
        assert (
            i["fields"]["invoice_number"] in msg["Subject"]
            or i["fields"]["po_number"] in msg["Subject"]
        )
    for n in out.data["non_invoice_emails"]:
        msg = email.message_from_bytes(
            (out.emls / n["eml_file"]).read_bytes(), policy=policy.default
        )
        ids.append(msg["Message-ID"])
        assert not list(msg.iter_attachments())
    assert len(ids) == len(set(ids)) == 65


def test_duplicates_and_reissues(out: Out) -> None:
    by_id = {i["id"]: i for i in _inv(out)}
    dups = [i for i in _inv(out) if i["scenario"] == "exact_duplicate"]
    assert len(dups) == 5
    for d in dups:
        o = by_id[d["duplicate_of"]]
        assert (out.pdfs / d["pdf_file"]).read_bytes() == (out.pdfs / o["pdf_file"]).read_bytes()
        assert d["message_id"] != o["message_id"] and d["eml_file"] != o["eml_file"]
        assert d["received"] > o["received"]
        assert d["fields"] == o["fields"]
    reissues = [i for i in _inv(out) if i["scenario"] == "reissue"]
    assert len(reissues) == 3
    assert {r["reissue_change"] for r in reissues} == {"date", "layout"}
    for r in reissues:
        o = by_id[r["reissue_of"]]
        assert r["fields"]["invoice_number"] == o["fields"]["invoice_number"]
        assert r["fields"]["vendor_name"] == o["fields"]["vendor_name"]
        assert (out.pdfs / r["pdf_file"]).read_bytes() != (out.pdfs / o["pdf_file"]).read_bytes()
        assert r["received"] > o["received"]
        if r["reissue_change"] == "date":
            assert r["fields"]["invoice_date"] != o["fields"]["invoice_date"]


def test_scanned_pdfs_are_image_only(out: Out) -> None:
    scanned = [i for i in _inv(out) if i["scanned"]]
    assert len(scanned) == 4
    for i in scanned:
        doc = pymupdf.open(out.pdfs / i["pdf_file"])
        assert doc[0].get_text().strip() == ""
        assert len(doc[0].get_images()) == 1
    clean = next(i for i in _inv(out) if i["scenario"] == "clean")
    text = pymupdf.open(out.pdfs / clean["pdf_file"])[0].get_text()
    assert clean["fields"]["invoice_number"] in text


def test_amounts_and_tax(out: Out) -> None:
    for i in _inv(out):
        f = i["fields"]
        lines = f["line_items"]
        for ln in lines:
            assert Decimal(ln["line_total"]) == Decimal(ln["unit_price"]) * ln["qty"]
        assert Decimal(f["subtotal"]) == sum(Decimal(ln["line_total"]) for ln in lines)
        tax = (
            Decimal("0.00")
            if f["currency"] == "USD"
            else (Decimal(f["subtotal"]) * Decimal("0.13")).quantize(Decimal("0.01"))
        )
        assert Decimal(f["tax"]) == tax
        assert Decimal(f["total"]) == Decimal(f["subtotal"]) + tax
        assert f["bill_to"] == "Demo Rail Leasing Co."


def test_scenario_specifics(ds: Dataset, out: Out) -> None:
    pos = {p.po_number: p for p in ds.purchase_orders}
    known = {v.name for v in ds.vendors}
    last_delivery = {
        p.po_number: max(r.received_at.date() for r in p.scheduled)
        for p in pos.values()
        if p.scheduled
    }
    for i in _inv(out):
        base = i["underlying_scenario"] or i["scenario"]
        f = i["fields"]
        if base == "unknown_vendor":
            assert f["vendor_name"] not in known and i["source_po"] is None
        else:
            assert f["vendor_name"] in known
        if base == "no_po":
            assert f["po_number"] is None
            assert pos[i["source_po"]].status == "RECEIVED"
            text = pymupdf.open(out.pdfs / i["pdf_file"])[0].get_text() if not i["scanned"] else ""
            assert "GEN-PO" not in text
        elif base != "unknown_vendor":
            po = pos[f["po_number"]]
            if base in ("clean", "reissue", "exact_duplicate"):
                assert po.status == "RECEIVED"
        if base == "partial_completes":
            po = pos[f["po_number"]]
            assert (
                po.status == "PARTIAL" and i["ready_day"] == last_delivery[po.po_number].isoformat()
            )
            assert date(2026, 11, 6) <= date.fromisoformat(i["ready_day"]) <= date(2026, 11, 30)
        else:
            assert i["ready_day"] is None
        if base == "partial_never":
            po = pos[f["po_number"]]
            assert po.status == "PARTIAL" and not po.scheduled
        if base == "price_above_po":
            po = pos[i["source_po"] or f["po_number"]]
            diffs = [
                (ln["part_no"], Decimal(ln["unit_price"]) / pl.unit_price - 1)
                for ln, pl in zip(f["line_items"], po.lines, strict=True)
                if Decimal(ln["unit_price"]) != pl.unit_price
            ]
            assert len(diffs) == 1
            assert Decimal("0.03") <= diffs[0][1] <= Decimal("0.10")
        if base == "amount_outlier":
            assert Decimal(f["total"]) >= Decimal(i["anomaly"]["vendor_history_max"]) * 3
        if i["scenario"] == "partial_never":
            assert i["expected_outcome"] == "STAYS_WAITING"
        # invoice date never in the future and never before the email is plausible
        assert i["fields"]["invoice_date"] <= i["received"][:10]
