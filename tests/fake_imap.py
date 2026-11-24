"""Fake IMAP session for tests: records every call, no network."""

import uuid
from email.message import EmailMessage
from typing import Any


class FakeFolder:
    def __init__(self) -> None:
        self.set_calls: list[tuple[str, bool]] = []

    def set(self, name: str, readonly: bool = False) -> None:
        self.set_calls.append((name, readonly))


class FakeImapMessage:
    def __init__(self, uid: str, with_pdf: bool = True) -> None:
        msg = EmailMessage()
        msg["Message-ID"] = f"<{uuid.uuid4().hex}@test.example>"
        msg["From"] = "billing@vendor.example"
        msg["Subject"] = f"Invoice {uid}"
        msg["Date"] = "Mon, 02 Nov 2026 09:00:00 +0000"
        msg.set_content("attached")
        if with_pdf:
            data = b"%PDF-1.4\n% " + uuid.uuid4().hex.encode() + b"\n%%EOF\n"
            msg.add_attachment(data, maintype="application", subtype="pdf", filename="i.pdf")
        self.uid = uid
        self.obj = msg
        self.message_id = str(msg["Message-ID"])


class FakeMailBox:
    def __init__(self, messages: list[FakeImapMessage], fail_flag_uids: set[str] | None = None):
        self.messages = messages
        self.fail_flag_uids = fail_flag_uids or set()
        self.folder = FakeFolder()
        self.flag_calls: list[tuple[list[str], Any, bool]] = []
        self.logged_out = 0

    def fetch(self, criteria: Any = None, **kwargs: Any) -> list[FakeImapMessage]:
        self.fetch_kwargs = kwargs
        return list(self.messages)

    def flag(self, uid_list: list[str], flag_set: Any, value: bool) -> None:
        self.flag_calls.append((list(uid_list), flag_set, value))
        if any(u in self.fail_flag_uids for u in uid_list):
            raise RuntimeError('Response status "OK" expected, but "NO" received')

    def logout(self) -> None:
        self.logged_out += 1


class CountingFactory:
    """mailbox_factory that hands out one FakeMailBox and counts logins."""

    def __init__(self, mailbox: FakeMailBox) -> None:
        self.mailbox = mailbox
        self.logins = 0

    def __call__(self) -> FakeMailBox:
        self.logins += 1
        return self.mailbox
