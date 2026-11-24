from types import SimpleNamespace
from typing import Any

from ap_agent.extract import batch
from ap_agent.extract.batch import CONFIRM_ABOVE, PendingPdf, run_pending


class Conn:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _pending(n: int) -> list[PendingPdf]:
    return [PendingPdf(f"{i:064d}", f"raw/2026/11/{i}.pdf", None) for i in range(n)]


def _extract(fail_on: set[str] | None = None) -> Any:
    def fake(conn: Any, h: str, pdf: bytes, received_date: Any = None) -> Any:
        if fail_on and h in fail_on:
            raise RuntimeError("boom")
        return SimpleNamespace(cached=False, cost_usd=0.002, method="text")

    return fake


def test_nothing_pending(monkeypatch: Any) -> None:
    monkeypatch.setattr(batch, "pending_pdfs", lambda c: [])
    s = run_pending(Conn(), lambda k: b"", out=lambda m: None)  # type: ignore[arg-type]
    assert s.pending == 0 and s.extracted == 0


def test_small_batch_no_prompt_commits_each_and_sums_cost(monkeypatch: Any) -> None:
    monkeypatch.setattr(batch, "pending_pdfs", lambda c: _pending(3))
    conn, lines = Conn(), []

    def no_prompt(_: str) -> str:
        raise AssertionError("must not prompt for <= 20 PDFs")

    s = run_pending(
        conn,  # type: ignore[arg-type]
        lambda k: b"%PDF",
        confirm=no_prompt,
        out=lines.append,
        extract=_extract(),
    )
    assert s.extracted == 3 and conn.commits == 3
    assert abs(s.total_cost_usd - 0.006) < 1e-9
    assert "total_cost_usd=0.006000" in lines[-1]


def test_large_batch_asks_and_can_abort(monkeypatch: Any) -> None:
    monkeypatch.setattr(batch, "pending_pdfs", lambda c: _pending(CONFIRM_ABOVE + 1))
    s = run_pending(
        Conn(),  # type: ignore[arg-type]
        lambda k: b"",
        confirm=lambda _: "n",
        out=lambda m: None,
        extract=_extract(),
    )
    assert s.aborted and s.extracted == 0


def test_large_batch_yes_flag_skips_prompt(monkeypatch: Any) -> None:
    monkeypatch.setattr(batch, "pending_pdfs", lambda c: _pending(CONFIRM_ABOVE + 1))
    s = run_pending(
        Conn(),  # type: ignore[arg-type]
        lambda k: b"",
        yes=True,
        confirm=lambda _: (_ for _ in ()).throw(AssertionError("no prompt")),
        out=lambda m: None,
        extract=_extract(),
    )
    assert s.extracted == CONFIRM_ABOVE + 1


def test_failure_is_isolated_and_rolled_back(monkeypatch: Any) -> None:
    pend = _pending(3)
    monkeypatch.setattr(batch, "pending_pdfs", lambda c: pend)
    conn = Conn()
    s = run_pending(
        conn,  # type: ignore[arg-type]
        lambda k: b"",
        out=lambda m: None,
        extract=_extract({pend[1].content_hash}),
    )
    assert (s.extracted, s.failed) == (2, 1)
    assert conn.rollbacks == 1 and conn.commits == 2
