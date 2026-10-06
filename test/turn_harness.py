"""Run a real dashboard chat turn in-process and record what it did.

``await run_turn(script)`` drives the production ``chat_runner._run_chat``,
dispatched through the production ``spawn_guarded_turn`` so it runs under the
same ceiling and the same ``_TURN_DEADLINE`` it runs under in the gateway,
against a scripted provider. It returns a :class:`TurnRecord`: the frames the
turn sent, the rows it persisted, its audit events, crew-log entries, provider
calls, session-manager calls, the turns it handed off to, and how it stopped.

It exists so a test of a turn asserts on what the turn DID, not on how
``_run_chat`` is spelled. A test that slices ``inspect.getsource(_run_chat)``
breaks when the function moves and stays green when the behaviour regresses
under the same spelling; a test on a :class:`TurnRecord` does neither.

Behind the seam, so no test sets it up:

* a ``DashboardState`` from ``chat_test_helpers._make_state`` with a real
  ``ConversationLog`` in a private temporary directory, and a session manager
  specced on ``SessionManager`` that answers the way one healthy dashboard
  session does and refuses any method it was not told how to answer;
* :class:`ScriptedProvider`, the in-process adapter at the provider port
  (``state.sessions.get_or_create``; production's adapter is the ACP session
  provider), which yields the script's events and records every call;
* two internal patches of ``chat_runner``: its ``sel`` (a recording audit log,
  until the runner reads its audit log through an adapter slot) and its hand-off
  dispatch (``spawn_guarded_turn``), so a queued successor is RECORDED instead
  of run; the crew-log emitter's entry points carry recorders, and the AutoNudge
  service is off unless the slot asks for a recording one;
* a :class:`VirtualClock`: the turn runs on its own event loop whose time is
  virtual and advances only when nothing else can run, so a 600 s approval
  window expires in microseconds and always at exactly 600 s.

Invariants: one turn per script (a ``then`` chain sends the slot's next
message as a further turn); the same script gives the same record. The harness
itself starts no process, binds no port and never sleeps; production code the
turn reaches may still use its own (a turn that writes files consults the
sensitive-path resolver, whose pool runs a helper child, and on Windows every
selector loop's self-pipe is a loopback socket pair). The loop runs on a worker
thread the call joins before it returns, after every task the turns left behind
has been cancelled and awaited.

Wall-clock reads stay real, because the runner's ``time`` is read by the
``chat_turn`` owners it composes as well, and they import it themselves. What a
turn reads off the wall clock:

* the ``heartbeat`` liveness frame, sent when 5 real seconds pass between two
  stream events;
* duration and time-to-first-token metrics, and crew-log step milliseconds;
* the eager-spawn backoff and a throttle signal's retry delay;
* the TTL of a slot's pending context entries (``context_entry_expired``);
* native sub-agent cards: auto-closed after 120 s with no progress, terminal
  records kept for an hour, and the elapsed time a card shows;
* the directive claim bound: a turn claims only directives parked after its own
  monotonic start;
* row and audit timestamps.

None decides an approval window, the turn ceiling or a stop, which all run on
``loop.time()``. The context TTL and the native-card staleness and retention are
wall-clock timeouts that a virtual :class:`Wait` does not advance: a script that
waits 600 virtual seconds has not aged a context entry or a sub-agent card. So
assert on none of them; a stalled CI worker changes them, and the virtual clock
does not.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import dataclasses
import inspect
import json
import selectors
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, create_autospec

import pytest
from chat_test_helpers import _make_state

from kiro_crew.acp.types import AcpEvent
from kiro_crew.history import HUMAN_TURN_META_KEY
from kiro_crew.providers.base import LLMProvider

#: The decisions a person's click puts on a prompt's approval future.
APPROVED = "approved"
APPROVED_TRUST_READS = "approved_trust_reads"
REJECTED = "rejected"
REJECTED_ONCE = "rejected_once"
#: Leave the prompt unanswered, so its window expires (the default).
UNANSWERED = "unanswered"
#: Cancel the running turn while the prompt is open (slot deletion, shutdown).
CANCEL_TURN = "cancel_turn"
#: Press Stop while the prompt is open: the stop path's unblock of pending waits.
STOP = "stop"

_DECISIONS = frozenset({APPROVED, APPROVED_TRUST_READS, REJECTED, REJECTED_ONCE})
_ANSWERS = _DECISIONS | {UNANSWERED, CANCEL_TURN, STOP}

#: Real seconds the turn may wait on work that is not virtual (an executor job)
#: before the harness fails it, so a blocked worker thread fails the test by name
#: instead of losing the xdist worker (testing-conventions class 6).
_REAL_WAIT_BOUND_SECS = 30.0
#: Virtual seconds granted to what the turn scheduled behind itself (title,
#: summary, save tasks) before the harness cancels it.
_SETTLE_SECS = 60.0


# ── the clock ───────────────────────────────────────────────────────────────


class VirtualClock:
    """Virtual time for one turn's event loop.

    ``loop.time()`` reads :attr:`now`. When the loop has nothing runnable, no
    ready I/O and no thread offload in flight, it jumps :attr:`now` to the next
    timer instead of sleeping, so every ``asyncio.sleep`` / ``wait_for``
    deadline fires in order, instantly, at its exact virtual time. Wall-clock
    reads (``time.time``/``time.monotonic``) are NOT virtual; see the module
    docstring for the ones a turn makes.
    """

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.start = float(start)
        self.now = float(start)
        self._in_flight = 0
        self._lock = threading.Lock()

    def elapsed(self) -> float:
        """Virtual seconds since the clock started."""
        return self.now - self.start

    def _hold(self, change: int) -> None:
        # An offload can be awaited from a thread other than the loop's.
        with self._lock:
            self._in_flight += change


class _VirtualSelector(selectors.DefaultSelector):
    """Advance the clock instead of blocking when only timers remain."""

    def __init__(self, clock: VirtualClock) -> None:
        super().__init__()
        self._clock = clock

    def select(self, timeout: float | None = None) -> list[Any]:
        ready = super().select(0)
        if ready or timeout == 0:
            return ready
        if self._clock._in_flight:
            # A thread is working for the loop; its completion arrives through
            # the loop's self-pipe. Wait for it in REAL time, bounded.
            ready = super().select(_REAL_WAIT_BOUND_SECS)
            if not ready:
                raise RuntimeError(
                    f"the turn waited {_REAL_WAIT_BOUND_SECS:.0f}s of real time on "
                    "executor work that never finished"
                )
            return ready
        if timeout is None:
            raise RuntimeError(
                "the turn is deadlocked: nothing is runnable, no timer is armed "
                "and no executor work is in flight"
            )
        self._clock.now += timeout
        return []


class _VirtualTimeLoop(asyncio.SelectorEventLoop):
    """A selector event loop on :class:`VirtualClock` time.

    Selector-based on every platform: the turn needs no subprocess transport,
    the one thing the Windows proactor loop would be needed for.
    """

    def __init__(self, clock: VirtualClock) -> None:
        self._clock = clock
        super().__init__(selector=_VirtualSelector(clock))
        _OffloadCount.acquire()
        self._counting = True

    def time(self) -> float:
        return self._clock.now

    def close(self) -> None:
        super().close()
        # Only once the loop really is closed: a close() that raises (the loop
        # is still running) leaves it open, and still counting.
        if self._counting:
            self._counting = False
            _OffloadCount.release()

    def _hold_for(self, job: concurrent.futures.Future[Any]) -> None:
        """Hold virtual time until the thread doing *job* is done with it.

        Tied to the job, not to the future awaiting it: a cancelled await leaves
        its job running on the thread, and virtual time must not move past work
        that is still going on. A job its executor drops unstarted (a shutdown
        that cancels pending work) is done the moment it is dropped.
        """
        if job.done():
            return
        self._clock._hold(1)

        def _finished(_job: concurrent.futures.Future[Any]) -> None:
            with contextlib.suppress(RuntimeError):  # the loop has closed
                self.call_soon_threadsafe(self._clock._hold, -1)

        job.add_done_callback(_finished)

    async def shutdown_default_executor(self, timeout: float | None = None) -> None:
        # The join runs on a raw thread, not through an awaited future: count
        # it, or the clock would jump past the join's own timeout.
        self._clock._hold(1)
        try:
            await super().shutdown_default_executor(timeout)
        finally:
            self._clock._hold(-1)


class _OffloadCount:
    """Count every thread offload a virtual loop awaits, while one is open.

    asyncio awaits a thread's result through one function, ``wrap_future``:
    ``run_in_executor`` and ``to_thread`` go through it, and so does production
    code that submits to its own pool (the embed pool behind context assembly).
    It is replaced while any virtual loop is open and put back when the last
    one closes; on any other loop it does exactly what the real one does.
    """

    _lock = threading.Lock()
    _users = 0
    _real: Any = None

    @classmethod
    def acquire(cls) -> None:
        with cls._lock:
            if cls._users == 0:
                cls._real = asyncio.futures.wrap_future
                asyncio.futures.wrap_future = _counted_wrap_future  # type: ignore[assignment]
                asyncio.wrap_future = _counted_wrap_future  # type: ignore[assignment]
            cls._users += 1

    @classmethod
    def release(cls) -> None:
        with cls._lock:
            cls._users -= 1
            if cls._users == 0:
                asyncio.futures.wrap_future = cls._real
                asyncio.wrap_future = cls._real


def _counted_wrap_future(future: Any, *, loop: Any = None) -> Any:
    target = loop
    if target is None:
        with contextlib.suppress(RuntimeError):
            target = asyncio.get_running_loop()
    wrapped = _OffloadCount._real(future, loop=loop)
    if isinstance(target, _VirtualTimeLoop) and isinstance(future, concurrent.futures.Future):
        # After the real wrap, so the job's result reaches the loop BEFORE the
        # release of its hold: released first, the clock could jump to the next
        # timer in the gap before the result lands.
        target._hold_for(future)
    return wrapped


# ── the script ──────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Raise:
    """The provider stream raises *error* at this point."""

    error: BaseException


@dataclasses.dataclass(frozen=True)
class Wait:
    """The provider spends *seconds* of virtual time before its next event."""

    seconds: float


@dataclasses.dataclass(frozen=True)
class Do:
    """Run ``action(ctx)`` on the turn's loop at this point of the stream.

    For what something OUTSIDE the turn does while it runs: another handler
    relinking the slot, a person popping it. *action* may be async.
    """

    action: Callable[["TurnContext"], Any]


@dataclasses.dataclass(frozen=True)
class Emit:
    """Yield the event ``make(ctx)`` builds at this point of the stream.

    For an event whose content depends on what the turn already did: the
    backend's echo of a steer the turn just sent.
    """

    make: Callable[["TurnContext"], AcpEvent]


Step = AcpEvent | Raise | Wait | Do | Emit


@dataclasses.dataclass(frozen=True)
class TurnScript:
    """What the provider does during the turn, and how its prompts are answered.

    ``events`` is the provider stream, in order. ``answers`` maps a permission
    request id to the responder's answer (one of this module's answer
    constants), or to a sequence of answers for an id the turn prompts more
    than once; an id it does not name is left :data:`UNANSWERED`. ``message``
    is what was sent; ``user_row`` says a person typed it in the dashboard (the
    send handler then writes their row and marks the turn as theirs). ``setup``
    runs on the turn's loop before the turn starts, for collaborators a test
    arranges (a Slack client, a link). ``allocation_error`` makes acquiring the
    session raise, the way a spawn refusal does. ``run_kwargs`` reach
    ``_run_chat`` as-is: the keyword inputs only its in-process callers pass.
    ``provider`` is the provider adapter's class, for a test that scripts a
    capability as well as the stream (a backend identity, a steer that never
    returns). ``then`` is the next message sent in the same slot once this turn
    has ended: it runs as a further turn on the same state and slot, and its
    record is this record's ``then``.
    """

    events: Sequence[Step] = ()
    answers: Mapping[str, str | Sequence[str]] = dataclasses.field(default_factory=dict)
    message: str = "hello"
    user_row: bool = True
    setup: Callable[["TurnContext"], Any] | None = None
    allocation_error: BaseException | None = None
    run_kwargs: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    provider: type["ScriptedProvider"] | None = None
    then: "TurnScript | None" = None

    def __post_init__(self) -> None:
        given = [
            answer
            for value in self.answers.values()
            for answer in ((value,) if isinstance(value, str) else value)
        ]
        unknown = {answer for answer in given if answer not in _ANSWERS}
        if unknown:
            raise ValueError(f"unknown answers {sorted(unknown)}; use the module's constants")


@dataclasses.dataclass(frozen=True)
class SlotSpec:
    """The dashboard slot the turn runs in, and the gateway mode around it.

    ``app`` makes the slot app-owned, which is what makes it unattended unless
    ``human_seen``. ``rows`` seeds its window with earlier ``(role, content)``
    rows. The trust fields are the slot's own approval state; ``yolo`` is the
    gateway's YOLO override. ``autonudge`` runs the gateway with AutoNudge on: a
    recording service with no goal loop armed. Off, as in a gateway with
    AutoNudge disabled, there is no service at all.
    """

    key: str = "chat-1"
    app: str = ""
    human_seen: bool = False
    trust: bool = False
    trust_reads: bool = False
    trust_scope: str = ""
    yolo: bool = False
    model: str = ""
    memory_mode: str = "persistent"
    linked_session_key: str = ""
    rows: Sequence[tuple[str, str]] = ()
    autonudge: bool = False


@dataclasses.dataclass
class TurnContext:
    """What a :class:`Do` action or a ``setup`` can reach."""

    state: Any
    slot: Any
    clock: VirtualClock
    provider: "ScriptedProvider | None" = None


# ── the record ──────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class WsFrame:
    """One frame the turn sent. ``channel`` is ``all`` or ``owners``."""

    channel: str
    kind: str
    payload: Any
    at: float


@dataclasses.dataclass(frozen=True)
class Call:
    """One call the turn made on a collaborator, at virtual second ``at``."""

    name: str
    args: tuple[Any, ...]
    kwargs: Mapping[str, Any]
    at: float


@dataclasses.dataclass(frozen=True)
class TurnRecord:
    """Everything one turn observably did, each list in the order it happened.

    ``history_rows`` are the rows PERSISTED to the slot's transcript;
    ``window`` is the slot's live message window after the turn, transient rows
    (permission cards) included -- what a history reload is served;
    ``successors`` are the turns its queue hand-off dispatched (recorded, not
    run); ``stop_reason`` is read off the turn's crew-log closer: the
    ``turn/completed`` stop reason, ``failed: <error>`` for a turn that ended
    without its terminal event, or ``""`` for one that never opened. Every
    ``at`` is a virtual second since the run's clock started; ``elapsed`` is
    this turn's own span. ``then`` is the record of the script's ``then`` turn.
    """

    ws_frames: tuple[WsFrame, ...]
    history_rows: tuple[Mapping[str, Any], ...]
    window: tuple[Mapping[str, Any], ...]
    audit_events: tuple[Call, ...]
    crew_log: tuple[Call, ...]
    provider_calls: tuple[Call, ...]
    session_calls: tuple[Call, ...]
    autonudge_calls: tuple[Call, ...]
    successors: tuple[Call, ...]
    stop_reason: str
    elapsed: float
    then: "TurnRecord | None" = None

    def frames(self, kind: str) -> list[WsFrame]:
        return [frame for frame in self.ws_frames if frame.kind == kind]

    def rows(self, role: str) -> list[Mapping[str, Any]]:
        return [row for row in self.history_rows if row.get("role") == role]

    def calls(self, name: str) -> list[Call]:
        return [call for call in self.provider_calls if call.name == name]

    def crew(self, name: str) -> list[Call]:
        return [call for call in self.crew_log if call.name == name]

    def audits(self, **match: Any) -> list[Mapping[str, Any]]:
        """``log_tool_invocation`` audit kwargs whose fields equal *match*."""
        return [
            call.kwargs
            for call in self.audit_events
            if call.name == "log_tool_invocation"
            and all(call.kwargs.get(key) == value for key, value in match.items())
        ]

    @property
    def approval_cards(self) -> list[Mapping[str, Any]]:
        """The permission cards shown, as their ``chat_message`` frames' payloads."""
        return [
            frame.payload
            for frame in self.frames("chat_message")
            if frame.payload.get("role") == "permission"
        ]

    @property
    def approval_decisions(self) -> list[Mapping[str, Any]]:
        """Each prompt's recorded decision (``approval/decided``), in order."""
        return [dict(call.kwargs) for call in self.crew("on_approval_decided")]

    @property
    def allocations(self) -> list[Call]:
        return [call for call in self.session_calls if call.name == "get_or_create"]

    @property
    def notify_approval_stalled(self) -> list[tuple[str, float]]:
        """``(slot key, virtual second)`` of each stalled-approval signal."""
        return [
            (call.args[0], call.at)
            for call in self.autonudge_calls
            if call.name == "notify_approval_stalled"
        ]


# ── the provider adapter ────────────────────────────────────────────────────


class ScriptedProvider(LLMProvider):
    """The in-process adapter at the provider port: yields the script, records calls.

    Production's adapter is the ACP session provider. This one subclasses the
    same ``LLMProvider`` port, so every capability it does not script answers
    the port's own default -- what a provider lacking that capability answers
    -- instead of a truthy ``MagicMock`` attribute. Subclass it to script a
    capability; call :meth:`record` from an override so the call is recorded.
    """

    def __init__(self, steps: Sequence[Step], ctx: TurnContext, calls: list[Call]) -> None:
        self._steps = list(steps)
        self._ctx = ctx
        self._calls = calls
        self._last_steer = 0.0

    @property
    def supports_refusal_steer(self) -> bool:
        return True

    @property
    def supports_steer(self) -> bool:
        return True

    @property
    def last_steer_monotonic(self) -> float:
        # Half of the steer capability (harness-parity H15): the keepalive route
        # compares it with the wall-clock monotonic reading a sleeping wait took.
        return self._last_steer

    @property
    def session_id(self) -> str:
        return "acp-session-1"

    def record(self, name: str, *args: Any) -> None:
        """Record a call on the provider at the current virtual second."""
        self._calls.append(Call(name, args, {}, self._ctx.clock.elapsed()))

    def recorded(self, name: str) -> list[Call]:
        """The calls named *name* the turn has made on the provider so far."""
        return [call for call in self._calls if call.name == name]

    async def _events(self) -> AsyncIterator[AcpEvent]:
        for step in self._steps:
            if isinstance(step, Raise):
                raise step.error
            if isinstance(step, Wait):
                await asyncio.sleep(step.seconds)
            elif isinstance(step, Do):
                outcome = step.action(self._ctx)
                if inspect.isawaitable(outcome):
                    await outcome
            elif isinstance(step, Emit):
                yield step.make(self._ctx)
            else:
                yield step

    def stream(self, message: str, *, allow_image: bool = True) -> AsyncIterator[AcpEvent]:
        self.record("stream", message)
        return self._events()

    def stream_command(self, command: str) -> AsyncIterator[AcpEvent]:
        self.record("stream_command", command)
        return self._events()

    async def approve_tool(self, request_id: str | int, *, always: bool = False) -> bool:
        self.record("approve_tool", request_id)
        return True

    async def reject_tool(self, request_id: str | int) -> None:
        self.record("reject_tool", request_id)

    async def steer(self, message: str) -> bool:
        self.record("steer", message)
        self._last_steer = time.monotonic()
        return True

    def context_usage_pct(self) -> float:
        return 1.0

    async def start(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None


# ── the collaborators behind the seam ───────────────────────────────────────


class _Recorder:
    """Each public method of *spec* called on it becomes a :class:`Call` and
    returns ``None``; a name *spec* does not have raises, as it would in
    production."""

    def __init__(self, spec: type, calls: Callable[[], list[Call]], clock: VirtualClock) -> None:
        self._spec = spec
        self._calls = calls
        self._clock = clock

    def __getattr__(self, name: str) -> Callable[..., None]:
        if name.startswith("_") or not callable(getattr(self._spec, name, None)):
            raise AttributeError(name)

        def _record(*args: Any, **kwargs: Any) -> None:
            self._calls().append(Call(name, args, dict(kwargs), self._clock.elapsed()))

        return _record


class _NoHooks:
    """A hook store with no hooks configured: every event fires nothing."""

    async def fire(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []


#: What the turn asks the session manager, answered the way ``SessionManager``
#: answers for one healthy, unlinked dashboard session with nothing pending.
#: Each is a plain value of the real method's return type, never a ``MagicMock``:
#: a mock reads truthy, and a turn asking "is a replay pending?" would hear yes.
#: An async method's answer is what awaiting it gives.
_SESSION_ANSWERS: dict[str, Any] = {
    "aflush": None,
    "allocation_requested_model": "",
    "begin_turn": None,
    "commit_provider_switch_replay_sid": False,
    "compact_wait_budget_secs": 60.0,
    "consume_needs_reinjection": False,
    "consume_provider_switch_replay": False,
    "consume_replay_suppression": False,
    "destroy": None,
    "effective_autocompact_pct": 80.0,
    "get_pid": None,
    "is_mirror_paused": False,
    "is_slack_paused": False,
    "mapped_sid": "",
    "mark_needs_reinjection": None,
    "mark_provider_switch_replay": False,
    "provider_switch_replay_pending": False,
    "record_failure": False,
    "record_success": None,
    "recycle_background": None,
    "release": None,
    "remove": None,
    "reset": True,
    "resumable_sid": None,
    "set_active_dashboard_slots": None,
    "set_approval_policy": None,
    "set_compacting_callback": None,
    "stop_generation": 0,
}
#: The real in-memory link stores ``chat_test_helpers._make_state`` builds,
#: carried onto the specced manager.
_HELPER_LINK_STORES = (
    "clear_mirror_link",
    "clear_mirror_link_if",
    "clear_slack_link",
    "clear_slack_link_if",
    "find_mirror_sessions",
    "get_mirror_link",
    "get_origin_link",
    "get_slack_link",
    "mirror_link_nonce",
    "set_mirror_link",
    "set_origin_link",
    "set_slack_link",
    "slack_link_nonce",
)
#: Answered per turn, from the turn's own provider (see ``_answer_sessions``).
_LIVE_SESSION_ANSWERS = ("check_context_usage", "get_or_create", "get_provider", "has_session")
#: Answered from the link stores, as the real manager answers them from its map.
_LINK_SESSION_ANSWERS = ("get_session_for_thread", "mirror_accepts_inbound")


def _session_manager(helper: Any, unanswered: list[str]) -> Any:
    """A session manager specced on ``SessionManager``.

    A renamed or removed method is an AttributeError here rather than a fresh
    truthy mock. A method the harness gives no answer raises where it is called
    and is noted in *unanswered*, so the run fails by name even when production
    code catches the error around that call.
    """
    from kiro_crew.session import SessionManager

    sessions = create_autospec(SessionManager, instance=True)
    sessions.count = 0
    sessions.final_drain_started = False
    for name in _HELPER_LINK_STORES:
        getattr(sessions, name).side_effect = getattr(helper, name).side_effect
    _link_answers(sessions, helper)
    known = {*_SESSION_ANSWERS, *_HELPER_LINK_STORES, *_LIVE_SESSION_ANSWERS}
    known |= set(_LINK_SESSION_ANSWERS)
    for name in dir(SessionManager):
        if name.startswith("_") or name in known:
            continue
        if isinstance(inspect.getattr_static(SessionManager, name), property):
            continue

        def _unanswered(*_args: Any, _name: str = name, **_kwargs: Any) -> Any:
            unanswered.append(_name)
            raise AssertionError(
                f"the turn called SessionManager.{_name}, which the harness does not "
                "answer: give it the healthy session's answer in _SESSION_ANSWERS"
            )

        getattr(sessions, name).side_effect = _unanswered
    return sessions


def _link_answers(sessions: Any, helper: Any) -> None:
    """Answer the reverse lookups from the same link stores the setters write."""
    slack_keys: list[str] = []

    def _set_slack_link(key: str, thread_ts: str, channel_id: str | None) -> None:
        if key not in slack_keys:
            slack_keys.append(key)
        helper.set_slack_link.side_effect(key, thread_ts, channel_id)

    def _session_for_thread(thread_ts: str) -> str | None:
        for key in slack_keys:
            if sessions.get_slack_link(key)[0] == thread_ts:
                return key
        return None

    def _accepts_inbound(key: str) -> bool:
        link = sessions.get_mirror_link(key)
        return link is not None and key in sessions.find_mirror_sessions(link, inbound_only=True)

    sessions.set_slack_link.side_effect = _set_slack_link
    sessions.get_session_for_thread.side_effect = _session_for_thread
    sessions.mirror_accepts_inbound.side_effect = _accepts_inbound


def _answer_sessions(
    sessions: Any,
    calls: list[Call],
    clock: VirtualClock,
    provider: ScriptedProvider,
    allocation_error: BaseException | None,
) -> None:
    def _answer(name: str, value: Any) -> Callable[..., Any]:
        def _call(*args: Any, **kwargs: Any) -> Any:
            calls.append(Call(name, args, dict(kwargs), clock.elapsed()))
            return value

        return _call

    for name, value in _SESSION_ANSWERS.items():
        getattr(sessions, name).side_effect = _answer(name, value)

    def _allocated() -> bool:
        return allocation_error is None and any(call.name == "get_or_create" for call in calls)

    async def _get_or_create(*args: Any, **kwargs: Any) -> tuple[Any, bool, bool]:
        calls.append(Call("get_or_create", args, dict(kwargs), clock.elapsed()))
        if allocation_error is not None:
            raise allocation_error
        return provider, True, False

    sessions.get_or_create.side_effect = _get_or_create
    # Live once the turn has acquired it, as the manager's own map would be.
    sessions.has_session.side_effect = lambda _key: _allocated()
    sessions.get_provider.side_effect = lambda _key: provider if _allocated() else None
    sessions.check_context_usage.side_effect = lambda _key, client: client.context_usage_pct()


def _autonudge_service(calls: Callable[[], list[Call]], clock: VirtualClock) -> Any:
    """A running AutoNudge service specced on ``AutoNudgeService``, with no goal
    loop armed: every call is recorded, and ``add``/``remove`` are awaitable."""
    from kiro_crew.autonudge import AutoNudgeService

    answers: dict[str, Any] = {"get_by_id": None, "get_by_slot": None, "list_all": []}
    service = create_autospec(AutoNudgeService, instance=True)
    for name in dir(AutoNudgeService):
        if name.startswith("_"):
            continue
        if isinstance(inspect.getattr_static(AutoNudgeService, name), property):
            continue

        def _record(*args: Any, _name: str = name, **kwargs: Any) -> Any:
            calls().append(Call(_name, args, dict(kwargs), clock.elapsed()))
            return answers.get(_name)

        getattr(service, name).side_effect = _record
    return service


def _slot_for(state: Any, spec: SlotSpec) -> Any:
    # Through the registry's own constructor, so what it derives at creation (the
    # restricted-transcript marker of a temporary slot, app ownership) is there.
    slot = state.get_or_create_slot(
        spec.key,
        model=spec.model,
        memory_mode=spec.memory_mode,
        app=spec.app,
        linked_session_key=spec.linked_session_key,
    )
    # A titled slot schedules no background title generation; nothing here is
    # about titles.
    slot._titled = True
    slot._human_seen = spec.human_seen
    slot._trust = spec.trust
    slot._trust_reads = spec.trust_reads
    slot._trust_scope = spec.trust_scope
    for role, content in spec.rows:
        slot.append(role, content, f"msg msg-{role[:1]}", broadcast=False)
    return slot


def _answerer(
    state: Any, slot: Any, answers: Mapping[str, str | Sequence[str]], turn: Any
) -> Callable[..., None]:
    """Answer each prompt once it is pending, as a person's click would."""
    from kiro_crew.dashboard import chat_handlers

    loop = asyncio.get_running_loop()
    # The futures themselves, not their ids: a freed future's id is reused, and
    # a later prompt would then read as already answered.
    seen: list[asyncio.Future[str]] = []
    asked: dict[str, int] = {}

    def _next_answer(request_id: str) -> str:
        given = answers.get(request_id, UNANSWERED)
        if isinstance(given, str):
            return given
        index = asked.get(request_id, 0)
        asked[request_id] = index + 1
        return given[index] if index < len(given) else UNANSWERED

    def _answer(request_id: str, future: asyncio.Future[str], answer: str) -> None:
        if future.done():
            return
        if answer == CANCEL_TURN:
            turn.cancel()
        elif answer == STOP:
            chat_handlers._unblock_pending_waits(state, slot)
        elif answer == APPROVED_TRUST_READS:
            # The "trust reads" button resolves the future itself; the resolver
            # takes only approved / rejected / rejected-once.
            future.set_result(APPROVED_TRUST_READS)
        elif answer in _DECISIONS:
            state.resolve_slot_approval(
                slot,
                request_id,
                answer == APPROVED,
                rejected_once=answer == REJECTED_ONCE,
                expected_future=future,
            )

    def _after_slots_update(*args: Any, **kwargs: Any) -> None:
        # The runner pushes a slots update right after it registers a prompt.
        for request_id, future in list(slot._approval_futures.items()):
            if future.done() or any(future is old for old in seen):
                continue
            seen.append(future)
            # On the next loop step, not now: the runner has registered the
            # prompt but not yet awaited it, and nobody can click a card before
            # the turn is waiting on it.
            loop.call_soon(_answer, request_id, future, _next_answer(request_id))

    return _after_slots_update


def _successor_recorder(
    successors: Callable[[], list[Call]], clock: VirtualClock
) -> Callable[..., Any]:
    """Stand in for the queue hand-off's dispatch: record the turn, never run it.

    One turn per call. A hand-off (a requeue, a recovery continuation, a queued
    message) would otherwise start a SECOND real turn against a script written
    for one; what it would have run is part of this turn's record.
    """

    def _dispatch(state: Any, slot: Any, coro: Any, **kwargs: Any) -> asyncio.Future[None]:
        # *coro* is the queue-exit guard wrapping the successor ``_run_chat``; its
        # ``args``/``kwargs`` locals are the successor's call. A guard that
        # renamed them would fail every hand-off test here, by name.
        bound = inspect.getcoroutinelocals(coro)
        coro.close()
        successors().append(
            Call(
                "turn",
                tuple(bound.get("args", ())),
                dict(bound.get("kwargs", {})),
                clock.elapsed(),
            )
        )
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        done.set_result(None)
        return done

    return _dispatch


def _record_crew_log(
    mp: pytest.MonkeyPatch, calls: Callable[[], list[Call]], clock: VirtualClock
) -> None:
    """Record every ``crew_log.emit.on_*`` entry point the turn reaches, then run it.

    The runner calls them as attributes of the module (``crew_log_emit.on_x``),
    so a spy installed on the module is the one it reaches.
    """
    from kiro_crew.crew_log import emit

    for name in dir(emit):
        real = getattr(emit, name)
        if not name.startswith("on_") or not callable(real):
            continue

        def _spy(*args: Any, _name: str = name, _real: Any = real, **kwargs: Any) -> Any:
            calls().append(Call(_name, args, dict(kwargs), clock.elapsed()))
            return _real(*args, **kwargs)

        mp.setattr(emit, name, _spy)


def _stop_reason(crew_log: Sequence[Call]) -> str:
    for call in reversed(crew_log):
        if call.name == "on_turn_completed":
            return str(call.kwargs.get("stop_reason") or "")
        if call.name == "on_turn_failed":
            return f"failed: {call.kwargs.get('error', '')}"
    return ""


def _write_config(config: Mapping[str, Any] | None) -> Callable[[], None]:
    """Write *config* as the data home's ``config.json``; return its restore.

    The loader's cache is dropped after both writes: it fingerprints the file,
    and a same-size rewrite inside one coarse mtime tick would otherwise be
    served the previous config.
    """
    from kiro_crew.config.loader import _invalidate_config_cache, config_path

    if config is None:
        return lambda: None
    path = config_path()
    previous = path.read_bytes() if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(config)), encoding="utf-8")
    _invalidate_config_cache()

    def _restore() -> None:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(previous)
        _invalidate_config_cache()

    return _restore


# ── the run ─────────────────────────────────────────────────────────────────


@dataclasses.dataclass
class _Capture:
    """What one turn did; the recorders write into the CURRENT turn's capture."""

    started: float = 0.0
    ended: float = 0.0
    frames: list[WsFrame] = dataclasses.field(default_factory=list)
    provider: list[Call] = dataclasses.field(default_factory=list)
    sessions: list[Call] = dataclasses.field(default_factory=list)
    audit: list[Call] = dataclasses.field(default_factory=list)
    crew_log: list[Call] = dataclasses.field(default_factory=list)
    nudges: list[Call] = dataclasses.field(default_factory=list)
    successors: list[Call] = dataclasses.field(default_factory=list)
    rows: tuple[Mapping[str, Any], ...] = ()
    window: tuple[Mapping[str, Any], ...] = ()


@dataclasses.dataclass
class _Sink:
    current: _Capture = dataclasses.field(default_factory=_Capture)
    turns: list[_Capture] = dataclasses.field(default_factory=list)
    #: Session-manager methods the run called that the harness does not answer.
    unanswered: list[str] = dataclasses.field(default_factory=list)

    def begin(self, clock: VirtualClock) -> _Capture:
        self.current = _Capture(started=clock.elapsed())
        self.turns.append(self.current)
        return self.current


def _state_for(home: Path, spec: SlotSpec, sink: _Sink, clock: VirtualClock) -> Any:
    state = _make_state(home)
    state.sessions = _session_manager(state.sessions, sink.unanswered)

    def _channel(channel: str) -> Callable[..., int]:
        def _send(kind: str, payload: Any = None) -> int:
            sink.current.frames.append(WsFrame(channel, kind, payload, clock.elapsed()))
            return 0

        return _send

    async def _deliver_owners(kind: str, payload: Any = None) -> int:
        return _channel("owners")(kind, payload)

    def _note(note: dict[str, Any]) -> None:
        kind = str(note.get("_type", ""))
        sink.current.frames.append(WsFrame("all", kind, note, clock.elapsed()))

    state.broadcast_ws = _channel("all")
    state.broadcast_ws_owners = _channel("owners")
    state.deliver_ws_owners = _deliver_owners
    # Transcript rows reach clients as ``chat_message`` notes through here, not
    # through ``broadcast_ws``.
    state._broadcast = _note
    # Re-reads a PR's status from the forge at turn boundaries; not this turn's.
    state.refresh_slot_source_status = lambda _key: None
    state.context_builder = None
    state.consolidator = None
    state._hook_store = _NoHooks()
    state.slack_client = None
    state.is_yolo_active = lambda: spec.yolo
    return state


async def _one_turn(
    script: TurnScript, state: Any, slot: Any, sink: _Sink, clock: VirtualClock
) -> None:
    from kiro_crew.dashboard import chat_runner
    from kiro_crew.dashboard.chat_utils import slot_history_key
    from kiro_crew.dashboard.turn_dispatch import chat_turn_timeout_secs, spawn_guarded_turn

    seen = sink.begin(clock)
    ctx = TurnContext(state=state, slot=slot, clock=clock)
    provider = (script.provider or ScriptedProvider)(script.events, ctx, seen.provider)
    ctx.provider = provider
    _answer_sessions(state.sessions, seen.sessions, clock, provider, script.allocation_error)
    if script.setup is not None:
        prepared = script.setup(ctx)
        if inspect.isawaitable(prepared):
            await prepared
    if script.user_row:
        # What the dashboard send handler does for a person before it starts
        # the turn (``api_chat``): their row, marked as a human turn.
        slot.append("user", script.message, "msg msg-u", meta={HUMAN_TURN_META_KEY: True})

    turn = spawn_guarded_turn(
        state,
        slot,
        chat_runner._run_chat(
            state,
            slot,
            script.message,
            **{"_directive_user_origin": script.user_row, **dict(script.run_kwargs)},
        ),
        timeout_secs=chat_turn_timeout_secs(),
    )
    slot.task = turn
    state.push_slots_update = MagicMock(side_effect=_answerer(state, slot, script.answers, turn))
    with contextlib.suppress(BaseException):
        await turn
    await _settle(state)
    log = state.conversation_log
    key = slot_history_key(slot)
    seen.rows = tuple(log.read_messages(key)) if log.has_log(key) else ()
    seen.window = tuple(json.loads(json.dumps(row, default=str)) for row in slot.messages)
    seen.ended = clock.elapsed()


async def _drive(
    script: TurnScript, spec: SlotSpec, clock: VirtualClock, home: Path, sink: _Sink
) -> None:
    state = _state_for(home, spec, sink, clock)
    slot = _slot_for(state, spec)
    turn: TurnScript | None = script
    while turn is not None:
        await _one_turn(turn, state, slot, sink, clock)
        turn = turn.then


async def _settle(state: Any) -> None:
    """Let what the turn scheduled behind itself finish, then cancel the rest."""
    pending = [task for task in list(state._background_tasks) if not task.done()]
    if pending:
        await asyncio.wait(pending, timeout=_SETTLE_SECS)
    leftover = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
    for task in leftover:
        task.cancel()
    for task in leftover:
        with contextlib.suppress(BaseException):
            await task


def _run_on_virtual_loop(
    script: TurnScript, spec: SlotSpec, clock: VirtualClock, home: Path, sink: _Sink
) -> None:
    with asyncio.Runner(loop_factory=lambda: _VirtualTimeLoop(clock)) as runner:
        runner.run(_drive(script, spec, clock, home, sink))


def _record_of(turns: Sequence[_Capture]) -> TurnRecord:
    seen, rest = turns[0], turns[1:]
    return TurnRecord(
        ws_frames=tuple(seen.frames),
        history_rows=seen.rows,
        window=seen.window,
        audit_events=tuple(seen.audit),
        crew_log=tuple(seen.crew_log),
        provider_calls=tuple(seen.provider),
        session_calls=tuple(seen.sessions),
        autonudge_calls=tuple(seen.nudges),
        successors=tuple(seen.successors),
        stop_reason=_stop_reason(seen.crew_log),
        elapsed=seen.ended - seen.started,
        then=_record_of(rest) if rest else None,
    )


async def run_turn(
    script: TurnScript,
    *,
    slot: SlotSpec | None = None,
    config: Mapping[str, Any] | None = None,
    clock: VirtualClock | None = None,
) -> TurnRecord:
    """Run one dashboard turn in *slot* as *script* says; return what it did.

    *config* is written as the data home's ``config.json`` for the run (the
    real loader reads it) and put back afterwards. *clock* is the run's virtual
    time; by default a fresh one.
    """
    from kiro_crew import autonudge
    from kiro_crew.dashboard import chat_runner
    from kiro_crew.sel import SecurityEventLog

    spec = slot or SlotSpec()
    clock = clock or VirtualClock()
    sink = _Sink()
    restore_config = _write_config(config)
    try:
        with (
            pytest.MonkeyPatch.context() as mp,
            tempfile.TemporaryDirectory(prefix="turn-") as home,
        ):
            audit_log = _Recorder(SecurityEventLog, lambda: sink.current.audit, clock)
            mp.setattr(chat_runner, "sel", lambda: audit_log)
            mp.setattr(
                chat_runner,
                "spawn_guarded_turn",
                _successor_recorder(lambda: sink.current.successors, clock),
            )
            nudges = _autonudge_service(lambda: sink.current.nudges, clock)
            mp.setattr(autonudge, "_INSTANCE", nudges if spec.autonudge else None)
            _record_crew_log(mp, lambda: sink.current.crew_log, clock)
            await asyncio.to_thread(_run_on_virtual_loop, script, spec, clock, Path(home), sink)
    finally:
        restore_config()
    if sink.unanswered:
        names = ", ".join(f"SessionManager.{name}" for name in dict.fromkeys(sink.unanswered))
        raise AssertionError(
            f"the turn called {names}, which the harness does not answer (whether or "
            "not production caught the error): give it the healthy session's answer "
            "in _SESSION_ANSWERS"
        )
    return _record_of(sink.turns)
