"""AWS Control backup: the nightly due check and its retry backoff.

Covers ``due_for_nightly`` reading an unparseable run stamp as due, the
``nightly_retry_delay_secs`` schedule and ceiling, and ``record_nightly_failure``:
its per-kind count and ``since`` streak start, the clear a current or recovered
run applies, the run-witness refusal when a run lands mid-attempt, and how the
stored error is redacted, stripped of control characters and truncated.

A corrupt or future-dated failure row reads as due, a corrupt count restarts at
one, and an unwritable state file makes the recorder return ``None`` rather than
raise.

The install-global record of a nightly that skipped because no AWS key works sits
beside ``record_nightly_failure``: one warning per streak, the carried ``since``,
the clear a resolved key applies, a corrupt count restarting the streak, and an
unusable ``since`` being re-stamped without resetting a valid count.
Every class points ``_state_path`` into ``tmp_path``.
"""

from __future__ import annotations

import datetime as dt
import errno
from unittest import mock

import pytest
from aws_control_backup_helpers import (
    ACCOUNT,
)

from kiro_crew.apps.builtins.aws_control.backend import backup

# ---------------------------------------------------------------------------
# due_for_nightly — the malformed-timestamp branch
# ---------------------------------------------------------------------------


class TestDueForNightlyBadStamp:
    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse state pin moved verbatim; no test in this module calls monkeypatch.undo()
    def _isolated_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        yield

    def test_unparseable_last_run_timestamp_reads_as_due(self):
        # A hand-corrupted `at` that ISO parsing rejects must not crash the
        # nightly scheduler; the safe reading is "we cannot prove it ran
        # recently", so treat it as due rather than silently skipping backups.
        backup.set_nightly(ACCOUNT, True)

        def mutate(state):
            entry = backup._account_state(state, ACCOUNT)
            entry.setdefault("runs", {})[backup.KIND_SNAPSHOT] = {
                "key": "snapshots/x.tar.gz",
                "bytes": 1,
                "at": "not-a-timestamp",
            }

        backup._locked_state_update(mutate)
        assert backup.due_for_nightly(ACCOUNT) is True


# ---------------------------------------------------------------------------
# The retry backoff — a failed unattended attempt is recorded, so a
# deterministic fault stops being re-attempted on every wake
# ---------------------------------------------------------------------------


def _fail(account: str, kind: str, error: str = "eio"):
    """Record a failed unattended attempt the way the nightly loop does.

    The witness is read FIRST and passed in, because ``record_nightly_failure`` requires
    it: a test that hand-rolled ``run_witness=None`` would be asserting against a
    protocol the loop does not follow, and the required keyword is what makes that
    impossible to do by accident. Returns the recorder's own answer -- a record, or
    ``None`` when the run slot moved and the write was refused.
    """
    return backup.record_nightly_failure(
        account, kind, error, run_witness=backup.nightly_run_witness(account, kind)
    )


class TestNightlyRetryBackoff:
    """Before this, only COMPLETED runs were recorded.

    So a deterministic fault -- an unreadable file, a disconnected mount, a full
    disk -- left the state file unable to tell "never ran" from "keeps breaking":
    ``due_for_nightly`` took its never-ran branch on every half-hourly wake, each
    attempt re-staged the whole data home into a fresh temporary directory, and the
    same traceback repeated at that cadence for as long as the fault lasted.

    Every test here asserts on the DUE-CHECK's answer rather than on the stored
    row, because the answer is what the loop acts on; the stored row is checked
    only where the point is what was written.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse state pin moved verbatim; no test in this module calls monkeypatch.undo()
    def _isolated_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        yield

    @staticmethod
    def _at(record: dict) -> dt.datetime:
        """The moment a recorded attempt was stamped, read back from the record.

        Read back rather than passed in, so the test measures elapsed time against
        the stamp the writer actually stored instead of against a clock the test
        froze -- a writer that stamped the wrong value would otherwise still pass.
        """
        return dt.datetime.fromisoformat(record["at"])

    def _write_failure_row(self, row: object, kind: str = backup.KIND_SNAPSHOT) -> None:
        """Put an arbitrary row in the failure map, bypassing the writer.

        The corruption cases need shapes the writer cannot produce, so they are
        written the way ``TestDueForNightlyBadStamp`` writes its bad stamp.
        """

        def mutate(state):
            entry = backup._account_state(state, ACCOUNT)
            entry.setdefault(backup.NIGHTLY_FAILURE_STATE_KEY, {})[kind] = row

        backup._locked_state_update(mutate)

    def _raise_stored_sequence(self, kind: str = backup.KIND_SNAPSHOT, by: int = 50) -> None:
        """Raise the stored run's sequence so this process's next run loses the slot.

        ``superseded`` in :func:`_record_run_locked` compares the stored record's
        ``process`` and ``sequence`` against the incoming run's, so raising the stored
        sequence is the whole precondition -- no second writer is needed, and the
        document stays one the writer itself produced apart from that field.
        """

        def mutate(state):
            stored = backup._account_state(state, ACCOUNT)["runs"][kind]
            stored["sequence"] = stored["sequence"] + by

        backup._locked_state_update(mutate)

    # -- the schedule itself ------------------------------------------------

    def test_the_schedule_backs_off_and_then_holds_at_its_ceiling(self):
        # Zero and below are not a backoff. `nightly_retry_delay_secs` is reached
        # with a count read from a state file, so a nonsense count must resolve to
        # "no wait" rather than to the first row by index arithmetic.
        assert backup.nightly_retry_delay_secs(0) == 0
        assert backup.nightly_retry_delay_secs(-3) == 0
        table = list(backup.NIGHTLY_RETRY_BACKOFF_SECS)
        assert [backup.nightly_retry_delay_secs(n) for n in range(1, len(table) + 1)] == table
        # Past the end the ceiling applies, so the table needs no row per failure.
        # TWO values past it, because one could be the last row by coincidence.
        assert backup.nightly_retry_delay_secs(len(table) + 1) == table[-1]
        assert backup.nightly_retry_delay_secs(10_000) == table[-1]

    def test_the_ceiling_stays_under_the_nightly_window(self):
        # The one property that keeps this a backoff rather than a mute: however
        # long a fault persists, the loop still attempts more often than once a
        # window. The module asserts it at import too, but an import-time assert is
        # stripped under `-O` and collapses as a collection error rather than as a
        # named failure, so the enforcement that a reader can act on lives here.
        assert max(backup.NIGHTLY_RETRY_BACKOFF_SECS) < backup.NIGHTLY_WINDOW_SECS

    def test_one_failure_still_retries_on_the_next_wake(self):
        # A single failure is not yet evidence of a pattern, and the issue calls
        # retrying a transient fault correct. So nothing about a one-off blip
        # changes: the first recorded failure earns no wait at all.
        backup.set_nightly(ACCOUNT, True)
        record = _fail(ACCOUNT, backup.KIND_SNAPSHOT, "transient")
        assert record["consecutive"] == 1
        assert backup.nightly_retry_delay_secs(1) == 0
        assert backup.due_for_nightly(ACCOUNT, now=self._at(record)) is True

    # -- the defect this change closes -------------------------------------

    def test_a_repeated_failure_withholds_the_next_wake_and_then_releases_it(self):
        # THE regression. With no run record at all -- the reported case, a nightly
        # that has never once succeeded -- a second failed attempt must stop the
        # next half-hourly wake from attempting again, and must release it once the
        # wait has passed. Before the failure record existed the state file held
        # nothing to read here, so both answers were True and the loop re-attempted
        # every wake indefinitely.
        backup.set_nightly(ACCOUNT, True)
        assert backup.last_runs(ACCOUNT).get(backup.KIND_SNAPSHOT) is None
        _fail(ACCOUNT, backup.KIND_SNAPSHOT, "mount gone")
        second = _fail(ACCOUNT, backup.KIND_SNAPSHOT, "mount gone")
        assert second["consecutive"] == 2
        delay = backup.nightly_retry_delay_secs(2)
        assert delay > 0  # the case would be vacuous at a zero delay
        at = self._at(second)
        # One wake later, inside the wait: withheld.
        assert backup.due_for_nightly(ACCOUNT, now=at + dt.timedelta(seconds=1800)) is False
        # A tick before the wait ends: still withheld.
        assert backup.due_for_nightly(ACCOUNT, now=at + dt.timedelta(seconds=delay - 1)) is False
        # And released the moment it ends -- the loop backs off, it does not stop.
        assert backup.due_for_nightly(ACCOUNT, now=at + dt.timedelta(seconds=delay + 1)) is True

    def test_the_count_survives_across_attempts_rather_than_restarting(self):
        # The backoff grows only if the count does. A writer that re-stamped `at`
        # without carrying the previous count forward would hold every fault at the
        # first row forever, which reads as working and backs off almost nothing.
        backup.set_nightly(ACCOUNT, True)
        counts = [_fail(ACCOUNT, backup.KIND_SNAPSHOT, "eio")["consecutive"] for _ in range(4)]
        assert counts == [1, 2, 3, 4]

    def test_a_completed_run_clears_the_count_and_the_map(self):
        # A success ends the backoff. Without this the count only ever grows, so one
        # bad week would leave a healthy install at the ceiling permanently.
        backup.set_nightly(ACCOUNT, True)
        for _ in range(3):
            _fail(ACCOUNT, backup.KIND_SNAPSHOT, "eio")
        assert backup.nightly_failures(ACCOUNT)[backup.KIND_SNAPSHOT]["consecutive"] == 3
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/i/a.tar.gz", 7, "fp", "v1")
        assert backup.nightly_failures(ACCOUNT) == {}
        # The key itself is gone, not left as an empty map or a stored zero, so
        # "nothing is failing" has exactly one spelling in the document.
        assert backup.NIGHTLY_FAILURE_STATE_KEY not in backup._account_view(ACCOUNT)

    @pytest.mark.parametrize("backoff_set", [True, False], ids=["backoff-set", "no-backoff"])
    @pytest.mark.parametrize("superseded", [True, False], ids=["superseded", "current"])
    def test_only_a_current_run_retires_the_backoff(self, superseded, backoff_set):
        """The four combinations of losing the slot and carrying a standing backoff.

        A success clears the count, and a run whose own record is refused as stale is
        not a success this document can act on: too stale to write a key is too stale
        to retire a count a later failure accumulated. So the clear shares the run
        write's condition, which is what :func:`_merge_pending` says at the other place
        the two travel together.

        Supersession is produced by raising the stored run's ``sequence``, because
        ``superseded`` is the same-process half of ``_run_is_newer`` -- it compares the
        stored record's ``process`` and ``sequence`` against the incoming run's, so a
        raised sequence makes this process's next run the loser.
        ``test_a_superseded_run_keeps_the_nightly_backoff`` is what shows two real runs
        reach that state; this one is what enumerates every combination cheaply.

        MUTATION: move the ``_clear_nightly_failure`` call in ``_record_run_locked`` out
        of the ``if not superseded:`` branch and the superseded/backoff-set case reddens.
        """
        backup._unpersisted_runs.clear()
        try:
            backup.set_nightly(ACCOUNT, True)
            backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/i/first.tar.gz", 7)
            if superseded:
                self._raise_stored_sequence()
            if backoff_set:
                assert _fail(ACCOUNT, backup.KIND_SNAPSHOT, "eio"), "the CAS refused the setup"
                assert backup.nightly_failures(ACCOUNT).get(backup.KIND_SNAPSHOT)
            else:
                assert backup.nightly_failures(ACCOUNT) == {}, "the case starts with no row"

            backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/i/second.tar.gz", 7)

            stored = backup._account_view(ACCOUNT).get("runs", {}).get(backup.KIND_SNAPSHOT)
            assert stored, "no run record survived at all"
            # Whether the second run won the slot is the case's own precondition, so it
            # is asserted rather than assumed: a bump that stopped working would
            # otherwise turn the superseded rows into duplicates of the current ones.
            if superseded:
                assert stored["key"] == "snapshots/i/first.tar.gz", "the run was not superseded"
            else:
                assert stored["key"] == "snapshots/i/second.tar.gz", "the run lost the slot"

            after = backup.nightly_failures(ACCOUNT).get(backup.KIND_SNAPSHOT)
            if superseded and backoff_set:
                assert (
                    after and after["consecutive"] == 1
                ), "a run refused as stale retired a count a later failure accumulated"
            else:
                assert after is None, (
                    "a current run left the backoff standing"
                    if backoff_set
                    else "a row appeared where the case wrote none"
                )
            if not backoff_set:
                # The absence has ONE spelling: the map goes with its last kind, so an
                # empty map left behind would be a second way to say nothing is failing.
                assert backup.NIGHTLY_FAILURE_STATE_KEY not in backup._account_view(ACCOUNT)
        finally:
            backup._unpersisted_runs.clear()

    def test_a_failure_before_the_winning_run_commits_is_retired_by_that_run(self):
        """The ordering half: WHEN the failure is written decides who meets it.

        A failure recorded before the winning run commits is retired by that run's own
        mutate, so it is gone whichever condition the clear carries -- the two
        placements agree here, and this is the ordering that a reader assumes is the
        only one. The divergent ordering is a failure recorded AFTER the winner commits,
        which ``test_a_superseded_run_keeps_the_nightly_backoff`` builds with two real
        writers: there the only run left to meet the row is one whose record was
        refused.

        Kept separate from the matrix above because the matrix varies WHO the run is
        while holding the order fixed; this varies the order.
        """
        backup.set_nightly(ACCOUNT, True)
        for _ in range(3):
            _fail(ACCOUNT, backup.KIND_SNAPSHOT, "eio")
        assert backup.nightly_failures(ACCOUNT)[backup.KIND_SNAPSHOT]["consecutive"] == 3
        # The winner commits after the row exists, and clears it in its own mutate.
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/i/winner.tar.gz", 7)
        assert (
            backup.nightly_failures(ACCOUNT) == {}
        ), "the run that WON the slot did not retire a count written before it"

    def test_an_unchanged_skip_also_clears_the_count(self):
        # `uploaded=False` is a successful comparison against an archive that is
        # provably in the drive, not a failure. Treating it as one would keep a
        # perfectly healthy install backing off for as long as its tree stayed
        # still -- the exact stretch during which nothing is wrong.
        backup.set_nightly(ACCOUNT, True)
        _fail(ACCOUNT, backup.KIND_SNAPSHOT, "eio")
        backup._record_run(
            ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/i/a.tar.gz", 7, "fp", "v1", uploaded=False
        )
        assert backup.nightly_failures(ACCOUNT) == {}

    def test_the_sessions_due_check_backs_off_on_its_own_record(self):
        # The sessions kind reads the backoff through its own call, so it needs its
        # own case: a test that only drove `due_for_nightly` would leave the second
        # consultation free to be deleted with nothing turning red. The blocked
        # reason is stubbed to None so this measures the backoff and not the host's
        # traversal capability, which has its own tests.
        backup.set_nightly_sessions(ACCOUNT, True)
        with mock.patch.object(backup, "scheduled_sessions_blocked_reason", return_value=None):
            assert backup.due_for_sessions_nightly(ACCOUNT) is True
            for _ in range(2):
                record = _fail(ACCOUNT, backup.KIND_SESSIONS, "no openat")
            at = self._at(record)
            delay = backup.nightly_retry_delay_secs(2)
            assert backup.due_for_sessions_nightly(ACCOUNT, now=at) is False
            later = at + dt.timedelta(seconds=delay + 1)
            assert backup.due_for_sessions_nightly(ACCOUNT, now=later) is True

    def test_each_kind_backs_off_on_its_own_count(self):
        # The record is per kind, so a transcript archive failing deterministically
        # must not withhold a snapshot that is still working. A shared counter would
        # let the payload most likely to be refused on a given host silence the one
        # the operator actually relies on.
        backup.set_nightly(ACCOUNT, True)
        for _ in range(3):
            _fail(ACCOUNT, backup.KIND_SESSIONS, "no openat")
        recorded = backup.nightly_failures(ACCOUNT)
        assert set(recorded) == {backup.KIND_SESSIONS}
        assert backup.due_for_nightly(ACCOUNT, now=self._at(recorded[backup.KIND_SESSIONS])) is True

    # -- fail OPEN: nothing unusable may keep the nightly quiet --------------

    @pytest.mark.parametrize(
        "row,why",
        [
            ({"consecutive": "3", "at": None}, "a count stored as a string"),
            ({"consecutive": True, "at": None}, "a bool, which is an int subclass"),
            ({"consecutive": 3.0, "at": None}, "a count stored as a float"),
            ({"at": None}, "a row with no count at all"),
            ({"consecutive": 3}, "a row with no stamp at all"),
            ({"consecutive": 3, "at": "not-a-timestamp"}, "a stamp ISO parsing rejects"),
            ({"consecutive": 3, "at": ["2026-01-01"]}, "a stamp stored as a list"),
            ({"consecutive": 3, "at": 1_700_000_000}, "a stamp stored as a number"),
        ],
    )
    def test_a_corrupt_failure_row_reads_as_due(self, row, why):
        # `_a_day_since_last_run` already states that an unparseable stamp must not
        # be the reason a backup the owner enabled silently stops running. A failure
        # record is a NEW place for exactly that silence to appear, so every
        # unusable reading here answers DUE. `at: None` in the cases above stands
        # for "stamped now", filled in below, so a case meant to fail on its count
        # cannot pass by accident on a missing stamp.
        backup.set_nightly(ACCOUNT, True)
        if "at" in row and row["at"] is None:
            row = dict(row)
            row["at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")
        self._write_failure_row(row)
        assert backup.due_for_nightly(ACCOUNT) is True, why

    def test_a_non_dict_failure_map_reads_as_due(self):
        # The level above the row, which `_account_view` does not flatten for us.
        backup.set_nightly(ACCOUNT, True)

        def mutate(state):
            backup._account_state(state, ACCOUNT)[backup.NIGHTLY_FAILURE_STATE_KEY] = "corrupt"

        backup._locked_state_update(mutate)
        assert backup.due_for_nightly(ACCOUNT) is True
        # And the projection survives it rather than raising on a polled endpoint.
        assert backup.nightly_failures(ACCOUNT) == {}

    def test_a_non_dict_failure_row_reads_as_due(self):
        backup.set_nightly(ACCOUNT, True)
        self._write_failure_row(["not", "a", "row"])
        assert backup.due_for_nightly(ACCOUNT) is True
        assert backup.nightly_failures(ACCOUNT) == {}

    def test_a_stamp_in_the_future_reads_as_due(self):
        # A backwards clock step, or a state file carried from a host that was
        # ahead. Withholding on that arithmetic would keep the nightly quiet for as
        # long as the skew lasted, with nothing in the document an operator could
        # read as the cause.
        backup.set_nightly(ACCOUNT, True)
        ahead = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30)
        self._write_failure_row({"consecutive": 5, "at": ahead.isoformat(timespec="microseconds")})
        assert backup.due_for_nightly(ACCOUNT) is True

    def test_a_timezone_less_stamp_is_read_as_utc_rather_than_raising(self):
        # A naive stamp PARSES, so it escapes the type and ValueError guards and
        # would raise TypeError on the aware subtraction -- inside the nightly loop,
        # on every wake. The same normalization `_a_day_since_last_run` applies.
        backup.set_nightly(ACCOUNT, True)
        naive = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        self._write_failure_row({"consecutive": 3, "at": naive.isoformat(timespec="microseconds")})
        assert backup.due_for_nightly(ACCOUNT) is False  # normalized, and withholding
        later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
            seconds=backup.nightly_retry_delay_secs(3) + 1
        )
        assert backup.due_for_nightly(ACCOUNT, now=later) is True

    def test_a_corrupt_stored_count_restarts_at_one_rather_than_extending(self):
        # Corruption may only ever SHORTEN a backoff. Reading an unusable stored
        # count as a long history would let a damaged document hold the nightly at
        # the ceiling, which is the silence this whole design avoids.
        backup.set_nightly(ACCOUNT, True)
        self._write_failure_row({"consecutive": True, "at": "not-a-timestamp"})
        assert _fail(ACCOUNT, backup.KIND_SNAPSHOT, "eio")["consecutive"] == 1

    # -- the grant and the window still decide first -------------------------

    def test_the_grant_still_answers_first(self):
        # A recorded failure must not make a nightly-disabled account look due.
        # The backoff narrows an already-due answer; it never widens one.
        _fail(ACCOUNT, backup.KIND_SNAPSHOT, "eio")
        assert backup.due_for_nightly(ACCOUNT) is False
        backup.set_nightly(ACCOUNT, True)
        assert backup.due_for_nightly(ACCOUNT) is True

    def test_a_recent_success_still_answers_before_the_backoff(self):
        # A run inside the window is not due whatever the failure map says, so a
        # stale count left by an earlier fault cannot be read as a reason to run.
        backup.set_nightly(ACCOUNT, True)
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/i/a.tar.gz", 7, "fp", "v1")
        self._write_failure_row(
            {
                "consecutive": 4,
                "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds"),
            }
        )
        assert backup.due_for_nightly(ACCOUNT) is False

    # -- a run that lands during the attempt supersedes the failure ----------

    def test_a_run_recorded_during_the_attempt_refuses_the_failure_write(self):
        # Both writers serialize under the sidecar lock, but each mutate re-reads fresh
        # state, so an unconditional write here lands AFTER a concurrent manual success
        # cleared the count and records a failure against a kind that just succeeded.
        # Measured cost: not a withheld attempt (the raced write restarts at 1, which
        # earns zero delay) but a false `nightly_failures` row for an account that just
        # backed up. The witness is read BEFORE the attempt, so the run that lands
        # inside it is detectable.
        backup.set_nightly(ACCOUNT, True)
        witness = backup.nightly_run_witness(ACCOUNT, backup.KIND_SNAPSHOT)
        # ... the owner's manual run succeeds while the nightly attempt is still failing.
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/i/a.tar.gz", 7, "fp", "v1")
        refused = backup.record_nightly_failure(
            ACCOUNT, backup.KIND_SNAPSHOT, "mount gone", run_witness=witness
        )
        assert refused is None
        assert backup.nightly_failures(ACCOUNT) == {}
        # And the key is absent rather than present-and-empty, so the skipped write left
        # the document exactly as the success did.
        assert backup.NIGHTLY_FAILURE_STATE_KEY not in backup._account_view(ACCOUNT)

    def test_a_second_run_during_the_attempt_also_refuses(self):
        # The witness is an IDENTITY, not a presence check: the slot already held a
        # record when this attempt began, and a DIFFERENT record now. A guard that only
        # asked "is the slot non-empty" would accept this and write the false failure.
        backup.set_nightly(ACCOUNT, True)
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/i/a.tar.gz", 7, "fp", "v1")
        witness = backup.nightly_run_witness(ACCOUNT, backup.KIND_SNAPSHOT)
        assert witness is not None  # the case would be vacuous against an empty slot
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/i/b.tar.gz", 8, "fp2", "v2")
        assert (
            backup.record_nightly_failure(ACCOUNT, backup.KIND_SNAPSHOT, "eio", run_witness=witness)
            is None
        )
        assert backup.nightly_failures(ACCOUNT) == {}

    def test_an_unmoved_slot_still_records_the_failure(self):
        # The other direction, and the one that matters most: with no run record at all
        # -- the reported case, a nightly that has never once succeeded -- absent
        # compares equal to absent and the count is written normally. A guard that
        # refused here would make the whole fix inert on exactly the case it is for.
        backup.set_nightly(ACCOUNT, True)
        witness = backup.nightly_run_witness(ACCOUNT, backup.KIND_SNAPSHOT)
        assert witness is None
        record = backup.record_nightly_failure(
            ACCOUNT, backup.KIND_SNAPSHOT, "mount gone", run_witness=witness
        )
        assert record is not None and record["consecutive"] == 1

    def test_an_unmoved_non_empty_slot_still_records_the_failure(self):
        # Same direction with a run record present: a nightly whose last success is old
        # and which is now failing must still accumulate a count, or the backoff never
        # engages for the account that has been working and then broke.
        backup.set_nightly(ACCOUNT, True)
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/i/a.tar.gz", 7, "fp", "v1")
        witness = backup.nightly_run_witness(ACCOUNT, backup.KIND_SNAPSHOT)
        record = backup.record_nightly_failure(
            ACCOUNT, backup.KIND_SNAPSHOT, "eio", run_witness=witness
        )
        assert record is not None and record["consecutive"] == 1

    def test_a_run_on_the_other_kind_does_not_refuse_this_kind(self):
        # The witness is per kind. A snapshot success must not suppress a transcripts
        # failure: they are separate payloads with separate faults, and conflating them
        # would let the kind that works hide the kind that does not.
        backup.set_nightly_sessions(ACCOUNT, True)
        witness = backup.nightly_run_witness(ACCOUNT, backup.KIND_SESSIONS)
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/i/a.tar.gz", 7, "fp", "v1")
        record = backup.record_nightly_failure(
            ACCOUNT, backup.KIND_SESSIONS, "no openat", run_witness=witness
        )
        assert record is not None and record["consecutive"] == 1

    def test_the_witness_reads_none_from_a_record_with_no_usable_identity(self):
        # A legacy record predating process/sequence, and a bool masquerading as one.
        # Both read as None, so an attempt spanning such a slot compares None-to-None and
        # records normally rather than being refused by an identity nobody can form.
        def mutate(state):
            entry = backup._account_state(state, ACCOUNT)
            entry.setdefault("runs", {})[backup.KIND_SNAPSHOT] = {
                "key": "snapshots/legacy.tar.gz",
                "bytes": 1,
                "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds"),
            }

        backup._locked_state_update(mutate)
        assert backup.nightly_run_witness(ACCOUNT, backup.KIND_SNAPSHOT) is None

        def mutate_bool(state):
            entry = backup._account_state(state, ACCOUNT)
            entry["runs"][backup.KIND_SNAPSHOT]["process"] = "p:1"
            entry["runs"][backup.KIND_SNAPSHOT]["sequence"] = True

        backup._locked_state_update(mutate_bool)
        assert backup.nightly_run_witness(ACCOUNT, backup.KIND_SNAPSHOT) is None

    # -- a run recovered from memory clears the count too ---------------------

    def test_a_recovered_run_clears_the_stale_count(self):
        # A run record reaches the document by TWO paths and the clear has to sit on
        # both. `_record_run_locked` clears beside its own write, but a run whose state
        # write raised is held in memory and arrives through `_merge_pending` instead --
        # carrying the run and, before the fix, not the clear. The stale count then
        # outlived the success that should have ended it, and after a restart withheld one
        # nightly for up to the ceiling on an account that had already backed up.
        backup.set_nightly(ACCOUNT, True)
        for _ in range(4):
            _fail(ACCOUNT, backup.KIND_SNAPSHOT, "eio")
        assert backup.nightly_failures(ACCOUNT)[backup.KIND_SNAPSHOT]["consecutive"] == 4
        # Stamp the streak strictly before the held success, so the mask does not rest on
        # two writes landing on different ticks of a coarse clock (see _stamp_failure_row).
        earlier = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat(
            timespec="microseconds"
        )
        self._stamp_failure_row(at=earlier)

        # The success whose state write fails: the run is held, nothing is persisted.
        with mock.patch.object(
            backup, "_locked_state_update", side_effect=OSError(errno.ENOSPC, "no space")
        ):
            backup._record_run(
                ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/i/held.tar.gz", 9, "fp9", "v9"
            )
        held = backup.last_runs(ACCOUNT).get(backup.KIND_SNAPSHOT)
        assert held and held["key"] == "snapshots/i/held.tar.gz"  # the overlay holds it
        # Still on disk, because the write never landed -- the precondition of the case.
        # The public projection masks it beside the newer held success; inspect storage
        # directly here because this assertion is specifically about persistence.
        persisted_failures = backup._account_view(ACCOUNT)[backup.NIGHTLY_FAILURE_STATE_KEY]
        assert persisted_failures[backup.KIND_SNAPSHOT]["consecutive"] == 4
        assert backup.nightly_failures(ACCOUNT) == {}

        # Any later successful state update drains the overlay through `_merge_pending`.
        backup.set_nightly(ACCOUNT, True)
        persisted = backup._account_view(ACCOUNT).get("runs", {}).get(backup.KIND_SNAPSHOT)
        assert persisted and persisted["key"] == "snapshots/i/held.tar.gz"  # run recovered
        assert backup.nightly_failures(ACCOUNT) == {}  # ...and the count went with it

    def _hold_a_success(self) -> dict:
        """A completed upload whose state write fails: held in memory, not on disk."""
        with mock.patch.object(
            backup, "_locked_state_update", side_effect=OSError(errno.ENOSPC, "no space")
        ):
            backup._record_run(
                ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/i/held.tar.gz", 9, "fp9", "v9"
            )
        held = backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT]
        assert held["key"] == "snapshots/i/held.tar.gz"
        return held

    @staticmethod
    def _stamp_failure_row(**fields) -> None:
        """Rewrite the persisted failure row the way ANOTHER process's write lands.

        ``read_state`` + ``write_state`` without ``_locked_state_update``, so this
        process's held run is not merged in: a second process has no overlay to merge.
        """
        state = backup.read_state()
        row = state["accounts"][ACCOUNT][backup.NIGHTLY_FAILURE_STATE_KEY][backup.KIND_SNAPSHOT]
        row.update(fields)
        backup.write_state(state)

    def test_a_held_success_releases_the_backoff_of_the_failure_it_superseded(self):
        # The retry schedule must read the latest outcome `last_runs` and the status
        # read serve, not the raw row a held success has already superseded.
        backup.set_nightly(ACCOUNT, True)
        for _ in range(6):
            _fail(ACCOUNT, backup.KIND_SNAPSHOT, "eio")
        held = self._hold_a_success()
        # Stamped strictly before the success, so the order does not rest on two
        # stamps landing on different ticks of a coarse clock.
        failed_at = self._at(held) - dt.timedelta(hours=1)
        self._stamp_failure_row(at=failed_at.isoformat(timespec="microseconds"))

        on_disk = backup._account_view(ACCOUNT)[backup.NIGHTLY_FAILURE_STATE_KEY]
        assert on_disk[backup.KIND_SNAPSHOT]["consecutive"] == 6  # still persisted
        assert backup.nightly_failures(ACCOUNT) == {}
        # Six failures back off for the 12 h ceiling, and one hour has passed.
        assert not backup._backoff_withholds(
            ACCOUNT, backup.KIND_SNAPSHOT, failed_at + dt.timedelta(hours=1)
        )
        # And the schedule is due again a day after the held success.
        assert backup.due_for_nightly(ACCOUNT, self._at(held) + dt.timedelta(hours=23, minutes=1))

    def test_a_failure_newer_than_the_held_success_stays_and_backs_off(self):
        # The held success is newer than the PERSISTED RUN, but a later attempt by
        # another process failed after it. That failure is the latest outcome.
        backup.set_nightly(ACCOUNT, True)
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/i/old.tar.gz", 1)
        persisted_run = backup._account_view(ACCOUNT)["runs"][backup.KIND_SNAPSHOT]
        _fail(ACCOUNT, backup.KIND_SNAPSHOT, "eio")
        held = self._hold_a_success()
        assert backup._run_is_newer(held, persisted_run)

        failed_at = self._at(held) + dt.timedelta(hours=22)
        self._stamp_failure_row(at=failed_at.isoformat(timespec="microseconds"), consecutive=6)
        assert not backup._run_is_newer(
            held,
            backup._account_view(ACCOUNT)[backup.NIGHTLY_FAILURE_STATE_KEY][backup.KIND_SNAPSHOT],
        )

        assert backup.nightly_failures(ACCOUNT)[backup.KIND_SNAPSHOT]["consecutive"] == 6
        # A day after the held success, one hour into the later failure's 12 h backoff.
        now = self._at(held) + dt.timedelta(hours=23, minutes=1)
        assert backup._backoff_withholds(ACCOUNT, backup.KIND_SNAPSHOT, now)
        assert backup.due_for_nightly(ACCOUNT, now) is False

    # -- the row says WHEN the streak started, not only the last attempt ------

    def test_the_row_carries_the_streaks_start_as_well_as_the_last_attempt(self):
        # The issue asks for this by name: an operator has to see that the nightly "has
        # been failing since a particular day". `at` is the backoff's clock and must be
        # the latest attempt, so one overwritten stamp cannot answer both questions.
        backup.set_nightly(ACCOUNT, True)
        first = _fail(ACCOUNT, backup.KIND_SNAPSHOT, "eio")
        assert first["since"] == first["at"]  # a streak of one starts where it is
        later = _fail(ACCOUNT, backup.KIND_SNAPSHOT, "eio")
        assert later["consecutive"] == 2
        assert later["since"] == first["since"], "the streak's start must be carried"
        row = backup.nightly_failures(ACCOUNT)[backup.KIND_SNAPSHOT]
        assert row["since"] == first["since"] and row["at"] == later["at"]
        # That `at` re-stamps while `since` does not is pinned against a SEEDED old value,
        # not against a stamp taken microseconds earlier. Windows' clock ticks about every
        # 15 ms, so two successive `now()` calls return the SAME string and a strict
        # `later["at"] > later["since"]` is false there. The product does not promise
        # strict advance and does not need it: the
        # backoff measures `now - at`, which is correct when two attempts share an instant.
        old = "2020-01-01T00:00:00.000000+00:00"
        self._write_failure_row({"consecutive": 2, "at": old, "since": old})
        again = _fail(ACCOUNT, backup.KIND_SNAPSHOT, "eio")
        assert again["since"] == old, "the carried start must survive the attempt untouched"
        assert again["at"] > old, "the latest attempt must be re-stamped"
        assert again["consecutive"] == 3

    def test_a_cleared_streak_starts_its_since_again(self):
        # `since` describes the run it sits in, so a success ending one streak must not
        # leave the next streak claiming to have started before that success.
        #
        # The old start is SEEDED rather than read back from a stamp taken microseconds
        # earlier. On Windows the clock ticks about every 15 ms, so both stamps come out
        # equal and inheriting the old start is then indistinguishable from restarting --
        # the assertion cannot see the bug it exists to catch. Seeding makes it observable
        # on any clock granularity, which is what makes the assertion mean anything.
        backup.set_nightly(ACCOUNT, True)
        old = "2020-01-01T00:00:00.000000+00:00"
        self._write_failure_row({"consecutive": 4, "at": old, "since": old})
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/i/a.tar.gz", 7, "fp", "v1")
        assert backup.nightly_failures(ACCOUNT) == {}
        fresh = _fail(ACCOUNT, backup.KIND_SNAPSHOT, "eio")
        assert fresh["consecutive"] == 1
        assert fresh["since"] == fresh["at"]
        assert fresh["since"] != old, "the new streak must not inherit the old start"
        assert fresh["since"] > old

    def test_a_negative_stored_count_restarts_at_one(self):
        # The docstring claims anything unusable restarts the count at 1, and a negative
        # count is unusable: the writer never produces one (it starts at 1 and only
        # increments) and a clear REMOVES the key rather than zeroing it, so this shape
        # only arrives by corruption. Without the positive-count term the increment would
        # carry it forward and store a nonsense `consecutive: -2` in an operator-facing
        # row.
        backup.set_nightly(ACCOUNT, True)
        self._write_failure_row(
            {
                "consecutive": -3,
                "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds"),
            }
        )
        record = _fail(ACCOUNT, backup.KIND_SNAPSHOT, "eio")
        assert record["consecutive"] == 1
        assert record["since"] == record["at"]

    def test_a_credential_in_the_error_is_redacted_before_it_is_stored(self):
        # The stored error EGRESSES as `nightlyFailures`, and its text is not ours: on this
        # path `snapshot.RedactionFailed` embeds file names out of the bundle, and
        # `snapshot._safe_name` only makes them printable. `sanitize_label`, one screen up
        # in the same module, runs these same two redactors on a foreign-authored name for
        # this exact reason.
        backup.set_nightly(ACCOUNT, True)
        record = _fail(
            ACCOUNT,
            backup.KIND_SNAPSHOT,
            "1 file(s) are not text: AKIAIOSFODNN7EXAMPLE. They were NOT removed",
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in record["error"]
        assert "AKIA" not in record["error"]
        assert "REDACTED" in record["error"]
        # And it is the STORED row that is clean, not just the returned dict.
        row = backup.nightly_failures(ACCOUNT)[backup.KIND_SNAPSHOT]
        assert "AKIA" not in row["error"]

    def test_the_error_is_redacted_before_it_is_truncated(self):
        # Order, not just presence. Truncating first can cut a credential mid-token: the
        # fragment left behind does not match the redactor, so a partial secret persists
        # in a row that is served to a dashboard. Positioned so the 200-char bound falls
        # INSIDE the key, which is the only arrangement that can tell the two orders apart.
        backup.set_nightly(ACCOUNT, True)
        key = "AKIAIOSFODNN7EXAMPLE"
        prefix = "x" * (200 - len(key) + 10)  # bound lands 10 chars into the key
        record = _fail(ACCOUNT, backup.KIND_SNAPSHOT, prefix + key)
        assert "AKIA" not in record["error"], "a truncated credential fragment survived"
        assert len(record["error"]) <= 200, "the length bound must still hold"

    def test_control_characters_are_stripped_from_the_error(self):
        # They survive both redactors untouched and this string lands in a dashboard row,
        # where an escape sequence can overwrite the line above it. `sanitize_label` strips
        # them FIRST for the same reason and states it.
        backup.set_nightly(ACCOUNT, True)
        record = _fail(ACCOUNT, backup.KIND_SNAPSHOT, "disk full\x1b[2Kfake row\r\n")
        assert "\x1b" not in record["error"]
        assert "\r" not in record["error"] and "\n" not in record["error"]
        assert "disk full" in record["error"]

    def test_an_error_that_is_all_control_characters_stores_empty(self):
        # Fails toward the empty string rather than inventing a message, matching
        # `sanitize_label`'s fallback. The ROW still exists -- the count is what the
        # backoff reads, and it must not depend on the message being renderable.
        backup.set_nightly(ACCOUNT, True)
        record = _fail(ACCOUNT, backup.KIND_SNAPSHOT, "\x1b\r\n")
        assert record["error"] == ""
        assert record["consecutive"] == 1

    def test_a_non_string_error_stores_empty_rather_than_raising(self):
        # `record_nightly_failure` documents that it NEVER raises: it runs on a path that is
        # already handling a failed backup, so raising here would replace a logged failure
        # with an unhandled one. A non-str would reach `"".join(... for ch in error)` and
        # blow up on, say, an int, so the guard upholds that stated contract.
        # `sanitize_label` carries the identical guard one screen up. Exposed by a surviving
        # mutation -- nothing pinned it.
        backup.set_nightly(ACCOUNT, True)
        record = _fail(ACCOUNT, backup.KIND_SNAPSHOT, 17)  # type: ignore[arg-type]
        assert record["error"] == ""
        assert record["consecutive"] == 1

    def test_an_intact_since_beside_a_corrupt_count_does_not_carry(self):
        # The case that makes the restart term load-bearing, and the one a surviving
        # mutation exposed: a row whose `consecutive` is unusable but whose `since` is a
        # perfectly good old stamp. The count restarts at 1 there, so the streak restarts
        # too -- carrying the old stamp would publish "1 consecutive failure, failing
        # since three days ago", which over-reports the outage. Corruption may only ever
        # under-report it. A clear REMOVES the row, so this asymmetry is invisible to any
        # test that reaches a restart by way of a success.
        backup.set_nightly(ACCOUNT, True)
        stale_since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).isoformat(
            timespec="microseconds"
        )
        self._write_failure_row(
            {
                "consecutive": "3",  # a string, so it is not a usable count
                "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds"),
                "since": stale_since,
            }
        )
        record = _fail(ACCOUNT, backup.KIND_SNAPSHOT, "eio")
        assert record["consecutive"] == 1
        assert record["since"] != stale_since, "a restarted streak must not inherit an old start"
        assert record["since"] == record["at"]

    def test_a_corrupt_since_restarts_the_streak_rather_than_extending_it(self):
        # Corruption may only ever UNDER-report how long the nightly has been failing,
        # never over-report it. And `since` is not on the backoff's path at all, so a
        # corrupt value here must leave scheduling untouched.
        backup.set_nightly(ACCOUNT, True)
        self._write_failure_row(
            {
                "consecutive": 2,
                "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds"),
                "since": ["not", "a", "stamp"],
            }
        )
        record = _fail(ACCOUNT, backup.KIND_SNAPSHOT, "eio")
        assert record["consecutive"] == 3  # the count still carries
        assert record["since"] == record["at"]  # but the streak start restarts here
        # The backoff is decided by `at` and the count, so it is unaffected.
        assert backup.due_for_nightly(ACCOUNT, now=self._at(record)) is False

    def test_a_since_that_is_a_string_but_not_a_stamp_does_not_carry(self):
        # The gap an `isinstance(carried, str) and carried` test cannot reach: the case
        # above stores a LIST, which fails the type check, but a non-empty string that is
        # not a timestamp passes it. This value is published in an operator-facing row,
        # so carrying it would render the day the failures began as whatever the file
        # happened to hold -- for the whole life of the streak, since each write carries
        # the previous one forward. Parsing is what makes "corruption may only
        # under-report" true rather than merely claimed.
        backup.set_nightly(ACCOUNT, True)
        self._write_failure_row(
            {
                "consecutive": 2,
                "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds"),
                "since": "banana",
            }
        )
        record = _fail(ACCOUNT, backup.KIND_SNAPSHOT, "eio")
        assert record["consecutive"] == 3  # the count is usable, so it still carries
        assert record["since"] != "banana", "an unparseable stamp must not reach the row"
        assert record["since"] == record["at"]
        # Whatever it published has to be readable as a stamp by the one reader that
        # parses stamps, or the row is honest about nothing.
        assert dt.datetime.fromisoformat(record["since"]) is not None
        # And `since` is still off the backoff's path.
        assert backup.due_for_nightly(ACCOUNT, now=self._at(record)) is False

    # -- the writer must not turn a failed backup into a crash ---------------

    def test_an_unwritable_state_file_is_logged_rather_than_raised(self):
        # This runs on a path already handling a failed backup. Letting an
        # unwritable state file raise would replace a logged failure with an
        # unhandled one and cost the caller its audit record, so the count is
        # dropped and the loop retries as it did before -- the safe direction.
        backup.set_nightly(ACCOUNT, True)
        with mock.patch.object(
            backup, "_locked_state_update", side_effect=OSError(errno.EROFS, "read-only")
        ):
            record = _fail(ACCOUNT, backup.KIND_SNAPSHOT, "eio")
        # None, because nothing was written. Returning the record it MEANT to write
        # would report a count the next reader cannot find.
        assert record is None
        assert backup.nightly_failures(ACCOUNT) == {}  # nothing persisted
        assert backup.due_for_nightly(ACCOUNT) is True  # so the loop still attempts

    def test_the_stored_error_is_truncated(self):
        # One pathological message must not grow the state document on every wake
        # for as long as the fault lasts.
        backup.set_nightly(ACCOUNT, True)
        record = _fail(ACCOUNT, backup.KIND_SNAPSHOT, "e" * 5000)
        assert len(record["error"]) == 200
        assert len(backup.nightly_failures(ACCOUNT)[backup.KIND_SNAPSHOT]["error"]) == 200


class TestNightlyNoWorkingKeySkip:
    """The install-global record for "no working AWS key, every wake skipped".

    A desktop gateway that cannot find the per-user ``aws`` / helpers has no
    healthy key, so ``hooks._run_once`` skips before any account resolves. The
    skip is install-global (no account to key on), warns once per streak, carries
    a usable ``since``, re-stamps an unusable ``since`` without resetting a valid
    count, and is cleared when a key resolves, so the accounts read reports
    a nightly the owner enabled as skipped rather than appearing to run.
    """

    @pytest.fixture(autouse=True)
    def _isolated_state(self, tmp_path, _floor_monkeypatch):
        _floor_monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        yield

    def test_enabled_any_account_reads_every_accounts_grant(self):
        # Nothing configured -> nothing scheduled, so no skip should ever be recorded.
        assert backup.nightly_enabled_any_account() is False
        # Snapshot grant on one account is enough.
        backup.set_nightly(ACCOUNT, True)
        assert backup.nightly_enabled_any_account() is True
        backup.set_nightly(ACCOUNT, False)
        assert backup.nightly_enabled_any_account() is False
        # The sessions grant counts too, on any account.
        backup.set_nightly_sessions("999988887777", True)
        assert backup.nightly_enabled_any_account() is True

    def test_warns_once_per_streak_and_carries_since(self):
        # First wake of the streak: the caller is told to warn, and a record lands.
        assert backup.record_nightly_no_key_skip() is True
        first = backup.nightly_no_key_skip()
        assert first is not None
        assert first["consecutive"] == 1
        assert first["reason"] == "no_working_key"
        assert first["since"] == first["at"]

        # Every later wake of the SAME streak: no warning, streak grows, `since`
        # stays the day the streak began while `at` advances.
        assert backup.record_nightly_no_key_skip() is False
        second = backup.nightly_no_key_skip()
        assert second["consecutive"] == 2
        assert second["since"] == first["since"]  # carried, not re-stamped
        assert second["at"] >= first["at"]

        assert backup.record_nightly_no_key_skip() is False
        assert backup.nightly_no_key_skip()["consecutive"] == 3

    def test_a_resolved_key_clears_the_record(self):
        backup.record_nightly_no_key_skip()
        assert backup.nightly_no_key_skip() is not None
        backup.clear_nightly_no_key_skip()
        assert backup.nightly_no_key_skip() is None
        # And the next skip starts a fresh streak (warns again, consecutive back to 1).
        assert backup.record_nightly_no_key_skip() is True
        assert backup.nightly_no_key_skip()["consecutive"] == 1

    def test_clear_is_a_no_op_when_there_is_no_record(self):
        # Never raises, and leaves the reader answering None.
        backup.clear_nightly_no_key_skip()
        assert backup.nightly_no_key_skip() is None

    def test_clear_with_no_record_leaves_the_state_file_alone(self, tmp_path):
        # The healthy path clears on every wake, so the no-record case must not
        # write: no file appears, and an existing one keeps its inode and mtime
        # (the state writer replaces the file atomically, which changes both).
        state_file = tmp_path / "backup.json"
        backup.clear_nightly_no_key_skip()
        assert not state_file.exists()

        backup.set_nightly(ACCOUNT, True)
        before = state_file.stat()
        backup.clear_nightly_no_key_skip()
        after = state_file.stat()
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)

    def test_a_corrupt_consecutive_restarts_the_streak_rather_than_reading_as_long(self):
        # A corrupted document carrying a bool/str must not read as a previous wake;
        # the streak restarts at 1 so corruption can only UNDER-report the skip.
        def mutate(state):
            state[backup.NIGHTLY_NO_KEY_STATE_KEY] = {
                "consecutive": True,  # bool is an int subclass; must be rejected
                "since": "not-a-timestamp",
                "at": "also-bad",
                "reason": "no_working_key",
            }

        backup._locked_state_update(mutate)
        # Warns (streak treated as new) and rewrites a clean record.
        assert backup.record_nightly_no_key_skip() is True
        row = backup.nightly_no_key_skip()
        assert row["consecutive"] == 1
        assert row["since"] == row["at"]  # the bad `since` was not carried

    def test_an_unusable_since_restamps_without_resetting_a_valid_count(self):
        # The count identifies the standing streak independently of its timestamp.
        def mutate(state):
            state[backup.NIGHTLY_NO_KEY_STATE_KEY] = {
                "consecutive": 7,
                "since": "not-a-timestamp",
                "at": "also-bad",
                "reason": "no_working_key",
            }

        backup._locked_state_update(mutate)
        before = dt.datetime.now(dt.timezone.utc)
        assert backup.record_nightly_no_key_skip() is False
        after = dt.datetime.now(dt.timezone.utc)

        row = backup.nightly_no_key_skip()
        assert row["consecutive"] == 8
        assert row["since"] == row["at"]
        assert before <= dt.datetime.fromisoformat(row["since"]) <= after

    def test_reader_treats_a_non_dict_record_as_absent(self):
        def mutate(state):
            state[backup.NIGHTLY_NO_KEY_STATE_KEY] = "corrupt"

        backup._locked_state_update(mutate)
        assert backup.nightly_no_key_skip() is None
