"""The idle sweep must find sessions left unconsolidated before a restart.

``HistoryConsolidator._last_activity`` is in memory only and was written only by
``maybe_consolidate``. After a gateway restart, a session with an unconsolidated
tail that nobody touched again was never in that dict, so ``check_idle_sessions``
never looked at it and its tail was never consolidated. The first sweep now seeds
the dict once, off the event loop, from the transcripts on disk.
"""

from __future__ import annotations

import asyncio
import os
import time
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
