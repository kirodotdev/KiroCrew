"""The consolidation row counts never read the transcript on the loop.

``HistoryConsolidator.maybe_consolidate`` runs on the gateway event loop after
every completed turn, and ``check_idle_sessions`` on every sweep. The turn that
just ended moved the transcript, so the message cache misses there, and counting
rows on the loop meant a full read and JSON decode of the whole file. These
tests pin that both counts go through one worker-thread counter (the streaming
``_count_messages``, which never takes the writer lock and fills no cache), that
every whole-transcript read is made in bounded chunks with the exact rows a
whole-file read gives, and that one transcript has at most one count in flight.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import history as history_mod
from kiro_crew import history_projection
from kiro_crew.history import ConversationLog, HistoryConsolidator
from kiro_crew.testing.wait import async_wait_until

KEY = "dashboard:chat-threshold"
THRESHOLD = history_mod._CONSOLIDATION_THRESHOLD
CHUNK = history_projection._TRANSCRIPT_READ_CHUNK


def _log(tmp_path) -> ConversationLog:
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.init()
    return log


def _append_rows(log: ConversationLog, count: int, start: int = 0, key: str = KEY) -> None:
    for i in range(start, start + count):
        log.append(key, "user", f"m{i}")


async def _append(log: ConversationLog, count: int, start: int = 0, key: str = KEY) -> None:
    # Off the loop, as a real writer appends: the transcript lock makes only one
    # non-blocking attempt on the event-loop thread.
    await asyncio.to_thread(_append_rows, log, count, start, key)


def _bulk_rows(log: ConversationLog, count: int, pad: int = 200) -> None:
    """Grow the transcript by *count* rows without one locked append per row."""
    _append_rows(log, 1)
    path = log._path(KEY)
    with open(path, "a", encoding="utf-8") as handle:
        for i in range(count - 1):
            handle.write(json.dumps({"role": "user", "content": f"bulk {i} " + "x" * pad}) + "\n")
    log._invalidate_cache(KEY)


def _consolidator(log: ConversationLog) -> HistoryConsolidator:
    memory = MagicMock()
    memory.read_preferences.return_value = ""
    memory.read_projects.return_value = ""
    return HistoryConsolidator(log=log, memory=memory, migrated=True, sessions=None)


async def _settle(c: HistoryConsolidator) -> None:
    while c._tasks:
        await asyncio.gather(*list(c._tasks))


async def _until(predicate, what: str) -> None:
    """Wait for *predicate* over loop-confined state; fail by name if it never holds."""
    await async_wait_until(predicate, describe=lambda: what)


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class _Spy:
    """Record whether each call of a ConversationLog method ran on the loop."""

    def __init__(self, monkeypatch, name: str) -> None:
        self.calls: list[bool] = []
        real = getattr(ConversationLog, name)

        def _spy(log_self: ConversationLog, *a: Any, **kw: Any) -> Any:
            self.calls.append(_on_loop())
            return real(log_self, *a, **kw)

        monkeypatch.setattr(ConversationLog, name, _spy)


# ---------------------------------------------------------------- the counter


@pytest.mark.asyncio
async def test_a_turn_below_the_threshold_counts_off_the_loop_and_parses_nothing(
    tmp_path, monkeypatch
):
    log = _log(tmp_path)
    await asyncio.to_thread(_bulk_rows, log, 20_000)
    c = _consolidator(log)
    c._prefs_offset[KEY] = 20_000
    await _append(log, 1, start=20_000)
    counts = _Spy(monkeypatch, "_count_messages")
    parses = _Spy(monkeypatch, "_read_messages_locked")

    with patch.object(c, "_consolidate", new_callable=AsyncMock) as run:
        c.maybe_consolidate(KEY)
        assert counts.calls == [] and parses.calls == [], "maybe_consolidate read before returning"
        await _settle(c)

    run.assert_not_awaited()
    assert counts.calls == [False], "the count did not run, or ran on the loop"
    # The streaming count materializes no row list and fills no cache.
    assert parses.calls == [], "the count parsed the transcript into a row list"
    assert KEY not in log._msg_cache


@pytest.mark.asyncio
async def test_the_threshold_fires_at_exactly_the_threshold(tmp_path):
    log = _log(tmp_path)
    await _append(log, THRESHOLD - 1)
    c = _consolidator(log)

    with patch.object(c, "_consolidate", AsyncMock(return_value=None)) as run:
        c.maybe_consolidate(KEY)
        await _settle(c)
        run.assert_not_awaited()
        assert KEY not in c._prefs_offset

        await _append(log, 1, start=THRESHOLD - 1)
        c.maybe_consolidate(KEY)
        await _settle(c)

    run.assert_awaited_once_with(KEY, include_history=False)
    assert c._prefs_offset[KEY] == THRESHOLD
    assert not c._counting


@pytest.mark.asyncio
async def test_rows_another_writer_appended_are_counted(tmp_path):
    log = _log(tmp_path)
    await _append(log, THRESHOLD - 10)
    # A second log over the same directory stands in for another process
    # (a subagent, a cron) appending to this transcript.
    other = ConversationLog(base_dir=tmp_path / "sessions")
    other.init()

    def _other_writer() -> None:
        for i in range(10):
            other.append(KEY, "assistant", f"other {i}")

    await asyncio.to_thread(_other_writer)
    c = _consolidator(log)

    with patch.object(c, "_consolidate", AsyncMock(return_value=None)) as run:
        c.maybe_consolidate(KEY)
        await _settle(c)

    run.assert_awaited_once()
    assert c._prefs_offset[KEY] == THRESHOLD


class _HeldCount:
    """Hold the first off-loop count after it has read, until released."""

    def __init__(self, log: ConversationLog) -> None:
        self._real = log._count_messages
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.calls = 0
        self.inflight = 0
        self.peak = 0
        self._lock = threading.Lock()

    def __call__(self, key: str, **kw: Any) -> int:
        with self._lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
        try:
            rows = self._real(key, **kw)
            self.calls += 1
            if self.calls == 1:
                self.entered.set()
                assert self.release.wait(30), "test never released the held count"
            return rows
        finally:
            with self._lock:
                self.inflight -= 1
            self.finished.set()


@pytest.mark.asyncio
async def test_a_turn_that_ends_during_a_count_starts_no_second_one(tmp_path, monkeypatch):
    log = _log(tmp_path)
    await _append(log, THRESHOLD - 1)
    c = _consolidator(log)
    held = _HeldCount(log)
    monkeypatch.setattr(log, "_count_messages", held)

    with patch.object(c, "_consolidate", AsyncMock(return_value=None)) as run:
        c.maybe_consolidate(KEY)
        assert await asyncio.to_thread(held.entered.wait, 30)
        # The first count has already read THRESHOLD - 1 rows. The next turn
        # lands its row and ends while that count is still in flight.
        await _append(log, 1, start=THRESHOLD - 1)
        c.maybe_consolidate(KEY)
        assert len(c._tasks) == 1, "a second count started beside the first"
        held.release.set()
        await _settle(c)
        assert held.calls == 1
        run.assert_not_awaited()

        # The following turn's count includes the row the dropped call saw.
        await _append(log, 1, start=THRESHOLD)
        c.maybe_consolidate(KEY)
        await _settle(c)

    assert held.calls == 2
    run.assert_awaited_once()
    assert c._prefs_offset[KEY] == THRESHOLD + 1


@pytest.mark.asyncio
async def test_two_spellings_of_one_transcript_share_one_claim(tmp_path, monkeypatch):
    """``_busy`` matches by file name, so the count claim does too."""
    alias = "dashboard_chat-threshold"
    log = _log(tmp_path)
    await _append(log, THRESHOLD)
    assert log._path(alias).name == log._path(KEY).name
    c = _consolidator(log)
    held = _HeldCount(log)
    monkeypatch.setattr(log, "_count_messages", held)

    with patch.object(c, "_consolidate", AsyncMock(return_value=None)):
        c.maybe_consolidate(KEY)
        assert await asyncio.to_thread(held.entered.wait, 30)
        c.maybe_consolidate(alias)
        assert len(c._tasks) == 1, "the alias started a second count of the same file"
        held.release.set()
        await _settle(c)

    assert held.peak == 1
    assert not c._counting


@pytest.mark.asyncio
async def test_a_pass_claimed_during_the_count_is_not_doubled(tmp_path, monkeypatch):
    log = _log(tmp_path)
    await _append(log, THRESHOLD)
    c = _consolidator(log)
    held = _HeldCount(log)
    monkeypatch.setattr(log, "_count_messages", held)

    with patch.object(c, "_consolidate", AsyncMock(return_value=None)) as run:
        c.maybe_consolidate(KEY)
        assert await asyncio.to_thread(held.entered.wait, 30)
        # An idle or session-end pass claims the transcript meanwhile.
        c._running.add(KEY)
        held.release.set()
        await _settle(c)

    run.assert_not_awaited()
    assert KEY not in c._prefs_offset
    assert c._running == {KEY}, "the threshold check released a claim it did not take"


@pytest.mark.asyncio
async def test_a_refused_pass_leaves_the_offset_alone(tmp_path):
    from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED

    log = _log(tmp_path)
    await _append(log, THRESHOLD)
    c = _consolidator(log)

    with patch.object(c, "_consolidate", AsyncMock(return_value=_CONSOLIDATION_REFUSED)) as run:
        c.maybe_consolidate(KEY)
        await _settle(c)

    run.assert_awaited_once()
    assert KEY not in c._prefs_offset


@pytest.mark.asyncio
async def test_an_unreadable_transcript_skips_the_check_without_raising(tmp_path, monkeypatch):
    log = _log(tmp_path)
    await _append(log, THRESHOLD)
    c = _consolidator(log)
    monkeypatch.setattr(log, "_count_messages", MagicMock(side_effect=OSError("EIO")))

    with patch.object(c, "_consolidate", new_callable=AsyncMock) as run:
        c.maybe_consolidate(KEY)
        await _settle(c)

    run.assert_not_awaited()
    assert KEY not in c._running
    assert not c._counting


@pytest.mark.asyncio
async def test_the_count_does_not_hold_the_writer_lock_while_it_reads(tmp_path, monkeypatch):
    """An on-loop ``_locked`` acquires the per-file lock blocking, so a count
    holding it for the whole stream would stall the loop for that long anyway."""
    log = _log(tmp_path)
    await _append(log, THRESHOLD)
    c = _consolidator(log)
    entered = threading.Event()
    release = threading.Event()
    real = history_projection.count_messages

    def _held_stream(path: Any) -> int:
        entered.set()
        assert release.wait(30), "test never released the held count"
        return real(path)

    monkeypatch.setattr(history_projection, "count_messages", _held_stream)

    with patch.object(c, "_consolidate", new_callable=AsyncMock):
        c.maybe_consolidate(KEY)
        assert await asyncio.to_thread(entered.wait, 30)
        lock = log._file_lock(KEY)
        free = lock.acquire(blocking=False)
        if free:
            lock.release()
        release.set()
        await _settle(c)

    assert free, "the off-loop count held the transcript's writer lock while it read"


@pytest.mark.asyncio
async def test_a_cancelled_count_keeps_its_claim_until_the_thread_stops(tmp_path, monkeypatch):
    log = _log(tmp_path)
    await _append(log, THRESHOLD)
    c = _consolidator(log)
    held = _HeldCount(log)
    monkeypatch.setattr(log, "_count_messages", held)

    with patch.object(c, "_consolidate", AsyncMock(return_value=None)) as run:
        c.maybe_consolidate(KEY)
        assert await asyncio.to_thread(held.entered.wait, 30)
        (task,) = list(c._tasks)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        # The thread is still reading: the claim must outlive the cancelled
        # task, or the next turn starts a second read of the same file.
        assert c._counting, "cancelling the caller released the claim mid-read"
        c.maybe_consolidate(KEY)
        assert not c._tasks, "a second count started while the first still read"
        held.release.set()
        await _until(lambda: not c._counting, "the claim to clear once the thread stopped")
        assert held.peak == 1
        assert KEY not in c._running
        run.assert_not_awaited()

        # The key is not stranded: the next turn counts and runs the pass.
        c.maybe_consolidate(KEY)
        await _settle(c)

    run.assert_awaited_once()
    assert c._prefs_offset[KEY] == THRESHOLD


@pytest.mark.asyncio
async def test_a_failed_pass_is_retrieved_and_leaves_the_offset_alone(tmp_path):
    """``_consolidate`` logs and re-raises; the counting task takes the raise so
    asyncio never reports it as an exception nobody retrieved."""
    log = _log(tmp_path)
    await _append(log, THRESHOLD)
    c = _consolidator(log)
    unretrieved: list[dict] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unretrieved.append(context))
    try:
        with patch.object(c, "_consolidate", AsyncMock(side_effect=OSError("disk full"))) as run:
            c.maybe_consolidate(KEY)
            (task,) = list(c._tasks)
            await _settle(c)
        run.assert_awaited_once()
        assert task.done() and not task.cancelled()
        assert task.exception() is None, "the pass's raise escaped the counting task"
    finally:
        loop.set_exception_handler(previous)
    assert KEY not in c._prefs_offset
    assert not unretrieved


@pytest.mark.asyncio
async def test_a_count_cancelled_before_it_starts_releases_its_claim(tmp_path):
    log = _log(tmp_path)
    await _append(log, 3)
    c = _consolidator(log)

    c.maybe_consolidate(KEY)
    (task,) = list(c._tasks)
    task.cancel()  # before its first step, so its own finally never runs
    await asyncio.gather(task, return_exceptions=True)

    assert not c._counting, "a task cancelled before it started stranded the claim"


def test_a_late_release_never_drops_a_newer_claim(tmp_path):
    log = _log(tmp_path)
    c = _consolidator(log)
    first = c._claim_count(KEY)
    assert first is not None
    c._release_count(first)
    second = c._claim_count(KEY)
    assert second is not None

    c._release_count(first)  # the first task's done callback, arriving late

    assert c._counting == {second.name: second}


@pytest.mark.asyncio
async def test_a_turn_inside_the_retry_deadline_counts_nothing(tmp_path, monkeypatch):
    """After a restart ``_prefs_offset`` is 0, so only the deadline stops a count per turn."""
    log = _log(tmp_path)
    await _append(log, THRESHOLD + 5)
    with history_mod.allow_on_loop_persist():
        log.update_metadata(KEY, {"consolidation_attempts": 1, "consolidation_retry_at": 4e9})
    c = _consolidator(log)
    counts = _Spy(monkeypatch, "_count_messages")

    with patch.object(c, "_consolidate", new_callable=AsyncMock) as run:
        c.maybe_consolidate(KEY)
        assert not c._tasks, "a backed-off key scheduled a task"
        await _settle(c)

    run.assert_not_awaited()
    assert counts.calls == [], "a backed-off key was counted"
    assert not c._counting


@pytest.mark.asyncio
async def test_a_deadline_armed_after_the_turn_check_is_caught_in_the_thread(tmp_path, monkeypatch):
    """The loop check passes; a deadline that lands before the thread runs still stops the count."""
    log = _log(tmp_path)
    await _append(log, THRESHOLD + 5)
    c = _consolidator(log)
    counts = _Spy(monkeypatch, "_count_messages")
    real = log.consolidation_retry_state
    seen: list[int] = []

    def armed_after_the_first_read(key: str, *a: Any, **kw: Any) -> tuple[int, float]:
        seen.append(1)
        return real(key, *a, **kw) if len(seen) == 1 else (1, 4e9)

    monkeypatch.setattr(log, "consolidation_retry_state", armed_after_the_first_read)

    with patch.object(c, "_consolidate", new_callable=AsyncMock) as run:
        c.maybe_consolidate(KEY)
        assert c._tasks, "the loop check refused a key with no deadline"
        await _settle(c)

    run.assert_not_awaited()
    assert counts.calls == [], "the thread counted a key whose deadline was armed meanwhile"
    assert not c._counting


# ------------------------------------------------------------ the idle sweep


def _idle(c: HistoryConsolidator, key: str = KEY) -> None:
    c._activity_seeded = True
    c._last_activity[key] = 0.0


@pytest.mark.asyncio
async def test_the_idle_sweep_never_parses_the_transcript_on_the_loop(tmp_path, monkeypatch):
    log = _log(tmp_path)
    await _append(log, 5)
    c = _consolidator(log)
    with patch.object(c, "_consolidate", AsyncMock(return_value=None)):
        c.maybe_consolidate(KEY)  # end of turn: the cache is left cold
        await _settle(c)
    counts = _Spy(monkeypatch, "_count_messages")
    _idle(c)

    with patch.object(c, "_consolidate", AsyncMock(return_value=None)) as run:
        c.check_idle_sessions()
        assert counts.calls == [], "the sweep read the transcript before returning"
        await _settle(c)

    assert counts.calls == [False]
    run.assert_awaited_once_with(KEY, include_history=True)
    assert KEY in c._history_consolidated
    assert not c._counting


@pytest.mark.asyncio
async def test_the_idle_sweep_counts_through_the_same_claim(tmp_path, monkeypatch):
    log = _log(tmp_path)
    await _append(log, THRESHOLD)
    c = _consolidator(log)
    held = _HeldCount(log)
    monkeypatch.setattr(log, "_count_messages", held)

    with patch.object(c, "_consolidate", AsyncMock(return_value=None)) as run:
        c.maybe_consolidate(KEY)
        assert await asyncio.to_thread(held.entered.wait, 30)
        _idle(c)
        c.check_idle_sessions()
        assert len(c._tasks) == 1, "the sweep counted beside the turn's count"
        held.release.set()
        await _settle(c)

    assert held.peak == 1
    run.assert_awaited_once_with(KEY, include_history=False)


@pytest.mark.asyncio
async def test_a_fully_consolidated_idle_key_runs_no_pass_and_is_stamped_clean(
    tmp_path, monkeypatch
):
    log = _log(tmp_path)
    await _append(log, 40)
    with history_mod.allow_on_loop_persist():
        log.mark_consolidated(KEY, 40)
    c = _consolidator(log)
    counts = _Spy(monkeypatch, "_count_messages")
    _idle(c)

    with patch.object(c, "_consolidate", AsyncMock(return_value=None)) as run:
        c.check_idle_sessions()
        await _settle(c)

    run.assert_not_awaited()
    assert counts.calls == [False]
    assert c._idle_clean.get(KEY) == c._transcript_stamp(KEY)


@pytest.mark.asyncio
async def test_a_key_inside_its_retry_deadline_schedules_no_count(tmp_path):
    log = _log(tmp_path)
    await _append(log, 3)
    c = _consolidator(log)
    with history_mod.allow_on_loop_persist():
        log.update_metadata(KEY, {"consolidation_attempts": 1, "consolidation_retry_at": 4e9})
    _idle(c)

    c.check_idle_sessions()

    assert not c._tasks
    assert not c._counting


@pytest.mark.asyncio
async def test_a_refused_idle_pass_sets_no_throttle(tmp_path):
    from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED

    log = _log(tmp_path)
    await _append(log, 3)
    c = _consolidator(log)
    _idle(c)

    with patch.object(c, "_consolidate", AsyncMock(return_value=_CONSOLIDATION_REFUSED)) as run:
        c.check_idle_sessions()
        await _settle(c)

    run.assert_awaited_once()
    assert KEY not in c._history_consolidated


# ------------------------------------------------------------- the chunks


def _reference_rows(raw: str) -> list[dict]:
    """The rows a whole-file ``read().splitlines()`` parse gives."""
    rows = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("_type") != "metadata":
            rows.append(data)
    return rows


def _row(content: str, **extra: Any) -> str:
    return json.dumps({"role": "user", "content": content, **extra})


FIXTURES: dict[str, str] = {
    "plain": "".join(_row(f"m{i}") + "\n" for i in range(5)),
    "blank_lines": "\n" + _row("a") + "\n   \n\n" + _row("b") + "\n",
    "malformed_rows": _row("a") + "\n{not json\n[1,2]\n42\n" + _row("b") + "\n",
    "extra_metadata_row": json.dumps({"_type": "metadata", "x": 1}) + "\n" + _row("a") + "\n",
    "archived_dropped": _row("a", archived=True) + "\n" + _row("b", dropped=True) + "\n",
    "crlf": _row("a") + "\r\n" + _row("b") + "\r\n",
    "u2028_raw": json.dumps({"role": "user", "content": "a\u2028b"}, ensure_ascii=False) + "\n",
    "lone_cr_between_rows": _row("a") + "\r" + _row("b") + "\n",
    "truncated_tail": _row("ok") + "\n" + '{"role": "user", "cont',
    "every_boundary": "\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029".join(_row(f"s{i}") for i in range(9)),
    # A truncated tail whose last byte (A8 of U+00A8) is also U+2028's last byte.
    "ends_on_a_boundary_lookalike": _row("a") + "\n" + '{"content": "\u00a8',
}

#: Bodies written with no real metadata line in front, so the leading line varies.
LEADING: dict[str, str] = {
    "no_metadata": _row("a") + "\n" + _row("b") + "\n",
    "metadata_shadowed_by_a_duplicate_key": '{"_type": "metadata", "_type": "x"}\n'
    + _row("a")
    + "\n",
    "metadata_then_cr": json.dumps({"_type": "metadata"}) + "\r" + _row("a") + "\n",
    "row_then_cr_metadata": _row("a") + "\r" + json.dumps({"_type": "metadata"}) + "\n",
    "blank_first_line": "\n" + _row("a") + "\n",
    "metadata_crlf": json.dumps({"_type": "metadata"}) + "\r\n" + _row("a") + "\r\n",
    "bom_metadata": "\ufeff" + json.dumps({"_type": "metadata"}) + "\n" + _row("a") + "\n",
    "empty": "",
}


def _write(log: ConversationLog, body: str, *, metadata: bool = True) -> None:
    if metadata:
        _append_rows(log, 1)  # writes the real metadata line first
    path = log._path(KEY)
    with open(path, "a" if metadata else "w", encoding="utf-8", newline="") as handle:
        handle.write(body)
    log._invalidate_cache(KEY)


@pytest.mark.parametrize("chunk", [1, 2, 3, 5, 4096])
@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_the_chunked_rows_and_the_count_are_exact(tmp_path, monkeypatch, name, chunk):
    log = _log(tmp_path)
    with history_mod.allow_on_loop_persist():
        _write(log, FIXTURES[name])
    expected = _reference_rows(log._path(KEY).read_text(encoding="utf-8"))
    monkeypatch.setattr(history_projection, "_TRANSCRIPT_READ_CHUNK", chunk)

    assert log._read_messages(KEY) == expected
    assert log._count_messages(KEY, hold_writer_lock=False) == len(expected)


@pytest.mark.parametrize("chunk", [1, 3, 4096])
@pytest.mark.parametrize("name", sorted(LEADING))
def test_the_rows_are_exact_whatever_the_leading_line_is(tmp_path, monkeypatch, name, chunk):
    log = _log(tmp_path)
    _write(log, LEADING[name], metadata=False)
    expected = _reference_rows(log._path(KEY).read_text(encoding="utf-8"))
    monkeypatch.setattr(history_projection, "_TRANSCRIPT_READ_CHUNK", chunk)

    assert log._read_messages(KEY) == expected
    assert log._count_messages(KEY, hold_writer_lock=False) == len(expected)


@pytest.mark.parametrize("chunk", [1, 2, 3, 4096])
def test_a_file_whose_last_row_has_no_newline_is_still_read(tmp_path, monkeypatch, chunk):
    """No metadata line and no trailing boundary: one row, zero boundaries."""
    log = _log(tmp_path)
    _write(log, _row("only"), metadata=False)
    monkeypatch.setattr(history_projection, "_TRANSCRIPT_READ_CHUNK", chunk)

    assert len(log._read_messages(KEY)) == 1
    assert log._count_messages(KEY, hold_writer_lock=False) == 1


class _ReadSizes:
    """Wrap a file handle, recording the size every ``read`` call asks for."""

    def __init__(self, handle: Any, sizes: list[int]) -> None:
        self._handle = handle
        self._sizes = sizes

    def read(self, size: int = -1) -> Any:
        self._sizes.append(size)
        return self._handle.read(size)

    def __enter__(self) -> _ReadSizes:
        self._handle.__enter__()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._handle.__exit__(*exc)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._handle, name)


@pytest.mark.asyncio
async def test_no_read_of_a_large_transcript_holds_more_than_one_chunk(tmp_path, monkeypatch):
    """The heartbeat property, asserted by schedule rather than by stopwatch.

    A whole-file ``read()`` or ``splitlines()`` is one GIL-holding call, so a
    full read in a worker thread (the consolidation pass's snapshot) still
    froze the loop for the whole file. Every read of the transcript asks for at
    most one chunk, so the loop waits at most one chunk's work at a time.
    """
    log = _log(tmp_path)
    await asyncio.to_thread(_bulk_rows, log, 8 * THRESHOLD, 50_000)  # about 12 MB
    assert log._path(KEY).stat().st_size > 8 * CHUNK
    path = str(log._path(KEY))
    sizes: list[int] = []
    real_open = open

    def _recording_open(file: Any, *a: Any, **kw: Any) -> Any:
        handle = real_open(file, *a, **kw)
        return _ReadSizes(handle, sizes) if str(file) == path else handle

    monkeypatch.setattr(history_projection, "open", _recording_open, raising=False)

    rows = await asyncio.to_thread(log._read_messages, KEY)

    assert len(rows) == 8 * THRESHOLD
    assert all(0 < size <= CHUNK for size in sizes), f"an unbounded read: {sorted(set(sizes))}"
    assert len(sizes) > 8, f"premise broken: {len(sizes)} reads"
