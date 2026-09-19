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

    def test_dashboard_replacement_invalidates_cache(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.append_history("before replacement")
        baseline = store.read_recent_history(days=14)

        assert store.write_today_history(
            "# replacement", expected_baseline=baseline, validate_current=lambda _content: None
        )

        assert store.read_recent_history(days=14) == "# replacement"

    def test_dashboard_replacement_refuses_a_concurrent_append(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.append_history("before replacement")
        stale = store.read_recent_history(days=14)
        store.append_history("concurrent consolidation")
        current = store.read_recent_history(days=14)

        assert not store.write_today_history(
            "# stale replacement", expected_baseline=stale, validate_current=lambda _content: None
        )

        assert store.read_recent_history(days=14) == current
        assert "concurrent consolidation" in current

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


class TestActiveProjectsHeader:
    """One normalizer owns the "Active Projects" header contract.

    Three copies of the normalize-or-wrap branch grew separately: two in
    ``MemoryStore`` (the plain write and the validated one) and one in the
    dashboard handler that validates the document before handing it over. The
    copies live in different packages, so a change to one would leave the
    validated and unvalidated write paths disagreeing about the header with no
    test positioned to notice.
    """

    def test_content_without_the_header_gains_one(self):
        from kiro_crew.memory import normalize_projects_document

        out = normalize_projects_document("just some notes", today="2026-09-15")
        assert out.startswith("# Active Projects\n\n_Updated: 2026-09-15_\n\n")
        assert out.endswith("just some notes\n")

    def test_content_with_the_header_is_not_wrapped_again(self):
        from kiro_crew.memory import normalize_projects_document

        out = normalize_projects_document("# Active Projects\n\nnotes", today="2026-09-15")
        assert out.count("# Active Projects") == 1
        assert out == "# Active Projects\n\nnotes\n"

    def test_the_two_branches_trim_asymmetrically(self):
        """Preserved, not tidied: only the already-headed branch strips.

        The pre-consolidation copies wrote ``content.strip()`` when the header was
        already present but interpolated the RAW content when it was not, so
        leading and trailing whitespace survives in exactly one of the two
        branches. Trimming both would be a behaviour change riding along with a
        refactor.
        """
        from kiro_crew.memory import normalize_projects_document

        assert (
            normalize_projects_document("# Active Projects\n\nnotes  ", today="2026-09-15")
            == "# Active Projects\n\nnotes\n"
        )
        assert (
            normalize_projects_document("  notes  ", today="2026-09-15")
            == "# Active Projects\n\n_Updated: 2026-09-15_\n\n  notes  \n"
        )

    def test_the_injected_date_is_the_caller_s(self):
        """The date is a parameter so the two writes can share one clock read."""
        from kiro_crew.memory import normalize_projects_document

        out = normalize_projects_document("notes", today="1999-01-02")
        assert "_Updated: 1999-01-02_" in out

    def test_both_store_paths_agree_on_the_document(self, tmp_path):
        """The validated and unvalidated writes must produce the same bytes."""
        store = MemoryStore(workspace=tmp_path)
        store.write_projects("notes from the plain write")
        plain = store.read_projects()

        assert plain.count("# Active Projects") == 1
        assert "# Active Projects\n\n_Updated: " in plain
        assert plain.endswith("notes from the plain write\n")


class TestRecallSearchFallback:
    def test_default_search_still_requires_every_literal_term(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.write_projects("# Active Projects\nNotebookquartz migration")
        question = "What do we know about Notebookquartz?"
        assert store.search(question) == []
        rows = store.search(question, match_any=True)
        assert len(rows) == 1
        assert "Notebookquartz" in rows[0]["snippet"]

    def test_or_fallback_quotes_operators_and_preserves_limit(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.write_projects("# Active Projects\nNotebookquartz project")
        store.write_preferences("Notebookquartz preference")
        store.append_history("Notebookquartz milestone")
        assert len(store.search("Notebookquartz", limit=1, match_any=True)) == 1
        # These are words, not executable FTS operators or a wildcard query.
        assert store.search('" OR NOT NEAR *', match_any=True) == []
        assert store.search("   ", match_any=True) == []
        assert store.search("nonexistentquartz", match_any=True) == []

    def test_question_fallback_does_not_match_indexed_paths(self, tmp_path):
        store = MemoryStore(workspace=tmp_path / "pathonlyquartz")
        store.write_projects("# Active Projects\nActual notebook content")
        assert store.search("pathonlyquartz")
        assert store.search("pathonlyquartz", match_any=True) == []

    def test_natural_question_reaches_an_old_decision_sharing_two_terms(self, tmp_path):
        """The old first turn showed an older day's first line unconditionally.

        A natural question about it carries far more task terms than that one
        line shares, so majority coverage alone loses it. Two shared terms admit
        the line; a document sharing a single word still does not.
        """
        store = MemoryStore(workspace=tmp_path)
        store.write_projects("# Active Projects\nQuartzscope dashboard colors")
        old_day = store._history_dir / "2026-01-05.md"
        store._history_dir.mkdir(parents=True, exist_ok=True)
        old_day.write_text(
            "# 2026-01-05\nChose Terraform over CDK for the Quartzscope infra "
            "after the cost review (OLDCHOICE)\n",
            encoding="utf-8",
        )
        store.rebuild_index()
        question = "Why did we pick Terraform for Quartzscope? Infrastructure decision rationale"
        rows = store.search(question, match_any=True)
        assert [row["path"] for row in rows] == [str(old_day)]
        assert "OLDCHOICE" in rows[0]["snippet"]
        assert 0 < rows[0]["relevance"] <= 0.5
        # Majority matches still win outright when one exists.
        store.append_history("Terraform Quartzscope infrastructure decision rationale recorded")
        rows = store.search(question, match_any=True)
        assert len(rows) == 1 and "rationale recorded" in rows[0]["snippet"]
