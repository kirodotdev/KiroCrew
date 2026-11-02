"""The Hooks page's UserPromptSubmit and Stop hooks on a Slack turn.

Both Slack paths run them: the transport path (``transport_dispatch.py``) through the
shared ``TurnDriver``, and the native path (``handler_runtime/turn.py``, review-mode
channels and ``messaging.use_transport = false``) through ``slack/script_hooks.py``.
Only the operator's own 1:1 DM runs them.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from slack_handler_helpers import FakeProvider, FakeSessionManager
from slack_transport_dispatch_helpers import (
    _MSG_TS,
    RecordingSlackClient,
    ScriptedProvider,
    _CapturingSessions,
    make_event,
)

from conftest import MockSlackClient
from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, STOP_REASON_END_TURN
from kiro_crew.hooks import HOOK_EVENT_STOP, HOOK_EVENT_USER_PROMPT_SUBMIT
from kiro_crew.messaging import script_hooks as messaging_hooks
from kiro_crew.providers.base import LLMEvent
from kiro_crew.slack import handler as slack_handler
from kiro_crew.slack import script_hooks, transport_dispatch

OWNER = "U_OWNER"


def _result(stdout=""):
    return SimpleNamespace(
        hook_name="probe", exit_code=0, stdout=stdout, stderr="", refused=False, failure_detail=""
    )


class _Store:
    """A hook store that records each fire; *on_prompt* runs inside UserPromptSubmit."""

    def __init__(self, prompt_stdout="", on_prompt=None):
        self.prompt_stdout = prompt_stdout
        self.on_prompt = on_prompt
        self.calls: list[tuple[str, str, str | None]] = []

    async def fire(self, event, context="", *, session_key=None, **_kw):
        self.calls.append((event, context, session_key))
        if event == HOOK_EVENT_USER_PROMPT_SUBMIT:
            if self.on_prompt is not None:
                self.on_prompt()
            return [_result(self.prompt_stdout)] if self.prompt_stdout else []
        return []


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setattr(slack_handler, "is_allowed_user", lambda user: user == OWNER)


def _install(monkeypatch, store):
    monkeypatch.setattr(messaging_hooks, "get_global_hook_store", lambda: store)


@pytest.mark.parametrize(
    ("channel", "sender", "runs"),
    [
        ("D1", OWNER, True),
        ("C1", OWNER, False),  # a channel others read
        ("G1", OWNER, False),  # a group DM
        ("D1", "B_TRUSTED_BOT", False),  # a trusted bot is not the operator
        ("D1", "", False),
    ],
)
def test_only_the_operators_own_dm_runs_hooks(owner, channel, sender, runs):
    assert script_hooks.runs_script_hooks(channel, sender) is runs


# ── transport path ────────────────────────────────────────────────────────────


class _RecordingProvider(ScriptedProvider):
    def __init__(self, events):
        super().__init__(events)
        self.prompts: list[str] = []

    async def stream(self, message, *args, **kwargs):
        self.prompts.append(message)
        async for event in super().stream(message, *args, **kwargs):
            yield event


def _run_transport(monkeypatch, *, channel, user_id=OWNER):
    monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "")
    monkeypatch.setattr(
        transport_dispatch, "_hydrate_thread_overrides", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
    monkeypatch.setattr(transport_dispatch, "_thread_agents", {})
    provider = _RecordingProvider(
        [
            make_event(EVENT_TEXT_CHUNK, text="All done."),
            make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
    )
    sessions = _CapturingSessions(provider)
    asyncio.run(
        transport_dispatch.handle_message_transport(
            slack=RecordingSlackClient(),
            sessions=sessions,
            channel=channel,
            text="hello",
            thread_ts=None,
            msg_ts=_MSG_TS,
            user_id=user_id,
            context_builder=None,
            conversation_log=None,
        )
    )
    return provider


def test_transport_dm_turn_fires_prompt_submit_then_stop(monkeypatch, owner):
    store = _Store(prompt_stdout="ticket 7")
    _install(monkeypatch, store)
    provider = _run_transport(monkeypatch, channel="D1")
    assert [(event, context) for event, context, _key in store.calls] == [
        (HOOK_EVENT_USER_PROMPT_SUBMIT, "hello"),
        (HOOK_EVENT_STOP, "All done."),
    ]
    assert provider.prompts[0].startswith("[Hook context]\nticket 7\n")


@pytest.mark.parametrize(("channel", "user_id"), [("C1", OWNER), ("D1", "B_TRUSTED_BOT")])
def test_transport_turn_outside_the_operators_dm_fires_nothing(
    monkeypatch, owner, channel, user_id
):
    store = _Store(prompt_stdout="ticket 7")
    _install(monkeypatch, store)
    provider = _run_transport(monkeypatch, channel=channel, user_id=user_id)
    assert store.calls == []
    assert provider.prompts == ["hello"]


# ── native path ───────────────────────────────────────────────────────────────


class _NativeProvider(FakeProvider):
    def __init__(self, chunks=("All done.",)):
        super().__init__([LLMEvent(kind="text_chunk", text=chunk) for chunk in chunks])
        self.prompts: list[str] = []

    async def stream(self, message, timeout=120.0):
        self.prompts.append(message)
        async for event in super().stream(message, timeout):
            yield event


class _StoppableSessions(FakeSessionManager):
    def __init__(self, provider):
        super().__init__(provider)
        self.stops = 0

    def stop_generation(self, key):
        return self.stops


def _run_native(channel, sessions, slack=None):
    asyncio.run(
        slack_handler.handle_message(
            slack or MockSlackClient(), sessions, channel, "hello", None, "m1", OWNER
        )
    )


def _shown(slack):
    return " ".join(
        str(kw.get("text", "")) for kind, kw in slack.actions if kind in ("post", "update")
    )


def test_native_stop_hook_runs_after_the_answer_and_the_permit(monkeypatch, owner):
    """A slow Stop hook must hold neither the reply nor the session permit: it fires
    once the answer is shown and the session is released."""
    slack = MockSlackClient()
    released: list[str] = []
    seen_at_stop: list[tuple[str, int]] = []

    class _Sessions(_StoppableSessions):
        def release(self, key):
            released.append(key)

    class _Recording(_Store):
        async def fire(self, event, context="", *, session_key=None, **kw):
            if event == HOOK_EVENT_STOP:
                seen_at_stop.append((_shown(slack), len(released)))
            return await super().fire(event, context, session_key=session_key, **kw)

    _install(monkeypatch, _Recording())
    _run_native("D1", _Sessions(_NativeProvider()), slack)
    assert len(seen_at_stop) == 1
    shown, releases = seen_at_stop[0]
    assert "All done." in shown
    assert releases >= 1


def test_native_message_deleted_during_prompt_submit_never_opens_the_stream(monkeypatch, owner):
    provider = _NativeProvider()
    deleted: list[bool] = []

    class _Sessions(_StoppableSessions):
        def is_cancelled(self, key, msg_ts):
            return bool(deleted)

    store = _Store(on_prompt=lambda: deleted.append(True))
    _install(monkeypatch, store)
    _run_native("D1", _Sessions(provider))
    assert provider.prompts == []


def test_native_dm_turn_fires_prompt_submit_then_stop(monkeypatch, owner):
    store = _Store(prompt_stdout="ticket 7")
    _install(monkeypatch, store)
    provider = _NativeProvider()
    _run_native("D1", _StoppableSessions(provider))
    assert [event for event, _context, _key in store.calls] == [
        HOOK_EVENT_USER_PROMPT_SUBMIT,
        HOOK_EVENT_STOP,
    ]
    assert store.calls[0][1] == "hello"
    assert "All done." in store.calls[1][1]
    assert provider.prompts[0].startswith("[Hook context]\nticket 7\n")


def test_native_stop_hook_never_sees_a_credential_split_across_chunks(monkeypatch, owner):
    """The stream redacts each chunk on its own, so a secret split across two chunks
    survives intact in the accumulated answer; the Stop hook must get it redacted."""
    store = _Store()
    _install(monkeypatch, store)
    secret = "postgresql://user:SuperSecret123@db.example.com:5432/prod"
    provider = _NativeProvider(chunks=("Use " + secret[:24], secret[24:] + " now."))
    _run_native("D1", _StoppableSessions(provider))
    stop_contexts = [context for event, context, _key in store.calls if event == HOOK_EVENT_STOP]
    assert len(stop_contexts) == 1
    assert "SuperSecret123" not in stop_contexts[0]
    assert "now." in stop_contexts[0]


def test_native_channel_turn_fires_nothing(monkeypatch, owner):
    store = _Store(prompt_stdout="ticket 7")
    _install(monkeypatch, store)
    provider = _NativeProvider()
    _run_native("C1", _StoppableSessions(provider))
    assert store.calls == []
    assert not provider.prompts[0].startswith("[Hook context]")


def test_native_stop_during_prompt_submit_never_opens_the_stream(monkeypatch, owner):
    provider = _NativeProvider()
    sessions = _StoppableSessions(provider)

    def _stop():
        sessions.stops += 1

    store = _Store(on_prompt=_stop)
    _install(monkeypatch, store)
    _run_native("D1", sessions)
    assert provider.prompts == []
    assert [event for event, _context, _key in store.calls] == [HOOK_EVENT_USER_PROMPT_SUBMIT]
