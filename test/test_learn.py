"""Tests for the lesson store module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from kiro_crew.learn import _DEFAULT_DIR, Lesson, LessonStore


def _make_lesson(rule: str, category: str = "knowledge", negative: str | None = None) -> Lesson:
    return Lesson(ts="2026-01-01T00:00:00Z", rule=rule, category=category, negative=negative)


class TestLessonStore:
    def test_save_and_load(self, tmp_path: Path) -> None:
        store = LessonStore(base_dir=tmp_path)
        store.save(_make_lesson("Use tool-b instead of tool-a", "tool", "Never use tool-a"))

        loaded = store.load_all()
        assert len(loaded) == 1
        assert "tool-b" in loaded[0].rule
        assert loaded[0].category == "tool"
        assert loaded[0].negative == "Never use tool-a"

    def test_remove_matching(self, tmp_path: Path) -> None:
        store = LessonStore(base_dir=tmp_path)
        store.save(_make_lesson("Use tool-b"))
        store.save(_make_lesson("Use tool-c"))
        assert store.remove("tool-b")
        assert len(store.load_all()) == 1
        assert "tool-c" in store.load_all()[0].rule

    def test_remove_no_match(self, tmp_path: Path) -> None:
        store = LessonStore(base_dir=tmp_path)
        store.save(_make_lesson("Use tool-b"))
        assert not store.remove("nonexistent")
        assert len(store.load_all()) == 1

    def test_get_context_empty(self, tmp_path: Path) -> None:
        store = LessonStore(base_dir=tmp_path)
        assert store.get_context() == ""

    def test_get_context_with_lessons(self, tmp_path: Path) -> None:
        store = LessonStore(base_dir=tmp_path)
        store.save(_make_lesson("Always use tool-b", negative="Never use tool-a"))

        ctx = store.get_context()
        assert "tool-b" in ctx
        assert "tool-a" in ctx
        assert "Learned corrections" in ctx

    def test_duplicate_with_new_negative_upgrades_the_stored_record(self, tmp_path: Path) -> None:
        """Re-teaching a rule WITH a negative repairs a record saved without
        one (e.g. written while the MCP path dropped the field) — instead of
        being skipped as a duplicate while the caller sees success."""
        store = LessonStore(base_dir=tmp_path)
        store.save(_make_lesson("always X"))  # no negative (pre-fix record)
        store.save(_make_lesson("always X", negative="never Y"))
        loaded = store.load_all()
        assert len(loaded) == 1, "still one record — upgraded, not duplicated"
        assert loaded[0].negative == "never Y"

    def test_duplicate_never_overwrites_an_existing_negative(self, tmp_path: Path) -> None:
        """Conflicting guidance is a curation decision, not last-writer-wins."""
        store = LessonStore(base_dir=tmp_path)
        store.save(_make_lesson("always X", negative="never Y"))
        store.save(_make_lesson("always X", negative="never Z"))
        loaded = store.load_all()
        assert len(loaded) == 1
        assert loaded[0].negative == "never Y"

    def test_plain_duplicate_still_skipped(self, tmp_path: Path) -> None:
        store = LessonStore(base_dir=tmp_path)
        store.save(_make_lesson("always X"))
        store.save(_make_lesson("always X"))
        assert len(store.load_all()) == 1

    def test_load_corrupted_line(self, tmp_path: Path) -> None:
        path = tmp_path / "lessons.jsonl"
        path.write_text("not json\n")
        store = LessonStore(base_dir=tmp_path)
        assert store.load_all() == []

    def test_multiple_saves(self, tmp_path: Path) -> None:
        store = LessonStore(base_dir=tmp_path)
        store.save(_make_lesson("Rule one"))
        store.save(_make_lesson("Rule two"))
        store.save(_make_lesson("Rule three"))
        assert len(store.load_all()) == 3


class TestLessonStoreSecurity:
    """Tests for sensitive path rejection and SEL audit in LessonStore."""

    def test_sensitive_base_dir_falls_back_to_default(self, tmp_path: Path) -> None:
        sensitive = tmp_path / ".ssh"
        sensitive.mkdir()
        with patch("kiro_crew.security.is_sensitive_path", return_value=True):
            store = LessonStore(base_dir=sensitive)
        assert store._dir == _DEFAULT_DIR

    def test_sensitive_base_dir_emits_sel_audit(self, tmp_path: Path) -> None:
        sensitive = tmp_path / ".aws"
        sensitive.mkdir()
        with (
            patch("kiro_crew.security.is_sensitive_path", return_value=True),
            patch("kiro_crew.sel.SecurityEventLog.log_tool_invocation") as mock_log,
        ):
            LessonStore(base_dir=sensitive)
        mock_log.assert_called_once()
        call_kwargs = mock_log.call_args[1]
        assert call_kwargs["outcome"] == "rejected"
        assert str(sensitive) in call_kwargs["resources"]

    def test_sel_failure_does_not_bypass_fallback(self, tmp_path: Path) -> None:
        sensitive = tmp_path / ".secret"
        sensitive.mkdir()
        with (
            patch("kiro_crew.security.is_sensitive_path", return_value=True),
            patch(
                "kiro_crew.sel.SecurityEventLog.log_tool_invocation",
                side_effect=RuntimeError("SEL broken"),
            ),
        ):
            store = LessonStore(base_dir=sensitive)
        assert store._dir == _DEFAULT_DIR

    def test_sensitive_config_dir_falls_back_to_default(self, tmp_path: Path) -> None:
        sensitive = tmp_path / ".kirocrew-sensitive"
        sensitive.mkdir()
        with (
            patch("kiro_crew.learn._config_dir", return_value=sensitive),
            patch("kiro_crew.security.is_sensitive_path", return_value=True),
        ):
            store = LessonStore()
        assert store._dir == _DEFAULT_DIR

    def test_config_dir_exception_falls_back_to_default(self) -> None:
        with patch("kiro_crew.learn._config_dir", side_effect=OSError("broken loader")):
            store = LessonStore()
        assert store._dir == _DEFAULT_DIR

    def test_config_dir_none_falls_back_to_default(self) -> None:
        with patch("kiro_crew.learn._config_dir", None):
            store = LessonStore()
        assert store._dir == _DEFAULT_DIR

    def test_non_sensitive_base_dir_used_directly(self, tmp_path: Path) -> None:
        with patch("kiro_crew.security.is_sensitive_path", return_value=False):
            store = LessonStore(base_dir=tmp_path)
        assert store._dir == tmp_path


class TestImportPurity:
    def test_importing_learn_never_calls_config_dir(self) -> None:
        # Single-point-migration invariant (PR #309): the one-time blocking
        # legacy-home migration fires ONLY at ensure_data_home() in the CLI
        # prologue. learn is eagerly imported by cli_server, slack/gateway,
        # context, taskrunner, and cli_commands — a module-scope config_dir()
        # call here would fire the migration as an import side effect
        # (potentially on the asyncio event loop). _DEFAULT_DIR must therefore
        # be a pure literal; config_dir() is resolved lazily in
        # LessonStore.__init__.
        import importlib
        from unittest.mock import patch

        import kiro_crew.learn as learn_mod

        with patch(
            "kiro_crew.config.loader.config_dir",
            side_effect=AssertionError("config_dir() called at learn import scope"),
        ):
            importlib.reload(learn_mod)
        # Restore the module to its normal state for other tests.
        importlib.reload(learn_mod)
        assert learn_mod._DEFAULT_DIR == Path.home() / ".kiro" / "crew"
