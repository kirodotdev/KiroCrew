"""Every channel-delivery leg redacts through ``redact_via_context``.

A composed host adds credential patterns through its platform context. The
narrow default pair does not know them, so a leg that grades only with the
default would deliver a companion-only credential whole. The sibling channel
sinks (``messaging/egress.py``, the chat mirror, Slack) already use
``redact_via_context``; these pins hold the three proactive legs to the same
grader on the display pass, and on the seam check where the leg chunks
with ``chunk_for_transport`` (the own-conversation leg slices plain text).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.handlers import messaging as mod
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.messaging.renderer import display_safe_for
from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES
from kiro_crew.webex.transport import WEBEX_CAPABILITIES

# Shaped so the default credential pair does not match it: only the fake
# companion grader below knows it.
_SECRET = "corpsess_Zq81LmXv02TtWw"
_TEXT = f"Daily digest is ready. token={_SECRET} done."


def _companion_redactor(text: str) -> str:
    return (text or "").replace(_SECRET, "[REDACTED]")


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


@pytest.fixture
def _companion(monkeypatch):
    """Install the fake companion grader and record what the chunker was handed."""
    seen: list[Any] = []
    real_chunk = mod.chunk_for_transport

    def _spy(text, capabilities, *, redactor=None):
        seen.append(redactor)
        return real_chunk(text, capabilities, redactor=redactor)

    monkeypatch.setattr(mod, "redact_via_context", _companion_redactor)
    monkeypatch.setattr(mod, "chunk_for_transport", _spy)
    with patch("kiro_crew.sel.sel", return_value=MagicMock()):
        yield seen


def test_the_default_pair_does_not_know_the_companion_secret():
    # Guards the fixture: if the default ever learned this shape, the leg tests
    # below would pass without proving the context grader is used.
    assert _SECRET in display_safe_for(_TEXT, TELEGRAM_CAPABILITIES)
    assert _SECRET not in display_safe_for(
        _TEXT, TELEGRAM_CAPABILITIES, redactor=_companion_redactor
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("caps", [TELEGRAM_CAPABILITIES, WEBEX_CAPABILITIES])
async def test_owner_dm_leg_redacts_with_the_context_grader(caps, _companion):
    transport = _Transport(caps)
    state = MagicMock()
    state.get_channel_transport.return_value = transport
    link = ChannelLink(channel_type="telegram", channel_id="user:42")

    with patch.object(chat_runner, "_resolve_channel_target", return_value=(link, transport)):
        sent, code, _ = await mod._deliver_channel_dm(state, "telegram", _TEXT, caller_session="")

    assert (sent, code) == (True, "")
    assert transport.sent and all(_SECRET not in part for part in transport.sent)
    assert _companion == [_companion_redactor]


@pytest.mark.asyncio
async def test_configured_target_leg_redacts_with_the_context_grader(monkeypatch, _companion):
    transport = _Transport(TELEGRAM_CAPABILITIES)
    monkeypatch.setattr(mod, "_vet_channel_send", lambda *_a, **_kw: "")
    state = SimpleNamespace(channel_transports={"telegram": transport})

    resp = await mod._send_to_channel_target(state, "telegram", "user:42", _TEXT)

    assert resp.status == 200
    assert transport.sent and all(_SECRET not in part for part in transport.sent)
    assert _companion == [_companion_redactor]


@pytest.mark.asyncio
async def test_own_conversation_leg_redacts_with_the_context_grader(_companion):
    transport = _Transport(TELEGRAM_CAPABILITIES)
    link = ChannelLink(channel_type="telegram", channel_id="conv-42")
    state = MagicMock()
    state.sessions.get_origin_link.return_value = link

    with patch.object(chat_runner, "_resolve_channel_target", return_value=(link, transport)):
        ok = await mod._deliver_to_channel(state, "telegram:x", _TEXT, channel_type="telegram")

    assert ok is True
    assert transport.sent and all(_SECRET not in part for part in transport.sent)
