"""``chat_done`` reports ``run_seconds``: the ``task`` setter stamps a run's first
turn, and the frame that hands the floor back reads and clears the stamp. A stamp
is the run's once a turn of it passed the dispatch gates (``_turns_started``);
a task that leaves the slot before that takes its stamp with it.

Every reading of a run goes through ``dashboard.run_clock``. Each test installs a
:class:`ManualClock` there and advances it in whole seconds, so no assertion
depends on how long the worker really took; the event loop keeps real time."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state
from turn_harness import Do, SlotSpec, TurnScript, run_turn

from kiro_crew import autonudge
from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, STOP_REASON_END_TURN, AcpEvent
from kiro_crew.apps.builtins.spec_builder.backend.runtime import _reserve_slot_turn
from kiro_crew.apps.chat_slot_sdk import ChatSlotSDK
from kiro_crew.dashboard import chat_runner as cr
from kiro_crew.dashboard import run_clock
from kiro_crew.testing.clock import ManualClock

APP = "run-clock"


@pytest.fixture
def clock(_floor_monkeypatch) -> ManualClock:
    """The run clock, frozen at a whole second until the test advances it."""
    frozen = ManualClock(monotonic_start=1000.0)
    frozen.install(_floor_monkeypatch, run_clock)
    return frozen


@pytest.fixture
def run(tmp_path, clock):
    """A slot, and ``done(at)``: send a frame ``at`` whole seconds after the run's stamp."""
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(f"{APP}-job-1")
    slot._app = APP
    frames: list[dict] = []
    state.broadcast_ws = MagicMock(side_effect=lambda kind, data: frames.append(dict(data)))

    async def done(at: int, **frame) -> dict:
        start = slot._run_started_at
        clock.advance(at if start is None else start + at - clock.monotonic())
        await cr._send_chat_done(
            state, slot, payload={"slot": slot.key, "needs_input": False, **frame}
        )
        return frames[-1]

    return state, slot, clock, done


def _admit(slot) -> None:
    """What ``_turn_dispatch_gates`` records when a turn passes every gate."""
    slot._turns_started += 1


async def _a_turn_of_three_seconds(slot, clock, done) -> dict:
    """Start a turn after the slot went idle and end it 3 s later."""
    slot.task = MagicMock()
    assert slot._run_started_at == clock.monotonic()
    _admit(slot)
    return await done(3, turn=1, continuing=False)


@pytest.mark.asyncio
async def test_a_run_is_measured_from_its_first_turn_to_its_final_frame(run):
    _state, slot, _clock, done = run
    slot.task = MagicMock()
    _admit(slot)
    stamp = slot._run_started_at
    assert "run_seconds" not in await done(30, turn=1, continuing=True)
    slot.task = None
    slot.task = MagicMock()
    _admit(slot)
    assert slot._run_started_at == stamp
    assert (await done(1200, turn=2, continuing=False))["run_seconds"] == 1200
    assert slot._run_started_at is None

    slot.task = None
    slot.task = MagicMock()
    _admit(slot)
    assert (await done(5, turn=3, continuing=False))["run_seconds"] == 5


@pytest.mark.asyncio
async def test_the_report_rounds_a_fractional_clock_to_the_nearest_second(
    tmp_path, _floor_monkeypatch
):
    # A real monotonic stamp is fractional: 1000.1 + 90 - 1000.1 is 89.99999999999989
    # as a float, which truncation would report as 89.
    fractional = ManualClock(monotonic_start=1000.1)
    fractional.install(_floor_monkeypatch, run_clock)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(f"{APP}-job-1")
    frames: list[dict] = []
    state.broadcast_ws = MagicMock(side_effect=lambda kind, data: frames.append(dict(data)))
    slot.task = MagicMock()
    _admit(slot)
    fractional.advance(90)
    payload = {"slot": slot.key, "needs_input": False, "turn": 1, "continuing": False}
    await cr._send_chat_done(state, slot, payload=payload)
    assert frames[-1]["run_seconds"] == 90


@pytest.mark.asyncio
async def test_a_frame_that_ends_no_turn_neither_reports_nor_clears(run):
    _state, slot, _clock, done = run
    slot.task = MagicMock()
    _admit(slot)
    assert "run_seconds" not in await done(10, continuing=True)
    assert (await done(90, turn=1, continuing=False))["run_seconds"] == 90


@pytest.mark.asyncio
async def test_a_question_reports_and_clears_while_work_continues(run):
    _state, slot, _clock, done = run
    slot.task = MagicMock()
    _admit(slot)
    assert (await done(42, turn=1, continuing=True, needs_input=True))["run_seconds"] == 42
    assert "run_seconds" not in await done(50, turn=1, continuing=False)


@pytest.mark.asyncio
async def test_a_frame_for_a_turn_that_passed_no_gate_reports_nothing(run):
    _state, slot, _clock, done = run
    slot.task = MagicMock()
    assert "run_seconds" not in await done(0, turn=1, continuing=False)
    assert slot._run_started_at is None


@pytest.mark.asyncio
async def test_an_app_reservation_released_without_a_turn_leaves_no_stamp(run):
    state, slot, clock, done = run

    released = asyncio.Event()

    async def _refused_request() -> None:
        reservation = ChatSlotSDK(APP).reserve(state, slot)
        assert reservation is asyncio.current_task()
        # Runs after the reservation's own release callback, registered first.
        reservation.add_done_callback(lambda _task: released.set())

    await asyncio.create_task(_refused_request())
    await released.wait()
    assert slot.task is None
    assert slot._run_started_at is None
    assert (await _a_turn_of_three_seconds(slot, clock, done))["run_seconds"] == 3


@pytest.mark.asyncio
async def test_a_refused_spec_builder_reservation_leaves_no_stamp(run):
    state, slot, clock, done = run

    released = asyncio.Event()

    async def _refused_answer() -> None:
        reservation = _reserve_slot_turn(state, slot)
        assert reservation is asyncio.current_task()
        reservation.add_done_callback(lambda _task: released.set())

    await asyncio.create_task(_refused_answer())
    await released.wait()
    assert slot.task is None
    assert slot._run_started_at is None
    assert (await _a_turn_of_three_seconds(slot, clock, done))["run_seconds"] == 3


@pytest.mark.asyncio
async def test_a_drain_that_starts_nothing_leaves_no_stamp(run, _floor_monkeypatch):
    state, slot, clock, done = run

    async def _starts_nothing(_state, _slot) -> bool:
        return False

    _floor_monkeypatch.setattr(cr, "_start_next_queued_turn", _starts_nothing)

    released = asyncio.Event()

    async def _refused_answer() -> None:
        reservation = _reserve_slot_turn(state, slot)
        reservation.add_done_callback(lambda _task: released.set())
        slot._queue.append({"id": "queued", "content": "continue"})

    await asyncio.create_task(_refused_answer())
    await released.wait()
    drain = slot.task
    assert drain is not None
    await drain
    assert (await _a_turn_of_three_seconds(slot, clock, done))["run_seconds"] == 3


# ── whole turns: a person's message starts a new run ─────────────────────────
#
# Real dashboard turns (``turn_harness.run_turn``) on one slot. A turn opened by
# a person's message starts a new run at that turn's own start; any other turn
# continues the open run. So work that ends without a turn of its own (a loop
# switched off, an activity probe that failed, a child that was cancelled) leaves
# no stamp behind that the person's next message is measured from.

_HOUR = 3600
_REPLY = AcpEvent(kind=EVENT_TEXT_CHUNK, text="the reply")
_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)


def _done_frame(record) -> dict:
    [frame] = [f.payload for f in record.ws_frames if f.kind == "chat_done"]
    return frame


def _an_hour_later(clock: ManualClock):
    """A turn's setup: an hour passes on the run clock before the turn starts, while
    the run an earlier turn opened is still stamped."""

    def _age(ctx) -> None:
        assert ctx.slot._run_started_at is not None
        clock.advance(_HOUR)

    return _age


def _runs_for(clock: ManualClock, seconds: int) -> Do:
    """A turn's event: *seconds* pass on the run clock after the turn started."""
    return Do(lambda _ctx: clock.advance(seconds))


class _Pending:
    """Work that keeps the run open: armed by a turn, ended with no turn."""

    def __init__(self, how: str) -> None:
        self.how = how
        self.loop = SimpleNamespace(active=False)
        self.workflows = {"pending": False, "probe_fails": False}

    def arm(self, ctx) -> None:
        if self.how == "loop":
            self.loop.active = True
            autonudge.get_instance().get_by_slot.side_effect = lambda _key: self.loop
            return
        self.workflows["probe_fails"] = self.how == "probe"
        self.workflows["pending"] = self.how == "child"

        def _has_pending_work_for(_key: str) -> bool:
            if self.workflows["probe_fails"]:
                raise RuntimeError("workflow store unreadable")
            return self.workflows["pending"]

        ctx.state.workflow_service = SimpleNamespace(
            registry=SimpleNamespace(has_pending_work_for=_has_pending_work_for)
        )

    def end_without_a_turn(self) -> None:
        self.loop.active = False
        self.workflows.update(pending=False, probe_fails=False)


def _then(*setups):
    def _setup(ctx) -> None:
        for step in setups:
            step(ctx)

    return _setup


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["loop", "probe", "child"])
async def test_a_persons_message_after_work_that_ended_silently_is_measured_alone(how, clock):
    pending = _Pending(how)
    record = await run_turn(
        TurnScript(
            events=[Do(pending.arm), _REPLY, _DONE],
            then=TurnScript(
                message="thanks",
                setup=_then(lambda _ctx: pending.end_without_a_turn(), _an_hour_later(clock)),
                events=[_runs_for(clock, 2), _REPLY, _DONE],
            ),
        ),
        slot=SlotSpec(autonudge=True),
    )
    first = _done_frame(record)
    assert first["continuing"] is True and "run_seconds" not in first
    last = _done_frame(record.then)
    assert last["continuing"] is False
    assert last["run_seconds"] == 2


@pytest.mark.asyncio
async def test_a_persons_message_while_a_run_waits_on_children_restarts_the_clock(clock):
    pending = _Pending("child")
    record = await run_turn(
        TurnScript(
            events=[Do(pending.arm), _REPLY, _DONE],
            then=TurnScript(
                message="any news?",
                setup=_an_hour_later(clock),
                events=[_REPLY, _DONE],
                then=TurnScript(
                    message="the children reported",
                    user_row=False,
                    run_kwargs={"_turn_actor": "subagent"},
                    setup=_then(
                        lambda _ctx: pending.end_without_a_turn(), lambda _ctx: clock.advance(5)
                    ),
                    events=[_REPLY, _DONE],
                ),
            ),
        ),
    )
    assert "run_seconds" not in _done_frame(record.then)
    assert _done_frame(record.then.then)["run_seconds"] == 5


@pytest.mark.asyncio
async def test_a_turn_no_person_sent_continues_the_open_run(clock):
    pending = _Pending("child")
    record = await run_turn(
        TurnScript(
            events=[Do(pending.arm), _REPLY, _DONE],
            then=TurnScript(
                message="the children reported",
                user_row=False,
                run_kwargs={"_turn_actor": "subagent"},
                setup=_then(lambda _ctx: pending.end_without_a_turn(), _an_hour_later(clock)),
                events=[_REPLY, _DONE],
            ),
        ),
    )
    assert _done_frame(record.then)["run_seconds"] == _HOUR
