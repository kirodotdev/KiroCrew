"""WeCom never names a learned peer as the channel's owner.

Under ``wecom.allow_all_users`` every warm peer is offered as a target, because
there is no configured list to draw on. Messaging the bot once must not make a
stranger the owner an owner-DM path reaches, so those peers are ``learned`` and
``sole_direct_target`` skips them.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.messaging.transport import ConfiguredChannelTarget, sole_direct_target
from kiro_crew.wecom.transport import WeComTransport


def _wecom(*, warm: list[str], allowed: tuple[str, ...] = (), owner_id: str = "") -> WeComTransport:
    transport = WeComTransport(
        MagicMock(), allowed_users=allowed, allow_all=True, owner_id=owner_id
    )
    for chat_id in warm:
        transport._warm_chats[chat_id] = None
    return transport


def _record_sends(transport: WeComTransport) -> list[tuple[str, str]]:
    """Replace the network send with one that keeps each (conversation, text) pair."""
    sent: list[tuple[str, str]] = []

    async def _send(conversation_id: str, text: str, *_rest: object, **_kw: object) -> str:
        sent.append((conversation_id, text))
        return ""

    transport.send_message = _send  # type: ignore[method-assign]
    return sent


def _targets(transport: WeComTransport) -> dict[str, ConfiguredChannelTarget]:
    return {t.target_id: t for t in transport.configured_targets()}


def test_a_peer_who_only_messaged_the_bot_is_offered_but_is_not_the_owner():
    transport = _wecom(warm=["stranger"])
    targets = _targets(transport)
    assert targets["user:stranger"].available and targets["user:stranger"].learned
    assert sole_direct_target(transport.configured_targets()) == ""


def test_the_configured_owner_is_named_beside_learned_peers():
    transport = _wecom(warm=["boss", "stranger"], owner_id="boss")
    targets = _targets(transport)
    assert not targets["user:boss"].learned and targets["user:stranger"].learned
    assert sole_direct_target(transport.configured_targets()) == "user:boss"


def test_an_allow_listed_user_is_never_learned():
    transport = _wecom(warm=["alice"], allowed=("alice",))
    assert not _targets(transport)["user:alice"].learned
    assert sole_direct_target(transport.configured_targets()) == "user:alice"


@pytest.mark.parametrize(("allowed", "owner_id"), [(("alice", "bob"), ""), (("alice",), "bob")])
def test_a_configured_user_who_has_not_written_keeps_the_owner_unnamed(allowed, owner_id):
    transport = WeComTransport(MagicMock(), allowed_users=allowed, owner_id=owner_id)
    transport._warm_chats["alice"] = None
    assert not _targets(transport)["user:bob"].available
    assert sole_direct_target(transport.configured_targets()) == ""


def test_sole_direct_target_skips_any_learned_target():
    targets = [
        ConfiguredChannelTarget("user:a", "a", learned=True),
        ConfiguredChannelTarget("user:b", "b"),
    ]
    assert sole_direct_target(targets) == "user:b"
    assert sole_direct_target(targets[:1]) == ""


@pytest.mark.asyncio
async def test_the_owner_notice_never_reaches_a_learned_peer():
    """The cross-channel owner notice counts configured people, never learned ones."""
    from kiro_crew.dashboard import server

    transport = _wecom(warm=["stranger"])
    sent = _record_sends(transport)
    state = SimpleNamespace(channel_transports={"wecom": transport})
    await server._notify_owner_channels(state, "override expired")
    assert sent == []


@pytest.mark.asyncio
async def test_the_owner_notice_reaches_the_configured_owner_beside_learned_peers():
    from kiro_crew.dashboard import server

    transport = _wecom(warm=["boss", "stranger"], owner_id="boss")
    sent = _record_sends(transport)
    state = SimpleNamespace(channel_transports={"wecom": transport})
    await server._notify_owner_channels(state, "override expired")
    assert sent == [("boss", "override expired")]
