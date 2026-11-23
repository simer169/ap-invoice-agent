"""Reset watcher state so the demo can be rerun.

Deletes agent.processed_emails rows and RUN_REQUESTED audit rows (after confirmation).
S3 objects and agent.parked_invoices are left untouched.
"""

import argparse
import sys

import psycopg

from ap_agent.config import get_database_url


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = p.parse_args()

    url = get_database_url()
    if not url:
        print("FAIL: DATABASE_URL is not set (see .env.example).")
        return 1
    with psycopg.connect(url, connect_timeout=10) as conn:
        n_mail = conn.execute("SELECT count(*) FROM agent.processed_emails").fetchone()
        n_audit = conn.execute(
            "SELECT count(*) FROM agent.audit_log WHERE action = 'RUN_REQUESTED'"
        ).fetchone()
        assert n_mail and n_audit
        print(f"processed_emails rows to delete:      {n_mail[0]}")
        print(f"RUN_REQUESTED audit rows to delete:   {n_audit[0]}")
        if not args.yes and input("Type 'yes' to delete: ").strip().lower() != "yes":
            print("Aborted.")
            return 1
        with conn.transaction():
            conn.execute("DELETE FROM agent.processed_emails")
            conn.execute("DELETE FROM agent.audit_log WHERE action = 'RUN_REQUESTED'")
    print("Done. S3 objects and parked invoices were not touched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
