"""run_once orchestration with fake graph/finders: failure isolation and counting (no DB)."""

from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from ap_agent import poller
from ap_agent.notify import NotifyEvent
from ap_agent.poller import ParkedRow, run_once

NOW = datetime(2026, 11, 25, 9, 0, tzinfo=UTC)


def _row(n: int) -> ParkedRow:
    return ParkedRow(f"invoice-{n}", "Acme", f"INV-{n}", "PO-1", "CAD", Decimal(10), None, None)


class _Snap:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values


class FakeGraph:
    def __init__(self, failing: set[str] = frozenset()) -> None:  # type: ignore[assignment]
        self.failing = failing
        self.resumed: list[tuple[str, Any]] = []
        self.status = "READY"

    def invoke(self, command: Any, config: Any) -> None:
        tid = config["configurable"]["thread_id"]
        if tid in self.failing:
            raise RuntimeError("boom")
        self.resumed.append((tid, command.resume))

    def get_state(self, config: Any) -> _Snap:
        return _Snap(
            {"status": self.status, "reasons": ["Anomaly PRICE (block): price 4% over PO"]}
        )


class FakeConn:
    def __init__(self) -> None:
        self.audits: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> "FakeConn":
        self.audits.append(params)
        return self

    @contextmanager
    def transaction(self) -> Any:
        yield


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch) -> FakeConn:
    monkeypatch.setattr(poller, "find_near_due", lambda c, t: [])
    monkeypatch.setattr(poller, "find_discount_expiring", lambda c, t: [])
    return FakeConn()


def test_resumes_every_received_thread_with_the_po_received_event(
    conn: FakeConn, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(poller, "find_received", lambda c: [_row(1), _row(2)])
    g = FakeGraph()
    sent: list[NotifyEvent] = []
    counts = run_once(g, conn, now=NOW, notifier=lambda c, e: sent.append(e) or True)  # type: ignore[arg-type]
    assert counts.resumed == 2 and counts.errors == 0
    assert g.resumed[0] == ("invoice-1", {"event": "po_received", "at": NOW.isoformat()})
    assert [e.kind for e in sent] == ["PO_RECEIVED", "PO_RECEIVED"]
    assert sent[0].warnings == ["price 4% over PO"]  # "Anomaly CODE (sev): " prefix stripped


def test_one_failing_thread_does_not_stop_the_others(
    conn: FakeConn, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(poller, "find_received", lambda c: [_row(1), _row(2), _row(3)])
    g = FakeGraph(failing={"invoice-2"})
    counts = run_once(g, conn, now=NOW, notifier=lambda c, e: True)  # type: ignore[arg-type]
    assert (counts.resumed, counts.errors) == (2, 1)
    assert [t for t, _ in g.resumed] == ["invoice-1", "invoice-3"]
    assert any(p and p[1] == "POLL_ERROR" and p[2] == "invoice-2" for p in conn.audits)


def test_thread_that_parks_again_is_not_counted_or_announced(
    conn: FakeConn, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(poller, "find_received", lambda c: [_row(1)])
    g = FakeGraph()
    g.status = "WAITING"
    sent: list[NotifyEvent] = []
    counts = run_once(g, conn, now=NOW, notifier=lambda c, e: sent.append(e) or True)  # type: ignore[arg-type]
    assert counts.resumed == 0 and sent == []


def test_notifier_failure_does_not_fail_the_resume(
    conn: FakeConn, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(poller, "find_received", lambda c: [_row(1)])

    def bad(c: Any, e: NotifyEvent) -> bool:
        raise RuntimeError("inbox down")

    counts = run_once(FakeGraph(), conn, now=NOW, notifier=bad)  # type: ignore[arg-type]
    assert (counts.resumed, counts.errors) == (1, 0)


def test_failing_alert_is_counted_and_the_rest_still_go(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(poller, "find_received", lambda c: [])
    monkeypatch.setattr(poller, "find_near_due", lambda c, t: [_row(1), _row(2)])
    monkeypatch.setattr(poller, "find_discount_expiring", lambda c, t: [_row(3)])

    def flaky(c: Any, e: NotifyEvent) -> bool:
        if e.thread_id == "invoice-1":
            raise RuntimeError("x")
        return True

    counts = run_once(FakeGraph(), FakeConn(), now=NOW, notifier=flaky)  # type: ignore[arg-type]
    assert (counts.near_due_alerts, counts.discount_alerts, counts.errors) == (1, 1, 1)


def test_event_today_is_the_run_date() -> None:
    assert _row(1).event("NEAR_DUE", date(2026, 11, 25)).today == date(2026, 11, 25)
