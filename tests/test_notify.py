"""notify: message formatting (pure) and the never-crash Slack behaviour (fake connection)."""

import logging
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest

from ap_agent.notify import NotifyEvent, format_message, notify, post_slack

TODAY = date(2026, 11, 25)


def _event(**over: Any) -> NotifyEvent:
    base: dict[str, Any] = {
        "kind": "PO_RECEIVED",
        "thread_id": "invoice-abc",
        "vendor": "Acme Ltd",
        "invoice_number": "INV-10042",
        "po_number": "8821",
        "currency": "CAD",
        "total": Decimal(4200),
        "due_date": date(2026, 12, 5),
        "warnings": ["price 4% over PO"],
        "today": TODAY,
    }
    return NotifyEvent(**{**base, **over})


def test_po_received_message_matches_the_spec_example() -> None:
    title, body = format_message(_event())
    assert title == "Invoice INV-10042 ready for review"
    assert body == (
        "Invoice INV-10042 from Acme Ltd - $4,200.00 CAD - PO 8821 fully received - "
        "due Dec 5 (10 days) - 1 warning: price 4% over PO"
    )


def test_warning_plural_and_none() -> None:
    assert format_message(_event(warnings=["a", "b"]))[1].endswith("2 warnings: a; b")
    assert format_message(_event(warnings=[]))[1].endswith("no warnings")


def test_overdue_today_and_singular_day() -> None:
    assert (
        "due Nov 22 (overdue by 3 days)" in format_message(_event(due_date=date(2026, 11, 22)))[1]
    )
    assert "due Nov 24 (overdue by 1 day)" in format_message(_event(due_date=date(2026, 11, 24)))[1]
    assert "due Nov 25 (today)" in format_message(_event(due_date=TODAY))[1]
    assert "due Nov 26 (1 day)" in format_message(_event(due_date=date(2026, 11, 26)))[1]


def test_missing_fields_are_left_out() -> None:
    _, body = format_message(
        _event(
            vendor=None, invoice_number=None, po_number=None, total=None, due_date=None, warnings=[]
        )
    )
    assert body == "Invoice (no number) from unknown vendor - no warnings"


def test_discount_and_near_due_kinds() -> None:
    title, body = format_message(
        _event(kind="DISCOUNT_EXPIRING", discount_deadline=date(2026, 11, 27))
    )
    assert title == "Invoice INV-10042 discount expiring"
    assert "discount ends Nov 27 (2 days)" in body
    assert "fully received" not in body  # only the PO_RECEIVED wording says that
    assert format_message(_event(kind="NEAR_DUE"))[0] == "Invoice INV-10042 due soon"


class _FakeConn:
    """Stands in for a psycopg connection: records the INSERT, returns an id or nothing."""

    def __init__(self, inserted: bool = True) -> None:
        self.inserted = inserted
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: tuple[Any, ...]) -> "_FakeConn":
        self.executed.append((sql, params))
        return self

    def fetchone(self) -> tuple[int] | None:
        return (1,) if self.inserted else None


def test_always_stores_a_row_and_skips_slack_without_webhook() -> None:
    conn = _FakeConn()
    posted: list[str] = []
    ok = notify(conn, _event(), post=lambda u, t: posted.append(t), webhook_url=lambda: None)  # type: ignore[arg-type]
    assert ok and posted == []
    sql, params = conn.executed[0]
    assert "agent.notifications" in sql
    assert params[:2] == ("invoice-abc", "PO_RECEIVED")


def test_posts_to_slack_when_configured() -> None:
    posted: list[tuple[str, str]] = []
    notify(
        _FakeConn(),  # type: ignore[arg-type]
        _event(),
        post=lambda u, t: posted.append((u, t)),
        webhook_url=lambda: "https://hooks.example/fake",
    )
    assert len(posted) == 1 and posted[0][0] == "https://hooks.example/fake"
    assert "INV-10042" in posted[0][1]


def test_slack_failure_is_logged_not_raised_and_does_not_leak_the_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "https://hooks.example/SECRET-TOKEN"

    def boom(url: str, text: str) -> None:
        raise httpx.ConnectTimeout(f"timed out talking to {url}")

    conn = _FakeConn()
    with caplog.at_level(logging.WARNING):
        ok = notify(conn, _event(), post=boom, webhook_url=lambda: secret)  # type: ignore[arg-type]
    assert ok is True and len(conn.executed) == 1  # the inbox row is already written
    assert "Slack post failed" in caplog.text
    assert "SECRET-TOKEN" not in caplog.text


def test_http_error_from_slack_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, **kw: Any) -> httpx.Response:
        assert kw["timeout"] == 10.0
        return httpx.Response(500, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(httpx.HTTPStatusError):
        post_slack("https://hooks.example/x", "hi")  # the raw poster raises...
    notify(_FakeConn(), _event(), webhook_url=lambda: "https://hooks.example/x")  # type: ignore[arg-type]  # ...notify does not


def test_repeated_po_received_is_not_announced_again() -> None:
    posted: list[str] = []
    conn = _FakeConn(inserted=False)  # unique index hit -> ON CONFLICT DO NOTHING
    ok = notify(conn, _event(), post=lambda u, t: posted.append(t), webhook_url=lambda: "u")  # type: ignore[arg-type]
    assert ok is False and posted == []
