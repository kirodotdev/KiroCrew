"""A Stop issued on ANY surface suppresses the end-of-turn continuations.

A channel-born dashboard slot runs its turns on the channel's session
(``effective_session_key`` returns ``linked_session_key``). A stop issued from
that channel -- Slack's ``/kirocrew stop`` -- reaches ``SessionManager.stop_turn``
and the provider cancel, but never the slot's own ``_stop_state``. The two
end-of-turn continuation gates read the slot's Stop signal, so they must ALSO
read a session-scoped one that ``stop_turn`` records, or a human's stop is
followed by one more turn the human did not ask for.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.agent_sdk import TURN_STOP_REASON_CANCELLED
from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard.chat import _run_chat
from kiro_crew.dashboard.state import REFUSAL_RECOVERY_PREFIX, DashboardState, _ChatSlot
from kiro_crew.history import ConversationLog
from kiro_crew.hooks import ToolHookResult
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    LLMEvent,
)
from kiro_crew.session import SessionManager

LINKED_KEY = "slack:1700000000.000100"


# ── SessionManager: the session-scoped Stop record ──


@pytest.fixture
def cfg(tmp_path):
    cfg = KiroCrewConfig()
    cfg.agent.soft_stop_budget_secs = 0.01
    return cfg


async def _empty_stream(_command: str):
    if False:  # pragma: no cover - establishes the async-generator protocol
        yield None


def _provider_factory():
    def _factory(session_key=None, agent=None, channel_id=None, **kwargs):
        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.cancel = AsyncMock(return_value="acked")
        provider.is_process_alive = lambda: True
        provider.context_usage_pct = lambda: 0.0
        provider.context_window_tokens = lambda: 0
        provider.has_active_turn = lambda: False
        provider.runtime_info = lambda: (None, None)
        provider.stream_command = MagicMock(side_effect=_empty_stream)
        return provider

    return _factory


async def _until_parked(mgr: SessionManager, key: str, *, deadline: float = 5.0) -> None:
    """Yield until some task is blocked on *key*'s held lease.

    ``asyncio.Semaphore`` keeps its blocked acquirers in ``_waiters`` (``None``
    or an empty deque while nobody waits); polling it is the one observable
    that says "the claimant has reached the semaphore", which is what the
    replay-gap scenarios need to be true BEFORE the reset lands. A wall-clock
    sleep cannot promise that on a slow runner. Bounded so a claimant that
    never arrives fails the test loudly instead of hanging it.
    """
    loop = asyncio.get_running_loop()
    stop = loop.time() + deadline
    while loop.time() < stop:
        session = mgr._sessions.get(mgr._fold_key(key))
        waiters = getattr(getattr(session, "semaphore", None), "_waiters", None)
        if waiters:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"no claimant parked on {key!r} within {deadline}s")


class TestStopTurnRecordsASessionScopedStop:
    @pytest.mark.asyncio
    async def test_stop_turn_bumps_the_count_before_the_provider_cancel_is_awaited(self, cfg):
        """The runner's gates may run as soon as the cancel lands, so the record
        must exist BEFORE the cancel is awaited, not after its ack."""
        mgr = SessionManager(cfg, provider_factory=_provider_factory())
        provider, _, _ = await mgr.get_or_create(LINKED_KEY)
        mgr.release(LINKED_KEY)
        seen_during_cancel: list[int] = []

        async def _cancel(**_kw):
            seen_during_cancel.append(mgr.stop_generation(LINKED_KEY))
            return "acked"

        provider.cancel = AsyncMock(side_effect=_cancel)
        assert mgr.stop_generation(LINKED_KEY) == 0

        outcome = await mgr.stop_turn(LINKED_KEY)

        assert outcome == "soft"
        assert seen_during_cancel == [1]
        assert mgr.stop_generation(LINKED_KEY) == 1
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_the_count_survives_a_hard_stop_that_resets_the_session(self, cfg):
        """A flag on the session object would vanish with the very turn it
        stopped; the record is keyed by session key and never rewound."""
        mgr = SessionManager(cfg, provider_factory=_provider_factory())
        provider, _, _ = await mgr.get_or_create(LINKED_KEY)
        mgr.release(LINKED_KEY)
        provider.cancel = AsyncMock(return_value="timeout")

        outcome = await mgr.stop_turn(LINKED_KEY)

        assert outcome == "hard"
        assert not mgr.has_session(LINKED_KEY)
        assert mgr.stop_generation(LINKED_KEY) == 1
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_the_count_is_dropped_when_the_key_is_removed_or_destroyed(self, cfg):
        """The per-key record is popped beside the sibling per-key dicts on the
        teardown paths that end a conversation for good, so a long-lived
        gateway does not keep one entry per channel thread it ever stopped."""
        mgr = SessionManager(cfg, provider_factory=_provider_factory())
        for key, teardown in ((LINKED_KEY, mgr.remove), ("dashboard:chat-2", mgr.destroy)):
            await mgr.get_or_create(key)
            mgr.release(key)
            await mgr.stop_turn(key)
            assert mgr.stop_generation(key) == 1

            await teardown(key)

            assert mgr.stop_generation(key) == 0, teardown.__name__
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_stop_with_no_session_records_nothing(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_provider_factory())

        assert await mgr.stop_turn(LINKED_KEY) == "idle"

        assert mgr.stop_generation(LINKED_KEY) == 0
        assert mgr.note_stop(LINKED_KEY) is False
        assert mgr.stop_generation(LINKED_KEY) == 0
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_stop_inside_a_replay_gap_is_recorded_without_a_session(self, cfg):
        """The channel pipelines reset the session and replay the same message
        on a successor after a transient compaction failure. A Stop landing in
        that window finds no session; the gap the turn opens around it is what
        keeps the record, so the replay can see the Stop and stay dropped. The
        gap is keyed canonically: a Slack Stop issued under the bare thread ts
        and the turn reading under ``slack:<ts>`` meet in one bucket."""
        mgr = SessionManager(cfg, provider_factory=_provider_factory())
        await mgr.get_or_create(LINKED_KEY)
        mgr.release(LINKED_KEY)
        before = mgr.stop_generation(LINKED_KEY)

        mgr.open_replay_gap(LINKED_KEY)
        await mgr.reset(LINKED_KEY)
        assert not mgr.has_session(LINKED_KEY)

        bare = LINKED_KEY.split(":", 1)[1]
        assert await mgr.stop_turn(bare) == "idle"
        assert mgr.note_stop(LINKED_KEY) is True
        assert mgr.stop_generation(LINKED_KEY) == before + 2
        assert mgr.stop_generation(bare) == before + 2

        # The successor reads the same bucket, so a turn that snapshotted
        # ``before`` at entry sees the Stops after it reacquires.
        await mgr.get_or_create(LINKED_KEY)
        mgr.release(LINKED_KEY)
        mgr.close_replay_gap(LINKED_KEY)
        assert mgr.stop_generation(LINKED_KEY) == before + 2
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_another_task_claims_the_successor_only_after_the_replay(self, cfg):
        """A newer message for the same key arriving inside the gap must not
        claim the successor first, or it runs -- and persists -- ahead of the
        older message's replay. Its ``get_or_create`` waits until the gap
        closes; the replay's own task passes straight through, because its
        claim is what closes the gap."""
        mgr = SessionManager(cfg, provider_factory=_provider_factory())
        await mgr.get_or_create(LINKED_KEY)
        mgr.release(LINKED_KEY)

        mgr.open_replay_gap(LINKED_KEY)
        await mgr.reset(LINKED_KEY)

        order: list[str] = []

        async def newer_message() -> None:
            await mgr.get_or_create(LINKED_KEY)
            order.append("newer")
            mgr.release(LINKED_KEY)

        newer = asyncio.create_task(newer_message())
        await asyncio.sleep(0.05)
        assert order == [], "the newer message must wait behind the open gap"
        assert not newer.done()

        # The replay (this task owns the gap) claims without waiting ...
        await mgr.get_or_create(LINKED_KEY)
        order.append("replay")
        mgr.close_replay_gap(LINKED_KEY)
        # ... and still holds the successor's turn permit, so the newer message
        # keeps waiting on the semaphore until the replay releases it.
        await asyncio.sleep(0.05)
        assert order == ["replay"]
        mgr.release(LINKED_KEY)
        await asyncio.wait_for(newer, timeout=2)

        assert order == ["replay", "newer"]
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_reset_wakes_a_claimant_waiting_on_the_popped_permit(self, cfg):
        """A message that found the key busy waits on that session's turn permit.
        Popping the session (reset) must not leave it there for good: the permit
        stays held by the turn being torn down, whose own release lands on the
        successor. The reset wakes the waiter, which re-enters the claim and
        gets a session -- here a fresh one, since nothing replaced the old."""
        mgr = SessionManager(cfg, provider_factory=_provider_factory())
        await mgr.get_or_create(LINKED_KEY)  # held: the running turn

        async def waiting_message() -> str:
            await mgr.get_or_create(LINKED_KEY)
            mgr.release(LINKED_KEY)
            return "claimed"

        waiter = asyncio.create_task(waiting_message())
        await asyncio.sleep(0.05)
        assert not waiter.done(), "parked on the busy permit"

        await mgr.reset(LINKED_KEY)

        assert await asyncio.wait_for(waiter, timeout=5) == "claimed"
        assert mgr.has_session(LINKED_KEY)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_the_torn_down_turns_late_release_cannot_unlock_the_successor(self, cfg):
        """``release`` is key-only. Once a woken waiter has put a successor
        under the key, the torn-down turn's own late release would land on that
        successor and unlock a turn still in flight -- two turns on one session.
        The reset records who held the popped permit, and that task's release
        is absorbed; the successor stays busy until ITS holder releases."""
        mgr = SessionManager(cfg, provider_factory=_provider_factory())
        await mgr.get_or_create(LINKED_KEY)  # this task: the turn about to be torn down
        successor_held = asyncio.Event()
        let_go = asyncio.Event()

        async def waiting_message() -> None:
            await mgr.get_or_create(LINKED_KEY)
            successor_held.set()
            await let_go.wait()
            mgr.release(LINKED_KEY)

        waiter = asyncio.create_task(waiting_message())
        await asyncio.sleep(0.05)

        await mgr.reset(LINKED_KEY)
        await asyncio.wait_for(successor_held.wait(), timeout=5)
        assert mgr.is_busy(LINKED_KEY), "the woken waiter holds the successor"

        mgr.release(LINKED_KEY)  # the torn-down turn's own finally
        assert mgr.is_busy(LINKED_KEY), "absorbed: the successor's permit is not ours to give back"

        let_go.set()
        await asyncio.wait_for(waiter, timeout=5)
        assert not mgr.is_busy(LINKED_KEY), "the holder's own release is the one that counts"
        # Absorbed exactly once: a later release from this task is an ordinary one.
        await mgr.get_or_create(LINKED_KEY)
        mgr.release(LINKED_KEY)
        assert not mgr.is_busy(LINKED_KEY)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_woken_claimant_lands_behind_the_successor(self, cfg):
        """When the resetting turn reacquires first (a replay), the woken
        waiter meets the successor at the front door and queues behind its
        held permit, so it runs after the replay -- never on a permit a later
        reset could pop from under it."""
        mgr = SessionManager(cfg, provider_factory=_provider_factory())
        await mgr.get_or_create(LINKED_KEY)
        order: list[str] = []

        async def waiting_message() -> None:
            await mgr.get_or_create(LINKED_KEY)
            order.append("newer")
            mgr.release(LINKED_KEY)

        waiter = asyncio.create_task(waiting_message())
        # The scenario is "a claimant ALREADY parked on the held lease when the
        # reset lands". A wall-clock sleep only approximates that: on a loaded
        # Windows runner the task may not have reached the semaphore in 50 ms,
        # and a claimant that has not parked yet is a different scenario with a
        # different (and legitimate) outcome. Wait for the park itself.
        await _until_parked(mgr, LINKED_KEY)

        mgr.open_replay_gap(LINKED_KEY)
        await mgr.reset(LINKED_KEY)
        await mgr.get_or_create(LINKED_KEY)  # the replay's successor, held
        order.append("replay")
        # Give the woken waiter every chance to run: it must be blocked on the
        # open gap, not merely slow. A few loop iterations plus a short sleep
        # is an absence check, so it stays generous rather than tight.
        for _ in range(20):
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        assert order == ["replay"], "the woken waiter waits behind the open gap"
        mgr.release(LINKED_KEY)
        mgr.close_replay_gap(LINKED_KEY)

        await asyncio.wait_for(waiter, timeout=5)
        assert order == ["replay", "newer"]
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_only_the_owning_task_can_close_a_replay_gap(self, cfg):
        """The pipelines close the gap from a ``finally`` that runs on every
        turn for the key, so a concurrent turn on the same key must not be able
        to end the owner's gap early -- that would let a newer message claim the
        successor ahead of the replay. Teardown keeps its forced discard."""
        mgr = SessionManager(cfg, provider_factory=_provider_factory())
        mgr.open_replay_gap(LINKED_KEY)

        async def other_turn_finally() -> None:
            mgr.close_replay_gap(LINKED_KEY)

        await asyncio.create_task(other_turn_finally())

        async def newer_message() -> None:
            await mgr.await_replay_gap(LINKED_KEY)

        newer = asyncio.create_task(newer_message())
        await asyncio.sleep(0.02)
        assert not newer.done(), "a stranger's close must leave the owner's gap open"

        mgr.close_replay_gap(LINKED_KEY)  # the owner
        await asyncio.wait_for(newer, timeout=2)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_drain_releases_anyone_waiting_behind_a_replay_gap(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_provider_factory())
        mgr.open_replay_gap(LINKED_KEY)

        async def newer_message() -> None:
            await mgr.await_replay_gap(LINKED_KEY)

        newer = asyncio.create_task(newer_message())
        await asyncio.sleep(0.01)
        assert not newer.done()

        await mgr.close_all()
        await asyncio.wait_for(newer, timeout=2)

    @pytest.mark.asyncio
    async def test_a_closed_replay_gap_records_nothing_again(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_provider_factory())
        mgr.open_replay_gap(LINKED_KEY)
        mgr.close_replay_gap(LINKED_KEY)
        mgr.close_replay_gap(LINKED_KEY)  # idempotent

        assert await mgr.stop_turn(LINKED_KEY) == "idle"

        assert mgr.stop_generation(LINKED_KEY) == 0
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_note_stop_records_against_a_live_session(self, cfg):
        """The direct-cancel channel stop paths record through ``note_stop``
        rather than ``stop_turn``; a live session counts either way."""
        mgr = SessionManager(cfg, provider_factory=_provider_factory())
        await mgr.get_or_create(LINKED_KEY)
        mgr.release(LINKED_KEY)

        assert mgr.note_stop(LINKED_KEY) is True

        assert mgr.stop_generation(LINKED_KEY) == 1
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cancel_current_is_not_a_user_stop(self, cfg):
        """``cancel_current`` is the host's own best-effort abort (queue drain,
        injection retry, run teardown), not a person pressing Stop; it must not
        suppress a continuation the way a Stop does."""
        mgr = SessionManager(cfg, provider_factory=_provider_factory())
        await mgr.get_or_create(LINKED_KEY)
        mgr.release(LINKED_KEY)

        await mgr.cancel_current(LINKED_KEY)

        assert mgr.stop_generation(LINKED_KEY) == 0
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_the_count_is_per_session_key(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_provider_factory())
        await mgr.get_or_create(LINKED_KEY)
        mgr.release(LINKED_KEY)
        await mgr.get_or_create("dashboard:chat-2")
        mgr.release("dashboard:chat-2")

        await mgr.stop_turn(LINKED_KEY)

        assert mgr.stop_generation(LINKED_KEY) == 1
        assert mgr.stop_generation("dashboard:chat-2") == 0
        await mgr.close_all()


# ── chat runner: the refusal-recovery gate on a channel-born slot ──


async def _events(items):
    for item in items:
        yield item


def _make_state(tmp_path, *, deny_reason: str, stop_counts: dict[str, int]):
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    client = AsyncMock()
    sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    sessions.record_failure = AsyncMock()
    sessions.check_context_usage = MagicMock()
    # The session manager's Stop record, as the runner reads it.
    sessions.stop_generation = MagicMock(side_effect=lambda key: stop_counts.get(key, 0))
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    cb = MagicMock()
    cb.hooks.on_tool_call.return_value = ToolHookResult.deny(deny_reason)
    cb.build_message.return_value = ("hello", None)
    state.context_builder = cb
    hook_store = MagicMock()
    hook_store.fire = AsyncMock(return_value=[])
    state._hook_store = hook_store
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    client.context_usage_pct = MagicMock(return_value=0.0)
    client._client = client
    client.last_prompt_stats = None
    return state, client


def _channel_born_slot() -> _ChatSlot:
    slot = _ChatSlot("chat-1-test")
    slot.linked_session_key = LINKED_KEY
    return slot


async def _run_denied_turn(state, client, slot, *, channel_stop: bool, stop_counts) -> int:
    """One turn: the only tool call is policy-denied, then the turn ends
    ``cancelled``. With ``channel_stop`` the session-scoped Stop record for the
    slot's LINKED key moves before the turn ends, exactly as ``stop_turn`` on
    the channel side does; the slot's own Stop state is never touched. Returns
    how many times the model was streamed to."""
    calls = {"n": 0}

    def _stream(*_a, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:

            async def _first():
                yield LLMEvent(
                    kind=EVENT_PERMISSION_REQUEST,
                    title="fs_write",
                    tool_kind="edit",
                    request_id="req-1",
                )
                if channel_stop:
                    stop_counts[LINKED_KEY] = stop_counts.get(LINKED_KEY, 0) + 1
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=TURN_STOP_REASON_CANCELLED)

            return _first()
        return _events([LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")])

    client.stream = MagicMock(side_effect=_stream)
    with patch("kiro_crew.dashboard.chat.sel") as mock_sel:
        mock_sel.return_value = MagicMock()
        await _run_chat(state, slot, "hello")
        if slot.task:
            await slot.task
    return calls["n"]


def _recovery_injects(slot: _ChatSlot) -> list[str]:
    return [
        m["content"]
        for m in slot.messages
        if m.get("role") == "inject" and m.get("content", "").startswith(REFUSAL_RECOVERY_PREFIX)
    ]


class TestChannelStopPurgesAQueuedContinuationAtDrain:
    """The dispatch-point purge compares the stop counters against their values
    AT ENQUEUE. A stop issued on the linked channel while the promise-only
    continuation waited in the queue moves only the session-scoped count, so
    the purge must read that one too or the announced action dispatches."""

    @staticmethod
    def _queued_continuation(tmp_path, stop_counts):
        from kiro_crew.dashboard.chat_utils import (
            _PROMISE_ONLY_CONTINUE_MSG,
            SYNTHETIC_RECOVERY_KIND,
            RecoveryPayload,
        )
        from kiro_crew.dashboard.session_control import containment_meta

        state, _client = _make_state(
            tmp_path, deny_reason="Blocked by security policy: git push", stop_counts=stop_counts
        )
        slot = state.get_or_create_slot("chat-1-test")
        slot.linked_session_key = LINKED_KEY
        slot.queue_insert(
            0,
            _PROMISE_ONLY_CONTINUE_MSG,
            kind=SYNTHETIC_RECOVERY_KIND,
            payload=RecoveryPayload.CONTINUATION,
            # The admission stamp `_queue_recovery` records, so the drain's
            # containment sweep keeps the entry.
            meta=containment_meta(state, slot),
        )
        # Snapshots taken at enqueue: no stop on either counter yet.
        slot._promise_only_stop_gen = slot._stop_generation
        slot._promise_only_session_stop_gen = 0
        return state, slot

    @pytest.mark.asyncio
    async def test_a_stop_on_the_linked_session_while_queued_purges_it(self, tmp_path):
        from kiro_crew.dashboard.chat_runner import _start_next_queued_turn

        stop_counts = {LINKED_KEY: 0}
        state, slot = self._queued_continuation(tmp_path, stop_counts)
        stop_counts[LINKED_KEY] = 1  # /kirocrew stop landed while it waited

        with patch("kiro_crew.dashboard.chat_runner._run_chat", new_callable=AsyncMock):
            started = await _start_next_queued_turn(state, slot)

        assert started is False
        assert slot._queue == []
        assert slot._stop_generation == 0, "the slot's own Stop state was never touched"
        assert any(
            m.get("role") == "notice" and "the turn was stopped" in m.get("content", "")
            for m in slot.messages
        )

    @pytest.mark.asyncio
    async def test_no_stop_lets_the_queued_continuation_dispatch(self, tmp_path):
        from kiro_crew.dashboard.chat_runner import _start_next_queued_turn

        stop_counts = {LINKED_KEY: 0}
        state, slot = self._queued_continuation(tmp_path, stop_counts)

        with patch("kiro_crew.dashboard.chat_runner._run_chat", new_callable=AsyncMock) as run_chat:
            started = await _start_next_queued_turn(state, slot)
            if slot.task:
                await slot.task

        assert started is True
        assert slot._queue == []
        run_chat.assert_awaited_once()


class TestChannelStopSuppressesRefusalRecovery:
    @pytest.mark.asyncio
    async def test_a_stop_on_the_linked_session_queues_no_recovery(self, tmp_path):
        stop_counts: dict[str, int] = {}
        state, client = _make_state(
            tmp_path, deny_reason="Blocked by security policy: git push", stop_counts=stop_counts
        )
        slot = _channel_born_slot()

        streams = await _run_denied_turn(
            state, client, slot, channel_stop=True, stop_counts=stop_counts
        )

        assert slot._stop_generation == 0, "the slot's own Stop state was never touched"
        assert not slot._stopping
        assert _recovery_injects(slot) == []
        assert slot._queue == []
        assert streams == 1, "a human stopped the turn; no continuation may run"
        client.reject_tool.assert_called()

    @pytest.mark.asyncio
    async def test_the_record_is_read_for_the_linked_key_not_the_slot_key(self, tmp_path):
        stop_counts: dict[str, int] = {}
        state, client = _make_state(
            tmp_path, deny_reason="Blocked by security policy: git push", stop_counts=stop_counts
        )
        slot = _channel_born_slot()

        await _run_denied_turn(state, client, slot, channel_stop=True, stop_counts=stop_counts)

        keys = {c.args[0] for c in state.sessions.stop_generation.call_args_list}
        assert keys == {LINKED_KEY}

    @pytest.mark.asyncio
    async def test_a_backend_abort_with_no_stop_still_queues_recovery(self, tmp_path):
        """Same turn, same ``cancelled`` wire reason, nobody stopped it: codex
        aborts a policy-denied turn this way, and the continuation is owed."""
        stop_counts: dict[str, int] = {}
        state, client = _make_state(
            tmp_path, deny_reason="Blocked by security policy: git push", stop_counts=stop_counts
        )
        slot = _channel_born_slot()

        streams = await _run_denied_turn(
            state, client, slot, channel_stop=False, stop_counts=stop_counts
        )

        recovery = _recovery_injects(slot)
        assert recovery, "the block reason must still reach the model"
        assert "security policy: git push" in recovery[-1].lower()
        assert streams >= 2
