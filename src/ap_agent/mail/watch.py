"""CLI: uv run python -m ap_agent.mail.watch --source local --path sample_inbox/eml"""

import argparse
import logging
import sys

import psycopg

from ap_agent.config import get_database_url
from ap_agent.mail.sources import ImapSource, LocalFolderSource, MailSource
from ap_agent.mail.storage import default_storage
from ap_agent.mail.watcher import OUTCOMES, run_once


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run the email watcher once.")
    p.add_argument("--source", choices=["local", "imap"], required=True)
    p.add_argument("--path", help="folder of .eml files (required for --source local)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    source: MailSource
    if args.source == "local":
        if not args.path:
            p.error("--path is required with --source local")
        source = LocalFolderSource(args.path)
    else:
        source = ImapSource()

    url = get_database_url()
    if not url:
        print("FAIL: DATABASE_URL is not set (see .env.example).")
        return 1
    with psycopg.connect(url, connect_timeout=10) as conn:
        counts = run_once(source, conn, default_storage())

    print(f"{'outcome':<20}{'count':>6}")
    print("-" * 26)
    for outcome in OUTCOMES:
        print(f"{outcome:<20}{counts[outcome]:>6}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
