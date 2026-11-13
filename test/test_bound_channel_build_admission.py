"""A chat-channel turn bounds its wait to enter the memory embed pool.

Each channel turn builds its prompt on the memory embed pool, behind the same
admission semaphore as the dashboard turn. With every slot held, an unbounded wait
leaves the person on the channel with no reply and no error. The build's admission
wait now gives up after ``TURN_BUILD_ADMISSION_TIMEOUT_SECS``, and each turn answers
with its channel's retryable memory-unavailable reply instead; the build never runs.
A monitor wake, which nobody waits on, keeps waiting for the pool.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from channel_turn_fakes import (
    FakeSessions,
    RecordingCtxBuilder,
    RecordingRenderer,
)
from channel_turn_fakes import ScriptedProvider as TurnProvider
from channel_turn_fakes import (
    answer,
)
from channel_turns_helpers import governance_fixture  # noqa: F401
from channel_turns_helpers import published_fixture  # noqa: F401
from channel_turns_helpers import spooled_fixture  # noqa: F401
from channel_turns_helpers import CONV, KEY, _asker, _turns
from slack_handler_helpers import FakeSessionManager
from slack_transport_dispatch_helpers import (
    _MSG_TS,
    RecordingSlackClient,
    ScriptedProvider,
    _CapturingCtxBuilder,
    _CapturingSessions,
    make_event,
)
from telegram_helpers import _drop_live_config_snapshot  # noqa: F401
from telegram_helpers import _dispatcher, _dm

from conftest import MockSlackClient
from kiro_crew import executors
from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, STOP_REASON_END_TURN
from kiro_crew.hooks import HookResult
from kiro_crew.messaging import dispatch as D
from kiro_crew.messaging.dispatch import (
    DISCORD_DRIFT,
    ChannelTurn,
    MonitorWake,
    Verdict,
    drive_turn,
)
from kiro_crew.monitoring.models import MonitorDispatchResult
from kiro_crew.slack import handler as slack_handler
from kiro_crew.slack import transport_dispatch as slack_transport
from kiro_crew.telegram.dispatch import turn_engine
from kiro_crew.testing.wait import await_until, finished, until_parked

_BUSY = "Memory is busy"


@contextlib.asynccontextmanager
async def _every_slot_held(
    monkeypatch: pytest.MonkeyPatch, bound: float = 0.05
) -> AsyncIterator[asyncio.Semaphore]:
    """Hold every memory admission slot on this loop, with a tiny admission bound.

    On exit the count must still be zero: a timed-out wait took no slot of its own.
    """
    for module in (D, slack_handler, slack_transport, turn_engine):
        # raising=False: without the fix the name is absent, and the turn must then
        # fail on its hang rather than on this patch.
        monkeypatch.setattr(module, "TURN_BUILD_ADMISSION_TIMEOUT_SECS", bound, raising=False)
    admission = executors._memory_admission(asyncio.get_running_loop())
    for _ in range(executors._MAX_EMBED_WORKERS):
        await admission.acquire()
    try:
        yield admission
        assert admission._value == 0  # noqa: SLF001
    finally:
        for _ in range(executors._MAX_EMBED_WORKERS):
            admission.release()


class _Builder:
    """A context builder whose build is never reached while the pool is saturated."""

    conversation_log = None

    def __init__(self) -> None:
        self.hooks = _Hooks()
        self.builds = 0

    def build_message(self, text: str, is_new: bool, session_key: str, **kw: Any) -> Any:
        self.builds += 1
        return text, None


class _Hooks:
    auto_approve_subagent_spawn = False

    def on_message(self, *a: Any, **kw: Any) -> HookResult:
        return HookResult.passthrough()


class _WakeHook:
    """A monitor completion hook that authorizes the wake."""

    accepted = False

    async def authorize(self) -> bool:
        return True

    def mark_accepted(self) -> None:
        self.accepted = True

    async def complete(self, disposition: Any, usage: Any) -> None:
        pass


@pytest.mark.asyncio
async def test_the_shared_pipeline_answers_memory_unavailable(monkeypatch) -> None:
    ctx = RecordingCtxBuilder()
    renderer = RecordingRenderer()
    turns = _turns(FakeSessions(), ctx=ctx)
    async with _every_slot_held(monkeypatch):
        outcome = await finished(turns.answer(_asker(), "hi", renderer), "the refused turn")
    assert outcome.verdict is Verdict.MEMORY_REFUSED
    assert _BUSY in "".join(renderer.shown)
    assert ctx.builds == []


@pytest.mark.asyncio
async def test_a_refused_first_turn_hands_the_new_session_back(monkeypatch) -> None:
    """The claim took a new session's first turn; a build the pool refused gives it back."""
    provider = TurnProvider(answer())
    sessions = FakeSessions(provider, is_new=True, resumed=True)
    rearmed: list[tuple[str, Any, bool, int]] = []
    sessions.rearm_first_turn = lambda key, prov, *, resumed: rearmed.append(  # type: ignore[attr-defined]
        (key, prov, resumed, sessions.held)
    )
    async with _every_slot_held(monkeypatch):
        await finished(_turns(sessions).answer(_asker(), "hi", RecordingRenderer()), "the turn")
    assert [(k, p, r) for k, p, r, _ in rearmed] == [(KEY, provider, True)]
    assert rearmed[0][3] > 0, "handed back while the turn still held the session"


@pytest.mark.parametrize(
    "first_turn, provider_is_live, expected",
    [
        pytest.param("NOTHING_ARMED", True, "RESUMED", id="claimed-by-this-turn"),
        pytest.param("FRESH", True, "FRESH", id="already-armed"),
        pytest.param("NOTHING_ARMED", False, "NOTHING_ARMED", id="replaced-session"),
    ],
)
def test_rearm_first_turn_arms_only_the_session_the_turn_claimed(
    first_turn, provider_is_live, expected
) -> None:
    from types import SimpleNamespace

    from kiro_crew.session_parts import claims
    from kiro_crew.session_parts.record import FirstTurnState

    provider = object()
    session = SimpleNamespace(
        first_turn=FirstTurnState[first_turn],
        provider=provider if provider_is_live else object(),
    )
    manager = SimpleNamespace(_sessions={"k": session}, _fold_key=lambda key: key)
    claims.rearm_first_turn(manager, "k", provider, resumed=True)
    assert session.first_turn is FirstTurnState[expected]


@pytest.mark.asyncio
async def test_a_monitor_wake_keeps_waiting_for_the_pool(monkeypatch) -> None:
    turns = _turns(FakeSessions(TurnProvider(answer())), drift=DISCORD_DRIFT)
    wake = MonitorWake(_WakeHook(), lambda: True)  # type: ignore[arg-type]
    # A zero bound refuses a bounded wait before it ever parks on the semaphore.
    async with _every_slot_held(monkeypatch, bound=0) as admission:
        turn = asyncio.ensure_future(
            turns.answer(_asker(route=None), "hi", RecordingRenderer(), monitor=wake)
        )
        await until_parked(admission)
        admission.release()
        outcome = await await_until(turn, what="the admitted wake")
        await finished(admission.acquire(), "the wake's slot coming back")
    assert outcome.monitor is MonitorDispatchResult.DISPATCHED


@pytest.mark.asyncio
async def test_an_unprompted_group_turn_gives_up_silently(monkeypatch) -> None:
    """A busy reply nobody asked for would be an unsolicited post into the group."""
    ctx = RecordingCtxBuilder()
    renderer = RecordingRenderer()
    turn = ChannelTurn(
        channel_type="weixin",
        conversation_id=CONV,
        agent="agentA",
        user_text="hi",
        renderer=renderer,
        session_key=KEY,
        approval_mode="auto",
        unprompted=True,
    )
    async with _every_slot_held(monkeypatch):
        await finished(
            drive_turn(turn, sessions=FakeSessions(), ctx_builder=ctx), "the refused turn"
        )
    assert renderer.shown == []
    assert ctx.builds == []


@pytest.mark.asyncio
async def test_the_native_slack_turn_answers_memory_unavailable(monkeypatch) -> None:
    slack = MockSlackClient()
    sessions = FakeSessionManager()
    builder = _Builder()
    async with _every_slot_held(monkeypatch):
        await finished(
            slack_handler.handle_message(
                slack, sessions, "C1", "hi", None, "m1", "U1", context_builder=builder
            ),
            "the refused turn",
        )
    assert builder.builds == 0
    shown = " ".join(str(a[1].get("text") or "") for a in slack.actions)
    assert _BUSY in shown


@pytest.mark.asyncio
async def test_a_refused_slack_turn_keeps_the_stopped_turn_to_recall(monkeypatch) -> None:
    """After a Stop, a turn the pool refused gives the soft-cancel flag back and saves no row.

    The retry then still recalls the stopped turn, not the refused attempt.
    """
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    log = MagicMock()
    log.get_metadata.return_value = {}
    log.get_metadata_status.return_value = ({}, True)
    log.read_messages.return_value = [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier partial answer"},
    ]
    builder = _Builder()
    builder.conversation_log = log
    sessions = FakeSessionManager()
    session = SimpleNamespace(prev_turn_cancelled=True, provider=sessions._provider)
    sessions._sessions = {"thread1": session}
    async with _every_slot_held(monkeypatch):
        await finished(
            slack_handler.handle_message(
                MockSlackClient(),
                sessions,
                "C1",
                "hi",
                "thread1",
                "m1",
                "U1",
                context_builder=builder,
                conversation_log=log,
            ),
            "the refused turn",
        )
    assert builder.builds == 0
    assert session.prev_turn_cancelled is True
    assert log.append.call_args_list == []


@pytest.mark.asyncio
async def test_the_slack_transport_turn_answers_memory_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(slack_transport, "_get_default_agent", lambda: "kirocrew")
    monkeypatch.setattr(slack_transport, "_hydrate_thread_overrides", _no_override)
    monkeypatch.setattr(slack_transport, "_hydrate_conv_flags", lambda *a, **k: None)
    monkeypatch.setattr(slack_transport, "_thread_agents", {})
    slack = RecordingSlackClient()
    provider = ScriptedProvider(
        [
            make_event(EVENT_TEXT_CHUNK, text="hi"),
            make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
    )
    builder = _CapturingCtxBuilder()
    async with _every_slot_held(monkeypatch):
        await finished(
            slack_transport.handle_message_transport(
                slack=slack,
                sessions=_CapturingSessions(provider),
                channel="C1",
                text="hi",
                thread_ts=None,
                msg_ts=_MSG_TS,
                user_id="U_OWNER",
                context_builder=builder,
                conversation_log=None,
            ),
            "the refused turn",
        )
    assert builder.captured == {}
    posted = [kw["text"] for (method, kw) in slack.transcript if method == "post_message"]
    assert any(_BUSY in text for text in posted), posted


@pytest.mark.asyncio
async def test_the_telegram_turn_answers_memory_unavailable(monkeypatch) -> None:
    d, cli, sessions = _dispatcher({7})
    async with _every_slot_held(monkeypatch):
        await finished(d.handle_message(_dm("hi")), "the refused turn")
    assert d.ctx_builder.build_calls == []
    assert sessions.failures == [], "a busy pool is not the session's failure"
    shown = [text for _mid, text, _markup in cli.edits] + [text for text, _ in cli.sent]
    assert any(_BUSY in text for text in shown), shown


async def _no_override(*_a: Any, **_k: Any) -> None:
    return None
