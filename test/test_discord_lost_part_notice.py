"""A long Discord reply split across messages never loses a part silently.

The renderer rotates to a new message whenever the reply outgrows Discord's
message limit, and the rotation drops each part's text once it is sealed. A part
whose every send and edit fails is therefore gone for good while the parts after
it still go out, so without a mark the reader sees the parts on either side as
one continuous answer. The real ``DiscordRenderer`` streams into the shared fake
client; the one seam is a failure for any send or edit carrying a marker. The
screen is rebuilt as Discord shows it: each message in send order with its last
landed text.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from test_discord import DISCORD_CAPABILITIES, FakeClient

from kiro_crew.discord.renderer import DiscordRenderer

# The exact wording the renderer sends in place of a lost part.
LOST_PART_NOTICE = "Part of this reply may not have been delivered."


class _Screen(FakeClient):
    """The shared fake, refusing every send and edit that carries *fail_marker*."""

    def __init__(self, fail_marker: str = "", *, edits_only: bool = False) -> None:
        super().__init__()
        self.fail_marker = fail_marker
        self.edits_only = edits_only
        self.screen: dict[str, str] = {}
        self.order: list[str] = []

    def _refuses(self, text: str) -> bool:
        return bool(self.fail_marker) and self.fail_marker in text

    async def send_message(self, channel_id: str, text: str, **kw: Any) -> Any:
        if self._refuses(text) and not self.edits_only:
            return None
        mid = await super().send_message(channel_id, text, **kw)
        if mid:
            self.order.append(mid)
            self.screen[mid] = text
        return mid

    async def edit_message(self, channel_id: str, message_id: str, text: str, **kw: Any) -> bool:
        if self._refuses(text):
            return False
        ok = await super().edit_message(channel_id, message_id, text, **kw)
        if ok:
            self.screen[message_id] = text
        return ok

    def visible(self) -> list[str]:
        return [self.screen[mid] for mid in self.order]


def _part(tag: str, ch: str, n: int) -> str:
    return f"{tag} " + " ".join([ch * 9] * (n // 10)) + "\n\n"


async def _three_part_reply(cli: _Screen) -> DiscordRenderer:
    r = DiscordRenderer(cli, "chan1", DISCORD_CAPABILITIES, session_key="sk")  # type: ignore[arg-type]
    await r.on_turn_start()
    for piece in (
        _part("PART-A", "a", 1800),
        _part("PART-B", "b", 1800),
        _part("PART-C", "c", 600),
    ):
        await r.on_text_chunk(piece)
    await r.on_done()
    return r


def _sequence(cli: _Screen) -> list[str]:
    out = []
    for text in cli.visible():
        if text == LOST_PART_NOTICE:
            out.append("NOTICE")
        else:
            out.extend(tag for tag in ("PART-A", "PART-B", "PART-C") if tag in text)
    return out


@pytest.mark.asyncio
async def test_a_part_that_fails_for_good_is_replaced_by_a_notice(caplog) -> None:
    cli = _Screen(fail_marker="PART-B")
    with caplog.at_level(logging.WARNING, logger="kiro_crew.discord.renderer"):
        r = await _three_part_reply(cli)
    assert _sequence(cli) == ["PART-A", "NOTICE", "PART-C"]
    assert any("could not be delivered" in rec.getMessage() for rec in caplog.records)
    # The reply reached the reader, so the turn is not a delivery failure.
    assert r.delivery_failed is False


@pytest.mark.asyncio
async def test_a_reply_whose_parts_all_land_sends_no_notice() -> None:
    cli = _Screen()
    await _three_part_reply(cli)
    assert _sequence(cli) == ["PART-A", "PART-B", "PART-C"]


@pytest.mark.asyncio
async def test_a_part_whose_fallback_send_lands_sends_no_notice() -> None:
    # Every edit carrying part B fails and its fresh send lands: delivered.
    cli = _Screen(fail_marker="PART-B", edits_only=True)
    r = await _three_part_reply(cli)
    assert "NOTICE" not in _sequence(cli)
    assert "PART-B" in _sequence(cli)
    assert r.delivery_failed is False
