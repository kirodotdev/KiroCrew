"""The reclaim path must take the search index's copy of a session's text with it.

``delete_session`` is not the only way a transcript leaves live storage:
``session_storage.move_to_trash`` stages whole sessions for deletion, and emptying
the trash then destroys them. The index keeps a compressed copy of each indexed
session's message text, so a reclaim that ignored the index would report a purge it
did not complete -- the transcript gone, the text still readable in SQLite.

Most tests exercise the helper that closes that gap rather than the whole reclaim
machinery: the helper is the seam, and driving a full reclaim would pull in the
live-session, age-threshold and co-tenant checks that have their own tests. The
chat search cache tests at the end drive a full move and restore, since the gap
they cover sits between the helper and the file move.
"""

from __future__ import annotations

import os
import threading

import pytest
from session_storage_helpers import (  # noqa: F401 -- fixtures are requested by name
    _DAY,
    _NOW,
    _cli_half,
    _fresh_scan_cache,
    _index,
    stores_fixture,
)

from kiro_crew import chat_search_cache as csc
from kiro_crew import history_index, session_storage
from kiro_crew._sqlite_compat import fts5_available
from kiro_crew.dashboard import chat_history_cached as chc
from kiro_crew.dashboard.handlers.sessions import message_query
from kiro_crew.history import ConversationLog
from kiro_crew.history_index import INDEX_FILENAME, SessionSearchIndex
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

pytestmark = pytest.mark.skipif(not fts5_available(), reason="SQLite built without FTS5")


@pytest.fixture(autouse=True)
def _no_index_quiet_window(monkeypatch):
    """Zero the backfill quiet window: ``_seeded_log`` appends and indexes in the
    same breath, so the production deferral would skip both sessions and the
    ``remaining == 0`` seed assertion would fail before any test ran.
    """
    from kiro_crew import history_search

    monkeypatch.setattr(history_search, "_INDEX_QUIET_WINDOW_SECS", 0.0)


def _seeded_log(tmp_path):
    """A log with two indexed sessions, and the index that vouches for them."""
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.append("keep", "user", "a session about deployment")
    log.append("doomed", "user", "a session about contention")
    report = log._catalog_projection.backfill_index(budget_secs=30)
    assert report["remaining"] == 0
    return log


def test_reclaiming_a_session_removes_its_indexed_text(tmp_path, monkeypatch):
    log = _seeded_log(tmp_path)
    index = log._catalog_projection.search_index
    assert {"keep", "doomed"} <= index.indexed_keys()
    monkeypatch.setattr(session_storage, "_crew_sessions_dir", lambda: tmp_path / "sessions")

    assert session_storage._purge_search_index(["doomed"]) is True

    fresh = SessionSearchIndex(tmp_path / "sessions" / ".index" / INDEX_FILENAME)
    try:
        assert "doomed" not in fresh.indexed_keys()
        assert "keep" in fresh.indexed_keys()
    finally:
        fresh.close()


def test_reclaim_reports_refusal_when_the_copy_cannot_be_removed(tmp_path, monkeypatch):
    """Nothing has moved at this point, so refusing costs a retry and nothing else."""
    _seeded_log(tmp_path)
    monkeypatch.setattr(session_storage, "_crew_sessions_dir", lambda: tmp_path / "sessions")
    monkeypatch.setattr(SessionSearchIndex, "drop", lambda self, keys: False)

    assert session_storage._purge_search_index(["doomed"]) is False


def test_reclaim_reports_refusal_when_the_index_is_present_but_unreadable(tmp_path, monkeypatch):
    _seeded_log(tmp_path)
    monkeypatch.setattr(session_storage, "_crew_sessions_dir", lambda: tmp_path / "sessions")
    # The store comes up unavailable while its file is still on disk, which is the
    # case that must fail closed: the copy may be in there and nothing can remove it.
    monkeypatch.setattr(history_index, "fts5_available", lambda: False)

    assert session_storage._purge_search_index(["doomed"]) is False


def test_reclaim_does_not_create_an_index_that_does_not_exist(tmp_path, monkeypatch):
    """A reclaim must not bring an index into being; absent means no copy to remove."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(session_storage, "_crew_sessions_dir", lambda: sessions)

    assert session_storage._purge_search_index(["anything"]) is True

    assert not (sessions / ".index").exists()


def test_no_stems_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr(session_storage, "_crew_sessions_dir", lambda: tmp_path / "sessions")
    assert session_storage._purge_search_index([]) is True


FIRST, LATER = "dashboard:chat-1-100", "dashboard:chat-2-200"
SID = "aaaa1111"


def _trashable_tab(crew_home, kiro_home):
    """Two transcripts of one tab, aged past the reclaim floor; ``FIRST`` is SID's.

    ``LATER``'s chain reads ``FIRST``'s row first, so its cache holds that text too.
    """
    sessions = crew_home / "sessions"
    log = ConversationLog(base_dir=sessions)
    log.append(FIRST, "user", "the quincefruit note", tab_id="tabchain0001")
    log.append(LATER, "user", "a plum note", tab_id="tabchain0001")
    old = _NOW - 40 * _DAY
    for path in sessions.glob("*.jsonl"):
        os.utime(path, (old, old))
    _cli_half(kiro_home, SID, log_bytes=8, age_days=40)
    return log


def _trash_first():
    return session_storage.move_to_trash(
        [SID], reason="manual", index=_index({SID: csc._safe_key(FIRST)}), now=_NOW
    )


def _holding(store, text: str) -> list[str]:
    if not store.dir.exists():
        return []
    return [p.name for p in store.dir.iterdir() if text.encode() in p.read_bytes()]


def _build(store, log, key: str, seen: list) -> tuple[threading.Thread, dict]:
    def entry_of(msg, index):
        seen.append(msg)
        return chc.cache_entry(msg, index)

    out: dict = {}

    def run() -> None:
        try:
            out["result"] = store.update(log, key, entry_of)
        except BaseException as exc:  # noqa: BLE001 - handed to the test
            out["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, out


def test_no_cache_is_built_between_the_trash_drop_and_the_file_move(stores, monkeypatch):
    crew_home, kiro_home = stores
    log = _trashable_tab(crew_home, kiro_home)
    store = csc.store_for(crew_home / "sessions")
    paused, release = threading.Event(), threading.Event()
    real_move = session_storage._move_file

    def held_move(src, dst):
        # The cache drop is done; no file has moved yet.
        if not paused.is_set():
            paused.set()
            assert release.wait(FALLBACK_TIMEOUT_SECS)
        return real_move(src, dst)

    monkeypatch.setattr(session_storage, "_move_file", held_move)
    out: dict = {}
    mover = threading.Thread(target=lambda: out.update(batch=_trash_first()), daemon=True)
    mover.start()
    try:
        assert paused.wait(FALLBACK_TIMEOUT_SECS)
        assert log._path(FIRST).exists()
        for key in (FIRST, LATER):
            seen: list = []
            thread, built = _build(store, log, key, seen)
            thread.join(FALLBACK_TIMEOUT_SECS)
            assert not thread.is_alive()
            assert isinstance(built.get("error"), csc.CacheDropped), key
            # Refused before it read a row of the chat being moved.
            assert seen == []
            assert not store.path(key).exists()
    finally:
        release.set()
        mover.join(FALLBACK_TIMEOUT_SECS)

    assert out["batch"].moved == (SID,)
    assert not log._path(FIRST).exists()
    later = ConversationLog(base_dir=crew_home / "sessions")
    snap = store.update(later, LATER, chc.cache_entry)
    assert _hits(store, snap, "plum") == 1
    assert _holding(store, "quincefruit") == []


def test_a_chat_restored_from_trash_is_cached_again(stores):
    crew_home, kiro_home = stores
    _trashable_tab(crew_home, kiro_home)
    store = csc.store_for(crew_home / "sessions")
    batch = _trash_first()
    assert batch.moved == (SID,)

    assert session_storage.restore(batch.batch_id) == 1

    log = ConversationLog(base_dir=crew_home / "sessions")
    snap = store.update(log, FIRST, chc.cache_entry)
    assert store.path(FIRST).exists()
    assert _hits(store, snap, "quincefruit") == 1


def test_a_chat_the_move_to_trash_left_in_place_is_cached_again(stores, monkeypatch):
    crew_home, kiro_home = stores
    log = _trashable_tab(crew_home, kiro_home)
    store = csc.store_for(crew_home / "sessions")

    def no_move(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(session_storage, "_move_file", no_move)
    with pytest.raises(session_storage.SessionStorageError):
        _trash_first()

    assert log._path(FIRST).exists()
    snap = store.update(log, FIRST, chc.cache_entry)
    assert _hits(store, snap, "quincefruit") == 1


def _hits(store, snap, q: str) -> int:
    reply = chc.search_cached(
        store,
        snap,
        query=message_query(q),
        kind="",
        since=None,
        until=None,
        before=None,
        limit=30,
    )
    return reply["counts"][""]
