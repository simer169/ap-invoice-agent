"""Connectivity checks for OpenRouter, Postgres and S3. Never prints secret values."""

import sys
from collections.abc import Callable

import boto3
import psycopg
from openai import OpenAI

from ap_agent.config import SettingsError, get_settings


def check_openrouter() -> str:
    s = get_settings()
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=s.OPENROUTER_API_KEY.get_secret_value(),
    )
    resp = client.chat.completions.create(
        model=s.EXTRACT_MODEL,
        messages=[{"role": "user", "content": "Reply with one short sentence saying hello."}],
        max_tokens=40,
    )
    text = (resp.choices[0].message.content or "").strip().replace("\n", " ")
    return text[:100]


def check_database() -> str:
    s = get_settings()
    with psycopg.connect(s.DATABASE_URL.get_secret_value(), connect_timeout=10) as conn:
        row = conn.execute("select 1").fetchone()
    return f"select 1 -> {row[0] if row else None}"


def check_s3() -> str:
    s = get_settings()
    session = boto3.Session(profile_name=s.AWS_PROFILE, region_name=s.AWS_REGION)
    resp = session.client("s3").list_objects_v2(Bucket=s.S3_BUCKET, MaxKeys=5)
    return f"{resp.get('KeyCount', 0)} object(s) listed"


def main() -> int:
    try:
        get_settings()
    except SettingsError as exc:
        print(f"FAIL settingsppppppp: {exc}")
        return 1
    checks: list[tuple[str, Callable[[], str]]] = [
        ("openrouter", check_openrouter),
        ("database", check_database),
        ("s3", check_s3),
    ]
    failed = 0
    for name, fn in checks:
        try:
            print(f"OK   {name}: {fn()}")
        except Exception as exc:  # noqa: BLE001 - report any failure, type only
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
