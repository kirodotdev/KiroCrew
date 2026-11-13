"""The Older Sessions listing index is built at boot, in the one pass over the heads.

Once the gateway serves, the search supervisor builds the listing index on a worker
thread and the search warm-up takes its window from that index, so the first
listing and the first search after boot read no transcript head. Every budget here
is a call count; each wait is bounded and fails by name.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_crew import history, history_index_worker, history_listing_index, history_listing_memo
from kiro_crew.dashboard.handlers.sessions_parts.history_reads import _is_user_listed_row
from kiro_crew.history import ConversationLog
from kiro_crew.history_listing_index import _STOP_CHECK_EVERY, SessionListingIndex
from kiro_crew.history_search import SessionCatalogProjection
from kiro_crew.testing.wait import async_wait_until

_BASE_NS = 1_700_000_000 * 10**9
_META = {"_type": "metadata", "created_at": "2026-01-01T00:00:00"}

#: Seconds one wait in this file may take before the test fails.
_BOUND = 30.0


@pytest.fixture(autouse=True)
def _no_listing_memo(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    # The scan's whole-listing memo would answer the comparison scan from before it.
    _floor_monkeypatch.setattr(history_listing_memo, "TTL_SECS", 0.0)


def _heads_dir(tmp_path, count: int):
    """*count* one-line transcripts with no title in their metadata: one head read each.

    A third hold a user message (titled from the head), a third an assistant message
    (untitled, with content), a third only a metadata line (untitled, empty).
    """
    d = tmp_path / "s"
    d.mkdir()
    for i in range(count):
        line = (
            {"role": "user", "content": f"question {i}"},
            {"role": "assistant", "content": f"answer {i}"},
            _META,
        )[i % 3]
        path = d / f"dashboard_chat-{i}-{i}.jsonl"
        path.write_text(json.dumps(line) + "\n", encoding="utf-8")
        # Whole seconds: NTFS keeps 100 ns and a float mtime cannot tell
        # nanoseconds apart at this epoch, so finer steps can tie.
        os.utime(path, ns=(_BASE_NS + i * 10**9, _BASE_NS + i * 10**9))
    # A settled directory stamp: a listing re-reads the names only when it moves.
    os.utime(d, ns=(_BASE_NS + count * 10**9, _BASE_NS + count * 10**9))
    return d


def _indexed_log(d) -> SimpleNamespace:
    """A log whose index never hands a reconcile to the maintenance pool unseen."""
    log = ConversationLog(base_dir=d)
    jobs: list = []
    log._listing_index = SessionListingIndex(log, clock=lambda: 1000.0, submit=jobs.append)
    return SimpleNamespace(log=log, index=log._listing_index, jobs=jobs)


def _count(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Record every row read and every head read, by transcript."""
    seen = SimpleNamespace(rows=[], heads=[])
    real_row = SessionCatalogProjection._listing_row
    real_head = SessionCatalogProjection._scan_head_lines

    def row(self, path, key, entry):
        seen.rows.append(key)
        return real_row(self, path, key, entry)

    def head(f):
        seen.heads.append(os.fspath(f.name))
        return real_head(f)

    monkeypatch.setattr(SessionCatalogProjection, "_listing_row", row)
    # Every head read, from a path or from the metadata read's handle, runs this.
    monkeypatch.setattr(SessionCatalogProjection, "_scan_head_lines", staticmethod(head))
    return seen


def _warm_lines(caplog: pytest.LogCaptureFixture) -> list[tuple]:
    return [
        tuple(r.args[:2])
        for r in caplog.records
        if r.name == history_listing_index.__name__
        and str(r.msg).startswith("Session listing index warm")
    ]


def _gate_first_row(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Hold the first row read until the test sets ``release``."""
    gate = SimpleNamespace(entered=threading.Event(), release=threading.Event(), reads=[])
    real = SessionCatalogProjection._listing_row

    def gated(self, path, key, entry):
        gate.reads.append(key)
        if len(gate.reads) == 1:
            gate.entered.set()
            assert gate.release.wait(_BOUND), "the test never released the first row read"
        return real(self, path, key, entry)

    monkeypatch.setattr(SessionCatalogProjection, "_listing_row", gated)
    return gate


class _WatchedLock:
    """The index lock, recording when a caller blocks on it while another holds it."""

    def __init__(self, inner: threading.Lock) -> None:
        self._inner = inner
        self.contended = threading.Event()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if self._inner.acquire(blocking=False):
            return True
        if not blocking:
            return False
        self.contended.set()
        return self._inner.acquire(True, timeout)

    def release(self) -> None:
        self._inner.release()

    def locked(self) -> bool:
        return self._inner.locked()

    def __enter__(self) -> _WatchedLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def test_the_warm_reads_each_head_once_and_the_first_listing_reads_none(
    tmp_path, monkeypatch, caplog
) -> None:
    d = _heads_dir(tmp_path, 1500)
    env = _indexed_log(d)
    seen = _count(monkeypatch)
    caplog.set_level(logging.INFO, logger=history_listing_index.__name__)

    assert env.log.warm_listing_index() is True
    assert len(seen.heads) == len(set(seen.heads)) == 1500
    assert len(seen.rows) == 1500
    assert _warm_lines(caplog) == [("built", 1500)]

    del seen.heads[:], seen.rows[:]
    rows = env.log.list_sessions_indexed()
    assert (seen.heads, seen.rows, env.jobs) == ([], [], [])
    with patch.object(env.log, "has_messages", wraps=env.log.has_messages) as reads:
        listed = [r["key"] for r in rows if _is_user_listed_row(env.log, r)]
    assert reads.call_count == 0
    # The user and assistant thirds; the metadata-only third is empty.
    assert len(listed) == 1000
    assert rows == ConversationLog(base_dir=d).list_sessions()


def test_a_raise_mid_warm_leaves_the_index_and_the_journal_as_they_were(
    tmp_path, monkeypatch
) -> None:
    d = _heads_dir(tmp_path, 600)
    env = _indexed_log(d)
    # Writes through a log, so the journal holds records no index has replayed.
    env.log.set_title("dashboard:chat-2-2", "Renamed")
    env.log.set_title("dashboard:chat-5-5", "Renamed too")
    base = str(d)
    journal = dict(ConversationLog._dir_bumped_stems[base])
    assert sorted(journal) == ["dashboard_chat-2-2", "dashboard_chat-5-5"]
    epoch = env.index._epoch
    reads: list[str] = []
    left = [300]
    real = SessionCatalogProjection._listing_row

    def failing(self, path, key, entry):
        reads.append(key)
        left[0] -= 1
        if left[0] == 0:
            raise RuntimeError("a row read failed")
        return real(self, path, key, entry)

    monkeypatch.setattr(SessionCatalogProjection, "_listing_row", failing)
    with pytest.raises(RuntimeError, match="a row read failed"):
        env.log.warm_listing_index()
    index = env.index
    assert (index._built, index._entries, index._order, index._epoch) == (False, {}, [], epoch)
    assert dict(ConversationLog._dir_bumped_stems[base]) == journal
    assert not index._lock.locked()

    # The next listing builds at once, as it would with no warm, and replays the journal.
    del reads[:]
    rows = env.log.list_sessions_indexed()
    assert len(reads) == 600
    assert index._epoch == ConversationLog._dir_write_epochs[base]
    assert not ConversationLog._dir_bumped_stems.get(base)
    titles = {r["key"]: r["title"] for r in rows}
    assert titles["dashboard_chat-2-2"] == "Renamed"
    assert titles["dashboard_chat-5-5"] == "Renamed too"
    assert rows == ConversationLog(base_dir=d).list_sessions()


def test_should_stop_ends_the_warm_at_its_next_check(tmp_path, monkeypatch, caplog) -> None:
    count = 3 * _STOP_CHECK_EVERY
    d = _heads_dir(tmp_path, count)
    env = _indexed_log(d)
    seen = _count(monkeypatch)
    caplog.set_level(logging.INFO, logger=history_listing_index.__name__)
    asked: list[int] = []

    def should_stop() -> bool:
        asked.append(len(seen.rows))
        return len(asked) == 2

    assert env.log.warm_listing_index(should_stop) is False
    assert asked == [0, _STOP_CHECK_EVERY]
    assert len(seen.rows) == _STOP_CHECK_EVERY
    assert (env.index._built, env.index._entries, env.index._epoch) == (False, {}, 0)
    assert _warm_lines(caplog) == [("not built", 0)]

    del seen.rows[:]
    assert len(env.log.list_sessions_indexed()) == count
    assert len(seen.rows) == count


@pytest.mark.asyncio
async def test_the_warm_returns_at_once_while_a_listing_holds_the_index(
    tmp_path, monkeypatch, caplog
) -> None:
    d = _heads_dir(tmp_path, 30)
    env = _indexed_log(d)
    gate = _gate_first_row(monkeypatch)
    caplog.set_level(logging.INFO, logger=history_listing_index.__name__)
    listing = asyncio.ensure_future(asyncio.to_thread(env.log.list_sessions_indexed))
    try:
        await async_wait_until(gate.entered.is_set, describe=lambda: "the listing never built")
        assert env.log.warm_listing_index() is False
        assert len(gate.reads) == 1
    finally:
        gate.release.set()
    rows = await asyncio.wait_for(listing, _BOUND)
    assert len(gate.reads) == 30
    assert _warm_lines(caplog) == [("superseded by a listing", 0)]
    assert rows == ConversationLog(base_dir=d).list_sessions()


@pytest.mark.asyncio
async def test_a_listing_during_the_warm_waits_for_it_and_reads_no_head(
    tmp_path, monkeypatch
) -> None:
    d = _heads_dir(tmp_path, 30)
    env = _indexed_log(d)
    lock = _WatchedLock(env.index._lock)
    env.index._lock = lock
    gate = _gate_first_row(monkeypatch)
    warm = asyncio.ensure_future(asyncio.to_thread(env.log.warm_listing_index))
    try:
        await async_wait_until(gate.entered.is_set, describe=lambda: "the warm never built")
        listing = asyncio.ensure_future(asyncio.to_thread(env.log.list_sessions_indexed))
        await async_wait_until(lock.contended.is_set, describe=lambda: "no listing waited")
    finally:
        gate.release.set()
    assert await asyncio.wait_for(warm, _BOUND) is True
    rows = await asyncio.wait_for(listing, _BOUND)
    assert len(gate.reads) == 30
    assert rows == ConversationLog(base_dir=d).list_sessions()


class _Supervisor:
    """The search index writer's supervisor with no child: it gives up once started."""

    def __init__(self, sessions_dir: object) -> None:
        self.gave_up = False

    def start(self) -> bool:
        self.gave_up = True
        return False

    def poll(self) -> float:
        raise AssertionError("polled a supervisor that gave up")

    def request_stop(self) -> None:
        pass

    def reap(self) -> None:
        pass


@pytest.mark.asyncio
async def test_boot_runs_one_head_read_pass(tmp_path, monkeypatch, caplog) -> None:
    """Both entrypoints kick the search supervisor; its warm-up builds the index once."""
    from kiro_crew.dashboard import server as srv

    window = 50
    monkeypatch.setattr(history, "_SEARCH_SCAN_WINDOW", window)
    count = window + 100
    d = _heads_dir(tmp_path, count)
    env = _indexed_log(d)
    seen = _count(monkeypatch)
    steps: list[str] = []
    stops: list[object] = []
    windows: list[list[str]] = []
    real_warm = ConversationLog.warm_listing_index

    def warm(self, should_stop=None):
        steps.append("listing index")
        stops.append(should_stop)
        return real_warm(self, should_stop)

    def shortlist(self, window, needles):
        steps.append("search warm")
        windows.append([m["key"] for m in window])
        return None, {}

    monkeypatch.setattr(ConversationLog, "warm_listing_index", warm)
    monkeypatch.setattr(SessionCatalogProjection, "_index_shortlist", shortlist)
    monkeypatch.setattr(
        SessionCatalogProjection,
        "search_index",
        property(lambda self: SimpleNamespace(available=True)),
    )
    monkeypatch.setattr(history_index_worker, "SessionIndexWorkerSupervisor", _Supervisor)
    stop = threading.Event()
    monkeypatch.setattr(srv, "shutdown_event", stop)
    caplog.set_level(logging.WARNING)
    state = SimpleNamespace(conversation_log=env.log, _background_tasks=set())

    srv._kick_session_search_index(state)
    await asyncio.wait_for(asyncio.gather(*list(state._background_tasks)), _BOUND)

    assert steps == ["listing index", "search warm"]
    assert stops == [stop.is_set]
    assert len(seen.heads) == len(set(seen.heads)) == count
    assert len(seen.rows) == count
    assert not [r.getMessage() for r in caplog.records if "warm" in r.getMessage()]

    del seen.heads[:], seen.rows[:]
    listed = env.log.list_sessions_indexed()
    assert (seen.heads, seen.rows) == ([], [])
    assert windows == [[m["key"] for m in listed[:window]]]
