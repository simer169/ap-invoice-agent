"""Compare ground-truth outcomes with what the graph has actually done so far.

    uv run python evals/outcome_check.py --split dev|eval

ground_truth expected_outcome is mapped to the status we should have NOW (no human step yet; anomaly
flags do not change the status, they must show up in the reasons), then compared with agent.parked_invoices / the graph's reject audit row.
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import psycopg

from ap_agent.config import get_database_url

GROUND_TRUTH = Path(__file__).resolve().parent / "ground_truth.json"
NOT_RUN = "NOT_RUN"  # no thread and no parked row
NO_FLAG = "NO_FLAG"  # READY as expected, but the expected anomaly flag is missing
EXPECTED_FLAG = {"ANOMALY_PRICE": "PRICE_OVER_TOLERANCE", "ANOMALY_AMOUNT": "AMOUNT_OUTLIER"}


def expected_statuses(outcome: str) -> set[str] | None:
    """Statuses acceptable right now for a ground-truth outcome; None = not evaluated."""
    if outcome == "READY" or outcome.startswith("ANOMALY_"):
        return {"READY"}  # flags are checked separately (EXPECTED_FLAG)
    if outcome in ("PARK_THEN_READY", "STAYS_WAITING"):
        return {"WAITING"}
    if outcome == "REJECT_DUPLICATE":
        return {"REJECTED", NOT_RUN}  # the watcher may have caught it before any run
    if outcome.startswith("EXCEPTION_"):
        return {"EXCEPTION"}
    return None


def actual_status(conn: psycopg.Connection[Any], content_hash: str) -> tuple[str, str]:
    """(status, reasons) for the thread of this PDF hash."""
    row = conn.execute(
        "SELECT status, anomalies FROM agent.parked_invoices WHERE content_hash = %s",
        (content_hash,),
    ).fetchone()
    if row:
        return row[0], "; ".join(row[1] or [])
    rej = conn.execute(
        "SELECT details ->> 'note' FROM agent.audit_log WHERE action = 'NODE_reject' AND ref = %s",
        (f"invoice-{content_hash}",),
    ).fetchone()
    if rej:
        return "REJECTED", rej[0] or ""
    return NOT_RUN, "no thread yet"


def evaluate(
    entries: list[dict[str, Any]], lookup: Any
) -> tuple[Counter[tuple[str, str]], list[tuple[str, str, str, str, str]], int]:
    """Returns (confusion[(expected_outcome, actual)], mismatches, evaluated)."""
    confusion: Counter[tuple[str, str]] = Counter()
    mismatches: list[tuple[str, str, str, str, str]] = []
    n = 0
    for e in entries:
        ok = expected_statuses(e["expected_outcome"])
        if ok is None:
            continue
        n += 1
        status, why = lookup(e["pdf_sha256"])
        flag = EXPECTED_FLAG.get(e["expected_outcome"])
        if flag and status in ok and flag not in why:
            status = NO_FLAG
        confusion[(e["expected_outcome"], status)] += 1
        if status not in ok:
            mismatches.append(
                (e["id"], e.get("scenario") or "", e["expected_outcome"], status, why)
            )
    return confusion, mismatches, n


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--split", choices=("dev", "eval"), required=True)
    args = p.parse_args()

    url = get_database_url()
    if not url:
        print("FAIL: DATABASE_URL is not set (see .env.example).")
        return 1
    data = json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))
    entries = [e for e in data["invoices"] if e.get("split") == args.split]
    with psycopg.connect(url, connect_timeout=10) as conn:
        confusion, mismatches, n = evaluate(entries, lambda h: actual_status(conn, h))

    print(f"split={args.split} evaluated={n}")
    actuals = sorted({a for _, a in confusion})
    outcomes = sorted({o for o, _ in confusion})
    w = max([len(o) for o in outcomes] + [8])
    print("expected".ljust(w), *(a.rjust(9) for a in actuals), sep="  ")
    for o in outcomes:
        print(o.ljust(w), *(str(confusion[(o, a)]).rjust(9) for a in actuals), sep="  ")
    print(f"\nmismatches: {len(mismatches)}")
    for inv_id, scenario, exp, act, why in mismatches:
        print(f"  {inv_id} [{scenario}] expected {exp}, got {act}: {why}")
    return 0 if not mismatches else 2


if __name__ == "__main__":
    sys.exit(main())
