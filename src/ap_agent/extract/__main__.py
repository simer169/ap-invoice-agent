"""CLI: uv run python -m ap_agent.extract <pdf> | --s3-key KEY [--cache]"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from ap_agent.config import get_settings
from ap_agent.mail.storage import sha256_hex


def _read_s3(key: str) -> bytes:
    import boto3

    s = get_settings()
    session = boto3.Session(profile_name=s.AWS_PROFILE, region_name=s.AWS_REGION)
    obj = session.client("s3").get_object(Bucket=s.S3_BUCKET, Key=key)
    return obj["Body"].read()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m ap_agent.extract")
    p.add_argument("pdf", nargs="?", type=Path, help="path to a local PDF")
    p.add_argument("--s3-key", help="read the PDF from the S3 bucket instead")
    p.add_argument("--cache", action="store_true", help="use the agent.extractions cache (DB)")
    p.add_argument("--refresh", action="store_true", help="with --cache: ignore the cached row")
    p.add_argument("--received", type=date.fromisoformat, help="email received date YYYY-MM-DD")
    args = p.parse_args(argv)
    if bool(args.pdf) == bool(args.s3_key):
        p.error("give exactly one of <pdf> or --s3-key")

    data = _read_s3(args.s3_key) if args.s3_key else args.pdf.read_bytes()

    if args.cache:
        import psycopg

        from ap_agent.extract.cache import extract_and_cache

        with psycopg.connect(get_settings().DATABASE_URL.get_secret_value()) as conn:
            result = extract_and_cache(
                conn, sha256_hex(data), data, received_date=args.received, refresh=args.refresh
            )
    else:
        from ap_agent.extract.reader import extract_invoice

        result = extract_invoice(data, received_date=args.received)

    out = result.model_dump(mode="json")
    print(json.dumps(out, indent=2))
    print(
        f"\nmethod={result.method} model={result.model} tokens_in={result.tokens_in} "
        f"tokens_out={result.tokens_out} cost_usd={result.cost_usd} retries={result.retries} "
        f"duration_ms={result.duration_ms} cached={result.cached} "
        f"date_ambiguous={result.date_ambiguous}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
