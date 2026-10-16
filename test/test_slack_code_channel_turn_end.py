"""The code-channel turn-end hook fires from the turn's session hand-back.

``_turn_release_session`` (chat_turn/session_reset.py) is where a turn hands its
session back on every exit path. A turn whose start notified the code-channel
hooks must notify their end exactly once there, before the permit is released,
and a failing hook must not skip that release.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_turn.session_reset import _turn_release_session


def _turn(
    *, code_channel_turn: bool, stop_reason: str, channel_origin: bool, acquired: bool = False
):
    released: list[str] = []
    sessions = SimpleNamespace(release=released.append)
    state = SimpleNamespace(slack_client=None, sessions=sessions)
    slot = SimpleNamespace(_turn_channel_origin=channel_origin, _active_turn_session_key="chat-1-1")
    turn = SimpleNamespace(
        _acquired=acquired,
        _code_channel_turn=code_channel_turn,
        _deferred_options_expiry=False,
        _mirror_active_task="",
        _mirror_active_task_title="",
        _mirror_chan="C0CODE",
        _mirror_stream_ts="",
        _mirror_thread="300.000001",
        _seen_tool_identity=set(),
        _turn_started=0.0,
        _stop_reason=stop_reason,
        needs_session_reset=False,
        needs_conversation_discard=False,
        session_key="chat-1-1",
        slot=slot,
        state=state,
    )
    return turn, released


@pytest.fixture
def ends(monkeypatch):
    calls: list[tuple] = []

    async def _notify(chan, thread, session_key, *, source, outcome):
        calls.append((chan, thread, session_key, source, outcome))

    async def _no_report(*_a, **_k):
        return None

    monkeypatch.setattr(chat_runner, "notify_code_channel_turn_end", _notify)
    monkeypatch.setattr(chat_runner, "_report_unclaimed_directives", _no_report)
    return calls


@pytest.mark.parametrize(
    ("stop_reason", "outcome"),
    [
        (chat_runner.STOP_REASON_END_TURN, "ok"),
        (chat_runner.STOP_REASON_CANCELLED, "stopped"),
        ("max_tokens", "error"),
    ],
)
def test_a_code_channel_turn_notifies_its_end_once(ends, stop_reason, outcome):
    turn, _ = _turn(code_channel_turn=True, stop_reason=stop_reason, channel_origin=True)
    asyncio.run(_turn_release_session(turn))
    asyncio.run(_turn_release_session(turn))
    assert ends == [("C0CODE", "300.000001", "chat-1-1", "slack", outcome)]
    assert turn._code_channel_turn is False


def test_a_dashboard_origin_turn_reports_dashboard_as_source(ends):
    turn, _ = _turn(
        code_channel_turn=True, stop_reason=chat_runner.STOP_REASON_END_TURN, channel_origin=False
    )
    asyncio.run(_turn_release_session(turn))
    assert [c[3] for c in ends] == ["dashboard"]


def test_an_ordinary_turn_notifies_nothing(ends):
    turn, _ = _turn(
        code_channel_turn=False, stop_reason=chat_runner.STOP_REASON_END_TURN, channel_origin=True
    )
    asyncio.run(_turn_release_session(turn))
    assert ends == []


def test_a_failing_end_hook_still_releases_the_session(monkeypatch, ends):
    async def _boom(*_a, **_k):
        raise RuntimeError("hook failed")

    monkeypatch.setattr(chat_runner, "notify_code_channel_turn_end", _boom)
    turn, released = _turn(
        code_channel_turn=True,
        stop_reason=chat_runner.STOP_REASON_END_TURN,
        channel_origin=True,
        acquired=True,
    )
    asyncio.run(_turn_release_session(turn))
    assert released == ["chat-1-1"]
    assert turn._code_channel_turn is False
