"""Email watcher (Agent 1). Plain code, no LLM.

For each new email: keep PDF attachments, store them in S3, skip content we have already
seen, and request an invoice run for new content. Every email is recorded exactly once in
agent.processed_emails so it is never reprocessed.
"""

import logging
from collections import Counter
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ap_agent.mail.sources import MailMessage, MailSource, pdf_attachments
from ap_agent.mail.storage import S3Storage, sha256_hex

log = logging.getLogger(__name__)

PROCESSED = "PROCESSED"
DUPLICATE_CONTENT = "DUPLICATE_CONTENT"
IGNORED = "IGNORED"
ERROR = "ERROR"
SKIPPED = "SKIPPED"
OUTCOMES = (PROCESSED, DUPLICATE_CONTENT, IGNORED, ERROR, SKIPPED)

ACTOR = "email_watcher"


def thread_id_for(content_hash: str) -> str:
    return f"invoice-{content_hash}"


def start_invoice_run(
    conn: psycopg.Connection, content_hash: str, s3_key: str, email_meta: dict[str, Any]
) -> str:
    """Placeholder for the LangGraph run (Phase 6): records RUN_REQUESTED in the audit log."""
    thread_id = thread_id_for(content_hash)
    exists = conn.execute(
        "SELECT 1 FROM agent.audit_log WHERE action = 'RUN_REQUESTED' AND ref = %s", (thread_id,)
    ).fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO agent.audit_log (actor, action, ref, details) "
            "VALUES (%s, 'RUN_REQUESTED', %s, %s)",
            (ACTOR, thread_id, Jsonb({"s3_key": s3_key, "email": email_meta})),
        )
    log.info("run requested thread_id=%s s3_key=%s", thread_id, s3_key)
    return thread_id


def _hash_seen(conn: psycopg.Connection, content_hash: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM agent.processed_emails WHERE content_hash = %s "
        "UNION ALL "
        "SELECT 1 FROM agent.audit_log WHERE action = 'RUN_REQUESTED' AND ref = %s LIMIT 1",
        (content_hash, thread_id_for(content_hash)),
    ).fetchone()
    return row is not None


def _already_recorded(conn: psycopg.Connection, message_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM agent.processed_emails WHERE message_id = %s", (message_id,)
        ).fetchone()
        is not None
    )


def _record(
    conn: psycopg.Connection,
    msg: MailMessage,
    outcome: str,
    content_hash: str | None = None,
    s3_key: str | None = None,
    error_message: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO agent.processed_emails "
        "(message_id, received_at, from_address, subject, outcome, content_hash, s3_key, "
        "error_message) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (message_id) DO NOTHING",
        (
            msg.message_id,
            msg.received_at,
            msg.from_address,
            msg.subject,
            outcome,
            content_hash,
            s3_key,
            error_message,
        ),
    )


def _process(conn: psycopg.Connection, msg: MailMessage, storage: S3Storage) -> str:
    """Handle one email inside the caller's transaction. Returns the email's outcome."""
    pdfs = pdf_attachments(msg)
    if not pdfs:
        _record(conn, msg, IGNORED)
        return IGNORED

    meta = {
        "message_id": msg.message_id,
        "from": msg.from_address,
        "subject": msg.subject,
        "received_at": msg.received_at.isoformat(),
    }
    started: list[tuple[str, str]] = []  # (hash, key) of PDFs that started a run
    first: tuple[str, str] | None = None
    for att in pdfs:
        if not att.data.startswith(b"%PDF-"):
            raise ValueError(f"attachment {att.name!r} is not a valid PDF")
        content_hash = sha256_hex(att.data)
        s3_key = storage.upload_pdf(att.data, msg.received_at)
        first = first or (content_hash, s3_key)
        if _hash_seen(conn, content_hash):
            continue
        start_invoice_run(conn, content_hash, s3_key, meta)
        started.append((content_hash, s3_key))

    if started:
        _record(conn, msg, PROCESSED, *started[0])
        return PROCESSED
    assert first is not None
    _record(conn, msg, DUPLICATE_CONTENT, *first)
    return DUPLICATE_CONTENT


def run_once(source: MailSource, conn: psycopg.Connection, storage: S3Storage) -> Counter[str]:
    """Process every message the source offers; returns counts per outcome."""
    counts: Counter[str] = Counter({o: 0 for o in OUTCOMES})
    try:
        for msg in source.fetch():
            if _already_recorded(conn, msg.message_id):
                counts[SKIPPED] += 1
                _mark_seen(source, msg)
                continue
            try:
                with conn.transaction():  # savepoint if the caller holds a transaction
                    outcome = _process(conn, msg, storage)
            except Exception as exc:
                log.exception("failed to process %s", msg.message_id)
                with conn.transaction():
                    _record(conn, msg, ERROR, error_message=f"{type(exc).__name__}: {exc}"[:500])
                outcome = ERROR
            counts[outcome] += 1
            _mark_seen(source, msg)
    finally:
        source.close()
    return counts


def _mark_seen(source: MailSource, msg: MailMessage) -> None:
    try:
        source.mark_seen(msg.message_id)
    except Exception:
        log.exception("could not mark %s as seen", msg.message_id)
