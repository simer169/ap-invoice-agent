"""Apply scheduled deliveries up to and including a day (idempotent).

    uv run python -m data_gen.simulate --day 2026-11-15
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import psycopg

from ap_agent.config import get_database_url
from data_gen.build import SCHEDULE_PATH


def load_schedule(path: Path = SCHEDULE_PATH) -> list[dict[str, object]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data)


def apply_day(conn: psycopg.Connection, day: date, schedule: list[dict[str, object]]) -> list[str]:
    """Insert every delivery dated <= day not yet applied; update touched PO statuses.

    Returns human-readable change lines (empty when nothing changed). Caller commits.
    """
    changes: list[str] = []
    touched: dict[str, None] = {}  # ordered set of PO numbers
    for d in schedule:
        if date.fromisoformat(str(d["delivery_date"])) > day:
            continue
        po_number = str(d["po_number"])
        row = conn.execute(
            "INSERT INTO trex.receiving_reports (rr_number, po_id, received_at, received_by) "
            "SELECT %s, id, %s, %s FROM trex.purchase_orders WHERE po_number = %s "
            "ON CONFLICT (rr_number) DO NOTHING RETURNING id",
            (d["rr_number"], d["received_at"], d["received_by"], po_number),
        ).fetchone()
        if row is None:  # already applied (or PO missing): lines are only added with a new RR
            continue
        lines = d["lines"]
        assert isinstance(lines, list)
        for ln in lines:
            conn.execute(
                "INSERT INTO trex.receiving_lines (rr_id, po_line_id, qty_received) "
                "SELECT %s, pl.id, %s FROM trex.po_lines pl "
                "JOIN trex.purchase_orders po ON po.id = pl.po_id "
                "WHERE po.po_number = %s AND pl.line_no = %s",
                (row[0], ln["qty"], po_number, ln["line_no"]),
            )
        changes.append(
            f"{d['delivery_date']}  {po_number}: received {d['rr_number']} ({len(lines)} lines)"
        )
        touched[po_number] = None

    for po_number in touched:
        old = conn.execute(
            "SELECT status FROM trex.purchase_orders WHERE po_number = %s", (po_number,)
        ).fetchone()
        new = conn.execute(
            "UPDATE trex.purchase_orders po SET status = CASE WHEN s.fully_received "
            "THEN 'RECEIVED' ELSE 'PARTIAL' END FROM trex.po_receipt_status s "
            "WHERE s.po_id = po.id AND po.po_number = %s AND po.status <> 'CLOSED' "
            "AND po.status <> CASE WHEN s.fully_received THEN 'RECEIVED' ELSE 'PARTIAL' END "
            "RETURNING po.status",
            (po_number,),
        ).fetchone()
        if new is not None and old is not None:
            changes.append(f"           {po_number}: status {old[0]} -> {new[0]}")
    return changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply scheduled deliveries up to a day.")
    parser.add_argument("--day", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    args = parser.parse_args(argv)

    url = get_database_url()
    if not url:
        print("FAIL: DATABASE_URL is not set (see .env.example).")
        return 1
    schedule = load_schedule()
    if not schedule:
        print(f"No schedule at {SCHEDULE_PATH}; run `python -m data_gen.build` first.")
        return 1
    try:
        with psycopg.connect(url, connect_timeout=10) as conn:
            changes = apply_day(conn, args.day, schedule)
    except psycopg.Error as exc:
        print(f"FAIL simulate: {type(exc).__name__}: {str(exc).splitlines()[0]}")
        return 1
    print(f"simulate --day {args.day}")
    print("\n".join(changes) if changes else "nothing to do")
    return 0


if __name__ == "__main__":
    sys.exit(main())
