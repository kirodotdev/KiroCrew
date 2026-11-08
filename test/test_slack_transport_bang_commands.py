"""``!`` commands and ``/agent <name>`` on the default transport path.

``messaging.use_transport`` defaults on, so ``handle_message_transport`` is where a
Slack message lands on a fresh install. It must route ``!ta``, ``!yolo`` and the other
``!`` commands through the same ``_route_bang_command`` gate the native path uses: a
command is answered (or refused) without an LLM session, and a plain message still
runs the turn.

The per-thread agent map is NOT patched on the transport module: transport and handler
share one ``_thread_agents`` dict, which is what lets a ``!ta`` set here pick the agent
of the thread's next turn. ``_clean_slack_thread_state`` (``conftest_slack``) clears it
and the thread-slot pins around every test.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_transport_dispatch_helpers import (
    _MSG_TS,
    RecordingSlackClient,
    ScriptedProvider,
    _CapturingSessions,
    make_event,
)

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, STOP_REASON_END_TURN
from kiro_crew.messaging.link import canonical_key
from kiro_crew.slack import handler as _handler
from kiro_crew.slack import transport_dispatch

_OWNER = "U_OWNER"
_MEMBER = "U_MEMBER"
_KEY = canonical_key(_MSG_TS)


class _RemovingSessions(_CapturingSessions):
    """Records the sessions a thread-agent switch drops so the next turn respawns."""

    def __init__(self, provider):
        super().__init__(provider)
        self.removed: list[str] = []

    async def remove(self, session_key):
        self.removed.append(session_key)


@pytest.fixture
def audit(monkeypatch):
    """The owner, an isolated hydration guard, and a recorder in place of the SEL."""
    monkeypatch.setattr(_handler, "_owner_id", _OWNER)
    monkeypatch.setattr(_handler, "_hydrated_sessions", set())
    monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "kirocrew")
    monkeypatch.setattr(
        transport_dispatch, "_hydrate_thread_overrides", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
    recorder = MagicMock()
    monkeypatch.setattr(_handler, "sel", lambda: recorder)
    return recorder


def _sessions() -> _RemovingSessions:
    return _RemovingSessions(
        ScriptedProvider(
            [
                make_event(EVENT_TEXT_CHUNK, text="hi"),
                make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
            ]
        )
    )


async def _send(slack, sessions, text, *, user_id=_OWNER, thread_ts=None, msg_ts=_MSG_TS):
    await transport_dispatch.handle_message_transport(
        slack=slack,
        sessions=sessions,
        channel="D1",
        text=text,
        thread_ts=thread_ts,
        msg_ts=msg_ts,
        user_id=user_id,
        context_builder=None,
        conversation_log=None,
    )


def _run(text, *, user_id=_OWNER):
    slack, sessions = RecordingSlackClient(), _sessions()
    asyncio.run(_send(slack, sessions, text, user_id=user_id))
    return slack, sessions


def _posts(slack) -> list[str]:
    return [kw["text"] for (method, kw) in slack.transcript if method == "post_message"]


@pytest.mark.parametrize("text", ["!ta", "<@UBOT> !ta"])
def test_a_bare_thread_agent_command_is_answered_without_a_turn(audit, text):
    slack, sessions = _run(text)

    assert sessions.agents == []
    assert any(p.startswith("No thread agent set.") for p in _posts(slack)), _posts(slack)


@pytest.mark.parametrize("text", ["!ta reviewer", "/agent reviewer"])
def test_a_thread_agent_switch_picks_the_agent_of_the_next_turn(audit, monkeypatch, text):
    monkeypatch.setattr(_handler, "_resolve_agent_name", lambda name, project=None: name)
    slack, sessions = RecordingSlackClient(), _sessions()

    async def _scenario():
        await _send(slack, sessions, text)
        assert sessions.agents == []
        assert _handler._thread_agents.get(_KEY) == "reviewer"
        await _send(slack, sessions, "hello", thread_ts=_MSG_TS, msg_ts="1700000000.000200")

    asyncio.run(_scenario())

    assert sessions.removed == [_KEY]
    assert "🔄 Thread agent: *reviewer*" in _posts(slack)
    assert sessions.agents == ["reviewer"]


def test_an_owner_command_from_a_non_owner_is_refused_and_audited(audit):
    slack, sessions = _run("!yolo on", user_id=_MEMBER)

    assert sessions.agents == []
    assert "⛔ Owner-only command." in _posts(slack)
    audit.log_api_access.assert_any_call(
        caller=_MEMBER,
        operation="slack.owner_command",
        outcome="denied",
        source="slack",
        resources="!yolo",
        error="unauthorized sender",
    )


def test_a_plain_message_still_runs_the_turn(audit):
    _slack, sessions = _run("hello there")

    assert sessions.agents == ["kirocrew"]


def test_voice_on_opts_the_thread_in_as_on_the_native_path(audit, monkeypatch):
    """A finished transport turn is spoken, so ``!voice on`` opts the thread in here
    exactly as it does natively: it reports Voice ON and runs no turn."""
    monkeypatch.setattr(_handler._vc, "sessions", set())
    slack, sessions = _run("!voice on")

    assert sessions.agents == []
    assert _KEY in _handler._vc.sessions
    posts = _posts(slack)
    assert any(p.startswith("\U0001f50a Voice ON") for p in posts), posts
    assert not any("not available here" in p for p in posts), posts
