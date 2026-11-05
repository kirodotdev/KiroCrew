"""A whole-directory session listing is reused briefly instead of rescanned.

A full listing stats every transcript in the store. With thousands of files and
several readers asking at once, those scans held the GIL long enough to starve
the gateway's event loop. These tests pin the memo: a repeat listing stats
nothing, every in-process write and every created file is seen at once, the
memo is shared across log instances, and callers cannot mutate it.
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

from kiro_crew import history as history_mod
from kiro_crew import history_listing_memo, history_search
from kiro_crew.history import ConversationLog


@pytest.fixture
def stats(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Count per-file stats a listing takes (the directory stat is not one)."""
    seen: list[str] = []
    real_scandir = os.scandir

    class _Counting:
        def __init__(self, entry: os.DirEntry[str]) -> None:
            self._entry = entry

        def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
            return getattr(self._entry, name)

        def stat(self, *a, **kw):  # type: ignore[no-untyped-def]
            if self._entry.name.endswith(".jsonl"):
                seen.append(self._entry.path)
            return self._entry.stat(*a, **kw)

    class _Scan:
        def __init__(self, path, *a, **kw) -> None:  # type: ignore[no-untyped-def]
            self._it = real_scandir(path, *a, **kw)

        def __enter__(self):  # type: ignore[no-untyped-def]
            return (_Counting(e) for e in self._it)

        def __exit__(self, *exc: object) -> None:
            self._it.close()

        def __iter__(self):  # type: ignore[no-untyped-def]
            return (_Counting(e) for e in self._it)

        def close(self) -> None:
            self._it.close()

    class _Os:
        """The listing's own ``os``: counts entry stats and takes the
        ``DirEntry.stat()`` branch on every platform. The stdlib module, which
        ``pathlib`` and the rest of the worker read, is left alone."""

        name = "posix"
        scandir = _Scan

        def __getattr__(self, attr: str):  # type: ignore[no-untyped-def]
            return getattr(os, attr)

    monkeypatch.setattr(history_search, "os", _Os())
    return seen


def _log(tmp_path) -> ConversationLog:
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.init()
    with history_mod.allow_on_loop_persist():
        log.append("a", "user", "first")
        log.append("b", "user", "second")
    return log


def test_a_repeat_listing_stats_no_transcript(tmp_path, stats) -> None:
    log = _log(tmp_path)
    first = log.list_sessions()
    scanned = len(stats)
    assert scanned == 2
    assert log.list_sessions() == first
    assert len(stats) == scanned


def test_another_log_over_the_same_directory_shares_the_memo(tmp_path, stats) -> None:
    log = _log(tmp_path)
    log.list_sessions()
    scanned = len(stats)
    other = ConversationLog(base_dir=tmp_path / "sessions")
    assert {row["key"] for row in other.list_sessions()} == {"a", "b"}
    assert len(stats) == scanned


def test_an_in_process_write_is_seen_at_once(tmp_path, stats) -> None:
    log = _log(tmp_path)
    log.list_sessions()
    with history_mod.allow_on_loop_persist():
        log.append("a", "user", "more")
    log.list_sessions()
    assert len(stats) == 4


def test_a_session_created_by_another_log_is_seen_at_once(tmp_path) -> None:
    log = _log(tmp_path)
    log.list_sessions()
    other = ConversationLog(base_dir=tmp_path / "sessions")
    with history_mod.allow_on_loop_persist():
        other.append("c", "user", "third")
    assert {row["key"] for row in log.list_sessions()} == {"a", "b", "c"}


def test_an_out_of_process_append_is_seen_after_the_ttl(tmp_path, stats, monkeypatch) -> None:
    log = _log(tmp_path)
    log.list_sessions()
    later = time.monotonic() + history_listing_memo.TTL_SECS + 1
    # The memo's own clock only; the stdlib ``time`` the worker shares keeps running.
    monkeypatch.setattr(history_listing_memo, "time", SimpleNamespace(monotonic=lambda: later))
    log.list_sessions()
    assert len(stats) == 4


def test_a_caller_cannot_change_the_memo(tmp_path) -> None:
    log = _log(tmp_path)
    for row in log.list_sessions():
        row["title"] = "mutated"
    assert {row["title"] for row in log.list_sessions()} == {"first", "second"}


def test_an_explicit_key_listing_is_never_answered_from_the_memo(tmp_path, stats) -> None:
    log = _log(tmp_path)
    log.list_sessions()
    rows = log.list_sessions(keys=["a"])
    assert [row["key"] for row in rows] == ["a"]
