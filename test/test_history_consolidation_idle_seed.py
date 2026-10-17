"""The idle sweep must find sessions left unconsolidated before a restart.

``HistoryConsolidator._last_activity`` is in memory only and was written only by
``maybe_consolidate``. After a gateway restart, a session with an unconsolidated
tail that nobody touched again was never in that dict, so ``check_idle_sessions``
never looked at it and its tail was never consolidated. The first sweep now seeds
the dict once, off the event loop, from the transcripts on disk.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import threading
import time
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import history as history_mod
from kiro_crew.history import ConversationLog, HistoryConsolidator
from kiro_crew.session_map import SessionMap

STEM = "dashboard_chat-left-behind"
LIVE = "dashboard:chat-left-behind"


def _restarted(
    tmp_path,
    *,
    unconsolidated: bool,
    memory_mode: str = "",
    key: str = "dashboard:chat-left-behind",
    session_map: SessionMap | None = None,
    with_sessions: bool = True,
    age_days: float = 2,
) -> tuple[ConversationLog, HistoryConsolidator]:
    """A transcript written by an earlier process, read by a fresh consolidator."""
    writer = ConversationLog(base_dir=tmp_path / "sessions")
    writer.init()
    with history_mod.allow_on_loop_persist():
        for i in range(3):
            writer.append(key, "user", f"m{i}")
        if not unconsolidated:
            writer.mark_consolidated(key, 3)
        if memory_mode:
            writer.update_metadata(key, {"memory_mode": memory_mode})
    old = time.time() - age_days * 86400
    os.utime(writer._path(key), (old, old))
    log = ConversationLog(base_dir=tmp_path / "sessions")
    memory = MagicMock()
    memory.read_preferences.return_value = ""
    memory.read_projects.return_value = ""
    # The gateway's SessionManager owns the durable privacy map the seed consults.
    sessions = SimpleNamespace(_session_map=session_map or SessionMap()) if with_sessions else None
    return log, HistoryConsolidator(
        log=log, memory=memory, sessions=sessions, migrated=True, history_idle_secs=3600
    )


async def _sweep_after_seed(consolidator: HistoryConsolidator) -> list[str]:
    started: list[str] = []

    async def fake_consolidate(key: str, include_history: bool = True) -> None:
        started.append(key)
        consolidator._running.discard(key)

    with patch.object(consolidator, "_consolidate", AsyncMock(side_effect=fake_consolidate)):
        consolidator.check_idle_sessions()  # starts the one-time seed
        seed = getattr(consolidator, "_seed_task", None)
        if seed is not None:
            await seed
        consolidator.check_idle_sessions()
        await asyncio.gather(*list(consolidator._tasks), return_exceptions=True)
    return started


@pytest.mark.asyncio
async def test_untouched_unconsolidated_session_is_picked_up_after_restart(tmp_path):
    _log, consolidator = _restarted(tmp_path, unconsolidated=True)

    started = await _sweep_after_seed(consolidator)

    # Under the LIVE session key, never the filename stem: receipts are keyed on it.
    assert started == [LIVE]
    # Seeded from the file's mtime, not "now": the session is already idle.
    assert consolidator._last_activity[LIVE] < time.time() - 86400


@pytest.mark.asyncio
async def test_fully_consolidated_session_is_not_seeded(tmp_path):
    _log, consolidator = _restarted(tmp_path, unconsolidated=False)

    consolidator.check_idle_sessions()
    assert consolidator._seed_task is not None
    await consolidator._seed_task

    assert consolidator._last_activity == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_private_transcript_is_not_seeded(tmp_path, mode):
    """``_consolidate`` refuses it, and a refusal sets no throttle: seeding it
    would re-dispatch a refused task on every sweep."""
    _log, consolidator = _restarted(tmp_path, unconsolidated=True, memory_mode=mode)

    assert await _sweep_after_seed(consolidator) == []
    assert consolidator._last_activity == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_channel_private_only_in_the_session_map_is_not_seeded(tmp_path, mode):
    """The header stamp failed, so the transcript reads as persistent; the
    session map still holds the thread's privacy, and it wins."""
    key = "slack:1720000000.000001"
    session_map = SessionMap()
    session_map.set_flag(key, mode, True)
    _log, consolidator = _restarted(tmp_path, unconsolidated=True, key=key, session_map=session_map)

    assert await _sweep_after_seed(consolidator) == []
    assert consolidator._last_activity == {}


@pytest.mark.asyncio
async def test_a_seeded_backlog_drains_a_few_per_sweep_without_starving(tmp_path):
    """Many stale seeds must not all start in the first sweep after a restart."""
    from kiro_crew.history_consolidation import _SEEDED_PER_SWEEP

    _log, consolidator = _restarted(tmp_path, unconsolidated=True)
    consolidator._activity_seeded = True
    stems = [f"seed-{i}" for i in range(_SEEDED_PER_SWEEP * 2)]
    for stem in stems:
        consolidator._last_activity[stem] = 0.0
        consolidator._seeded_keys.add(stem)
    started: list[str] = []

    def counts(key: str) -> tuple[int, int]:
        return (3, 3)

    async def fake_consolidate(key: str, include_history: bool = True) -> None:
        started.append(key)
        consolidator._running.discard(key)

    with (
        patch.object(consolidator._log, "consolidation_counts", side_effect=counts) as read,
        patch.object(consolidator, "retry_eligible", return_value=True) as eligible,
        patch.object(consolidator, "_consolidate", AsyncMock(side_effect=fake_consolidate)),
    ):
        consolidator.check_idle_sessions()
        await asyncio.gather(*list(consolidator._tasks), return_exceptions=True)
        assert started == stems[:_SEEDED_PER_SWEEP]
        consolidator._history_consolidated.clear()
        consolidator.check_idle_sessions()
        await asyncio.gather(*list(consolidator._tasks), return_exceptions=True)

    assert started == stems
    # A seed's (possibly cold, large) transcript is never read on the event loop:
    # ``_consolidate`` snapshots it off the loop and enforces the backoff itself.
    read.assert_not_called()
    eligible.assert_not_called()


@pytest.mark.asyncio
async def test_a_transcript_older_than_the_backfill_window_is_not_seeded(tmp_path):
    _log, consolidator = _restarted(tmp_path, unconsolidated=True, age_days=8)

    assert await _sweep_after_seed(consolidator) == []
    assert consolidator._last_activity == {}


@pytest.mark.asyncio
async def test_a_seed_with_nothing_left_is_dropped(tmp_path):
    """A seeded pass that finds an empty tail stops tracking the seed."""
    _log, consolidator = _restarted(tmp_path, unconsolidated=False)
    consolidator._last_activity[LIVE] = 0.0
    consolidator._seeded_keys.add(LIVE)

    await consolidator._consolidate(LIVE, include_history=True)

    assert LIVE not in consolidator._seeded_keys
    assert LIVE not in consolidator._last_activity


@pytest.mark.asyncio
async def test_without_a_session_map_nothing_is_seeded(tmp_path):
    """No durable privacy record to check against: fail closed."""
    _log, consolidator = _restarted(tmp_path, unconsolidated=True, with_sessions=False)

    assert await _sweep_after_seed(consolidator) == []


@pytest.mark.asyncio
async def test_session_end_does_not_start_a_second_run_under_the_live_key(tmp_path):
    """A seeded stem already running blocks the same transcript's live key."""
    _log, consolidator = _restarted(tmp_path, unconsolidated=True)
    consolidator._running.add(STEM)

    with patch.object(consolidator, "_consolidate", AsyncMock()) as run:
        consolidator.consolidate_session("dashboard:chat-left-behind")
        consolidator.maybe_consolidate("dashboard:chat-left-behind")

    run.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_slack_file_and_canonical_key_share_one_run(tmp_path):
    """``slack:<ts>`` still reads a pre-migration ``<ts>.jsonl``: same transcript."""
    log, consolidator = _restarted(tmp_path, unconsolidated=True)
    (tmp_path / "sessions" / "1720000000.000001.jsonl").write_text("{}\n", encoding="utf-8")
    consolidator._running.add("1720000000.000001")

    assert consolidator._busy("slack:1720000000.000001")
    assert not consolidator._busy("slack:1720000000.000002")


@pytest.mark.asyncio
async def test_a_refused_seed_waits_an_idle_window_and_is_kept(tmp_path):
    """A refused seed (private after the scan, or inside its retry backoff) is not
    re-sent every sweep, and is not forgotten: a backoff that expires is retried."""
    from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED

    _log, consolidator = _restarted(tmp_path, unconsolidated=True)
    consolidator._activity_seeded = True
    consolidator._last_activity[LIVE] = 0.0
    consolidator._seeded_keys.add(LIVE)

    async def refuse(key: str, include_history: bool = True) -> object:
        consolidator._running.discard(key)  # as the real ``finally`` does
        return _CONSOLIDATION_REFUSED

    refused = AsyncMock(side_effect=refuse)
    with patch.object(consolidator, "_consolidate", refused):
        consolidator.check_idle_sessions()
        await asyncio.gather(*list(consolidator._tasks), return_exceptions=True)
        consolidator.check_idle_sessions()  # same idle window: throttled
        await asyncio.gather(*list(consolidator._tasks), return_exceptions=True)
        assert refused.await_count == 1
        consolidator._history_consolidated[LIVE] = 0.0  # the idle window has passed
        consolidator.check_idle_sessions()
        await asyncio.gather(*list(consolidator._tasks), return_exceptions=True)

    assert refused.await_count == 2
    assert LIVE in consolidator._last_activity and LIVE in consolidator._seeded_keys


@pytest.mark.asyncio
async def test_a_seed_that_speaks_again_is_no_longer_a_seed(tmp_path):
    """Live activity takes the key out of the seeded budget and refusal rules."""
    _log, consolidator = _restarted(tmp_path, unconsolidated=True)
    consolidator._activity_seeded = True
    consolidator._last_activity[LIVE] = 0.0
    consolidator._seeded_keys.add(LIVE)

    consolidator.maybe_consolidate(LIVE)

    assert list(consolidator._last_activity) == [LIVE]
    assert LIVE not in consolidator._seeded_keys


@pytest.mark.asyncio
async def test_a_seeded_session_looks_up_the_receipt_committed_before_restart(
    tmp_path, monkeypatch
):
    """A V2 member-memory pass commits a receipt keyed on the session key, then
    the gateway stops before the transcript is marked. After the restart the
    seeded pass must ask for THAT receipt, so it finds it and appends nothing."""
    from kiro_crew.context import ContextBuilder
    from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

    class _Stop(Exception):
        pass

    execution = ExecutionContext(
        "member-id", MemoryStoreRef("member-store", "member-id"), "member", "kirocrew", "persistent"
    )
    monkeypatch.setattr("kiro_crew.execution_context.read_session_execution", lambda _k: execution)
    monkeypatch.setattr("kiro_crew.memory_stores.memory_store_version", lambda _store: 2)
    asked: list[str] = []
    vectors = MagicMock()
    vectors.algorithm_version = "v2"
    vectors.consolidation_receipt.side_effect = lambda source_id: asked.append(source_id) or (
        _ for _ in ()
    ).throw(_Stop())
    monkeypatch.setattr(ContextBuilder, "ensure_store", AsyncMock(return_value=vectors))

    log, before = _restarted(tmp_path, unconsolidated=True)
    monkeypatch.setattr(ContextBuilder, "get_memory_for", lambda **_kwargs: before._memory)
    # Each pass stops at the receipt lookup (the crash point for the first one). The
    # stop would arm an environment backoff that refuses the second pass before its
    # lookup; keep it out so both passes are judged on the receipt alone.
    monkeypatch.setattr(
        ConversationLog, "record_consolidation_environment_failure", lambda *_a, **_k: (0, 0.0)
    )
    with pytest.raises(_Stop):
        await before._consolidate(LIVE, include_history=True)  # the pre-restart live pass

    after = HistoryConsolidator(
        log=ConversationLog(base_dir=tmp_path / "sessions"),
        memory=before._memory,
        sessions=SimpleNamespace(_session_map=SessionMap()),
        migrated=True,
        history_idle_secs=3600,
    )
    after.check_idle_sessions()
    assert after._seed_task is not None
    await after._seed_task
    (seeded,) = after._seeded_keys
    with pytest.raises(_Stop):
        await after._consolidate(seeded, include_history=True)

    assert len(asked) == 2 and asked[0] == asked[1]


# Sessions that had a turn in this process. When a sweep's read finds nothing left to
# consolidate, it remembers the transcript's stamp and skips the key until the stamp
# moves, so a finished session costs a stat per sweep, not a transcript read, and a
# row from any writer still reaches the next sweep.


def _live(
    tmp_path, keys: list[str], *, consolidated: bool
) -> tuple[ConversationLog, HistoryConsolidator]:
    """Sessions with a turn in this process, idle since, each fully consolidated or not."""
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.init()
    with history_mod.allow_on_loop_persist():
        for key in keys:
            for i in range(3):
                log.append(key, "user", f"m{i}")
            if consolidated:
                log.mark_consolidated(key, 3)
    memory = MagicMock()
    memory.read_preferences.return_value = ""
    memory.read_projects.return_value = ""
    consolidator = HistoryConsolidator(
        log=log, memory=memory, migrated=True, history_idle_secs=3600
    )
    consolidator._activity_seeded = True  # the restart seed is covered above
    for key in keys:
        consolidator.maybe_consolidate(key)  # the session's turn
        consolidator._last_activity[key] = 0.0  # idle ever since
    return log, consolidator


async def _sweep(
    consolidator: HistoryConsolidator,
    log: ConversationLog,
    *,
    after_read: Callable[[str], None] | None = None,
) -> tuple[list[str], AsyncMock]:
    """One idle sweep with ``_consolidate`` stubbed; returns the keys whose transcript it read.

    The sweep's read is the off-loop count (``_count_rows``, in a worker thread). A
    turn's own count still in flight defers the idle count to the next sweep, so the
    counts ``_live``'s turns started are drained first. *after_read* runs right after
    each read, in the worker thread, as a writer in another thread would.
    """
    await asyncio.wait_for(
        asyncio.gather(*list(consolidator._tasks), return_exceptions=True), timeout=30
    )
    reads: list[str] = []
    counts = consolidator._count_rows

    def counting(key: str, floor: int | None) -> object:
        reads.append(key)
        found = counts(key, floor)
        if after_read is not None:
            after_read(key)
        return found

    fake = AsyncMock(return_value=None)
    with (
        patch.object(consolidator, "_count_rows", counting),
        patch.object(consolidator, "_consolidate", fake),
    ):
        consolidator.check_idle_sessions()
        await asyncio.wait_for(
            asyncio.gather(*list(consolidator._tasks), return_exceptions=True), timeout=30
        )
    return reads, fake


@pytest.mark.asyncio
async def test_a_consolidated_idle_session_is_read_once_then_left_alone(tmp_path):
    keys = [f"dashboard:chat-{i}" for i in range(3)]
    log, consolidator = _live(tmp_path, keys, consolidated=True)

    first, fake = await _sweep(consolidator, log)
    second, _ = await _sweep(consolidator, log)

    assert sorted(first) == keys  # one read each finds nothing left to consolidate
    assert second == [], f"idle sessions with nothing left are reread on every sweep: {second}"
    fake.assert_not_awaited()


def _write_row(log: ConversationLog, key: str, writer: str) -> None:
    """Add one row to *key*'s transcript the way each kind of writer does."""
    if writer == "other_process":  # no ConversationLog call, so no cache invalidation either
        with open(log._path(key), "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"role": "user", "content": "from another process"}) + "\n")
        return
    with history_mod.allow_on_loop_persist():
        log.append(key, "user", "a later row")


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["turn", "turn_that_skips_maybe_consolidate", "other_process"])
async def test_a_row_written_after_a_clean_read_is_swept(tmp_path, writer):
    """Monitor-wake, recovery and cancelled turns append rows without ``maybe_consolidate``."""
    key = "dashboard:chat-later"
    log, consolidator = _live(tmp_path, [key], consolidated=True)
    await _sweep(consolidator, log)  # finds nothing left

    _write_row(log, key, writer)
    if writer == "turn":
        consolidator.maybe_consolidate(key)
        consolidator._last_activity[key] = 0.0  # idle again

    reads, fake = await _sweep(consolidator, log)

    assert reads == [key]
    fake.assert_awaited_once_with(key, include_history=True)


@pytest.mark.asyncio
async def test_a_row_that_lands_during_the_read_is_swept_next(tmp_path):
    """The stamp is taken before the read, so a row that lands after a clean count shows."""
    key = "dashboard:chat-racing"
    log, consolidator = _live(tmp_path, [key], consolidated=True)
    await _sweep(
        consolidator, log, after_read=lambda racing: _write_row(log, racing, "other_process")
    )

    reads, fake = await _sweep(consolidator, log)

    assert reads == [key]
    fake.assert_awaited_once_with(key, include_history=True)


@pytest.mark.asyncio
async def test_a_marker_reset_that_keeps_size_and_mtime_is_swept(tmp_path):
    """``mark_consolidated`` rewrites the metadata line and restores the mtime, so a stamp
    of size and mtime alone cannot see the rows it reopens."""
    key = "dashboard:chat-reset"
    log, consolidator = _live(tmp_path, [key], consolidated=True)
    os.utime(log._path(key), (1_700_000_000, 1_700_000_000))  # a whole second restores exactly
    await _sweep(consolidator, log)
    before = log._path(key).stat()

    with history_mod.allow_on_loop_persist():
        log.mark_consolidated(key, 0)  # all three rows are unconsolidated again
    after = log._path(key).stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)

    reads, fake = await _sweep(consolidator, log)

    assert reads == [key]
    fake.assert_awaited_once_with(key, include_history=True)


@pytest.mark.asyncio
async def test_a_cache_invalidation_makes_the_next_sweep_read(tmp_path):
    """A writer that invalidates a key's caches changed its file in a way the stat may not
    show (a same-length rewrite inside one clock tick), so the stamp carries the generation."""
    key = "dashboard:chat-invalidated"
    log, consolidator = _live(tmp_path, [key], consolidated=True)
    await _sweep(consolidator, log)

    log._invalidate_cache(key)
    reads, _ = await _sweep(consolidator, log)

    assert reads == [key]


@pytest.mark.asyncio
async def test_an_idle_session_with_rows_left_stays_tracked(tmp_path):
    key = "dashboard:chat-pending"
    log, consolidator = _live(tmp_path, [key], consolidated=False)

    first, fake = await _sweep(consolidator, log)  # the stub leaves its rows unconsolidated
    second, _ = await _sweep(consolidator, log)  # inside the pass's idle window: not counted
    fake.assert_awaited_once_with(key, include_history=True)
    assert first == [key] and second == []
    assert key in consolidator._last_activity and key not in consolidator._idle_clean

    consolidator._running.discard(key)  # the real ``_consolidate`` releases it; the stub does not
    consolidator._history_consolidated[key] = 0.0  # the idle window has passed
    third, again = await _sweep(consolidator, log)

    assert third == [key]  # rows left: never stamped clean, so counted again
    again.assert_awaited_once_with(key, include_history=True)


_IDLE_RECORDS = ("_last_activity", "_seeded_keys", "_history_consolidated", "_idle_clean")


def _delete(log: ConversationLog, key: str) -> None:
    """Delete *key*'s session, as the dashboard does."""
    with history_mod.allow_on_loop_persist():
        assert log.delete_session(key)


@pytest.mark.asyncio
async def test_a_deleted_session_leaves_no_idle_record(tmp_path):
    """Deleting a session removes its transcript, and the next sweep forgets every record the
    consolidator holds for it, so the stamp of a finished session does not outlive the session."""
    gone, kept = "dashboard:chat-gone", "dashboard:chat-kept"
    log, consolidator = _live(tmp_path, [gone, kept], consolidated=True)
    await _sweep(consolidator, log)  # both are read once and stamped
    for key in (gone, kept):
        consolidator._history_consolidated[key] = time.time()  # a pass has run for it
    assert gone in consolidator._idle_clean and kept in consolidator._idle_clean

    _delete(log, gone)
    reads, _ = await _sweep(consolidator, log)

    assert gone not in reads
    for name in _IDLE_RECORDS:
        assert gone not in getattr(consolidator, name), name
    assert kept in consolidator._last_activity and kept in consolidator._idle_clean


@pytest.mark.asyncio
async def test_a_deleted_session_is_forgotten_once_its_pass_has_ended(tmp_path):
    """A pass still running for the session records its throttle when it ends, so the records
    go after the pass, not under it."""
    key = "dashboard:chat-running"
    log, consolidator = _live(tmp_path, [key], consolidated=True)
    await _sweep(consolidator, log)
    consolidator._running.add(key)  # a pass is in flight

    _delete(log, key)
    await _sweep(consolidator, log)
    assert key in consolidator._last_activity and key in consolidator._idle_clean

    consolidator._running.discard(key)  # the pass ends and writes its throttle
    consolidator._history_consolidated[key] = time.time()
    await _sweep(consolidator, log)

    for name in _IDLE_RECORDS:
        assert key not in getattr(consolidator, name), name


@pytest.mark.asyncio
async def test_a_deleted_session_is_forgotten_once_its_count_has_ended(tmp_path):
    """An off-loop count still running for the session records its clean stamp when it ends,
    so the records go after the count, never before it: a forget under the count would be
    undone by the count's own write and leave a stamp for a key no sweep visits again."""
    key = "dashboard:chat-counting"
    log, consolidator = _live(tmp_path, [key], consolidated=True)
    await asyncio.wait_for(
        asyncio.gather(*list(consolidator._tasks), return_exceptions=True), timeout=30
    )
    entered, release = threading.Event(), threading.Event()
    counts = consolidator._count_rows

    def held(k: str, floor: int | None) -> object:
        found = counts(k, floor)  # the thread has read the transcript ...
        entered.set()
        assert release.wait(timeout=30)  # ... and returns only after the delete
        return found

    with patch.object(consolidator, "_count_rows", held):
        consolidator.check_idle_sessions()  # starts the idle count, held in its thread
        try:
            assert await asyncio.to_thread(entered.wait, 30)
            stamp = consolidator._transcript_stamp(key)
            _delete(log, key)
            consolidator.check_idle_sessions()  # the transcript is gone, the count still runs
            assert key in consolidator._last_activity
        finally:
            release.set()
        await asyncio.wait_for(
            asyncio.gather(*list(consolidator._tasks), return_exceptions=True), timeout=30
        )
    assert consolidator._idle_clean.get(key) == stamp  # the count recorded its result

    await _sweep(consolidator, log)

    for name in _IDLE_RECORDS:
        assert key not in getattr(consolidator, name), name


@pytest.mark.asyncio
async def test_a_transcript_that_cannot_be_statted_is_not_a_deleted_one(tmp_path):
    """Only an absent file means the session is gone. A stat that fails otherwise (permissions,
    a stale mount) keeps the key tracked and falls back to reading it."""
    key = "dashboard:chat-denied"
    log, consolidator = _live(tmp_path, [key], consolidated=True)
    await _sweep(consolidator, log)
    assert key in consolidator._idle_clean

    def denied(stat_result: os.stat_result) -> object:
        # The stamp's stat fails inside ``_transcript_stamp``; the off-loop count still
        # reads the file, so only the stamp is denied here.
        raise PermissionError(errno.EACCES, "Permission denied")

    with patch.object(log, "_cache_identity", denied):
        reads, _ = await _sweep(consolidator, log)

    assert reads == [key]  # read: not skipped on the old stamp, not forgotten
    assert key in consolidator._last_activity


@pytest.mark.asyncio
async def test_a_count_that_reads_nothing_leaves_no_clean_stamp(tmp_path):
    """Only a count that found nothing left stamps the key. A count that failed (or was
    refused by the retry deadline) proved nothing, so the next sweep counts again."""
    key = "dashboard:chat-unread"
    log, consolidator = _live(tmp_path, [key], consolidated=True)
    await asyncio.wait_for(
        asyncio.gather(*list(consolidator._tasks), return_exceptions=True), timeout=30
    )

    with patch.object(consolidator, "_count_rows", side_effect=OSError(errno.EIO, "I/O error")):
        consolidator.check_idle_sessions()
        await asyncio.wait_for(
            asyncio.gather(*list(consolidator._tasks), return_exceptions=True), timeout=30
        )
    assert key not in consolidator._idle_clean

    reads, _ = await _sweep(consolidator, log)

    assert reads == [key]
    assert key in consolidator._idle_clean  # this count found nothing left


@pytest.mark.asyncio
async def test_a_seed_with_nothing_left_still_goes_to_consolidate(tmp_path):
    log, consolidator = _restarted(tmp_path, unconsolidated=False)
    consolidator._activity_seeded = True
    consolidator._last_activity[LIVE] = 0.0
    consolidator._seeded_keys.add(LIVE)

    reads, fake = await _sweep(consolidator, log)

    assert reads == []  # a seed is never read on the loop
    fake.assert_awaited_once_with(LIVE, include_history=True)
    assert LIVE in consolidator._last_activity  # only ``_consolidate`` drops a finished seed
