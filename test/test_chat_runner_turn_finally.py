"""The duties a dashboard turn performs on every exit, and their order.

Whatever ends a dashboard turn (a landed reply, an error in the stream, a process
death, a terminal ACP error, a sign-in failure, prompt-busy exhaustion, a closing
session manager, a cancel, a refused session, or a local slash command that never
reaches the provider), the turn still:

* hands the session back exactly once, and retires its identity and its
  in-flight marker before the cycle's last frame;
* drops the live handles it published on the slot and clears the turn-scoped
  wait and audience state, and breaks the pre-stream exhaustion streak when
  output streamed;
* closes the turn once in the crew log, after its open tool calls and its last
  step;
* re-arms AutoNudge, records its sign-in outcome, and ends the cycle with one
  ``chat_done`` or one hand-off;
* reads its permission-wait and unfinished-children snapshots before it drops the
  handles and closes the cards they are read from.

The exits are one table, so a duty skipped on one path shows up as one red row.
Everything is read from the ``TurnRecord`` and the slot after the turn, plus the
moment of the ``release`` and of the cycle's last frame; nothing reads the
runner's source.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import pytest
from turn_harness import (
    Do,
    Raise,
    ScriptedProvider,
    SlotSpec,
    Step,
    TurnContext,
    TurnRecord,
    TurnScript,
    Wait,
    run_turn,
)

from kiro_crew import runtime_death
from kiro_crew.acp.transport_errors import AcpAuthRequired, AcpError, AcpProcessDied
from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, STOP_REASON_END_TURN, AcpEvent
from kiro_crew.crew_log import emit
from kiro_crew.llm_helpers import PromptBusyExhaustedError
from kiro_crew.session_allocation import SessionClosingError

_REPLY = AcpEvent(kind=EVENT_TEXT_CHUNK, text="the reply")
_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
_CLOSER_ENTRIES = (
    "close_open_tool_calls",
    "on_step_completed",
    "on_turn_completed",
    "on_turn_failed",
)


class _Handle:
    """The inner client a provider publishes as ``slot._acp_client``."""

    def __init__(self, awaiting_permission: bool = False) -> None:
        self._awaiting_permission = awaiting_permission


class _HandleProvider(ScriptedProvider):
    """A provider whose inner handle the turn publishes on the slot while it runs."""

    awaiting_permission = False

    @property
    def client(self) -> _Handle:
        return _Handle(self.awaiting_permission)


class _AwaitingProvider(_HandleProvider):
    awaiting_permission = True


class _Teardown:
    """Seeds the turn-scoped state a teardown must clear, and notes what it saw."""

    def __init__(self) -> None:
        self.ctx: TurnContext | None = None
        self.turn_task: Any = None
        self.mid: dict[str, bool] = {}
        self.moments: list[tuple[str, str, int]] = []
        self.order: list[str] = []

    def setup(self, ctx: TurnContext) -> None:
        self.ctx = ctx
        slot = ctx.slot
        slot._wait_state = {"seeded": True}
        slot._end_wait_request = {"seeded": True}
        slot._wait_contested = True
        slot._steer_audience_fences["seeded-fence"] = {}
        slot._steer_audience_fence_holders["seeded-fence"] = 1
        slot._prestream_exhausted_cycles = 1
        release = ctx.state.sessions.release.side_effect

        def _release(*args: Any, **kwargs: Any) -> Any:
            self._moment("release")
            return release(*args, **kwargs)

        ctx.state.sessions.release.side_effect = _release
        frames, notes = ctx.state.broadcast_ws, ctx.state._broadcast

        def _frame(kind: str, payload: Any = None) -> Any:
            self._seen_running_turn()
            if kind in ("chat_done", "queue_pop"):
                self._moment(kind)
            return frames(kind, payload)

        def _note(note: dict[str, Any]) -> Any:
            self._seen_running_turn()
            return notes(note)

        ctx.state.broadcast_ws, ctx.state._broadcast = _frame, _note

    def _seen_running_turn(self) -> None:
        # The first thing the turn shows anyone happens while its own task runs.
        if self.turn_task is None:
            self.turn_task = self.slot.task

    def _moment(self, name: str) -> None:
        slot = self.slot
        self.moments.append((name, slot._active_turn_session_key, slot._turn_in_flight_generation))

    def during(self, ctx: TurnContext) -> None:
        self.mid = {
            "client": ctx.slot._acp_client is not None,
            "segment_cut": ctx.slot._steer_segment_cut is not None,
        }

    def cancel(self, ctx: TurnContext) -> None:
        ctx.slot.task.cancel()

    @property
    def slot(self) -> Any:
        assert self.ctx is not None
        return self.ctx.slot


@pytest.fixture
def fresh_runtime_deaths() -> Iterator[None]:
    """No process death recorded and no shared-death streak, before and after."""
    runtime_death._reset_for_tests()
    yield
    runtime_death._reset_for_tests()


@pytest.fixture
def teardown(monkeypatch: pytest.MonkeyPatch, fresh_runtime_deaths: None) -> _Teardown:
    """A ``_Teardown`` whose ``order`` lists the crew-log closer entries in order.

    ``close_open_tool_calls`` is not an ``on_*`` entry point, so the record does not
    hold it; the closers are wrapped here, ahead of the harness's own recorders,
    so all of them land in one list.
    """
    seen = _Teardown()
    for name in _CLOSER_ENTRIES:
        real = getattr(emit, name)

        def _closer(*args: Any, _name: str = name, _real: Any = real, **kwargs: Any) -> Any:
            seen.order.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(emit, name, _closer)
    return seen


#: The closer label of the sign-in, prompt-busy and ``AcpError`` arms: they close the
#: turn with an empty error label, not the name of the class they caught.
_UNNAMED: tuple[str, ...] = ("",)


@dataclasses.dataclass(frozen=True)
class _Exit:
    #: The provider stream, built fresh for every run so no exception outlives it.
    events: Callable[[_Teardown], Sequence[Step]] = lambda _t: ()
    allocation_error: Callable[[], BaseException] | None = None
    message: str = "hello"
    #: A session was acquired, so it must be released exactly once.
    acquired: bool = True
    #: The turn reached its main ``try`` (a local command answers above it).
    reached_try: bool = True
    #: ``None`` when the turn never opened in the crew log; else the closer and the
    #: error labels it may carry (empty for a turn that completed).
    closer: tuple[str, tuple[str, ...]] | None = None
    #: ``chat_done`` ends the cycle, else the queue hands off to one successor.
    ends_cycle: bool = True
    auth_required: bool = False
    #: Model output streamed before the exit.
    streamed: bool = True


def _streamed(*tail: Callable[[], Step]) -> Callable[[_Teardown], Sequence[Step]]:
    return lambda t: [_REPLY, Do(t.during), *(make() for make in tail)]


_EXITS = {
    "landed": _Exit(events=_streamed(lambda: _DONE), closer=("on_turn_completed", ())),
    "error-after-chunk": _Exit(
        events=_streamed(lambda: Raise(RuntimeError("boom"))),
        closer=("on_turn_failed", ("RuntimeError",)),
    ),
    "process-died": _Exit(
        events=_streamed(lambda: Raise(AcpProcessDied("process exited"))),
        closer=("on_turn_failed", ("AcpProcessDied",)),
        ends_cycle=False,
    ),
    "terminal-acp-error": _Exit(
        events=_streamed(lambda: Raise(AcpError("bad request", transient=False))),
        closer=("on_turn_failed", _UNNAMED),
    ),
    "auth-required": _Exit(
        events=_streamed(lambda: Raise(AcpAuthRequired("Not signed in"))),
        closer=("on_turn_failed", _UNNAMED),
        auth_required=True,
    ),
    "prompt-busy-exhausted": _Exit(
        events=_streamed(lambda: Raise(PromptBusyExhaustedError())),
        closer=("on_turn_failed", _UNNAMED),
        ends_cycle=False,
    ),
    "session-closing": _Exit(
        events=_streamed(lambda: Raise(SessionClosingError("closing"))),
        closer=("on_turn_failed", ("SessionClosingError",)),
    ),
    "cancelled": _Exit(
        events=lambda t: [_REPLY, Do(t.during), Do(t.cancel), Wait(1), _DONE],
        closer=("on_turn_failed", ("CancelledError",)),
    ),
    "refused-allocation": _Exit(
        allocation_error=lambda: RuntimeError("spawn refused"), acquired=False, streamed=False
    ),
    "local-command": _Exit(
        message="/goal ship the release", acquired=False, reached_try=False, streamed=False
    ),
}


async def _run_exit(exit_: _Exit, seen: _Teardown, provider: type = _HandleProvider) -> TurnRecord:
    return await run_turn(
        TurnScript(
            events=exit_.events(seen),
            allocation_error=exit_.allocation_error() if exit_.allocation_error else None,
            message=exit_.message,
            setup=seen.setup,
            provider=provider,
        ),
        slot=SlotSpec(autonudge=True),
    )


def _names(calls: Sequence[Any]) -> list[str]:
    return [call.name for call in calls]


class TestEveryExitPerformsItsDuties:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", list(_EXITS))
    async def test_turn_finally_hands_back_the_session_and_ends_the_cycle_once(
        self, name: str, teardown: _Teardown
    ) -> None:
        exit_ = _EXITS[name]
        record = await _run_exit(exit_, teardown)

        releases = [moment for moment in teardown.moments if moment[0] == "release"]
        assert len(releases) == int(exit_.acquired)
        if exit_.acquired and exit_.closer and exit_.closer[0] == "on_turn_failed":
            # A turn that did not land still has its in-flight marker at the release;
            # the marker is retired after the session is handed back.
            assert releases[0][2] > 0
        slot = teardown.slot
        assert slot._active_turn_session_key == ""
        assert slot._turn_in_flight_generation == 0
        # The turn's own task has finished, and the slot does not keep it.
        assert teardown.turn_task is not None and teardown.turn_task.done()
        if exit_.ends_cycle:
            assert slot.task is None
        else:
            assert slot.task is not teardown.turn_task
        # One chat_done, or one hand-off, and that last frame sees the identity
        # and the in-flight marker already gone.
        done = [frame.kind for frame in record.ws_frames].count("chat_done")
        assert (done, len(record.successors)) == ((1, 0) if exit_.ends_cycle else (0, 1))
        [last] = [moment for moment in teardown.moments if moment[0] != "release"]
        assert last == ("chat_done" if exit_.ends_cycle else "queue_pop", "", 0)
        if exit_.acquired:
            order = [moment[0] for moment in teardown.moments]
            assert order.index("release") < order.index(last[0])
        # AutoNudge's idle timer is re-armed on every exit, once.
        assert _names(record.autonudge_calls).count("notify_turn_complete") == 1
        assert slot._last_turn_auth_required is exit_.auth_required

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", list(_EXITS))
    async def test_turn_finally_drops_what_it_held_and_closes_the_crew_log_once(
        self, name: str, teardown: _Teardown
    ) -> None:
        exit_ = _EXITS[name]
        record = await _run_exit(exit_, teardown)

        slot = teardown.slot
        if exit_.streamed:
            # Both handles were live while the turn streamed...
            assert teardown.mid == {"client": True, "segment_cut": True}
        # ...and neither outlives it, so a late steer cannot reach a finished turn.
        assert slot._acp_client is None
        assert slot._steer_segment_cut is None
        assert slot._steer_audience_fences == {}
        assert slot._steer_audience_fence_holders == {}
        # Only a turn that reached its try can have run a wait.
        wait_state = (slot._wait_state, slot._end_wait_request, slot._wait_contested)
        if exit_.reached_try:
            assert wait_state == (None, None, False)
        else:
            assert wait_state == ({"seeded": True}, {"seeded": True}, True)
        # Model output breaks the pre-stream exhaustion streak.
        assert slot._prestream_exhausted_cycles == (0 if exit_.streamed else 1)

        if exit_.closer is None:
            assert record.crew_log == ()
            assert teardown.order == []
            return
        closer, labels = exit_.closer
        names = _names(record.crew_log)
        assert [n for n in names if n in ("on_turn_completed", "on_turn_failed")] == [closer]
        assert names[-1] == closer
        # Open tool calls, then the last step, then the turn: in that order, last.
        assert teardown.order[-3:] == ["close_open_tool_calls", "on_step_completed", closer]
        assert teardown.order.count(closer) == 1
        if labels:
            [failed] = record.crew("on_turn_failed")
            assert failed.kwargs["error"] in labels


class TestTeardownSnapshots:
    @pytest.mark.asyncio
    async def test_turn_finally_reads_the_permission_wait_before_dropping_the_client(
        self, teardown: _Teardown
    ) -> None:
        await _run_exit(_EXITS["error-after-chunk"], teardown, provider=_AwaitingProvider)

        # Read off the live handle, which the same teardown then drops.
        assert teardown.slot._last_turn_awaiting_permission is True
        assert teardown.slot._acp_client is None

    @pytest.mark.asyncio
    async def test_turn_finally_reads_unfinished_children_before_closing_their_cards(
        self, teardown: _Teardown
    ) -> None:
        def _child_still_running(ctx: TurnContext) -> None:
            ctx.slot._native_subagent_tracker["child-1"] = {
                "done": False,
                "agent": "a",
                "task": "t",
            }

        record = await run_turn(
            TurnScript(
                events=[_REPLY, Do(_child_still_running), Raise(RuntimeError("boom"))],
                setup=teardown.setup,
                provider=_HandleProvider,
            )
        )

        assert teardown.slot._last_turn_children_announced is True
        assert [frame.kind for frame in record.ws_frames].count("subagent_done") == 1

    @pytest.mark.asyncio
    async def test_turn_finally_local_command_keeps_the_recorded_sign_in_outcome(
        self, teardown: _Teardown
    ) -> None:
        def _signed_out_before(ctx: TurnContext) -> None:
            teardown.setup(ctx)
            ctx.slot._last_turn_auth_required = True

        await run_turn(TurnScript(message="/goal ship the release", setup=_signed_out_before))

        # A turn that never reached its try records no auth outcome of its own, so a
        # queue held after a sign-in failure stays held.
        assert teardown.slot._last_turn_auth_required is True
