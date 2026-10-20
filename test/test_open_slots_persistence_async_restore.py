"""Open-slots restore tests: the startup restore runs off the event loop.

The async restore yields between tabs, reads transcripts and metadata on a worker
thread, keeps the periodic flush from truncating the snapshot it reads, and refuses
to republish a session deleted or closed while its read was in flight.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from unittest.mock import patch

import pytest
from chat_test_helpers import _make_state
from open_slots_persistence_helpers import _seed_session

from kiro_crew.dashboard.chat_persistence import (
    _rehydrate_slot_from_history,
    restore_open_slots,
    restore_open_slots_async,
)
from kiro_crew.dashboard.chat_utils import _history_key_for
from kiro_crew.testing.clock import ManualClock

# ── Startup must not block the event loop (stall-watchdog crash-loop) ──
#
# Regression cover for the gateway crash-loop: restoring many large tabs ran
# synchronously on the event loop, so the LoopStallWatchdog heartbeat (which pets
# the watchdog FROM A COROUTINE) never got a turn. After exit_after=25s the
# watchdog dumped thread stacks and _exit()ed, so the app never finished starting.


def test_restore_open_slots_async_yields_between_tabs(tmp_path, monkeypatch):
    """The async restore must hand the loop back per tab so the heartbeat can run.

    Pins the actual crash mechanism: a coroutine running concurrently with the
    restore must observe a partially restored slot set. If restore ever blocks
    through every tab and yields only after the work is done, this fails.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    tab_count = 6
    for i in range(tab_count):
        _seed_session(state, f"chat-{i}-yield")
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": [f"chat-{i}-yield" for i in range(tab_count)], "ts": 0.0})
    )

    state2 = _make_state(tmp_path / "sessions")
    partial_restore_observed = False

    async def _drive():
        async def ticker():
            nonlocal partial_restore_observed
            while True:
                restored_so_far = len(state2._slots)
                if 0 < restored_so_far < tab_count:
                    partial_restore_observed = True
                await asyncio.sleep(0)

        t = asyncio.create_task(ticker())
        try:
            return await restore_open_slots_async(state2)
        finally:
            t.cancel()
            # `cancel()` only requests cancellation; without awaiting it the ticker is
            # still live when `asyncio.run` tears the loop down, leaving a "coroutine
            # ignored GeneratorExit" for a later test to trip over.
            try:
                await t
            except asyncio.CancelledError:
                pass

    restored = asyncio.run(_drive())
    assert restored == tab_count
    # Observe intermediate progress rather than an incidental number of scheduler
    # turns; a single yield after all tab work is complete must not satisfy the test.
    assert partial_restore_observed, "restore did not yield between tabs"


def test_restore_reads_transcript_before_backfilling_tab_id(tmp_path, monkeypatch):
    """A tab needing a tab_id backfill must be READ before the backfill fires.

    ``_rehydrate_slot_from_history`` mints a tab_id for a legacy session that
    lacks one and persists it via ``update_metadata_off_loop`` — which dispatches
    an ``os.replace()`` of THIS session file to a worker thread. If that write is
    dispatched BEFORE the loop-thread transcript read of the same file, the
    replace races the read: on Windows the in-flight replace makes the reader's
    ``open()`` raise a sharing violation, and the on-loop read retry cannot pause
    (a loop sleep would starve the LoopStallWatchdog), so it drops the tab —
    the intermittent ``restored == N-1`` open-tabs loss on restart.

    Reproduced deterministically without threads: dispatching the backfill for a
    key arms its transcript read to raise the sharing violation, standing in for
    the in-flight replace holding the file. Under the correct order (read first)
    the read completes while the file is quiescent, so nothing is ever armed and
    every tab restores. Under the buggy order the armed read faults and the tab
    is dropped. The read-retry mechanics themselves are out of scope here (they
    are exercised by test_history_storage's sharing-violation tests); this pins the
    ordering that keeps the file quiescent for the read in the first place.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    n = 4
    for i in range(n):
        # _seed_session writes no tab_id, so every tab triggers the backfill.
        _seed_session(state, f"chat-{i}-race")
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": [f"chat-{i}-race" for i in range(n)], "ts": 0.0})
    )

    import kiro_crew.dashboard.chat_persistence as chat_persistence

    state2 = _make_state(tmp_path / "sessions")
    log = state2.conversation_log
    armed_unreadable: set[str] = set()

    real_read_messages = log._read_messages

    def guarded_read_messages(key):
        if key in armed_unreadable:
            raise PermissionError(f"[WinError 32] simulated in-flight os.replace holding {key}")
        return real_read_messages(key)

    monkeypatch.setattr(log, "_read_messages", guarded_read_messages)

    real_backfill = chat_persistence.update_metadata_off_loop

    def arming_backfill(conv_log, key, fields):
        # Dispatching the tab_id os.replace makes the file briefly unreadable.
        armed_unreadable.add(key)
        return real_backfill(conv_log, key, fields)

    monkeypatch.setattr(chat_persistence, "update_metadata_off_loop", arming_backfill)

    restored = asyncio.run(restore_open_slots_async(state2))

    assert restored == n, (
        "a tab was dropped: its transcript was read while its tab_id backfill "
        "replace was in flight (read must precede the backfill dispatch)"
    )
    assert set(state2._slots) == {f"chat-{i}-race" for i in range(n)}


def test_restore_open_slots_drains_its_tab_id_backfills_before_returning(tmp_path, monkeypatch):
    """A tab-id backfill is an ``os.replace`` of a session file. If the open-slot
    restore leaves it in flight (the fire-and-forget off-loop helper), it can
    still be mid-replace when a caller reads the same transcript on the loop
    right after restore — on Windows that ``open()`` is a sharing violation
    (``PermissionError``) the on-loop read cannot wait out, so it re-raises (the
    intermittent dropped-tab / read error on restart). The async open-slot
    driver must COMMIT its own backfills before it returns, the way the
    recent-session driver does.

    The seeded transcripts carry no ``tab_id``, so every restored tab mints one
    and schedules a backfill. This asserts the driver's contract: it does NOT
    hand those backfills to the fire-and-forget off-loop helper, and once it
    RETURNS the backfilled ``tab_id`` is already durable on every restored tab —
    nothing left mid-replace to race a reader.

    Fails on the base: there the backfill goes through
    ``update_metadata_off_loop`` and is still in flight at return.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = ("chat-1-drain", "chat-2-drain")
    for k in keys:
        _seed_session(state, k)  # no tab_id -> restore mints + backfills one
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": list(keys), "ts": 0.0}))

    import kiro_crew.dashboard.chat_persistence as chat_persistence

    state2 = _make_state(tmp_path / "sessions")
    log = state2.conversation_log

    # The fire-and-forget helper must not be the one carrying a restore backfill:
    # that is the path whose os.replace outlives the driver's return and races
    # the next on-loop reader.
    fire_and_forget: list[tuple[str, dict]] = []
    real_off_loop = chat_persistence.update_metadata_off_loop

    def _record_off_loop(conv_log, key, fields):
        fire_and_forget.append((key, dict(fields)))
        return real_off_loop(conv_log, key, fields)

    monkeypatch.setattr(chat_persistence, "update_metadata_off_loop", _record_off_loop)

    restored = asyncio.run(restore_open_slots_async(state2))

    assert restored == 2
    # The async driver drained the backfills itself rather than firing them off:
    # nothing was handed to the fire-and-forget helper.
    assert fire_and_forget == [], (
        "an open-slot tab-id backfill was fired off the loop; its os.replace "
        "outlives restore and can race a reader on Windows"
    )
    # Every restored tab's backfilled tab_id is already durable on disk the
    # moment restore returned — no replace still in flight.
    for k in keys:
        assert log.get_metadata(_history_key_for(k)).get("tab_id"), k
    # An immediate on-loop read of a restored transcript still returns it.
    assert log.read_messages_chained(_history_key_for(keys[0]))


def test_open_slot_drained_backfill_does_not_recreate_a_session_deleted_mid_restore(
    tmp_path, monkeypatch
):
    """Deferring the tab-id backfills to the end of the restore pass widens the
    window in which a ``DELETE /api/sessions/{key}`` can land. The drain must go
    through ``update_metadata_if(require_existing=True)``, which re-checks
    existence UNDER the write lock, so a session unlinked during that window is
    left deleted instead of being resurrected as a metadata-only transcript the
    next restore would reopen as an empty tab.

    We model the delete by unlinking one restored transcript as its slot is
    applied — after it is queued for backfill but before the end-of-loop drain
    runs. The drained backfill for that key must NOT recreate the file.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = ("chat-1-live", "chat-2-victim")
    for k in keys:
        _seed_session(state, k)
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": list(keys), "ts": 0.0}))

    import kiro_crew.dashboard.chat_persistence as chat_persistence

    state2 = _make_state(tmp_path / "sessions")
    log = state2.conversation_log
    victim_key = "chat-2-victim"
    victim_path = (tmp_path / "sessions") / f"{_history_key_for(victim_key)}.jsonl"
    real_apply = chat_persistence._apply_restored_open_slot

    def _apply_then_maybe_delete(state_, key, *args, **kwargs):
        result = real_apply(state_, key, *args, **kwargs)
        # The delete lands after the victim's backfill is queued but before the
        # end-of-loop drain commits it.
        if key == victim_key and victim_path.exists():
            victim_path.unlink()
        return result

    monkeypatch.setattr(chat_persistence, "_apply_restored_open_slot", _apply_then_maybe_delete)

    asyncio.run(restore_open_slots_async(state2))

    # The deleted transcript was NOT recreated by the drained backfill.
    assert not victim_path.exists(), "a deleted session was resurrected by its backfill"
    # The surviving tab still got its tab_id.
    assert log.get_metadata(_history_key_for("chat-1-live")).get("tab_id")


def test_restore_open_slots_async_matches_sync_result(tmp_path, monkeypatch):
    """The async and sync drivers must restore the same slots.

    They share one generator, so this guards the two thin wrappers from drifting.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-same")
    _seed_session(state, "chat-2-same")
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": ["chat-1-same", "chat-2-same"], "ts": 0.0})
    )

    sync_state = _make_state(tmp_path / "sessions")
    async_state = _make_state(tmp_path / "sessions")
    assert restore_open_slots(sync_state) == asyncio.run(restore_open_slots_async(async_state))
    assert set(sync_state._slots) == set(async_state._slots) == {"chat-1-same", "chat-2-same"}


def test_rehydrate_does_not_scan_the_whole_session_dir(tmp_path, monkeypatch):
    """Rehydrating one tab must not call list_sessions().

    list_sessions() stats + reads the first line of EVERY session file. Running it
    once per restored tab to look up one title (which it cannot find anyway — its
    keys are filename stems, ``dashboard_x``, while the lookup uses the canonical
    ``dashboard:x``) makes restore O(tabs x all sessions), which is ~13s of stall
    on a real 77-tab home.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-noscan")
    state.conversation_log.update_metadata(
        _history_key_for("chat-1-noscan"), {"title": "Kept Title"}
    )

    state2 = _make_state(tmp_path / "sessions")
    with patch.object(
        type(state2.conversation_log),
        "list_sessions",
        side_effect=AssertionError("list_sessions() must not be called per slot"),
    ):
        slot = _rehydrate_slot_from_history(state2, "chat-1-noscan")

    assert slot is not None
    # Title still comes through, from the metadata line we already read.
    assert slot.title == "Kept Title"
    assert slot._titled is True


def test_bulk_restore_emits_one_slots_broadcast(tmp_path, monkeypatch):
    """suspend_slots_push() coalesces the per-slot broadcasts into one.

    get_or_create_slot() broadcasts the FULL slot list every call, so restoring N
    tabs serialized 1+2+...+N slots — quadratic redaction work for intermediate
    states no client ever renders.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    for i in range(5):
        _seed_session(state, f"chat-{i}-bcast")
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": [f"chat-{i}-bcast" for i in range(5)], "ts": 0.0})
    )

    state2 = _make_state(tmp_path / "sessions")
    seen: list[str] = []
    state2._broadcast = lambda note: seen.append(note.get("_type"))  # type: ignore[method-assign]

    with state2.suspend_slots_push():
        restored = restore_open_slots(state2)

    assert restored == 5
    assert seen.count("slots") == 1, f"expected 1 coalesced slots push, got {seen.count('slots')}"


def test_suspend_slots_push_unwinds_and_flushes_on_exception(tmp_path, monkeypatch):
    """The suspend depth must unwind (and the owed push fire) even if the body raises.

    Otherwise one failed restore would leave the gateway permanently unable to
    broadcast slot updates.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    seen: list[str] = []
    state._broadcast = lambda note: seen.append(note.get("_type"))  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        with state.suspend_slots_push():
            state.push_slots_update()
            raise RuntimeError("boom")

    assert state._slots_push_suspend == 0
    assert seen.count("slots") == 1


def test_suspend_slots_push_nested_does_not_flush_early(tmp_path, monkeypatch):
    """Only the OUTERMOST suspend block flushes — nested users must not push early."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    seen: list[str] = []
    state._broadcast = lambda note: seen.append(note.get("_type"))  # type: ignore[method-assign]

    with state.suspend_slots_push():
        with state.suspend_slots_push():
            state.push_slots_update()
        assert seen.count("slots") == 0, "inner exit flushed early"
    assert seen.count("slots") == 1


def test_suspend_slots_push_no_push_means_no_broadcast(tmp_path, monkeypatch):
    """An empty suspend block must not synthesize a broadcast nobody asked for."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    seen: list[str] = []
    state._broadcast = lambda note: seen.append(note.get("_type"))  # type: ignore[method-assign]

    with state.suspend_slots_push():
        pass

    assert seen.count("slots") == 0


# ── The deferred restore must not let a flush truncate the snapshot ──
#
# start_flush_loop() is running (every 5s) BEFORE the startup restore. While the
# restore was synchronous it starved that timer, so a flush could never land
# mid-restore. Now that the restore yields per tab, one can — and
# _flush_dirty_slots calls _persist_open_slots, which would overwrite the very
# file being restored FROM with a half-populated slot set. Reproduced at real
# scale before the guard: 77 tabs collapsed to 70.


def test_flush_during_async_restore_does_not_truncate_snapshot(tmp_path, monkeypatch):
    """A flush landing mid-restore must not shrink open_slots.json."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = [f"chat-{i}-flush" for i in range(8)]
    for k in keys:
        _seed_session(state, k)
    snapshot = tmp_path / "open_slots.json"
    snapshot.write_text(json.dumps({"keys": keys, "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")

    # Fire the flush deterministically at the restore's own yield point — that is
    # exactly where the real 5s flush timer gets to run — rather than racing a
    # second task (which the scheduler may not interleave at all).
    real_sleep = asyncio.sleep
    observed: list[int] = []

    async def flushing_sleep(delay, *a, **kw):
        if delay == 0:
            state2._persist_open_slots()
            # Record what a crash at THIS instant would leave on disk.
            observed.append(len(json.loads(snapshot.read_text())["keys"]))
        return await real_sleep(delay, *a, **kw)

    with patch("kiro_crew.dashboard.chat_persistence.asyncio.sleep", side_effect=flushing_sleep):
        restored = asyncio.run(restore_open_slots_async(state2))

    assert restored == 8
    assert observed, "flush never landed mid-restore — test would not detect the bug"
    # Assert on the INTERMEDIATE states, not just the final one. Without the guard
    # the file transiently reads 1, 2, 3 … tabs; it only ends up complete because
    # the restore happens to finish. A kill in that window is what loses tabs.
    assert all(
        n == len(keys) for n in observed
    ), f"snapshot was truncated mid-restore: sizes {observed} (expected all {len(keys)})"
    assert set(json.loads(snapshot.read_text())["keys"]) == set(keys)


def test_restoring_flag_clears_and_reenables_persistence(tmp_path, monkeypatch):
    """The guard must be released after the restore so snapshots resume."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-flag")
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": ["chat-1-flag"], "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    assert state2.restoring_open_slots is False
    asyncio.run(restore_open_slots_async(state2))
    assert state2.restoring_open_slots is False

    # A normal flush after restore writes the live set again.
    state2.get_or_create_slot("chat-9-postrestore")
    state2._persist_open_slots()
    assert set(json.loads((tmp_path / "open_slots.json").read_text())["keys"]) == {
        "chat-1-flag",
        "chat-9-postrestore",
    }


def test_restoring_flag_cleared_even_if_restore_raises(tmp_path, monkeypatch):
    """A crash mid-restore must not leave open-tab persistence disabled forever."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-boom")
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": ["chat-1-boom"], "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    with patch(
        "kiro_crew.dashboard.chat_persistence._build_kiro_model_map",
        side_effect=RuntimeError("boom"),
    ):
        with pytest.raises(RuntimeError):
            asyncio.run(restore_open_slots_async(state2))

    assert state2.restoring_open_slots is False


# ── Startup must not READ on the event loop either ──
#
# The per-tab yield above bounded how long ONE tab could hold the loop, but the
# per-tab WORK still ran on it: get_metadata_status plus a chained
# read_messages_chained walk on a multi-megabyte transcript is seconds of loop
# time *between* yields, and the ``open_slots.json`` read plus the agent→model
# map glob were on-loop before the first yield ever arrived. So the reads now
# move into asyncio.to_thread and only the slot mutation stays on the loop --
# the same prefetch-then-apply split rehydrate_slot_from_history_async
# established, and for the same non-negotiable reason (slot construction
# broadcasts through asyncio.Queue.put_nowait / Event.set, neither thread-safe).


def _record_restore_threads(monkeypatch, state):
    """Instrument one state's restore so each half's thread is observable.

    Returns a dict of thread-name lists, filled in as the restore runs:

    * ``walk`` — ``read_messages_chained``, the expensive half. Must NEVER run on
      the loop thread; this is the property #895 is about.
    * ``prefetch`` — ``_prefetch_rehydrate_inputs``, the offloaded read bundle.
    * ``recheck`` — ``_deletion_during_read``, the post-hop deletion guard, which
      must run ON the loop *by design*: it gates the build and no suspension point
      may separate the two. So it is asserted separately rather than lumped in
      with the reads.
    * ``build`` — ``_rehydrate_slot_from_history``, loop-affine.
    * ``snapshot`` — the ``open_slots.json`` read.

    Wraps the REAL implementations so the restore still completes end to end and
    the assertions are about placement, not about a stub's behaviour.
    """
    from kiro_crew.dashboard import chat_persistence

    log = state.conversation_log
    assert log is not None
    seen: dict[str, list[str]] = {
        "walk": [],
        "prefetch": [],
        "recheck": [],
        "build": [],
        "snapshot": [],
    }

    real_chained = log.read_messages_chained
    real_prefetch = chat_persistence._prefetch_rehydrate_inputs
    real_recheck = chat_persistence._deletion_during_read
    real_build = chat_persistence._rehydrate_slot_from_history
    # The drivers read through the snapshot helper, which carries the "was the set
    # knowable" half of the answer with the keys. Probing the keys-only wrapper would
    # observe nothing and the off-loop assertion below would pass vacuously.
    real_keys = chat_persistence._read_open_slots_snapshot

    def _chained(key):
        seen["walk"].append(threading.current_thread().name)
        return real_chained(key)

    def _prefetch(*a, **kw):
        seen["prefetch"].append(threading.current_thread().name)
        return real_prefetch(*a, **kw)

    def _recheck(*a, **kw):
        seen["recheck"].append(threading.current_thread().name)
        return real_recheck(*a, **kw)

    def _build(*a, **kw):
        seen["build"].append(threading.current_thread().name)
        return real_build(*a, **kw)

    def _keys():
        seen["snapshot"].append(threading.current_thread().name)
        return real_keys()

    monkeypatch.setattr(log, "read_messages_chained", _chained)
    monkeypatch.setattr(chat_persistence, "_prefetch_rehydrate_inputs", _prefetch)
    monkeypatch.setattr(chat_persistence, "_deletion_during_read", _recheck)
    monkeypatch.setattr(chat_persistence, "_rehydrate_slot_from_history", _build)
    monkeypatch.setattr(chat_persistence, "_read_open_slots_snapshot", _keys)
    return seen


def test_restore_open_slots_async_reads_off_the_loop(tmp_path, monkeypatch):
    """Every per-tab disk read must run in a worker thread, not on the loop.

    This is the assertion the yield-only fix could not make: it passes only when
    the metadata read and the chained transcript walk have actually left the
    event-loop thread. Driving the synchronous generator (the previous shape)
    fails it on the first tab.

    The post-hop deletion re-check is deliberately excluded and asserted to be ON
    the loop instead — see the sibling test below.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = [f"chat-{i}-offloop" for i in range(3)]
    for k in keys:
        _seed_session(state, k)
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": keys, "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    seen = _record_restore_threads(monkeypatch, state2)

    main = threading.current_thread().name
    restored = asyncio.run(restore_open_slots_async(state2))

    assert restored == len(keys)
    assert set(state2._slots) == set(keys)
    assert seen["walk"], "no transcript was read — the test would not detect the bug"
    assert all(t != main for t in seen["walk"]), (
        "a transcript walk ran ON the event-loop thread; a large transcript "
        f"there stalls the stall-watchdog heartbeat (threads: {sorted(set(seen['walk']))})"
    )
    assert seen["prefetch"] and all(
        t != main for t in seen["prefetch"]
    ), f"the prefetch bundle ran on the loop (threads: {sorted(set(seen['prefetch']))})"
    assert seen["snapshot"] and all(
        t != main for t in seen["snapshot"]
    ), "open_slots.json was read on the event loop before the first yield"
    assert seen["build"] == [main] * len(keys), (
        "slot construction left the event-loop thread; it broadcasts through "
        "asyncio.Queue.put_nowait / Event.set, neither of which is thread-safe "
        f"(threads: {seen['build']})"
    )


def test_the_deletion_recheck_runs_on_the_loop_next_to_the_build(tmp_path, monkeypatch):
    """The guard must NOT be offloaded — that would reopen the window it closes.

    ``_deletion_during_read`` gates the build, so an await between the two would
    let a delete land in the gap. It is one mtime-cached metadata line, which is
    why paying for it on the loop is the right trade.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-gated")
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": ["chat-1-gated"], "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    seen = _record_restore_threads(monkeypatch, state2)

    main = threading.current_thread().name
    assert asyncio.run(restore_open_slots_async(state2)) == 1
    assert seen["recheck"] == [main], (
        "the deletion re-check must run on the loop, immediately before the "
        f"build it gates (threads: {seen['recheck']})"
    )
    assert seen["build"] == [main]


def test_restore_open_slots_sync_driver_still_reads_inline(tmp_path, monkeypatch):
    """The synchronous caller keeps its inline reads — no loop to offload to.

    Guards the split from being "fixed" by making the sync path spawn threads it
    has no reason to: with no running loop, ``_locked`` already takes the patient
    path and a thread hop would only add latency. It also must not pay for the
    post-hop deletion re-check, since its read has no suspension point to go
    stale across.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-inline")
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": ["chat-1-inline"], "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    seen = _record_restore_threads(monkeypatch, state2)

    main = threading.current_thread().name
    assert restore_open_slots(state2) == 1
    assert seen["walk"] and all(t == main for t in seen["walk"])
    assert seen["build"] == [main]
    assert seen["recheck"] == [], "the sync driver paid for a post-hop re-check it cannot need"


def test_async_restore_keeps_an_unreadable_tab_in_the_reopen_seed(tmp_path, monkeypatch):
    """The offloaded read must still distinguish "unreadable" from "gone".

    ``get_metadata`` reports ``{}`` for both, and treating the second as the
    first is what silently drops a live tab (the Windows sharing-violation
    shape). Moving the read into a worker thread must not lose the
    ``get_metadata_status`` readability flag that carries the difference.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = ["chat-1-ok", "chat-2-unreadable"]
    for k in keys:
        _seed_session(state, k)
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": keys, "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    log = state2.conversation_log
    real_status = log.get_metadata_status

    def _status(key):
        if key.endswith("unreadable"):
            return {}, False
        return real_status(key)

    monkeypatch.setattr(log, "get_metadata_status", _status)

    restored = asyncio.run(restore_open_slots_async(state2))

    assert restored == 1
    assert "chat-1-ok" in state2._slots
    assert "chat-2-unreadable" not in state2._slots
    assert (
        "chat-2-unreadable" in state2.unrestored_slot_keys
    ), "an unreadable tab was dropped from the reopen seed instead of carried"


def test_async_restore_does_not_carry_a_confidently_absent_tab(tmp_path, monkeypatch):
    """A readable-but-empty metadata read is a confident answer, not a retry.

    Companion to the test above: carrying every miss would resurrect keys for
    sessions that are already gone, forever.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-present")
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": ["chat-1-present", "chat-2-absent"], "ts": 0.0})
    )

    state2 = _make_state(tmp_path / "sessions")
    restored = asyncio.run(restore_open_slots_async(state2))

    assert restored == 1
    assert "chat-2-absent" not in state2.unrestored_slot_keys


def test_async_restore_rejects_a_path_separator_key(tmp_path, monkeypatch):
    """The path-traversal screen must survive the driver rewrite.

    ``open_slots.json`` is attacker-writable in the threat model the screen
    exists for, and the async driver does not share the generator's body — so
    the screen is asserted on the driver that startup actually runs.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-clean")
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": ["../../etc/passwd", "..\\..\\windows", "chat-1-clean"], "ts": 0.0})
    )

    state2 = _make_state(tmp_path / "sessions")
    restored = asyncio.run(restore_open_slots_async(state2))

    assert restored == 1
    assert set(state2._slots) == {"chat-1-clean"}


def test_async_restore_leaves_a_carried_seed_alone_when_there_is_no_snapshot(tmp_path, monkeypatch):
    """A missing snapshot is a no-op — including for ``unrestored_slot_keys``.

    The set is rebound per restore so a key that becomes readable stops being
    carried. Rebinding it when there is nothing to restore FROM would instead
    erase a seed the previous boot deliberately kept.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    carried = {"chat-7-carried"}
    state.unrestored_slot_keys = carried

    assert asyncio.run(restore_open_slots_async(state)) == 0
    assert state.unrestored_slot_keys is carried
    assert state.restoring_open_slots is False


# ── Deletion during the offloaded read ──
#
# ``ConversationLog.delete_session`` leaves NO tombstone — its own docstring
# notes that once the delete releases the lock "a concurrent writer can recreate
# the session". Offloading the transcript read opened a window where the user can
# permanently delete a session while restore holds its content, and the published
# slot would rewrite the deleted file on its next flush. The dashboard's HTTP
# listener is bound BEFORE startup restore runs (``_start_site`` precedes it in
# ``start_dashboard``), so this is reachable, not theoretical.
#
# The chat-resume handler already guards its own read this way; the restore paths
# mirror it rather than reinventing it. Raised as blocking by GPT 5.6 review.


def test_a_session_deleted_during_the_read_is_not_restored(tmp_path, monkeypatch):
    """A tab whose session vanished mid-read must not be rebuilt from stale bytes."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = ["chat-1-keep", "chat-2-deleted"]
    for k in keys:
        _seed_session(state, k)
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": keys, "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    log = state2.conversation_log
    real_chained = log.read_messages_chained

    def _delete_then_read(key):
        msgs = real_chained(key)
        if "chat-2-deleted" in key:
            # The user hits Delete while this transcript is in flight.
            log.delete_session(key)
        return msgs

    monkeypatch.setattr(log, "read_messages_chained", _delete_then_read)

    restored = asyncio.run(restore_open_slots_async(state2))

    assert restored == 1
    assert set(state2._slots) == {"chat-1-keep"}, (
        "restored a tab for a session the user permanently deleted; its next "
        "flush would rewrite the deleted transcript"
    )
    # A confident answer, not a retry — the key must not be carried forward.
    assert "chat-2-deleted" not in state2.unrestored_slot_keys


def test_a_session_recreated_during_the_read_is_not_overwritten(tmp_path, monkeypatch):
    """Delete-then-recreate leaves metadata PRESENT but belonging to a new chat.

    Existence alone reads that as "still here" and would publish a slot holding
    the OLD transcript, whose flush overwrites a conversation the user is actively
    using — worse than the plain-delete arm, because the destroyed data is live.
    ``created_at`` is the discriminator.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-swapped")
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": ["chat-1-swapped"], "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    log = state2.conversation_log
    real_chained = log.read_messages_chained

    def _swap_then_read(key):
        msgs = real_chained(key)
        if "swapped" in key:
            log.delete_session(key)
            log.append(key, "user", "a brand new conversation")
            log.update_metadata(key, {"created_at": "2099-01-01T00:00:00"})
        return msgs

    monkeypatch.setattr(log, "read_messages_chained", _swap_then_read)

    assert asyncio.run(restore_open_slots_async(state2)) == 0
    assert "chat-1-swapped" not in state2._slots
    # The replacement conversation is intact on disk.
    assert [
        m.get("content") for m in log.read_messages_chained(_history_key_for("chat-1-swapped"))
    ] == ["a brand new conversation"]


def test_an_unreadable_recheck_still_restores_the_tab(tmp_path, monkeypatch):
    """Refusing is the destructive direction on doubt, so unreadable != deleted.

    ``get_metadata`` returns ``{}`` for both "deleted" and "could not be read",
    and treating an unreadable line as a deletion would silently discard a LIVE
    tab — the Windows sharing-violation shape again. The guard must fall through
    to restoring.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-flaky")
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": ["chat-1-flaky"], "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    log = state2.conversation_log
    real_status = log.get_metadata_status
    calls = {"n": 0}

    def _status(key):
        calls["n"] += 1
        # First call is the prefetch (must succeed); the post-hop re-check reads
        # back unreadable.
        if calls["n"] > 1:
            return {}, False
        return real_status(key)

    monkeypatch.setattr(log, "get_metadata_status", _status)

    assert asyncio.run(restore_open_slots_async(state2)) == 1
    assert "chat-1-flaky" in state2._slots
    assert calls["n"] >= 2, "the post-hop re-check never ran"


def test_a_rewrite_that_preserves_created_at_is_not_refused(tmp_path, monkeypatch):
    """A compaction/rewrite carries ``created_at`` through, so it must not fire.

    Guards the identity arm from being a blanket "anything changed" refusal,
    which would drop tabs on ordinary housekeeping.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-rewritten")
    log0 = state.conversation_log
    key0 = _history_key_for("chat-1-rewritten")
    log0.update_metadata(key0, {"created_at": "2026-01-01T00:00:00"})
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": ["chat-1-rewritten"], "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    log = state2.conversation_log
    real_chained = log.read_messages_chained

    def _touch_then_read(key):
        msgs = real_chained(key)
        if "rewritten" in key:
            # Metadata churn that keeps identity — the rewrite case.
            log.update_metadata(key, {"title": "renamed by housekeeping"})
        return msgs

    monkeypatch.setattr(log, "read_messages_chained", _touch_then_read)

    assert asyncio.run(restore_open_slots_async(state2)) == 1
    assert "chat-1-rewritten" in state2._slots


def test_missing_created_at_falls_through_to_restoring(tmp_path, monkeypatch):
    """Pre-``created_at`` transcripts must stay restorable.

    Refusing when the discriminator is absent on either side would reject every
    session whose metadata predates the field — a visible break for real users —
    to close a narrow race. The residual (an undetected recreate of such a
    transcript) is accepted and documented; the durable fix is a tombstone in
    ``history.delete_session``, which is out of scope.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-legacy")
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": ["chat-1-legacy"], "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    log = state2.conversation_log
    key = _history_key_for("chat-1-legacy")
    from kiro_crew.dashboard import chat_persistence as cp

    # The session is PRESENT (so the absence arm cannot fire) and the pre-read
    # metadata carries no ``created_at`` — the legacy-transcript shape. The
    # identity arm needs two DIFFERING stamps, so one missing side falls through.
    assert cp._deletion_during_read(log, key, {"title": "legacy"}, []) is None
    # And with both sides present but equal, it also falls through.
    live_meta = log.get_metadata(key)
    assert live_meta.get("created_at"), "fixture no longer stamps created_at"
    assert cp._deletion_during_read(log, key, live_meta, []) is None

    assert asyncio.run(restore_open_slots_async(state2)) == 1
    assert "chat-1-legacy" in state2._slots


def test_a_never_persisted_key_is_not_treated_as_a_deletion(tmp_path, monkeypatch):
    """An absent key is a new conversation, not something that was deleted."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    from kiro_crew.dashboard import chat_persistence as cp

    assert (
        cp._deletion_during_read(state.conversation_log, "dashboard:never-existed", {}, None)
        is None
    )


def test_a_tab_closed_during_the_open_slot_read_is_not_restored(tmp_path, monkeypatch):
    """The open-tab driver needs the SAME tombstone re-check as its siblings.

    ``rehydrate_slot_from_history_async`` and the recent-sessions driver both
    consult the close tombstone after their thread hop; this driver must too.
    The close pops
    the slot and records the tombstone synchronously, but persists the ``closed``
    flag only after its own awaits — so the metadata read mid-flight still says
    open. Restoring from it re-creates a dismissed tab, and worse, the restored
    slot's next flush writes metadata WITHOUT ``closed``, erasing the close.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = ["chat-1-stays", "chat-2-dismissed"]
    for k in keys:
        _seed_session(state, k)
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": keys, "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    log = state2.conversation_log
    real_chained = log.read_messages_chained

    from kiro_crew.dashboard import channel_slots

    def _close_then_read(key):
        if "chat-2-dismissed" in key:
            # The user clicks ✕ while this transcript is in flight.
            channel_slots.note_slot_closed(state2, "chat-2-dismissed")
        return real_chained(key)

    monkeypatch.setattr(log, "read_messages_chained", _close_then_read)

    restored = asyncio.run(restore_open_slots_async(state2))

    assert restored == 1
    assert set(state2._slots) == {"chat-1-stays"}, (
        "restored a tab the user dismissed mid-read; its flush would also clear "
        "the closed marker"
    )
    # A confident answer, not a retry — the key must not be carried forward.
    assert "chat-2-dismissed" not in state2.unrestored_slot_keys


def test_an_older_close_does_not_block_an_open_slot_restore(tmp_path, monkeypatch):
    """Negative control: only a close DURING the read is the race.

    Without the ``>= started`` comparison the guard would make a reopened tab
    un-restorable for the tombstone's whole lifetime.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-reopened")
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": ["chat-1-reopened"], "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    from kiro_crew.dashboard import channel_slots

    # The close must land STRICTLY before the driver's own ``started =
    # time.time()``, and a real sleep does not guarantee that: on Windows under
    # CPython <= 3.12 (what CI pins) ``time.sleep`` waits on a high-resolution
    # timer while ``time.time`` still steps in ~15.6 ms system-clock ticks, so a
    # 10 ms sleep can leave both readings EQUAL — and ``slot_closed_since`` is
    # inclusive (``when >= instant``), so the tombstone would block the reopen
    # and this negative control would accuse the guard of the very defect it
    # exists to disprove. Stamp an explicitly older instant instead, making
    # eligibility arithmetic on every platform. 60 s is far inside
    # ``_CLOSE_TOMBSTONE_TTL_SECS`` (3600 s), so the tombstone still EXISTS when
    # the guard consults it and the assertion cannot pass vacuously.
    # The clock goes on ``channel_slots`` alone, for this one call, so the
    # restore below reads real time. A whole second survives the clock's
    # nanosecond storage exactly, so the returned instant compares equal.
    closed_at = float(int(time.time()) - 60)
    with monkeypatch.context() as mp:
        ManualClock(start=closed_at).install(mp, channel_slots)
        assert channel_slots.note_slot_closed(state2, "chat-1-reopened") == closed_at  # reopened

    assert asyncio.run(restore_open_slots_async(state2)) == 1
    assert "chat-1-reopened" in state2._slots
