"""The Telegram client's last-resort length cap counts UTF-16 units, as Telegram does.

The tag-safety of ``truncate_html_safe`` is pinned in the existing Telegram client tests.
"""

from __future__ import annotations

import pytest

from kiro_crew.telegram.client import TELEGRAM_MAX_TEXT, _cap_text, truncate_html_safe
from kiro_crew.telegram.transport import TelegramTransport


def _units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def test_plain_text_of_emoji_is_cut_to_the_utf16_cap() -> None:
    text = "\U0001f7e2" * 3000  # 3000 code points, 6000 UTF-16 units

    capped = _cap_text(text, None)

    assert _units(capped) <= TELEGRAM_MAX_TEXT
    assert capped == "\U0001f7e2" * (TELEGRAM_MAX_TEXT // 2)  # no split surrogate pair


def test_html_of_emoji_is_cut_to_the_utf16_cap_and_stays_balanced() -> None:
    text = "<b>" + "\U0001f7e2" * 3000 + "</b>"

    capped = _cap_text(text, "HTML")

    assert _units(capped) <= TELEGRAM_MAX_TEXT
    assert capped.startswith("<b>") and capped.endswith("</b>")


def test_text_under_the_utf16_cap_is_returned_unchanged() -> None:
    text = "\U0001f7e2" * (TELEGRAM_MAX_TEXT // 2)

    assert _cap_text(text, None) == text
    assert truncate_html_safe(text) == text


def test_ascii_text_is_cut_exactly_as_before() -> None:
    text = "a" * (TELEGRAM_MAX_TEXT + 10)

    assert _cap_text(text, None) == "a" * TELEGRAM_MAX_TEXT


class _RecordingClient:
    def __init__(self, refuse_after: int | None = None) -> None:
        self.sent: list[str] = []
        self._refuse_after = refuse_after

    async def send_message(self, chat_id, text, **_kwargs):
        if self._refuse_after is not None and len(self.sent) >= self._refuse_after:
            return None
        self.sent.append(text)
        return len(self.sent)


@pytest.mark.asyncio
async def test_an_emoji_dense_plain_send_is_split_not_cut() -> None:
    """A part under 4096 code points can be over 4096 units: it must not lose its tail."""
    client = _RecordingClient()
    transport = TelegramTransport(client, allowed_user_ids=[1])
    text = "\U0001f7e2" * 3000  # what a code-point chunker hands over

    message_id = await transport.send_message("1", text)

    assert "".join(client.sent) == text, "the whole text was delivered"
    assert all(_units(part) <= TELEGRAM_MAX_TEXT for part in client.sent)
    assert message_id == str(len(client.sent))


@pytest.mark.asyncio
async def test_a_refused_piece_fails_the_whole_send() -> None:
    client = _RecordingClient(refuse_after=1)
    transport = TelegramTransport(client, allowed_user_ids=[1])

    assert await transport.send_message("1", "\U0001f7e2" * 3000) == ""


@pytest.mark.asyncio
async def test_the_utf16_split_does_not_rejoin_a_key_across_its_cut() -> None:
    """The split drops the newline at its cut, so two lines must not read as one key."""
    client = _RecordingClient()
    transport = TelegramTransport(client, allowed_user_ids=[1])
    key = "AKIA" + "Q3XRZ7MBN2TLW5YP"
    # 2039 emoji fill 4078 units; the newline then falls where the split cuts.
    text = "\U0001f7e2" * 2039 + key[:12] + "\n" + key[12:]

    await transport.send_message("1", text)

    assert client.sent, "something was delivered"
    assert key not in "".join(client.sent)
    assert all(_units(part) <= TELEGRAM_MAX_TEXT for part in client.sent)
