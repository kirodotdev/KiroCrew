"""A channel Stop while the chat's message still waits in Shared Crew fair admission.

With fair admission on and the Crew at capacity, a channel message waits in
``turn_gate.run_channel_turn`` before its turn starts, so it holds no session
and ``is_busy`` reads False. The session manager records a Stop only for a key
with a live session, which a chat's first message does not have while it
waits. These tests drive the real ``run_channel_turn`` against a real
``AdmissionService`` over a work store in ``tmp_path``, entered the way each
channel enters it, and the Stop each channel sends:

* Telegram: the real ``TelegramDispatcher.handle_message`` for the message and
  for ``/stop`` (``stop_running_turn``, which Discord and Teams share).
* ``ChannelTurns`` (Discord, Teams, Webex, iMessage): ``channel_fair_admission``
  with the Stop recorded through ``note_user_stop``, as Webex and iMessage do.
* WhatsApp: ``channel_fair_admission`` and the real ``_handle_stop``.
* Slack: ``run_slack_message`` with the Stop ``!stop`` records, naming the
  thread itself or the session the thread is linked to.

Faked: the turn body and a session manager with no live session.

* A Stop during the wait keeps the message from running: its work ends
  ``cancelled``, ``cancelled_by_user``, and no capacity note is sent.
* A Stop on another conversation, and a Stop while a monitor wake waits, leave
  the waiting turn to run once.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

import pytest
from telegram_helpers import _dispatcher, _dm

from kiro_crew.config.sections_shared_crew import effective_limits, shared_crew_config_from
from kiro_crew.messaging import channel_fair_admission
from kiro_crew.messaging.commands import note_user_stop
from kiro_crew.messaging.dispatch_contract import Verdict
from kiro_crew.shared_crew import admission_entry as entry
from kiro_crew.shared_crew import admission_gate as ag
from kiro_crew.shared_crew import turn_gate
from kiro_crew.shared_crew.admission import Liveness, OperationState, ReasonCode
from kiro_crew.shared_crew.containment import ContainmentReport, SliceManager
from kiro_crew.shared_crew.contracts import EnforcementLevel, Principal
from kiro_crew.shared_crew.work_store import WorkStore
from kiro_crew.taskq import lanes
from kiro_crew.taskq.store import TaskStore
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.wait import async_wait_until
from kiro_crew.whatsapp.transport_dispatch import WhatsAppDispatcher

#: Bound, in seconds, on every wait a test releases itself.
BOUND = 30.0
S = OperationState
#: The session key a ``ChannelTurns`` conversation's turn and its Stop both name.
_KEY = "teams:alice:dm"
#: A Slack thread, and the session key its messages are admitted under.
_TS = "1700000000.000100"
#: Every capacity note starts with this (``turn_gate.capacity_note``).
_CAPACITY = "did not start"


def _clear_registries() -> None:
    entry._PENDING_SLOTS.clear()
    turn_gate._UNADMITTED_RUNNING.clear()
    # getattr: prove.py runs the tests on a tree with this map reverted away.
    getattr(turn_gate, "_CHANNEL_WAITS", {}).clear()


@pytest.fixture(autouse=True)
def _clean_registries() -> Any:
    _clear_registries()
    yield
    _clear_registries()


@pytest.fixture
def service(tmp_path, monkeypatch) -> ag.AdmissionService:
    """A real admission service: one turn at a time per principal, two queued."""
    (tmp_path / "svc").mkdir()
    clock = ManualClock(start=80_000.0)
    store = WorkStore(
        TaskStore(tmp_path / "svc" / "t.db", clock=clock.time).open(), clock=clock.time
    )
    limits = effective_limits(
        shared_crew_config_from(
            {
                "admission_enabled": True,
                "principal_concurrency": 1,
                "crew_concurrency": 2,
                "principal_queue_depth": 2,
            }
        ),
        auto_crew_concurrency=8,
    )
    report = ContainmentReport(
        platform="linux",
        available=True,
        reason="",
        cpu=True,
        memory=True,
        pids=True,
        level=EnforcementLevel.BEST_EFFORT,
        hard_ready=True,
        slices_enabled=True,
    )
    svc = ag.AdmissionService(
        store,
        limits=limits,
        clock=clock.time,
        containment_probe=lambda: report,
        slices=SliceManager(
            runner=lambda cmd: subprocess.CompletedProcess(cmd, 0, "", ""),
            systemctl="/usr/bin/systemctl",
        ),
        liveness=lambda ref: Liveness.STOPPED,
    )

    async def _current() -> ag.AdmissionService:
        return svc

    monkeypatch.setattr(ag, "current_service", _current)
    monkeypatch.setattr(ag, "_SERVICE", svc)
    monkeypatch.setattr(lanes, "_LANE_RESOLVER", svc.budget_for_session)
    return svc


@dataclass
class _Channel:
    """One channel's message, its Stop, what its turn body ran and the notes it sent."""

    send: Callable[[], Awaitable[Any]]
    stop: Callable[[], Awaitable[Any]]
    ran: list[str]
    notes: Callable[[], list[str]]


def _no_live_session() -> Any:
    """A session manager with no live session: ``note_stop`` records nothing, as the real one."""
    return SimpleNamespace(note_stop=lambda key: False, stop_generation=lambda key: 0)


class _Turns:
    """The part of ``ChannelTurns`` that ``channel_fair_admission.run_admitted`` reads."""

    def __init__(self, channel: str) -> None:
        self.channel = channel
        self.ran: list[str] = []
        self.notes: list[str] = []

    async def _run(self, asker: Any, text: str, source: Any, **kwargs: Any) -> Verdict:
        self.ran.append(text)
        return Verdict.ANSWERED

    def _outcome(self, verdict: Verdict, monitor: Any, **kwargs: Any) -> Verdict:
        return verdict

    async def source(self, _quiet: bool) -> Any:
        async def _chunk(text: str) -> None:
            self.notes.append(text)

        async def _done() -> None:
            return None

        return SimpleNamespace(on_text_chunk=_chunk, on_done=_done)


def _channel_turns(channel: str = "teams", stop_key: str = _KEY, **kwargs: Any) -> _Channel:
    turns = _Turns(channel)
    sessions = _no_live_session()

    async def _send() -> Any:
        asker = SimpleNamespace(session_key=_KEY)
        return await channel_fair_admission.run_admitted(
            turns, asker, "hello", turns.source, **kwargs
        )

    async def _stop() -> None:
        note_user_stop(sessions, stop_key)

    return _Channel(send=_send, stop=_stop, ran=turns.ran, notes=lambda: turns.notes)


def _telegram() -> _Channel:
    d, client, sessions = _dispatcher({7})
    sessions.note_stop = lambda key: False  # type: ignore[attr-defined]
    sessions.stop_generation = lambda key: 0  # type: ignore[attr-defined]
    ran: list[str] = []

    async def _run_turn(msg: Any, text: str, **kwargs: Any) -> bool:
        ran.append(text)
        return False

    d._run_turn = _run_turn  # type: ignore[method-assign]
    return _Channel(
        send=lambda: d.handle_message(_dm("hello")),
        stop=lambda: d.handle_message(_dm("/stop")),
        ran=ran,
        notes=lambda: [str(sent[0]) for sent in client.sent if _CAPACITY in str(sent[0])],
    )


def _whatsapp() -> _Channel:
    channel = _channel_turns("whatsapp")
    # Only what ``_handle_stop`` reads; ``stop_turn`` finds no session.
    d = object.__new__(WhatsAppDispatcher)

    async def _stop_turn(key: str, **kwargs: Any) -> str:
        return "idle"

    async def _say(scope: str, text: str) -> None:
        return None

    d.sessions = SimpleNamespace(stop_turn=_stop_turn, note_stop=lambda key: False)
    d._live_session_key = lambda scope: _KEY  # type: ignore[method-assign]
    d._say = _say  # type: ignore[method-assign]
    return replace(channel, stop=lambda: d._handle_stop("447700900000@s.whatsapp.net"))


def _slack(linked: str = "") -> _Channel:
    ran: list[str] = []
    posts: list[str] = []
    sessions = SimpleNamespace(
        note_stop=lambda key: False, get_session_for_thread=lambda ts: linked or None
    )

    async def _handle_message(**kwargs: Any) -> None:
        ran.append(kwargs["text"])

    async def _post(channel: str, text: str, ts: str) -> None:
        posts.append(text)

    args = {
        "slack": SimpleNamespace(post_message=_post),
        "sessions": sessions,
        "channel": "C1",
        "thread_ts": _TS,
        "msg_ts": _TS,
        "text": "hello",
    }

    async def _stop() -> None:
        # ``!stop`` names the session the thread is linked to, else the thread.
        note_user_stop(sessions, sessions.get_session_for_thread(_TS) or _TS)

    return _Channel(
        send=lambda: turn_gate.run_slack_message(_handle_message, args),
        stop=_stop,
        ran=ran,
        notes=lambda: posts,
    )


_CHANNELS: dict[str, Callable[[], _Channel]] = {
    "telegram": _telegram,
    "channel-turns": _channel_turns,
    "whatsapp": _whatsapp,
    "slack": _slack,
    "slack-linked-thread": lambda: _slack(linked="dashboard:linked"),
}


def _queued(service: ag.AdmissionService) -> list[Any]:
    return [w for w in service._store.list_sync() if w.state is S.QUEUED]


async def _stop_during_the_wait(service: ag.AdmissionService, channel: _Channel) -> Any:
    """Send *channel*'s message while the Crew is full, Stop it, free the Crew.

    The work record of the message once its turn returned.
    """
    await service.set_limits(replace(service.limits, crew_active=1))
    unknown = Principal.unknown().id
    release = asyncio.Event()

    async def _held() -> None:
        await asyncio.wait_for(release.wait(), 2 * BOUND)

    # The Crew's one place is taken by other work until *release* is set.
    holder = asyncio.ensure_future(
        turn_gate.run_admitted(_held(), session_key="elsewhere", text="x", budget_key=unknown)
    )
    turn: asyncio.Future[Any] | None = None
    try:
        await async_wait_until(
            lambda: service.scheduler.usage(unknown)[0].active == 1,
            describe=lambda: service.scheduler.usage(unknown),
        )
        turn = asyncio.ensure_future(channel.send())
        await async_wait_until(
            lambda: turn is not None and (turn.done() or len(_queued(service)) == 1),
            describe=lambda: service._store.list_sync(),
            off_loop=True,
        )
        assert not turn.done(), "the message did not wait for admission"
        [work] = await asyncio.to_thread(_queued, service)
        await asyncio.wait_for(channel.stop(), BOUND)
        release.set()
        await asyncio.wait_for(holder, BOUND)
        await asyncio.wait_for(turn, BOUND)
    finally:
        release.set()
        pending = [task for task in (holder, turn) if task is not None]
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    assert getattr(turn_gate, "_CHANNEL_WAITS", {}) == {}, "an ended wait kept its Stop record"
    return await asyncio.to_thread(service._store.get_sync, work.work_id)


@pytest.mark.asyncio
@pytest.mark.timeout(90)
@pytest.mark.parametrize("name", sorted(_CHANNELS))
async def test_a_stop_during_the_wait_keeps_the_message_from_running(name, service):
    channel = _CHANNELS[name]()
    record = await _stop_during_the_wait(service, channel)
    assert channel.ran == [], (
        f"the stopped message ran once the wait ended; its work ended "
        f"{record.state.value}/{record.reason.value if record.reason else ''}"
    )
    assert (record.state, record.reason) == (S.CANCELLED, ReasonCode.CANCELLED_BY_USER)
    assert channel.notes() == []


@pytest.mark.asyncio
@pytest.mark.timeout(90)
async def test_a_stop_on_another_conversation_leaves_the_message_to_run(service):
    channel = _channel_turns(stop_key="teams:bob:dm")
    record = await _stop_during_the_wait(service, channel)
    assert channel.ran == ["hello"]
    assert record.state is S.COMPLETED


@pytest.mark.asyncio
@pytest.mark.timeout(90)
async def test_a_monitor_wake_still_runs_after_a_stop_during_its_wait(service):
    # A wake is the monitor's, not a person's message; a refused wake retries.
    channel = _channel_turns(monitor=object())
    record = await _stop_during_the_wait(service, channel)
    assert channel.ran == ["hello"]
    assert record.state is S.COMPLETED
