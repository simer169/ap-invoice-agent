"""Watcher tests: LocalFolderSource + moto S3 + the real test DB (rolled back).

Skipped when DATABASE_URL is unset. All rows are written in a transaction that is rolled
back at teardown; message ids and PDF bodies are unique per test run.
"""

import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

import boto3
import psycopg
import pytest
from moto import mock_aws

from ap_agent.config import get_database_url
from ap_agent.mail.sources import LocalFolderSource
from ap_agent.mail.storage import S3Storage, pdf_key, sha256_hex
from ap_agent.mail.watcher import run_once

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from migrate import migrate

DATABASE_URL = get_database_url()
BUCKET = "test-ap-bucket"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(DATABASE_URL is None, reason="DATABASE_URL not set"),
]


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    assert DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL, connect_timeout=10) as c:
        migrate(c)
        try:
            yield c
        finally:
            c.rollback()


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> Iterator[S3Storage]:
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "testing")
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield S3Storage(BUCKET, client)


def _pdf() -> bytes:
    return b"%PDF-1.4\n% " + uuid.uuid4().hex.encode() + b"\n%%EOF\n"


def _eml(folder: Path, name: str, day: int, attachment: tuple[str, str, bytes] | None) -> str:
    msg = EmailMessage()
    mid = f"<{uuid.uuid4().hex}@test.example>"
    msg["Message-ID"] = mid
    msg["From"] = "billing@vendor.example"
    msg["To"] = "ap@demorail.example"
    msg["Subject"] = f"Invoice {name}"
    msg["Date"] = datetime(2026, 11, day, 9, 0, tzinfo=UTC).strftime("%a, %d %b %Y %H:%M:%S %z")
    msg.set_content("Please find the invoice attached.")
    if attachment:
        fname, ctype, data = attachment
        maintype, subtype = ctype.split("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=fname)
    (folder / f"{name}.eml").write_bytes(bytes(msg))
    return mid


def _row(conn: psycopg.Connection, mid: str) -> tuple[str, str | None, str | None, str | None]:
    r = conn.execute(
        "SELECT outcome, content_hash, s3_key, error_message FROM agent.processed_emails "
        "WHERE message_id = %s",
        (mid,),
    ).fetchone()
    assert r
    return r


def _runs(conn: psycopg.Connection, content_hash: str) -> int:
    r = conn.execute(
        "SELECT count(*) FROM agent.audit_log WHERE action = 'RUN_REQUESTED' AND ref = %s",
        (f"invoice-{content_hash}",),
    ).fetchone()
    assert r
    return int(r[0])


def _object_count(s3: S3Storage) -> int:
    return s3.client.list_objects_v2(Bucket=BUCKET).get("KeyCount", 0)


def test_run_twice_processes_each_email_once(
    conn: psycopg.Connection, s3: S3Storage, tmp_path: Path
) -> None:
    pdfs = [_pdf(), _pdf(), _pdf()]
    mids = [
        _eml(tmp_path, f"a{i}", i + 1, ("inv.pdf", "application/pdf", p))
        for i, p in enumerate(pdfs)
    ]
    source = LocalFolderSource(tmp_path)

    first = run_once(source, conn, s3)
    assert first["PROCESSED"] == 3
    assert first["SKIPPED"] == 0
    second = run_once(source, conn, s3)
    assert second["SKIPPED"] == 3
    assert sum(second[o] for o in ("PROCESSED", "DUPLICATE_CONTENT", "IGNORED", "ERROR")) == 0

    for mid, p in zip(mids, pdfs, strict=True):
        h = sha256_hex(p)
        outcome, got_hash, key, _ = _row(conn, mid)
        assert (outcome, got_hash) == ("PROCESSED", h)
        assert key == pdf_key(h, datetime(2026, 11, mids.index(mid) + 1, tzinfo=UTC))
        assert _runs(conn, h) == 1
    assert _object_count(s3) == 3


def test_same_pdf_in_two_emails_uploads_once(
    conn: psycopg.Connection, s3: S3Storage, tmp_path: Path
) -> None:
    pdf = _pdf()
    m1 = _eml(tmp_path, "first", 1, ("a.pdf", "application/pdf", pdf))
    m2 = _eml(tmp_path, "second", 2, ("b.pdf", "application/pdf", pdf))

    counts = run_once(LocalFolderSource(tmp_path), conn, s3)

    assert counts["PROCESSED"] == 1
    assert counts["DUPLICATE_CONTENT"] == 1
    assert _object_count(s3) == 1
    assert _row(conn, m1)[0] == "PROCESSED"  # oldest email wins
    assert _row(conn, m2)[0] == "DUPLICATE_CONTENT"
    assert _runs(conn, sha256_hex(pdf)) == 1


def test_email_without_pdf_is_ignored(
    conn: psycopg.Connection, s3: S3Storage, tmp_path: Path
) -> None:
    plain = _eml(tmp_path, "plain", 1, None)
    other = _eml(tmp_path, "image", 2, ("logo.png", "image/png", b"\x89PNG\r\n"))

    counts = run_once(LocalFolderSource(tmp_path), conn, s3)

    assert counts["IGNORED"] == 2
    assert _row(conn, plain)[0] == "IGNORED"
    assert _row(conn, other)[0] == "IGNORED"
    assert _object_count(s3) == 0


def test_pdf_detected_by_name_when_content_type_is_generic(
    conn: psycopg.Connection, s3: S3Storage, tmp_path: Path
) -> None:
    mid = _eml(tmp_path, "octet", 1, ("Invoice.PDF", "application/octet-stream", _pdf()))
    run_once(LocalFolderSource(tmp_path), conn, s3)
    assert _row(conn, mid)[0] == "PROCESSED"


def test_corrupt_attachment_is_error_and_batch_continues(
    conn: psycopg.Connection, s3: S3Storage, tmp_path: Path
) -> None:
    bad = _eml(tmp_path, "a_bad", 1, ("broken.pdf", "application/pdf", b"not a pdf at all"))
    good_pdf = _pdf()
    good = _eml(tmp_path, "b_good", 2, ("ok.pdf", "application/pdf", good_pdf))

    counts = run_once(LocalFolderSource(tmp_path), conn, s3)

    assert counts["ERROR"] == 1
    assert counts["PROCESSED"] == 1
    outcome, _, _, err = _row(conn, bad)
    assert outcome == "ERROR"
    assert err and "not a valid PDF" in err
    assert _row(conn, good)[0] == "PROCESSED"
    assert _object_count(s3) == 1  # nothing uploaded for the corrupt one
    # the recorded error is final: a rerun skips it
    assert run_once(LocalFolderSource(tmp_path), conn, s3)["SKIPPED"] == 2


def test_upload_skipped_when_object_exists(s3: S3Storage) -> None:
    data = _pdf()
    when = datetime(2026, 3, 5, tzinfo=UTC)
    key = s3.upload_pdf(data, when)
    assert key == f"raw/2026/03/{sha256_hex(data)}.pdf"
    head = s3.client.head_object(Bucket=BUCKET, Key=key)
    assert head["ServerSideEncryption"] == "AES256"
    first_etag_time = head["LastModified"]
    assert s3.upload_pdf(data, when) == key
    assert s3.client.head_object(Bucket=BUCKET, Key=key)["LastModified"] == first_etag_time
    assert _object_count(s3) == 1


def test_flag_failure_is_logged_and_batch_continues(
    conn: psycopg.Connection, s3: S3Storage, caplog: pytest.LogCaptureFixture
) -> None:
    from ap_agent.mail.sources import ImapSource
    from fake_imap import CountingFactory, FakeImapMessage, FakeMailBox

    box = FakeMailBox([FakeImapMessage("201"), FakeImapMessage("202")], fail_flag_uids={"201"})
    factory = CountingFactory(box)
    with caplog.at_level("ERROR"):
        counts = run_once(ImapSource(factory), conn, s3)

    assert counts["PROCESSED"] == 2  # both recorded despite the first flag failing
    assert [c[0] for c in box.flag_calls] == [["201"], ["202"]]  # batch not stopped
    assert "could not mark" in caplog.text
    assert factory.logins == 1
    assert box.logged_out == 1  # run_once closed the session
    for m in box.messages:
        assert _row(conn, m.message_id)[0] == "PROCESSED"
