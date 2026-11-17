"""A shared user (slack.shared_access) never stamps, presses or releases a code channel's
Stop hold (``stop_hold``; RFC ``rfc-slack-code-channels`` §3 "Stop").

Code channels stay the owner's: each Slack path a shared user's message, command,
button or linked thread takes is refused or dropped before a person's stamp is
minted, before a press, and before any hold is released.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from slack_code_channel_surface_helpers import (  # noqa: F401 - fixtures by name
    CH,
    SLOT,
    _bind,
    _slash,
    env_fixture,
)

# Imported here, never under a patch: a module first imported while a name is
# patched binds the patched value for the rest of the session.
from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot
from kiro_crew.slack import events
from kiro_crew.slack.code_channels import commands, routing, stop_helpers, stop_hold
from kiro_crew.slack.handler_runtime.inbound import LinkedRoute, route_linked_thread

SHARED = "U0SHARED01"

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_holds() -> Any:
    stop_hold._keys.clear()
    stop_hold._channels.clear()
    yield
    stop_hold._keys.clear()
    stop_hold._channels.clear()


def _as_shared() -> tuple[Any, ...]:
    """SHARED is a confirmed, listed shared user (never the owner), as every module
    that imported the names reads them."""
    return tuple(
        patch(f"{mod}.{name}", lambda u: u == SHARED)
        for mod in ("kiro_crew.slack.handler", "kiro_crew.slack.events")
        for name in ("is_shared_user", "is_shared_listed")
    )


def _held(key: str) -> Any:
    slot = _ChatSlot(key)
    stop_hold.Pressed(next(stop_hold._presses)).hold(slot)
    stop_hold._last = slot._stop_hold_seq
    return slot


def _still_held(slot: Any) -> None:
    assert slot._stop_held is True
    assert slot.key in stop_hold._keys
    assert stop_hold.decide(slot, "") == stop_hold.HELD


class _Reached(Exception):
    """The message went on past the code channel route (plain routing)."""


@pytest.mark.parametrize(
    ("sender", "channel"), [(SHARED, "C0CODE"), (SHARED, "D0DM00001"), ("UOWNER", "C0CODE")]
)
async def test_a_shared_users_message_mints_no_stamp_and_releases_nothing(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, sender: str, channel: str
) -> None:
    """``_route_message``: a shared user's message in a code channel is dropped, and
    in a DM it skips the code channel route, whose call is the only place a
    message's stamp is minted; the held session stays held. The owner's message on
    the same path carries a stamp (the control)."""
    from slack_code_channels_helpers import OWNER, make_env

    from kiro_crew.slack.code_channels.store import Binding
    from kiro_crew.slack.events import SeenCache, _route_message

    for env in make_env(monkeypatch, tmp_path):
        env.store.put_binding(
            Binding(
                channel_id="C0CODE", team_id="T1", session_key="chat-9-1", anchor_ts="300.000001"
            )
        )
        env.info("C0CODE")
        slot = _held("chat-9-1")
        handed: list[str] = []

        async def dispatch(*_a: Any, **kw: Any) -> bool:
            handed.append(kw.get("stop_hold_stamp", ""))
            return True

        def past_the_route(_paths: Any) -> None:
            raise _Reached

        monkeypatch.setattr(events, "code_channel_dispatch", dispatch)
        monkeypatch.setattr(events, "set_linked_thread_files", past_the_route)
        from test_slack_code_channels_events import _orch

        orch = _orch(env)
        dm = channel.startswith("D")  # where a shared user drives work as the owner
        event = {
            "type": "message",
            "user": sender,
            "channel": channel,
            "ts": "301.000001",
            "text": "now add a regression test",
            "team": "T1",
        }
        reached = False
        a, b, c, d = _as_shared()
        owner = patch("kiro_crew.slack.events.is_allowed_user", lambda u: u == OWNER)
        with a, b, c, d, owner:
            try:
                await _route_message(orch, event, SeenCache(), stop_seen=stop_hold.seen())
            except _Reached:
                reached = True
        if sender == OWNER:
            assert len(handed) == 1 and stop_hold.decide(slot, handed[0]) == stop_hold.RELEASE
            return
        assert handed == [] and env.stamps == []
        assert reached is dm  # past the route in a DM, dropped in the code channel
        _still_held(slot)


async def test_a_shared_users_typed_stop_in_a_code_channel_presses_nothing(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``!stop`` typed in a bound code channel: dropped before the inbound gate's
    ``_stop_bound``, so a shared user presses no hold."""
    from slack_code_channels_helpers import OWNER, make_env

    from kiro_crew.slack.code_channels.store import Binding
    from kiro_crew.slack.events import SeenCache, _route_message

    for env in make_env(monkeypatch, tmp_path):
        env.store.put_binding(
            Binding(
                channel_id="C0CODE", team_id="T1", session_key="chat-9-1", anchor_ts="300.000001"
            )
        )
        env.info("C0CODE")
        pressed: list[str] = []

        async def stop_channel(*_a: Any, **_kw: Any) -> str:
            pressed.append("pressed")
            return ""

        monkeypatch.setattr(stop_helpers, "stop_channel", stop_channel)
        from test_slack_code_channels_events import _orch

        event = {"type": "message", "user": SHARED, "channel": "C0CODE", "text": "!stop"}
        a, b, c, d = _as_shared()
        owner = patch("kiro_crew.slack.events.is_allowed_user", lambda u: u == OWNER)
        with a, b, c, d, owner:
            await _route_message(
                _orch(env), {**event, "ts": "302.000001", "team": "T1"}, SeenCache()
            )
            assert pressed == [] and stop_hold._keys == {}
            # The control: the owner's typed ``!stop`` on the same path presses.
            await _route_message(
                _orch(env), {**event, "user": OWNER, "ts": "302.000002", "team": "T1"}, SeenCache()
            )
        assert pressed == ["pressed"]


async def test_a_shared_users_code_channel_command_mints_no_stamp(env: Any) -> None:
    """``commands.handle_slash``: the stamp is minted only after ``is_allowed_user``,
    which never admits a shared user."""
    _bind(env)
    slot = _held(SLOT)
    replies: list[str] = []

    async def respond(text: str) -> None:
        replies.append(text)

    a, b, c, d = _as_shared()
    with a, b, c, d:
        for i, command in enumerate(("/summarize", "/wrap-up", "/create-pr")):
            slash = _slash(command, user=SHARED, trigger=f"T-{i}")
            assert await commands.handle_slash(env.orch, slash, respond, stop_seen=stop_hold.seen())
    assert env.routed == [] and env.stamps == [] and len(replies) == 3
    _still_held(slot)


async def test_a_shared_users_channel_stop_presses_nothing(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The context bar's Stop (and a typed ``!stop`` with no client): refused before
    ``stop_channel``, so a shared user presses no hold."""
    _bind(env)
    pressed: list[str] = []

    async def stop_channel(*_a: Any, **_kw: Any) -> str:
        pressed.append("pressed")
        return ""

    monkeypatch.setattr(stop_helpers, "stop_channel", stop_channel)
    last = stop_hold._last
    a, b, c, d = _as_shared()
    with a, b, c, d:
        outcome = await routing.stop_from_channel(env.orch, {"channel": CH}, SHARED)
    assert outcome is not None and pressed == []
    assert stop_hold._last == last and stop_hold._keys == {}


async def test_a_shared_users_linked_thread_never_releases_a_held_session(env: Any) -> None:
    """``route_linked_thread``: a shared user's message into a code channel's held
    session is refused before the hold is asked, even with a stamp that would release."""
    from kiro_crew.slack import handler

    _bind(env)
    slot = _held(SLOT)
    releasing = stop_hold.stamp(stop_hold.seen(), True)
    assert stop_hold.decide(slot, releasing) == stop_hold.RELEASE
    state = SimpleNamespace(get_linked_slot=lambda _ts: slot)
    a, b, c, d = _as_shared()
    with a, b, c, d, patch.object(handler, "_dashboard_state", state):
        for channel in ("D0DM00001", CH):
            outcome = await route_linked_thread(
                "now add a regression test",
                "slack:555.000001",
                SHARED,
                channel,
                env.slack,
                "555.000001",
                stop_hold_stamp=releasing,
            )
            assert outcome is LinkedRoute.REFUSED, channel
    _still_held(slot)
