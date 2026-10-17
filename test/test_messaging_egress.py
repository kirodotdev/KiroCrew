"""The unattended channel-egress legs share one redact -> chunk -> send owner.

``messaging.egress`` owns the transform between a vetted channel target and the
wire for the three legs that push text into a non-Slack conversation without a
renderer: the dashboard reply mirror, the proactive completion leg (subagent and
cron output) and the compaction notice. Pinned here:

* the owner's contract: control tags stripped, a display-form credential
  redacted, ``[OPTIONS:]`` numbered only when asked, chunked under the channel
  cap with every part redacted, and the confirmation stop;
* each leg ships exactly the parts the owner produces for the same text, so a
  fix to the transform reaches all three.

Every collaborator is a double: no socket, no gateway, no write outside the
per-test home.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.dashboard import chat_compaction_notice, chat_runner
from kiro_crew.messaging import egress
from kiro_crew.messaging.egress import channel_egress_parts, send_channel_parts
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.slack import gateway as gw

_CAPS = TransportCapabilities(max_message_chars=120)

#: A credential the client reassembles once it renders the emphasis away.
_COLLAPSING = "AKIA**IOSF**ODNN7EXAMPLE"

#: Long enough to need several parts under ``_CAPS``, fenced so a blind slice
#: would orphan the code, with a credential and a trailing control tag.
_LONG = (
    "Summary line.\n\n```text\n"
    + "\n".join(f"log line {i:03d} ok" for i in range(30))
    + f"\n```\n\ntoken {_COLLAPSING} end\n<!-- keep-visible -->"
)


class _Transport:
    """Records every send; *ids* scripts the message id each send returns."""

    def __init__(self, ids: list[str] | None = None) -> None:
        self.capabilities = _CAPS
        self.sent: list[tuple[str, str, Any]] = []
        self._ids = list(ids or [])

    async def send_message(self, conversation_id: str, text: str, thread_id: Any = None) -> str:
        self.sent.append((conversation_id, text, thread_id))
        return self._ids.pop(0) if self._ids else "MSG"

    async def resolve_configured_target(self, target_id: str) -> Any:
        return None

    @property
    def texts(self) -> list[str]:
        return [text for _, text, _ in self.sent]


def _no_credential(parts: list[str]) -> None:
    joined = "\n".join(parts)
    assert "AKIAIOSFODNN7EXAMPLE" not in joined.replace("*", "")


# ─── The owner's contract ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parts_are_stripped_redacted_and_under_the_cap() -> None:
    parts = await channel_egress_parts(_LONG, _CAPS)

    assert len(parts) > 1
    assert all(len(part) <= _CAPS.max_message_chars for part in parts)
    assert "keep-visible" not in "".join(parts)
    _no_credential(parts)
    for part in parts:
        assert part.count("```") % 2 == 0, part


@pytest.mark.asyncio
async def test_options_are_numbered_only_when_asked() -> None:
    text = "Pick one.\n\n[OPTIONS: Ship it | Hold it]"

    plain = "\n".join(await channel_egress_parts(text, _CAPS))
    numbered = "\n".join(await channel_egress_parts(text, _CAPS, number_options=True))

    assert "[OPTIONS:" in plain
    assert "[OPTIONS" not in numbered
    assert "1. Ship it" in numbered and "2. Hold it" in numbered


@pytest.mark.asyncio
async def test_every_part_goes_to_the_one_conversation_and_thread() -> None:
    tr = _Transport()

    ok = await send_channel_parts(tr, "C1", ["a", "b", "c"], thread_id="T1")

    assert ok is True
    assert tr.sent == [("C1", "a", "T1"), ("C1", "b", "T1"), ("C1", "c", "T1")]


@pytest.mark.asyncio
async def test_an_unconfirmed_part_stops_the_send_only_when_confirmation_is_required() -> None:
    strict = _Transport(ids=["M1", "", "M3"])
    lenient = _Transport(ids=["M1", "", "M3"])

    assert (
        await send_channel_parts(
            strict, "C1", ["a", "b", "c"], thread_id=None, require_confirmation=True
        )
        is False
    )
    assert await send_channel_parts(lenient, "C1", ["a", "b", "c"], thread_id=None) is True

    assert strict.texts == ["a", "b"]
    assert lenient.texts == ["a", "b", "c"]


# ─── Each leg ships the owner's parts ────────────────────────────────────


@pytest.mark.asyncio
async def test_the_dashboard_mirror_ships_the_owners_parts() -> None:
    expected = await channel_egress_parts(_LONG, _CAPS, number_options=True)
    tr = _Transport()
    link = ChannelLink("telegram", channel_id="C1", thread_id="T1")

    with (
        patch.object(chat_runner, "mirror_is_paused", return_value=False),
        patch(
            "kiro_crew.dashboard.session_control._read_mirror_binding",
            return_value=(link, None),
        ),
        patch("kiro_crew.dashboard.session_control.publication_withheld", return_value=False),
        patch.object(chat_runner, "_resolve_channel_target", return_value=(link, tr)),
    ):
        await chat_runner._deliver_cross_surface_reply(MagicMock(), "dashboard:x", _LONG)

    assert tr.texts == expected
    assert {(c, t) for c, _, t in tr.sent} == {("C1", "T1")}


@pytest.mark.asyncio
async def test_the_completion_leg_ships_the_owners_parts() -> None:
    expected = await channel_egress_parts(_LONG, _CAPS)
    tr = _Transport()
    link = ChannelLink("discord", channel_id="C1", thread_id="T1")
    orch: Any = SimpleNamespace(dashboard_state=MagicMock())

    with patch.object(gw, "_resolve_channel_target", return_value=(link, tr)):
        ok = await gw.GatewayOrchestrator._deliver_channel_reply(
            orch, "discord:kirocrew:direct:U1", _LONG, resolved_link=(link, False)
        )

    assert ok is True
    assert tr.texts == expected
    assert {(c, t) for c, _, t in tr.sent} == {("C1", "T1")}


@pytest.mark.asyncio
async def test_the_compaction_notice_ships_the_owners_parts() -> None:
    expected = await channel_egress_parts(_LONG, _CAPS)
    tr = _Transport()
    link = ChannelLink("discord", channel_id="C1", thread_id="T1")
    state = SimpleNamespace(sessions=MagicMock())
    state.sessions.get_origin_link.return_value = link

    with (
        patch("kiro_crew.dashboard.chat_runner._resolve_channel_target", return_value=(link, tr)),
        patch.object(chat_compaction_notice, "notice_text", return_value=_LONG),
    ):
        await chat_compaction_notice._deliver_via_transport(state, "discord:x", 0.9, success=True)

    assert tr.texts == expected
    assert {(c, t) for c, _, t in tr.sent} == {("C1", "T1")}


@pytest.mark.asyncio
async def test_the_compaction_notice_does_not_swallow_a_composition_error() -> None:
    """An invalid governance ceiling re-raises from the redactor and is never degraded.

    The notice's send stays best-effort, but the redaction step runs outside that
    ``try`` so a ``PlatformCompositionError`` reaches the caller, as it does from
    the module's own governance gate.
    """
    tr = _Transport()
    link = ChannelLink("discord", channel_id="C1", thread_id="T1")
    state = SimpleNamespace(sessions=MagicMock())
    state.sessions.get_origin_link.return_value = link

    def _cannot_compose(_text: str) -> str:
        raise PlatformCompositionError("invalid ceiling")

    with (
        patch("kiro_crew.dashboard.chat_runner._resolve_channel_target", return_value=(link, tr)),
        patch.object(egress, "redact_via_context", _cannot_compose),
        pytest.raises(PlatformCompositionError),
    ):
        await chat_compaction_notice._deliver_via_transport(state, "discord:x", 0.9, success=True)

    assert tr.sent == []
