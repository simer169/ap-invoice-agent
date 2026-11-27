"""Notifications: always an agent.notifications row (the in-app inbox), plus Slack when configured.

Plain code, no LLM. Slack problems are logged and never raised.
"""

import logging
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any, Literal

import httpx
import psycopg
from pydantic import BaseModel, Field

from ap_agent.config import get_slack_webhook_url

log = logging.getLogger(__name__)

Kind = Literal["PO_RECEIVED", "NEAR_DUE", "DISCOUNT_EXPIRING"]
SLACK_TIMEOUT_S = 10.0

_HEADLINE: dict[str, str] = {
    "PO_RECEIVED": "ready for review",
    "NEAR_DUE": "due soon",
    "DISCOUNT_EXPIRING": "discount expiring",
}


class NotifyEvent(BaseModel):
    kind: Kind
    thread_id: str
    vendor: str | None = None
    invoice_number: str | None = None
    po_number: str | None = None
    currency: str | None = None
    total: Decimal | None = None
    due_date: date | None = None
    discount_deadline: date | None = None
    warnings: list[str] = Field(default_factory=list)
    today: date


def _days_phrase(target: date, today: date, label: str) -> str:
    n = (target - today).days
    when = f"{target.strftime('%b')} {target.day}"
    if n < 0:
        return f"{label} {when} (overdue by {-n} day{'' if n == -1 else 's'})"
    if n == 0:
        return f"{label} {when} (today)"
    return f"{label} {when} ({n} day{'' if n == 1 else 's'})"


def _warnings_phrase(warnings: list[str]) -> str:
    n = len(warnings)
    if n == 0:
        return "no warnings"
    return f"{n} warning{'' if n == 1 else 's'}: " + "; ".join(warnings)


def format_message(event: NotifyEvent) -> tuple[str, str]:
    """(title, body). Example body:
    Invoice INV-10042 from Acme - $4,200.00 CAD - PO 8821 fully received - due Dec 5 (10 days)
    - 1 warning: price 4% over PO
    """
    number = event.invoice_number or "(no number)"
    parts = [f"Invoice {number} from {event.vendor or 'unknown vendor'}"]
    if event.total is not None:
        parts.append(f"${event.total:,.2f} {event.currency or ''}".rstrip())
    if event.po_number:
        parts.append(
            f"PO {event.po_number} fully received"
            if event.kind == "PO_RECEIVED"
            else f"PO {event.po_number}"
        )
    if event.due_date:
        parts.append(_days_phrase(event.due_date, event.today, "due"))
    if event.kind == "DISCOUNT_EXPIRING" and event.discount_deadline:
        parts.append(_days_phrase(event.discount_deadline, event.today, "discount ends"))
    parts.append(_warnings_phrase(event.warnings))
    title = f"Invoice {number} {_HEADLINE[event.kind]}"
    return title, " - ".join(parts)


Poster = Callable[[str, str], None]


def post_slack(url: str, text: str) -> None:
    """POST to a Slack incoming webhook. Raises on failure; `notify` catches it."""
    httpx.post(url, json={"text": text}, timeout=SLACK_TIMEOUT_S).raise_for_status()


def notify(
    conn: psycopg.Connection[Any],
    event: NotifyEvent,
    *,
    post: Poster = post_slack,
    webhook_url: Callable[[], str | None] = get_slack_webhook_url,
) -> bool:
    """Store the notification, then post to Slack if configured.

    Returns False when the row already existed (a repeated PO_RECEIVED), True otherwise. Slack
    errors never propagate; the webhook URL is never logged.
    """
    title, body = format_message(event)
    row = conn.execute(
        "INSERT INTO agent.notifications (thread_id, kind, title, body) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id",
        (event.thread_id, event.kind, title, body),
    ).fetchone()
    if row is None:
        return False
    url = webhook_url()
    if url:
        try:
            post(url, f"{title}\n{body}")
        except Exception as exc:  # noqa: BLE001 - Slack must never break the run
            log.warning("Slack post failed for %s: %s", event.thread_id, type(exc).__name__)
    return True
