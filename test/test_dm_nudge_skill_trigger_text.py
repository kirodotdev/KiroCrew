"""A DM auto-nudge matches skill triggers on the loop's own message.

``_fire_dm_nudge`` delivers a nudge as a synthetic inbound whose text starts with
Kiro Crew's ``[auto-nudge cycle N]`` header. The Discord and Webex dispatchers
carry the loop's message to ``build_message`` as ``skill_trigger_text``, so a
trigger made of the header's words cannot load a skill into every cycle. A typed
message names no trigger text, so its own text is what triggers read.
"""

from __future__ import annotations

from typing import Any

import pytest
from discord_helpers import _dispatcher as _discord_dispatcher
from discord_helpers import _drop_live_config_snapshot  # noqa: F401
from webex_dispatch_helpers import FakeClient, FakeCtx, FakeProvider, FakeSessions
from webex_dispatch_helpers import _dispatcher as _webex_dispatcher
from webex_dispatch_helpers import _inbound

from kiro_crew.acp.types import EVENT_COMPLETE, AcpEvent
from kiro_crew.messaging.transport import InboundMessage

_TAGGED = "[auto-nudge cycle 1]\nkeep checking"


class _RecordingWebexCtx(FakeCtx):
    def __init__(self) -> None:
        super().__init__()
        self.builds: list[dict[str, Any]] = []

    def build_message(self, text, is_new, key, **kw):
        self.builds.append({"text": text, **kw})
        return super().build_message(text, is_new, key, **kw)


def _record_discord_builds(d: Any) -> list[dict[str, Any]]:
    builds: list[dict[str, Any]] = []
    inner = d.ctx_builder.build_message

    def _build(text: str, is_new: bool, key: str, **kw: Any) -> Any:
        builds.append({"text": text, **kw})
        return inner(text, is_new, key, **kw)

    d.ctx_builder.build_message = _build
    return builds


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [({"skill_trigger_text": "keep checking"}, "keep checking"), ({}, None)],
    ids=["nudge", "typed"],
)
async def test_discord_hands_the_trigger_text_to_the_build(
    kwargs: dict[str, Any], expected: str | None
) -> None:
    d, _cli, _sess = _discord_dispatcher({"u1"})
    builds = _record_discord_builds(d)
    msg = InboundMessage(channel_type="discord", user_id="u1", conversation_id="c1", text=_TAGGED)

    await d.handle_message(msg, interpret_commands=False, **kwargs)

    assert len(builds) == 1
    assert builds[0]["text"] == _TAGGED
    assert builds[0]["skill_trigger_text"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [({"skill_trigger_text": "keep checking"}, "keep checking"), ({}, None)],
    ids=["nudge", "typed"],
)
async def test_webex_hands_the_trigger_text_to_the_build(
    kwargs: dict[str, Any], expected: str | None
) -> None:
    ctx = _RecordingWebexCtx()
    sessions = FakeSessions(FakeProvider([AcpEvent(kind=EVENT_COMPLETE)]))
    d = _webex_dispatcher(sessions, ctx, FakeClient())

    await d.handle_message(_inbound(_TAGGED), interpret_commands=False, **kwargs)

    assert len(ctx.builds) == 1
    assert ctx.builds[0]["text"] == _TAGGED
    assert ctx.builds[0]["skill_trigger_text"] == expected
