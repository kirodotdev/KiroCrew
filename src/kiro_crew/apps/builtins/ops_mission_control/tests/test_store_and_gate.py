"""Tests for the dispatch index and the autonomy gate.

The claim tests cover the property that prevents duplicate investigations, and the
autonomy tests cover the property that prevents the agent writing to a stranger's
production tooling without an explicit, scoped grant. Both are the kind of thing
that fails silently in production if it regresses, which is why they are asserted
directly rather than through the HTTP layer. The ledger, tier arming, write-gate and
index read-modify-write tests are in the sibling ``test_store_and_gate_<topic>.py`` files.

``KIROCREW_HOME`` is redirected to a temp dir so nothing here touches the real
data home.
"""

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from kiro_crew.apps.builtins.ops_mission_control.tests.store_and_gate_helpers import _HomeIsolated


class TestClaim(_HomeIsolated):
    def test_claim_creates_incident(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        self.assertIsNotNone(inc)
        assert inc is not None
        self.assertEqual(inc.status, models.STATUS_DISPATCHED)
        self.assertTrue(inc.incident_id.startswith("INV-"))

    def test_second_claim_of_same_signal_loses(self):
        """Exactly one caller may own a signal — this is what stops duplicate
        investigations when two heartbeats overlap."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        sig = self._signal()
        first = store.claim(sig, operating_mode=models.MODE_OBSERVE)
        second = store.claim(sig, operating_mode=models.MODE_OBSERVE)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_stale_incident_is_reclaimable_in_place(self):
        """Re-pickup must reuse the timeline, not accumulate a new incident."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        sig = self._signal()
        first = store.claim(sig, operating_mode=models.MODE_OBSERVE)
        assert first is not None
        store.transition(first.incident_id, models.STATUS_STALE)
        again = store.claim(sig, operating_mode=models.MODE_OBSERVE)
        self.assertIsNotNone(again)
        assert again is not None
        self.assertEqual(again.incident_id, first.incident_id)
        self.assertEqual(again.status, models.STATUS_DISPATCHED)
        self.assertEqual(len(store.read_index()), 1)

    def test_a_resolved_alarm_that_refires_is_claimed_again(self):
        """The bug that made the app's whole premise unreachable.

        ``signal.id`` is stable for the alarm's lifetime
        (``cloudwatch:alarm/DlqDepth`` forever), and ``claim`` treated ANY existing
        incident — including a closed one — as "accounted for". So once an alarm was
        resolved, the app **permanently stopped responding to it**: proven live, a
        resolved alarm re-firing on days 2, 3 and 30 all returned ``None``.

        Worse, it made the compounding-memory fast path unreachable in production. That
        payoff can only happen on a SECOND occurrence, and a second occurrence could
        never be claimed — so the feature the app is built around could never fire.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        sig = self._signal()
        first = store.claim(sig, operating_mode=models.MODE_OBSERVE)
        assert first is not None
        store.transition(first.incident_id, models.STATUS_INVESTIGATING)
        store.transition(first.incident_id, models.STATUS_RESOLVED)

        again = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        self.assertIsNotNone(again, "a resolved alarm that fires again must be claimable")
        assert again is not None
        # A recurrence is a NEW incident, not a reopening: the first one owns its
        # diagnosis, resolution, and Slack thread, and overwriting those would destroy
        # the record that makes the ledger trustworthy.
        self.assertNotEqual(again.incident_id, first.incident_id)
        self.assertEqual(again.status, models.STATUS_DISPATCHED)
        closed = store.get_incident(first.incident_id)
        assert closed is not None
        self.assertEqual(closed.status, models.STATUS_RESOLVED, "history must survive")

    def test_an_escalated_alarm_that_refires_is_claimed_again(self):
        """Escalated is the other terminal state and must behave identically."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        first = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert first is not None
        store.transition(first.incident_id, models.STATUS_INVESTIGATING)
        store.transition(first.incident_id, models.STATUS_ESCALATED)
        self.assertIsNotNone(store.claim(self._signal(), operating_mode=models.MODE_OBSERVE))

    def test_an_open_incident_still_blocks_a_duplicate_claim(self):
        """The recurrence fix must NOT weaken the dedupe it sits next to.

        needs_human is the trap: it is waiting on a person, not closed, so a heartbeat
        must not open a second incident for the same still-firing alarm.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        for status in (
            models.STATUS_DISPATCHED,
            models.STATUS_INVESTIGATING,
            models.STATUS_NEEDS_HUMAN,
        ):
            with self.subTest(status=status):
                # A distinct alarm per subtest, so each case is independent without
                # needing to tear down and rebuild the data home.
                native = f"alarm/open-{status}"
                first = store.claim(self._signal(native), operating_mode=models.MODE_OBSERVE)
                assert first is not None
                if status != models.STATUS_DISPATCHED:
                    store.transition(first.incident_id, models.STATUS_INVESTIGATING)
                if status == models.STATUS_NEEDS_HUMAN:
                    store.transition(first.incident_id, models.STATUS_NEEDS_HUMAN)
                self.assertIsNone(
                    store.claim(self._signal(native), operating_mode=models.MODE_OBSERVE),
                    f"an incident in {status} must still own its signal",
                )

    def test_closed_incidents_are_pruned_but_open_work_never_is(self):
        """Bounds the index that making resolved alarms re-claimable just unbounded.

        A flapping alarm on the 2-minute dispatch cadence now mints one incident per
        flap, and every claim re-reads and re-writes the whole index — measured
        superlinear (50 entries → 6ms/claim, 450 → 53ms). A month of one flapping alarm
        projects to ~21,600 incidents. Open work is exempt at any age: live work
        disappearing because history is long would be far worse than a large index.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        # 12 closed, then 2 still open.
        for n in range(12):
            inc = store.claim(self._signal(f"alarm/closed-{n}"), operating_mode=models.MODE_OBSERVE)
            assert inc is not None
            store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
            store.transition(inc.incident_id, models.STATUS_RESOLVED)
        open_ids = []
        for n in range(2):
            inc = store.claim(self._signal(f"alarm/open-{n}"), operating_mode=models.MODE_OBSERVE)
            assert inc is not None
            open_ids.append(inc.incident_id)

        removed = store.prune_closed(keep=5)
        self.assertEqual(removed, 7)
        index = store.read_index()
        self.assertEqual(len(index), 7, "5 kept closed + 2 open")
        for incident_id in open_ids:
            self.assertIn(incident_id, index, "open work must survive pruning")

    def test_pruning_keeps_the_most_recently_closed(self):
        """Ordered by when it CLOSED, so a long-running incident that just finished is
        treated as recent rather than aged out by its claim time."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        last = None
        for n in range(6):
            inc = store.claim(self._signal(f"alarm/c-{n}"), operating_mode=models.MODE_OBSERVE)
            assert inc is not None
            store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
            last = store.transition(inc.incident_id, models.STATUS_RESOLVED)
        assert last is not None
        store.prune_closed(keep=2)
        self.assertIn(last.incident_id, store.read_index(), "newest close must be kept")

    def test_pruning_under_the_cap_changes_nothing(self):
        """The daily pass must stay silent when there is nothing to retire."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        store.transition(inc.incident_id, models.STATUS_RESOLVED)
        self.assertEqual(store.prune_closed(keep=500), 0)
        self.assertEqual(len(store.read_index()), 1)

    def test_a_flapping_alarm_index_stays_bounded(self):
        """The scenario end to end: repeated flap, then one maintenance pass."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        for _ in range(25):
            inc = store.claim(self._signal("alarm/Flappy"), operating_mode=models.MODE_OBSERVE)
            assert inc is not None, "each flap opens a fresh incident"
            store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
            store.transition(inc.incident_id, models.STATUS_RESOLVED)
        self.assertEqual(len(store.read_index()), 25, "unbounded before maintenance")
        store.prune_closed(keep=10)
        self.assertEqual(len(store.read_index()), 10)

    def test_terminal_statuses_are_derived_from_the_grammar(self):
        """Derived, not hand-listed, so the two can never disagree.

        A status with no outgoing transition IS terminal by definition; keeping a second
        literal list would be one more thing to forget when a status is added.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        self.assertEqual(
            models.TERMINAL_STATUSES,
            frozenset({models.STATUS_RESOLVED, models.STATUS_ESCALATED}),
        )
        for status in models.TERMINAL_STATUSES:
            self.assertEqual(models.LEGAL_TRANSITIONS[status], frozenset())
        # And nothing open may be misfiled as terminal.
        self.assertFalse(models.TERMINAL_STATUSES & models.OPEN_STATUSES)

    def test_ids_increment(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        a = store.claim(self._signal("alarm/a"), operating_mode=models.MODE_OBSERVE)
        b = store.claim(self._signal("alarm/b"), operating_mode=models.MODE_OBSERVE)
        assert a is not None and b is not None
        self.assertNotEqual(a.incident_id, b.incident_id)


class TestTransition(_HomeIsolated):
    def test_illegal_transition_refused(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert inc is not None
        # Back to unclaimed is not legal: an incident that exists has been claimed by
        # definition, and "release it" is `stale`, not a rewind.
        #
        # ``dispatched -> resolved`` is NOT a usable example here: it is a REQUIRED edge,
        # because reconcile must close an incident whose signal cleared before the
        # agent's first turn. Same-status is deliberately a no-op update (see
        # store.transition), so it is not a usable example of illegality either.
        with self.assertRaises(ValueError):
            store.transition(inc.incident_id, models.STATUS_UNCLAIMED)

    def test_terminal_state_cannot_transition(self):
        """A resolved incident is final; a signal that returns is a NEW incident."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_RESOLVED)
        for target in (
            models.STATUS_INVESTIGATING,
            models.STATUS_DISPATCHED,
            models.STATUS_NEEDS_HUMAN,
        ):
            with self.assertRaises(ValueError):
                store.transition(inc.incident_id, target)

    def test_legal_path_works(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        final = store.transition(inc.incident_id, models.STATUS_RESOLVED, resolution="cleared")
        self.assertEqual(final.status, models.STATUS_RESOLVED)
        self.assertEqual(final.resolution, "cleared")

    def test_unknown_incident_raises_keyerror(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        with self.assertRaises(KeyError):
            store.transition("INV-999", models.STATUS_INVESTIGATING)

    def test_terminal_state_is_final(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        store.transition(inc.incident_id, models.STATUS_RESOLVED)
        with self.assertRaises(ValueError):
            store.transition(inc.incident_id, models.STATUS_INVESTIGATING)

    def test_same_status_is_a_field_update_not_a_transition(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert inc is not None
        updated = store.update_fields(inc.incident_id, diagnosis="pool exhaustion")
        self.assertEqual(updated.diagnosis, "pool exhaustion")
        self.assertEqual(updated.status, models.STATUS_DISPATCHED)


class TestClosingAnIncidentLeavesAnArtifact(_HomeIsolated):
    """A closed incident must leave a Markdown record a non-KiroCrew reader can be handed.

    The renderer existed for the whole life of the app with exactly one reference — its own
    definition — so ``incidents/<id>.md`` was documented on-disk state that could not
    exist, and ``/incident``'s ``log`` was a structurally-empty field. These tests pin the
    call site rather than the renderer, because the renderer was never the broken half.
    """

    def test_resolving_an_incident_writes_its_postmortem(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(title="DLQ depth above 100"), operating_mode="observe")
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING, diagnosis="pool exhausted")
        store.transition(inc.incident_id, models.STATUS_RESOLVED, resolution="raised the cap")

        path = store.incident_log_path(inc.incident_id)
        self.assertTrue(path.is_file(), "resolving an incident wrote no postmortem")
        body = path.read_text(encoding="utf-8")
        self.assertIn("DLQ depth above 100", body)
        self.assertIn("pool exhausted", body)
        self.assertIn("raised the cap", body)
        # The route that serves it must find it too — a file nothing can read is no better
        # than no file.
        self.assertEqual(store.read_log(inc.incident_id), body)

    def test_escalating_also_writes_one(self):
        """Both terminal statuses, not just the happy one.

        An escalation is the case where a colleague is MOST likely to be handed the record,
        so writing the artifact only on ``resolved`` would miss the main use.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode="observe")
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        store.transition(inc.incident_id, models.STATUS_ESCALATED, diagnosis="beyond me")
        self.assertTrue(store.incident_log_path(inc.incident_id).is_file())

    def test_an_open_incident_has_no_artifact_yet(self):
        """A postmortem describes a finished investigation; writing one early would lie."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode="observe")
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING, diagnosis="looking")
        self.assertFalse(store.incident_log_path(inc.incident_id).is_file())
        self.assertEqual(store.read_log(inc.incident_id), "")

    def test_every_terminal_status_is_covered_by_the_writer(self):
        """Derived from the grammar, so a new terminal status cannot ship without an artifact.

        ``TERMINAL_STATUSES`` is computed from ``LEGAL_TRANSITIONS``, so a future status
        with no outgoing edge becomes terminal automatically — and would silently produce
        no record if this test named the two statuses by hand instead.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        for status in sorted(models.TERMINAL_STATUSES):
            inc = store.claim(self._signal(native_id=f"alarm/{status}"), operating_mode="observe")
            assert inc is not None
            store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
            store.transition(inc.incident_id, status)
            self.assertTrue(
                store.incident_log_path(inc.incident_id).is_file(),
                f"closing into {status!r} left no postmortem",
            )

    def test_a_later_field_update_cannot_blank_a_good_artifact(self):
        """The record is sourced from the incident, never from the caller's kwargs.

        ``update_fields`` re-enters ``transition`` with the SAME (terminal) status and no
        diagnosis, so a writer reading its arguments would re-render the artifact with
        empty sections — destroying the finished record on an unrelated write.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode="observe")
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        store.transition(inc.incident_id, models.STATUS_RESOLVED, diagnosis="thread pool starved")
        store.update_fields(inc.incident_id, slack_thread_ts="1700000000.000100")

        body = store.read_log(inc.incident_id)
        self.assertIn("thread pool starved", body)
        self.assertNotIn("_pending_", body)

    def test_the_artifact_is_owner_only(self):
        """It describes production failures and is meant to be shared BY CHOICE.

        The directory has been 0o700 from the start; the file inside it inherited the
        umask, so on a machine with a permissive umask the filesystem decided who could
        read it before the operator did.
        """
        import os
        import stat

        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        if os.name == "nt":  # pragma: no cover - POSIX permission bits only
            self.skipTest("POSIX mode bits")
        inc = store.claim(self._signal(), operating_mode="observe")
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        store.transition(inc.incident_id, models.STATUS_RESOLVED)
        mode = stat.S_IMODE(store.incident_log_path(inc.incident_id).stat().st_mode)
        self.assertEqual(mode & 0o077, 0, f"postmortem is readable beyond its owner: {mode:o}")

    def test_a_failed_write_does_not_fail_the_close(self):
        """A record of a state change must never be able to fail the state change.

        Same reasoning the Slack mirror already runs on: by the time the artifact is
        rendered the index write is durable, so an unwritable data directory must leave the
        incident resolved rather than raising out of ``transition``.
        """
        from unittest import mock

        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode="observe")
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        with mock.patch.object(store, "write_log", side_effect=OSError("read-only fs")):
            closed = store.transition(inc.incident_id, models.STATUS_RESOLVED)
        self.assertEqual(closed.status, models.STATUS_RESOLVED)
        persisted = store.get_incident(inc.incident_id)
        assert persisted is not None
        self.assertEqual(persisted.status, models.STATUS_RESOLVED)

    def test_a_credential_in_provider_text_never_reaches_the_artifact(self):
        """The postmortem is the one file an operator is EXPECTED to hand to someone else.

        So it is the worst possible place for a leaked credential: the operator forwards it
        themselves, with their own confidence behind it. The renderer interpolated provider
        titles and a model-authored diagnosis verbatim and no ``redact`` call existed in
        this module at all — invisible only because no caller ever produced a file.

        Covers both scanners on purpose. Core alone misses a bare-hex Datadog key and a
        prefix-less bearer token; the app's token pass alone misses an AWS access key id.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        dd_key = "0123456789abcdef0123456789abcdef"
        signal = self._signal(title=f"probe failed: DD-API-KEY: {dd_key}", resource="svc/api")
        inc = store.claim(signal, operating_mode="observe")
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        store.transition(
            inc.incident_id,
            models.STATUS_RESOLVED,
            diagnosis="the reproducer used glpat-IOSFODNN7EXAMPLE",
            resolution="rotated it; curl used Bearer sk-abcdefghijklmnopqrst",
        )

        body = store.read_log(inc.incident_id)
        self.assertTrue(body, "no artifact to check")
        self.assertNotIn(dd_key, body)
        self.assertNotIn("glpat-IOSFODNN7EXAMPLE", body)
        self.assertNotIn("sk-abcdefghijklmnopqrst", body)
        # Still a usable postmortem: only the secrets are gone, not the narrative.
        self.assertIn("probe failed", body)
        self.assertIn("the reproducer used", body)

    def test_the_narrative_survives_redaction(self):
        """Redaction must not be so eager that the artifact stops being worth sharing.

        A scanner that ate ordinary diagnostic prose would make the file useless and push
        people back to pasting the raw transcript, which is strictly worse.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        prose = "Connection pool exhausted at 14:02; tokenization-heavy requests queued."
        inc = store.claim(self._signal(), operating_mode="observe")
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        store.transition(inc.incident_id, models.STATUS_RESOLVED, diagnosis=prose)
        self.assertIn(prose, store.read_log(inc.incident_id))

    def test_pruning_the_index_keeps_the_written_record(self):
        """``prune_closed`` bounds the INDEX, and must not destroy history on disk.

        That was already the stated decision; it only became a testable claim once a file
        actually existed to delete.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode="observe")
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        store.transition(inc.incident_id, models.STATUS_RESOLVED)
        store.prune_closed(keep=0)
        self.assertIsNone(store.get_incident(inc.incident_id))
        self.assertTrue(store.incident_log_path(inc.incident_id).is_file())
        self.assertIn("## Diagnosis", store.read_log(inc.incident_id))


class TestStaleSweep(_HomeIsolated):
    @staticmethod
    def _backdate(incident_id: str, hours: int) -> None:
        """Age an incident by rewriting the index directly.

        ``transition``/``update_fields`` deliberately re-stamp ``updated_at`` —
        any transition IS activity — so a caller cannot backdate an incident.
        The sweep is therefore exercised by editing the persisted index.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import store

        path = store.index_path()
        data = json.loads(path.read_text(encoding="utf-8"))
        data[incident_id]["updated_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_idle_incident_is_released(self):
        """A dead investigation must not hold a signal claimed forever."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert inc is not None
        self._backdate(inc.incident_id, hours=5)
        released = store.sweep_stale(stale_after_secs=3600)
        self.assertIn(inc.incident_id, released)
        refreshed = store.get_incident(inc.incident_id)
        assert refreshed is not None
        self.assertEqual(refreshed.status, models.STATUS_STALE)

    def test_fresh_incident_survives(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert inc is not None
        self.assertEqual(store.sweep_stale(stale_after_secs=3600), [])

    def test_investigating_incident_is_also_sweepable(self):
        """Both pre-terminal working states can strand a signal."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        self._backdate(inc.incident_id, hours=5)
        self.assertIn(inc.incident_id, store.sweep_stale(stale_after_secs=3600))

    def test_terminal_incident_is_never_swept(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        store.transition(inc.incident_id, models.STATUS_RESOLVED)
        self._backdate(inc.incident_id, hours=99)
        self.assertEqual(store.sweep_stale(stale_after_secs=3600), [])

    def test_unanswered_needs_human_is_eventually_released(self):
        """An incident nobody answers must not pin its signal as claimed forever.

        ``LEGAL_TRANSITIONS`` has always legalised ``needs_human -> stale`` for exactly
        this reason, and the sweep never traversed it: ``needs_human`` was absent from
        ``_SWEEPABLE_STATUSES``, so the alarm was never re-claimed and nothing said so.
        The old guard asserted only that the transition was *legal*, which is why the
        gap survived — this exercises the sweep itself.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_NEEDS_HUMAN)
        # Past even the longer needs_human threshold (6× the working one).
        self._backdate(inc.incident_id, hours=99)
        released = store.sweep_stale(stale_after_secs=3600)
        self.assertIn(inc.incident_id, released)
        refreshed = store.get_incident(inc.incident_id)
        assert refreshed is not None
        self.assertEqual(refreshed.status, models.STATUS_STALE)

    def test_needs_human_gets_a_longer_grace_than_a_dead_investigation(self):
        """Waiting on a person is legitimately slower than an agent dying.

        At 5 hours idle an ``investigating`` incident is stale (its agent is gone) but a
        ``needs_human`` one is not — the operator may simply be asleep, and releasing it
        would discard the investigation's context to re-derive it.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        waiting = store.claim(
            self._signal(native_id="alarm/waiting"), operating_mode=models.MODE_OBSERVE
        )
        working = store.claim(
            self._signal(native_id="alarm/working"), operating_mode=models.MODE_OBSERVE
        )
        assert waiting is not None and working is not None
        store.transition(waiting.incident_id, models.STATUS_NEEDS_HUMAN)
        store.transition(working.incident_id, models.STATUS_INVESTIGATING)
        self._backdate(waiting.incident_id, hours=5)
        self._backdate(working.incident_id, hours=5)

        released = store.sweep_stale(stale_after_secs=3600)
        self.assertIn(working.incident_id, released)
        self.assertNotIn(waiting.incident_id, released)

    def test_needs_human_threshold_is_independently_tunable(self):
        """An operator who wants them coupled differently can say so."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert inc is not None
        store.transition(inc.incident_id, models.STATUS_NEEDS_HUMAN)
        self._backdate(inc.incident_id, hours=5)
        # Default multiplier would keep it (6h); an explicit 1h releases it.
        self.assertEqual(store.sweep_stale(stale_after_secs=3600), [])
        self.assertIn(
            inc.incident_id,
            store.sweep_stale(stale_after_secs=3600, needs_human_after_secs=3600),
        )


class TestAutonomyGate(_HomeIsolated):
    def _write_config(self, payload: dict) -> None:
        """Seed the ceiling on the KEYSTONE floor, the way the dashboard PUT does.

        Writing `mode`/`autonomy_rules` into `data/config.json` and relying on the read path
        to pick them up does not work, on purpose: `config.json` is agent-writable, so
        honouring a ceiling found there would let the constrained party set its own ceiling
        (see `policy_store`). Seeding via `policy_store` keeps these tests exercising the
        gate rather than the hole.

        Non-ceiling keys still go to `config.json`, which is where the app legitimately reads
        them, so a test that mixes both still lands each value in its real home.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store
        from kiro_crew.apps.builtins.ops_mission_control.backend import store as store_mod
        from kiro_crew.apps.manager import app_data_dir

        rest = dict(payload)
        if "mode" in rest:
            policy_store.set_mode(str(rest.pop("mode")))
        if "autonomy_rules" in rest:
            policy_store.set_rules(list(rest.pop("autonomy_rules")))
        data_dir = app_data_dir(store_mod.APP_NAME)
        (data_dir / "config.json").write_text(json.dumps(rest), encoding="utf-8")

    def test_default_is_observe(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, rotation

        self.assertEqual(rotation.app_mode(), models.MODE_OBSERVE)
        allowed, reason = rotation.authorize_action(self._signal(), models.ACTION_RESOLVE)
        self.assertFalse(allowed)
        self.assertIn("observe", reason)

    def test_act_mode_alone_is_not_enough(self):
        """Both an app-level ceiling AND a matching rule are required."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, rotation

        self._write_config({"mode": models.MODE_ACT})
        allowed, reason = rotation.authorize_action(self._signal(), models.ACTION_RESOLVE)
        self.assertFalse(allowed)
        self.assertIn("no matching act-rule", reason)

    def test_blanket_rule_is_refused(self):
        """'Act on everything from this provider' must not be expressible."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, rotation

        self._write_config(
            {
                "mode": models.MODE_ACT,
                "autonomy_rules": [{"source": "cloudwatch", "mode": models.MODE_ACT}],
            }
        )
        self.assertEqual(rotation.load_rules(), [])
        allowed, _ = rotation.authorize_action(self._signal(), models.ACTION_RESOLVE)
        self.assertFalse(allowed)

    def test_scoped_rule_grants(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, rotation

        self._write_config(
            {
                "mode": models.MODE_ACT,
                "autonomy_rules": [
                    {
                        "source": "cloudwatch",
                        "mode": models.MODE_ACT,
                        "resource_glob": "AWS/SQS/*",
                    }
                ],
            }
        )
        matching = self._signal(resource="AWS/SQS/Messages")
        allowed, _ = rotation.authorize_action(matching, models.ACTION_RESOLVE)
        self.assertTrue(allowed)

    def test_scoped_rule_does_not_leak_to_other_resources(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, rotation

        self._write_config(
            {
                "mode": models.MODE_ACT,
                "autonomy_rules": [
                    {
                        "source": "cloudwatch",
                        "mode": models.MODE_ACT,
                        "resource_glob": "AWS/SQS/*",
                    }
                ],
            }
        )
        other = self._signal(resource="AWS/RDS/DatabaseConnections")
        allowed, _ = rotation.authorize_action(other, models.ACTION_RESOLVE)
        self.assertFalse(allowed)

    def test_app_ceiling_clamps_a_permissive_rule(self):
        """A rule cannot escalate an instance the operator pinned to observe."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, rotation

        self._write_config(
            {
                "mode": models.MODE_OBSERVE,
                "autonomy_rules": [
                    {
                        "source": "cloudwatch",
                        "mode": models.MODE_ACT,
                        "resource_glob": "*",
                    }
                ],
            }
        )
        allowed, _ = rotation.authorize_action(
            self._signal(resource="anything"), models.ACTION_RESOLVE
        )
        self.assertFalse(allowed)

    def test_rule_can_narrow_which_actions(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, rotation

        self._write_config(
            {
                "mode": models.MODE_ACT,
                "autonomy_rules": [
                    {
                        "source": "cloudwatch",
                        "mode": models.MODE_ACT,
                        "resource_glob": "AWS/SQS/*",
                        "actions": [models.ACTION_COMMENT],
                    }
                ],
            }
        )
        sig = self._signal(resource="AWS/SQS/Messages")
        self.assertTrue(rotation.authorize_action(sig, models.ACTION_COMMENT)[0])
        self.assertFalse(rotation.authorize_action(sig, models.ACTION_RESOLVE)[0])

    def test_a_narrow_observe_rule_carves_an_exception_out_of_a_broad_act_grant(self):
        """When rules overlap the TIGHTEST wins, not the most permissive.

        Selection was `max(matching, key=MODE_ORDER)`, which broke the one thing a second rule
        is for. An operator who grants `act` across a service and then adds a narrow `observe`
        rule to hold back one critical queue is explicitly carving out an exception -- and got
        no protection at all, because the broad grant still won and the write was authorized.
        Asserted through `authorize_action`, the decision that actually reaches a provider, and
        it fails under `max`.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, rotation

        self._write_config(
            {
                "mode": models.MODE_ACT,
                "autonomy_rules": [
                    # Broad grant across the service...
                    {
                        "source": "cloudwatch",
                        "mode": models.MODE_ACT,
                        "resource_glob": "AWS/SQS/*",
                    },
                    # ...and the carve-out for the one queue that must stay hands-off.
                    {
                        "source": "cloudwatch",
                        "mode": models.MODE_OBSERVE,
                        "resource_glob": "AWS/SQS/payments-*",
                    },
                ],
            }
        )
        self.assertEqual(len(rotation.load_rules()), 2, "both rules must parse")

        carved_out = self._signal(resource="AWS/SQS/payments-dlq")
        allowed, reason = rotation.authorize_action(carved_out, models.ACTION_RESOLVE)
        self.assertFalse(allowed, "the narrower observe rule must win over the broad act grant")
        self.assertIn("observe", reason)

        # The broad grant still applies everywhere the carve-out does not match, so the fix
        # narrows the overlap rather than disabling the rule.
        still_granted = self._signal(resource="AWS/SQS/reports")
        self.assertTrue(rotation.authorize_action(still_granted, models.ACTION_RESOLVE)[0])

    def test_unknown_action_refused(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        allowed, reason = rotation.authorize_action(self._signal(), "delete_everything")
        self.assertFalse(allowed)
        self.assertIn("unknown action", reason)

    def test_label_match_rule(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, rotation

        self._write_config(
            {
                "mode": models.MODE_ACT,
                "autonomy_rules": [
                    {
                        "source": "cloudwatch",
                        "mode": models.MODE_ACT,
                        "label_match": {"env": "staging"},
                    }
                ],
            }
        )
        staging = self._signal(labels={"env": "staging"})
        prod = self._signal(labels={"env": "prod"})
        self.assertTrue(rotation.authorize_action(staging, models.ACTION_RESOLVE)[0])
        self.assertFalse(rotation.authorize_action(prod, models.ACTION_RESOLVE)[0])


class TestThePostmortemSaysWhetherAnythingWasVerified(_HomeIsolated):
    """The artifact is what a colleague reads with no access to the board.

    "Actions taken: silenced the alarm" is the sentence most likely to be believed as an
    OUTCOME, and for the whole life of the renderer it would have said exactly that on the
    strength of a 2xx, with nothing in the file admitting no code had looked again.
    """

    def _closed(self, **fields):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, store

        inc = store.claim(
            models.Signal.create(source="cloudwatch", native_id="alarm/dlq", title="DLQ deep"),
            operating_mode=models.MODE_ACT,
        )
        assert inc is not None
        if fields:
            store.update_fields(inc.incident_id, **fields)
        store.transition(inc.incident_id, models.STATUS_INVESTIGATING)
        store.transition(inc.incident_id, models.STATUS_RESOLVED, resolution="silenced it")
        return store.read_log(inc.incident_id)

    def test_a_still_firing_action_is_named_as_such_in_the_artifact(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            ACTION_SILENCE,
            VERIFY_STILL_FIRING,
        )

        log = self._closed(
            last_action=ACTION_SILENCE,
            verification=VERIFY_STILL_FIRING,
            verification_detail="Still firing at cloudwatch after the silence.",
        )
        self.assertIn("STILL FIRING", log)

    def test_an_unverifiable_action_says_sent_not_confirmed(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            ACTION_ACK,
            VERIFY_NOT_CHECKABLE,
        )

        log = self._closed(last_action=ACTION_ACK, verification=VERIFY_NOT_CHECKABLE)
        self.assertIn("cannot observe", log)
        self.assertIn("not as confirmed", log)

    def test_an_incident_that_closed_before_its_recheck_ran_admits_it(self):
        """`resolved` is terminal, so this is the last word on it — it must not imply success."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            ACTION_SILENCE,
            VERIFY_PENDING,
        )

        log = self._closed(last_action=ACTION_SILENCE, verification=VERIFY_PENDING)
        self.assertIn("NOT CONFIRMED", log)

    def test_an_incident_with_no_action_gets_no_verification_line_at_all(self):
        """Most incidents. A "not applicable" line on every one buries the cases that matter."""
        log = self._closed()
        self.assertNotIn("Verification", log)

    def test_the_verification_detail_goes_through_the_redactor(self):
        """It quotes a provider's own poll-failure text, which can carry a credential."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            ACTION_SILENCE,
            VERIFY_UNKNOWN,
        )

        log = self._closed(
            last_action=ACTION_SILENCE,
            verification=VERIFY_UNKNOWN,
            verification_detail="401 from https://api.datadoghq.com?api_key=" + "a" * 32,
        )
        self.assertNotIn("a" * 32, log)


class TestTheSweepWindowsAreReadableBack(_HomeIsolated):
    """``PUT /settings`` accepted these three and no read path returned any of them.

    So an operator could change how long a dead investigation pins a signal and get no
    confirmation, no way to look the value up again, and — for the untouched case, which is
    every install — no way to discover the defaults they were already living under. The
    knob turned and the dial did not exist. A regression here silently restores that:
    nothing else in the app would fail, which is precisely why it survived so long.
    """

    def test_the_defaults_in_force_are_reported_without_any_config(self):
        """The commonest case. An install that never touched these still has values."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, rotation, store

        windows = rotation.sweep_windows()
        self.assertEqual(windows["max_claims_per_cycle"], dispatch.DEFAULT_MAX_CLAIMS_PER_CYCLE)
        self.assertEqual(windows["stale_after_secs"], dispatch.DEFAULT_STALE_AFTER_SECS)
        self.assertEqual(
            windows["needs_human_stale_after_secs"],
            dispatch.DEFAULT_STALE_AFTER_SECS * store.DEFAULT_NEEDS_HUMAN_STALE_MULTIPLIER,
        )

    def test_an_unset_needs_human_window_is_reported_resolved_not_as_zero(self):
        """Unset does NOT mean "never released" — ``sweep_stale`` derives it.

        Reporting the raw stored ``0`` would tell an operator an unanswered question holds
        its signal forever, which is the opposite of what the sweep does.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation, store
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            set_top_level,
        )

        set_top_level("stale_after_secs", 600)
        windows = rotation.sweep_windows()
        self.assertEqual(
            windows["needs_human_stale_after_secs"],
            600 * store.DEFAULT_NEEDS_HUMAN_STALE_MULTIPLIER,
        )
        # And it says the number is ours, not the operator's — a derived window MOVES when
        # the working threshold changes, and the UI must be able to say which it is.
        self.assertTrue(windows["needs_human_derived"])

    def test_an_explicitly_set_needs_human_window_is_reported_as_pinned(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            set_top_level,
        )

        set_top_level("stale_after_secs", 600)
        set_top_level("needs_human_stale_after_secs", 9000)
        windows = rotation.sweep_windows()
        self.assertEqual(windows["needs_human_stale_after_secs"], 9000)
        self.assertFalse(windows["needs_human_derived"])

    def test_the_reported_windows_are_the_ones_the_heartbeat_actually_applies(self):
        """The whole value of the dial is that it points where the machine is going.

        ``rotation`` has to duplicate the config-key strings (importing them would close an
        import cycle with ``dispatch``), so a rename on either side would leave this panel
        confidently displaying a default while the heartbeat used the operator's value.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, rotation

        self.assertEqual(rotation._CONFIG_MAX_CLAIMS, dispatch._CONFIG_MAX_CLAIMS)
        self.assertEqual(rotation._CONFIG_STALE_AFTER, dispatch._CONFIG_STALE_AFTER)
        self.assertEqual(
            rotation._CONFIG_NEEDS_HUMAN_STALE_AFTER,
            dispatch._CONFIG_NEEDS_HUMAN_STALE_AFTER,
        )

    def test_a_corrupt_stored_window_falls_back_instead_of_reporting_nonsense(self):
        """``_config_int`` already guards this; the dial must inherit that, not bypass it."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch, rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            set_top_level,
        )

        set_top_level("stale_after_secs", "not a number")
        self.assertEqual(
            rotation.sweep_windows()["stale_after_secs"], dispatch.DEFAULT_STALE_AFTER_SECS
        )


class TestClaimProvenance(unittest.TestCase):
    """`claimed_at` recorded WHEN; nothing recorded BY WHAT.

    There are two paths into `store.claim` — the dispatch heartbeat and the board's
    manual claim — and after the fact they were indistinguishable. That is the question
    an operator actually asks of a surprising incident: did the agent decide to pick this
    up, or did I? Recording the claiming PATH is the smallest answer that distinguishes
    them.
    """

    def setUp(self):
        import os

        self.tmp = tempfile.mkdtemp()
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp

    def tearDown(self):
        import os

        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _signal(self, native_id="alarm/dlq"):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        return models.Signal.create(source="cloudwatch", native_id=native_id, title="DLQ deep")

    def test_the_heartbeat_is_the_default(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import (
            models,
            store,
        )

        """A caller that says nothing is the cron, which is the overwhelming majority."""
        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE)
        assert inc is not None
        self.assertEqual(inc.claimed_by, models.CLAIMED_BY_HEARTBEAT)

    def test_an_operator_claim_is_recorded_as_such(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import (
            models,
            store,
        )

        inc = store.claim(
            self._signal(),
            operating_mode=models.MODE_OBSERVE,
            claimed_by=models.CLAIMED_BY_OPERATOR,
        )
        assert inc is not None
        self.assertEqual(inc.claimed_by, models.CLAIMED_BY_OPERATOR)

    def test_an_unknown_claimant_is_coerced_not_stored(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import (
            models,
            store,
        )

        """This value reaches the board and the digest, so it must not be free text.

        Storing whatever a caller passed would let a typo render as provenance — and a
        wrong provenance is worse than an absent one, because it reads as a fact.
        """
        inc = store.claim(self._signal(), operating_mode=models.MODE_OBSERVE, claimed_by="nonsense")
        assert inc is not None
        self.assertEqual(inc.claimed_by, models.CLAIMED_BY_HEARTBEAT)

    def test_it_round_trips_through_the_index(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import (
            models,
            store,
        )

        """The field is useless if it does not survive a reload."""
        inc = store.claim(
            self._signal(),
            operating_mode=models.MODE_OBSERVE,
            claimed_by=models.CLAIMED_BY_OPERATOR,
        )
        assert inc is not None
        reloaded = store.get_incident(inc.incident_id)
        assert reloaded is not None
        self.assertEqual(reloaded.claimed_by, models.CLAIMED_BY_OPERATOR)

    def test_an_incident_written_before_the_field_existed_still_loads(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import models

        """Every incident already on disk lacks this key; absent must mean unrecorded.

        A required field here would make the whole index unreadable on upgrade — the
        board would show nothing, which is a far worse failure than missing provenance.
        """
        legacy = {
            "incident_id": "INV-1",
            "signal": {"id": "cloudwatch:alarm/x", "source": "cloudwatch", "title": "t"},
            "status": models.STATUS_DISPATCHED,
        }
        inc = models.Incident.from_dict(legacy)
        self.assertEqual(inc.claimed_by, "")


if __name__ == "__main__":
    unittest.main()
