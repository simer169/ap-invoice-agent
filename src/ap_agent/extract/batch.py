"""Batch extraction: extract every started PDF that has no agent.extractions row yet."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import psycopg

from ap_agent.extract.cache import extract_and_cache

CONFIRM_ABOVE = 20
_HASH_PREFIX_LEN = len("invoice-")

# RUN_REQUESTED is written by mail.watcher.start_invoice_run for every PDF that started a run
# (a processed_emails row only keeps the first PDF of an email), so it is the complete list.
_PENDING_SQL = """
SELECT substr(a.ref, %s + 1), a.details ->> 's3_key', a.details -> 'email' ->> 'received_at'
FROM agent.audit_log a
WHERE a.action = 'RUN_REQUESTED' AND a.ref LIKE 'invoice-%%'
  AND NOT EXISTS (
      SELECT 1 FROM agent.extractions e WHERE e.content_hash = substr(a.ref, %s + 1)
  )
ORDER BY a.id
"""


@dataclass(frozen=True)
class PendingPdf:
    content_hash: str
    s3_key: str
    received_at: datetime | None


@dataclass
class BatchSummary:
    pending: int = 0
    extracted: int = 0
    cached: int = 0
    failed: int = 0
    total_cost_usd: float = 0.0
    aborted: bool = False
    errors: list[str] = field(default_factory=list)


def pending_pdfs(conn: psycopg.Connection[Any]) -> list[PendingPdf]:
    rows = conn.execute(_PENDING_SQL, (_HASH_PREFIX_LEN, _HASH_PREFIX_LEN)).fetchall()
    out: list[PendingPdf] = []
    for h, key, received in rows:
        if not key:
            continue
        try:
            when = datetime.fromisoformat(received) if received else None
        except ValueError:
            when = None
        out.append(PendingPdf(h, key, when))
    return out


def run_pending(
    conn: psycopg.Connection[Any],
    read_pdf: Callable[[str], bytes],
    yes: bool = False,
    confirm: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
    extract: Callable[..., Any] = extract_and_cache,
) -> BatchSummary:
    """Extract pending PDFs, committing after each so a crash never repeats paid work."""
    todo = pending_pdfs(conn)
    summary = BatchSummary(pending=len(todo))
    if not todo:
        out("Nothing pending: every started PDF already has an extraction.")
        return summary
    if len(todo) > CONFIRM_ABOVE and not yes:
        answer = confirm(f"{len(todo)} PDFs will be sent to the LLM (costs money). Proceed? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            summary.aborted = True
            out("Aborted.")
            return summary
    for i, p in enumerate(todo, start=1):
        try:
            res = extract(
                conn, p.content_hash, read_pdf(p.s3_key), received_date=_date(p.received_at)
            )
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - one bad PDF must not stop the batch
            conn.rollback()
            summary.failed += 1
            summary.errors.append(f"{p.content_hash[:12]}: {type(exc).__name__}")
            out(f"[{i}/{len(todo)}] FAILED {p.content_hash[:12]}: {type(exc).__name__}")
            continue
        if res.cached:
            summary.cached += 1
        else:
            summary.extracted += 1
            summary.total_cost_usd += res.cost_usd or 0.0
        out(f"[{i}/{len(todo)}] {p.content_hash[:12]} {res.method} cost={res.cost_usd}")
    out(
        f"Done: pending={summary.pending} extracted={summary.extracted} cached={summary.cached} "
        f"failed={summary.failed} total_cost_usd={summary.total_cost_usd:.6f}"
    )
    return summary


def _date(value: datetime | None) -> date | None:
    return value.date() if value else None
