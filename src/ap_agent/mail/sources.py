"""Mail sources: where the watcher gets messages from."""

import hashlib
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from ap_agent.config import get_settings


class Attachment(BaseModel):
    name: str
    content_type: str
    data: bytes


class MailMessage(BaseModel):
    message_id: str
    from_address: str
    subject: str
    received_at: datetime
    attachments: list[Attachment] = []


class MailSource(Protocol):
    def fetch(self) -> list[MailMessage]: ...

    def mark_seen(self, message_id: str) -> None:
        """Called by the watcher only after the message has been recorded."""
        ...


def _is_pdf(name: str, content_type: str) -> bool:
    return content_type.lower() == "application/pdf" or name.lower().endswith(".pdf")


def pdf_attachments(msg: MailMessage) -> list[Attachment]:
    return [a for a in msg.attachments if _is_pdf(a.name, a.content_type)]


def parse_eml(raw: bytes, fallback_id: str) -> MailMessage:
    """Parse raw RFC 5322 bytes into a MailMessage."""
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    assert isinstance(parsed, EmailMessage)
    message_id = (parsed["Message-ID"] or "").strip() or fallback_id
    date_hdr = parsed["Date"]
    try:
        received = parsedate_to_datetime(str(date_hdr)) if date_hdr else None
    except (TypeError, ValueError):
        received = None
    if received is None:
        received = datetime.now(UTC)
    elif received.tzinfo is None:
        received = received.replace(tzinfo=UTC)
    attachments = [
        Attachment(
            name=part.get_filename() or "",
            content_type=part.get_content_type(),
            data=part.get_payload(decode=True) or b"",
        )
        for part in parsed.iter_attachments()
    ]
    return MailMessage(
        message_id=message_id,
        from_address=str(parsed["From"] or ""),
        subject=str(parsed["Subject"] or ""),
        received_at=received,
        attachments=attachments,
    )


class LocalFolderSource:
    """Reads .eml files from a folder, oldest first."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def fetch(self) -> list[MailMessage]:
        messages: list[MailMessage] = []
        for f in sorted(self.path.glob("*.eml")):
            fallback = "local-" + hashlib.sha256(f.name.encode()).hexdigest()[:16]
            messages.append(parse_eml(f.read_bytes(), fallback))
        messages.sort(key=lambda m: m.received_at)  # stable: ties keep filename order
        return messages

    def mark_seen(self, message_id: str) -> None:
        return None


class ImapSource:
    """UNSEEN messages in INBOX; flagged seen only after the watcher records them."""

    def __init__(self) -> None:
        self._uids: dict[str, str] = {}

    def fetch(self) -> list[MailMessage]:
        from imap_tools import AND, MailBox

        s = get_settings()
        out: list[MailMessage] = []
        self._uids = {}
        with MailBox(s.IMAP_HOST).login(
            s.IMAP_USER, s.IMAP_APP_PASSWORD.get_secret_value(), initial_folder="INBOX"
        ) as mb:
            for m in mb.fetch(AND(seen=False), mark_seen=False, bulk=False):
                uid = m.uid or ""
                parsed = parse_eml(m.obj.as_bytes(), f"imap-{uid}")
                self._uids[parsed.message_id] = uid
                out.append(parsed)
        out.sort(key=lambda x: x.received_at)
        return out

    def mark_seen(self, message_id: str) -> None:
        from imap_tools import MailBox

        uid = self._uids.get(message_id)
        if not uid:
            return
        s = get_settings()
        with MailBox(s.IMAP_HOST).login(
            s.IMAP_USER, s.IMAP_APP_PASSWORD.get_secret_value(), initial_folder="INBOX"
        ) as mb:
            mb.flag(uid, "\\Seen", True)
