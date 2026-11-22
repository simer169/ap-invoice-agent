"""Fill the trex schema with deterministic FAKE data.

    uv run python -m data_gen.build --seed 42
"""

import argparse
import json
import sys
from pathlib import Path

import psycopg

from ap_agent.config import get_database_url
from data_gen import db
from data_gen.generate import Dataset, generate

SCHEDULE_PATH = Path(__file__).resolve().parent / "output" / "schedule.json"


def write_schedule(ds: Dataset, path: Path = SCHEDULE_PATH) -> int:
    """Write future deliveries (replacing any stale file). Returns the number of deliveries."""
    entries = ds.schedule_entries()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
    return len(entries)


def build(conn: psycopg.Connection, seed: int) -> tuple[Dataset, dict[str, int]]:
    """Delete previously generated rows, insert fresh ones. Caller commits."""
    ds = generate(seed)
    db.clean(conn)
    db.load(conn, ds)
    return ds, db.counts(conn)


def format_table(rows: dict[str, int]) -> str:
    width = max(len(k) for k in rows)
    lines = [f"{'table':<{width}}  rows", f"{'-' * width}  ----"]
    lines += [f"{k:<{width}}  {v:>4}" for k, v in rows.items()]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate fake trex data (idempotent rebuild).")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)

    url = get_database_url()
    if not url:
        print("FAIL: DATABASE_URL is not set (see .env.example).")
        return 1
    try:
        with psycopg.connect(url, connect_timeout=10) as conn:
            ds, counts = build(conn, args.seed)
            statuses = db.status_counts(conn)
    except psycopg.errors.ForeignKeyViolation as exc:
        print(f"FAIL: non-generated rows reference GEN- data, not touching them: {exc}")
        return 1
    except psycopg.Error as exc:
        print(f"FAIL build: {type(exc).__name__}: {str(exc).splitlines()[0]}")
        return 1
    n_sched = write_schedule(ds)
    print(f"seed={args.seed}  demo today=2026-11-05\n")
    print(format_table(counts))
    print("\nPO status: " + ", ".join(f"{k}={v}" for k, v in statuses.items()))
    print(f"scheduled deliveries (not inserted): {n_sched} -> {SCHEDULE_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
