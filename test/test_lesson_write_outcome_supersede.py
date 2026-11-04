"""A write that supersedes a stored lesson names it and leaves a durable record.

This file pins the store side of a dedup supersede: what ``write_lesson`` reports in
``superseded``, which recoveries work, and the durable delete record. How the CLI,
the ``/api/lessons`` route and the ``learn_add`` tool show the removed rules lives in
``test_lesson_write_outcome_surfaces.py``.
"""

from __future__ import annotations

import sqlite3

import pytest
from lesson_write_outcome_helpers import (
    _rule_of,
    _store,
)

from kiro_crew.vector_memory import LessonWriteOutcome, VectorMemoryStore


class TestASupersedingWriteNamesWhatItRemoved:
    """A write that deletes a stored lesson its rule contains names that lesson.

    Deleting is deliberate ("longer wins"), so these tests do not assert it stops.
    The name is the only recoverable trace: the tombstoned row is gone from
    ``get_lessons``, ``learn_list`` and the injected lessons block.
    """

    GENERAL = "never force push to a shared branch"
    NARROWER = (
        "when a release is in progress, never force push to a shared branch, "
        "and tell the release manager first"
    )

    def test_the_general_rule_is_still_deleted(self, tmp_path):
        """The supersede is REPORTED, not prevented.

        Loosening the containment test would pass the tests below while the store
        filled with near-identical lessons.
        """
        store = _store(tmp_path)
        try:
            store.write_lesson(self.GENERAL, "tool")
            store.write_lesson(self.NARROWER, "tool")
            live = [_rule_of(row) for row in store.get_lessons()]
            assert self.GENERAL not in live, "supersede-on-containment must still delete"
            assert self.NARROWER in live
        finally:
            store.close()

    def test_the_write_names_the_rule_it_superseded(self, tmp_path):
        store = _store(tmp_path)
        try:
            store.write_lesson(self.GENERAL, "tool")
            result = store.write_lesson(self.NARROWER, "tool")
            # Still an insert, still truthy -- the submitted lesson did land.
            assert result.outcome is LessonWriteOutcome.INSERTED
            assert bool(result) is True
            # ...and the call discloses what that cost.
            assert result.superseded == (self.GENERAL,)
        finally:
            store.close()

    def test_a_write_that_supersedes_nothing_reports_nothing(self, tmp_path):
        """The field must not be a warning that fires on every write.

        A surface renders it with a bare ``if``, so a non-empty value would show a
        data-loss warning for a write that lost nothing.
        """
        store = _store(tmp_path)
        try:
            first = store.write_lesson(self.GENERAL, "tool")
            assert first.superseded == ()
            unrelated = store.write_lesson("write commit messages in the imperative", "tool")
            assert unrelated.superseded == ()
        finally:
            store.close()

    def test_one_write_names_every_rule_it_removed_not_just_the_first(self, tmp_path):
        """The delete branch ``continue``s, so a single call can tombstone several rows.

        The two stored rules share no significant word, so both are live when the third
        write arrives; a chain of longer rules would collapse each one as it went.
        """
        store = _store(tmp_path)
        try:
            store.write_lesson("prefer tabs", "preference")
            store.write_lesson("run ruff", "tool")
            assert len(store.get_lessons()) == 2, "the two rules must not dedup each other"
            result = store.write_lesson("prefer tabs and run ruff on every python file", "tool")
            assert set(result.superseded) == {"prefer tabs", "run ruff"}
            live = [_rule_of(row) for row in store.get_lessons()]
            assert live == ["prefer tabs and run ruff on every python file"]
        finally:
            store.close()

    def test_the_refuse_direction_is_unchanged_and_deletes_nothing(self, tmp_path):
        """The OTHER direction of the same test must keep refusing.

        A rule CONTAINED IN a stored one is covered by it, so it is declined without
        mutating anything.
        """
        store = _store(tmp_path)
        try:
            store.write_lesson("always run the linter before pushing a branch", "tool")
            result = store.write_lesson("run the linter", "tool")
            assert result.outcome is LessonWriteOutcome.DEDUPED
            assert result.reason == "substring_covered"
            assert result.superseded == ()
            live = [_rule_of(row) for row in store.get_lessons()]
            assert live == ["always run the linter before pushing a branch"]
        finally:
            store.close()

    def test_the_report_keeps_the_not_clause_of_the_rule_it_removed(self, tmp_path):
        """A report that drops the clause hands back a rule the user cannot restore.

        Dedup compares through ``_lesson_embed_text``, which returns the ``rule`` field
        ALONE. The report uses the display rendering, so the NOT-clause survives.
        """
        store = _store(tmp_path)
        try:
            store.write_lesson("prefer ruff over flake8", "tool", "for type checking")
            result = store.write_lesson(
                "in CI prefer ruff over flake8 and fail the build on any finding", "tool"
            )
            assert result.outcome is LessonWriteOutcome.INSERTED
            assert len(result.superseded) == 1
            reported = result.superseded[0]
            assert reported.startswith("prefer ruff over flake8")
            assert "for type checking" in reported, "the NOT-clause was dropped from the report"
            assert reported == "prefer ruff over flake8 \u2014 NOT: for type checking"
        finally:
            store.close()

    def test_a_clause_less_rule_is_reported_verbatim_with_no_separator(self, tmp_path):
        """The display rendering must not decorate a row that has no clause."""
        store = _store(tmp_path)
        try:
            store.write_lesson(self.GENERAL, "tool")
            result = store.write_lesson(self.NARROWER, "tool")
            assert result.superseded == (self.GENERAL,)
            assert "NOT:" not in result.superseded[0]
        finally:
            store.close()

    def test_the_advice_it_prints_describes_a_recovery_the_policy_permits(self, tmp_path):
        """The warning's purpose is restorability, so its advice must actually work.

        A VERBATIM re-add is declined by the substring branch (the narrower rule
        contains it), so "re-add it" alone would lead to a refusal.
        """
        store = _store(tmp_path)
        try:
            store.write_lesson(self.GENERAL, "tool")
            store.write_lesson(self.NARROWER, "tool")

            # 1. Verbatim re-add: declined, and it changes nothing.
            again = store.write_lesson(self.GENERAL, "tool")
            assert again.outcome is LessonWriteOutcome.DEDUPED
            assert again.reason == "substring_covered"
            assert again.superseded == ()
            assert [_rule_of(r) for r in store.get_lessons()] == [self.NARROWER]

            # 2. Wording sharing few significant words: coexists, nothing superseded.
            other = store.write_lesson(
                "protected branches reject a forced update at all times", "tool"
            )
            assert other.outcome is LessonWriteOutcome.INSERTED
            assert other.superseded == (), "a non-overlapping rule must not ping-pong"
            assert len(store.get_lessons()) == 2
        finally:
            store.close()

    def test_removing_the_narrower_rule_first_restores_the_general_one_exactly(self, tmp_path):
        """The other recovery the text names, and the only one that restores verbatim."""
        store = _store(tmp_path)
        try:
            store.write_lesson(self.GENERAL, "tool")
            store.write_lesson(self.NARROWER, "tool")
            narrower_key = next(
                r["key"] for r in store.get_lessons() if _rule_of(r) == self.NARROWER
            )
            store.delete_semantic(narrower_key, "user_explicit")
            restored = store.write_lesson(self.GENERAL, "tool")
            assert restored.outcome is LessonWriteOutcome.INSERTED
            assert [_rule_of(r) for r in store.get_lessons()] == [self.GENERAL]
        finally:
            store.close()

    def test_removing_only_the_substring_branch_would_not_have_saved_the_lesson(self):
        """Why the store reports instead of preventing: the collapse is over-determined.

        Containment makes the stored keyword set a SUBSET of the submitted one, so the
        topic-overlap rule scores 100% and deletes the same row anyway.
        """
        keywords = VectorMemoryStore._lesson_keywords
        general = keywords(self.GENERAL.lower())
        narrower = keywords(self.NARROWER.lower())
        assert general, "the general rule must contribute keywords for the branch to run"
        assert general <= narrower, "containment should make the keyword set a subset"
        ratio = len(general & narrower) / min(len(narrower), len(general))
        assert ratio == 1.0
        assert ratio >= 0.5, "topic overlap would delete the general lesson regardless"


class _EventInsertFailingConn:
    """Delegates to a real sqlite3 connection but fails any memory_events insert.

    ``sqlite3.Connection.execute`` is a read-only C attribute, so it cannot be
    patched in place; wrapping the connection injects the event-write failure
    while every other statement runs normally.
    """

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, *args, **kwargs):
        if "INSERT INTO memory_events" in sql:
            raise sqlite3.OperationalError("no such table: memory_events")
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)


class TestSupersedeRecordIsDurableAndAttributable:
    """A dedup supersede must leave a durable, attributable on-disk trace.

    The tombstone itself names the winner and reason, so a call that returns
    ``timed out`` while committing can still be traced to what was retired.
    """

    # Two rules that share >=50% of the larger keyword set without either
    # containing the other, so the topic-overlap branch is the one that fires.
    _STORED = "prefer ruff for linting python code in this repository"
    _SUBMITTED = "prefer ruff for formatting python code in this repository"

    def _supersede(self, store):
        stored = store.write_lesson(self._STORED, "tool")
        assert stored.outcome is LessonWriteOutcome.INSERTED
        result = store.write_lesson(self._SUBMITTED, "tool")
        assert result.superseded, "the overlap branch should have retired the stored rule"
        return result

    def _retired_key(self, store):
        return next(e["memory_key"] for e in store.get_events() if e["event_type"] == "delete")

    def test_delete_event_records_the_winning_key_and_reason(self, tmp_path) -> None:
        import json

        store = _store(tmp_path)
        try:
            self._supersede(store)
            deletes = [e for e in store.get_events() if e["event_type"] == "delete"]
            assert len(deletes) == 1, "exactly one lesson was superseded"
            payload = json.loads(deletes[0]["new_value"])
            # The winner is named by row id, and the reason is the overlap ratio.
            assert payload["superseded_by"].startswith("lesson."), payload
            assert "overlap" in payload["reason"], payload
        finally:
            store.close()

    def test_supersede_tombstone_reads_apart_from_an_explicit_forget(self, tmp_path) -> None:
        from kiro_crew import memory_record_metadata as meta

        store = _store(tmp_path)
        try:
            self._supersede(store)
            # The retired row's key is md5(rule, scope); recover it from the delete
            # event rather than recomputing, so the test does not couple to the hash.
            record = meta.get_record_metadata(store.db, "key:" + self._retired_key(store))
            assert (
                record["status"] == "superseded"
            ), "a dedup supersede must not read as a plain 'forgotten' tombstone"
        finally:
            store.close()

    def test_supersede_revision_records_the_operation(self, tmp_path) -> None:
        """The retained revision names the deletion a 'supersede', not a 'forget'.

        Pins the ``operation`` field independently of the record status, so a
        reversion of the ``operation=`` ternary alone is caught.
        """
        store = _store(tmp_path)
        try:
            self._supersede(store)
            rid = "key:" + self._retired_key(store)
            ops = [
                row[0]
                for row in store.db.execute(
                    "SELECT operation FROM memory_revisions WHERE record_id=?", (rid,)
                )
            ]
            assert "supersede" in ops, ops
            assert "forget" not in ops, "a supersede must not record itself as a forget"
        finally:
            store.close()

    def test_supersede_is_logged_at_warning_so_a_default_install_records_it(
        self, tmp_path, caplog
    ) -> None:
        """The supersede line logs at WARNING, not INFO.

        A default install's file handler records WARNING+ only. Captured at WARNING,
        not DEBUG, so an INFO line would not be seen.
        """
        import logging

        store = _store(tmp_path)
        try:
            caplog.clear()
            with caplog.at_level(logging.WARNING, logger="kiro_crew.vector_memory"):
                self._supersede(store)
            supersede_lines = [
                r for r in caplog.records if r.getMessage().startswith("Lesson supersede:")
            ]
            assert supersede_lines, "the supersede must log at WARNING or above"
            assert supersede_lines[0].levelno >= logging.WARNING
        finally:
            store.close()

    def test_an_explicit_forget_is_unchanged_by_the_supersede_path(self, tmp_path) -> None:
        """The non-supersede delete_semantic path still records forgotten/None.

        Status 'forgotten', operation 'forget' (never 'supersede'), null audit new_value.
        """
        from kiro_crew import memory_record_metadata as meta
        from kiro_crew.vector_memory import _lesson_key

        store = _store(tmp_path)
        try:
            store.write_lesson("always pin dependency versions in the lockfile", "tool")
            key = _lesson_key("always pin dependency versions in the lockfile", "")
            # A plain forget: no superseded_by / supersede_reason kwargs.
            assert store.delete_semantic(key, "user_explicit")
            record = meta.get_record_metadata(store.db, "key:" + key)
            assert record["status"] == "forgotten", record
            ops = [
                row[0]
                for row in store.db.execute(
                    "SELECT operation FROM memory_revisions WHERE record_id=?",
                    ("key:" + key,),
                )
            ]
            assert "forget" in ops and "supersede" not in ops, ops
            delete_evt = next(
                e
                for e in store.get_events()
                if e["event_type"] == "delete" and e["memory_key"] == key
            )
            assert delete_evt["new_value"] in (None, ""), delete_evt["new_value"]
        finally:
            store.close()

    def test_the_durable_record_carries_only_identities_not_rule_text(self, tmp_path) -> None:
        """The audit new_value is exactly {superseded_by, reason} and nothing else.

        The full parsed shape is asserted, so a third key or rule text would fail.
        """
        import json

        store = _store(tmp_path)
        try:
            self._supersede(store)
            deletes = [e for e in store.get_events() if e["event_type"] == "delete"]
            payload = json.loads(deletes[0]["new_value"])
            assert set(payload) == {"superseded_by", "reason"}, payload
            assert "linting" not in deletes[0]["new_value"], deletes[0]["new_value"]
            assert "formatting" not in deletes[0]["new_value"], deletes[0]["new_value"]
        finally:
            store.close()

    def test_a_text_shaped_supersede_reason_is_refused_at_the_sink(self, tmp_path) -> None:
        """The 'identities only' invariant is structural, not just contractual.

        A reason with a newline or long free text is refused at delete_semantic rather
        than written to the disk-persisted audit row.
        """
        from kiro_crew.vector_memory import _lesson_key

        store = _store(tmp_path)
        try:
            store.write_lesson("always pin dependency versions in the lockfile", "tool")
            key = _lesson_key("always pin dependency versions in the lockfile", "")
            with pytest.raises(ValueError):
                store.delete_semantic(
                    key,
                    "tool",
                    superseded_by="lesson.deadbeef",
                    supersede_reason="overlap with the user's secret rule\nsecond line",
                )
        finally:
            store.close()

    def test_a_plain_forget_survives_an_unavailable_audit_table(self, tmp_path) -> None:
        """A plain forget commits its tombstone even when the event write fails.

        The audit event on the forget path is best-effort: a failed
        ``memory_events`` insert is swallowed, so the tombstone and revision still
        commit. A supersede is the opposite -- its attribution event is atomic, so
        the same failure rolls the whole supersede back and the row survives.
        """
        from kiro_crew.vector_memory import _lesson_key

        store = _store(tmp_path)
        try:
            store.write_lesson("always pin dependency versions in the lockfile", "tool")
            key = _lesson_key("always pin dependency versions in the lockfile", "")
            real_conn = store._db
            store._db = _EventInsertFailingConn(real_conn)
            try:
                assert store.delete_semantic(key, "user_explicit") is True
            finally:
                store._db = real_conn
            # Event write failed, but the row is tombstoned: a re-forget finds nothing.
            assert store.delete_semantic(key, "user_explicit") is False
        finally:
            store.close()

    def test_a_supersede_rolls_back_when_its_attribution_event_fails(self, tmp_path) -> None:
        """A supersede is atomic: a failed attribution event keeps the row alive."""
        from kiro_crew.vector_memory import _lesson_key

        store = _store(tmp_path)
        try:
            store.write_lesson("always pin dependency versions in the lockfile", "tool")
            key = _lesson_key("always pin dependency versions in the lockfile", "")
            real_conn = store._db
            store._db = _EventInsertFailingConn(real_conn)
            try:
                with pytest.raises(sqlite3.OperationalError):
                    store.delete_semantic(
                        key,
                        "tool",
                        superseded_by="lesson.deadbeef",
                        supersede_reason="62% keyword overlap",
                    )
            finally:
                store._db = real_conn
            # The rollback kept the row: a plain forget still finds it to tombstone.
            assert store.delete_semantic(key, "user_explicit") is True
        finally:
            store.close()
