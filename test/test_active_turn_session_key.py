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
        identity IS the channel session — which is what keeps the #2462 fix."""
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
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._expand_prompt_mention",
            lambda mention, st, sl: ("expanded prompt body", "ok"),
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
