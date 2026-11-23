"""A bang command's deprecation notice names only a slash command that exists and does the same thing.

``handler._BANG_TO_SLASH`` maps each bang to the ``/kirocrew`` sub-command the
notice offers as its replacement. A Slack slash command arrives with no thread, so
a bang that acts on its own thread (``!stop``, ``!ta``, ``!link-to-dashboard``) has
no replacement and gets no notice.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.slack import handler
from kiro_crew.slack.events_slash import SLASH_REGISTRY

#: The bangs that act on the thread they are typed in.
_THREAD_SCOPED = ("!stop", "!ta", "!link-to-dashboard")


def test_every_named_replacement_is_a_registered_sub_command():
    named = {bang: slash for bang, slash in handler._BANG_TO_SLASH.items() if slash}
    assert named, "the map names no replacement at all, so this test checks nothing"
    missing = {}
    for bang, slash in named.items():
        prefix, _, sub = slash.partition(" ")
        if prefix != "/kirocrew" or sub not in SLASH_REGISTRY:
            missing[bang] = slash
    assert missing == {}


@pytest.mark.parametrize("bang", _THREAD_SCOPED)
def test_a_thread_scoped_bang_is_still_a_bang_with_no_replacement(bang):
    # Still a key: the linked-thread routing reads the keys to know a bang.
    assert bang in handler._BANG_TO_SLASH
    assert handler._BANG_TO_SLASH[bang] is None


def _slack():
    slack = MagicMock()
    slack.post_message = AsyncMock()
    slack.post_blocks = AsyncMock()
    return slack


def _notices(slack) -> list[str]:
    texts = []
    for call in slack.post_blocks.call_args_list:
        for block in call.args[1]:
            for element in block.get("elements", []):
                texts.append(element.get("text", ""))
    return [t for t in texts if "is deprecated" in t]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bang", "owner_fn"),
    [
        ("!stop", "_bang_stop"),
        ("!ta", "_bang_thread_agent"),
        ("!link-to-dashboard", "_bang_link_to_dashboard"),
    ],
)
async def test_a_thread_scoped_bang_posts_no_deprecation_notice(monkeypatch, bang, owner_fn):
    ran = AsyncMock(return_value="")
    monkeypatch.setattr(handler, owner_fn, ran)
    monkeypatch.setattr(handler, "is_allowed_user", lambda _user: True)
    slack = _slack()

    await handler._handle_slash_command(bang, slack, MagicMock(), "C1", "t1", "msg1", "t1", "U1")

    ran.assert_awaited_once()
    assert _notices(slack) == []


@pytest.mark.asyncio
async def test_the_channel_notice_names_the_channels_command(monkeypatch):
    monkeypatch.setattr(handler, "_bang_channel", AsyncMock(return_value=""))
    monkeypatch.setattr(handler, "is_allowed_user", lambda _user: True)
    slack = _slack()

    await handler._handle_slash_command(
        "!channel", slack, MagicMock(), "C1", "t1", "msg1", "t1", "U1"
    )

    assert _notices(slack) == ["⚠️ `!channel` is deprecated. Use `/kirocrew channels` instead."]
