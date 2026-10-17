"""An ``[OPTIONS:]`` trailer on a proactive send arrives as numbered text.

The proactive legs (a cron's ``send_message``, ``ctx.notify``, the owner DM, a
configured channel target) and the cross-surface reply mirror hand plain text to
``transport.send_message``, which takes no choices. So the trailer must be
rewritten before it gets there, or the user reads raw protocol text. Pinned on
every leg and on every channel that renders buttons on a reply, because those
are the ones whose own renderer would otherwise keep the choices for a widget
this path never builds.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.handlers import messaging as mod
from kiro_crew.discord.transport import DISCORD_CAPABILITIES
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.messaging.renderer import render_options_as_text
from kiro_crew.teams.transport import TEAMS_CAPABILITIES
from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES
from kiro_crew.webex.transport import WEBEX_CAPABILITIES

_TEXT = "Daily digest is ready.\n\n[OPTIONS: Show it | Skip today]"

_WIDGET_CHANNELS = [
    pytest.param("telegram", TELEGRAM_CAPABILITIES, id="telegram"),
    pytest.param("discord", DISCORD_CAPABILITIES, id="discord"),
    pytest.param("teams", TEAMS_CAPABILITIES, id="teams"),
    pytest.param("webex", WEBEX_CAPABILITIES, id="webex"),
]


class _Transport:
    def __init__(self, capabilities: Any) -> None:
        self.capabilities = capabilities
        self.sent: list[str] = []

    def configured_targets(self) -> list[Any]:
        return [SimpleNamespace(target_id="user:42", available=True, label="DM")]

    async def resolve_configured_target(self, target_id: str):
        return ("conv-42", None)

    def may_send_to(self, conversation_id: str, thread_id=None, *, principal: str = "") -> bool:
        return conversation_id == "conv-42"

    async def send_message(self, conversation_id: str, text: str, thread_id=None) -> str:
        self.sent.append(text)
        return "MSG1"


def _assert_numbered(sent: list[str]) -> None:
    joined = "\n".join(sent)
    assert "[OPTIONS" not in joined
    assert joined.startswith("Daily digest is ready.")
    assert "1. Show it" in joined
    assert "2. Skip today" in joined


@pytest.fixture
def _quiet_sel():
    with patch("kiro_crew.sel.sel", return_value=MagicMock()):
        yield


@pytest.mark.asyncio
@pytest.mark.parametrize(("channel", "caps"), _WIDGET_CHANNELS)
async def test_configured_target_leg_numbers_the_choices(channel, caps, monkeypatch, _quiet_sel):
    transport = _Transport(caps)
    monkeypatch.setattr(mod, "_vet_channel_send", lambda *_a, **_kw: "")
    state = SimpleNamespace(channel_transports={channel: transport})

    resp = await mod._send_to_channel_target(state, channel, "user:42", _TEXT)

    assert resp.status == 200
    _assert_numbered(transport.sent)


@pytest.mark.asyncio
@pytest.mark.parametrize(("channel", "caps"), _WIDGET_CHANNELS)
async def test_owner_dm_leg_numbers_the_choices(channel, caps, _quiet_sel):
    transport = _Transport(caps)
    state = MagicMock()
    state.get_channel_transport.return_value = transport
    link = ChannelLink(channel_type=channel, channel_id="user:42")

    with patch.object(chat_runner, "_resolve_channel_target", return_value=(link, transport)):
        sent, code, _ = await mod._deliver_channel_dm(state, channel, _TEXT, caller_session="")

    assert (sent, code) == (True, "")
    _assert_numbered(transport.sent)


@pytest.mark.asyncio
@pytest.mark.parametrize(("channel", "caps"), _WIDGET_CHANNELS)
async def test_own_conversation_leg_numbers_the_choices(channel, caps, _quiet_sel):
    transport = _Transport(caps)
    link = ChannelLink(channel_type=channel, channel_id="conv-42")
    state = MagicMock()
    state.sessions.get_origin_link.return_value = link

    with patch.object(chat_runner, "_resolve_channel_target", return_value=(link, transport)):
        ok = await mod._deliver_to_channel(state, f"{channel}:x", _TEXT, channel_type=channel)

    assert ok is True
    _assert_numbered(transport.sent)


@pytest.mark.asyncio
@pytest.mark.parametrize(("channel", "caps"), _WIDGET_CHANNELS)
async def test_cross_surface_reply_mirror_numbers_the_choices(channel, caps):
    transport = _Transport(caps)
    link = ChannelLink(channel_type=channel, channel_id="conv-42")
    state = MagicMock()

    with (
        patch.object(chat_runner, "mirror_is_paused", return_value=False),
        patch(
            "kiro_crew.dashboard.session_control._read_mirror_binding",
            return_value=(link, None),
        ),
        patch("kiro_crew.dashboard.session_control.publication_withheld", return_value=False),
        patch.object(chat_runner, "_resolve_channel_target", return_value=(link, transport)),
    ):
        await chat_runner._deliver_cross_surface_reply(state, "dashboard:x", _TEXT)

    _assert_numbered(transport.sent)


@pytest.mark.asyncio
async def test_a_trailing_control_tag_does_not_hide_the_trailer(monkeypatch, _quiet_sel):
    # The display pass strips control comments; the trailer is read after it,
    # so a tag after the marker cannot leave the marker literal.
    transport = _Transport(TELEGRAM_CAPABILITIES)
    monkeypatch.setattr(mod, "_vet_channel_send", lambda *_a, **_kw: "")
    state = SimpleNamespace(channel_transports={"telegram": transport})

    resp = await mod._send_to_channel_target(
        state, "telegram", "user:42", _TEXT + "\n<!-- keep-visible -->"
    )

    assert resp.status == 200
    _assert_numbered(transport.sent)


class TestRenderOptionsAsText:
    def test_text_without_a_marker_is_unchanged(self) -> None:
        text = "no choices here, see the [OPTIONS section"
        assert render_options_as_text(text, TELEGRAM_CAPABILITIES) == text

    def test_an_empty_marker_is_removed(self) -> None:
        assert render_options_as_text("Done.\n[OPTIONS: ]", TELEGRAM_CAPABILITIES) == "Done."

    def test_a_marker_alone_becomes_the_list(self) -> None:
        out = render_options_as_text("[OPTIONS: Yes | No]", TELEGRAM_CAPABILITIES)
        assert "[OPTIONS" not in out
        assert out.startswith("1. Yes")

    def test_every_choice_is_kept_on_a_widget_channel(self) -> None:
        # Teams shows 5 buttons on a reply. This function returns the body only,
        # so keeping any choice for a widget would drop it: all 7 are numbered.
        labels = [f"c{i}" for i in range(7)]
        out = render_options_as_text(
            "Pick.\n\n[OPTIONS: " + " | ".join(labels) + "]", TEAMS_CAPABILITIES
        )
        for i, label in enumerate(labels, start=1):
            assert f"{i}. {label}" in out
