"""State a dashboard turn shares with code outside it, and with its own awaits.

A dashboard turn's working state is reached by other code while the turn is
suspended: the steer handler calls the segment cut the turn publishes on the
slot, and a cancel can land on any await. A cached copy, a flag returned instead
of written in place, or a handle kept past the turn would each change what the
turn does. These pin, through real turns driven by ``turn_harness.run_turn``:

* a steer cut made by another task while the turn awaits, then a cancel, saves the
  cut text once;
* a write made just before an await survives a cancel that lands on that await:
  the session reset, the closer's label, the sign-in outcome, and the cancel arm's
  mark that keeps a discarded turn's history owed;
* the consumption callbacks fire once, or are handed to the successor, which
  reports through them;
* the slot keeps no path to the turn's provider once the turn has ended, and the
  segment cut the turn publishes is freed by reference counting alone;
* a linked Slack mirror's task stream advances on each tool call and is
  completed and stopped at the end;
* a landed turn publishes the per-prompt stats its client reports, and its usage
  row carries the completion's own billing.
"""

from __future__ import annotations

import asyncio
import functools
import gc
import inspect
import json
import sys
import types
import weakref
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from turn_harness import (
    Do,
    Raise,
    ScriptedProvider,
    TurnContext,
    TurnRecord,
    TurnScript,
    Wait,
    run_turn,
)

from kiro_crew import runtime_death
from kiro_crew.acp.transport_errors import AcpAuthRequired, AcpError, AcpProcessDied
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    STOP_REASON_END_TURN,
    AcpEvent,
    AcpPromptStats,
    TurnUsage,
)
from kiro_crew.dashboard.handlers import usage as usage_handlers
from kiro_crew.llm_helpers import PromptBusyExhaustedError
from kiro_crew.slack.client import SlackClientOps

_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
_BEFORE = AcpEvent(kind=EVENT_TEXT_CHUNK, text="before steer")
_AFTER = AcpEvent(kind=EVENT_TEXT_CHUNK, text="after steer")
_PARTIAL = AcpEvent(kind=EVENT_TEXT_CHUNK, text="partial answer")


@pytest.fixture
def fresh_runtime_deaths() -> Iterator[None]:
    """No process death recorded and no shared-death streak, before and after."""
    runtime_death._reset_for_tests()
    yield
    runtime_death._reset_for_tests()


def _tool(n: int) -> AcpEvent:
    return AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id=f"t{n}", title=f"read_{n}", tool_kind="read")


def _cancel(ctx: TurnContext) -> None:
    ctx.slot.task.cancel()


def _steer_from_another_task(ctx: TurnContext) -> None:
    # The steer handler runs on the same loop while the turn awaits its stream.
    asyncio.get_running_loop().call_soon(ctx.slot._steer_segment_cut)


def _assistant(record: TurnRecord) -> list[str]:
    return [row["content"] for row in record.rows("assistant")]


class TestSteerCutAndCancel:
    @pytest.mark.asyncio
    async def test_turn_shared_state_steer_cut_then_cancel_saves_the_text_once(self) -> None:
        record = await run_turn(
            TurnScript(
                events=[_BEFORE, Do(_steer_from_another_task), Wait(1), Do(_cancel), Wait(1), _DONE]
            )
        )

        # The cut saved the text; the cancel found nothing left to save.
        assert _assistant(record) == ["before steer"]
        [sent] = record.crew("on_message_sent")
        assert sent.kwargs["interrupted"] is True
        assert record.stop_reason == "failed: CancelledError"

    @pytest.mark.asyncio
    async def test_turn_shared_state_text_after_a_steer_cut_is_saved_on_its_own(self) -> None:
        record = await run_turn(
            TurnScript(
                events=[
                    _BEFORE,
                    Do(_steer_from_another_task),
                    Wait(1),
                    _AFTER,
                    Do(_cancel),
                    Wait(1),
                    _DONE,
                ]
            )
        )

        assert _assistant(record) == ["before steer", "after steer"]
        assert len(record.crew("on_message_sent")) == 2
        assert [row["role"] for row in record.window] == ["user", "assistant", "assistant", "done"]


class _CancelOnTheSaveAwait:
    """Cancel the turn at the first await after its partial reply is appended.

    With the gateway draining, an abnormal end saves the partial reply off the
    loop before it goes on; the cancel is queued as the reply row is broadcast,
    so it lands on that save's await, right after the arm's earlier writes.
    """

    def __call__(self, ctx: TurnContext) -> None:
        ctx.state.sessions.final_drain_started = True
        notes = ctx.state._broadcast
        armed = [True]

        def _note(note: dict[str, Any]) -> Any:
            if armed and note.get("_type") == "chat_message" and note.get("role") == "assistant":
                armed.clear()
                asyncio.get_running_loop().call_soon(ctx.slot.task.cancel)
            return notes(note)

        ctx.state._broadcast = _note


class TestACancelOnAnArmsAwait:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("error", "label", "auth_required"),
        [
            (lambda: AcpProcessDied("process exited"), "AcpProcessDied", False),
            (lambda: AcpAuthRequired("Not signed in"), "", True),
            (lambda: PromptBusyExhaustedError(), "", False),
            (lambda: AcpError("process exited"), "", False),
            (lambda: AcpError("Session not found"), "", False),
        ],
        ids=[
            "process-died",
            "auth-required",
            "prompt-busy-exhausted",
            "pipe-death",
            "session-lost",
        ],
    )
    async def test_turn_shared_state_cancel_after_the_reset_flag_still_resets(
        self,
        fresh_runtime_deaths: None,
        error: Callable[[], BaseException],
        label: str,
        auth_required: bool,
    ) -> None:
        slots: list[Any] = []
        record = await run_turn(
            TurnScript(
                events=[_PARTIAL, Do(_CancelOnTheSaveAwait()), Raise(error())],
                setup=lambda ctx: slots.append(ctx.slot),
            )
        )

        sessions = [call.name for call in record.session_calls]
        # Written before the await: the reset the teardown owes, the closer's label
        # and the sign-in outcome the queue hand-off reads.
        assert "reset" in sessions
        [failed] = record.crew("on_turn_failed")
        assert failed.kwargs["error"] == label
        assert slots[0]._last_turn_auth_required is auth_required
        assert sessions.count("release") == 1

    @pytest.mark.asyncio
    async def test_turn_shared_state_cancel_after_the_generic_label_keeps_it(self) -> None:
        record = await run_turn(
            TurnScript(events=[_PARTIAL, Do(_CancelOnTheSaveAwait()), Raise(RuntimeError("boom"))])
        )

        [failed] = record.crew("on_turn_failed")
        assert failed.kwargs["error"] == "RuntimeError"
        assert [call.name for call in record.session_calls].count("release") == 1

    @pytest.mark.asyncio
    async def test_turn_shared_state_a_landed_turn_settles_the_history_it_carried(self) -> None:
        record = await run_turn(TurnScript(events=[_PARTIAL, _DONE]))

        sessions = [call.name for call in record.session_calls]
        assert sessions.count("mark_first_turn_history_owed") == 1
        assert sessions.count("consume_first_turn_history_owed") == 1

    @pytest.mark.asyncio
    async def test_turn_shared_state_cancel_on_the_cancel_arms_save_keeps_the_history_owed(
        self,
    ) -> None:
        record = await run_turn(
            TurnScript(events=[_PARTIAL, Do(_CancelOnTheSaveAwait()), Do(_cancel), Wait(1), _DONE])
        )

        sessions = [call.name for call in record.session_calls]
        # The cancel arm marks the turn cancelled before its save awaits. A cancelled
        # turn is one the backend discards, so the history it carried stays owed.
        assert sessions.count("mark_first_turn_history_owed") == 1
        assert "consume_first_turn_history_owed" not in sessions
        assert record.stop_reason == "failed: CancelledError"
        assert sessions.count("release") == 1


class _Consumption:
    """The two consumption callbacks a dispatcher hands a turn, recording calls."""

    def __init__(self) -> None:
        self.reports: list[tuple[Any, ...]] = []

    def consumed(self, value: bool) -> None:
        self.reports.append(("consumed", value))

    def irreversibly_consumed(self) -> None:
        self.reports.append(("irreversible",))

    def kwargs(self) -> dict[str, Any]:
        return {
            "_on_consumed": self.consumed,
            "_on_irreversibly_consumed": self.irreversibly_consumed,
        }


class TestConsumptionIsReportedOnce:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "events",
        [
            [AcpEvent(kind=EVENT_TEXT_CHUNK, text="the reply"), _DONE],
            [_PARTIAL, Do(_cancel), Wait(1), _DONE],
        ],
        ids=["landed", "cancelled-after-text"],
    )
    async def test_turn_shared_state_a_consuming_turn_reports_each_once(
        self, events: list[Any]
    ) -> None:
        consumption = _Consumption()
        record = await run_turn(TurnScript(events=events, run_kwargs=consumption.kwargs()))

        assert consumption.reports == [("irreversible",), ("consumed", True)]
        assert record.successors == ()

    @pytest.mark.asyncio
    async def test_turn_shared_state_a_requeued_turn_hands_the_reports_on(
        self, fresh_runtime_deaths: None
    ) -> None:
        consumption = _Consumption()
        record = await run_turn(
            TurnScript(
                events=[Raise(AcpProcessDied("process exited"))],
                run_kwargs=consumption.kwargs(),
            )
        )

        # Nothing is reported for a turn that did not consume the message...
        assert consumption.reports == []
        # ...and the successor it hands off to reports through the same callbacks.
        [successor] = record.successors
        successor.kwargs["_on_consumed"](True)
        reported = successor.kwargs["_on_irreversibly_consumed"]()
        if inspect.isawaitable(reported):
            await reported
        assert consumption.reports == [("consumed", True), ("irreversible",)]

    @pytest.mark.asyncio
    async def test_turn_shared_state_an_empty_reply_takes_its_report_back(self) -> None:
        consumption = _Consumption()
        record = await run_turn(TurnScript(events=[_DONE], run_kwargs=consumption.kwargs()))

        # Requeued verbatim: the message is delivered again, so the report is retracted.
        assert consumption.reports == [("consumed", True), ("consumed", False)]
        [successor] = record.successors
        successor.kwargs["_on_consumed"](True)
        assert consumption.reports[-1] == ("consumed", True)


class _Handle:
    """An inner client that points back at its provider, as the ACP client does."""

    def __init__(self, owner: Any) -> None:
        self.owner = owner


class _OwnedProvider(ScriptedProvider):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.client = _Handle(self)


def _reaches(root: Any, target: Any, stops: list[Any]) -> bool:
    """Whether *target* is reachable from *root* by strong references.

    The walk does not enter *stops*, modules, module globals or classes: the
    gateway-lifetime objects (the dashboard state, the session manager that owns
    every live provider) reach everything, and the question is what the SLOT
    keeps on its own account.
    """
    module_globals = {
        id(getattr(module, "__dict__", None)) for module in list(sys.modules.values())
    }
    fenced = {id(stop) for stop in stops} | module_globals
    seen: set[int] = set()
    pending = [root]
    while pending:
        obj = pending.pop()
        if obj is target:
            return True
        if id(obj) in seen or id(obj) in fenced or isinstance(obj, (type, types.ModuleType)):
            continue
        seen.add(id(obj))
        pending.extend(gc.get_referents(obj))
    return False


def _closed_over(fn: Any) -> Iterator[Any]:
    """The values *fn* holds: a partial's bound arguments, then its function's closure
    cells, skipping a cell nothing has bound yet."""
    if isinstance(fn, functools.partial):
        yield from fn.args
        yield from fn.keywords.values()
        fn = fn.func
    for cell in getattr(fn, "__closure__", None) or ():
        try:
            yield cell.cell_contents
        except ValueError:
            continue


class TestNothingOnTheSlotOutlivesTheTurn:
    @pytest.mark.asyncio
    async def test_turn_shared_state_slot_keeps_no_path_to_the_turns_provider(self) -> None:
        # Not "freed without gc.collect()": the harness's TurnContext and the specced
        # session manager hold the provider in reference cycles, and the dashboard
        # state stays reachable from process-wide services, so the provider stays
        # alive after run_turn whether or not the collector runs. What a turn owns
        # must not stay reachable from the slot, which outlives every turn.
        seen: dict[str, Any] = {}

        def _while_running(ctx: TurnContext) -> None:
            seen["ctx"] = ctx
            seen["during"] = _reaches(ctx.slot, ctx.provider, [ctx.state, ctx.state.sessions, ctx])

        record = await run_turn(
            TurnScript(
                events=[AcpEvent(kind=EVENT_TEXT_CHUNK, text="hi"), Do(_while_running), _DONE],
                provider=_OwnedProvider,
            )
        )

        ctx = seen["ctx"]
        assert record.stop_reason == STOP_REASON_END_TURN
        # Reachable through the published handle while the turn runs...
        assert seen["during"] is True
        # ...and through nothing once it has ended.
        assert _reaches(ctx.slot, ctx.provider, [ctx.state, ctx.state.sessions, ctx]) is False

    @pytest.mark.asyncio
    async def test_turn_shared_state_the_published_cut_dies_without_the_collector(self) -> None:
        # The cut the turn publishes for the steer handler closes over the turn's
        # state. Freed by reference counting alone once the turn ends, with the
        # collector off, it is in no cycle that would keep the turn alive until a
        # collection runs.
        cuts: list[weakref.ref[Any]] = []

        def _while_running(ctx: TurnContext) -> None:
            cuts.append(weakref.ref(ctx.slot._steer_segment_cut))

        was_enabled = gc.isenabled()
        gc.disable()
        try:
            record = await run_turn(TurnScript(events=[_PARTIAL, Do(_while_running), _DONE]))
            assert record.stop_reason == STOP_REASON_END_TURN
            [cut] = cuts
            assert cut() is None
        finally:
            if was_enabled:
                gc.enable()

    @pytest.mark.asyncio
    async def test_turn_shared_state_the_turn_object_dies_without_the_collector(self) -> None:
        # Every closure that reads the turn's shared state holds the turn object. Were
        # the turn to hold a closure in turn, the two would form a cycle that keeps every
        # ended turn, and the client on it, alive until a collection runs.
        from kiro_crew.dashboard import chat_runner

        turns: list[weakref.ref[Any]] = []

        def _while_running(ctx: TurnContext) -> None:
            turns.extend(
                weakref.ref(value)
                for value in _closed_over(ctx.slot._steer_segment_cut)
                if isinstance(value, chat_runner._ChatTurn)
            )

        was_enabled = gc.isenabled()
        gc.disable()
        try:
            record = await run_turn(TurnScript(events=[_PARTIAL, Do(_while_running), _DONE]))
            assert record.stop_reason == STOP_REASON_END_TURN
            [turn] = turns
            assert turn() is None
        finally:
            if was_enabled:
                gc.enable()


class _SlackRecorder:
    """A Slack client that records each call the turn makes, bound to the real port.

    Only methods the Slack client port declares exist, and each call is bound to
    that method's signature, so the arguments are read by name.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        declared = getattr(SlackClientOps, name, None) if not name.startswith("_") else None
        if not inspect.iscoroutinefunction(declared):
            raise AttributeError(name)
        signature = inspect.signature(declared)

        async def _call(*args: Any, **kwargs: Any) -> str:
            bound = signature.bind(self, *args, **kwargs)
            bound.apply_defaults()
            self.calls.append((name, dict(bound.arguments)))
            return f"{name}-ts"

        return _call


class TestSlackMirrorTaskStream:
    @pytest.mark.asyncio
    async def test_turn_shared_state_mirror_task_stream_advances_on_each_tool_call(
        self,
    ) -> None:
        slack = _SlackRecorder()

        def _linked(ctx: TurnContext) -> None:
            ctx.state.slack_client = slack
            ctx.state.sessions.set_slack_link("dashboard:chat-1", "thread-1", "C-1")

        record = await run_turn(
            TurnScript(
                events=[
                    _tool(1),
                    _tool(2),
                    AcpEvent(kind=EVENT_TEXT_CHUNK, text="the reply"),
                    _DONE,
                ],
                setup=_linked,
            )
        )

        names = [name for name, _ in slack.calls]
        updates = [args for name, args in slack.calls if name == "append_task"]
        task_ids: list[str] = []
        for args in updates:
            if args["task_id"] not in task_ids:
                task_ids.append(args["task_id"])
        # One task per tool call, each opened then completed.
        assert [
            [(args["title"], args["status"]) for args in updates if args["task_id"] == task_id]
            for task_id in task_ids
        ] == [
            [("read_1", "in_progress"), ("read_1", "complete")],
            [("read_2", "in_progress"), ("read_2", "complete")],
        ]
        # The next tool call completes the previous task before opening its own.
        first, second = task_ids
        statuses = [(args["task_id"], args["status"]) for args in updates]
        assert statuses.index((first, "complete")) < statuses.index((second, "in_progress"))
        # The stream is started before the first task and stopped last, once.
        assert names.index("start_stream") < names.index("append_task")
        assert names[-1] == "stop_stream"
        assert names.count("stop_stream") == 1
        assert record.stop_reason == STOP_REASON_END_TURN


class _StatsProvider(ScriptedProvider):
    """A client that reports per-prompt stats, fresh for each prompt."""

    def stream(self, message: str, *, allow_image: bool = True) -> Any:
        self.last_prompt_stats = AcpPromptStats(
            credits=2.5,
            event_count=7,
            tool_calls=[("t1", "read"), ("t2", "read"), ("t3", "edit")],
        )
        return super().stream(message, allow_image=allow_image)


class TestALandedTurnReportsItsStats:
    @pytest.mark.asyncio
    async def test_turn_shared_state_landed_turn_publishes_stats_and_writes_usage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(usage_handlers, "_TOKEN_USAGE_DIR", tmp_path)
        record = await run_turn(
            TurnScript(
                events=[
                    AcpEvent(kind=EVENT_TEXT_CHUNK, text="the reply"),
                    AcpEvent(
                        kind=EVENT_COMPLETE,
                        stop_reason=STOP_REASON_END_TURN,
                        usage=TurnUsage(credits=4.0),
                    ),
                ],
                provider=_StatsProvider,
            )
        )

        [stats] = [
            frame.payload["text"]
            for frame in record.frames("activity_event")
            if frame.payload.get("kind") == "stats"
        ]
        # The client's own counts, not a fresh empty stats object.
        assert "7 events" in stats
        assert "3 tool calls" in stats
        rows = [
            json.loads(line)
            for shard in sorted(tmp_path.glob("*.jsonl"))
            for line in shard.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        # The usage row is billed from the completion event, not the client's stats.
        assert [(row["stop_reason"], row["credits"]) for row in rows] == [
            (STOP_REASON_END_TURN, 4.0)
        ]
        assert record.stop_reason == STOP_REASON_END_TURN
