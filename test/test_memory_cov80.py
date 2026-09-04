"""Coverage tests for kiro_crew.memory — module path helpers, legacy-migration
detection, the combined legacy read/write split, history pruning + decay, and the
FTS error-swallowing paths.

Companion to test_memory.py / test_memory_selfheal.py: those cover the happy paths
of MemoryStore, this one aims at the guard clauses and failure branches.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.memory import (
    MemoryStore,
    legacy_memory_present,
    memory_dir,
    memory_file,
    workspace_dir,
)


@pytest.fixture()
def fake_config_dir(tmp_path, monkeypatch):
    """Point kiro_crew.memory's config_dir() at an isolated tmp dir."""
    root = tmp_path / "cfg"
    root.mkdir()
    monkeypatch.setattr("kiro_crew.memory.config_dir", lambda: root)
    return root


class TestModulePathHelpers:
    def test_paths_derive_from_config_dir(self, fake_config_dir):
        assert workspace_dir() == fake_config_dir / "workspace"
        assert memory_dir() == fake_config_dir / "workspace" / "memory"
        assert memory_file() == fake_config_dir / "workspace" / "memory" / "preferences.md"


class TestLegacyMemoryPresent:
    def test_false_when_nothing_on_disk(self, fake_config_dir):
        assert legacy_memory_present() is False

    def test_false_for_header_only_markdown(self, fake_config_dir):
        md = fake_config_dir / "workspace" / "memory"
        md.mkdir(parents=True)
        (md / "preferences.md").write_text("# User Preferences\n\nno bullets here\n")
        (md / "projects.md").write_text("# Active Projects\n")
        assert legacy_memory_present() is False

    def test_true_for_preferences_bullet(self, fake_config_dir):
        md = fake_config_dir / "workspace" / "memory"
        md.mkdir(parents=True)
        (md / "preferences.md").write_text("# User Preferences\n\n- zzqqx mode\n")
        assert legacy_memory_present() is True

    def test_true_for_projects_bullet_when_preferences_is_empty(self, fake_config_dir):
        md = fake_config_dir / "workspace" / "memory"
        md.mkdir(parents=True)
        (md / "preferences.md").write_text("# User Preferences\n")
        (md / "projects.md").write_text("# Active Projects\n\n- wxyv project\n")
        assert legacy_memory_present() is True

    def test_true_for_any_history_file(self, fake_config_dir):
        history = fake_config_dir / "workspace" / "memory" / "history"
        history.mkdir(parents=True)
        (history / "2026-01-01.md").write_text("# 2026-01-01\n")
        assert legacy_memory_present() is True

    def test_history_dir_without_markdown_is_not_enough(self, fake_config_dir):
        history = fake_config_dir / "workspace" / "memory" / "history"
        history.mkdir(parents=True)
        (history / "notes.txt").write_text("ignored")
        assert legacy_memory_present() is False

    def test_true_for_nontrivial_lessons_file(self, fake_config_dir):
        (fake_config_dir / "lessons.jsonl").write_text('{"rule": "qqzz"}\n')
        assert legacy_memory_present() is True

    def test_tiny_lessons_file_is_not_enough(self, fake_config_dir):
        (fake_config_dir / "lessons.jsonl").write_text("{}\n")
        assert legacy_memory_present() is False


class TestLegacyCombinedReadWrite:
    def test_write_splits_on_projects_header(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.write("# User Preferences\n\n- qqzz\n\n# Active Projects\n\n- wxyv\n")

        prefs = store.read_preferences()
        projects = store.read_projects()
        assert "qqzz" in prefs
        assert "Active Projects" not in prefs
        # write() must not re-add a header/timestamp the way write_projects does.
        assert projects.startswith("# Active Projects")
        assert "_Updated:" not in projects

    def test_read_concatenates_both_sections(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.write("# User Preferences\n\n- qqzz\n\n# Active Projects\n\n- wxyv\n")

        combined = store.read()
        assert "qqzz" in combined
        assert "wxyv" in combined
        assert combined.index("qqzz") < combined.index("wxyv")


class TestPruneHistory:
    def test_returns_zero_when_history_dir_absent(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        assert store.prune_history(keep_days=30) == 0

    def test_deletes_only_files_older_than_cutoff(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.init()
        history = tmp_path / "memory" / "history"
        today = datetime.now().date()

        old = history / f"{(today - timedelta(days=400)).strftime('%Y-%m-%d')}.md"
        recent = history / f"{(today - timedelta(days=2)).strftime('%Y-%m-%d')}.md"
        bogus = history / "not-a-date.md"
        for f in (old, recent, bogus):
            f.write_text("# x\n")

        assert store.prune_history(keep_days=30) == 1
        assert not old.exists()
        assert recent.exists()
        # An unparseable stem is skipped, never deleted.
        assert bogus.exists()


class TestRecentHistoryDecay:
    def _write_day(self, history, days_ago: int, body: str) -> None:
        day = datetime.now().date() - timedelta(days=days_ago)
        (history / f"{day.strftime('%Y-%m-%d')}.md").write_text(body, encoding="utf-8")

    def test_full_then_summary_then_count(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.init()
        history = tmp_path / "memory" / "history"

        self._write_day(history, 0, "# today\n\n#### 10:00 UTC\nfresh-qqzz\n")
        self._write_day(history, 3, "")  # empty file is skipped entirely
        self._write_day(
            history,
            20,
            "# mid\n\n#### 09:00 UTC\nfirst-wxyv\n\n#### 10:00 UTC\nsecond-vvxx\n",
        )
        self._write_day(history, 100, "# ancient\n\n#### 08:00 UTC\na\n\n#### 09:00 UTC\nb\n")

        out = store.read_recent_history(days=14)

        # < days: verbatim
        assert "fresh-qqzz" in out
        # days <= i < 61: summarised to header + first entry + a "more entries" note
        assert "first-wxyv" in out
        assert "second-vvxx" not in out
        assert "1 more entries" in out
        # i >= 61: collapsed to a conversation count only
        assert "2 conversation(s)" in out
        assert "ancient" not in out.split("conversation(s)")[1]

    def test_summary_of_day_without_entries_has_no_more_note(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.init()
        history = tmp_path / "memory" / "history"
        self._write_day(history, 30, "# header-only-qqzz\n")

        out = store.read_recent_history(days=7)
        assert "header-only-qqzz" in out
        assert "more entries" not in out

    def test_zero_days_short_circuits(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.init()
        assert store.read_recent_history(days=0) == ""

    def test_read_history_uses_thirty_day_window(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.init()
        history = tmp_path / "memory" / "history"
        self._write_day(history, 5, "# recent\n\n#### 10:00 UTC\nqqzz-body\n")

        assert "qqzz-body" in store.read_history()


class TestFtsErrorPaths:
    """Every FTS entry point swallows a sqlite failure instead of propagating."""

    @staticmethod
    def _break_db(store) -> None:
        def _raise():
            raise sqlite3.OperationalError("no such table: memory_fts")

        store._get_db = _raise  # type: ignore[method-assign]

    def test_index_file_swallows_db_failure(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.init()
        self._break_db(store)

        store.write_preferences("# User Preferences\n\n- qqzz\n")
        assert "qqzz" in store.read_preferences()

    def test_rebuild_index_still_counts_files_when_db_fails(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.init()
        (tmp_path / "memory" / "history" / "2026-01-01.md").write_text("# 2026-01-01\n")
        self._break_db(store)

        # preferences.md + projects.md + one history file
        assert store.rebuild_index() == 3

    def test_search_returns_empty_list_when_db_fails(self, tmp_path):
        store = MemoryStore(workspace=tmp_path)
        store.init()
        self._break_db(store)

        assert store.search("qqzz") == []
