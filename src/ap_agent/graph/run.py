"""CLI: uv run python -m ap_agent.graph.run --pending | --thread ID | --resume ID"""

import argparse
import json
import sys
from typing import Any

import psycopg

from ap_agent.config import get_database_url
from ap_agent.graph.checkpointer import make_checkpointer, make_pool
from ap_agent.graph.deps import prod_deps
from ap_agent.graph.graph import build_graph
from ap_agent.graph.runner import (
    ThreadSummary,
    config_for,
    pending_threads,
    resume_thread,
    run_thread,
    summarize,
)

_COLS = ("thread", "vendor", "invoice #", "PO", "status", "reasons")


def _row(s: ThreadSummary) -> tuple[str, ...]:
    status = f"CRASHED({s.next_nodes[0] if s.next_nodes else '?'})" if s.error else s.status
    reasons = s.error if s.error else "; ".join(s.reasons)
    return (s.thread_id[:20], s.vendor, s.invoice_number, s.po_number, status, reasons)


def print_table(summaries: list[ThreadSummary]) -> None:
    rows = [_COLS, *(_row(s) for s in summaries)]
    widths = [min(max(len(r[i]) for r in rows), 60 if i == 5 else 28) for i in range(len(_COLS))]
    for r in rows:
        print("  ".join(c[:w].ljust(w) for c, w in zip(r, widths, strict=True)).rstrip())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run or inspect invoice graph threads")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--pending", action="store_true", help="run every RUN_REQUESTED without a thread"
    )
    g.add_argument("--thread", metavar="ID", help="print saved state and history of a thread")
    g.add_argument("--resume", metavar="ID", help="continue a thread that crashed mid-run")
    args = p.parse_args(argv)

    url = get_database_url()
    if not url:
        print("FAIL: DATABASE_URL is not set (see .env.example).")
        return 1
    pool = make_pool(url)
    try:
        graph = build_graph(prod_deps(url), make_checkpointer(pool))
        if args.pending:
            with psycopg.connect(url, autocommit=True, connect_timeout=10) as conn:
                todo = pending_threads(conn)
            if not todo:
                print("Nothing pending.")
                return 0
            print_table([run_thread(graph, st) for st in todo])
        elif args.resume:
            print_table([resume_thread(graph, args.resume)])
        else:
            return _show(graph, args.thread)
    finally:
        pool.close()
    return 0


def _show(graph: Any, thread_id: str) -> int:
    snap = graph.get_state(config_for(thread_id))
    if not snap.created_at:
        print(f"No saved state for {thread_id}")
        return 1
    values = dict(snap.values)
    history = values.pop("history", [])
    print(json.dumps(values, indent=2, default=str))
    print("\nHistory:")
    for line in history:
        print(f"  {line}")
    s = summarize(graph, thread_id)
    print(f"\nNext: {', '.join(s.next_nodes) or '(finished)'}  waiting_for: {s.waiting_for}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
