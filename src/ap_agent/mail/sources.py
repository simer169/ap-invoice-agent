"""Mail sources: where the watcher gets messages from."""

import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol, Self

from imap_tools import MailMessageFlags
from pydantic import BaseModel

from ap_agent.config import get_settings

log = logging.getLogger(__name__)


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

    def close(self) -> None:
        """Release any session; called by the watcher at the end of a run."""
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

    def close(self) -> None:
        return None


def _login_from_settings() -> Any:
    """Open one IMAP session (INBOX not yet selected). Secrets come from config only."""
    from imap_tools import MailBox

    s = get_settings()
    return MailBox(s.IMAP_HOST).login(s.IMAP_USER, s.IMAP_APP_PASSWORD.get_secret_value())


class ImapSource:
    """UNSEEN messages in INBOX; flagged seen only after the watcher records them.

    One IMAP session per run: fetch() logs in and keeps the session open; mark_seen() flags
    the uid captured during fetch on that same session; close() logs out.
    """

    def __init__(self, mailbox_factory: Callable[[], Any] = _login_from_settings) -> None:
        self._factory = mailbox_factory
        self._mb: Any = None
        self._uids: dict[str, str] = {}

    def fetch(self) -> list[MailMessage]:
        from imap_tools import AND

        self.close()
        out: list[MailMessage] = []
        self._mb = self._factory()
        self._mb.folder.set("INBOX", readonly=False)  # SELECT (read-write), never EXAMINE
        for m in self._mb.fetch(AND(seen=False), mark_seen=False, bulk=True):
            uid = m.uid or ""
            parsed = parse_eml(m.obj.as_bytes(), f"imap-{uid}")
            self._uids[parsed.message_id] = uid
            out.append(parsed)
        out.sort(key=lambda x: x.received_at)
        return out

    def mark_seen(self, message_id: str) -> None:
        uid = self._uids.get(message_id)
        if self._mb is None or not uid:
            log.debug("mark_seen skipped for %s: no open session or uid", message_id)
            return
        self._mb.flag([uid], MailMessageFlags.SEEN, True)

    def close(self) -> None:
        mb, self._mb = self._mb, None
        self._uids = {}
        if mb is not None:
            try:
                mb.logout()
            except Exception:
                log.exception("IMAP logout failed")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
