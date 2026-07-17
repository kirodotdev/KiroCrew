"""Tests for memory module."""

from __future__ import annotations

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
        assert "Preferences" in prefs.read_text()

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
        assert "test entry" in history_file.read_text()

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
