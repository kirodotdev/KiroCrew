"""Consolidation must never write semantic memory as ``user_explicit``.

``_write_structured_memory`` writes a consolidation item under the consolidation
source, never ``"user_explicit"``, whatever ``confidence`` the model reports. That
confidence is the MODEL's, produced by an LLM re-summarising history — it is not
evidence that the pass observed the user stating anything. Because
``_write_semantic`` gives ``source="user_explicit"`` the unconditional-win path
(see ``vector_memory._write_semantic``: "user_explicit always wins"), deriving
that source from the score would let a re-summarisation overwrite a genuine
user-stated key and silently shrink or corrupt it.

These tests pin the source that reaches ``set_semantic``: always the
consolidation source, so real ``user_explicit`` keys reach the ``conflict_skip``
branch instead of being overwritten.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import history_consolidation as H
from kiro_crew.dashboard.notification_coordinator import NotificationCoordinator
from kiro_crew.history import HistoryConsolidator
from kiro_crew.notifications.bus import (
    NotificationBus,
    NotificationValidationError,
    payload_from_legacy,
)
from kiro_crew.vector_memory import (
    SemanticRejectCode,
    VectorMemoryStore,
    _import_source,
)


@pytest.fixture(autouse=True)
def _quiet_logger():
    """``HistoryConsolidator._logger`` is a read-only property, so it cannot be
    assigned on an instance — patch it on the class for the duration of a test."""
    with patch.object(HistoryConsolidator, "_logger", MagicMock()):
        yield


def _consolidator(store: MagicMock) -> HistoryConsolidator:
    """Build a consolidator with only the attribute the method under test uses.

    ``__new__`` sidesteps the real constructor deliberately: this is a unit test
    of one write path, and going through ``__init__`` would drag in the whole
    gateway wiring for no added coverage.
    """
    consolidator = HistoryConsolidator.__new__(HistoryConsolidator)
    consolidator._vector_store = store
    return consolidator


class TestConsolidationDoesNotImpersonateUser:
    def test_confidence_one_does_not_escalate_to_user_explicit(self):
        store = MagicMock()
        store.set_semantic.return_value = None
        consolidator = _consolidator(store)

        consolidator._write_structured_memory(
            {"semantic": [{"key": "project.alpha", "value": "v", "confidence": 1.0}]},
            "session-key",
        )

        store.set_semantic.assert_called_once()
        kwargs = store.set_semantic.call_args.kwargs
        assert kwargs["source"] == "consolidation:session-key"
        assert kwargs["source"] != "user_explicit"

    def test_confidence_is_preserved(self):
        """Only the source changes — the model's confidence still reaches the store,
        so the confidence-threshold gate in set_semantic behaves as before."""
        store = MagicMock()
        store.set_semantic.return_value = None
        consolidator = _consolidator(store)

        consolidator._write_structured_memory(
            {"semantic": [{"key": "project.alpha", "value": "v", "confidence": 1.0}]},
            "session-key",
        )

        assert store.set_semantic.call_args.kwargs["confidence"] == 1.0

    def test_sub_threshold_confidence_unchanged(self):
        """Lower-confidence items already used the consolidation source; unchanged."""
        store = MagicMock()
        store.set_semantic.return_value = None
        consolidator = _consolidator(store)

        consolidator._write_structured_memory(
            {"semantic": [{"key": "project.beta", "value": "v", "confidence": 0.6}]},
            "session-key",
        )

        assert store.set_semantic.call_args.kwargs["source"] == "consolidation:session-key"

    def test_consolidation_can_still_create_keys(self):
        """The fix must not stop consolidation writing at all — only impersonation."""
        store = MagicMock()
        store.set_semantic.return_value = None
        consolidator = _consolidator(store)

        consolidator._write_structured_memory(
            {
                "semantic": [
                    {"key": "project.one", "value": "a", "confidence": 1.0},
                    {"key": "project.two", "value": "b", "confidence": 0.4},
                ]
            },
            "session-key",
        )

        assert store.set_semantic.call_count == 2
        for call in store.set_semantic.call_args_list:
            assert call.kwargs["source"] == "consolidation:session-key"


def _notifying_consolidator(
    store: MagicMock, notifier: MagicMock | None, loop: object = None
) -> HistoryConsolidator:
    """A consolidator with just the attributes the refusal path reads."""
    consolidator = HistoryConsolidator.__new__(HistoryConsolidator)
    consolidator._vector_store = store
    consolidator._on_memory_conflict = notifier
    consolidator._conflict_notified = {}
    consolidator._event_loop = loop
    return consolidator


def _refusing_store(existing_source: str | None) -> MagicMock:
    """A store that refuses with CONFLICT and reports *existing_source* as owner."""
    store = MagicMock()
    store.set_semantic.return_value = (SemanticRejectCode.CONFLICT, "cannot be overwritten")
    store.get_semantic.return_value = (
        None if existing_source is None else {"source": existing_source, "value_json": '"old"'}
    )
    return store


class TestRefusedCorrectionIsSurfaced:
    """Refusing the write keeps memory correct; saying nothing loses the correction.

    The ``user_explicit`` conflict branch discards a value the USER expressed (in
    chat, extracted by consolidation) in favour of the stored one. That is the
    right resolution, but the only other record of it is an operational log line,
    so the user cannot know their correction never landed — and the recovery path
    (an explicit write, which wins) is only reachable by a user who knows.
    """

    def test_conflict_on_user_owned_key_notifies(self):
        store = _refusing_store("user_explicit")
        notifier = MagicMock()
        consolidator = _notifying_consolidator(store, notifier)

        consolidator._write_structured_memory(
            {"semantic": [{"key": "pref.color", "value": "teal", "confidence": 1.0}]},
            "session-key",
        )

        notifier.assert_called_once()
        kind, title, body = notifier.call_args.args
        assert kind == "memory_conflict"
        assert "pref.color" in title
        # The refused value must be IN the notice: it is what the user has to
        # re-apply, and a notice that omits it cannot be acted on.
        assert "teal" in body

    def test_other_reject_causes_do_not_notify(self):
        """A malformed or disallowed key is the model's problem, not the user's."""
        store = MagicMock()
        store.set_semantic.return_value = (SemanticRejectCode.ALLOWLIST, "not allowlisted")
        notifier = MagicMock()
        consolidator = _notifying_consolidator(store, notifier)

        consolidator._write_structured_memory(
            {"semantic": [{"key": "pref.color", "value": "teal"}]}, "session-key"
        )

        notifier.assert_not_called()

    def test_higher_confidence_conflict_does_not_notify(self):
        """Both conflict branches report CONFLICT, but only one discards user input.

        Losing to a higher-confidence entry loses nothing the user said, so
        announcing it would be noise about consolidation disagreeing with itself.
        """
        store = _refusing_store("consolidation:earlier")
        notifier = MagicMock()
        consolidator = _notifying_consolidator(store, notifier)

        consolidator._write_structured_memory(
            {"semantic": [{"key": "pref.color", "value": "teal"}]}, "session-key"
        )

        notifier.assert_not_called()

    def test_repeated_identical_refusal_notifies_once(self):
        """Consolidation re-reads the same history every pass and the prompt tells
        the model to UPDATE an existing key, so this refusal recurs indefinitely.
        Without dedupe one dropped correction becomes a notification per pass."""
        store = _refusing_store("user_explicit")
        notifier = MagicMock()
        consolidator = _notifying_consolidator(store, notifier)
        item = {"semantic": [{"key": "pref.color", "value": "teal"}]}

        consolidator._write_structured_memory(item, "session-key")
        consolidator._write_structured_memory(item, "session-key")
        consolidator._write_structured_memory(item, "session-key")

        assert notifier.call_count == 1

    def test_a_new_refused_value_notifies_again(self):
        """Dedupe is per (key, value) — a genuinely different correction is news."""
        store = _refusing_store("user_explicit")
        notifier = MagicMock()
        consolidator = _notifying_consolidator(store, notifier)

        consolidator._write_structured_memory(
            {"semantic": [{"key": "pref.color", "value": "teal"}]}, "session-key"
        )
        consolidator._write_structured_memory(
            {"semantic": [{"key": "pref.color", "value": "amber"}]}, "session-key"
        )

        assert notifier.call_count == 2
        assert "amber" in notifier.call_args.args[2]

    def test_no_notifier_wired_is_not_an_error(self):
        """The CLI and eval paths have no notifier; the refusal must still be a
        plain refusal rather than a crash that loses the remaining writes."""
        store = _refusing_store("user_explicit")
        consolidator = _notifying_consolidator(store, None)

        consolidator._write_structured_memory(
            {
                "semantic": [
                    {"key": "pref.color", "value": "teal"},
                    {"key": "pref.other", "value": "v"},
                ]
            },
            "session-key",
        )

        # Both items were still attempted — the missing sink changed nothing.
        assert store.set_semantic.call_count == 2

    def test_delivery_is_handed_to_the_event_loop(self):
        """``_write_structured_memory`` runs in the embed pool, and a dashboard
        notifier reaches ``DashboardState._broadcast`` → ``put_nowait`` on SSE
        ``asyncio.Queue``s plus an ``asyncio.Event``. Neither is thread-safe and
        the lost wakeup is silent, so the sink must be scheduled onto the loop.
        """
        store = _refusing_store("user_explicit")
        notifier = MagicMock()
        loop = MagicMock()
        consolidator = _notifying_consolidator(store, notifier, loop=loop)

        consolidator._write_structured_memory(
            {"semantic": [{"key": "pref.color", "value": "teal"}]}, "session-key"
        )

        loop.call_soon_threadsafe.assert_called_once()
        assert loop.call_soon_threadsafe.call_args.args[0] is notifier
        # Not called directly on this thread.
        notifier.assert_not_called()

    def test_a_failing_sink_does_not_abort_consolidation(self):
        store = _refusing_store("user_explicit")
        notifier = MagicMock(side_effect=RuntimeError("sink exploded"))
        consolidator = _notifying_consolidator(store, notifier)

        consolidator._write_structured_memory(
            {
                "semantic": [
                    {"key": "pref.color", "value": "teal"},
                    {"key": "pref.other", "value": "v"},
                ]
            },
            "session-key",
        )

        assert store.set_semantic.call_count == 2

    def test_notified_map_is_bounded(self):
        """A model inventing a fresh key every pass must not grow the map forever."""
        store = _refusing_store("user_explicit")
        consolidator = _notifying_consolidator(store, MagicMock())

        for i in range(H._MAX_CONFLICT_NOTIFIED_KEYS + 20):
            consolidator._write_structured_memory(
                {"semantic": [{"key": f"pref.k{i}", "value": "v"}]}, "session-key"
            )

        assert len(consolidator._conflict_notified) <= H._MAX_CONFLICT_NOTIFIED_KEYS


class TestUserOwnedRowsSurviveAutomatedRemoval:
    """Removal is guarded exactly as overwrite is, and pinned against a REAL store.

    The overwrite guard alone is bypassable in two steps rather than defeated in
    one: ``_write_semantic`` resolves conflicts only ``if existing and not
    existing["is_deleted"]``, so a tombstoned row takes the create path with no
    conflict check. Tombstone the user's key, write again, and the model's value
    lands where the user's value was.

    These run against a real ``VectorMemoryStore`` rather than a ``MagicMock``: the
    property under test is what the SQLite row looks like after two real calls, and
    a mock store would assert only that this test called the methods it just called.
    """

    def _store(self, tmp_path: Path) -> VectorMemoryStore:
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        return store

    def test_consolidation_cannot_tombstone_a_user_owned_key(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        assert store.set_semantic("pref.editor", "vim", 1.0, "user_explicit") is None

        assert store.delete_semantic("pref.editor", "consolidation:s1") is False

        row = store.get_semantic("pref.editor")
        assert row is not None, "the user's row was tombstoned by an automated source"
        assert json.loads(row["value_json"]) == "vim"

    def test_delete_then_write_cannot_launder_the_model_value_in(self, tmp_path: Path) -> None:
        """The bypass end to end — the reason guarding overwrite alone is not enough."""
        store = self._store(tmp_path)
        store.set_semantic("pref.editor", "vim", 1.0, "user_explicit")

        store.delete_semantic("pref.editor", "consolidation:s1")
        err = store.set_semantic("pref.editor", "emacs", 1.0, "consolidation:s1")

        assert err is not None, "consolidation overwrote a user-set key after tombstoning it"
        assert err[0] is SemanticRejectCode.CONFLICT
        row = store.get_semantic("pref.editor")
        assert row is not None
        assert json.loads(row["value_json"]) == "vim"

    def test_the_user_can_still_delete_their_own_key(self, tmp_path: Path) -> None:
        """Negative control: the guard must not lock the user out of their own row."""
        store = self._store(tmp_path)
        store.set_semantic("pref.editor", "vim", 1.0, "user_explicit")

        assert store.delete_semantic("pref.editor", "user_explicit") is True
        assert store.get_semantic("pref.editor") is None

    def test_an_automated_row_is_still_removable(self, tmp_path: Path) -> None:
        """Negative control: the guard is scoped to user-owned rows, not to all rows.

        Without this, a guard that simply refused every automated delete would pass
        every other test in this class while breaking consolidation's own cleanup.
        """
        store = self._store(tmp_path)
        store.set_semantic("pref.editor", "vim", 1.0, "consolidation:s1")

        assert store.delete_semantic("pref.editor", "consolidation:s2") is True
        assert store.get_semantic("pref.editor") is None


class TestWriteSourceIsAClosedSet:
    """``source`` carries privilege, so it cannot be a free string any writer asserts.

    ``user_explicit`` takes the unconditional-win path, is the only source allowed to
    write a reserved ``system.`` key, and now the only one allowed to remove a
    user-owned row — while two entry points build ``source`` from caller-controlled
    data (``import_memory`` from the imported file, the dashboard handler from the
    request body).
    """

    def _store(self, tmp_path: Path) -> VectorMemoryStore:
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        return store

    def test_an_unknown_source_is_refused(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)

        err = store.set_semantic("pref.editor", "vim", 1.0, "totally-made-up")

        assert err is not None
        assert err[0] is SemanticRejectCode.SOURCE_UNKNOWN
        assert store.get_semantic("pref.editor") is None

    def test_consolidations_per_session_source_is_still_accepted(self, tmp_path: Path) -> None:
        """The set is closed in KIND, not in NUMBER.

        Consolidation writes under ``f"consolidation:{key}"``, so an exact-match set
        would refuse every consolidation write — the namespace split is load-bearing,
        not decoration.
        """
        store = self._store(tmp_path)

        assert store.set_semantic("pref.editor", "vim", 1.0, "consolidation:sess-abc") is None
        row = store.get_semantic("pref.editor")
        assert row is not None
        assert row["source"] == "consolidation:sess-abc"

    @pytest.mark.parametrize(
        "source", ["user_explicit", "consolidation", "import", "migration", "promotion"]
    )
    def test_every_source_the_code_actually_writes_is_accepted(
        self, tmp_path: Path, source: str
    ) -> None:
        """Guards the closed set against being too tight to admit its own callers."""
        store = self._store(tmp_path)

        assert store.set_semantic("pref.editor", "vim", 1.0, source) is None


class TestSupersedeReportingStaysTruthful:
    def test_a_refused_deletion_is_not_reported_as_superseded(self, tmp_path: Path) -> None:
        """``superseded`` is the caller's only record of what was destroyed.

        A refusable delete means a report that names a row without deleting it makes
        the result lie in the one direction that matters, so only a real tombstone is
        reported.
        """
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        store.write_lesson("always run the linter", source="user_explicit")

        result = store.write_lesson("always run the linter before pushing", source="consolidation")

        surviving = [
            json.loads(r["value_json"])
            for r in store.db.execute(
                "SELECT value_json FROM semantic_memory "
                "WHERE key LIKE 'lesson.%' AND is_deleted = 0"
            ).fetchall()
        ]
        rules = [v.get("rule") if isinstance(v, dict) else v for v in surviving]
        assert "always run the linter" in rules, "the user's lesson was tombstoned"
        for reported in result.superseded:
            assert "always run the linter" != reported


class TestConflictNoticeDoesNotOverclaimProvenance:
    def test_the_notice_reports_the_marker_not_the_user(self) -> None:
        """Rows the removed escalation stamped are neither re-sourced nor flagged.

        Nothing migrates them on upgrade, so a row marked ``user_explicit`` by the
        old bug is indistinguishable from one the user really set. Saying "you set
        this" would state as history something only the marker claims, for exactly
        the rows the old bug mislabelled.
        """
        notifier = MagicMock()
        consolidator = _notifying_consolidator(_refusing_store("user_explicit"), notifier)

        consolidator._write_structured_memory(
            {"semantic": [{"key": "pref.editor", "value": "emacs", "confidence": 0.9}]},
            "sess-1",
        )

        assert notifier.call_count == 1
        body = notifier.call_args[0][2]
        assert "set by you directly" not in body
        assert "marked as user-set" in body


class TestTheGuardDoesNotBreakUserInitiatedRemoval:
    """The dashboard's contradiction sweep is a USER action wearing another label.

    `handlers/cron.py` supersedes a stored lesson by calling
    `delete_semantic(key, "contradiction_superseded")`, and it fires because the user
    wrote a lesson contradicting that one. `write_lesson` defaults to
    `user_explicit`, so the contradicted row is almost always user-owned — a guard
    keyed on the literal string alone silently breaks superseding for exactly the
    rows the sweep exists to retire.
    """

    def _store(self, tmp_path: Path) -> VectorMemoryStore:
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        return store

    def test_the_contradiction_sweep_can_still_supersede_a_user_lesson(
        self, tmp_path: Path
    ) -> None:
        store = self._store(tmp_path)
        store.set_semantic("pref.editor", "vim", 1.0, "user_explicit")

        assert store.delete_semantic("pref.editor", "contradiction_superseded") is True
        assert store.get_semantic("pref.editor") is None

    def test_an_automated_source_is_still_refused(self, tmp_path: Path) -> None:
        """Control: widening the guard must not re-open it to consolidation."""
        store = self._store(tmp_path)
        store.set_semantic("pref.editor", "vim", 1.0, "user_explicit")

        assert store.delete_semantic("pref.editor", "consolidation:s1") is False
        assert store.get_semantic("pref.editor") is not None


class TestANonStringSourceIsRejectedNotCrashed:
    """`source` arrives from data the caller controls, so it is not always a str.

    `import_memory` reads it out of the imported file and the dashboard handler out
    of the request body, so a JSON null, number or list gets here. Splitting on ":"
    to find the namespace would raise AttributeError and turn a rejectable input
    into a crash on an untrusted path.
    """

    @pytest.mark.parametrize("bad", [None, 123, 4.5, ["user_explicit"], {"a": 1}, True])
    def test_a_non_string_source_is_refused(self, tmp_path: Path, bad: object) -> None:
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()

        err = store.set_semantic("pref.editor", "vim", 1.0, bad)  # type: ignore[arg-type]

        assert err is not None
        assert err[0] is SemanticRejectCode.SOURCE_UNKNOWN
        assert store.get_semantic("pref.editor") is None


class TestTheDeleteGuardIsAtomic:
    def test_the_authorization_read_happens_under_the_write_lock(self) -> None:
        """The guard is an authorization decision, so it cannot straddle the lock.

        Pinned structurally rather than by racing threads: a timing test here would
        be flaky in both directions. What must hold is that the row backing the
        decision is read INSIDE the same `_db_lock` hold that performs the UPDATE, so
        a `user_explicit` write cannot land in between and be tombstoned on the
        strength of a row that is already gone.
        """
        import inspect

        src = inspect.getsource(VectorMemoryStore.delete_semantic)
        body = src.split('"""', 2)[-1]
        lock_at = body.index("with self._db_lock:")
        read_at = body.index("self.get_semantic(key)")
        guard_at = body.index("_USER_OWNED_REMOVAL_SOURCES")
        update_at = body.index("UPDATE semantic_memory")

        assert lock_at < read_at, "the row is read before the lock is taken"
        assert lock_at < guard_at, "the guard is evaluated outside the lock"
        assert lock_at < update_at


class TestImportedDataCannotImpersonateTheUser:
    """`source` on an import comes from the FILE, so it cannot assert a privilege.

    `user_explicit` wins conflicts unconditionally, is the only source allowed to
    write a reserved `system.` key, and the only one allowed to remove a user-owned
    row. A file naming it would let imported content overwrite and retire keys the
    user actually set — the same impersonation this change removes from
    consolidation, arriving through a different door.
    """

    def _store(self, tmp_path: Path) -> VectorMemoryStore:
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        return store

    def test_an_import_claiming_user_explicit_lands_as_import(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)

        store.import_memory(
            {"semantic": [{"key": "pref.editor", "value": "emacs", "source": "user_explicit"}]}
        )

        row = store.get_semantic("pref.editor")
        assert row is not None, "the entry should still import, just without the privilege"
        assert row["source"] == "import"

    def test_an_imported_entry_cannot_overwrite_a_user_key(self, tmp_path: Path) -> None:
        """The consequence that makes the downgrade worth having."""
        store = self._store(tmp_path)
        store.set_semantic("pref.editor", "vim", 1.0, "user_explicit")

        store.import_memory(
            {"semantic": [{"key": "pref.editor", "value": "emacs", "source": "user_explicit"}]}
        )

        row = store.get_semantic("pref.editor")
        assert row is not None
        assert json.loads(row["value_json"]) == "vim"

    def test_an_imported_entry_cannot_remove_a_user_key(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        store.set_semantic("pref.editor", "vim", 1.0, "user_explicit")

        assert store.delete_semantic("pref.editor", _import_source("user_explicit")) is False
        assert store.get_semantic("pref.editor") is not None

    def test_an_ordinary_imported_source_is_preserved(self, tmp_path: Path) -> None:
        """Control: the downgrade targets the privileged value, not every import."""
        assert _import_source("import") == "import"
        assert _import_source("migration") == "migration"


class TestARejectedSourceIsNotEchoedIntoLogs:
    def test_the_reject_message_carries_the_type_not_the_value(self, tmp_path: Path) -> None:
        """The message reaches `logger` and the persisted reject event.

        `source` is caller-controlled on the paths that make this branch reachable —
        an imported file and a request body — so echoing it turns a rejected write
        into a disclosure of whatever the caller put in the field.
        """
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        secret = "sk-live-DEADBEEFdeadbeef0123456789"

        err = store.set_semantic("pref.editor", "vim", 1.0, secret)

        assert err is not None
        assert err[0] is SemanticRejectCode.SOURCE_UNKNOWN
        assert secret not in err[1], "the rejected source value leaked into the message"
        assert "str" in err[1]


class TestNotifierIsActuallyWired:
    def test_setter_binds_the_sink(self):
        """The real wiring is late-bound (DashboardState is built after the
        consolidator), so the setter is the path that must work."""
        consolidator = HistoryConsolidator.__new__(HistoryConsolidator)
        sink = MagicMock()

        consolidator.set_memory_conflict_notifier(sink)

        assert consolidator._on_memory_conflict is sink

    def test_dashboard_state_binds_its_notifier(self):
        """Guards against the fix being dead code: DashboardState is the single
        place every entry point's consolidator passes through."""
        source = Path(H.__file__).with_name("dashboard") / "state.py"
        text = source.read_text(encoding="utf-8")
        assert "set_memory_conflict_notifier(self.notify)" in text

    def test_refusal_notice_survives_the_real_bus(self):
        """End-to-end proof the rider is not dead code.

        Every other test here binds a ``MagicMock`` sink, which by construction
        cannot catch a payload the production ``NotificationCoordinator`` would
        reject: it swallows ``NotificationValidationError`` and only logs, so a
        rejected refusal notice would ship as dead code with those tests still
        green. This one drives the REAL ``payload_from_legacy``, the REAL
        ``NotificationBus`` (whose ``push`` runs ``validate()`` *and* rejects an
        unregistered channel), and the real error type.
        """
        delivered: list[dict] = []
        state = MagicMock()
        state.notification_bus = NotificationBus(delivered.append)
        logger = MagicMock()
        coordinator = NotificationCoordinator(
            logger_provider=lambda: logger,
            payload_from_legacy=payload_from_legacy,
            validation_error=NotificationValidationError,
            redact_value=lambda value: value,
            sweep_expired=lambda notes: 0,
            persist_one=lambda note: True,
            rewrite_all=lambda notes: None,
            executor_provider=MagicMock(),
            max_persisted=10,
        )

        # Exactly the call shape ``ConflictNotifier`` permits -- three
        # positional strings. ``url``/``actions`` are the only two fields the
        # adapter rejects rather than repairs, and this signature cannot supply
        # them, which is why no reject path is reachable from consolidation.
        coordinator.notify(
            state,
            "memory_conflict",
            "Correction not applied",
            "Consolidation kept the value you set directly.",
            meta=None,
            url=None,
            actions=None,
        )

        assert len(delivered) == 1, "the refusal notice was dropped, not delivered"
        assert delivered[0]["kind"] == "memory_conflict"
        # ``system.memory_conflict`` is deliberately NOT a registered channel;
        # the adapter falls back to ``system.agent``, and that fallback is what
        # keeps ``push`` from raising on an unregistered channel.
        assert delivered[0]["channel"] == "system.agent"
        logger.warning.assert_not_called()

    def test_the_real_bus_harness_would_catch_a_dropped_note(self):
        """Negative control for the test above, so its pass is not vacuous.

        Supplying an external ``url`` -- the documented reject path, and the one
        thing ``payload_from_legacy`` refuses to repair -- must be swallowed by
        the coordinator and logged rather than raised back through consolidation.
        If this delivered, the sibling test would prove nothing.
        """
        delivered: list[dict] = []
        state = MagicMock()
        state.notification_bus = NotificationBus(delivered.append)
        logger = MagicMock()
        coordinator = NotificationCoordinator(
            logger_provider=lambda: logger,
            payload_from_legacy=payload_from_legacy,
            validation_error=NotificationValidationError,
            redact_value=lambda value: value,
            sweep_expired=lambda notes: 0,
            persist_one=lambda note: True,
            rewrite_all=lambda notes: None,
            executor_provider=MagicMock(),
            max_persisted=10,
        )

        coordinator.notify(
            state,
            "memory_conflict",
            "Correction not applied",
            "body",
            meta=None,
            url="https://example.invalid/exfiltrate",
            actions=None,
        )

        assert delivered == [], "an external deep link must not reach the sink"
        logger.warning.assert_called_once()
