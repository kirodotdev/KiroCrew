"""A write that supersedes a stored lesson names it and leaves a durable record.

This file pins the store side of a dedup supersede. A write whose rule contains a
stored lesson still deletes it, and ``write_lesson`` names every rule it removed in
``superseded``, NOT-clause included, and names none when it removed nothing. A
verbatim re-add of the removed rule is declined, a rewording that shares few
significant words coexists with the narrower rule, removing the narrower rule first
restores the general one exactly, and the keyword arithmetic shows why the store
reports the collapse instead of preventing it. The delete event carries only the
winner's row id and the reason, the tombstone reads ``superseded`` and the revision
``supersede``, the supersede logs at WARNING, a text-shaped reason is refused, and a
supersede rolls back when its attribution event fails while a plain forget keeps its
own status and survives a failed audit insert. How the CLI, the ``/api/lessons``
route and the ``learn_add`` tool show the removed rules lives in
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
    """The store deletes a stored lesson the submitted rule contains, and said nothing.

    From the issue: teach "never force push to a shared branch", then teach "when a
    release is in progress, never force push to a shared branch, and tell the release
    manager first". The second rule's text CONTAINS the first, so the substring rule
    tombstones the general lesson. Reporting a plain ``inserted`` with ``reason=None``
    tells the user the save succeeded while leaving no rule at all against force
    pushing outside a release.

    Deleting is deliberate (``write_lesson``'s docstring: "longer wins" / "newer
    replaces older"), and these tests do NOT assert it stops. They assert the write
    NAMES the rule it destroyed, which is the only recoverable trace: the row is
    tombstoned, so it is gone from ``get_lessons``, ``learn_list`` and the injected
    lessons block.

    POSITIVE CONTROL for these tests: they fail on unpatched ``main``, where
    ``LessonWriteResult`` has no ``superseded`` field at all -- ``result.superseded``
    raises ``AttributeError``. That is a fail for the right reason (the field does not
    exist), and it is distinct from a fail on the wording of a message.
    """

    GENERAL = "never force push to a shared branch"
    NARROWER = (
        "when a release is in progress, never force push to a shared branch, "
        "and tell the release manager first"
    )

    def test_the_general_rule_is_still_deleted(self, tmp_path):
        """Pinned deliberately: the fix REPORTS the supersede, it does not prevent it.

        Without this, a later change could "fix" the issue by loosening the
        containment test, and the tests below would still pass while the dedup rule
        that keeps the store from filling with near-identical lessons had quietly
        stopped saying no. This is the assertion that makes that visible.
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

        A surface renders it with a bare ``if``, so a non-empty value on an ordinary
        insert would put a data-loss warning in front of the user for a write that
        lost nothing -- and a warning that always fires is read as noise, which is how
        a real one gets ignored.
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

        A count would be a smaller lie than silence but still a lie: the user cannot
        restore a rule the report does not name.

        The two stored rules are deliberately unrelated to each other -- they share no
        significant word, so neither supersedes the other and both are live when the
        third write arrives. A chain of progressively longer rules could not set this
        up, because each write already collapses the one before it.
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

        Submitting a rule CONTAINED IN a stored one is genuinely covered by it, so it
        is declined without mutating anything. Loosening the containment test to save
        the general lesson would have changed this too -- accumulating a near-identical
        row for every re-phrasing. It is pinned here so that cannot happen quietly.
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

        Dedup compares rules through ``_lesson_embed_text``, which returns the ``rule``
        field ALONE so the comparison basis matches the embedding space. Reporting on
        that same rendering silently dropped the NOT-clause -- so the clause, which
        carries the sharpest part of the guidance, became unrecoverable at the exact
        moment the row was tombstoned. The report uses the display rendering instead;
        the comparisons still use the embed one, so no dedup decision moves.
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

        Measured, because the wording was weaker than this before: a VERBATIM re-add of
        the superseded rule is declined by the substring branch (the narrower rule now
        contains it), so telling the user only to "re-add it" would send them into a
        refusal. Two recoveries do work, and both are what the text now names.
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
        """Why the fix reports instead of preventing: the collapse is over-determined.

        Verbatim containment at word boundaries makes the stored rule's keyword set a
        SUBSET of the submitted rule's, so the topic-overlap rule three lines below
        scores 100% and deletes the same row anyway. Deleting the substring branch
        alone therefore changes nothing a user would notice -- the general lesson is
        still gone, just via the next rule down. Preventing the collapse means editing
        all three dedup rules, which is the design call the issue reserved for
        maintainers.

        Computed from the store's own keyword helper so it cannot drift from the
        arithmetic the branch actually performs.
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

    A ``learn_add`` that supersedes a lesson records which lesson it deleted in the
    tombstone itself, not only in the tool reply and a log line -- so a call that
    returns ``timed out`` while committing can still be traced to what was retired.
    These pin that the tombstone carries the winner and reason, that the deletion is
    logged at a level a default install records to disk, and that an ordinary forget
    is unaffected.
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

        A default install's file handler records WARNING+ only, so an INFO-level
        supersede would leave no on-disk trace of a data deletion. Capture at WARNING
        (not DEBUG) so the assertion FAILS if the level is reverted to INFO.
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

        Exercises the ``else`` arms of the new ternaries: a plain forget must keep
        status 'forgotten', operation 'forget' (never 'supersede'), and a null audit
        new_value.
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

        Stronger than a bare 'linting'/'formatting' absence check: it asserts the
        full parsed shape, so any future change that widened the payload to include
        rule text (or any third key) would fail here.
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

        A reason carrying a newline or long free text (e.g. a future caller
        splicing rule text into it) is refused at delete_semantic rather than
        written to the disk-persisted audit row.
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
