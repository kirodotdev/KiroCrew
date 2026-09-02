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
        self.recheck_threads: list[str] = []

    def get_metadata(self, key: str) -> dict | None:
        self.read_threads.append(threading.current_thread().name)
        return self._meta

    def read_messages_chained(self, key: str) -> list[dict]:
        self.read_threads.append(threading.current_thread().name)
        return self._messages

    def get_metadata_status(self, key: str) -> tuple[dict, bool]:
        """The post-hop deletion re-check reads through here.

        Recorded SEPARATELY from ``read_threads``: this one runs ON the loop by
        design (it gates the build, so no await may separate the two), while
        every entry in ``read_threads`` must have left the loop. Lumping them
        together would make the off-loop assertions unsatisfiable.
        """
        self.recheck_threads.append(threading.current_thread().name)
        return self._meta or {}, True


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


# ── The shared prefetch seam (#895) ──
#
# The wrapper's read half is now a named module-level function so the two bulk
# startup restore drivers can hoist the SAME reads into a worker thread instead
# of each growing its own copy. These tests pin the contract the drivers depend
# on, which the wrapper's own tests above do not reach: the readability flag, and
# not paying for a transcript walk that will be thrown away.


class _StatusLog(_Log):
    """``_Log`` plus the ``get_metadata_status`` form the open-tab restore needs."""

    def __init__(self, meta: dict | None, messages: list[dict] | None, readable: bool) -> None:
        super().__init__(meta, messages)
        self._readable = readable

    def get_metadata_status(self, key: str) -> tuple[dict, bool]:
        self.read_threads.append(threading.current_thread().name)
        return self._meta or {}, self._readable


def test_prefetch_reports_an_unreadable_metadata_read() -> None:
    """``with_status`` must carry the difference ``get_metadata`` cannot express.

    ``{}`` means both "never persisted" and "could not be read after retries",
    and the open-tab restore does something destructive with the second reading:
    it drops a live tab. So the flag must survive the trip through the prefetch.
    """
    log = _StatusLog({}, [], readable=False)
    meta, readable, messages, model_map, _mid = cp._prefetch_rehydrate_inputs(
        log, "dashboard:chat-1-x", with_status=True
    )
    assert meta == {}
    assert readable is False
    assert messages is None, "an unreadable session must not report a transcript"
    assert model_map is None


def test_prefetch_skips_the_transcript_walk_for_an_absent_session() -> None:
    """No metadata → no transcript read. The walk is the expensive half."""
    log = _StatusLog({}, [{"role": "user", "content": "hi"}], readable=True)
    meta, readable, messages, _, _mid = cp._prefetch_rehydrate_inputs(
        log, "dashboard:chat-1-x", with_status=True
    )
    assert (meta, readable, messages) == ({}, True, None)
    # Only the metadata line was read — one recorded read, not two — even though
    # the stub has a transcript sitting there ready to hand over.
    assert len(log.read_threads) == 1


def test_prefetch_skips_the_transcript_walk_for_a_closed_session() -> None:
    """A session closed with ✕ is not rebuilt, so its transcript is dead weight."""
    log = _Log({"closed": True}, [{"role": "user", "content": "hi"}])
    _meta, _readable, messages, model_map, _mid = cp._prefetch_rehydrate_inputs(
        log, "dashboard:chat-1-closed"
    )
    assert messages is None
    assert model_map is None
    # Only the metadata line was read — one recorded read, not two.
    assert len(log.read_threads) == 1


def test_prefetch_adopts_a_closed_session_on_request() -> None:
    """``adopt_closed`` callers (app-owned worker slots) still get the walk."""
    log = _Log({"closed": True}, [{"role": "user", "content": "hi"}])
    _meta, _readable, messages, _, _mid = cp._prefetch_rehydrate_inputs(
        log, "dashboard:chat-1-closed", adopt_closed=True, kiro_model_map={}
    )
    assert messages == [{"role": "user", "content": "hi"}]


def test_prefetch_reuses_a_callers_model_map() -> None:
    """A bulk caller builds the agent→model map once; the prefetch must not re-glob.

    Rebuilding it per slot re-globs and re-parses every agent JSON to produce a
    byte-identical dict — O(N) directory scans for one boot.
    """
    log = _Log({"title": "x"}, [])
    shared = {"kirocrew": "sonnet"}
    called = False

    def _boom() -> dict[str, str]:
        nonlocal called
        called = True
        return {}

    original = cp._build_kiro_model_map
    cp._build_kiro_model_map = _boom  # type: ignore[assignment]
    try:
        _m, _r, _msgs, model_map, _mid = cp._prefetch_rehydrate_inputs(
            log, "dashboard:chat-1-x", kiro_model_map=shared
        )
    finally:
        cp._build_kiro_model_map = original  # type: ignore[assignment]

    assert model_map is shared
    assert called is False, "the prefetch rebuilt a map the caller already had"


# ── Deletion during the wrapper's own read (#895 round 4) ──
#
# This wrapper's read has ALWAYS been offloaded, so its delete-during-read window
# predates #895 — but a class of defect fixed at two of three prefetch-then-apply
# call sites just returns through the third, so the guard is folded in here too.
# Its three production callers (the Slack gateway's AutoNudge dashboard fire, the
# spec-builder route, and the issue-radar crew runtime) already handle a ``None``
# return, which is what a refusal looks like.


@pytest.mark.asyncio
async def test_a_deletion_during_the_read_abandons_rehydration(monkeypatch) -> None:
    """A session deleted mid-read must not be rebuilt from the stale snapshot.

    ``delete_session`` leaves no tombstone, so the rebuilt slot's next flush
    recreates the transcript the user permanently deleted.
    """
    log = _Log(
        {"title": "Doomed", "created_at": "2026-01-01T00:00:00"},
        [{"role": "user", "content": "hi"}],
    )
    state = _state(log)
    monkeypatch.setattr(cp, "_build_kiro_model_map", lambda: {})
    monkeypatch.setattr(
        cp,
        "_rehydrate_slot_from_history",
        lambda *a, **k: pytest.fail("rebuilt a slot for a deleted session"),
    )
    # The post-hop re-check sees the session gone.
    monkeypatch.setattr(log, "get_metadata_status", lambda key: ({}, True))

    assert await cp.rehydrate_slot_from_history_async(state, "chat-1-doomed") is None


@pytest.mark.asyncio
async def test_a_recreate_during_the_read_abandons_rehydration(monkeypatch) -> None:
    """Delete-then-recreate leaves metadata present but under a new identity.

    Rebuilding would publish a slot holding the OLD transcript, whose flush
    overwrites the conversation the user is now using.
    """
    log = _Log(
        {"title": "Swapped", "created_at": "2026-01-01T00:00:00"},
        [{"role": "user", "content": "hi"}],
    )
    state = _state(log)
    monkeypatch.setattr(cp, "_build_kiro_model_map", lambda: {})
    monkeypatch.setattr(
        cp,
        "_rehydrate_slot_from_history",
        lambda *a, **k: pytest.fail("rebuilt a slot over a replacement session"),
    )
    monkeypatch.setattr(
        log, "get_metadata_status", lambda key: ({"created_at": "2099-01-01T00:00:00"}, True)
    )

    assert await cp.rehydrate_slot_from_history_async(state, "chat-1-swapped") is None


@pytest.mark.asyncio
async def test_the_deletion_guard_applies_even_to_adopt_closed(monkeypatch) -> None:
    """``adopt_closed`` opts into the closed FLAG, never into resurrection.

    App-owned worker slots may legitimately restore a session carrying ``closed``,
    because their lifecycle belongs to the app. Nothing wants a permanently
    DELETED transcript back.
    """
    log = _Log(
        {"closed": True, "created_at": "2026-01-01T00:00:00"}, [{"role": "user", "content": "hi"}]
    )
    state = _state(log)
    monkeypatch.setattr(cp, "_build_kiro_model_map", lambda: {})
    monkeypatch.setattr(
        cp,
        "_rehydrate_slot_from_history",
        lambda *a, **k: pytest.fail("resurrected a deleted app-owned session"),
    )
    monkeypatch.setattr(log, "get_metadata_status", lambda key: ({}, True))

    assert (
        await cp.rehydrate_slot_from_history_async(state, "chat-1-worker", adopt_closed=True)
        is None
    )


@pytest.mark.asyncio
async def test_an_intact_session_still_rehydrates(monkeypatch) -> None:
    """Negative control: the guard must not refuse an untouched session."""
    log = _Log(
        {"title": "Fine", "created_at": "2026-01-01T00:00:00"}, [{"role": "user", "content": "hi"}]
    )
    state = _state(log)
    sentinel = MagicMock(name="slot")
    monkeypatch.setattr(cp, "_build_kiro_model_map", lambda: {})
    monkeypatch.setattr(cp, "_rehydrate_slot_from_history", lambda *a, **k: sentinel)

    assert await cp.rehydrate_slot_from_history_async(state, "chat-1-fine") is sentinel


@pytest.mark.asyncio
async def test_the_deletion_recheck_runs_on_the_loop(monkeypatch) -> None:
    """The guard must stay ON the loop — offloading it reopens the window.

    ``_deletion_during_read`` gates the build; an await between the two would let
    a delete land in the gap. One mtime-cached metadata line is the price.
    """
    log = _Log(
        {"title": "Fine", "created_at": "2026-01-01T00:00:00"}, [{"role": "user", "content": "hi"}]
    )
    state = _state(log)
    monkeypatch.setattr(cp, "_build_kiro_model_map", lambda: {})
    monkeypatch.setattr(cp, "_rehydrate_slot_from_history", lambda *a, **k: MagicMock())

    main = threading.current_thread().name
    await cp.rehydrate_slot_from_history_async(state, "chat-1-fine")

    assert log.recheck_threads == [main], (
        "the deletion re-check must run on the loop, immediately before the "
        f"build it gates (threads: {log.recheck_threads})"
    )
    assert log.read_threads and all(
        t != main for t in log.read_threads
    ), "the prefetch reads must still be off the loop"
