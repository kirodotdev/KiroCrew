"""Tests for session-permit release at dashboard turn teardown.

The defect these pin: every ``await`` in ``_run_chat``'s ``finally`` was guarded
by ``except Exception``, but ``CancelledError`` has derived from
``BaseException`` since Python 3.8. A cancellation delivered while one of those
awaits was suspended slipped past every guard and skipped
``state.sessions.release(...)`` entirely.

The permit is keyed by SESSION, so leaking it does not merely lose one turn: the
session reads as permanently busy, every later turn for it blocks forever, no
queued turn drains, and only a gateway restart clears it. From the user's side
the chat simply stops accepting messages with no error shown.

Commit 9359b3d1 fixed this shape for the messaging dispatcher, but with
``except Exception`` — already present here — so it does not cover the
cancellation case. A nested ``try``/``finally`` does.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.acp.client import AcpAuthRequired
from kiro_crew.dashboard.chat_runner import _run_chat


def _state_and_slot(tmp_path: Path):
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
    slot = state.get_or_create_slot("release-slot")
    slot.append("user", "hello", "msg msg-u")
    client = state.sessions.get_or_create.return_value[0]
    client.shutdown = AsyncMock()
    return state, slot, client


def _stream_raises(client: MagicMock, exc: BaseException) -> None:
    async def _raise(msg):
        raise exc
        yield  # pragma: no cover - generator shape only

    client.stream = _raise
    client.stream_command = _raise


class TestTurnTeardownRelease:
    @pytest.mark.asyncio
    async def test_release_survives_cancellation_during_session_reset(self, tmp_path) -> None:
        """The regression: a cancelled reset must not strand the permit.

        AcpAuthRequired sets ``needs_session_reset``, which is what puts an
        ``await`` between the ``finally`` and the release.
        """
        state, slot, client = _state_and_slot(tmp_path)
        _stream_raises(client, AcpAuthRequired("kiro-cli is not logged in."))
        state.sessions.reset = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await _run_chat(state, slot, "test message")

        state.sessions.release.assert_called_once()

    @pytest.mark.asyncio
    async def test_release_happens_exactly_once_on_the_happy_path(self, tmp_path) -> None:
        """A double release on a plain Semaphore(1) would destroy mutual exclusion.

        ``_Session.semaphore`` is not a BoundedSemaphore, so an extra release
        raises nothing and silently lets two turns run concurrently. Guard
        against a fix that hands the permit back twice.
        """
        state, slot, client = _state_and_slot(tmp_path)

        async def _empty(msg):
            return
            yield  # pragma: no cover - generator shape only

        client.stream = _empty
        client.stream_command = _empty

        await _run_chat(state, slot, "test message")

        assert state.sessions.release.call_count == 1

    @pytest.mark.asyncio
    async def test_release_still_runs_when_reset_raises_normally(self, tmp_path) -> None:
        state, slot, client = _state_and_slot(tmp_path)
        _stream_raises(client, AcpAuthRequired("kiro-cli is not logged in."))
        state.sessions.reset = AsyncMock(side_effect=RuntimeError("reset exploded"))

        await _run_chat(state, slot, "test message")

        state.sessions.release.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_release_when_the_permit_was_never_acquired(self, tmp_path) -> None:
        """get_or_create failing before the permit is taken must not release."""
        state, slot, _client = _state_and_slot(tmp_path)
        state.sessions.get_or_create = AsyncMock(side_effect=RuntimeError("cold start failed"))

        await _run_chat(state, slot, "test message")

        state.sessions.release.assert_not_called()
