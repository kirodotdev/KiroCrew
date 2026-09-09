"""The reclaim path must take the search index's copy of a session's text with it.

``delete_session`` is not the only way a transcript leaves live storage:
``session_storage.move_to_trash`` stages whole sessions for deletion, and emptying
the trash then destroys them. The index keeps a compressed copy of each indexed
session's message text, so a reclaim that ignored the index would report a purge it
did not complete -- the transcript gone, the text still readable in SQLite.

These tests exercise the helper that closes that gap rather than the whole reclaim
machinery: the helper is the seam, and driving a full reclaim would pull in the
live-session, age-threshold and co-tenant checks that have their own tests.
"""

from __future__ import annotations

import pytest

from kiro_crew import history_index, session_storage
from kiro_crew._sqlite_compat import fts5_available
from kiro_crew.history import ConversationLog
from kiro_crew.history_index import INDEX_FILENAME, SessionSearchIndex

pytestmark = pytest.mark.skipif(not fts5_available(), reason="SQLite built without FTS5")


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
