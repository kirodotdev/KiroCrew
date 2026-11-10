"""A Discord reasoning note's redactions count only when the note landed.

``_tally_redactions`` feeds the turn's one redaction notice, and its contract is
to count LANDED text. A reasoning note whose send failed was still counted, so
the reader was told a secret had been redacted from text they never got.
"""

from __future__ import annotations

from typing import Any

import pytest
from discord_helpers import _drop_live_config_snapshot  # noqa: F401
from discord_helpers import FakeClient

from kiro_crew.discord.renderer import DiscordRenderer
from kiro_crew.discord.transport import DISCORD_CAPABILITIES

_KEY = "AKIAIOSFODNN7EXAMPLE"


class _NoteSendFails(FakeClient):
    """Every send lands except the ``💭`` reasoning note."""

    async def send_message(self, channel_id: str, text: str, **kw: Any) -> Any:
        mid = await super().send_message(channel_id, text, **kw)
        return None if "💭" in text else mid


@pytest.mark.parametrize("note_lands", [True, False], ids=["note-landed", "note-failed"])
@pytest.mark.asyncio
async def test_the_redaction_notice_follows_the_note_that_landed(note_lands: bool) -> None:
    cli = FakeClient() if note_lands else _NoteSendFails()
    r = DiscordRenderer(
        cli, "chan1", DISCORD_CAPABILITIES, session_key="sk", show_thinking=True  # type: ignore[arg-type]
    )
    await r.on_turn_start()
    await r.on_text_chunk("The answer.")
    await r.on_thinking(f"the key is {_KEY}")
    await r.on_done()

    sent = [text for text, _ in cli.sent]
    assert all(_KEY not in text for text in sent)
    notices = [text for text in sent if text.startswith("Security notice:")]
    assert bool(notices) is note_lands
