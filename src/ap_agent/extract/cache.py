"""Extraction cache: never pay twice for the same PDF."""

from datetime import date
from typing import Any

import instructor
import psycopg
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from ap_agent.config import get_settings
from ap_agent.extract.normalize import DateOrder
from ap_agent.extract.reader import extract_invoice
from ap_agent.models import ExtractionResult, InvoiceExtraction

_SELECT = """
SELECT model, method, extracted_json, tokens_in, tokens_out, cost_usd, retries, duration_ms
FROM agent.extractions WHERE content_hash = %s
"""

# One row per PDF hash; re-extracting with a different model replaces the row.
_UPSERT = """
INSERT INTO agent.extractions
    (content_hash, model, method, extracted_json, tokens_in, tokens_out, cost_usd,
     retries, duration_ms)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (content_hash) DO UPDATE SET
    model = EXCLUDED.model, method = EXCLUDED.method, extracted_json = EXCLUDED.extracted_json,
    tokens_in = EXCLUDED.tokens_in, tokens_out = EXCLUDED.tokens_out,
    cost_usd = EXCLUDED.cost_usd, retries = EXCLUDED.retries,
    duration_ms = EXCLUDED.duration_ms, created_at = now()
"""


def get_cached(
    conn: psycopg.Connection[Any],
    content_hash: str,
    model: str,
    received_date: date | None = None,
    date_order: DateOrder = "DMY",
) -> ExtractionResult | None:
    """Cached extraction for this hash and model, re-normalised with the current date rules.

    The row stores the raw LLM output, so changing DATE_ORDER never needs a paid re-extraction.
    A row that no longer validates against the current schema counts as a miss.
    """
    row = conn.execute(_SELECT, (content_hash,)).fetchone()
    if row is None or row[0] != model:
        return None
    try:
        raw = InvoiceExtraction.model_validate(row[2])
    except ValidationError:
        return None
    invoice, ambiguous = raw.to_invoice(received_date=received_date, date_order=date_order)
    return ExtractionResult(
        invoice=invoice,
        raw=raw,
        date_ambiguous=ambiguous,
        method=row[1],
        model=row[0],
        tokens_in=row[3],
        tokens_out=row[4],
        cost_usd=float(row[5]) if row[5] is not None else None,
        retries=row[6],
        duration_ms=row[7],
        cached=True,
    )


def store(conn: psycopg.Connection[Any], content_hash: str, result: ExtractionResult) -> None:
    conn.execute(
        _UPSERT,
        (
            content_hash,
            result.model,
            result.method,
            Jsonb(result.raw.model_dump(mode="json")),
            result.tokens_in,
            result.tokens_out,
            result.cost_usd,
            result.retries,
            result.duration_ms,
        ),
    )


def extract_and_cache(
    conn: psycopg.Connection[Any],
    content_hash: str,
    pdf_bytes: bytes,
    client: instructor.Instructor | None = None,
    received_date: date | None = None,
    refresh: bool = False,
) -> ExtractionResult:
    """Return the cached extraction for this hash and model, else extract and store it.

    refresh=True skips the lookup and re-extracts (overwrites the row).
    """
    settings = get_settings()
    model, date_order = settings.EXTRACT_MODEL, settings.DATE_ORDER
    if not refresh:
        cached = get_cached(conn, content_hash, model, received_date, date_order)
        if cached is not None:
            return cached
    result = extract_invoice(
        pdf_bytes, client=client, model=model, received_date=received_date, date_order=date_order
    )
    store(conn, content_hash, result)
    return result
