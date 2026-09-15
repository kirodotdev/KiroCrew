"""Privacy enforcement at the consolidation choke point.

The relay mirrors a remote turn's frames through ordinary local ``slot.append``
calls, which lands peer-authored conversation in local history. Incognito and
temporary transcripts are also present on disk but are never summarizable. The
single guard in ``_consolidate`` classifies both markers before the transcript
snapshot, fences a private transcript's rows behind ``last_consolidated`` so
they stay out of memory after the slot-owned marker clears, and leaves
unreadable metadata pending for a later retry.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import history as history_mod
from kiro_crew.history import ConversationLog, HistoryConsolidator
from kiro_crew.history_consolidation import _TranscriptPrivacy

KEY = "dashboard:peer-mirror"


def _seed_log(tmp_path, key: str = KEY, count: int = 3) -> ConversationLog:
    """A real transcript with *count* unconsolidated messages."""
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.init()
    _append_rows(log, key, count, prefix="m")
    return log


def _append_rows(log: ConversationLog, key: str, count: int, *, prefix: str) -> None:
    with history_mod.allow_on_loop_persist():
        for i in range(count):
            log.append(key, "user", f"{prefix}{i}")


def _make_consolidator(log: Any, **kw: Any) -> HistoryConsolidator:
    memory = MagicMock()
    memory.read_preferences.return_value = ""
    memory.read_projects.return_value = ""
    kw.setdefault("history_idle_secs", 0)
    kw.setdefault("sessions", None)
    return HistoryConsolidator(log=log, memory=memory, migrated=True, **kw)


def _mark_metadata(log: ConversationLog, key: str = KEY, **values: object) -> None:
    with history_mod.allow_on_loop_persist():
        log.update_metadata(key, values)


def _drop_metadata_keys(log: ConversationLog, *keys: str, key: str = KEY) -> None:
    """Rewrite the header WITHOUT *keys*, the way a slot save retracts an owned key.

    The dashboard slot save rebuilds the metadata line from slot state and
    writes ``executor`` only while the slot is bound to a remote peer, so an
    unbind clears the marker by omission rather than by writing a value.
    """
    with history_mod.allow_on_loop_persist(), log._locked(key):
        path = log._path(key)
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        meta = json.loads(lines[0])
        for name in keys:
            meta.pop(name, None)
        lines[0] = json.dumps(meta) + "\n"
        path.write_text("".join(lines), encoding="utf-8")
        log._invalidate_cache(key)


def _make_private(log: ConversationLog, transition: str) -> None:
    if transition == "executor":
        _mark_metadata(log, executor="remote")
    else:
        _mark_metadata(log, memory_mode="incognito")


def _make_local_again(log: ConversationLog, transition: str) -> None:
    if transition == "executor":
        _drop_metadata_keys(log, "executor")
    else:
        _mark_metadata(log, memory_mode="persistent")


class TestTranscriptPrivacy:
    """One metadata read produces local, private, or retryable unknown."""

    def test_an_explicit_remote_executor_is_private(self, tmp_path):
        log = _seed_log(tmp_path)
        _mark_metadata(log, executor="remote")
        assert _make_consolidator(log)._transcript_privacy(KEY) is _TranscriptPrivacy.PRIVATE

    def test_absent_privacy_markers_read_as_local(self, tmp_path):
        log = _seed_log(tmp_path)
        assert _make_consolidator(log)._transcript_privacy(KEY) is _TranscriptPrivacy.LOCAL

    @pytest.mark.parametrize("value", ["", "local", "acp", "unknown-future-value"])
    def test_other_executor_values_read_as_local(self, tmp_path, value):
        log = _seed_log(tmp_path)
        _mark_metadata(log, executor=value)
        assert _make_consolidator(log)._transcript_privacy(KEY) is _TranscriptPrivacy.LOCAL

    @pytest.mark.parametrize("value", ["REMOTE", "Remote", "rEmOtE"])
    def test_remote_executor_comparison_is_case_insensitive(self, tmp_path, value):
        log = _seed_log(tmp_path)
        _mark_metadata(log, executor=value)
        assert _make_consolidator(log)._transcript_privacy(KEY) is _TranscriptPrivacy.PRIVATE

    @pytest.mark.parametrize("value", ["incognito", "temporary", "INCOGNITO", "Temporary"])
    def test_restricted_memory_modes_are_private_case_insensitively(self, tmp_path, value):
        log = _seed_log(tmp_path)
        _mark_metadata(log, memory_mode=value)
        assert _make_consolidator(log)._transcript_privacy(KEY) is _TranscriptPrivacy.PRIVATE

    @pytest.mark.parametrize("value", ["", "persistent", "unknown-future-value"])
    def test_other_memory_modes_read_as_local(self, tmp_path, value):
        log = _seed_log(tmp_path)
        _mark_metadata(log, memory_mode=value)
        assert _make_consolidator(log)._transcript_privacy(KEY) is _TranscriptPrivacy.LOCAL

    def test_an_unreadable_header_is_unknown(self):
        log = MagicMock()
        log.get_metadata_status.return_value = ({}, False)
        assert _make_consolidator(log)._transcript_privacy(KEY) is _TranscriptPrivacy.UNKNOWN
        log.get_metadata_status.assert_called_once_with(KEY)

    def test_a_metadata_status_exception_is_unknown(self):
        log = MagicMock()
        log.get_metadata_status.side_effect = OSError("metadata status unavailable")
        assert _make_consolidator(log)._transcript_privacy(KEY) is _TranscriptPrivacy.UNKNOWN
        log.get_metadata_status.assert_called_once_with(KEY)


class TestConsolidateRefusesPrivateTranscripts:
    @pytest.mark.asyncio
    async def test_the_privacy_guard_is_evaluated_off_the_event_loop(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        evaluated_without_running_loop: list[bool] = []

        def classify(_key: str) -> _TranscriptPrivacy:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                evaluated_without_running_loop.append(True)
            else:
                evaluated_without_running_loop.append(False)
            return _TranscriptPrivacy.PRIVATE

        c._transcript_privacy = classify  # type: ignore[method-assign]

        assert await c._consolidate(KEY) is None
        assert evaluated_without_running_loop == [True]

    @pytest.mark.asyncio
    async def test_a_remote_transcript_is_refused_before_the_transcript_is_read(self, tmp_path):
        log = _seed_log(tmp_path)
        _mark_metadata(log, executor="remote")
        c = _make_consolidator(log)
        c._call_llm = AsyncMock(side_effect=AssertionError("a private transcript reached the LLM"))
        log.snapshot_for_consolidation = MagicMock(
            side_effect=AssertionError("guard did not fire before the snapshot")
        )

        assert await c._consolidate(KEY) is None

        log.snapshot_for_consolidation.assert_not_called()
        c._call_llm.assert_not_awaited()
        c._memory.append_history.assert_not_called()
        # The rows are fenced: the marker covers every row on disk.
        assert log.unconsolidated_count(KEY) == 0
        assert log.get_metadata(KEY)["last_consolidated"] == 3

    @pytest.mark.asyncio
    @pytest.mark.parametrize("memory_mode", ["incognito", "temporary"])
    async def test_a_restricted_memory_transcript_is_refused_before_snapshot(
        self, tmp_path, memory_mode
    ):
        log = _seed_log(tmp_path)
        _mark_metadata(log, memory_mode=memory_mode)
        c = _make_consolidator(log)
        c._call_llm = AsyncMock(side_effect=AssertionError("a private transcript reached the LLM"))
        log.snapshot_for_consolidation = MagicMock(
            side_effect=AssertionError("guard did not fire before the snapshot")
        )

        assert await c._consolidate(KEY) is None

        log.snapshot_for_consolidation.assert_not_called()
        c._call_llm.assert_not_awaited()
        c._memory.append_history.assert_not_called()
        assert log.unconsolidated_count(KEY) == 0
        assert log.get_metadata(KEY)["last_consolidated"] == 3

    @pytest.mark.asyncio
    @pytest.mark.parametrize("transition", ["executor", "memory_mode"])
    async def test_rows_fenced_while_private_stay_out_after_the_marker_clears(
        self, tmp_path, transition
    ):
        """Slot-owned markers clear; the fence is what makes the skip durable."""
        log = ConversationLog(base_dir=tmp_path / "sessions")
        log.init()
        _append_rows(log, KEY, 3, prefix="private-row-")
        _make_private(log, transition)
        c = _make_consolidator(log)
        c._call_llm = AsyncMock(return_value={"history_entry": "local summary"})

        assert await c._consolidate(KEY) is None
        c._call_llm.assert_not_awaited()

        # The slot unbinds from its peer / returns to normal memory, then the
        # local conversation continues.
        _make_local_again(log, transition)
        assert c._transcript_privacy(KEY) is _TranscriptPrivacy.LOCAL
        _append_rows(log, KEY, 2, prefix="local-row-")

        assert await c._consolidate(KEY) is None

        c._call_llm.assert_awaited_once()
        prompt = c._call_llm.await_args.args[0]
        assert "local-row-0" in prompt and "local-row-1" in prompt
        assert "private-row-" not in prompt
        c._memory.append_history.assert_called_once_with("local summary")
        assert log.get_metadata(KEY)["last_consolidated"] == 5
        assert log.unconsolidated_count(KEY) == 0

    @pytest.mark.asyncio
    async def test_a_second_private_pass_rewrites_nothing(self, tmp_path):
        log = _seed_log(tmp_path)
        _mark_metadata(log, executor="remote")
        c = _make_consolidator(log)

        assert await c._consolidate(KEY) is None
        log.mark_consolidated = MagicMock(wraps=log.mark_consolidated)

        assert await c._consolidate(KEY) is None

        log.mark_consolidated.assert_not_called()
        assert log.get_metadata(KEY)["last_consolidated"] == 3

    @pytest.mark.asyncio
    async def test_the_idle_sweep_skips_a_fenced_private_transcript(self, tmp_path):
        log = _seed_log(tmp_path)
        _mark_metadata(log, executor="remote")
        c = _make_consolidator(log)

        assert await c._consolidate(KEY) is None
        assert log.unconsolidated_count(KEY) == 0

        c._last_activity[KEY] = 0.0
        c._consolidate = AsyncMock(  # type: ignore[method-assign]
            side_effect=AssertionError("the sweep re-classified a fenced transcript")
        )

        c.check_idle_sessions()

        c._consolidate.assert_not_called()
        assert KEY not in c._running
        assert not c._tasks

    @pytest.mark.asyncio
    async def test_a_failed_fence_write_raises_and_releases_the_claim(self, tmp_path):
        log = _seed_log(tmp_path)
        _mark_metadata(log, executor="remote")
        c = _make_consolidator(log)
        c._running.add(KEY)
        log.fence_consolidation = MagicMock(side_effect=OSError("marker write failed"))
        log.snapshot_for_consolidation = MagicMock(
            side_effect=AssertionError("guard did not fire before the snapshot")
        )

        with pytest.raises(OSError, match="marker write failed"):
            await c._consolidate(KEY)

        assert KEY not in c._running
        log.fence_consolidation.assert_called_once_with(KEY)
        log.snapshot_for_consolidation.assert_not_called()
        c._memory.append_history.assert_not_called()
        # Nothing was fenced and nothing was charged: the next pass retries.
        assert log.unconsolidated_count(KEY) == 3
        assert c.retry_eligible(KEY, message_count=3)

    @pytest.mark.asyncio
    async def test_an_unreadable_header_releases_the_claim_and_stays_pending(self, tmp_path):
        log = _seed_log(tmp_path)
        path = log._path(KEY)
        lines = path.read_text(encoding="utf-8").splitlines()
        lines[0] = "{malformed metadata"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log._meta_cache.clear()
        c = _make_consolidator(log)
        c._running.add(KEY)
        c._prefs_offset[KEY] = 1
        c._history_consolidated[KEY] = 123.0
        log.mark_consolidated = MagicMock(wraps=log.mark_consolidated)

        assert await c.consolidate_now(KEY) is False

        assert KEY not in c._running
        assert c._prefs_offset[KEY] == 1
        assert c._history_consolidated[KEY] == 123.0
        log.mark_consolidated.assert_not_called()
        c._memory.append_history.assert_not_called()
        assert log.unconsolidated_count(KEY) == 3

    @pytest.mark.asyncio
    async def test_an_absent_metadata_header_still_consolidates(self, tmp_path):
        log = _seed_log(tmp_path)
        path = log._path(KEY)
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join(lines[1:]) + "\n", encoding="utf-8")
        log._meta_cache.clear()
        c = _make_consolidator(log)
        c._call_llm = AsyncMock(return_value={"history_entry": "local summary"})

        assert await c._consolidate(KEY) is None

        c._call_llm.assert_awaited_once()
        c._memory.append_history.assert_called_once_with("local summary")

    @pytest.mark.asyncio
    async def test_a_local_transcript_reaches_the_snapshot(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        reached = MagicMock(side_effect=RuntimeError("reached the snapshot"))
        log.snapshot_for_consolidation = reached

        with pytest.raises(RuntimeError, match="reached the snapshot"):
            await c._consolidate(KEY)

        reached.assert_called_once()

    @pytest.mark.asyncio
    async def test_the_cli_path_reports_done_for_a_confirmed_private_skip(self, tmp_path):
        log = _seed_log(tmp_path)
        _mark_metadata(log, executor="remote")
        c = _make_consolidator(log)
        log.snapshot_for_consolidation = MagicMock(
            side_effect=AssertionError("the CLI path reached the transcript")
        )

        assert await c.consolidate_now(KEY) is True

        log.snapshot_for_consolidation.assert_not_called()
        c._memory.append_history.assert_not_called()
        assert log.unconsolidated_count(KEY) == 0
