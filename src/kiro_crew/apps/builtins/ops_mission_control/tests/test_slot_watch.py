"""Tests for slot-derived incident status.

The property under test is that the board TELLS THE TRUTH about what needs a
person. An agent parked on a tool approval has stopped working, but nothing in the
dispatch path notices — so before this existed the board showed "Dispatched"
(progressing) for an incident that was actually waiting on the operator. Since the
operator reads the board specifically to find what needs them, that is the worst
possible thing for it to get wrong, and it fails silently.

Observed live: the investigating agent's FIRST action was
a read-only AWS probe, which parked on a ``permission`` message while the incident
was still ``dispatched`` — which is why that edge has to be legal.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path


class _HomeIsolated(unittest.TestCase):
    def setUp(self):
        import os

        self.tmp = Path(tempfile.mkdtemp())
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = str(self.tmp)
        self._clear_caches()

    def tearDown(self):
        import os

        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        self._clear_caches()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _clear_caches():
        from kiro_crew.config import loader

        for name in ("config_dir", "_config_dir"):
            fn = getattr(loader, name, None)
            if fn is not None and hasattr(fn, "cache_clear"):
                fn.cache_clear()

    def _claim(self, status=None):
        from kiro_crew.apps.builtins.ops_mission_control.backend import store
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            MODE_OBSERVE,
            Signal,
        )

        sig = Signal.create(source="cloudwatch", native_id="alarm/x", title="thing broke")
        inc = store.claim(sig, operating_mode=MODE_OBSERVE)
        assert inc is not None
        if status and status != inc.status:
            inc = store.transition(inc.incident_id, status)
        return inc


class TestTransitionGrammar(unittest.TestCase):
    def test_dispatched_can_reach_needs_human(self):
        """An agent can block on approval before finishing its first turn."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            LEGAL_TRANSITIONS,
            STATUS_DISPATCHED,
            STATUS_NEEDS_HUMAN,
        )

        self.assertIn(STATUS_NEEDS_HUMAN, LEGAL_TRANSITIONS[STATUS_DISPATCHED])

    def test_needs_human_can_go_stale(self):
        """An unanswered incident must not pin its signal as claimed forever."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            LEGAL_TRANSITIONS,
            STATUS_NEEDS_HUMAN,
            STATUS_STALE,
        )

        self.assertIn(STATUS_STALE, LEGAL_TRANSITIONS[STATUS_NEEDS_HUMAN])


class TestDeriveStatus(_HomeIsolated):
    def test_pending_approval_flag_blocks(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import slot_watch
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            BLOCKED_ON_APPROVAL,
            STATUS_NEEDS_HUMAN,
        )

        inc = self._claim()
        status, reason = slot_watch.derive_status(inc, {"pending_approval": True})
        self.assertEqual(status, STATUS_NEEDS_HUMAN)
        self.assertEqual(reason, BLOCKED_ON_APPROVAL)

    def test_trailing_permission_message_blocks_even_without_the_flag(self):
        """The flag lags the transcript — the message lands first.

        This is the real observed shape: a slot whose last message is a
        ``permission`` role while ``pending_approval`` is still falsey. Relying on
        the flag alone leaves the board wrong for however long that gap is.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import slot_watch
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            BLOCKED_ON_APPROVAL,
            STATUS_NEEDS_HUMAN,
        )

        inc = self._claim()
        slot = {
            "pending_approval": False,
            "running": True,
            "messages": [
                {"role": "user"},
                {"role": "assistant"},
                {"role": "tool"},
                {"role": "permission"},
            ],
        }
        status, reason = slot_watch.derive_status(inc, slot)
        self.assertEqual(status, STATUS_NEEDS_HUMAN)
        self.assertEqual(reason, BLOCKED_ON_APPROVAL)

    def test_waiting_for_input_blocks(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import slot_watch
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            BLOCKED_ON_INPUT,
            STATUS_NEEDS_HUMAN,
        )

        inc = self._claim()
        status, reason = slot_watch.derive_status(inc, {"waiting_for_input": True})
        self.assertEqual(status, STATUS_NEEDS_HUMAN)
        self.assertEqual(reason, BLOCKED_ON_INPUT)

    def test_running_slot_is_investigating_and_unblocked(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import slot_watch
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            STATUS_INVESTIGATING,
        )

        inc = self._claim()
        status, reason = slot_watch.derive_status(inc, {"running": True, "messages": []})
        self.assertEqual(status, STATUS_INVESTIGATING)
        self.assertEqual(reason, "")

    def test_approval_wins_over_running(self):
        """A running slot that is parked is still parked."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import slot_watch
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            STATUS_NEEDS_HUMAN,
        )

        inc = self._claim()
        status, _ = slot_watch.derive_status(inc, {"running": True, "pending_approval": True})
        self.assertEqual(status, STATUS_NEEDS_HUMAN)

    def test_idle_slot_with_turns_but_no_diagnosis_needs_a_person(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import slot_watch
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            BLOCKED_ON_DIAGNOSIS,
            STATUS_NEEDS_HUMAN,
        )

        inc = self._claim()
        slot = {"running": False, "messages": [{"role": "user"}, {"role": "assistant"}]}
        status, reason = slot_watch.derive_status(inc, slot)
        self.assertEqual(status, STATUS_NEEDS_HUMAN)
        self.assertEqual(reason, BLOCKED_ON_DIAGNOSIS)

    def test_missing_slot_changes_nothing(self):
        """No slot is NOT evidence — the agent may not have created it yet."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import slot_watch

        inc = self._claim()
        status, reason = slot_watch.derive_status(inc, None)
        self.assertEqual(status, inc.status)
        self.assertEqual(reason, inc.blocked_reason)

    def test_idle_slot_with_a_diagnosis_is_left_alone(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import slot_watch, store

        inc = self._claim()
        inc = store.update_fields(inc.incident_id, diagnosis="root cause found")
        slot = {"running": False, "messages": [{"role": "user"}, {"role": "assistant"}]}
        status, reason = slot_watch.derive_status(inc, slot)
        self.assertEqual(status, inc.status)
        self.assertEqual(reason, "")

    def test_recording_a_diagnosis_clears_awaiting_diagnosis(self):
        """The write-back loop: Phase 4 is what un-flags the incident.

        Verified against real incidents — an agent that produced a
        full root-cause analysis in chat but never called ``/incident/transition``
        still read as "Stopped, no diagnosis" on the board, which misreports a
        finished analysis as a dead end. Recording the diagnosis must clear it.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import slot_watch, store
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            BLOCKED_ON_DIAGNOSIS,
        )

        inc = self._claim()
        idle_with_turns = {
            "running": False,
            "messages": [{"role": "user"}, {"role": "assistant"}],
        }
        # Before Phase 4: flagged as stopped without a conclusion.
        slot_watch.reconcile(inc.incident_id, idle_with_turns)
        mid = store.get_incident(inc.incident_id)
        assert mid is not None
        self.assertEqual(mid.blocked_reason, BLOCKED_ON_DIAGNOSIS)

        # Phase 4 records the finding — the same call the SOP now spells out.
        store.update_fields(inc.incident_id, diagnosis="target-side trust policy denies the caller")
        slot_watch.reconcile(inc.incident_id, idle_with_turns)

        after = store.get_incident(inc.incident_id)
        assert after is not None
        self.assertEqual(after.blocked_reason, "")
        self.assertTrue(after.diagnosis)


class TestReconcile(_HomeIsolated):
    def test_blocked_incident_is_persisted_as_needs_human(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import slot_watch, store
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            BLOCKED_ON_APPROVAL,
            STATUS_NEEDS_HUMAN,
        )

        inc = self._claim()  # dispatched
        changed = slot_watch.reconcile(inc.incident_id, {"pending_approval": True})
        self.assertIsNotNone(changed)
        stored = store.get_incident(inc.incident_id)
        assert stored is not None
        self.assertEqual(stored.status, STATUS_NEEDS_HUMAN)
        self.assertEqual(stored.blocked_reason, BLOCKED_ON_APPROVAL)

    def test_approving_clears_the_block_on_the_next_reconcile(self):
        """This is why the reason is DERIVED and not stored as intent."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import slot_watch, store
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            STATUS_INVESTIGATING,
        )

        inc = self._claim()
        slot_watch.reconcile(inc.incident_id, {"pending_approval": True})
        # Operator approves from the embedded chat: the slot resumes running.
        slot_watch.reconcile(inc.incident_id, {"running": True, "messages": []})
        stored = store.get_incident(inc.incident_id)
        assert stored is not None
        self.assertEqual(stored.status, STATUS_INVESTIGATING)
        self.assertEqual(stored.blocked_reason, "")

    def test_no_change_returns_none(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import slot_watch

        inc = self._claim()
        self.assertIsNone(slot_watch.reconcile(inc.incident_id, None))

    def test_terminal_incident_is_never_revived(self):
        """A resolved incident whose slot still exists must stay resolved."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import slot_watch, store
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            STATUS_INVESTIGATING,
            STATUS_RESOLVED,
        )

        inc = self._claim()
        store.transition(inc.incident_id, STATUS_INVESTIGATING)
        store.transition(inc.incident_id, STATUS_RESOLVED)
        self.assertIsNone(slot_watch.reconcile(inc.incident_id, {"pending_approval": True}))
        stored = store.get_incident(inc.incident_id)
        assert stored is not None
        self.assertEqual(stored.status, STATUS_RESOLVED)

    def test_unknown_incident_is_a_noop(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import slot_watch

        self.assertIsNone(slot_watch.reconcile("INV-999", {"pending_approval": True}))

    def test_blocked_summary_counts_by_reason(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import slot_watch, store
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            BLOCKED_ON_APPROVAL,
        )

        inc = self._claim()
        slot_watch.reconcile(inc.incident_id, {"pending_approval": True})
        summary = slot_watch.blocked_summary(store.open_incidents())
        self.assertEqual(summary.get(BLOCKED_ON_APPROVAL), 1)

    def test_blocked_incident_still_counts_as_open_work(self):
        """A blocked incident must not vanish from the board."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import slot_watch, store

        inc = self._claim()
        slot_watch.reconcile(inc.incident_id, {"pending_approval": True})
        self.assertIn(inc.incident_id, [i.incident_id for i in store.open_incidents()])


class TestPersistence(_HomeIsolated):
    def test_blocked_reason_round_trips_through_the_index(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import slot_watch, store
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            BLOCKED_ON_APPROVAL,
        )

        inc = self._claim()
        slot_watch.reconcile(inc.incident_id, {"pending_approval": True})
        raw = json.loads(store.index_path().read_text(encoding="utf-8"))
        self.assertEqual(raw[inc.incident_id]["blocked_reason"], BLOCKED_ON_APPROVAL)
        # ...and survives a re-read through from_dict.
        reread = store.get_incident(inc.incident_id)
        assert reread is not None
        self.assertEqual(reread.blocked_reason, BLOCKED_ON_APPROVAL)


class TestReconcileKeepsItsTolerantContract(_HomeIsolated):
    """`reconcile` is a background truth-sync, so it must survive what the store refuses.

    The store's mutation base is now strict: `transition` reads the index for update and
    RAISES rather than publishing over a read it could not make. That is right for the
    store and wrong to let escape here -- reconciliation's whole job is to survive a mess,
    and it already skips illegal transitions and lost races by returning `None`.

    Tolerating the refusal is safe BECAUSE the store refused: the mutation was abandoned
    before writing, so the incident keeps the status it already had, which is exactly what
    `None` already means to every caller. The next reconcile pass retries from a clean
    read. Found in review (GPT 5.6), whose suggested remedy was to revert the strict
    reader -- that would restore the silent-truncation bug, so the tolerance was moved to
    the caller that wants it instead.

    On WHY these stub `store.transition` rather than making the file unreadable: `reconcile`
    looks the incident up through `store.get_incident`, which goes through the LENIENT
    `read_index`. So a fully unreadable index makes that lookup answer `None` and the
    function returns before `transition` is ever called -- a file-level fault cannot reach
    the path under test, and a test written that way passes without exercising it (mine did,
    until the mutation probe caught it). The reachable window is the transient one GPT
    named: the lookup succeeds, then `transition`'s own for-update read fails. Stubbing the
    write to raise is that window, exactly.
    """

    @staticmethod
    def _refusing_write(*_a, **_kw):
        raise PermissionError(13, "Permission denied")

    def test_a_refused_transition_skips_the_reconcile_instead_of_raising(self):
        from unittest import mock

        from kiro_crew.apps.builtins.ops_mission_control.backend import slot_watch, store
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            STATUS_NEEDS_HUMAN,
        )

        inc = self._claim()
        # A slot that really derives a change, so the reconcile reaches `transition`.
        # Asserted rather than assumed: a shape that derived nothing would make this
        # test pass without exercising the path at all.
        slot = {"pending_approval": True}
        derived, _ = slot_watch.derive_status(inc, slot)
        self.assertEqual(derived, STATUS_NEEDS_HUMAN, "premise: this slot derives a change")

        with mock.patch.object(store, "transition", self._refusing_write):
            self.assertIsNone(
                slot_watch.reconcile(inc.incident_id, slot),
                "a refused transition must skip the reconcile, not raise out of it",
            )

    def test_the_incident_keeps_the_status_it_already_had(self):
        """The durable half: a skipped reconcile must not have changed anything."""
        from unittest import mock

        from kiro_crew.apps.builtins.ops_mission_control.backend import slot_watch, store

        inc = self._claim()
        before = inc.status

        with mock.patch.object(store, "transition", self._refusing_write):
            slot_watch.reconcile(inc.incident_id, {"pending_approval": True})

        after = store.get_incident(inc.incident_id)
        assert after is not None
        self.assertEqual(after.status, before, "a skipped reconcile changed the incident")

    def test_a_corrupt_index_is_refused_rather_than_tolerated(self):
        """The deliberate asymmetry: EACCES is tolerated here, corruption is not.

        The difference is persistence. An unreadable index is transient -- the next
        reconcile probably succeeds, so skipping one status sync costs nothing. A
        corrupt index fails identically on every future reconcile until a person fixes
        the file, so tolerating it means this instance quietly stops syncing status
        forever while the board still renders.

        `JSONDecodeError` subclasses `ValueError`, which the tolerant clause already
        catches for the unrelated illegal-transition case -- so without an explicit
        clause the two failure modes would share one handler and corruption would be
        filed under "we lost a race" at debug level. This pins that they do not.

        Stubbed rather than driven from a corrupt file for the same reason as the
        `OSError` tests above: `reconcile` looks the incident up through the LENIENT
        `read_index`, which answers empty on a corrupt file, so the function returns
        before `transition` is reached. The reachable window is the transient one --
        the lookup parses, then the write's own read meets the corruption.
        """
        import json
        from unittest import mock

        from kiro_crew.apps.builtins.ops_mission_control.backend import slot_watch, store

        inc = self._claim()

        def _corrupt(*_a, **_kw):
            raise json.JSONDecodeError("Expecting value", "{ not json", 2)

        with mock.patch.object(store, "transition", _corrupt):
            with self.assertRaises(json.JSONDecodeError):
                slot_watch.reconcile(inc.incident_id, {"pending_approval": True})


if __name__ == "__main__":
    unittest.main()
