"""``_ChatSlot._active_turn_session_key`` — the running turn's own identity.

``linked_session_key`` says where the slot routes a NEW turn, and it is mutable
on a live slot: ``inject_cron_result_to_dashboard`` binds an already-running
slot to ``cron:<id>`` with no ``running`` gate. ``_run_chat`` captures its
session key once, at the boundary below every local-command return, and uses
that one key to acquire, audit and release for the whole turn.

These tests pin the field's LIFECYCLE against the real ``_run_chat``; the
cancel routes that consume it are covered in
``test_stop_addresses_linked_session.py``. The two things that can go wrong are
a key that outlives its turn (a later cancel aims at a session that is gone) and
a key retired by the wrong turn (a cancel falls back to mutable routing while a
successor is running).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.acp.client import AcpAuthRequired
from kiro_crew.dashboard.chat_runner import _run_chat

LINKED_KEY = "slack:1730000000.123456"


def _state_and_slot(tmp_path: Path, name: str = "turn-id-slot"):
    """The harness ``test_turn_teardown_release`` uses, so the turn is real."""
    state = _make_state(tmp_path)
    state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, False))
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.sessions.record_failure = AsyncMock()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.is_yolo_active = MagicMock(return_value=False)
    state._background_tasks = set()
    slot = state.get_or_create_slot(name)
    slot.append("user", "hello", "msg msg-u")
    client = state.sessions.get_or_create.return_value[0]
    client.shutdown = AsyncMock()
    return state, slot, client


def _stream_empty(client: MagicMock) -> None:
    async def _empty(msg):
        return
        yield  # pragma: no cover - generator shape only

    client.stream = _empty
    client.stream_command = _empty


def _stream_completes(client: MagicMock, on_stream=None) -> None:
    """A stream that yields text and completes, so no empty-response recovery re-runs the turn.

    ``_stream_empty`` triggers the runner's empty-response replay, which
    dispatches a SECOND ``_run_chat`` in the background; a test that asserts on
    shared state (the session-switch lock) after the first turn returns would
    race that replay. *on_stream* runs inside the live turn.
    """
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

    async def _complete(msg):
        if on_stream is not None:
            on_stream()
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ready")
        yield LLMEvent(kind=EVENT_COMPLETE)

    client.stream = _complete
    client.stream_command = _complete
    client.context_usage_pct = MagicMock(return_value=1.0)


def _stream_observes(client: MagicMock, sink: list) -> None:
    """A stream that records the slot's identity from INSIDE the live turn."""

    def _make(slot):
        async def _observe(msg):
            sink.append(slot._active_turn_session_key)
            return
            yield  # pragma: no cover - generator shape only

        return _observe

    return _make


class TestTheKeyIsInstalledForTheRunningTurn:
    @pytest.mark.asyncio
    async def test_a_plain_turn_publishes_its_own_session(self, tmp_path) -> None:
        state, slot, client = _state_and_slot(tmp_path)
        seen: list[str] = []
        client.stream = _stream_observes(client, seen)(slot)
        client.stream_command = client.stream

        await _run_chat(state, slot, "test message")

        assert seen == ["dashboard:turn-id-slot"]

    @pytest.mark.asyncio
    async def test_a_linked_slot_publishes_the_session_it_runs_on(self, tmp_path) -> None:
        """A channel-born tab is bound before its turn starts, so the captured
        identity IS the channel session."""
        state, slot, client = _state_and_slot(tmp_path)
        slot.linked_session_key = LINKED_KEY
        seen: list[str] = []
        client.stream = _stream_observes(client, seen)(slot)
        client.stream_command = client.stream

        await _run_chat(state, slot, "test message")

        assert seen == [LINKED_KEY]

    @pytest.mark.asyncio
    async def test_a_rebind_mid_turn_does_not_move_the_published_identity(self, tmp_path) -> None:
        """The whole point: the cron injection's assignment must not retarget a
        turn that is already running."""
        state, slot, client = _state_and_slot(tmp_path)
        seen: list[str] = []

        async def _rebind_then_finish(msg):
            slot.linked_session_key = "cron:nightly-report"
            seen.append(slot._active_turn_session_key)
            return
            yield  # pragma: no cover - generator shape only

        client.stream = _rebind_then_finish
        client.stream_command = _rebind_then_finish

        await _run_chat(state, slot, "test message")

        assert seen == ["dashboard:turn-id-slot"]


class TestTheKeyDoesNotOutliveItsTurn:
    @pytest.mark.asyncio
    async def test_cleared_after_a_normal_completion(self, tmp_path) -> None:
        state, slot, client = _state_and_slot(tmp_path)
        _stream_empty(client)

        await _run_chat(state, slot, "test message")

        assert slot._active_turn_session_key == ""

    @pytest.mark.asyncio
    async def test_cleared_when_teardown_itself_is_cancelled(self, tmp_path) -> None:
        """CancelledError derives from BaseException, so an ``except Exception``
        cleanup misses it — the shape that once stranded the session permit.

        Cancelled at the same place ``test_turn_teardown_release`` cancels:
        ``AcpAuthRequired`` sets ``needs_session_reset``, which puts an ``await``
        inside the teardown, and the cancel lands on it.
        """
        state, slot, client = _state_and_slot(tmp_path)

        async def _raise(msg):
            raise AcpAuthRequired("kiro-cli is not logged in.")
            yield  # pragma: no cover - generator shape only

        client.stream = _raise
        client.stream_command = _raise
        state.sessions.reset = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await _run_chat(state, slot, "test message")

        assert slot._active_turn_session_key == ""

    @pytest.mark.asyncio
    async def test_cleared_after_a_provider_error(self, tmp_path) -> None:
        state, slot, client = _state_and_slot(tmp_path)

        async def _raise(msg):
            raise AcpAuthRequired("kiro-cli is not logged in.")
            yield  # pragma: no cover - generator shape only

        client.stream = _raise
        client.stream_command = _raise

        await _run_chat(state, slot, "test message")

        assert slot._active_turn_session_key == ""

    @pytest.mark.asyncio
    async def test_cleared_when_the_session_was_never_acquired(self, tmp_path) -> None:
        state, slot, _client = _state_and_slot(tmp_path)
        state.sessions.get_or_create = AsyncMock(side_effect=RuntimeError("cold start failed"))

        await _run_chat(state, slot, "test message")

        assert slot._active_turn_session_key == ""


class TestOneTurnCannotRetireAnother:
    @pytest.mark.asyncio
    async def test_the_clear_lands_before_a_successor_can_start(
        self, tmp_path, monkeypatch
    ) -> None:
        """``_start_next_queued_turn`` runs inside the FIRST turn's teardown.

        Two orderings have to hold and both are invisible to a post-hoc read, so
        this observes the dispatch point itself: the retiring turn's identity
        must already be gone when the successor is dispatched, and whatever the
        successor installs must survive the rest of turn one's teardown.
        """
        state, slot, client = _state_and_slot(tmp_path)
        _stream_empty(client)
        slot.queue_append("second message")
        observed: dict[str, str] = {}

        async def _fake_start(st, sl) -> bool:
            observed["at_dispatch"] = sl._active_turn_session_key
            # Stand in for the successor publishing its own identity.
            sl._active_turn_session_key = "dashboard:successor"
            return True

        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._start_next_queued_turn", _fake_start)

        await _run_chat(state, slot, "first message")

        assert (
            observed.get("at_dispatch") == ""
        ), "the finished turn still advertised an identity when its successor started"
        assert (
            slot._active_turn_session_key == "dashboard:successor"
        ), "the retiring turn erased its successor's identity"


class TestThePromptsGetReEntry:
    """``/prompts get`` calls ``_run_chat`` again at ``_prompt_depth=1``.

    The depth-0 invocation is a local wrapper that returns without reaching the
    turn machinery; the depth-1 one is the turn. Keying the identity on
    ``_prompt_depth == 0`` would put it on the wrapper — which is why it is
    keyed on the local-command boundary instead.
    """

    @pytest.mark.asyncio
    async def test_the_inner_invocation_owns_the_identity(self, tmp_path, monkeypatch) -> None:
        state, slot, client = _state_and_slot(tmp_path)
        seen: list[str] = []
        # Stubbed at the resolver — the FILESYSTEM half — rather than at the
        # coroutine the command calls, so the real offload and the real on-loop
        # chip append stay in the path being measured. An empty chip appends
        # nothing, which keeps this test about the identity and not the message.
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._resolve_prompt_mention",
            lambda message, project_dir: ("expanded prompt body", "ok", ""),
        )

        async def _observe(msg):
            seen.append((msg, slot._active_turn_session_key))
            return
            yield  # pragma: no cover - generator shape only

        client.stream = _observe
        client.stream_command = _observe

        await _run_chat(state, slot, "/prompts get demo")

        assert seen == [
            ("expanded prompt body", "dashboard:turn-id-slot")
        ], "the real turn ran without a published identity"
        assert slot._active_turn_session_key == ""


class TestTheKeyIsPublishedBeforeAdmission:
    """The identity is installed BEFORE the first admission await, not after it.

    The admission awaits (the shared memory-preparation wait, the OPTIONS
    expiry) are where a cron rebind can land on a live slot, and ``slot.task``
    already reports the turn as running there. A key published only after
    admission leaves that whole window with no turn identity, so every reader
    of it -- the cancel routes, the slot-switch busy scan -- falls back to the
    mutable routing and can miss the session the turn is actually starting.
    Refusal at admission must retire the key through the same compare-and-clear
    the normal path uses: the turn's own key goes, a successor's stays.
    """

    @pytest.mark.asyncio
    async def test_visible_while_admission_is_pending_and_retired_on_refusal(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.memory_startup import MemoryStartupUnavailable

        state, slot, client = _state_and_slot(tmp_path)
        _stream_empty(client)
        # Rebind BEFORE the turn: the captured key is the channel session, the
        # value a later cancel or busy scan must find during admission.
        slot.linked_session_key = LINKED_KEY
        parked = asyncio.Event()
        release = asyncio.Event()
        verdict: dict[str, str] = {"admit": "refuse"}

        async def _park_then_decide(task) -> None:
            parked.set()
            await release.wait()
            if verdict["admit"] == "refuse":
                raise MemoryStartupUnavailable("Memory preparation is still running.")

        monkeypatch.setattr(
            "kiro_crew.memory_startup.wait_for_memory_preparation", _park_then_decide
        )

        # Phase 1: the key is visible while the turn is parked on admission,
        # and a routing rebind landing in that window does not move it.
        turn = asyncio.create_task(_run_chat(state, slot, "test message"))
        await asyncio.wait_for(parked.wait(), timeout=5)
        assert (
            slot._active_turn_session_key == LINKED_KEY
        ), "the turn was parked on admission with no published identity"
        slot.linked_session_key = "cron:nightly-report"
        await asyncio.sleep(0)
        assert slot._active_turn_session_key == LINKED_KEY
        # Refused: the turn retires the key it published.
        release.set()
        await asyncio.wait_for(turn, timeout=5)
        assert slot._active_turn_session_key == "", "a refused turn left its identity behind"

        # Phase 2: the same refusal must not clobber a successor's key. While
        # this turn is parked, a successor publishes; the refusal's
        # compare-and-clear sees a key that is not its own and leaves it.
        parked.clear()
        release.clear()
        slot.linked_session_key = LINKED_KEY
        slot.append("user", "again", "msg msg-u2")
        turn = asyncio.create_task(_run_chat(state, slot, "test message"))
        await asyncio.wait_for(parked.wait(), timeout=5)
        assert slot._active_turn_session_key == LINKED_KEY
        slot._active_turn_session_key = "dashboard:successor"
        release.set()
        await asyncio.wait_for(turn, timeout=5)
        assert (
            slot._active_turn_session_key == "dashboard:successor"
        ), "the refused turn erased its successor's identity"


class TestDispatchSerializesWithTheSwitchLock:
    """Binding capture + session registration run under the slot-switch lock.

    The switch handlers hold ``slot_switch_session_lock(session_key)`` across
    their busy scan and their reset. A dispatch that captured its bindings
    before that scan and registered its session after the reset would start
    the shared session on the pre-switch bindings while the switch reports
    success. So the dispatch takes the same lock from binding capture through
    ``get_or_create`` (registration happens inside it): started while a switch
    holds the lock, it parks, then captures whatever the switch committed --
    and it releases before the turn streams, since the refusal-fallback
    helpers take the same non-reentrant lock during the turn.
    """

    @pytest.mark.asyncio
    async def test_dispatch_parks_on_a_held_switch_lock_and_starts_on_new_bindings(
        self, tmp_path, monkeypatch
    ) -> None:
        class _ObservedLock(asyncio.Lock):
            def __init__(self) -> None:
                super().__init__()
                self.waiting = asyncio.Event()

            async def acquire(self) -> bool:
                self.waiting.set()
                return await super().acquire()

        observed = _ObservedLock()
        keys_seen: list[str] = []

        def _lock_for(key: str) -> asyncio.Lock:
            keys_seen.append(key)
            return observed

        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.slot_switch_session_lock", _lock_for)

        state, slot, client = _state_and_slot(tmp_path)
        # A cold start: nothing registered for this session yet.
        state.sessions.has_session = MagicMock(return_value=False)
        slot.linked_session_key = LINKED_KEY
        slot.model = "claude-opus-4.8"
        held_while_streaming: list[bool] = []
        _stream_completes(client, lambda: held_while_streaming.append(observed.locked()))

        # A switch transaction on the shared session is in progress.
        async with observed:
            turn = asyncio.create_task(_run_chat(state, slot, "test message"))
            await asyncio.wait_for(observed.waiting.wait(), timeout=5)
            # Parked: nothing registered while the switch holds the lock.
            await asyncio.sleep(0)
            state.sessions.get_or_create.assert_not_awaited()
            # The switch commits its new binding inside its critical section.
            slot.model = "gpt-5.6-sol"
        await asyncio.wait_for(turn, timeout=5)

        assert keys_seen == [
            LINKED_KEY
        ], "the dispatch keyed the lock on something other than its session"
        state.sessions.get_or_create.assert_awaited_once()
        assert (
            state.sessions.get_or_create.await_args.kwargs["model"] == "gpt-5.6-sol"
        ), "the session registered on the pre-switch binding"
        assert (
            state.sessions.get_or_create.await_args.kwargs["wait_if_busy"] is False
        ), "a cold start under the switch lock must never wait for a turn lease"
        assert held_while_streaming == [False], "the dispatch held the switch lock into the turn"
        assert not observed.locked(), "the dispatch leaked the switch lock"


class TestDispatchNeverWaitsForALeaseUnderTheSwitchLock:
    """A busy, already-registered session: the lease wait happens OUTSIDE the lock.

    Alias A dispatches onto a session whose turn lease alias B's live turn
    holds. B's turn will, before it releases the lease, take the switch lock
    (its refusal-fallback restore does). If A waited for the lease while
    holding the switch lock, A and B would wait on each other until the lease
    timeout. A registered session is already visible to the switch handlers'
    busy scan, so the lock protects nothing there: A must drop it before
    waiting.
    """

    @pytest.mark.asyncio
    async def test_busy_session_lease_wait_does_not_hold_the_switch_lock(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.llm_helpers import slot_switch_session_lock

        state, slot, client = _state_and_slot(tmp_path)
        slot.linked_session_key = LINKED_KEY
        # B's turn is live on this session: registered, lease held.
        state.sessions.has_session = MagicMock(return_value=True)
        lock = slot_switch_session_lock(LINKED_KEY)
        lease_released = asyncio.Event()
        real_get_or_create = state.sessions.get_or_create

        async def _wait_for_lease(key, **kwargs):
            # Stands in for ``await session.semaphore.acquire()`` on a busy
            # session: returns only once B's turn has let go of the lease.
            await lease_released.wait()
            return await real_get_or_create(key, **kwargs)

        state.sessions.get_or_create = AsyncMock(side_effect=_wait_for_lease)
        _stream_completes(client)

        async def _b_turn_restore_then_release() -> None:
            # B's refusal-fallback restore: needs the switch lock, then B's
            # turn ends and the lease is released.
            async with lock:
                await asyncio.sleep(0)
            lease_released.set()

        a_turn = asyncio.create_task(_run_chat(state, slot, "alias A message"))
        # Let A reach its lease wait before B's turn goes for the lock.
        for _ in range(50):
            await asyncio.sleep(0)
        b_turn = asyncio.create_task(_b_turn_restore_then_release())
        await asyncio.wait_for(asyncio.gather(a_turn, b_turn), timeout=5)

        state.sessions.get_or_create.assert_awaited_once()
        assert (
            state.sessions.get_or_create.await_args.kwargs["wait_if_busy"] is True
        ), "a registered session is claimed with the normal lease wait"
        assert not lock.locked(), "the dispatch leaked the switch lock"
