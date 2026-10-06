"""The turn harness's own interface: what ``run_turn`` promises a test.

``turn_harness.run_turn`` is the test surface for a dashboard turn, so its
promises are pinned here once rather than re-derived by every caller: the
record says what the real turn did, time is virtual and exact, a script's
answers and steps reach the turn where it stands, a hand-off is recorded and
not run, and nothing the harness patched for the run outlives it.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
from typing import Any

import pytest
from turn_harness import (
    APPROVED,
    APPROVED_TRUST_READS,
    CANCEL_TURN,
    REJECTED,
    REJECTED_ONCE,
    STOP,
    Do,
    Emit,
    Raise,
    ScriptedProvider,
    SlotSpec,
    TurnContext,
    TurnRecord,
    TurnScript,
    VirtualClock,
    Wait,
    _counted_wrap_future,
    _Recorder,
    _VirtualTimeLoop,
    run_turn,
)

from kiro_crew import autonudge
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_STEER_CONSUMED,
    EVENT_TEXT_CHUNK,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.constants import TOOL_APPROVAL_TIMEOUT
from kiro_crew.context import ContextBuilder
from kiro_crew.crew_log import emit
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard import turn_dispatch as td
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.sel import SecurityEventLog
from kiro_crew.start_priority import StartPriority

_REPLY = AcpEvent(kind=EVENT_TEXT_CHUNK, text="the reply")
_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)


def _prompt(request_id: str = "req-1") -> AcpEvent:
    return AcpEvent(
        kind=EVENT_PERMISSION_REQUEST, request_id=request_id, title="fs_write", tool_kind="edit"
    )


def _shape(record: TurnRecord) -> dict:
    """What a record says, minus the ids production mints per run (row ids,
    stream generations, wall-clock row stamps) and the wall-clock ``heartbeat``
    liveness frame (see the harness's docstring)."""
    return {
        "frames": [
            (frame.channel, frame.kind, frame.at)
            for frame in record.ws_frames
            if frame.kind != "heartbeat"
        ],
        "rows": [(row["role"], row["content"]) for row in record.history_rows],
        "provider": [(call.name, call.args, call.at) for call in record.provider_calls],
        "crew": [call.name for call in record.crew_log],
        "audit": [(call.name, call.at) for call in record.audit_events],
        "sessions": [(call.name, call.at) for call in record.session_calls],
        "stop": record.stop_reason,
        "elapsed": record.elapsed,
    }


async def _wrap_future_now() -> Any:
    return asyncio.wrap_future


def _tool_calls(record: TurnRecord) -> list[tuple[str, tuple]]:
    return [(call.name, call.args) for call in record.provider_calls if "tool" in call.name]


class TestTheRecord:
    @pytest.mark.asyncio
    async def test_a_landed_turn_is_recorded_from_its_rows_to_its_stop(self) -> None:
        record = await run_turn(TurnScript(events=[_REPLY, _DONE], message="hi there"))
        assert [row["content"] for row in record.rows("user")] == ["hi there"]
        assert [row["content"] for row in record.rows("assistant")] == ["the reply"]
        chunks = [frame.payload["content"] for frame in record.frames("chat_chunk")]
        assert "".join(chunks) == "the reply"
        assert [call.args for call in record.calls("stream")] == [("hi there",)]
        assert record.crew("on_turn_completed")
        assert record.stop_reason == STOP_REASON_END_TURN
        assert record.then is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("user_row", "priority"),
        [(True, StartPriority.FOREGROUND), (False, StartPriority.BACKGROUND)],
        ids=["person", "no-person"],
    )
    async def test_the_session_is_acquired_at_the_turns_start_priority(
        self, user_row: bool, priority: StartPriority
    ) -> None:
        # A person typing is waiting on the cold start; anything else is not.
        record = await run_turn(TurnScript(events=[_REPLY, _DONE], user_row=user_row))
        [allocation] = record.allocations
        assert allocation.kwargs["start_priority"] is priority

    @pytest.mark.asyncio
    async def test_the_same_script_gives_the_same_record(self) -> None:
        script = TurnScript(events=[Wait(5), _prompt(), _REPLY, _DONE])
        first = await run_turn(script)
        second = await run_turn(script)
        assert _shape(first) == _shape(second)

    @pytest.mark.asyncio
    async def test_audits_select_by_field(self) -> None:
        record = await run_turn(
            TurnScript(events=[_prompt(), _REPLY, _DONE], answers={"req-1": REJECTED})
        )
        [refused] = record.audits(request_id="req-1", outcome="rejected")
        assert refused["tool_name"] == "fs_write"
        assert record.audits(outcome="approved") == []

    @pytest.mark.asyncio
    async def test_a_prompt_is_recorded_as_a_card_and_a_decision(self) -> None:
        record = await run_turn(
            TurnScript(events=[_prompt(), _REPLY, _DONE], answers={"req-1": APPROVED})
        )
        [card] = record.approval_cards
        assert card["content"] == "fs_write"
        assert json.loads(card["cls"])["request_id"] == "req-1"
        [decision] = record.approval_decisions
        assert (decision["approval_id"], decision["decision"]) == ("req-1", "approved")
        # The card itself is transient: in the slot's window, never persisted.
        assert [row["role"] for row in record.window].count("permission") == 1
        assert record.rows("permission") == []


class TestVirtualTime:
    @pytest.mark.asyncio
    async def test_a_wait_costs_exactly_its_virtual_seconds(self) -> None:
        record = await run_turn(TurnScript(events=[Wait(1234.5), _REPLY, _DONE]))
        assert record.elapsed == 1234.5
        assert record.frames("chat_chunk")[0].at == 1234.5

    @pytest.mark.asyncio
    async def test_an_unanswered_prompt_expires_at_exactly_its_window(self) -> None:
        record = await run_turn(
            TurnScript(events=[_prompt(), _REPLY, _DONE]), slot=SlotSpec(autonudge=True)
        )
        [reject] = record.calls("reject_tool")
        assert reject.at == TOOL_APPROVAL_TIMEOUT
        assert record.notify_approval_stalled == [("chat-1", TOOL_APPROVAL_TIMEOUT)]

    @pytest.mark.asyncio
    async def test_a_provider_that_never_answers_is_cut_at_the_turn_ceiling(self) -> None:
        class _Silent(ScriptedProvider):
            def stream(self, message: str, *, allow_image: bool = True):
                self.record("stream", message)
                return self._never()

            async def _never(self):
                await asyncio.Event().wait()
                yield _DONE

        ceiling = 900
        record = await run_turn(
            TurnScript(provider=_Silent), config={"agent": {"chat_turn_timeout_secs": ceiling}}
        )
        assert record.elapsed == ceiling
        # The card is the slot's; the cut turn is never saved after it.
        assert [row["content"] for row in record.window if row["role"] == "error"] == [
            td.format_turn_timeout_card(float(ceiling))
        ]

    @pytest.mark.asyncio
    async def test_a_passed_clock_is_the_one_the_turn_runs_on(self) -> None:
        clock = VirtualClock(start=50.0)
        await run_turn(TurnScript(events=[Wait(7), _REPLY, _DONE]), clock=clock)
        assert clock.now >= 57.0
        assert clock.elapsed() == clock.now - 50.0

    def test_executor_work_finishes_before_virtual_time_moves(self) -> None:
        """A timer never fires past work still running on a thread: virtual time
        jumps only when the loop has nothing else it could be doing."""
        clock = VirtualClock(start=0.0)
        order: list[tuple[str, float]] = []
        release = threading.Event()

        def _job() -> None:
            assert release.wait(timeout=5)

        async def _main() -> None:
            loop = asyncio.get_running_loop()
            loop.call_later(600, lambda: order.append(("timer", loop.time())))
            job = loop.run_in_executor(None, _job)
            loop.call_soon(release.set)
            await job
            order.append(("job", loop.time()))
            await asyncio.sleep(600)

        with asyncio.Runner(loop_factory=lambda: _VirtualTimeLoop(clock)) as runner:
            runner.run(_main())
        assert order == [("job", 0.0), ("timer", 600.0)]

    def test_a_cancelled_wait_on_executor_work_still_holds_virtual_time(self) -> None:
        """Cancelling the await does not stop the job on its thread, so the loop
        must keep waiting on the job, not read itself as deadlocked or jump time."""
        clock = VirtualClock(start=0.0)
        started, release = threading.Event(), threading.Event()
        finished: list[float] = []

        async def _main() -> None:
            loop = asyncio.get_running_loop()
            done = loop.create_future()

            def _job() -> None:
                started.set()
                assert release.wait(timeout=5)
                finished.append(clock.now)
                loop.call_soon_threadsafe(done.set_result, None)

            job = loop.run_in_executor(None, _job)
            await loop.run_in_executor(None, started.wait)
            job.cancel()
            loop.call_soon(release.set)
            # Only the job, still running on its thread, can complete this.
            await done
            await asyncio.sleep(600)

        with asyncio.Runner(loop_factory=lambda: _VirtualTimeLoop(clock)) as runner:
            runner.run(_main())
        assert finished == [0.0]
        assert clock.now == 600.0

    def test_a_job_its_executor_drops_unstarted_is_cancelled_not_waited_on(self) -> None:
        clock = VirtualClock(start=0.0)
        started, release = threading.Event(), threading.Event()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)

        def _busy() -> bool:
            started.set()
            return release.wait(timeout=5)

        async def _main() -> None:
            loop = asyncio.get_running_loop()
            busy = loop.run_in_executor(pool, _busy)
            await loop.run_in_executor(None, started.wait)
            # The pool's only worker is busy, so this one waits in its queue...
            queued = loop.run_in_executor(pool, lambda: "never")
            # ...until a shutdown drops it, unstarted.
            pool.shutdown(wait=False, cancel_futures=True)
            loop.call_soon(release.set)
            with pytest.raises(asyncio.CancelledError):
                await queued
            assert await busy is True
            await asyncio.sleep(600)

        try:
            with asyncio.Runner(loop_factory=lambda: _VirtualTimeLoop(clock)) as runner:
                runner.run(_main())
        finally:
            pool.shutdown(wait=True)
        assert clock.now == 600.0

    def test_work_handed_to_a_pool_directly_also_holds_virtual_time(self) -> None:
        """Code that submits to its own pool and awaits ``wrap_future`` (the embed
        pool does) is waited on like ``run_in_executor`` work: a timer armed
        meanwhile fires after the job, at its own virtual second."""
        clock = VirtualClock(start=0.0)
        started, release = threading.Event(), threading.Event()
        order: list[tuple[str, float]] = []
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)

        def _job() -> bool:
            started.set()
            return release.wait(timeout=5)

        async def _main() -> None:
            loop = asyncio.get_running_loop()
            timer = asyncio.ensure_future(asyncio.sleep(100))
            job = pool.submit(_job)
            await loop.run_in_executor(None, started.wait)
            loop.call_soon(release.set)
            assert await asyncio.wrap_future(job, loop=loop) is True
            order.append(("job", loop.time()))
            await timer
            order.append(("timer", loop.time()))

        try:
            with asyncio.Runner(loop_factory=lambda: _VirtualTimeLoop(clock)) as runner:
                runner.run(_main())
        finally:
            pool.shutdown(wait=True)
        assert order == [("job", 0.0), ("timer", 100.0)]

    def test_asyncio_is_itself_again_once_the_loop_closes(self) -> None:
        with asyncio.Runner(loop_factory=lambda: _VirtualTimeLoop(VirtualClock())) as runner:
            assert runner.run(_wrap_future_now()) is _counted_wrap_future
        assert asyncio.wrap_future is not _counted_wrap_future
        assert asyncio.futures.wrap_future is not _counted_wrap_future

    def test_a_close_that_raises_leaves_the_loop_counting(self) -> None:
        async def _close_while_running() -> Any:
            with pytest.raises(RuntimeError):
                asyncio.get_running_loop().close()
            return asyncio.wrap_future

        with asyncio.Runner(loop_factory=lambda: _VirtualTimeLoop(VirtualClock())) as runner:
            assert runner.run(_close_while_running()) is _counted_wrap_future
        assert asyncio.wrap_future is not _counted_wrap_future

    def test_a_hold_is_released_only_after_its_result_reaches_the_loop(self) -> None:
        """Released first, the clock could jump to the next timer in the gap
        before the result lands. The job is finished on the loop's own thread, so
        the order of the two hand-backs is the code's, not a thread race's."""
        clock = VirtualClock(start=0.0)
        delivered_at_release: list[bool] = []

        async def _main() -> None:
            loop = asyncio.get_running_loop()
            job: concurrent.futures.Future[str] = concurrent.futures.Future()
            waiting = asyncio.wrap_future(job, loop=loop)
            hold = clock._hold

            def _watched(change: int) -> None:
                if change < 0:
                    delivered_at_release.append(waiting.done())
                hold(change)

            clock._hold = _watched  # type: ignore[method-assign]
            try:
                loop.call_soon(job.set_result, "done")
                assert await waiting == "done"
            finally:
                del clock._hold

        with asyncio.Runner(loop_factory=lambda: _VirtualTimeLoop(clock)) as runner:
            runner.run(_main())
        assert delivered_at_release == [True]

    @pytest.mark.asyncio
    async def test_a_turn_that_assembles_its_context_in_the_embed_pool_completes(self) -> None:
        # A real ContextBuilder: _run_chat awaits build_message on the embed pool.
        def _real_builder(ctx) -> None:
            ctx.state.context_builder = ContextBuilder()

        record = await run_turn(
            TurnScript(events=[_REPLY, _DONE], message="with context", setup=_real_builder)
        )
        assert record.stop_reason == STOP_REASON_END_TURN
        [(prompt,)] = [call.args for call in record.calls("stream")]
        assert "with context" in prompt
        assert record.elapsed < 60

    def test_a_loop_with_nothing_left_to_run_fails_by_name(self) -> None:
        clock = VirtualClock()

        async def _forever() -> None:
            await asyncio.get_running_loop().create_future()

        with asyncio.Runner(loop_factory=lambda: _VirtualTimeLoop(clock)) as runner:
            with pytest.raises(RuntimeError, match="deadlocked"):
                runner.run(_forever())


class TestTheScript:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("answer", "wire"),
        [
            (APPROVED, "approve_tool"),
            (APPROVED_TRUST_READS, "approve_tool"),
            (REJECTED, "reject_tool"),
            (REJECTED_ONCE, "reject_tool"),
            (STOP, "reject_tool"),
        ],
    )
    async def test_each_answer_reaches_the_prompt_it_names(self, answer: str, wire: str) -> None:
        record = await run_turn(
            TurnScript(events=[_prompt(), _REPLY, _DONE], answers={"req-1": answer})
        )
        assert _tool_calls(record) == [(wire, ("req-1",))]
        # Answered on the spot, not by the window expiring.
        assert record.calls(wire)[0].at == 0.0

    @pytest.mark.asyncio
    async def test_cancelling_the_turn_ends_it_mid_prompt(self) -> None:
        record = await run_turn(
            TurnScript(events=[_prompt(), _REPLY, _DONE], answers={"req-1": CANCEL_TURN})
        )
        assert record.stop_reason == "failed: CancelledError"
        assert record.rows("assistant") == []

    @pytest.mark.asyncio
    async def test_answers_for_a_repeated_request_id_are_taken_in_order(self) -> None:
        record = await run_turn(
            TurnScript(
                events=[_prompt(), _REPLY, _prompt(), _DONE],
                answers={"req-1": [REJECTED, APPROVED]},
            )
        )
        # Both on the spot: a dropped answer would expire into a reject instead.
        assert [(call.name, call.at) for call in record.provider_calls if "tool" in call.name] == [
            ("reject_tool", 0.0),
            ("approve_tool", 0.0),
        ]

    @pytest.mark.asyncio
    async def test_every_prompt_of_a_long_turn_gets_its_own_answer(self) -> None:
        # Many prompts in one turn: each is answered, none mistaken for an earlier
        # one whose future has since been freed.
        ids = [f"req-{n}" for n in range(8)]
        record = await run_turn(
            TurnScript(
                events=[*(_prompt(request_id) for request_id in ids), _DONE],
                answers={request_id: APPROVED for request_id in ids},
            )
        )
        assert [(call.name, call.args, call.at) for call in record.calls("approve_tool")] == [
            ("approve_tool", (request_id,), 0.0) for request_id in ids
        ]
        assert record.calls("reject_tool") == []

    def test_an_unknown_answer_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown answers"):
            TurnScript(answers={"req-1": "maybe"})
        with pytest.raises(ValueError, match="unknown answers"):
            TurnScript(answers={"req-1": [APPROVED, "later"]})

    @pytest.mark.asyncio
    async def test_a_raise_step_fails_the_turn_where_it_stands(self) -> None:
        record = await run_turn(TurnScript(events=[Raise(RuntimeError("boom"))]))
        assert record.stop_reason == "failed: RuntimeError"
        assert [row["content"] for row in record.rows("error")] == ["boom"]

    @pytest.mark.asyncio
    async def test_an_allocation_error_never_reaches_the_provider(self) -> None:
        record = await run_turn(TurnScript(allocation_error=RuntimeError("spawn refused")))
        assert record.calls("stream") == []
        assert [row["content"] for row in record.rows("error")] == ["spawn refused"]
        assert len(record.allocations) == 1

    @pytest.mark.asyncio
    async def test_do_and_setup_run_on_the_turns_loop(self) -> None:
        seen: list[str] = []

        def _setup(ctx) -> None:
            seen.append(f"setup:{ctx.slot.key}")

        async def _mid_stream(ctx) -> None:
            seen.append(f"do:{ctx.clock.elapsed()}")

        record = await run_turn(
            TurnScript(events=[Wait(3), Do(_mid_stream), _REPLY, _DONE], setup=_setup),
            slot=SlotSpec(key="chat-do"),
        )
        assert seen == ["setup:chat-do", "do:3.0"]
        assert record.stop_reason == STOP_REASON_END_TURN

    @pytest.mark.asyncio
    async def test_an_emit_step_builds_its_event_from_the_turn_so_far(self) -> None:
        built: list[str] = []

        def _echo(ctx) -> AcpEvent:
            built.append(ctx.provider.recorded("stream")[0].args[0])
            return AcpEvent(kind=EVENT_STEER_CONSUMED, text="")

        record = await run_turn(TurnScript(events=[Emit(_echo), _REPLY, _DONE], message="x"))
        assert built == ["x"]
        assert record.stop_reason == STOP_REASON_END_TURN

    @pytest.mark.asyncio
    async def test_a_hand_off_is_recorded_not_run(self) -> None:
        # An empty reply requeues the message; the successor turn it dispatches is
        # in the record, and the provider is prompted once.
        record = await run_turn(TurnScript(events=[_DONE], message="again please"))
        [successor] = record.successors
        assert successor.args[0] == "again please"
        assert len(record.calls("stream")) == 1

    @pytest.mark.asyncio
    async def test_a_then_turn_is_the_slots_next_message(self) -> None:
        record = await run_turn(
            TurnScript(
                events=[_REPLY, _DONE],
                message="first",
                then=TurnScript(events=[Wait(4), _REPLY, _DONE], message="second"),
            )
        )
        assert record.then is not None
        assert [call.args for call in record.calls("stream")] == [("first",)]
        # Each turn gets a fresh provider session, so the second is handed the
        # conversation so far ahead of its own message.
        [(prompt,)] = [call.args for call in record.then.calls("stream")]
        assert prompt.endswith("second")
        assert [row["content"] for row in record.then.rows("user")] == ["first", "second"]
        assert record.then.elapsed == 4.0


class TestTheCollaborators:
    @pytest.mark.asyncio
    async def test_a_fresh_session_names_no_predecessor(self) -> None:
        record = await run_turn(TurnScript(events=[_REPLY, _DONE]))
        [opened] = record.crew("on_session_opened")
        assert opened.kwargs["previous_sid"] == ""

    @pytest.mark.asyncio
    async def test_a_session_method_with_no_answer_fails_the_turn_by_name(self) -> None:
        def _ask_unanswered(ctx) -> None:
            ctx.state.sessions.note_stop(ctx.slot.key)

        with pytest.raises(AssertionError, match="SessionManager.note_stop"):
            await run_turn(TurnScript(events=[_REPLY, _DONE], setup=_ask_unanswered))

    @pytest.mark.asyncio
    async def test_an_unanswered_call_the_turn_catches_still_fails_the_run(self) -> None:
        # Production wraps many session reads in ``except Exception``; the run
        # must not pass just because the error was swallowed where it was raised.
        def _ask_and_swallow(ctx) -> None:
            try:
                ctx.state.sessions.note_stop(ctx.slot.key)
            except Exception:
                pass

        with pytest.raises(AssertionError, match="SessionManager.note_stop"):
            await run_turn(TurnScript(events=[Do(_ask_and_swallow), _REPLY, _DONE]))

    @pytest.mark.asyncio
    async def test_the_reverse_lookups_read_the_link_stores(self) -> None:
        seen: list[Any] = []

        def _link_and_ask(ctx) -> None:
            sessions, key = ctx.state.sessions, "dashboard:chat-1"
            seen.append(
                (sessions.get_session_for_thread("t-1"), sessions.mirror_accepts_inbound(key))
            )
            sessions.set_slack_link(key, "t-1", "C-1")
            mirror = ChannelLink(channel_type="slack", channel_id="C-2", thread_id="t-2")
            sessions.set_mirror_link(key, mirror, accepts_inbound=True)
            seen.append(
                (sessions.get_session_for_thread("t-1"), sessions.mirror_accepts_inbound(key))
            )

        await run_turn(TurnScript(events=[_REPLY, _DONE], setup=_link_and_ask))
        assert seen == [(None, False), ("dashboard:chat-1", True)]

    @pytest.mark.asyncio
    async def test_the_session_manager_has_only_the_real_methods(self) -> None:
        seen: list[bool] = []

        def _probe(ctx) -> None:
            seen.append(hasattr(ctx.state.sessions, "no_such_session_method"))

        await run_turn(TurnScript(events=[_REPLY, _DONE], setup=_probe))
        assert seen == [False]

    def test_the_audit_log_has_only_the_real_methods(self) -> None:
        audit = _Recorder(SecurityEventLog, list, VirtualClock())
        assert callable(audit.log_tool_invocation)
        with pytest.raises(AttributeError):
            audit.no_such_audit_method  # noqa: B018

    @pytest.mark.asyncio
    async def test_with_autonudge_off_a_goal_is_refused_as_the_gateway_refuses_it(self) -> None:
        record = await run_turn(TurnScript(message="/goal ship the release"))
        replies = [row["content"] for row in record.window if row["role"] != "user"]
        assert any("Goal loops are unavailable" in reply for reply in replies)
        assert record.autonudge_calls == ()

    @pytest.mark.asyncio
    async def test_with_autonudge_on_a_goal_is_armed_on_the_service(self) -> None:
        record = await run_turn(
            TurnScript(message="/goal ship the release"),
            slot=SlotSpec(key="chat-goal", autonudge=True),
        )
        [armed] = [call for call in record.autonudge_calls if call.name == "add"]
        assert armed.args == ("chat-goal",)
        assert armed.kwargs["max_cycles"] == 50
        assert "ship the release" in armed.kwargs["message"]


class TestTheProviderAdapter:
    @pytest.mark.asyncio
    async def test_a_steer_it_accepts_stamps_when_it_steered(self) -> None:
        # Steering and its stamp are one capability (harness-parity H15): the
        # keepalive route ends a sleeping wait by comparing the stamp.
        clock = VirtualClock()
        calls: list = []
        provider = ScriptedProvider([], TurnContext(state=None, slot=None, clock=clock), calls)
        assert provider.last_steer_monotonic == 0.0
        assert await provider.steer("look again") is True
        assert provider.last_steer_monotonic > 0.0
        assert [(call.name, call.args) for call in calls] == [("steer", ("look again",))]


class TestNothingOutlivesTheRun:
    @pytest.mark.asyncio
    async def test_every_patch_is_undone(self) -> None:
        before = (
            chat_runner.sel,
            chat_runner.spawn_guarded_turn,
            autonudge._INSTANCE,
            emit.on_turn_completed,
        )
        await run_turn(TurnScript(events=[_prompt(), _REPLY, _DONE]))
        after = (
            chat_runner.sel,
            chat_runner.spawn_guarded_turn,
            autonudge._INSTANCE,
            emit.on_turn_completed,
        )
        assert after == before

    @pytest.mark.asyncio
    async def test_config_is_the_turns_and_is_put_back(self) -> None:
        from kiro_crew.config.loader import config_path

        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"agent": {}}), encoding="utf-8")
        record = await run_turn(
            TurnScript(events=[_prompt(), _REPLY, _DONE]),
            config={"agent": {"tool_approval_timeout_secs": 300}},
        )
        assert record.calls("reject_tool")[0].at == 300
        assert json.loads(path.read_text(encoding="utf-8")) == {"agent": {}}

    @pytest.mark.asyncio
    async def test_an_absent_config_stays_absent(self) -> None:
        from kiro_crew.config.loader import config_path

        path = config_path()
        path.unlink(missing_ok=True)
        await run_turn(TurnScript(events=[_REPLY, _DONE]), config={"agent": {}})
        assert not path.exists()
