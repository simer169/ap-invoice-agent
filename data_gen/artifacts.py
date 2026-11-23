"""Write invoice PDFs, .eml files and evals/ground_truth.json from a Dataset (no DB needed)."""

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from data_gen.emails import invoice_email, non_invoice_emails
from data_gen.generate import DEMO_TODAY, Dataset
from data_gen.pdfs import render_invoice, scan
from data_gen.scenarios import BILL_TO, InvoiceSpec, plan

ROOT = Path(__file__).resolve().parent.parent
PDF_DIR = ROOT / "sample_inbox" / "pdfs"
EML_DIR = ROOT / "sample_inbox" / "eml"
TRUTH_PATH = ROOT / "evals" / "ground_truth.json"


def _clear_generated(directory: Path, pattern: str) -> None:
    """Remove only files this generator wrote earlier (gen_*); leave everything else alone."""
    directory.mkdir(parents=True, exist_ok=True)
    for p in directory.glob(pattern):
        p.unlink()


def _truth(spec: InvoiceSpec, by_key: dict[int, InvoiceSpec], sha: str) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": spec.id,
        "split": spec.split,
        "scenario": spec.scenario,
        "underlying_scenario": spec.underlying_scenario,
        "expected_outcome": spec.expected_outcome,
        "ready_day": spec.ready_day.isoformat() if spec.ready_day else None,
        "scanned": spec.scanned,
        "pdf_file": spec.pdf_file,
        "pdf_sha256": sha,
        "eml_file": spec.eml_file,
        "attachment_name": spec.attachment_name,
        "message_id": spec.message_id,
        "received": spec.email_dt.isoformat(),
        "source_po": spec.source_po or spec.po_number if spec.vendor.known else None,
        "duplicate_of": by_key[spec.duplicate_of].id if spec.duplicate_of else None,
        "reissue_of": by_key[spec.reissue_of].id if spec.reissue_of else None,
        "reissue_change": spec.reissue_change,
        "anomaly": spec.anomaly,
        "fields": {
            "vendor_name": spec.vendor.name,
            "invoice_number": spec.invoice_number,
            "invoice_date": spec.invoice_date.isoformat(),
            "po_number": spec.po_number,
            "currency": spec.vendor.currency,
            "payment_terms": spec.vendor.payment_terms,
            "bill_to": BILL_TO,
            "subtotal": str(spec.subtotal),
            "tax": str(spec.tax),
            "total": str(spec.total),
            "line_items": [
                {
                    "part_no": ln.part_no,
                    "description": ln.description,
                    "qty": ln.qty,
                    "unit_price": str(ln.unit_price),
                    "line_total": str(ln.line_total),
                }
                for ln in spec.lines
            ],
        },
    }
    return entry


def build_artifacts(
    ds: Dataset,
    seed: int,
    pdf_dir: Path = PDF_DIR,
    eml_dir: Path = EML_DIR,
    truth_path: Path = TRUTH_PATH,
) -> list[InvoiceSpec]:
    """Regenerate every invoice artifact. Same seed -> byte-identical ground_truth.json."""
    specs = plan(ds, seed)
    by_key = {s.key: s for s in specs}
    _clear_generated(pdf_dir, "gen_*.pdf")
    _clear_generated(eml_dir, "gen_*.eml")
    truth_path.parent.mkdir(parents=True, exist_ok=True)

    pdfs: dict[int, bytes] = {}
    for s in specs:  # sorted by email time: originals always precede their duplicates
        if s.duplicate_of is not None:
            pdfs[s.key] = pdfs[s.duplicate_of]  # same bytes, second email
            continue
        data = render_invoice(s)
        pdfs[s.key] = scan(data, f"{seed}-scan-{s.id}") if s.scanned else data

    entries = []
    for s in specs:
        (pdf_dir / s.pdf_file).write_bytes(pdfs[s.key])
        (eml_dir / s.eml_file).write_bytes(invoice_email(s, pdfs[s.key]))
        entries.append(_truth(s, by_key, hashlib.sha256(pdfs[s.key]).hexdigest()))

    non_invoice = []
    for meta, raw in non_invoice_emails(ds, seed):
        (eml_dir / meta.eml_file).write_bytes(raw)
        non_invoice.append(meta.model_dump())

    truth = {
        "seed": seed,
        "demo_today": DEMO_TODAY.isoformat(),
        "invoices": entries,
        "non_invoice_emails": non_invoice,
    }
    truth_path.write_text(
        json.dumps(truth, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    return specs


def summary(specs: list[InvoiceSpec]) -> dict[str, int]:
    c = Counter(s.scenario for s in specs)
    return dict(sorted(c.items()))
