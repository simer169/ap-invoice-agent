"""ImapSource tests with a fake IMAP session (no network, no DB)."""

from imap_tools import MailMessageFlags

from ap_agent.mail.sources import ImapSource
from fake_imap import CountingFactory, FakeImapMessage, FakeMailBox


def test_flag_uses_fetched_uid_on_same_session() -> None:
    box = FakeMailBox([FakeImapMessage("101"), FakeImapMessage("102")])
    factory = CountingFactory(box)
    source = ImapSource(factory)

    msgs = source.fetch()
    for m in msgs:
        source.mark_seen(m.message_id)

    assert factory.logins == 1  # one session for fetch + all flags
    assert box.folder.set_calls == [("INBOX", False)]  # read-write SELECT
    assert box.fetch_kwargs["mark_seen"] is False
    assert [c[0] for c in box.flag_calls] == [["101"], ["102"]]
    assert all(c[1] == MailMessageFlags.SEEN and c[2] is True for c in box.flag_calls)
    assert box.logged_out == 0  # still open until close()

    source.close()
    source.close()  # idempotent
    assert box.logged_out == 1


def test_mark_seen_without_session_or_uid_is_noop() -> None:
    box = FakeMailBox([])
    source = ImapSource(CountingFactory(box))
    source.mark_seen("<unknown>")  # never fetched
    assert box.flag_calls == []
