"""Tests for memory module."""

from __future__ import annotations

import pytest

from conftest import plant_day_link
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


class TestNonUtf8Tolerance:
    """A stray non-UTF-8 byte in a memory file must not raise the reader.

    The PURE readers (``read_recent_history``, ``rebuild_index``) skip a
    corrupt file instead of raising ``UnicodeDecodeError`` — they never write
    their result back, so dropping an undecodable file is safe. The
    read-modify-write readers (``read_preferences``, ``read_projects``,
    ``append_history``) keep a strict decode: their value feeds a whole-file
    rewrite, so an undecodable file raises there and is left intact and
    recoverable rather than round-tripped lossily. No existing read path
    changes — only the decode failure is caught.
    """

    # An invalid UTF-8 lead byte (0xff never appears in valid UTF-8).
    BAD = b"# Prefs\n\n- good line \xff bad byte\n"

    def test_read_preferences_strict_leaves_corrupt_file_intact(self, tmp_path):
        """read_preferences feeds RMW callers, so it decodes strictly."""
        import pytest

        store = MemoryStore(workspace=tmp_path)
        store.init()
        store._preferences_file.write_bytes(self.BAD)
        with pytest.raises(UnicodeDecodeError):
            store.read_preferences()
        assert store._preferences_file.read_bytes() == self.BAD

    def test_read_projects_strict_leaves_corrupt_file_intact(self, tmp_path):
        """read_projects feeds RMW callers, so it decodes strictly."""
        import pytest

        store = MemoryStore(workspace=tmp_path)
        store.init()
        store._projects_file.write_bytes(self.BAD)
        with pytest.raises(UnicodeDecodeError):
            store.read_projects()
        assert store._projects_file.read_bytes() == self.BAD

    def test_read_recent_history_skips_corrupt_day(self, tmp_path):
        """A corrupt day file is skipped; a clean day still reads."""
        store = MemoryStore(workspace=tmp_path)
        store.append_history("CLEAN_ENTRY")
        from datetime import date, timedelta

        corrupt_day = (date.today() - timedelta(days=1)).isoformat()
        (store._history_dir / f"{corrupt_day}.md").write_bytes(b"# old \xff bad\n")
        out = store.read_recent_history(days=2)  # must not raise
        assert "CLEAN_ENTRY" in out

    def test_rebuild_index_skips_corrupt_file(self, tmp_path):
        """A corrupt history file is skipped; rebuild still succeeds.

        Trade-off: the corrupt file is left out of the FTS index until it is
        repaired. rebuild_index uses a single read with no reopen.
        """
        store = MemoryStore(workspace=tmp_path)
        store.init()
        store.append_history("CLEAN_ENTRY")
        from datetime import date, timedelta

        corrupt_day = (date.today() - timedelta(days=1)).isoformat()
        (store._history_dir / f"{corrupt_day}.md").write_bytes(b"# old \xff bad\n")
        count = store.rebuild_index()  # must not raise
        assert count >= 1
        # The clean entry is still indexed and searchable.
        assert store.search("CLEAN_ENTRY")

    def test_append_history_does_not_rewrite_corrupt_file_lossily(self, tmp_path):
        """A corrupt today-file must not be rewritten from a lossy decode.

        append_history is read-modify-write: a lossy read here would persist
        U+FFFD over the original bytes. It keeps a strict decode, so an
        undecodable today-file raises and is left intact and recoverable.
        """
        import pytest

        store = MemoryStore(workspace=tmp_path)
        store.append_history("first entry")
        from datetime import date

        day = store._history_dir / f"{date.today().isoformat()}.md"
        corrupt = day.read_bytes() + b"\ncorrupt \xff marker\n"
        day.write_bytes(corrupt)
        with pytest.raises(UnicodeDecodeError):
            store.append_history("second entry")
        assert day.read_bytes() == corrupt

    def test_get_context_survives_corrupt_preferences(self, tmp_path):
        """The every-turn context build must not raise on a bad preferences byte."""
        store = MemoryStore(workspace=tmp_path)
        store.init()
        store.append_history("CLEAN_ENTRY")
        store._preferences_file.write_bytes(self.BAD)
        # Startup path (include_activity=False) reads preferences only; must not raise.
        store.get_context(include_activity=False)
        # Full path reads preferences + projects + history; skips the corrupt
        # preferences section but still surfaces clean history.
        out = store.get_context(include_activity=True)
        assert "CLEAN_ENTRY" in out

    def test_activity_index_survives_corrupt_projects(self, tmp_path):
        """activity_index reads projects strictly-consumed; must not raise on a bad byte."""
        store = MemoryStore(workspace=tmp_path)
        store.init()
        store.append_history("CLEAN_ENTRY")
        store._projects_file.write_bytes(b"# Active Projects\n\n- proj \xff bad\n")
        out = store.activity_index()  # must not raise
        assert "CLEAN_ENTRY" in out
        assert "proj" not in out  # the corrupt projects section is skipped
        assert "good line" not in out  # the corrupt preferences file is skipped


class TestUnreadableActivityTolerance:
    def test_activity_context_skips_unreadable_history(self, tmp_path):
        """An unreadable history day costs that one day, not the whole window."""
        from datetime import date, timedelta

        store = MemoryStore(workspace=tmp_path)
        store.init()
        store.write_preferences("# User Preferences\n\n- PREF_SENTINEL\n")
        store.write_projects("PROJECT_SENTINEL")
        store.append_history("VALID_HISTORY_SENTINEL")
        unreadable_day = (date.today() - timedelta(days=1)).isoformat()
        (store._history_dir / f"{unreadable_day}.md").mkdir()

        history = store.read_recent_history(days=14)
        assert "VALID_HISTORY_SENTINEL" in history
        assert unreadable_day not in history

        activity = store.get_activity_context()
        context = store.get_context(include_activity=True)
        assert "PROJECT_SENTINEL" in activity
        assert "PREF_SENTINEL" in context
        assert "PROJECT_SENTINEL" in context
        assert "## Recent History" in activity
        assert "## Recent History" in context
        assert "VALID_HISTORY_SENTINEL" in activity
        assert "VALID_HISTORY_SENTINEL" in context

    def test_activity_context_skips_history_when_whole_window_is_unreadable(
        self, tmp_path, monkeypatch
    ):
        """A failure not tied to one day file drops only the history section."""
        import pytest

        store = MemoryStore(workspace=tmp_path)
        store.init()
        store.write_preferences("# User Preferences\n\n- PREF_SENTINEL\n")
        store.write_projects("PROJECT_SENTINEL")
        store.append_history("VALID_HISTORY_SENTINEL")

        def _unreadable(*args, **kwargs):
            raise PermissionError("history directory is unreadable")

        monkeypatch.setattr(store, "_read_recent_history_uncached", _unreadable)

        with pytest.raises(OSError):
            store.read_recent_history(days=14)

        activity = store.get_activity_context()
        context = store.get_context(include_activity=True)
        assert "PROJECT_SENTINEL" in activity
        assert "PREF_SENTINEL" in context
        assert "PROJECT_SENTINEL" in context
        assert "## Recent History" not in activity
        assert "## Recent History" not in context
        assert "VALID_HISTORY_SENTINEL" not in context

    def test_context_skips_unreadable_projects(self, tmp_path):
        """An unreadable projects file does not suppress preferences or history."""
        import pytest

        store = MemoryStore(workspace=tmp_path)
        store.init()
        store.write_preferences("# User Preferences\n\n- PREF_SENTINEL\n")
        store.append_history("HISTORY_SENTINEL")
        store._projects_file.unlink()
        store._projects_file.mkdir()

        with pytest.raises(OSError):
            store.read_projects()

        activity = store.get_activity_context()
        context = store.get_context(include_activity=True)
        assert "HISTORY_SENTINEL" in activity
        assert "PREF_SENTINEL" in context
        assert "HISTORY_SENTINEL" in context
        assert "## Active Projects" not in activity
        assert "## Active Projects" not in context

    def test_activity_context_skips_linked_history_day(self, tmp_path):
        """A linked history day cannot inject its target into memory context."""
        from datetime import date, timedelta

        store = MemoryStore(workspace=tmp_path)
        store.init()
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("SECRET_SENTINEL", encoding="utf-8")
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        plant_day_link(store._history_dir / f"{yesterday}.md", secret)
        store.append_history("VALID_HISTORY_SENTINEL")

        history = store.read_recent_history(days=14)
        assert "SECRET_SENTINEL" not in history
        assert "VALID_HISTORY_SENTINEL" in history

        activity = store.get_activity_context()
        context = store.get_context(include_activity=True)
        assert "SECRET_SENTINEL" not in activity
        assert "SECRET_SENTINEL" not in context
        assert "VALID_HISTORY_SENTINEL" in activity
        assert "VALID_HISTORY_SENTINEL" in context

    def test_crlf_history_day_reads_with_universal_newlines(self, tmp_path):
        """A day file with ``\\r\\n`` endings reads exactly as a text-mode read does."""
        from datetime import date, timedelta

        store = MemoryStore(workspace=tmp_path)
        store.init()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        (store._history_dir / f"{yesterday}.md").write_bytes(
            b"# day\r\n\r\n#### 10:00 UTC\r\nCRLF_SENTINEL\r\n"
        )

        history = store.read_recent_history(days=14)
        assert "CRLF_SENTINEL" in history
        assert "\r" not in history
        assert "# day\n\n#### 10:00 UTC\nCRLF_SENTINEL" in history

    def test_activity_context_skips_linked_projects_file(self, tmp_path):
        """A link planted at ``projects.md`` cannot put its target into the context."""
        store = MemoryStore(workspace=tmp_path)
        store.init()
        store.write_preferences("# User Preferences\n\n- PREF_SENTINEL\n")
        store.append_history("VALID_HISTORY_SENTINEL")
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("SECRET_SENTINEL", encoding="utf-8")
        store._projects_file.unlink()
        plant_day_link(store._projects_file, secret)

        activity = store.get_activity_context()
        context = store.get_context(include_activity=True)
        assert "SECRET_SENTINEL" not in activity
        assert "SECRET_SENTINEL" not in context
        assert "## Active Projects" not in activity
        assert "PREF_SENTINEL" in context
        assert "VALID_HISTORY_SENTINEL" in activity
        assert "VALID_HISTORY_SENTINEL" in context

    def test_crlf_projects_file_reads_with_universal_newlines(self, tmp_path):
        """A ``\\r\\n`` projects file renders its section without a carriage return."""
        store = MemoryStore(workspace=tmp_path)
        store.init()
        store._projects_file.write_bytes(b"# Active Projects\r\n\r\n- CRLF_PROJECT_SENTINEL\r\n")

        activity = store.get_activity_context()
        assert "## Active Projects" in activity
        assert "CRLF_PROJECT_SENTINEL" in activity
        assert "\r" not in activity


class TestHistoryDecayWording:
    PHRASE = "14 full days, then decayed summaries and counts to day 180"

    def test_every_history_decay_description_matches_the_tiers(self):
        """Every source and documentation copy states the exact decay boundary."""
        import ast
        import re
        from pathlib import Path

        repo = Path(__file__).resolve().parents[1]
        paths = list((repo / "src" / "kiro_crew").rglob("*.py"))
        paths.append(repo / "src" / "kiro_crew" / "docs" / "configuration.md")
        paths.extend((repo / "docs").rglob("*.md"))

        copies: list[tuple[str, str]] = []
        for path in sorted(set(paths)):
            source = path.read_text(encoding="utf-8")
            if path.suffix == ".py":
                candidates = (
                    node.value
                    for node in ast.walk(ast.parse(source))
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)
                )
            else:
                candidates = (source,)
            for candidate in candidates:
                normalized = re.sub(r"\s+", " ", candidate)
                start = 0
                while (index := normalized.find("14 full days", start)) >= 0:
                    copies.append(
                        (
                            path.relative_to(repo).as_posix(),
                            normalized[index : index + len(self.PHRASE)],
                        )
                    )
                    start = index + len("14 full days")

        assert copies, "no history decay descriptions found"
        assert all(copy == self.PHRASE for _, copy in copies), copies

    def test_history_decay_boundaries(self, tmp_path):
        """The full, summary, count, and exclusion tiers meet their stated ages."""
        from datetime import date, timedelta

        store = MemoryStore(workspace=tmp_path)
        store.init()
        today = date(2026, 1, 1)
        days = (0, 13, 14, 60, 61, 180, 181)
        for age in days:
            day = today - timedelta(days=age)
            (store._history_dir / f"{day.isoformat()}.md").write_text(
                f"# {day.isoformat()}\n\n"
                f"#### 10:00 UTC\nFIRST_SENTINEL_{age}\n\n"
                f"#### 11:00 UTC\nSECOND_SENTINEL_{age}\n",
                encoding="utf-8",
            )

        history = store._read_recent_history_uncached(days=14, today=today)

        for age in (0, 13):
            assert f"FIRST_SENTINEL_{age}" in history
            assert f"SECOND_SENTINEL_{age}" in history
        for age in (14, 60):
            assert f"FIRST_SENTINEL_{age}" in history
            assert f"SECOND_SENTINEL_{age}" not in history
        for age in (61, 180):
            day = today - timedelta(days=age)
            assert f"# {day.isoformat()}\n_2 conversation(s)_" in history
            assert f"FIRST_SENTINEL_{age}" not in history
            assert f"SECOND_SENTINEL_{age}" not in history
        excluded_day = today - timedelta(days=181)
        assert excluded_day.isoformat() not in history
        assert "FIRST_SENTINEL_181" not in history
        assert "SECOND_SENTINEL_181" not in history


class TestNamedV1StoreUnderFencedTree:
    """A NAMED V1 store's markdown lives under the fenced ``memory_stores`` tree.

    ``safe_read_file_bytes_nolink`` refuses any path ``is_sensitive_path``
    fences, so a named store's own day files would read as empty through it.
    The store therefore takes the descriptor-pinned reader for its admitted
    files; these tests pin that the fence leaves a named store readable while
    a planted link or hardlink at a day name is still refused.
    """

    NAME = "work"

    def _named_store(self, monkeypatch) -> MemoryStore:
        import json

        from kiro_crew import memory_stores
        from kiro_crew.config import loader as loader_mod
        from kiro_crew.config.loader import config_dir
        from kiro_crew.memory_stores import memory_index_path_for, memory_store_dir_for

        (config_dir() / "config.json").write_text(
            json.dumps({"memory_stores": {"default": {}, self.NAME: {}}}), encoding="utf-8"
        )
        loader_mod._invalidate_config_cache()
        monkeypatch.setattr(memory_stores, "_DECLARED_MEMO", None)
        workspace = memory_store_dir_for(self.NAME)
        workspace.mkdir(parents=True, exist_ok=True)
        store = MemoryStore(workspace=workspace, index_db=memory_index_path_for(self.NAME))
        assert store._memory_store_name == self.NAME
        store.init()
        return store

    @staticmethod
    def _fence(monkeypatch, root) -> None:
        """Make the nolink reader's sensitive-path check fence everything under *root*."""
        import os

        from kiro_crew import hooks

        fenced_root = os.path.realpath(str(root))

        def _fenced(path: str) -> bool:
            try:
                return os.path.commonpath([os.path.realpath(path), fenced_root]) == fenced_root
            except ValueError:
                return False

        monkeypatch.setattr(hooks, "is_sensitive_path", _fenced)

    def test_named_store_stays_readable_under_the_fence(self, tmp_path, monkeypatch):
        store = self._named_store(monkeypatch)
        store.write_preferences("# User Preferences\n\n- PREF_SENTINEL\n")
        store.write_projects("PROJECT_SENTINEL")
        self._fence(monkeypatch, store._workspace)

        from kiro_crew import hooks

        # Precondition: the nolink reader refuses the fenced store's own file.
        assert (
            hooks.safe_read_file_bytes_nolink(
                str(store._preferences_file), within_root=str(store._memory_dir)
            )
            is None
        )

        assert "PREF_SENTINEL" in store.read_preferences()
        assert "PROJECT_SENTINEL" in store.read_projects()
        store.append_history("H1")
        assert "H1" in store.read_recent_history(days=14)
        activity = store.get_activity_context()
        assert "PROJECT_SENTINEL" in activity
        assert "H1" in activity

    def test_linked_day_is_refused_under_the_fence(self, tmp_path, monkeypatch):
        from datetime import date, timedelta

        store = self._named_store(monkeypatch)
        self._fence(monkeypatch, store._workspace)
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("SECRET_SENTINEL", encoding="utf-8")
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        plant_day_link(store._history_dir / f"{yesterday}.md", secret)
        store.append_history("VALID_HISTORY_SENTINEL")

        history = store.read_recent_history(days=14)
        assert "SECRET_SENTINEL" not in history
        assert "VALID_HISTORY_SENTINEL" in history
        context = store.get_context(include_activity=True)
        assert "SECRET_SENTINEL" not in context
        assert "VALID_HISTORY_SENTINEL" in context

    def test_linked_day_is_refused_by_the_descriptor_open_alone(self, tmp_path, monkeypatch):
        """With the lstat leaf check neutralized, the descriptor path still refuses the link.

        On POSIX the planted day is a file symlink and the ``O_NOFOLLOW`` open
        refuses it. On Windows :func:`plant_day_link` plants a directory junction
        instead, and the following ``stat`` reports a directory, so the
        non-regular check refuses it before any open. Either way the target's
        bytes never reach the history.
        """
        from datetime import date, timedelta

        from kiro_crew import memory as memory_mod

        store = self._named_store(monkeypatch)
        self._fence(monkeypatch, store._workspace)
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("SECRET_SENTINEL", encoding="utf-8")
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        plant_day_link(store._history_dir / f"{yesterday}.md", secret)
        store.append_history("VALID_HISTORY_SENTINEL")
        monkeypatch.setattr(memory_mod, "is_link_or_junction", lambda _p: False)

        history = store.read_recent_history(days=14)
        assert "SECRET_SENTINEL" not in history
        assert "VALID_HISTORY_SENTINEL" in history

    @staticmethod
    def _stub_second_link(monkeypatch, planted) -> None:
        """Make ``os.fstat`` report ``st_nlink == 2`` for the inode at *planted*.

        ``_guarded_entry`` reads the link count only from the descriptor it
        opens (``os.fstat`` in ``_read_entry_bytes`` and in
        ``safe_read_file_bytes_nolink``); the by-name ``stat`` calls around the
        read check only the file type, mtime and size. Matching on
        ``(st_dev, st_ino)`` pins the stub to that one inode, so every other
        file the reader opens keeps its real link count.
        """
        import os

        real_fstat = os.fstat
        target = os.stat(planted)
        identity = (target.st_dev, target.st_ino)

        class _SecondLink:
            def __init__(self, wrapped):
                self._wrapped = wrapped

            st_nlink = 2

            def __getattr__(self, name):
                return getattr(self._wrapped, name)

        def _fstat(fd):
            result = real_fstat(fd)
            if (result.st_dev, result.st_ino) == identity:
                return _SecondLink(result)
            return result

        monkeypatch.setattr(os, "fstat", _fstat)

    @pytest.mark.parametrize("shape", ["real_hardlink", "stubbed_link_count"])
    def test_hardlinked_day_is_refused_under_the_fence(self, tmp_path, monkeypatch, shape):
        """A second name for an outside inode at a day name is refused by its link count.

        ``real_hardlink`` uses ``os.link``; where the filesystem or platform
        refuses one, it falls through to the ``stubbed_link_count`` shape so the
        assertion never skips. That shape writes the secret as a regular day file
        and stubs the descriptor's ``st_nlink`` to 2, which is the exact reading
        the guarded reader refuses.
        """
        import os
        from datetime import date, timedelta

        store = self._named_store(monkeypatch)
        self._fence(monkeypatch, store._workspace)
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("SECRET_SENTINEL", encoding="utf-8")
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        planted = store._history_dir / f"{yesterday}.md"
        linked = False
        if shape == "real_hardlink":
            try:
                os.link(secret, planted)
                linked = True
            except (OSError, NotImplementedError, AttributeError):
                linked = False
        if not linked:
            planted.write_text("SECRET_SENTINEL", encoding="utf-8")
            self._stub_second_link(monkeypatch, planted)
        store.append_history("VALID_HISTORY_SENTINEL")

        history = store.read_recent_history(days=14)
        assert "SECRET_SENTINEL" not in history
        assert "VALID_HISTORY_SENTINEL" in history
        context = store.get_context(include_activity=True)
        assert "SECRET_SENTINEL" not in context
        assert secret.read_text(encoding="utf-8") == "SECRET_SENTINEL"

    def test_default_store_history_reads_empty_under_the_same_fence(self, tmp_path, monkeypatch):
        """Control: the DEFAULT store keeps the nolink reader, so the fence empties its history."""
        store = MemoryStore(workspace=tmp_path / "ws")
        assert store._memory_store_name == ""
        store.init()
        store.write_preferences("# User Preferences\n\n- PREF_SENTINEL\n")
        store.append_history("VALID_HISTORY_SENTINEL")
        assert "VALID_HISTORY_SENTINEL" in store.read_recent_history(days=14)
        store._invalidate_history_cache()
        self._fence(monkeypatch, store._workspace)

        assert store.read_recent_history(days=14) == ""
        # The default store's preferences read is a plain strict read, not the
        # nolink reader, so the fence leaves the protected preferences in place.
        assert "PREF_SENTINEL" in store.read_preferences()
        assert "## Recent History" not in store.get_activity_context()
