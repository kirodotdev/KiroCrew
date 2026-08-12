"""Tests for memory module."""

from __future__ import annotations

from kiro_crew import platform_compat
from kiro_crew.memory import MemoryStore


class TestMemoryStore:
    def test_init_creates_defaults(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.init()

        prefs = tmp_path / "memory" / "preferences.md"
        projects = tmp_path / "memory" / "projects.md"
        history_dir = tmp_path / "memory" / "history"
        assert prefs.exists()
        assert projects.exists()
        assert history_dir.is_dir()
        assert "Preferences" in prefs.read_text(encoding="utf-8")

    def test_read_returns_empty_when_missing(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        assert store.read() == ""

    def test_write_and_read(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.write("# My Memory\n\nI like lobsters.")
        assert "lobsters" in store.read()

    def test_get_context_empty_for_default(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.init()
        assert store.get_context() == ""

    def test_get_context_with_content(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.write_preferences("# User Preferences\n\n- dark mode\n")
        ctx = store.get_context()
        assert "[Memory" in ctx
        assert "dark mode" in ctx
        assert "[End of memory]" in ctx

    def test_init_does_not_overwrite(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.write_preferences("custom prefs")
        store.init()
        assert "custom prefs" in store.read_preferences()

    def test_preferences(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.add_preference("dark mode")
        store.add_preference("vim keybindings")
        store.add_preference("dark mode")  # duplicate
        prefs = store.read_preferences()
        assert prefs.count("dark mode") == 1
        assert "vim keybindings" in prefs

    def test_projects(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.write_projects("Building KiroCrew agent")
        projects = store.read_projects()
        assert "KiroCrew" in projects
        assert "Updated:" in projects

    def test_daily_history(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.append_history("Discussed cron scheduling")
        store.append_history("Fixed file locking bug")
        history = store.read_recent_history(days=1)
        assert "cron scheduling" in history
        assert "file locking" in history


class TestRecentHistoryCache:
    """read_recent_history TTL cache (per-message hot path)."""

    def test_repeated_reads_hit_cache(self, tmp_path, monkeypatch):
        """A second read within the TTL must not re-walk the history files."""
        store = MemoryStore(workspace=tmp_path)
        store.append_history("Discussed cron scheduling")

        calls = {"n": 0}
        orig = store._read_recent_history_uncached

        def _counting(days, today):
            calls["n"] += 1
            return orig(days, today)

        monkeypatch.setattr(store, "_read_recent_history_uncached", _counting)
        first = store.read_recent_history(days=1)
        for _ in range(4):
            store.read_recent_history(days=1)
        assert "cron scheduling" in first
        assert calls["n"] == 1  # only the first read walked the files

    def test_append_invalidates_cache(self, tmp_path):
        """A new entry must be visible on the next read despite the cache."""
        store = MemoryStore(workspace=tmp_path)
        store.append_history("first entry")
        assert "first entry" in store.read_recent_history(days=1)
        store.append_history("second entry")
        result = store.read_recent_history(days=1)
        assert "second entry" in result

    def test_distinct_days_arg_not_conflated(self, tmp_path):
        """Different ``days`` arguments must not serve each other's cached value."""
        store = MemoryStore(workspace=tmp_path)
        store.append_history("today entry")
        # days=0 short-circuits to "" before the cache; days=1 returns content.
        assert store.read_recent_history(days=0) == ""
        assert "today entry" in store.read_recent_history(days=1)

    def test_source_citations_in_context(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.write_preferences("# User Preferences\n\n- likes lobsters\n")
        ctx = store.get_context()
        assert "_[source:" in ctx
        assert "preferences.md" in ctx

    def test_fts_search(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.init()
        store.write_preferences("# Preferences\n\n- loves Python programming\n")
        store.append_history("Deployed the cron scheduler to production")
        store.rebuild_index()
        results = store.search("Python")
        assert len(results) >= 1
        assert "Python" in results[0]["snippet"] or "python" in results[0]["snippet"].lower()

    def test_fts_search_empty(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.init()
        store.rebuild_index()
        results = store.search("nonexistent_term_xyz")
        assert results == []

    def test_rebuild_index(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.init()
        store.append_history("entry one")
        store.append_history("entry two")
        count = store.rebuild_index()
        # preferences + projects + at least 1 history file
        assert count >= 3

    def test_write_projects_no_double_header(self, tmp_path):
        """BUG 6 regression: write_projects shouldn't double-wrap header."""
        store = MemoryStore(workspace=tmp_path)
        store.write_projects("# Active Projects\n\nKiroCrew agent")
        content = store.read_projects()
        assert content.count("# Active Projects") == 1

    def test_write_indexes_projects(self, tmp_path):
        """BUG 1 regression: legacy write() should update FTS index."""
        store = MemoryStore(workspace=tmp_path)
        store.init()
        store.write("# Memory\n\nlobster facts")
        store.rebuild_index()
        results = store.search("lobster")
        assert len(results) >= 1

    def test_get_context_with_history_only(self, tmp_path):
        """Context should include history even if prefs/projects are default."""
        store = MemoryStore(workspace=tmp_path)
        store.init()
        store.append_history("Deployed cron scheduler")
        ctx = store.get_context()
        assert "cron scheduler" in ctx

    def test_append_history_creates_date_file(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.append_history("test entry")
        from datetime import date

        today = date.today().isoformat()
        history_file = tmp_path / "memory" / "history" / f"{today}.md"
        assert history_file.exists()
        assert "test entry" in history_file.read_text(encoding="utf-8")

    def test_read_recent_history_respects_days(self, tmp_path):
        """Only returns history within the requested day range."""
        store = MemoryStore(workspace=tmp_path)
        store.append_history("today entry")
        # read_recent_history(days=0) should return nothing
        assert store.read_recent_history(days=0) == ""

    def test_fts_self_healing(self, tmp_path):
        """Corrupted DB should be auto-deleted and rebuilt."""
        store = MemoryStore(workspace=tmp_path)
        store.init()
        store.write_preferences("# Prefs\n\n- likes Python\n")
        store.rebuild_index()
        # Corrupt the DB
        db_path = tmp_path / "memory_index.db"
        if db_path.exists():
            db_path.write_bytes(b"corrupted data")
        # Should self-heal
        count = store.rebuild_index()
        assert count >= 1

    def test_add_preference_empty_string(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.add_preference("")
        prefs = store.read_preferences()
        # Empty pref should not add a blank bullet
        assert "\n- \n" not in prefs


_REAL_PROJECTS = "# Active Projects\n\n" + "".join(
    f"## Project {i}\n- owner: someone\n- status: in progress and being tracked\n\n"
    for i in range(12)
)


class TestDegenerateWriteGuard:
    """The consolidator asks an LLM for the whole file and writes the reply.

    A model that answers the "return existing content if nothing changed"
    instruction with a placeholder used to replace the entire file with that
    word.  Guarded writes must reject those replies.
    """

    def test_placeholder_body_rejected(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.write_projects(_REAL_PROJECTS, force=True)

        assert store.write_projects("unchanged") is False
        assert "Project 7" in store.read_projects()

    def test_placeholder_variants_rejected(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.write_projects(_REAL_PROJECTS, force=True)

        for body in (
            "Unchanged",
            "no changes",
            "(unchanged)",
            "N/A",
            "same as before",
            "unchanged.",
            "*unchanged*",
            "`no changes`",
            "Nothing changed!",
            "_N/A_",
            "nothing to update",
            "No changes required.",
            "no updates required",
        ):
            assert store.write_projects(body) is False, body
        assert "Project 7" in store.read_projects()

    def test_placeholder_under_header_rejected(self, tmp_path):
        """The wrapped form is just as destructive as the bare word."""
        store = MemoryStore(workspace=tmp_path)
        store.write_projects(_REAL_PROJECTS, force=True)

        assert store.write_projects("# Active Projects\n\n_Updated: 2026-08-05_\n\nunchanged") is False
        assert "Project 7" in store.read_projects()

    def test_empty_body_rejected(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.write_projects(_REAL_PROJECTS, force=True)

        assert store.write_projects("") is False
        assert store.write_projects("# Active Projects\n\n") is False
        assert "Project 7" in store.read_projects()

    def test_truncation_rejected(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.write_projects(_REAL_PROJECTS, force=True)

        assert store.write_projects("## Project 0\n- still here\n") is False
        assert "Project 7" in store.read_projects()

    def test_legitimate_prune_allowed(self, tmp_path):
        """Dropping some entries is normal consolidation, not a clobber."""
        store = MemoryStore(workspace=tmp_path)
        store.write_projects(_REAL_PROJECTS, force=True)

        pruned = "# Active Projects\n\n" + "".join(
            f"## Project {i}\n- owner: someone\n- status: in progress and being tracked\n\n"
            for i in range(8)
        )
        assert store.write_projects(pruned) is True
        assert "Project 7" in store.read_projects()
        assert "Project 11" not in store.read_projects()

    def test_force_allows_clearing(self, tmp_path):
        """An explicit human edit may shrink or clear the file."""
        store = MemoryStore(workspace=tmp_path)
        store.write_projects(_REAL_PROJECTS, force=True)

        assert store.write_projects("## Only one left\n", force=True) is True
        assert "Project 7" not in store.read_projects()

    def test_guard_does_not_block_first_write(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.init()
        assert store.write_projects("## Fresh start\n- doing things\n") is True
        assert "Fresh start" in store.read_projects()

    def test_preferences_guarded_too(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        big = "# User Preferences\n\n" + "".join(
            f"- preference number {i} that the user stated explicitly\n" for i in range(20)
        )
        store.write_preferences(big, force=True)

        assert store.write_preferences("unchanged") is False
        assert "preference number 17" in store.read_preferences()


class TestWriteLockIsBounded:
    """The lock must never block a caller indefinitely.

    A blocking ``flock`` would freeze whichever thread waits on it for as long
    as a wedged holder lives. Acquisition is non-blocking with a bounded retry,
    so a stuck holder makes the write fail closed (raises
    ``MemoryWriteLockTimeout``) rather than write unserialized.
    """

    def test_raises_when_lock_is_held_elsewhere(self, tmp_path, monkeypatch):
        import os
        import time

        import pytest

        from kiro_crew import memory as memory_mod

        store = MemoryStore(workspace=tmp_path)
        store.write_projects(_REAL_PROJECTS, force=True)

        monkeypatch.setattr(memory_mod, "_WRITE_LOCK_TIMEOUT_SECS", 0.2)

        # Hold the lock from an independent fd, as another process would.
        lock_path = tmp_path / "memory" / memory_mod._WRITE_LOCK_FILE
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        assert platform_compat.try_acquire_lock(fd, exclusive=True)
        try:
            started = time.monotonic()
            updated = _REAL_PROJECTS + "## Project 12\n- added under contention\n"
            # Fail closed: refuse rather than write unserialized.
            with pytest.raises(memory_mod.MemoryWriteLockTimeout):
                store.write_projects(updated)
            elapsed = time.monotonic() - started
        finally:
            platform_compat.release_lock(fd)
            os.close(fd)

        # Bounded: it gave up waiting (raised) rather than hanging.
        assert elapsed < 5.0
        # The contended write did not land.
        assert "added under contention" not in store.read_projects()

    def test_lock_is_released_after_write(self, tmp_path):
        import os

        from kiro_crew import memory as memory_mod

        store = MemoryStore(workspace=tmp_path)
        store.write_projects(_REAL_PROJECTS, force=True)

        lock_path = tmp_path / "memory" / memory_mod._WRITE_LOCK_FILE
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            # Nothing still holds it, so an outside acquire succeeds immediately.
            assert platform_compat.try_acquire_lock(fd, exclusive=True)
            platform_compat.release_lock(fd)
        finally:
            os.close(fd)

    """Concurrent sessions rewrite these files wholesale from their own view."""

    def test_stale_write_rejected(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.write_projects(_REAL_PROJECTS, force=True)

        snapshot = store.read_projects()  # what a session read before its LLM turn

        # A concurrent session lands its own update in the meantime.
        concurrent = snapshot + "\n## Project from other session\n- new work\n"
        assert store.write_projects(concurrent, force=True) is True

        # The first session's reply was computed from the stale snapshot.
        stale = snapshot + "\n## Project from first session\n- other work\n"
        assert store.write_projects(stale, expected=snapshot) is False

        current = store.read_projects()
        assert "Project from other session" in current
        assert "Project from first session" not in current

    def test_fresh_write_accepted(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.write_projects(_REAL_PROJECTS, force=True)

        snapshot = store.read_projects()
        updated = snapshot + "\n## Newly tracked project\n- just started\n"
        assert store.write_projects(updated, expected=snapshot) is True
        assert "Newly tracked project" in store.read_projects()

    def test_expected_none_skips_cas(self, tmp_path):
        """Callers that pass no expectation keep the old last-writer-wins path."""
        store = MemoryStore(workspace=tmp_path)
        store.write_projects(_REAL_PROJECTS, force=True)

        replacement = "# Active Projects\n\n" + "".join(
            f"## Replacement {i}\n- owner: someone else\n- status: also in progress\n\n"
            for i in range(12)
        )
        assert store.write_projects(replacement) is True
        assert "Replacement 3" in store.read_projects()
