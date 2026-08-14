"""Tests for the lesson store module."""

from __future__ import annotations

import stat
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.atomic_write import replace_with_retry
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


class TestSaveOrEnrich:
    """Re-submitting a rule to attach a NOT-clause must store the clause.

    The duplicate check matched on the rule alone and returned before looking at
    ``negative``, so the clause was dropped and the route still answered 200.
    """

    def test_attaches_a_clause_to_an_existing_rule(self, tmp_path: Path) -> None:
        store = LessonStore(base_dir=tmp_path)
        store.save(_make_lesson("Pin the port", "tool"))

        assert store.save_or_enrich(_make_lesson("Pin the port", "tool", "Do not autopick")) == (
            "enriched"
        )

        records = store.load_all()
        assert len(records) == 1, "must enrich in place, not append a second record"
        assert records[0].negative == "Do not autopick"

    def test_replaces_an_existing_clause(self, tmp_path: Path) -> None:
        store = LessonStore(base_dir=tmp_path)
        store.save(_make_lesson("Pin the port", "tool", "Old reason"))

        assert store.save_or_enrich(_make_lesson("Pin the port", "tool", "New reason")) == (
            "enriched"
        )
        assert [le.negative for le in store.load_all()] == ["New reason"]

    def test_a_bare_resubmit_never_strips_a_stored_clause(self, tmp_path: Path) -> None:
        """No clause supplied must not blank one that is already stored."""
        store = LessonStore(base_dir=tmp_path)
        store.save(_make_lesson("Pin the port", "tool", "Do not autopick"))

        assert store.save_or_enrich(_make_lesson("Pin the port", "tool")) == "unchanged"
        assert [le.negative for le in store.load_all()] == ["Do not autopick"]

    def test_inserts_when_no_rule_matches(self, tmp_path: Path) -> None:
        store = LessonStore(base_dir=tmp_path)
        assert store.save_or_enrich(_make_lesson("Pin the port", "tool")) == "inserted"
        assert len(store.load_all()) == 1

    def test_requires_an_exact_rule_not_a_superset(self, tmp_path: Path) -> None:
        """A stored rule that merely CONTAINS the submitted one is a different lesson."""
        store = LessonStore(base_dir=tmp_path)
        store.save(_make_lesson("Pin the port in every environment", "tool"))

        assert store.save_or_enrich(_make_lesson("Pin the port", "tool", "Do not autopick")) == (
            "inserted"
        )

        records = store.load_all()
        assert len(records) == 2
        assert records[0].negative is None, "the superset record must be untouched"

    def test_distinct_words_differing_only_by_sharp_s_are_not_conflated(
        self, tmp_path: Path
    ) -> None:
        """"Maße" (dimensions) and "Masse" (mass) are DIFFERENT rules. casefold() maps ß
        to ss, so under it these compared equal and a clause submitted for "Masse"
        attached itself to the stored "Maße" while the intended lesson was never
        created -- the wrong rule enriched, the right one discarded. lower() keeps them
        distinct."""
        store = LessonStore(base_dir=tmp_path)
        store.save(_make_lesson("Ma\u00dfe", "tool"))

        store.save_or_enrich(_make_lesson("Masse", "tool", "Do not confuse with volume"))

        records = store.load_all()
        rules = {le.rule for le in records}
        assert rules == {"Ma\u00dfe", "Masse"}, f"distinct rules were conflated: {rules}"
        stored_masse = next(le for le in records if le.rule == "Masse")
        assert stored_masse.negative == "Do not confuse with volume"
        stored_masze = next(le for le in records if le.rule == "Ma\u00dfe")
        assert stored_masze.negative is None, "the clause landed on the wrong rule"

    def test_a_sharp_s_case_variant_inserts_rather_than_enriching(self, tmp_path: Path) -> None:
        """The cost of choosing lower(): a stored "Straße" and a submitted "STRASSE" no
        longer compare equal, so this inserts a second row instead of enriching. That is
        the deliberate trade -- a missed enrichment is recoverable, conflating "Maße"
        with "Masse" is not. ASCII case variants still enrich (see the test above this
        one in the class)."""
        store = LessonStore(base_dir=tmp_path)
        store.save(_make_lesson("Stra\u00dfe", "tool"))

        store.save_or_enrich(_make_lesson("STRASSE", "tool", "Do not misspell"))

        records = store.load_all()
        assert len(records) == 2, f"expected the miss, not a conflation: {records}"
        assert {le.rule for le in records} == {"Stra\u00dfe", "STRASSE"}
        stored_original = next(le for le in records if le.rule == "Stra\u00dfe")
        assert stored_original.negative is None, "the clause must not land on the ß row"

    def test_idempotent_when_the_clause_is_already_present(self, tmp_path: Path) -> None:
        store = LessonStore(base_dir=tmp_path)
        store.save(_make_lesson("Pin the port", "tool", "Do not autopick"))

        assert store.save_or_enrich(
            _make_lesson("Pin the port", "tool", "Do not autopick")
        ) == "unchanged"
        assert len(store.load_all()) == 1

    def test_a_whitespace_only_clause_never_overwrites_a_stored_one(self, tmp_path: Path) -> None:
        """Same defect class as the vector store's: a truthy blank clause replaced a
        real stored one. Both stores are reached directly by the CLI, the route,
        consolidation and the task runner, so both normalise."""
        store = LessonStore(base_dir=tmp_path)
        store.save_or_enrich(_make_lesson("Pin the port", "tool", "Do not autopick"))

        assert store.save_or_enrich(_make_lesson("Pin the port", "tool", "   ")) == "unchanged"
        assert [le.negative for le in store.load_all()] == ["Do not autopick"]

    def test_a_whitespace_only_clause_is_not_stored_on_insert(self, tmp_path: Path) -> None:
        store = LessonStore(base_dir=tmp_path)
        assert store.save_or_enrich(_make_lesson("Pin the port", "tool", "  \t ")) == "inserted"
        assert [le.negative for le in store.load_all()] == [None], "blanks were persisted"

    def test_a_non_string_clause_does_not_crash_the_write(self, tmp_path: Path) -> None:
        """Same defect class as the vector store's: consolidation hands over the LLM's own
        value, so .strip() on an int would abort the run with AttributeError."""
        store = LessonStore(base_dir=tmp_path)
        assert store.save_or_enrich(_make_lesson("Pin the port", "tool", 123)) == "inserted"
        assert [le.negative for le in store.load_all()] == [None]

    def test_a_non_string_clause_never_overwrites_a_stored_one(self, tmp_path: Path) -> None:
        store = LessonStore(base_dir=tmp_path)
        store.save_or_enrich(_make_lesson("Pin the port", "tool", "Do not autopick"))

        assert store.save_or_enrich(_make_lesson("Pin the port", "tool", 123)) == "unchanged"
        assert [le.negative for le in store.load_all()] == ["Do not autopick"]

    def test_save_still_returns_none(self, tmp_path: Path) -> None:
        """save() delegates but keeps its signature, so existing callers are unaffected."""
        store = LessonStore(base_dir=tmp_path)
        assert store.save(_make_lesson("Pin the port", "tool")) is None

    def test_save_does_not_overwrite_a_stored_clause(self, tmp_path: Path) -> None:
        """An AUTOMATIC writer must not replace a clause a human authored.

        save() is reached by consolidation, task-runner extraction and onboarding
        import. If it enriched, any of those arriving with its own negative for an
        already-stored rule would silently replace the human's wording. Only
        explicit refinement -- the route and `learn add` -- may enrich.
        """
        store = LessonStore(base_dir=tmp_path)
        store.save_or_enrich(_make_lesson("Pin the port", "tool", "Human wrote this"))

        store.save(_make_lesson("Pin the port", "tool", "Machine wrote this"))

        records = store.load_all()
        assert len(records) == 1
        assert records[0].negative == "Human wrote this", "an automatic writer overwrote a human"

    def test_save_still_skips_a_plain_duplicate(self, tmp_path: Path) -> None:
        store = LessonStore(base_dir=tmp_path)
        store.save(_make_lesson("Pin the port", "tool"))
        store.save(_make_lesson("Pin the port", "tool"))
        assert len(store.load_all()) == 1

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX mode bits are not meaningful on Windows: os.chmod only toggles the "
        "read-only flag, so the file reports 0o666 whatever mode was requested",
    )
    def test_a_restrictive_store_mode_survives_a_write(self, tmp_path: Path) -> None:
        """Swapping the inode used to drop the store's permissions.

        write_text reused the existing inode, so a 0600 store stayed 0600 implicitly.
        A temp-file rename installs a NEW inode carrying umask permissions (0644
        under the usual 0022), which would widen a private lessons file on the next
        save. The mode is now read off the destination and reapplied.
        """
        store = LessonStore(base_dir=tmp_path)
        store.save(_make_lesson("Pin the port", "tool"))
        store._path.chmod(0o600)

        store.save_or_enrich(_make_lesson("Pin the port", "tool", "Do not autopick"))

        assert stat.S_IMODE(store._path.stat().st_mode) == 0o600, "permissions widened"

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX mode bits are not meaningful on Windows (reports 0o666 regardless)",
    )
    def test_a_new_store_is_not_world_readable(self, tmp_path: Path) -> None:
        """Lesson text is personal content; a store created fresh defaults to 0600."""
        store = LessonStore(base_dir=tmp_path)
        store.save(_make_lesson("Pin the port", "tool"))

        assert stat.S_IMODE(store._path.stat().st_mode) == 0o600

    def test_the_rename_goes_through_the_windows_retry_helper(self, tmp_path: Path) -> None:
        """A bare os.replace raises PermissionError when Windows Search or AV holds a
        handle. The repo's replace_with_retry exists for exactly that; this pins that
        the store's write routes through it rather than hand-rolling the rename."""
        store = LessonStore(base_dir=tmp_path)
        with patch(
            "kiro_crew.atomic_write.replace_with_retry", wraps=replace_with_retry
        ) as spy:
            store.save(_make_lesson("Pin the port", "tool"))
        assert spy.called, "the write bypassed replace_with_retry"


class TestConcurrentClauseAttach:
    """One lock acquisition, not two.

    Doing this as enrich-then-insert took the lock twice. Two concurrent posts of
    the same rule with different clauses interleaved in the gap: both enrich checks
    missed, the first insert appended, the second saw a duplicate and skipped -- so
    one clause was lost behind a 200.
    """

    def test_concurrent_attaches_never_lose_a_record(self, tmp_path: Path) -> None:
        store = LessonStore(base_dir=tmp_path)
        outcomes: list[str] = []
        barrier = threading.Barrier(8)

        def _attach(i: int) -> None:
            barrier.wait()  # maximise overlap inside the critical section
            outcomes.append(store.save_or_enrich(_make_lesson("Pin the port", "tool", f"no {i}")))

        threads = [threading.Thread(target=_attach, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        records = store.load_all()
        assert len(records) == 1, f"the same rule must collapse to one record, got {records}"
        # Exactly one writer inserts; every other either enriches or finds its own
        # clause already stored. A dropped clause would show up as a record whose
        # negative is None despite every writer supplying one.
        assert outcomes.count("inserted") == 1, f"expected a single insert, got {outcomes}"
        assert records[0].negative is not None, "a clause was dropped"
        assert records[0].negative.startswith("no ")

    def test_concurrent_save_and_remove_do_not_lose_the_write(self, tmp_path: Path) -> None:
        """remove() used to rewrite the file with no lock at all, so a concurrent
        save could be lost outright."""
        store = LessonStore(base_dir=tmp_path)
        store.save(_make_lesson("Keep me", "tool"))
        barrier = threading.Barrier(2)

        def _add() -> None:
            barrier.wait()
            store.save_or_enrich(_make_lesson("Added under contention", "tool"))

        def _drop() -> None:
            barrier.wait()
            store.remove("nothing matches this")

        threads = [threading.Thread(target=_add), threading.Thread(target=_drop)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        rules = {le.rule for le in store.load_all()}
        assert rules == {"Keep me", "Added under contention"}, f"lost a write: {rules}"

    def test_separate_instances_on_one_file_share_a_lock(self, tmp_path: Path) -> None:
        """Two instances over the same file must serialize against each other.

        DashboardState.lessons and context.get_lessons_for() build separate instances
        over the same global store. With a per-instance lock they serialized against
        nothing, so a dashboard refinement racing a consolidation write could lose a
        clause -- and both composed the same pid-named temp file, so one os.replace
        consumed the other's.
        """
        a = LessonStore(base_dir=tmp_path)
        b = LessonStore(base_dir=tmp_path)
        assert a._lock is b._lock, "instances on one file must share the lock"

        different = LessonStore(base_dir=tmp_path / "other")
        assert different._lock is not a._lock, "a different file must not share it"

    def test_concurrent_writes_from_separate_instances_lose_nothing(
        self, tmp_path: Path
    ) -> None:
        stores = [LessonStore(base_dir=tmp_path) for _ in range(6)]
        barrier = threading.Barrier(len(stores))

        def _add(i: int) -> None:
            barrier.wait()
            stores[i].save_or_enrich(_make_lesson(f"rule {i}", "tool"))

        threads = [threading.Thread(target=_add, args=(i,)) for i in range(len(stores))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        rules = {le.rule for le in stores[0].load_all()}
        assert rules == {f"rule {i}" for i in range(len(stores))}, f"lost a write: {rules}"
        assert list(tmp_path.rglob("*.tmp")) == [], "a temp file survived"


class TestAtomicWrite:
    """The live file is swapped, never written in place.

    load_all() reads without the lock, so a partial write would be observable, and
    a crash mid-write could truncate the store.
    """

    def test_leaves_no_temp_file_behind(self, tmp_path: Path) -> None:
        store = LessonStore(base_dir=tmp_path)
        store.save(_make_lesson("Pin the port", "tool"))
        store.save_or_enrich(_make_lesson("Pin the port", "tool", "Do not autopick"))
        store.remove("Pin")

        strays = list(tmp_path.rglob("*.tmp"))
        assert strays == [], f"os.replace should consume the temp file, found {strays}"

    def test_a_failed_swap_leaves_the_store_and_cache_intact(self, tmp_path: Path) -> None:
        """A failed install must not leave the clause visible via the cache either.

        load_all() returns the cached objects, so an in-place mutation followed by a
        failed write would advertise an unpersisted clause to every later reader --
        including context injection.
        """
        store = LessonStore(base_dir=tmp_path)
        store.save(_make_lesson("Pin the port", "tool"))
        store.load_all()  # prime the cache

        # Patched inside atomic_write, which is where the rename now happens. This is
        # the real seam: learn.py no longer touches os itself.
        with patch(
            "kiro_crew.atomic_write.replace_with_retry", side_effect=OSError("disk full")
        ):
            with pytest.raises(OSError):
                store.save_or_enrich(_make_lesson("Pin the port", "tool", "Do not autopick"))

        assert all(le.negative is None for le in store.load_all()), "cache advertises a lost write"
        store._cache = None
        assert all(le.negative is None for le in store.load_all()), "file was modified"
        assert list(tmp_path.rglob("*.tmp")) == [], "temp file survived a failed swap"
