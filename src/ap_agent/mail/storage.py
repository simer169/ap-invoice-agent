"""S3 storage for raw invoice PDFs."""

import hashlib
from datetime import datetime
from typing import Any

from botocore.exceptions import ClientError

from ap_agent.config import get_settings


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pdf_key(content_hash: str, received_at: datetime) -> str:
    return f"raw/{received_at:%Y}/{received_at:%m}/{content_hash}.pdf"


class S3Storage:
    def __init__(self, bucket: str, client: Any) -> None:
        self.bucket = bucket
        self.client = client

    def upload_pdf(self, data: bytes, received_at: datetime) -> str:
        """Upload under raw/YYYY/MM/<sha256>.pdf unless it already exists. Returns the key."""
        key = pdf_key(sha256_hex(data), received_at)
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return key
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey", "NotFound"):
                raise
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType="application/pdf",
            ServerSideEncryption="AES256",
        )
        return key


def default_storage() -> S3Storage:
    import boto3

    s = get_settings()
    session = boto3.Session(profile_name=s.AWS_PROFILE, region_name=s.AWS_REGION)
    return S3Storage(s.S3_BUCKET, session.client("s3"))
