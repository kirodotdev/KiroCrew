"""The in-memory listing index answers exactly what the directory scan answers.

``ConversationLog.list_sessions_indexed`` must return the same rows, in the same order,
as ``list_sessions`` after every kind of write, while reading only the transcripts that
changed. Every budget here is a call count against an injected clock, and a reconcile
runs only when the test runs the job a listing handed to the pool.
"""

from __future__ import annotations

import builtins
import errno
import functools
import json
import logging
import os
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew import history_cache, history_listing_index, history_listing_memo
from kiro_crew.dashboard.handlers import api_sessions
from kiro_crew.dashboard.handlers.sessions_parts.history_reads import _is_user_listed_row
from kiro_crew.history import ConversationLog
from kiro_crew.history_cache import _LRUCache
from kiro_crew.history_listing_index import RECONCILE_SECS, SessionListingIndex
from kiro_crew.history_search import SessionCatalogProjection

_BASE_NS = 1_700_000_000 * 10**9
_SECOND_NS = 10**9
_META = {"_type": "metadata", "created_at": "2026-01-01T00:00:00"}


def _stamp(seconds: int) -> int:
    """A file timestamp separated from adjacent test stamps by whole seconds."""
    return _BASE_NS + seconds * _SECOND_NS


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Jobs:
    """The maintenance pool, run by the test."""

    def __init__(self) -> None:
        self.pending: list = []

    def __call__(self, job) -> None:
        self.pending.append(job)

    def run(self) -> int:
        ran = 0
        while self.pending:
            self.pending.pop(0)()
            ran += 1
        return ran


@pytest.fixture(autouse=True)
def _no_listing_memo(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    # The scan's whole-listing memo would answer an outside write from before it.
    _floor_monkeypatch.setattr(history_listing_memo, "TTL_SECS", 0.0)


def _write(path, lines: list[dict], mtime_ns: int) -> None:
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    os.utime(path, ns=(mtime_ns, mtime_ns))


def _home(tmp_path):
    """A varied session directory; every stamp set explicitly."""
    d = tmp_path / "sessions"
    d.mkdir()
    _write(d / "dashboard_chat-1-1.jsonl", [{**_META, "title": "Titled"}], _stamp(1))
    _write(
        d / "dashboard_chat-2-2.jsonl",
        [_META, {"role": "user", "content": "first question " * 10}],
        _stamp(2),
    )
    _write(d / "dashboard_chat-3-3.jsonl", [_META], _stamp(3))
    _write(d / "subagent_abc.jsonl", [_META, {"role": "assistant", "content": "x"}], _stamp(4))
    _write(d / "workflow_w1.jsonl", [_META, {"role": "user", "content": "go"}], _stamp(5))
    _write(
        d / "dashboard_chat-4-4.jsonl",
        [{**_META, "title": "Closed", "closed_at": "2026-01-02T00:00:00", "agent": "a1"}],
        _stamp(6),
    )
    # A stacked resume duplicate, newer than its canonical file, and a pair sharing one
    # mtime so the tie-break is pinned to directory order.
    _write(d / "dashboard_chat-5-5.jsonl", [{**_META, "title": "Old"}], _stamp(7))
    _write(d / "dashboard_dashboard_chat-5-5.jsonl", [{**_META, "title": "Stacked"}], _stamp(8))
    _write(d / "dashboard_chat-6-6.jsonl", [{**_META, "title": "Tie A"}], _stamp(9))
    _write(d / "dashboard_chat-7-7.jsonl", [{**_META, "title": "Tie B"}], _stamp(9))
    _write(d / "slack_1.jsonl", [{"role": "user", "content": "no metadata line"}], _stamp(10))
    (d / "broken.jsonl").write_text("not json\n", encoding="utf-8")
    os.utime(d / "broken.jsonl", ns=(_stamp(11), _stamp(11)))
    # Heads of 22 metadata lines: the head read cannot settle has_messages.
    deep = [_META] * 22 + [{"role": "assistant", "content": "y"}]
    _write(d / "cron_deep.jsonl", deep, _stamp(12))
    _write(d / "cron_empty.jsonl", [_META] * 22, _stamp(13))
    # Archive lives in a subdirectory and is never listed.
    (d / "archive").mkdir()
    _write(d / "archive" / "dashboard_chat-1-1.jsonl", [_META], _stamp(0))
    # A settled directory stamp: a listing re-reads the names only when it moves.
    os.utime(d, ns=(_stamp(100), _stamp(100)))
    return d


def _attach(log: ConversationLog) -> SimpleNamespace:
    clock, jobs = _Clock(), _Jobs()
    log._listing_index = SessionListingIndex(log, clock=clock, submit=jobs)
    return SimpleNamespace(log=log, clock=clock, jobs=jobs, index=log._listing_index)


@pytest.fixture
def indexed(tmp_path) -> SimpleNamespace:
    d = _home(tmp_path)
    env = _attach(ConversationLog(base_dir=d))
    env.dir = d
    return env


def _assert_same(log: ConversationLog) -> None:
    # A second log, so a cache this log poisoned is not served to both sides.
    scan = ConversationLog(base_dir=log._dir)
    expected = scan.list_sessions()
    assert log.list_sessions_indexed() == expected
    for row in expected:
        key = row["key"]
        assert log.listing_has_messages(key) == scan.has_messages(key), key


def _reconciled(env: SimpleNamespace) -> None:
    env.clock.now += RECONCILE_SECS
    env.log.list_sessions_indexed()
    assert env.jobs.run() == 1
    _assert_same(env.log)


class _Count:
    def __init__(self) -> None:
        self.calls: list = []

    def wrap(self, real):
        def counted(*args, **kwargs):
            self.calls.append(args)
            return real(*args, **kwargs)

        return counted


def test_cold_build_matches_the_scan(indexed) -> None:
    log = indexed.log
    keys = [r["key"] for r in log.list_sessions_indexed()]
    assert "dashboard_dashboard_chat-5-5" in keys and "dashboard_chat-5-5" not in keys
    assert indexed.index.has_messages("dashboard_chat-3-3") is False
    assert indexed.index.has_messages("dashboard_chat-2-2") is True
    assert indexed.index.has_messages("cron_deep") is None
    _assert_same(log)


def test_a_handoff_alias_is_never_listed(indexed) -> None:
    try:
        os.symlink(indexed.dir / "dashboard_chat-1-1.jsonl", indexed.dir / "alias.jsonl")
    except (OSError, NotImplementedError):
        pytest.skip("this platform cannot create a symlink here")
    assert "alias" not in [r["key"] for r in indexed.log.list_sessions_indexed()]
    _assert_same(indexed.log)


def test_every_in_process_write_shows_at_once(indexed) -> None:
    log = indexed.log
    _assert_same(log)
    steps = [
        lambda: log.append("dashboard:chat-3-3", "user", "now it has a message"),
        lambda: log.set_title("dashboard:chat-2-2", "Renamed"),
        lambda: log.update_metadata("dashboard:chat-1-1", {"folder_id": "f1"}),
        lambda: log.append("dashboard:chat-8-8", "user", "a brand new chat"),
        lambda: log.delete_session("dashboard:chat-4-4"),
        lambda: log.rewrite_session("dashboard:chat-2-2", [{"role": "user", "content": "kept"}]),
        lambda: log.append("subagent:abc", "assistant", "more"),
        lambda: log.delete_session("dashboard:chat-8-8"),
        lambda: log.append("dashboard:chat-8-8", "user", "the same key, again"),
    ]
    for step in steps:
        step()
        _assert_same(log)
    assert indexed.jobs.pending == []


def test_rotation_shows_at_once(indexed, monkeypatch) -> None:
    import kiro_crew.history_rewrite as rewrite

    log = indexed.log
    _assert_same(log)
    monkeypatch.setattr(rewrite, "_facade_session_max_bytes", lambda: 400)
    monkeypatch.setattr(rewrite, "_facade_session_keep_lines", lambda: 2)
    for i in range(8):
        log.append("dashboard:chat-2-2", "user", f"line {i} " * 10)
        _assert_same(log)


def test_another_process_appending_in_place_shows_after_the_next_reconcile(indexed) -> None:
    log = indexed.log
    _assert_same(log)
    path = indexed.dir / "dashboard_chat-3-3.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"role": "user", "content": "from the CLI"}) + "\n")
    os.utime(path, ns=(_stamp(30), _stamp(30)))
    # No bump was recorded here: the documented window, bounded by the reconcile.
    assert log.list_sessions_indexed() != ConversationLog(base_dir=indexed.dir).list_sessions()
    assert indexed.jobs.pending == []
    _reconciled(indexed)


def test_listing_has_messages_rejects_an_index_answer_from_an_older_file(tmp_path) -> None:
    d = tmp_path / "s"
    d.mkdir()
    path = d / "dashboard_chat-1-1.jsonl"
    _write(path, [_META], _stamp(1))
    env = _attach(ConversationLog(base_dir=d))
    assert env.log.list_sessions_indexed()
    assert env.log.listing_has_messages("dashboard_chat-1-1") is False

    # Model another process, which cannot add an in-process journal record.
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"role": "assistant", "content": "outside write"}) + "\n")
    os.utime(path, ns=(_stamp(2), _stamp(2)))

    with patch.object(env.index, "_reread", wraps=env.index._reread) as rereads:
        assert env.log.listing_has_messages("dashboard_chat-1-1") is True
    assert rereads.call_count == 0
    assert env.index._entries["dashboard_chat-1-1"].has_messages is False
    assert env.jobs.pending == []


def test_another_process_creating_renaming_or_removing_shows_at_once(indexed) -> None:
    d = indexed.dir
    _assert_same(indexed.log)
    _write(d / "dashboard_chat-9-9.jsonl", [{**_META, "title": "External"}], _stamp(20))
    _assert_same(indexed.log)
    os.replace(d / "dashboard_chat-9-9.jsonl", d / "dashboard_chat-10-10.jsonl")
    _assert_same(indexed.log)
    (d / "dashboard_chat-10-10.jsonl").unlink()
    _assert_same(indexed.log)
    assert indexed.jobs.pending == []


def test_another_process_replacing_a_listed_file_shows_after_a_reconcile(indexed) -> None:
    d = indexed.dir
    _assert_same(indexed.log)
    _write(d / "replace.tmp", [{**_META, "title": "Replaced atomically"}], _stamp(21))
    os.replace(d / "replace.tmp", d / "dashboard_chat-1-1.jsonl")
    # A names pass reads only the files it has no row for: the documented window.
    assert indexed.log.list_sessions_indexed() != ConversationLog(base_dir=d).list_sessions()
    _reconciled(indexed)


def test_the_directory_stamp_is_trusted_only_once_it_settles(tmp_path, monkeypatch) -> None:
    d = _plain_dir(tmp_path, 5)
    os.utime(d, ns=(_BASE_NS, _BASE_NS))
    # One nanosecond after the directory's last change.
    now = [_BASE_NS + 1]
    monkeypatch.setattr(history_listing_index, "_wall_ns", lambda: now[0])
    env = _attach(ConversationLog(base_dir=d))
    scans = _Count()
    monkeypatch.setattr(SessionListingIndex, "_scan", scans.wrap(SessionListingIndex._scan))
    env.log.list_sessions_indexed()
    env.log.list_sessions_indexed()
    # The build, then a names pass: a file made in the same tick would not move the stamp.
    assert len(scans.calls) == 2
    now[0] = _BASE_NS + 10 * history_listing_index._DIR_STAMP_SETTLE_NS
    for _ in range(3):
        env.log.list_sessions_indexed()
    # One names pass records the settled stamp; the listings after it do not scan.
    assert len(scans.calls) == 3

    _write(d / "dashboard_chat-50-50.jsonl", [{**_META, "title": "Outside"}], _stamp(50))
    os.utime(d, ns=(_stamp(10), _stamp(10)))
    assert "dashboard_chat-50-50" in [r["key"] for r in env.log.list_sessions_indexed()]
    assert len(scans.calls) == 4
    assert env.jobs.pending == []
    _assert_same(env.log)


def test_steady_listing_op_budget(tmp_path, monkeypatch) -> None:
    """Per listing, before the reconcile is due: one stat of the directory, no scan, no row read."""
    d = tmp_path / "s"
    d.mkdir()
    for i in range(2000):
        _write(d / f"dashboard_chat-{i}-{i}.jsonl", [{**_META, "title": f"t{i}"}], _stamp(i))
    os.utime(d, ns=(_BASE_NS, _BASE_NS))
    env = _attach(ConversationLog(base_dir=d))
    env.log.list_sessions_indexed()
    stats, scans, rows = _Count(), _Count(), _Count()
    monkeypatch.setattr(os, "stat", stats.wrap(os.stat))
    monkeypatch.setattr(SessionListingIndex, "_scan", scans.wrap(SessionListingIndex._scan))
    monkeypatch.setattr(
        SessionCatalogProjection, "_listing_row", rows.wrap(SessionCatalogProjection._listing_row)
    )
    for _ in range(3):
        assert len(env.log.list_sessions_indexed()) == 2000
    assert (len(stats.calls), len(scans.calls), len(rows.calls)) == (3, 0, 0)
    assert {os.fspath(call[0]) for call in stats.calls} == {str(d)}
    assert env.jobs.pending == []

    env.clock.now += RECONCILE_SECS
    env.log.list_sessions_indexed()
    # Due: handed to the pool, not run on the request.
    assert (len(scans.calls), len(rows.calls), len(env.jobs.pending)) == (0, 0, 1)
    assert env.jobs.run() == 1
    assert (len(scans.calls), len(rows.calls)) == (1, 0)

    env.log.append("dashboard:chat-7-7", "user", "one write")
    env.log.list_sessions_indexed()
    # The re-read of the one row, and a names pass: the write's lock sentinel is a new
    # name in the directory.
    assert (len(scans.calls), len(rows.calls)) == (2, 1)


def test_the_build_fills_has_messages_for_untitled_rows(tmp_path, monkeypatch) -> None:
    """The first ``user_only`` filter after the build opens no transcript and takes no lock.

    The metadata and title LRUs are far smaller than the directory, as they are past
    ``_METADATA_CACHE_MAX`` transcripts.
    """
    d = tmp_path / "s"
    d.mkdir()
    for i in range(500):
        _write(d / f"dashboard_t-{i}.jsonl", [{**_META, "title": f"t{i}"}], _stamp(i))
        _write(
            d / f"dashboard_u-{i}.jsonl",
            [_META, {"role": "user", "content": f"q{i}"}],
            _stamp(500 + i),
        )
        stem = f"cron_job-{i}" if i % 2 else f"dashboard_m-{i}"
        _write(d / f"{stem}.jsonl", [_META], _stamp(1000 + i))
    log = ConversationLog(base_dir=d)
    log._meta_cache = _LRUCache(64)
    log._title_scan_cache = _LRUCache(64)
    _attach(log)
    rows = log.list_sessions_indexed()
    assert len(rows) == 1500

    opens = _Count()
    real_open = builtins.open
    monkeypatch.setattr(
        builtins,
        "open",
        lambda file, *a, **k: (
            opens.wrap(real_open)(file, *a, **k)
            if str(file).startswith(str(d))
            else real_open(file, *a, **k)
        ),
    )
    with (
        patch.object(log, "has_messages", wraps=log.has_messages) as reads,
        patch.object(log, "_locked", wraps=log._locked) as locks,
    ):
        listed = [r["key"] for r in rows if _is_user_listed_row(log, r)]
    assert (reads.call_count, locks.call_count, len(opens.calls)) == (0, 0, 0)
    assert len(listed) == 1000
    assert not [k for k in listed if k.startswith(("cron_", "dashboard_m-"))]


def _fail_scandir_once(monkeypatch: pytest.MonkeyPatch, directory) -> list[str]:
    """Make the next ``os.scandir`` of *directory* raise ``EMFILE``; returns the record."""
    failed: list[str] = []
    real_scandir = os.scandir

    def _scandir(path="."):
        if isinstance(path, (str, os.PathLike)) and os.fspath(path) == str(directory):
            if not failed:
                failed.append(os.fspath(path))
                raise OSError(errno.EMFILE, "Too many open files")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", _scandir)
    return failed


def test_a_scan_error_in_the_reconcile_keeps_the_previous_listing(indexed, monkeypatch) -> None:
    log, d = indexed.log, indexed.dir
    before = log.list_sessions_indexed()
    # A change a successful reconcile would show, and that leaves the directory's
    # names as they were, so no listing reads them.
    path = d / "dashboard_chat-3-3.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"role": "user", "content": "from the CLI"}) + "\n")
    os.utime(path, ns=(_stamp(30), _stamp(30)))
    failed = _fail_scandir_once(monkeypatch, d)
    indexed.clock.now += RECONCILE_SECS
    assert log.list_sessions_indexed() == before
    assert indexed.jobs.run() == 1
    assert failed
    rows = _Count()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            SessionCatalogProjection,
            "_listing_row",
            rows.wrap(SessionCatalogProjection._listing_row),
        )
        assert log.list_sessions_indexed() == before
    assert rows.calls == []
    # The next reconcile reads the directory again.
    _reconciled(indexed)


def test_a_scan_error_in_a_names_pass_keeps_the_rows_and_retries(indexed, monkeypatch) -> None:
    log, d = indexed.log, indexed.dir
    before = log.list_sessions_indexed()
    log.append("dashboard:chat-8-8", "user", "a new chat")
    failed = _fail_scandir_once(monkeypatch, d)
    assert log.list_sessions_indexed() == before
    assert failed
    _assert_same(log)


def test_a_missing_directory_empties_the_index(indexed) -> None:
    assert indexed.log.list_sessions_indexed()
    indexed.dir.rename(indexed.dir.with_name("moved"))
    indexed.clock.now += RECONCILE_SECS
    indexed.log.list_sessions_indexed()
    assert indexed.jobs.run() == 1
    assert indexed.log.list_sessions_indexed() == []


def test_the_reconcile_keeps_a_row_a_listing_re_read_meanwhile(indexed, monkeypatch) -> None:
    log, d, index = indexed.log, indexed.dir, indexed.index
    _assert_same(log)
    path = d / "dashboard_chat-3-3.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"role": "user", "content": "from the CLI"}) + "\n")
    os.utime(path, ns=(_stamp(30), _stamp(30)))
    real = index._stat_pass

    def _stat_then_write(known):
        result = real(known)
        # After the reconcile read the row, before it swaps the rows in.
        log.set_title("dashboard:chat-3-3", "Renamed here")
        log.list_sessions_indexed()
        return result

    monkeypatch.setattr(index, "_stat_pass", _stat_then_write)
    _reconciled(indexed)
    titles = {r["key"]: r["title"] for r in log.list_sessions_indexed()}
    assert titles["dashboard_chat-3-3"] == "Renamed here"


def test_the_reconcile_keeps_the_order_a_names_pass_found_meanwhile(indexed, monkeypatch) -> None:
    log, index = indexed.log, indexed.index
    _assert_same(log)
    real = index._stat_pass

    def _stat_then_create(known):
        result = real(known)
        log.append("dashboard:chat-20-20", "user", "created during the reconcile")
        log.list_sessions_indexed()
        return result

    monkeypatch.setattr(index, "_stat_pass", _stat_then_create)
    _reconciled(indexed)
    assert "dashboard_chat-20-20" in [r["key"] for r in log.list_sessions_indexed()]


def test_a_head_that_is_not_an_object_is_read_once_across_three_listings(
    tmp_path, monkeypatch
) -> None:
    d = _plain_dir(tmp_path, 3)
    (d / "dashboard_array.jsonl").write_text("[1, 2]\n", encoding="utf-8")
    env = _attach(ConversationLog(base_dir=d))
    rows = _Count()
    monkeypatch.setattr(
        SessionCatalogProjection, "_listing_row", rows.wrap(SessionCatalogProjection._listing_row)
    )
    for _ in range(3):
        titles = {r["key"]: r["title"] for r in env.log.list_sessions_indexed()}
        assert titles["dashboard_array"] == "dashboard_array"
    assert len([call for call in rows.calls if call[2] == "dashboard_array"]) == 1
    assert env.index._retry == set()
    # The scan does not memoize a listing that holds the row.
    row = SessionCatalogProjection._listing_row(
        env.log._catalog_projection, str(d / "dashboard_array.jsonl"), "dashboard_array", None
    )
    assert (row.volatile, row.reusable) == (False, False)
    _assert_same(env.log)


def test_rows_are_copies(indexed) -> None:
    log = indexed.log
    first = log.list_sessions_indexed()
    first[0]["preview"] = "written by a caller"
    first[0]["title"] = "changed by a caller"
    assert log.list_sessions_indexed() == ConversationLog(base_dir=indexed.dir).list_sessions()


def test_bulk_delete_does_not_rewrite_the_order_per_stem(tmp_path) -> None:
    d = tmp_path / "s"
    d.mkdir()
    for i in range(40):
        _write(d / f"dashboard_chat-{i}-{i}.jsonl", [{**_META, "title": f"t{i}"}], _stamp(i))
    os.utime(d, ns=(_BASE_NS, _BASE_NS))
    env = _attach(ConversationLog(base_dir=d))
    env.log.list_sessions_indexed()
    order = env.index._order
    for i in range(0, 40, 2):
        assert env.log.delete_session(f"dashboard_chat-{i}-{i}")
        # The directory stamp held still, so only the journal replay runs.
        os.utime(d, ns=(_BASE_NS, _BASE_NS))
        env.log.list_sessions_indexed()
    assert env.index._order is order
    _assert_same(env.log)


def _plain_dir(tmp_path, count: int):
    d = tmp_path / "s"
    d.mkdir()
    for i in range(count):
        _write(d / f"dashboard_chat-{i}-{i}.jsonl", [{**_META, "title": f"t{i}"}], _stamp(i))
    return d


def _journal(directory) -> int:
    return len(ConversationLog._dir_bumped_stems.get(str(directory), ()))


def test_the_bump_journal_holds_at_most_its_cap_and_loses_no_write(indexed, monkeypatch) -> None:
    cap = 8
    monkeypatch.setattr(history_cache, "_BUMP_JOURNAL_MAX", cap)
    log, base = indexed.log, str(indexed.dir)
    _assert_same(log)
    replayed = indexed.index._epoch
    # The oldest record, which the cap drops first, is a write to a row the index holds.
    log.set_title("dashboard:chat-2-2", "Renamed before the overflow")
    new_keys = [f"dashboard_chat-{100 + i}-{100 + i}" for i in range(3 * cap)]
    for key in new_keys:
        log.append(key, "user", "a new chat")
        assert _journal(indexed.dir) <= cap
    assert ConversationLog._dir_bump_floors[base] > replayed
    rows = {r["key"]: r for r in log.list_sessions_indexed()}
    assert rows["dashboard_chat-2-2"]["title"] == "Renamed before the overflow"
    assert set(new_keys) <= set(rows)
    _assert_same(log)
    # This index is the directory's only reader and has replayed every record.
    assert _journal(indexed.dir) == 0


def test_a_record_stays_until_every_index_over_the_directory_replayed_it(
    tmp_path, monkeypatch
) -> None:
    d = _plain_dir(tmp_path, 10)
    first = _attach(ConversationLog(base_dir=d))
    second = _attach(ConversationLog(base_dir=d))
    first.log.list_sessions_indexed()
    second.log.list_sessions_indexed()
    first.log.set_title("dashboard_chat-2-2", "Seen by both")
    first.log.list_sessions_indexed()
    assert _journal(d) > 0
    rows = _Count()
    monkeypatch.setattr(
        SessionCatalogProjection, "_listing_row", rows.wrap(SessionCatalogProjection._listing_row)
    )
    titles = {r["key"]: r["title"] for r in second.log.list_sessions_indexed()}
    assert titles["dashboard_chat-2-2"] == "Seen by both"
    # Replayed from the journal: the one row read again, no rebuild.
    assert len(rows.calls) == 1
    assert _journal(d) == 0


def test_a_delete_drops_the_row_and_create_delete_cycles_keep_only_the_files(tmp_path) -> None:
    d = _plain_dir(tmp_path, 10)
    env = _attach(ConversationLog(base_dir=d))
    env.log.list_sessions_indexed()
    assert env.log.delete_session("dashboard_chat-3-3")
    env.log.list_sessions_indexed()
    assert "dashboard_chat-3-3" not in env.index._entries
    for i in range(50):
        key = f"dashboard_chat-{100 + i}-{100 + i}"
        env.log.append(key, "user", "short-lived")
        assert key in [r["key"] for r in env.log.list_sessions_indexed()]
        assert env.log.delete_session(key)
        assert key not in [r["key"] for r in env.log.list_sessions_indexed()]
        assert key not in env.index._entries
    # The next pass re-reads the names, which drops every removed stem from the order.
    _reconciled(env)
    on_disk = sorted(p.stem for p in d.glob("*.jsonl"))
    index = env.index
    assert sorted(index._entries) == sorted(index._order) == sorted(index._safe_of) == on_disk
    assert sorted(s for stems in index._by_safe.values() for s in stems) == on_disk
    assert _journal(d) == 0


def test_over_the_ceiling_the_listing_is_the_direct_scan_and_warns_once(
    indexed, monkeypatch, caplog
) -> None:
    log, index, d = indexed.log, indexed.index, indexed.dir
    files = len(list(d.glob("*.jsonl")))
    monkeypatch.setattr(history_listing_index, "MAX_ROWS", files)
    caplog.set_level(logging.WARNING, logger=history_listing_index.__name__)
    _assert_same(log)
    assert index._built

    # An in-process create crosses the ceiling: the names pass drops every row.
    log.append("dashboard:chat-8-8", "user", "one past the ceiling")
    with patch.object(log, "list_sessions", wraps=log.list_sessions) as direct:
        for _ in range(3):
            _assert_same(log)
            # Past the retry interval: the retry counts the names again and stops again.
            indexed.clock.now += RECONCILE_SECS
        assert direct.call_count == 3
    assert (index._built, index._entries, index._order, index._by_safe) == (False, {}, [], {})
    assert index.has_messages("dashboard_chat-2-2") is None
    assert indexed.jobs.pending == []

    # Under the ceiling again, the next retry builds the rows.
    monkeypatch.setattr(history_listing_index, "MAX_ROWS", files + 1)
    _assert_same(log)
    assert index._built

    # Another process's create crosses it again: the next listing's names pass drops
    # every row.
    _write(d / "dashboard_chat-9-9.jsonl", [{**_META, "title": "External"}], _stamp(20))
    _assert_same(log)
    assert (index._built, index._entries) == (False, {})
    assert indexed.jobs.pending == []
    warnings = [
        r
        for r in caplog.records
        if r.name == history_listing_index.__name__ and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]


def _index_warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == history_listing_index.__name__ and r.levelno == logging.WARNING
    ]


def test_over_the_byte_budget_the_listing_is_the_direct_scan_and_warns_once(
    tmp_path, monkeypatch, caplog
) -> None:
    d = _plain_dir(tmp_path, 10)
    env = _attach(ConversationLog(base_dir=d))
    log, index = env.log, env.index
    caplog.set_level(logging.WARNING, logger=history_listing_index.__name__)
    _assert_same(log)
    held = index._bytes
    monkeypatch.setattr(history_listing_index, "MAX_RETAINED_BYTES", held + 1000)
    long_title = "a long title " * 100

    # A long title written here moves the running total past the budget.
    log.set_title("dashboard:chat-2-2", long_title)
    with patch.object(log, "list_sessions", wraps=log.list_sessions) as direct:
        for _ in range(3):
            _assert_same(log)
            # Past the retry interval: the retry builds, passes the budget, stops again.
            env.clock.now += RECONCILE_SECS
        assert direct.call_count == 3
    assert (index._built, index._entries, index._bytes) == (False, {}, 0)
    assert index.has_messages("dashboard_chat-2-2") is None
    assert env.jobs.pending == []
    titles = {r["key"]: r["title"] for r in log.list_sessions_indexed()}
    assert titles["dashboard_chat-2-2"] == long_title

    # Within the budget again, the next retry builds the rows.
    monkeypatch.setattr(history_listing_index, "MAX_RETAINED_BYTES", 10**9)
    env.clock.now += RECONCILE_SECS
    _assert_same(log)
    assert index._built

    # Another process's long title passes it again: the reconcile drops every row.
    monkeypatch.setattr(history_listing_index, "MAX_RETAINED_BYTES", index._bytes + 1000)
    _write(d / "dashboard_chat-3-3.jsonl", [{**_META, "title": long_title}], _stamp(20))
    env.clock.now += RECONCILE_SECS
    log.list_sessions_indexed()
    assert env.jobs.run() == 1
    assert (index._built, index._entries, index._bytes) == (False, {}, 0)
    _assert_same(log)
    assert len(_index_warnings(caplog)) == 1, _index_warnings(caplog)


def test_a_nested_title_is_charged_its_keys_and_containers(tmp_path, monkeypatch, caplog) -> None:
    d = _plain_dir(tmp_path, 10)
    env = _attach(ConversationLog(base_dir=d))
    log, index = env.log, env.index
    caplog.set_level(logging.WARNING, logger=history_listing_index.__name__)
    _assert_same(log)
    assert index._built
    # Room for one more row and 4,000 bytes of its JSON.
    monkeypatch.setattr(
        history_listing_index,
        "MAX_RETAINED_BYTES",
        index._bytes + history_listing_index._ROW_OVERHEAD + 4000,
    )

    # An imported transcript whose title is an object of long keys and nested
    # containers holding no string: about 9,000 bytes of JSON.
    title = {f"field-{i:03d}-" + "k" * 60: {"n": [i, None, True]} for i in range(100)}
    _write(d / "dashboard_chat-50-50.jsonl", [{**_META, "title": title}], _stamp(50))
    with patch.object(log, "list_sessions", wraps=log.list_sessions) as direct:
        for _ in range(3):
            _assert_same(log)
            # Past the retry interval: the retry builds, passes the budget, stops again.
            env.clock.now += RECONCILE_SECS
        assert direct.call_count == 3
    assert (index._built, index._entries, index._bytes) == (False, {}, 0)
    assert len(_index_warnings(caplog)) == 1, _index_warnings(caplog)


def _deep_size(value: object) -> int:
    """``sys.getsizeof`` of *value* and every object it holds, each counted once."""
    import sys

    seen: set[int] = set()
    stack = [value]
    total = 0
    while stack:
        obj = stack.pop()
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        total += sys.getsizeof(obj)
        if isinstance(obj, dict):
            stack.extend(obj.keys())
            stack.extend(obj.values())
        elif isinstance(obj, (list, tuple)):
            stack.extend(obj)
    return total


@pytest.mark.parametrize(
    "title",
    [
        [{} for _ in range(2000)],
        [[] for _ in range(2000)],
        [[[], []] for _ in range(1000)],
        [functools.reduce(lambda inner, _: [inner], range(500), []) for _ in range(20)],
        [[i] for i in range(2000)],
        ["a"] * 2000,
        list(range(2000)),
        {f"k{i:05d}": [] for i in range(1000)},
        "é" * 10_000,
        "a typical session title",
    ],
    ids=[
        "empty-dicts",
        "empty-lists",
        "nested-lists",
        "singleton-chains",
        "singletons",
        "short-strs",
        "ints",
        "keys",
        "latin-1-string",
        "plain",
    ],
)
def test_a_row_is_charged_at_least_what_it_holds(title) -> None:
    # The value a metadata line decoded to, as listing_row stores it.
    row = {
        "key": "dashboard_chat-1-1",
        "title": json.loads(json.dumps(title)),
        "messages": 3,
        "modified": 1.5,
        "created": "2026-11-03T00:00:00",
    }
    assert history_listing_index._retained_bytes(row) >= _deep_size(row)


@pytest.mark.parametrize(
    "extra",
    [
        {"title": "a" * 10_000 + "😀"},
        {"title": "a" * 10_000 + "漢"},
        {"metadata": {"nested": ["a" * 10_000 + "😀"]}},
        {"metadata": {"a" * 10_000 + "😀": "value"}},
    ],
    ids=["mixed-wide-title", "mixed-bmp-title", "nested-wide-value", "wide-key"],
)
def test_non_ascii_strings_are_charged_at_their_allocated_width(extra) -> None:
    row = {
        "key": "dashboard_chat-1-1",
        "title": "plain",
        "messages": 3,
        "modified": 1.5,
        "created": "2026-11-03T00:00:00",
        **json.loads(json.dumps(extra)),
    }
    assert history_listing_index._retained_bytes(row) >= _deep_size(row)


def test_a_build_over_the_byte_budget_stops_before_its_next_read(tmp_path, monkeypatch) -> None:
    d = _plain_dir(tmp_path, 10)
    # Room for two and a half of these rows, so the third read passes the budget.
    one_row = history_listing_index._retained_bytes(ConversationLog(base_dir=d).list_sessions()[0])
    monkeypatch.setattr(history_listing_index, "MAX_RETAINED_BYTES", one_row * 5 // 2)
    env = _attach(ConversationLog(base_dir=d))
    reads = _Count()
    monkeypatch.setattr(
        SessionListingIndex, "_make_entry", reads.wrap(SessionListingIndex._make_entry)
    )
    assert env.log.list_sessions_indexed() == ConversationLog(base_dir=d).list_sessions()
    assert not env.index._built
    assert len(reads.calls) == 3


def test_the_retained_total_follows_every_insert_replace_and_drop(tmp_path) -> None:
    env = _attach(ConversationLog(base_dir=_plain_dir(tmp_path, 10)))
    log, index = env.log, env.index

    def _total() -> int:
        log.list_sessions_indexed()
        assert index._built
        assert index._bytes == sum(e.size for e in index._entries.values())
        return index._bytes

    before = _total()
    # Inserted by a names pass, replaced by a re-read, dropped by a re-read.
    log.append("dashboard:chat-20-20", "user", "a new chat")
    inserted = _total()
    assert inserted > before
    log.set_title("dashboard:chat-20-20", "a longer title " * 20)
    assert _total() > inserted
    assert log.delete_session("dashboard:chat-20-20")
    assert _total() == before


def test_an_equal_re_read_keeps_one_row_object_per_entry(tmp_path) -> None:
    env = _attach(ConversationLog(base_dir=_plain_dir(tmp_path, 4)))
    log, index = env.log, env.index
    log.list_sessions_indexed()
    snapshot = index._snapshot
    for i in range(4):
        log._invalidate_cache(f"dashboard:chat-{i}-{i}")
    log.list_sessions_indexed()
    assert index._snapshot is snapshot
    held = {id(e.row) for e in index._entries.values()}
    assert {id(row) for row in snapshot} == held


def test_threaded_writers_and_reader_agree(indexed) -> None:
    log = indexed.log
    errors: list[BaseException] = []
    done = threading.Event()

    def _writer(n: int) -> None:
        try:
            for i in range(20):
                log.append(f"dashboard:chat-w{n}-1", "user", f"message {i}")
                if i % 5 == 0:
                    log.set_title(f"dashboard:chat-w{n}-1", f"writer {n} at {i}")
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    def _reader() -> None:
        try:
            while not done.is_set():
                for row in log.list_sessions_indexed():
                    log.listing_has_messages(row["key"])
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    reader = threading.Thread(target=_reader)
    writers = [threading.Thread(target=_writer, args=(n,)) for n in range(4)]
    reader.start()
    for thread in writers:
        thread.start()
    for thread in writers:
        thread.join(timeout=120)
    done.set()
    reader.join(timeout=120)
    assert [t for t in (*writers, reader) if t.is_alive()] == []
    assert errors == []
    _assert_same(log)


# ── the handler ────────────────────────────────────────────────────────────


def _request(log, query: dict[str, str], *, app: str = "") -> web.Request:
    state = MagicMock()
    state.conversation_log = log
    state._slots = {}
    request = MagicMock(spec=web.Request)
    request.get = ({"app": app} if app else {}).get
    request.app = {"state": state}
    request.query = query
    return request


async def _keys(request: web.Request) -> list[str]:
    resp = await api_sessions(request)
    assert resp.status == 200
    return [row["key"] for row in json.loads(resp.body.decode("utf-8"))["sessions"]]


@pytest.mark.asyncio
async def test_the_handler_reads_the_index(tmp_path) -> None:
    d = tmp_path / "s"
    d.mkdir()
    second = 10**9
    _write(d / "dashboard_titled.jsonl", [{**_META, "title": "Titled"}], _BASE_NS + second)
    used = [_META, {"role": "user", "content": "hi"}]
    _write(d / "dashboard_used.jsonl", used, _BASE_NS + 2 * second)
    _write(d / "dashboard_empty.jsonl", [_META], _BASE_NS + 3 * second)
    task = [_META, {"role": "user", "content": "task"}]
    _write(d / "subagent_x.jsonl", task, _BASE_NS + 4 * second)
    env = _attach(ConversationLog(base_dir=d))
    log = env.log
    log.list_sessions_indexed()
    with (
        patch.object(log, "list_sessions", side_effect=AssertionError("the dashboard scanned")),
        patch.object(log, "has_messages", wraps=log.has_messages) as reads,
    ):
        keys = await _keys(_request(log, {"user_only": "1"}))
    assert keys == ["dashboard_used", "dashboard_titled"]
    assert reads.call_count == 0


@pytest.mark.asyncio
async def test_the_app_path_reads_the_scan() -> None:
    log = MagicMock()
    log.list_sessions.return_value = []
    log.list_sessions_indexed.side_effect = AssertionError("an app listing read the index")
    assert await _keys(_request(log, {"user_only": "1"}, app="someapp")) == []
    log.list_sessions.assert_called_once_with()


@pytest.mark.parametrize("files", [600, 1200, 2400, 4704])
def test_a_listing_after_an_in_process_write_reads_one_row_at_any_size(
    tmp_path, monkeypatch, files: int
) -> None:
    """The callers moved to ``list_sessions_indexed`` pay for the file that changed only.

    The scan's memo is keyed on this process's write epoch, so every write here makes
    the next ``list_sessions()`` re-read a row per transcript: 4704 on the busy host the
    switch was measured on. The index replays the write journal and re-reads one.
    """
    d = tmp_path / "sessions"
    d.mkdir()
    for i in range(files):
        _write(d / f"dashboard_chat-{i}-{i}.jsonl", [{**_META, "title": f"t{i}"}], _stamp(i))
    os.utime(d, ns=(_stamp(files + 1), _stamp(files + 1)))
    env = _attach(ConversationLog(base_dir=d))
    env.log.list_sessions_indexed()
    env.log.list_sessions()
    rows = _Count()
    monkeypatch.setattr(
        SessionCatalogProjection, "_listing_row", rows.wrap(SessionCatalogProjection._listing_row)
    )

    env.log.append("dashboard:chat-0-0", "user", "one more line")
    assert len(env.log.list_sessions_indexed()) == files
    indexed_rows = len(rows.calls)
    rows.calls.clear()
    env.log.append("dashboard:chat-0-0", "user", "and another")
    assert len(env.log.list_sessions()) == files

    assert (indexed_rows, len(rows.calls)) == (1, files)
