"""A steer-boundary seal that never landed marks its place, like a length rotation.

``on_steer_consumed`` seals the pre-steer segment and then drops its text. When
every send of that seal fails, the next segment opens right below the previous
message, so the gap reads as one continuous answer. The length rotation already
posts ``LOST_PART_NOTICE`` for exactly this case; the steer seal must too.
"""

from __future__ import annotations

import pytest
from discord_helpers import _drop_live_config_snapshot  # noqa: F401
from discord_helpers import FakeClient

from kiro_crew.discord.renderer import LOST_PART_NOTICE, DiscordRenderer
from kiro_crew.discord.transport import DISCORD_CAPABILITIES


@pytest.mark.parametrize("seal_lands", [True, False], ids=["seal-landed", "seal-failed"])
@pytest.mark.asyncio
async def test_a_lost_steer_seal_is_marked(seal_lands: bool) -> None:
    cli = FakeClient()
    r = DiscordRenderer(cli, "chan1", DISCORD_CAPABILITIES, session_key="sk")  # type: ignore[arg-type]
    await r.on_turn_start()
    cli.fail_sends = not seal_lands
    cli.edit_ok = seal_lands
    await r.on_text_chunk("Text written before the steer.")
    await r.on_steer_consumed("do it differently")
    cli.fail_sends = False
    cli.edit_ok = True
    await r.on_text_chunk("Text written after the steer.")
    await r.on_done()

    sent = [text for text, _ in cli.sent]
    assert (LOST_PART_NOTICE in sent) is not seal_lands
    assert any("Text written after the steer." in text for text in sent + [e[1] for e in cli.edits])
