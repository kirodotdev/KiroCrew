"""The one queue-held gate: deriving the hold, and the idle-send gate that honours it."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_runner_coverage_helpers import _slot, _state
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import (
    chat_handlers,
    chat_runner,
    chat_utils_completion,
    responsive_chat,
)
from kiro_crew.dashboard.chat_utils import (
    effective_session_key,
    teardown_hold_active,
)
from kiro_crew.dashboard.chat_utils_completion import SYNTHESIS_CLEAR, chat_done_payload
from kiro_crew.dashboard.chat_utils_injected_rows import (
    CRON_NOTIFICATION_KIND,
    STEER_INTENT_META,
)
from kiro_crew.start_priority import StartPriority
from kiro_crew.testing.wait import async_wait_until, await_until

GAVE_UP = "Could not drop the previous conversation"


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    return state, state.get_or_create_slot("s1")


@pytest.fixture
def held(fresh, monkeypatch):
    """A slot with a discard armed and no sub-agents attached."""
    state, slot = fresh
    slot._pending_discard_conversation_key = "s1"
    monkeypatch.setattr(chat_runner, "subagents_attached_async", AsyncMock(return_value=False))
    return state, slot


@pytest.fixture
def landing(held):
    state, slot = held
    discard = AsyncMock(return_value=True)
    state.sessions.discard_conversation = discard
    return state, slot, discard


def _tick(monkeypatch, secs: float = 0.001, max_ticks: int | None = None) -> None:
    monkeypatch.setattr(chat_runner, "_PENDING_RESET_RETRY_DELAY_SECS", secs)
    if max_ticks is not None:
        monkeypatch.setattr(chat_runner, "_TEARDOWN_HOLD_MAX_TICKS", max_ticks)


def _children(*running: str) -> MagicMock:
    return MagicMock(
        running_agents_for=MagicMock(return_value=list(running)),
        queued_count_for_async=AsyncMock(return_value=0),
        queued_count_or_none_async=AsyncMock(return_value=0),
        has_in_memory_pending_work_for=MagicMock(return_value=False),
        published_queued_depths=MagicMock(return_value={}),
    )


def _record_runs(monkeypatch, owner=chat_runner) -> list[str]:
    ran: list[str] = []

    async def fake_run_chat(_st, _sl, msg, **_kwargs):
        ran.append(msg)

    monkeypatch.setattr(owner, "_run_chat", fake_run_chat)
    return ran


def _retry(state, slot, msg: str = "no retry is armed for the slot"):
    retry = state._pending_reset_retries.get(slot.key)
    assert retry is not None and retry[0] is slot, msg
    return retry[1]


def _drop_retry(state, slot) -> None:
    retry = state._pending_reset_retries.pop(slot.key, None)
    if retry is not None:
        retry[1].cancel()


def _cancel_turn(slot) -> None:
    if slot.task is not None:
        slot.task.cancel()


async def _land(retry, slot) -> None:
    await await_until(retry, what="the discard retry")
    if slot.task is not None:
        await await_until(slot.task, what="the drained turn")


def _gave_up(slot) -> bool:
    return any(GAVE_UP in row.get("content", "") for row in slot.messages)


async def _post(state, *messages: str, **extra) -> list[dict]:
    bodies = []
    async with TestClient(TestServer(_make_app(state))) as client:
        for message in messages:
            body = {"message": message, "slot": "s1", **extra}
            resp = await client.post("/api/chat?ws=1", json=body)
            assert resp.status == 200
            bodies.append(await resp.json())
    return bodies


async def _until(predicate, what: str, interval: float = 0.001) -> None:
    await async_wait_until(predicate, interval=interval, describe=lambda: what)


async def _ticks(read, n: int, what: str) -> None:
    # Barrier on a COUNT, not the clock: a negative after it passes only if n ticks really ran.
    await async_wait_until(
        lambda: read() >= n, interval=0.005, describe=lambda: f"{n} {what}, saw {read()}"
    )


class TestTeardownHoldActive:
    def test_a_pending_reset_does_not_hold(self, fresh):
        """Every turn consumes a deferred reset before acquiring a session; a hold only parks."""
        state, slot = fresh
        slot._pending_reset_history_key = "s1"
        assert teardown_hold_active(state, slot) is False
        assert teardown_hold_active(state, slot) is False

    def test_a_pending_discard_holds(self, fresh):
        state, slot = fresh
        slot._pending_discard_conversation_key = "s1"
        assert teardown_hold_active(state, slot) is True

    def test_clearing_a_cause_elsewhere_releases_the_hold_with_no_recompute(self, fresh):
        """A predicate, not a stored flag: causes are cleared at sites blind to the queue."""
        state, slot = fresh
        slot._pending_discard_conversation_key = "s1"
        assert teardown_hold_active(state, slot) is True
        slot._pending_discard_conversation_key = None
        assert teardown_hold_active(state, slot) is False

    def test_no_cause_does_not_hold(self, fresh):
        state, slot = fresh
        assert teardown_hold_active(state, slot) is False


@pytest.mark.asyncio
class TestSynthesisConsultsTheHold:
    """Synthesis drains the queue too, so a held queue withholds it or the held prompts fail."""

    @staticmethod
    def _setup(tmp_path):
        state, slot = _state(tmp_path), _slot()
        state._slots[slot.key] = slot
        state.subagents = _children()
        return state, slot

    @staticmethod
    async def _finish_with_a_send_in_the_done_read(state, slot) -> list[object]:
        arrivals = {"count": 0}
        started: list[object] = []

        async def done_payload_that_races(_st, sl, **_kwargs):
            # The send lands inside the awaited child-store read, the window the dispatch is for.
            arrivals["count"] += 1
            if arrivals["count"] == 1:
                sl.queue_append("sent during the done read")
            return {}

        async def record_start(_st, _sl, **kwargs):
            started.append(kwargs.get("required_queue_id"))
            return False

        with (
            patch.object(chat_runner, "chat_done_payload", new=done_payload_that_races),
            patch.object(chat_runner, "_start_next_queued_turn", new=record_start),
        ):
            await chat_runner._finish_queue_cycle(state, slot, drain=False)
        return started

    async def test_a_held_queue_withholds_the_synthesis_dispatch(self, tmp_path):
        state, slot = self._setup(tmp_path)
        slot._pending_synthesis = True
        slot.queue_append("held until the discard lands")
        slot._pending_discard_conversation_key = slot.key
        with patch.object(chat_runner, "_run_pending_synthesis", new=AsyncMock()):
            await chat_runner._finish_queue_cycle(state, slot)
            await asyncio.sleep(0)
        assert slot._synthesis_inflight is False
        assert slot._pending_synthesis is True
        _cancel_turn(slot)

    async def test_the_recheck_timer_does_not_fire_synthesis_while_the_queue_is_held(
        self, tmp_path, monkeypatch
    ):
        """The timer calls `_launch_synthesis` directly, bypassing the fire site's hold check."""
        monkeypatch.setattr(chat_runner, "_SYNTHESIS_RECHECK_SECS", 0.01)
        state, slot = self._setup(tmp_path)
        slot._pending_synthesis = True
        slot.queue_append("held behind the discard")
        slot._pending_discard_conversation_key = slot.key
        launched: list[str] = []
        with (
            patch.object(chat_runner, "_launch_synthesis", lambda _st, sl: launched.append(sl.key)),
            patch.object(
                chat_runner, "synthesis_fire_verdict", AsyncMock(return_value=SYNTHESIS_CLEAR)
            ),
        ):
            chat_runner._arm_synthesis_recheck(state, slot)
            assert slot._synthesis_recheck is not None
            # Barrier: `_recheck` clears this handle first, so None proves its body ran.
            await _until(
                lambda: slot._synthesis_recheck is None, "the recheck to fire", interval=0.005
            )
        assert launched == [], "the recheck fired synthesis while the queue was held"
        assert [entry.get("content") for entry in slot._queue] == ["held behind the discard"]
        _cancel_turn(slot)

    async def test_a_send_arriving_during_the_done_read_does_not_bypass_the_hold(self, tmp_path):
        """Dispatching the arrival by ID reads as explicit and turns the teardown hold off."""
        state, slot = self._setup(tmp_path)
        slot._pending_discard_conversation_key = slot.key
        started = await self._finish_with_a_send_in_the_done_read(state, slot)
        assert started == [], "the arrival was dispatched past the teardown hold"
        assert [entry.get("content") for entry in slot._queue] == ["sent during the done read"]
        _cancel_turn(slot)

    async def test_a_send_arriving_during_the_done_read_runs_when_nothing_is_held(self, tmp_path):
        """Positive control: the arrival still gets the floor with no hold on."""
        state, slot = self._setup(tmp_path)
        started = await self._finish_with_a_send_in_the_done_read(state, slot)
        assert started and started[0] is not None, "the arrival never got the floor"
        _cancel_turn(slot)

    async def test_an_ultra_mode_pause_withholds_the_synthesis_dispatch(
        self, tmp_path, monkeypatch
    ):
        """Pause all holds the session: its armed synthesis waits for Resume all."""
        from kiro_crew import crew_pause

        monkeypatch.setattr(crew_pause, "holds_slot", lambda sl: True)
        state, slot = self._setup(tmp_path)
        slot._pending_synthesis = True
        with patch.object(chat_runner, "_run_pending_synthesis", new=AsyncMock()):
            await chat_runner._finish_queue_cycle(state, slot)
            await asyncio.sleep(0)
        assert slot._synthesis_inflight is False
        assert slot._pending_synthesis is True
        _cancel_turn(slot)

    async def test_a_pause_after_the_verdict_still_holds_the_synthesis_turn(
        self, tmp_path, monkeypatch
    ):
        """The recheck timer reaches `_run_pending_synthesis` directly: it holds there too."""
        from kiro_crew import crew_pause

        monkeypatch.setattr(crew_pause, "holds_slot", lambda sl: True)
        state, slot = self._setup(tmp_path)
        slot._pending_synthesis = True
        runs = _record_runs(monkeypatch)
        await chat_runner._run_pending_synthesis(state, slot)
        assert runs == []
        assert slot._pending_synthesis is True
        _cancel_turn(slot)

    async def test_a_pause_during_the_synthesis_queue_drain_holds_the_synthesis(
        self, tmp_path, monkeypatch
    ):
        """The drain awaits before it refuses: a pause published then holds synthesis too."""
        from kiro_crew import crew_pause

        paused = {"on": False}
        monkeypatch.setattr(crew_pause, "holds_slot", lambda sl: paused["on"])

        async def drain_then_paused(_st, _sl, **_kwargs):
            paused["on"] = True
            return False

        state, slot = self._setup(tmp_path)
        slot._pending_synthesis = True
        slot.queue_append("waiting behind the pause")
        runs = _record_runs(monkeypatch)
        monkeypatch.setattr(chat_runner, "_start_next_queued_turn", drain_then_paused)
        await chat_runner._run_pending_synthesis(state, slot)
        assert runs == []
        assert slot._pending_synthesis is True
        _cancel_turn(slot)

    async def test_synthesis_still_dispatches_when_nothing_is_held(self, tmp_path):
        """Positive control for the gate above."""
        state, slot = self._setup(tmp_path)
        slot._pending_synthesis = True
        with patch.object(chat_runner, "_run_pending_synthesis", new=AsyncMock()):
            await chat_runner._finish_queue_cycle(state, slot)
            await asyncio.sleep(0)
        assert slot._synthesis_inflight is True
        _cancel_turn(slot)


@pytest.mark.asyncio
class TestIdleSendTeardownGate:
    async def test_an_idle_send_is_not_answered_by_a_conversation_awaiting_discard(
        self, fresh, monkeypatch
    ):
        """Fails on main: the send is answered from the conversation the discard will drop."""
        state, slot = fresh
        slot._pending_discard_conversation_key = "s1"
        state.sessions.discard_conversation = AsyncMock(return_value=False)
        ran = _record_runs(monkeypatch, chat_handlers)
        try:
            (body,) = await _post(state, "hold me")
        finally:
            _drop_retry(state, slot)
        assert ran == []
        assert body.get("queued") is True

    async def test_a_steer_send_is_not_answered_by_a_conversation_awaiting_discard(
        self, fresh, monkeypatch
    ):
        """Steer waives the SUB-AGENT wait only; attached children are why discards defer."""
        state, slot = fresh
        slot._pending_discard_conversation_key = "s1"
        state.sessions.discard_conversation = AsyncMock(return_value=False)
        ran = _record_runs(monkeypatch, chat_handlers)
        try:
            (body,) = await _post(state, "steer past the children", steer=True)
        finally:
            _drop_retry(state, slot)
        assert ran == [], "the steer send was answered by the conversation awaiting discard"
        assert body.get("queued") is True

    async def test_a_steer_send_still_skips_the_subagent_wait(self, fresh, monkeypatch):
        """Positive control: with no discard armed, the steer opt-out still dispatches."""
        state, _ = fresh
        state.subagents = _children("a1")
        ran = _record_runs(monkeypatch, chat_handlers)
        await _post(state, "steer past the children", steer=True)
        assert ran == ["steer past the children"], "the steer opt-out stopped working"

    async def test_an_idle_send_queues_behind_a_deferred_discard_until_the_retry_lands_it(
        self, held, monkeypatch
    ):
        """The send queues, and the retry it arms lands the discard and drains it."""
        state, slot = held
        _tick(monkeypatch, 0.01)
        released = asyncio.Event()

        async def discard_when_released(*_args, **_kwargs):
            await released.wait()
            return True

        state.sessions.discard_conversation = discard_when_released
        ran = _record_runs(monkeypatch)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat?ws=1", json={"message": "hold me", "slot": "s1"})
            assert resp.status == 200
            assert (await resp.json())["queued"] is True
            assert ran == []
            retry = _retry(state, slot)
            released.set()
            await _land(retry, slot)
        assert slot._pending_discard_conversation_key is None
        assert ran == ["hold me"]

    async def test_an_idle_send_wakes_an_armed_retry_instead_of_waiting_for_its_tick(
        self, landing, monkeypatch
    ):
        """With the tick an hour away, only the send's wake can land the discard."""
        state, slot, _ = landing
        _tick(monkeypatch, 3600.0)
        ran = _record_runs(monkeypatch)
        chat_runner._arm_pending_reset_retry(state, slot)
        retry = _retry(state, slot)
        try:
            async with TestClient(TestServer(_make_app(state))) as client:
                body = {"message": "hold me", "slot": "s1"}
                resp = await client.post("/api/chat?ws=1", json=body)
                assert resp.status == 200
                assert (await resp.json())["queued"] is True
                await _land(retry, slot)
        finally:
            retry.cancel()
        assert slot._pending_discard_conversation_key is None
        assert ran == ["hold me"]

    async def test_a_wake_discard_behind_a_pending_question_does_not_hold_the_answer(
        self, fresh, monkeypatch
    ):
        """Only the answer, run as a turn, clears the card the discard waits for."""
        state, slot = fresh
        slot._pending_discard_conversation_key = "s1"
        slot._pending_discard_from_wake = True
        slot._question_pending = {"q1": {"blocking": False}}
        assert teardown_hold_active(state, slot) is False
        ran = _record_runs(monkeypatch, chat_handlers)
        (body,) = await _post(state, "my answer")
        assert body.get("queued") is not True
        assert ran == ["my answer"]
        assert slot._pending_discard_conversation_key == "s1"

    async def test_a_persons_own_discard_still_holds_behind_a_pending_question(self, fresh):
        """Negative control for the wake exemption above."""
        state, slot = fresh
        slot._pending_discard_conversation_key = "s1"
        slot._pending_discard_from_wake = False
        slot._question_pending = {"q1": {"blocking": False}}
        assert teardown_hold_active(state, slot) is True

    @pytest.mark.parametrize("has_session", [False, True])
    async def test_an_idle_send_dispatches_with_only_a_reset_armed(
        self, fresh, monkeypatch, has_session
    ):
        """A deferred reset is the dispatched turn's own pre-acquire consume to land."""
        state, slot = fresh
        armed = effective_session_key(slot)
        slot._pending_reset_history_key = armed
        state.sessions.reset = AsyncMock(return_value=False)
        state.sessions.has_session = MagicMock(return_value=has_session)
        ran = _record_runs(monkeypatch, chat_handlers)
        first, second = await _post(state, "first", "second")
        assert first.get("queued") is not True
        assert second.get("queued") is not True
        assert ran == ["first", "second"]
        assert slot._pending_reset_history_key == armed

    async def test_a_signed_out_slot_does_not_hold_an_idle_send(self, fresh, monkeypatch):
        """Only a turn that runs clears the sign-out cause, so the send gate must not read it."""
        state, slot = fresh
        slot._last_turn_auth_required = True
        ran = _record_runs(monkeypatch, chat_handlers)
        (body,) = await _post(state, "after the sign-in failure")
        assert body.get("queued") is not True
        assert ran == ["after the sign-in failure"]


@pytest.mark.asyncio
class TestDiscardFailureArmsTheRetry:
    async def test_a_raising_discard_still_releases_the_queue(self, held, monkeypatch):
        """Every branch leaving the discard armed must arm the retry, or an idle queue stays held."""
        state, slot = held
        _tick(monkeypatch, 0.01)
        slot.queue_append("parked behind the failed discard")
        state.sessions.discard_conversation = AsyncMock(side_effect=[RuntimeError("boom"), True])
        ran = _record_runs(monkeypatch)
        assert await chat_runner._consume_pending_reset(state, slot, allow_discard=True) is False
        await _land(_retry(state, slot), slot)
        assert slot._pending_discard_conversation_key is None
        assert ran == ["parked behind the failed discard"]

    async def test_a_discard_that_always_raises_gives_up_the_hold_and_says_so(
        self, held, monkeypatch
    ):
        """A held queue answers and reports nothing, so a wedged provider gets a hold budget."""
        state, slot = held
        _tick(monkeypatch, 0.001, 3)
        slot.queue_append("parked behind the wedged discard")
        attempts: list[int] = []

        async def always_raises(*_args, **_kwargs):
            attempts.append(1)
            raise RuntimeError("provider wedged")

        state.sessions.discard_conversation = always_raises
        ran = _record_runs(monkeypatch)
        assert await chat_runner._consume_pending_reset(state, slot, allow_discard=True) is False
        await _land(_retry(state, slot), slot)
        assert len(attempts) > 1
        assert teardown_hold_active(state, slot) is False
        assert ran == ["parked behind the wedged discard"]
        assert any(
            row.get("role") == "system" and GAVE_UP in row.get("content", "")
            for row in slot.messages
        )

    async def test_a_drain_that_starts_nothing_still_reaches_the_recheck(
        self, landing, monkeypatch
    ):
        """A drain can start nothing on a non-empty queue; returning then strands the synthesis."""
        state, slot, _ = landing
        _tick(monkeypatch)
        slot._pending_synthesis = True
        slot.queue_append("dropped by the admission sweep")

        async def drain_that_drops_everything(_st, sl, **_kwargs):
            # What the real sweep does when every entry stopped qualifying.
            sl._queue.clear()
            return False

        monkeypatch.setattr(chat_runner, "_start_next_queued_turn", drain_that_drops_everything)
        chat_runner._arm_pending_reset_retry(state, slot)
        retry = _retry(state, slot)
        try:
            await _until(lambda: slot._pending_discard_conversation_key is None, "the discard")
            await _until(lambda: slot._synthesis_recheck is not None, "the synthesis re-armed")
        finally:
            if slot._synthesis_recheck is not None:
                slot._synthesis_recheck.cancel()
            retry.cancel()

    async def test_a_drain_that_starts_a_turn_does_not_also_arm_the_recheck(
        self, landing, monkeypatch
    ):
        """Positive control: an unconditional recheck would double-dispatch behind the turn."""
        state, slot, _ = landing
        _tick(monkeypatch)
        slot._pending_synthesis = True
        slot.queue_append("drained normally")
        started: list[bool] = []

        async def drain_that_starts_a_turn(_st, _sl, **_kwargs):
            started.append(True)
            return True

        monkeypatch.setattr(chat_runner, "_start_next_queued_turn", drain_that_starts_a_turn)
        chat_runner._arm_pending_reset_retry(state, slot)
        retry = _retry(state, slot)
        try:
            # Wait on the drain HAVING RUN, or "no recheck" passes before the branch is reached.
            await _until(lambda: bool(started), "the release to reach the drain")
            assert slot._synthesis_recheck is None, "the recheck fired behind a running turn"
        finally:
            retry.cancel()

    async def test_the_retry_reserves_admission_while_released_queue_drain_yields(
        self, landing, monkeypatch
    ):
        state, slot, _ = landing
        slot.queue_append("drained after discard")
        drain_entered = asyncio.Event()
        release_drain = asyncio.Event()

        async def yielding_drain(_st, _sl, **_kwargs):
            drain_entered.set()
            await release_drain.wait()
            return False

        monkeypatch.setattr(chat_runner, "_start_next_queued_turn", yielding_drain)
        chat_runner._arm_pending_reset_retry(state, slot)
        retry = _retry(state, slot)
        chat_runner._wake_pending_reset_retry(state, slot)
        try:
            await await_until(drain_entered, what="the released queue drain to yield")
            assert slot._turn_admission_reserved is True
        finally:
            release_drain.set()
            await await_until(retry, what="the discard retry")
        assert slot._turn_admission_reserved is False

    async def test_the_release_leaves_a_queue_held_for_sign_in_alone(self, landing, monkeypatch):
        """The drain has no auth check and a signed-out drain drops a person's prompt for good.

        Positive control: the test above, where the same retry drains with the flag clear.
        """
        state, slot, _ = landing
        _tick(monkeypatch)
        slot._last_turn_auth_required = True
        slot.queue_append("waiting for the user to sign in")
        ran = _record_runs(monkeypatch)
        chat_runner._arm_pending_reset_retry(state, slot)
        retry = _retry(state, slot)
        try:
            await _until(lambda: slot._pending_discard_conversation_key is None, "the discard")
            assert ran == [], "the release drained a queue held for sign-in"
            assert [e.get("content") for e in slot._queue] == ["waiting for the user to sign in"]
        finally:
            retry.cancel()

    async def test_attached_children_never_spend_the_hold_budget(self, landing, monkeypatch):
        """Landing the discard kills attached children, so waiting on them is deliberate."""
        state, slot, _ = landing
        _tick(monkeypatch, 0.001, 2)
        slot.queue_append("waiting on the children")
        attached = {"value": True}
        probes = {"count": 0}

        async def children_attached(*_args, **_kwargs):
            probes["count"] += 1
            return attached["value"]

        monkeypatch.setattr(chat_runner, "subagents_attached_async", children_attached)
        ran = _record_runs(monkeypatch)
        assert await chat_runner._consume_pending_reset(state, slot, allow_discard=True) is False
        retry = _retry(state, slot)
        await _ticks(lambda: probes["count"], 40, "structural-wait ticks")
        assert teardown_hold_active(state, slot) is True, "the budget was spent on a real wait"
        assert not _gave_up(slot)
        # The children detach: the same retry lands the discard and drains.
        attached["value"] = False
        await _land(retry, slot)
        assert slot._pending_discard_conversation_key is None
        assert ran == ["waiting on the children"]

    async def test_a_pending_card_never_spends_the_hold_budget(self, landing, monkeypatch):
        """Only the person's answer clears the card, and a person can take hours."""
        state, slot, _ = landing
        _tick(monkeypatch, 0.001, 2)
        slot._pending_discard_from_wake = True
        slot._question_pending = {"q1": {"blocking": False}}
        probes = {"count": 0}

        async def children_absent(*_args, **_kwargs):
            probes["count"] += 1
            return False

        monkeypatch.setattr(chat_runner, "subagents_attached_async", children_absent)
        assert await chat_runner._consume_pending_reset(state, slot, allow_discard=True) is False
        retry = _retry(state, slot)
        await _ticks(lambda: probes["count"], 40, "card-deferred ticks")
        assert slot._pending_discard_conversation_key == "s1"
        assert not _gave_up(slot)
        retry.cancel()

    async def test_a_long_project_reset_does_not_spend_a_later_discards_budget(
        self, fresh, monkeypatch
    ):
        """The budget belongs to one discard; reset-only ticks must not pre-spend it."""
        state, slot = fresh
        _tick(monkeypatch, 0.001, 3)
        # A reset whose key has no session never lands, so the retry task stays alive.
        slot._pending_reset_history_key = "s1"
        state.sessions.reset = AsyncMock(return_value=False)
        probes = {"count": 0}

        async def children_attached(*_args, **_kwargs):
            probes["count"] += 1
            return True

        monkeypatch.setattr(chat_runner, "subagents_attached_async", children_attached)
        ran = _record_runs(monkeypatch)
        # Queued from the start: only a reset is owed, so the give-up must never release it.
        slot.queue_append("not held by anything yet")
        chat_runner._arm_pending_reset_retry(state, slot)
        retry = _retry(state, slot)
        await _ticks(lambda: probes["count"], 30, "retry ticks")
        assert ran == [], "the budget was spent while only a reset was owed"
        # Only NOW does a discard arrive, onto a task that has ticked 30 times.
        slot._pending_discard_conversation_key = "s1"
        slot.queue_append("must not be released on the first tick")
        before = probes["count"]
        await _until(lambda: probes["count"] >= before + 4, "4 more ticks", interval=0.005)
        assert teardown_hold_active(state, slot) is True
        assert not _gave_up(slot)
        retry.cancel()

    async def test_a_busy_linked_session_does_not_spend_the_budget_on_an_empty_queue(
        self, held, monkeypatch
    ):
        """A hold withholding nothing frees nothing by giving up, and its row would lie."""
        state, slot = held
        _tick(monkeypatch, 0.001, 3)
        assert not slot._queue
        assert not slot._pending_synthesis
        attempts = {"count": 0}

        async def busy_elsewhere(*_args, **_kwargs):
            # skip_if_busy refusing for a channel turn that holds the linked session.
            attempts["count"] += 1
            return False

        state.sessions.discard_conversation = busy_elsewhere
        chat_runner._arm_pending_reset_retry(state, slot)
        retry = _retry(state, slot)
        await _ticks(lambda: attempts["count"], 30, "refused attempts")
        assert slot._pending_discard_conversation_key == "s1", "the reset was thrown away"
        assert teardown_hold_active(state, slot) is True
        assert not _gave_up(slot), "a give-up row was posted for a queue that held nothing"
        retry.cancel()

    async def test_a_held_synthesis_spends_the_budget_without_claiming_queued_messages(
        self, held, monkeypatch
    ):
        """Positive control: a held synthesis keeps the budget, and the row tells the truth."""
        state, slot = held
        _tick(monkeypatch, 0.001, 3)
        slot._pending_synthesis = True
        assert not slot._queue

        async def busy_elsewhere(*_args, **_kwargs):
            return False

        state.sessions.discard_conversation = busy_elsewhere
        monkeypatch.setattr(chat_runner, "_arm_synthesis_recheck", lambda *_a, **_k: None)
        chat_runner._arm_pending_reset_retry(state, slot)
        retry = _retry(state, slot)
        await _until(
            lambda: slot._pending_discard_conversation_key is None, "spent", interval=0.005
        )
        rows = [
            row.get("content", "") for row in slot.messages if GAVE_UP in row.get("content", "")
        ]
        assert len(rows) == 1, "the budget did not apply to a held synthesis"
        assert "queued messages" not in rows[0], rows[0]
        retry.cancel()

    async def test_the_queue_drains_when_the_discard_lands_and_a_reset_stays_armed(
        self, landing, monkeypatch
    ):
        """The release keys on the DISCARD clearing; an unlandable reset would strand the queue."""
        state, slot, _ = landing
        _tick(monkeypatch)
        slot._pending_reset_history_key = "s1"
        slot.queue_append("released when the discard lands")
        state.sessions.reset = AsyncMock(return_value=False)
        ran = _record_runs(monkeypatch)
        chat_runner._arm_pending_reset_retry(state, slot)
        retry = _retry(state, slot)
        await _until(lambda: ran == ["released when the discard lands"], "the queue to drain")
        assert slot._pending_discard_conversation_key is None
        # A consume may re-point a stale key at the live session, so assert armed, not equal.
        assert slot._pending_reset_history_key is not None, "the reset is still owed"
        if slot.task is not None:
            await await_until(slot.task, what="the drained turn")
        retry.cancel()

    async def test_the_retry_waits_while_a_turn_holds_admission(self, landing, monkeypatch):
        """`skip_if_busy` cannot see a reserved turn; discarding then loses the question it answers."""
        state, slot, discard = landing
        _tick(monkeypatch, 0.001, 2)
        slot._turn_admission_reserved = True
        assert slot.running is True, "the fixture must model a reserved turn"
        # The loop re-reads the live slot every tick, so counting that read proves ticks ran.
        ticks = {"count": 0}
        real_get_slot = state.get_slot

        def counting_get_slot(key):
            ticks["count"] += 1
            return real_get_slot(key)

        monkeypatch.setattr(state, "get_slot", counting_get_slot)
        chat_runner._arm_pending_reset_retry(state, slot)
        retry = _retry(state, slot)
        await _ticks(lambda: ticks["count"], 15, "ticks with admission reserved")
        assert discard.await_count == 0, "the retry tore down under a reserved turn"
        assert slot._pending_discard_conversation_key == "s1"
        assert not _gave_up(slot), "the budget was spent while a turn held admission"
        # Positive half: once admission is released the same retry lands it.
        slot._turn_admission_reserved = False
        await await_until(retry, what="the discard retry")
        assert slot._pending_discard_conversation_key is None

    async def test_the_consume_rechecks_admission_after_its_awaited_probes(
        self, landing, monkeypatch
    ):
        """A turn can reserve admission during the attached-children probe the consume awaits."""
        state, slot, discard = landing

        async def reserve_during_the_probe(*_args, **_kwargs):
            slot._turn_admission_reserved = True
            return False

        monkeypatch.setattr(chat_runner, "subagents_attached_async", reserve_during_the_probe)
        assert await chat_runner._consume_pending_reset(state, slot, allow_discard=True) is False
        assert discard.await_count == 0, "the consume tore down under a turn admitted mid-probe"
        assert slot._pending_discard_conversation_key == "s1"
        _retry(state, slot, "the deferral must arm the retry").cancel()

    async def test_the_admission_check_is_evaluated_under_the_registry_lock(self, held):
        """A turn can reserve while the discard waits for the lock, so the teardown asks inside it."""
        state, slot = held
        seen: list[bool] = []

        async def discard_that_waits_for_the_lock(_key, **kwargs):
            refuse_if = kwargs.get("refuse_if")
            assert refuse_if is not None, "the caller did not hand its check to the teardown"
            # The reservation lands while the real call waits on `owner._lock`.
            slot._turn_admission_reserved = True
            await asyncio.sleep(0)
            seen.append(refuse_if())
            return not seen[-1]

        state.sessions.discard_conversation = discard_that_waits_for_the_lock
        assert await chat_runner._consume_pending_reset(state, slot, allow_discard=True) is False
        assert seen == [True], "the check did not see the reservation that landed in the wait"
        assert slot._pending_discard_conversation_key == "s1"
        _retry(state, slot, "the refusal must arm the retry").cancel()

    async def test_that_check_still_answers_no_for_an_unreserved_slot(self, held):
        """Positive control: a predicate hard-wired to true would strand every discard."""
        state, slot = held
        seen: list[bool] = []

        async def discard_that_waits_for_the_lock(_key, **kwargs):
            refuse_if = kwargs["refuse_if"]
            await asyncio.sleep(0)
            seen.append(refuse_if())
            return not seen[-1]

        state.sessions.discard_conversation = discard_that_waits_for_the_lock
        assert await chat_runner._consume_pending_reset(state, slot, allow_discard=True) is True
        assert seen == [False]
        assert slot._pending_discard_conversation_key is None

    async def test_a_discard_that_lands_on_a_later_attempt_keeps_the_hold(self, held, monkeypatch):
        """Negative control: the budget must not fire on a discard still landing."""
        state, slot = held
        _tick(monkeypatch, 0.001, 3)
        slot.queue_append("parked once")
        state.sessions.discard_conversation = AsyncMock(
            side_effect=[RuntimeError("boom"), RuntimeError("boom"), True]
        )
        ran = _record_runs(monkeypatch)
        assert await chat_runner._consume_pending_reset(state, slot, allow_discard=True) is False
        await _land(_retry(state, slot), slot)
        assert ran == ["parked once"]
        assert not _gave_up(slot)


@pytest.mark.asyncio
class TestTheConsumeYieldsOnlyToAnotherTurn:
    """``slot.task`` is never a turn-path caller's own task, so the caller declares ``own_turn``."""

    async def test_the_turns_own_consume_lands_the_discard(self, landing):
        """``slot.task`` is live and is not this task -- production's shape."""
        state, slot, discard = landing
        outer = asyncio.ensure_future(asyncio.Event().wait())
        slot.task = outer
        assert slot.task is not asyncio.current_task()
        try:
            assert (
                await chat_runner._consume_pending_reset(
                    state, slot, allow_discard=True, own_turn=True
                )
                is True
            )
            assert discard.await_count == 1
            assert slot._pending_discard_conversation_key is None
            assert slot.key not in state._pending_reset_retries
        finally:
            outer.cancel()

    async def test_another_turns_consume_still_defers_the_discard(self, landing):
        """Negative control: same live ``slot.task``, ``own_turn=False`` defers -- the flag decides."""
        state, slot, discard = landing
        other = asyncio.ensure_future(asyncio.Event().wait())
        slot.task = other
        try:
            assert (
                await chat_runner._consume_pending_reset(state, slot, allow_discard=True) is False
            )
            assert discard.await_count == 0
            assert slot._pending_discard_conversation_key == "s1"
            _retry(state, slot, "the deferral must arm the retry").cancel()
        finally:
            other.cancel()

    async def test_a_reservation_defers_even_the_turns_own_consume(self, landing):
        """``chat_send`` clears its own reservation before the turn body, so one seen is another's."""
        state, slot, discard = landing
        slot._turn_admission_reserved = True
        assert (
            await chat_runner._consume_pending_reset(state, slot, allow_discard=True, own_turn=True)
            is False
        )
        assert discard.await_count == 0
        _retry(state, slot).cancel()

    async def test_the_real_end_of_turn_tail_lands_the_discard(self, landing, monkeypatch):
        """The CALL SITE: `_end_turn_tail` under a live `slot.task` that is not this task."""
        state, slot, discard = landing
        monkeypatch.setattr(chat_runner, "schedule_eager_spawn", lambda *_a, **_kw: None)
        outer = asyncio.ensure_future(asyncio.Event().wait())
        slot.task = outer
        turn_exit = chat_runner._TurnExit()
        turn_exit.outcome = chat_runner._TurnOutcome(
            auth_required=False,
            memory_admitted=True,
            start_priority=StartPriority.BACKGROUND,
            nudge_facts={"reply_text": ""},
            quiet_end=False,
        )
        try:
            await chat_runner._end_turn_tail(state, slot, turn_exit)
            assert discard.await_count == 1, "the tail deferred its own discard to the retry"
            assert slot._pending_discard_conversation_key is None
        finally:
            outer.cancel()
            if slot.task is not None and slot.task is not outer:
                slot.task.cancel()

    async def test_an_in_turn_clear_reports_discarded_not_queued(self, landing, monkeypatch):
        """Deferred, ``/clear`` answers "busy", records ``queued`` and leaves the plan pill up."""
        state, slot, discard = landing
        slot._pending_discard_conversation_key = None
        monkeypatch.setattr(chat_runner, "schedule_eager_spawn", lambda *_a, **_kw: None)
        outer = asyncio.ensure_future(asyncio.Event().wait())
        slot.task = outer
        try:
            outcome = await chat_runner._answer_slash_without_channel(
                state, slot, effective_session_key(slot), "/clear"
            )
            assert outcome == "discarded", "the in-turn /clear was deferred to the retry"
            assert discard.await_count == 1
        finally:
            outer.cancel()

    async def test_the_retry_respawns_the_session_it_dropped(self, landing, monkeypatch):
        """Without a respawn the next message pays the cold start the eager path hides."""
        state, slot, _ = landing
        _tick(monkeypatch)
        spawned: list[str] = []
        # The owner's functions carry the RUNNER's globals, so the bare call resolves there.
        monkeypatch.setattr(
            chat_runner, "schedule_eager_spawn", lambda _st, sl, **_kw: spawned.append(sl.key)
        )
        chat_runner._arm_pending_reset_retry(state, slot)
        _retry(state, slot)
        await _until(lambda: spawned == ["s1"], "the retry to respawn after landing the discard")
        assert slot._pending_discard_conversation_key is None


@pytest.mark.asyncio
class TestDrainChokePoint:
    @staticmethod
    async def _drain(state, slot, monkeypatch) -> list[str]:
        ran = _record_runs(monkeypatch)
        await chat_runner._start_next_queued_turn(state, slot)
        if slot.task is not None:
            await slot.task
        return ran

    async def test_a_queued_entry_drains_while_an_unlandable_reset_is_armed(
        self, fresh, monkeypatch
    ):
        """A reset whose key has no session declines forever; the queue behind it still drains."""
        state, slot = fresh
        slot._pending_reset_history_key = "gone"
        state.sessions.has_session = MagicMock(return_value=False)
        slot.queue_append("queued behind the reset")
        assert teardown_hold_active(state, slot) is False
        assert await self._drain(state, slot, monkeypatch) == ["queued behind the reset"]

    async def test_a_signed_out_slot_still_drains_the_send_that_resumes_it(
        self, fresh, monkeypatch
    ):
        """Only a turn that runs clears the sign-out cause, so the choke point must not hold on it."""
        state, slot = fresh
        slot._last_turn_auth_required = True
        slot.queue_append("after signing in")
        assert await self._drain(state, slot, monkeypatch) == ["after signing in"]

    async def test_a_system_entry_still_passes_a_held_queue_under_auto_priority(
        self, fresh, monkeypatch
    ):
        """Auto-promotion sets `required_queue_id` like Run now, which must not park a cron entry."""
        state, slot = fresh
        slot._pending_discard_conversation_key = "s1"
        slot.queue_append("the person's waiting send")
        slot.queue_append("[Cron notification] nightly digest", CRON_NOTIFICATION_KIND)
        monkeypatch.setattr(responsive_chat, "first_priority_id", lambda sl: sl._queue[0].get("id"))
        ran = await self._drain(state, slot, monkeypatch)
        assert ran == ["[Cron notification] nightly digest"], "the system entry was parked"
        assert [entry.get("content") for entry in slot._queue] == ["the person's waiting send"]

    async def test_a_queued_steer_does_not_drain_into_a_conversation_awaiting_discard(
        self, fresh, monkeypatch
    ):
        """The sub-agent hold's selector releases steers on purpose; the teardown hold must not."""
        state, slot = fresh
        slot._pending_discard_conversation_key = "s1"
        slot.queue_append("steer that fell back to the queue", "", {STEER_INTENT_META: True})
        assert await self._drain(state, slot, monkeypatch) == []
        assert [e.get("content") for e in slot._queue] == ["steer that fell back to the queue"]

    async def test_a_queued_steer_still_drains_past_the_subagent_hold(self, fresh, monkeypatch):
        """Positive control: with no discard armed, the steer exemption still releases the entry."""
        state, slot = fresh
        state.subagents = _children("a1")
        slot.queue_append("steer that fell back to the queue", "", {STEER_INTENT_META: True})
        assert await self._drain(state, slot, monkeypatch) == ["steer that fell back to the queue"]

    async def test_a_deferred_discard_holds_the_queued_user_entry(self, fresh, monkeypatch):
        """Negative control for the two drains above."""
        state, slot = fresh
        slot._pending_discard_conversation_key = "s1"
        slot.queue_append("held until the discard lands")
        assert await self._drain(state, slot, monkeypatch) == []
        assert len(slot._queue) == 1


@pytest.mark.asyncio
class TestTheCompletionHintCountsOnlyADrainableQueue:
    """``continuing`` counts a queue only when something will dequeue it."""

    @staticmethod
    def _queued(fresh, monkeypatch):
        monkeypatch.setattr(
            chat_utils_completion, "subagents_attached_async", AsyncMock(return_value=False)
        )
        state, slot = fresh
        slot.queue_append("queued behind the hold")
        return state, slot

    async def test_a_drainable_queue_reads_as_work_continuing(self, fresh, monkeypatch):
        """Positive control: the cases below would pass for a hint that never reports work."""
        state, slot = self._queued(fresh, monkeypatch)
        assert (await chat_done_payload(state, slot))["continuing"] is True

    async def test_a_signed_out_slot_does_not_announce_work_continuing(self, fresh, monkeypatch):
        state, slot = self._queued(fresh, monkeypatch)
        slot._last_turn_auth_required = True
        assert (await chat_done_payload(state, slot))["continuing"] is False

    async def test_a_held_teardown_does_not_announce_work_continuing(self, fresh, monkeypatch):
        state, slot = self._queued(fresh, monkeypatch)
        slot._pending_discard_conversation_key = "s1"
        assert (await chat_done_payload(state, slot))["continuing"] is False
