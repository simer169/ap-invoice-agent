"""Reset the agent's run state so the graph can be rerun from scratch.

Deletes LangGraph checkpoints (schema langgraph), agent.parked_invoices and the graph's audit rows
(after confirmation). Keeps processed_emails, extractions and the RUN_REQUESTED rows, so
`python -m ap_agent.graph.run --pending` reprocesses every invoice without new LLM spend.
"""

import argparse
import sys

import psycopg

from ap_agent.config import get_database_url
from ap_agent.graph.deps import ACTOR

_CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")


def _count(conn: psycopg.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()  # type: ignore[call-overload]
    assert row
    return int(row[0])


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = p.parse_args()

    url = get_database_url()
    if not url:
        print("FAIL: DATABASE_URL is not set (see .env.example).")
        return 1
    with psycopg.connect(url, connect_timeout=10) as conn:
        present = [
            t
            for t in _CHECKPOINT_TABLES
            if conn.execute("SELECT to_regclass(%s)", (f"langgraph.{t}",)).fetchone()[0]  # type: ignore[index]
        ]
        for t in present:
            print(
                f"langgraph.{t} rows to delete: {_count(conn, f'SELECT count(*) FROM langgraph.{t}')}"
            )
        print(
            f"agent.parked_invoices rows to delete: {_count(conn, 'SELECT count(*) FROM agent.parked_invoices')}"
        )
        n_audit = conn.execute(
            "SELECT count(*) FROM agent.audit_log WHERE actor = %s", (ACTOR,)
        ).fetchone()
        assert n_audit
        print(f"graph audit rows to delete: {n_audit[0]}")
        if not args.yes and input("Type 'yes' to delete: ").strip().lower() != "yes":
            print("Aborted.")
            return 1
        with conn.transaction():
            for t in present:
                conn.execute(f"DELETE FROM langgraph.{t}")  # type: ignore[arg-type]
            conn.execute("DELETE FROM agent.parked_invoices")
            conn.execute("DELETE FROM agent.audit_log WHERE actor = %s", (ACTOR,))
    print("Done. processed_emails, extractions and RUN_REQUESTED rows were kept.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
