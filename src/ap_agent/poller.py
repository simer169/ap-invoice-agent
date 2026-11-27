"""Poller: wakes parked invoices whose PO is now fully received, and raises due/discount alerts.

Plain code + SQL, no LLM. It never pays anything: a woken thread ends at the human-approval pause.

    uv run python -m ap_agent.poller --once
    uv run python -m ap_agent.poller --every 60      # demo mode, APScheduler
"""

import argparse
import logging
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
from langgraph.types import Command
from psycopg.types.json import Jsonb

from ap_agent.config import get_database_url
from ap_agent.graph.runner import config_for
from ap_agent.notify import Kind, NotifyEvent, notify

log = logging.getLogger(__name__)

ACTOR = "poller"
NEAR_DUE_DAYS = 7
DISCOUNT_DAYS = 3
NEAR_DUE_ACTION = "NEAR_DUE_ALERT"
DISCOUNT_ACTION = "DISCOUNT_ALERT"

Notifier = Callable[[psycopg.Connection[Any], NotifyEvent], bool]

_COLUMNS = (
    "p.thread_id, v.name, p.invoice_number, p.po_number, p.currency, p.total, p.due_date, "
    "p.discount_deadline"
)
_FROM = "FROM agent.parked_invoices p LEFT JOIN trex.vendors v ON v.id = p.vendor_id"

# The one discovery query: WAITING invoices whose PO the receiving simulator/TREX has completed.
_RECEIVED_SQL = f"""
SELECT {_COLUMNS}
{_FROM}
JOIN trex.po_receipt_status s ON s.po_number = p.po_number
WHERE p.status = 'WAITING' AND s.fully_received
  AND (p.vendor_id IS NULL OR s.vendor_id = p.vendor_id)
ORDER BY p.parked_at, p.id
"""

# Alerts are once per thread per day: the audit row records the day it was sent for.
_NEAR_DUE_SQL = f"""
SELECT {_COLUMNS}
{_FROM}
WHERE p.status = 'WAITING' AND p.due_date IS NOT NULL AND p.due_date <= %(limit)s
  AND NOT EXISTS (
      SELECT 1 FROM agent.audit_log a
      WHERE a.action = %(action)s AND a.ref = p.thread_id AND a.details ->> 'day' = %(day)s)
ORDER BY p.due_date, p.id
"""

_DISCOUNT_SQL = f"""
SELECT {_COLUMNS}
{_FROM}
WHERE p.status IN ('READY', 'WAITING') AND p.discount_deadline IS NOT NULL
  AND p.discount_deadline BETWEEN %(today)s AND %(limit)s
  AND NOT EXISTS (
      SELECT 1 FROM agent.audit_log a
      WHERE a.action = %(action)s AND a.ref = p.thread_id AND a.details ->> 'day' = %(day)s)
ORDER BY p.discount_deadline, p.id
"""

_ANOMALY_PREFIX = re.compile(r"^Anomaly \S+ \(\w+\): ")


@dataclass
class RunCounts:
    resumed: int = 0
    near_due_alerts: int = 0
    discount_alerts: int = 0
    errors: int = 0


@dataclass(frozen=True)
class ParkedRow:
    thread_id: str
    vendor: str | None
    invoice_number: str | None
    po_number: str | None
    currency: str | None
    total: Decimal | None
    due_date: date | None
    discount_deadline: date | None

    def event(self, kind: Kind, today: date, warnings: list[str] | None = None) -> NotifyEvent:
        return NotifyEvent(
            kind=kind,
            thread_id=self.thread_id,
            vendor=self.vendor,
            invoice_number=self.invoice_number,
            po_number=self.po_number,
            currency=self.currency,
            total=self.total,
            due_date=self.due_date,
            discount_deadline=self.discount_deadline,
            warnings=warnings or [],
            today=today,
        )


def _rows(
    conn: psycopg.Connection[Any], sql: str, params: dict[str, Any] | None = None
) -> list[ParkedRow]:
    return [ParkedRow(*r) for r in conn.execute(sql, params).fetchall()]  # type: ignore[arg-type]


def find_received(conn: psycopg.Connection[Any]) -> list[ParkedRow]:
    """WAITING parked invoices whose PO is fully received (one SQL query)."""
    return _rows(conn, _RECEIVED_SQL)


def find_near_due(conn: psycopg.Connection[Any], today: date) -> list[ParkedRow]:
    return _rows(
        conn,
        _NEAR_DUE_SQL,
        {
            "limit": today + timedelta(days=NEAR_DUE_DAYS),
            "action": NEAR_DUE_ACTION,
            "day": today.isoformat(),
        },
    )


def find_discount_expiring(conn: psycopg.Connection[Any], today: date) -> list[ParkedRow]:
    return _rows(
        conn,
        _DISCOUNT_SQL,
        {
            "today": today,
            "limit": today + timedelta(days=DISCOUNT_DAYS),
            "action": DISCOUNT_ACTION,
            "day": today.isoformat(),
        },
    )


def _audit(conn: psycopg.Connection[Any], action: str, ref: str, details: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO agent.audit_log (actor, action, ref, details) VALUES (%s, %s, %s, %s)",
        (ACTOR, action, ref, Jsonb(details)),
    )


def _warnings(graph: Any, thread_id: str) -> list[str]:
    values = graph.get_state(config_for(thread_id)).values or {}
    return [_ANOMALY_PREFIX.sub("", str(r)) for r in values.get("reasons") or []]


def _resume_one(
    graph: Any, conn: psycopg.Connection[Any], row: ParkedRow, now: datetime, notifier: Notifier
) -> bool:
    """Resume one thread. True when it left WAITING (and the user was notified)."""
    payload = {"event": "po_received", "at": now.isoformat()}
    graph.invoke(Command(resume=payload), config_for(row.thread_id))
    values = graph.get_state(config_for(row.thread_id)).values or {}
    if values.get("status") == "WAITING":  # receipt changed under us: parked again, nothing to say
        return False
    try:
        notifier(conn, row.event("PO_RECEIVED", now.date(), _warnings(graph, row.thread_id)))
    except Exception:
        log.exception("notification failed for %s", row.thread_id)
    return True


def _alert(
    conn: psycopg.Connection[Any],
    row: ParkedRow,
    kind: Kind,
    action: str,
    today: date,
    notifier: Notifier,
) -> None:
    with conn.transaction():  # notification row and the once-per-day marker land together
        notifier(conn, row.event(kind, today))
        _audit(conn, action, row.thread_id, {"day": today.isoformat()})


def run_once(
    graph: Any,
    conn: psycopg.Connection[Any],
    *,
    now: datetime | None = None,
    notifier: Notifier = notify,
) -> RunCounts:
    """One poll. `conn` must be autocommit. One failing thread/alert never stops the others."""
    now = now or datetime.now(UTC)
    today = now.date()
    counts = RunCounts()

    for row in find_received(conn):
        try:
            if _resume_one(graph, conn, row, now, notifier):
                counts.resumed += 1
        except Exception as exc:  # noqa: BLE001
            counts.errors += 1
            log.warning("resume failed for %s: %s", row.thread_id, type(exc).__name__)
            _best_effort_error_audit(conn, row.thread_id, exc)

    for finder, kind, action in (
        (find_near_due, "NEAR_DUE", NEAR_DUE_ACTION),
        (find_discount_expiring, "DISCOUNT_EXPIRING", DISCOUNT_ACTION),
    ):
        for row in finder(conn, today):  # type: ignore[operator]
            try:
                _alert(conn, row, kind, action, today, notifier)  # type: ignore[arg-type]
            except Exception as exc:  # noqa: BLE001
                counts.errors += 1
                log.warning("%s failed for %s: %s", action, row.thread_id, type(exc).__name__)
                continue
            if kind == "NEAR_DUE":
                counts.near_due_alerts += 1
            else:
                counts.discount_alerts += 1
    return counts


def _best_effort_error_audit(conn: psycopg.Connection[Any], thread_id: str, exc: Exception) -> None:
    try:
        _audit(conn, "POLL_ERROR", thread_id, {"error": f"{type(exc).__name__}: {str(exc)[:200]}"})
    except Exception:  # noqa: BLE001
        log.warning("could not write POLL_ERROR audit for %s", thread_id)


def _format_counts(c: RunCounts) -> str:
    return (
        f"resumed={c.resumed} near_due_alerts={c.near_due_alerts} "
        f"discount_alerts={c.discount_alerts} errors={c.errors}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wake parked invoices and send alerts.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="poll one time and exit")
    mode.add_argument("--every", type=int, metavar="SECONDS", help="poll repeatedly (demo mode)")
    args = parser.parse_args(argv)
    if args.every is not None and args.every < 1:
        parser.error("--every must be at least 1 second")

    url = get_database_url()
    if not url:
        print("FAIL: DATABASE_URL is not set (see .env.example).")
        return 1

    from ap_agent.graph.checkpointer import make_checkpointer, make_pool
    from ap_agent.graph.deps import prod_deps
    from ap_agent.graph.graph import build_graph

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    pool = make_pool(url)
    try:
        graph = build_graph(prod_deps(url), make_checkpointer(pool))

        def tick() -> None:
            try:
                with psycopg.connect(url, autocommit=True, connect_timeout=10) as conn:
                    print(_format_counts(run_once(graph, conn)), flush=True)
            except Exception as exc:  # noqa: BLE001 - keep the schedule alive
                log.warning("poll failed: %s", type(exc).__name__)

        if args.once:
            with psycopg.connect(url, autocommit=True, connect_timeout=10) as conn:
                counts = run_once(graph, conn)
            print(_format_counts(counts))
            return 1 if counts.errors else 0

        from apscheduler.schedulers.blocking import BlockingScheduler

        scheduler = BlockingScheduler()
        scheduler.add_job(
            tick,
            "interval",
            seconds=args.every,
            max_instances=1,
            coalesce=True,
            next_run_time=datetime.now(UTC),
        )
        print(f"polling every {args.every}s - Ctrl+C to stop")
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            pass
        return 0
    except psycopg.Error as exc:
        print(f"FAIL poller: {type(exc).__name__}: {str(exc).splitlines()[0]}")
        return 1
    finally:
        pool.close()


if __name__ == "__main__":
    sys.exit(main())
