"""Tests for the async slot-rehydration wrapper's thread split.

Two competing constraints meet here, and the wrapper exists to satisfy both:

* The disk reads must NOT run on the event loop. A real session transcript
  reaches tens of MB, and the gateway serves every HTTP request, every agent
  turn and the stall-watchdog heartbeat on one thread — so reading one inline
  in a timer callback stalls all of it.
* Slot construction must NOT run off the event loop. It broadcasts through
  ``asyncio.Queue.put_nowait`` and ``Event.set`` (neither thread-safe) and
  through ``ensure_future``, which off-loop raises inside a broad ``except``
  that marks every connected dashboard client dead and drops it *without a
  close frame* — browsers then never reconnect and stop receiving frames until
  a manual reload. ``restore_open_slots_async`` documents the same invariant.

So the wrapper hoists only the reads. These tests pin that split by recording
the thread each half actually ran on.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard import chat_persistence as cp


class _Log:
    """Conversation-log stub that records which thread read from it."""

    def __init__(self, meta: dict | None, messages: list[dict] | None) -> None:
        self._meta = meta
        self._messages = messages or []
        self.read_threads: list[str] = []

    def get_metadata(self, key: str) -> dict | None:
        self.read_threads.append(threading.current_thread().name)
        return self._meta

    def read_messages_chained(self, key: str) -> list[dict]:
        self.read_threads.append(threading.current_thread().name)
        return self._messages


def _state(log: _Log) -> MagicMock:
    state = MagicMock()
    state.conversation_log = log
    state._slots = {}
    return state


@pytest.mark.asyncio
async def test_reads_run_off_the_loop_and_build_runs_on_it(monkeypatch) -> None:
    log = _Log({"title": "Babysit PR"}, [{"role": "user", "content": "hi"}])
    state = _state(log)
    built_on: list[str] = []
    sentinel = MagicMock(name="slot")

    def _build(_state, _slot_name, **kwargs):
        built_on.append(threading.current_thread().name)
        # The prefetched values must be threaded through so the builder does no
        # further disk I/O of its own.
        assert kwargs["_prefetched_meta"] == {"title": "Babysit PR"}
        assert kwargs["_prefetched_messages"] == [{"role": "user", "content": "hi"}]
        return sentinel

    monkeypatch.setattr(cp, "_rehydrate_slot_from_history", _build)
    monkeypatch.setattr(cp, "_build_kiro_model_map", lambda: {})

    main = threading.current_thread().name
    result = await cp.rehydrate_slot_from_history_async(state, "chat-1-1785")

    assert result is sentinel
    assert log.read_threads, "the transcript was never read"
    assert all(t != main for t in log.read_threads), "a disk read ran on the event loop"
    assert built_on == [main], "slot construction left the event-loop thread"


@pytest.mark.asyncio
async def test_close_during_the_read_abandons_rehydration(monkeypatch) -> None:
    """✕ clicked while the transcript loads must not resurrect the tab.

    The close pops the slot and records a tombstone synchronously on the loop,
    but persists the ``closed`` flag only after its own awaits — so metadata
    read before the click still says open. Rebuilding from that stale snapshot
    would re-create a dismissed tab and then fire a nudge turn into it.
    """
    from kiro_crew.dashboard import channel_slots

    log = _Log({"title": "Babysit PR"}, [{"role": "user", "content": "hi"}])
    state = _state(log)
    monkeypatch.setattr(cp, "_build_kiro_model_map", lambda: {})
    monkeypatch.setattr(
        cp, "_rehydrate_slot_from_history", lambda *a, **k: pytest.fail("rebuilt a closed tab")
    )

    real_read = log.read_messages_chained

    def _read_then_close(key: str):
        # Simulate the user clicking ✕ while this read is in flight.
        channel_slots.note_slot_closed(state, "chat-1-1785")
        return real_read(key)

    monkeypatch.setattr(log, "read_messages_chained", _read_then_close)

    assert await cp.rehydrate_slot_from_history_async(state, "chat-1-1785") is None


@pytest.mark.asyncio
async def test_a_close_predating_the_read_does_not_block(monkeypatch) -> None:
    """Only a close DURING the read is the race; older tombstones are inert.

    A close that already landed is visible in the metadata itself, so the guard
    must not additionally reject an unrelated stale tombstone — that would make
    a reopened tab un-rehydratable for the tombstone's lifetime.
    """
    import time as _time

    from kiro_crew.dashboard import channel_slots

    log = _Log({"title": "Reopened"}, [{"role": "user", "content": "hi"}])
    state = _state(log)
    channel_slots.note_slot_closed(state, "chat-1-old")  # then reopened
    _time.sleep(0.01)
    sentinel = MagicMock(name="slot")
    monkeypatch.setattr(cp, "_build_kiro_model_map", lambda: {})
    monkeypatch.setattr(cp, "_rehydrate_slot_from_history", lambda *a, **k: sentinel)

    assert await cp.rehydrate_slot_from_history_async(state, "chat-1-old") is sentinel


@pytest.mark.asyncio
async def test_missing_metadata_returns_none_without_building(monkeypatch) -> None:
    """A session that was never persisted must not create a phantom tab."""
    state = _state(_Log(None, None))
    monkeypatch.setattr(
        cp, "_rehydrate_slot_from_history", lambda *a, **k: pytest.fail("built a phantom slot")
    )
    assert await cp.rehydrate_slot_from_history_async(state, "chat-1-nope") is None


@pytest.mark.asyncio
async def test_closed_session_is_not_resurrected(monkeypatch) -> None:
    """Clicking ✕ is respected — same contract as the synchronous form."""
    state = _state(_Log({"closed": True}, []))
    monkeypatch.setattr(
        cp, "_rehydrate_slot_from_history", lambda *a, **k: pytest.fail("resurrected a closed tab")
    )
    assert await cp.rehydrate_slot_from_history_async(state, "chat-1-closed") is None


@pytest.mark.asyncio
async def test_already_loaded_slot_short_circuits(monkeypatch) -> None:
    """The hot path must not pay for a thread hop or a read."""
    log = _Log({"title": "x"}, [])
    state = _state(log)
    live = MagicMock(name="live-slot")
    state._slots = {"chat-1-hot": live}
    monkeypatch.setattr(cp, "_normalize_slot_key", lambda k: k)

    assert await cp.rehydrate_slot_from_history_async(state, "chat-1-hot") is live
    assert log.read_threads == [], "short-circuit still read from disk"


@pytest.mark.asyncio
async def test_no_conversation_log_returns_none() -> None:
    state = MagicMock()
    state.conversation_log = None
    assert await cp.rehydrate_slot_from_history_async(state, "chat-1-x") is None
