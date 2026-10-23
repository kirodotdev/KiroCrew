"""Tests for the ``aws_control`` backup state file and its held-run overlay.

Covers a state update that refuses to rewrite a file it cannot read, while a
missing file still takes a first write and a corrupt one is repaired. Covers the
in-memory hold for a completed run whose state read or write fails: it feeds
``last_runs``, ``uploaded_keys`` and ``due_for_nightly``, carries the
conversations-retained fact the retention gate re-checks, serves the later of two
same-second runs, and is taken inside the state lock.

Also pins the digest prefix and byte budget of ``_staging_name``, and the
``costs`` cache branches: ``read_cached`` on an absent or corrupt file and
``is_fresh`` on missing, non-string or naive ``fetchedAt`` stamps.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import errno
import hashlib
import json
import logging
import time
from pathlib import Path
from unittest import mock

import pytest
from aws_control_backup_helpers import (
    ACCOUNT,
)

from kiro_crew.apps.builtins.aws_control.backend import backup, costs
from kiro_crew.testing.wait import default_timeout

# ---------------------------------------------------------------------------
# costs — cache read and freshness branches
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The state file is a WHOLE document: a read that failed must not be published
# ---------------------------------------------------------------------------


OTHER_ACCOUNT = "444455556666"


class TestUnreadableStateIsNotOverwritten:
    """``_locked_state_update`` rewrites the ENTIRE state document.

    ``read_state`` is a display read and collapses every failure to ``{}``. Used
    as the base of a read-modify-write, that empty dict is not "no fields to
    carry forward" -- it is an instruction to replace every account's nightly
    toggle and run history with whatever this one mutation writes. A missing
    file is the only failure where ``{}`` is true. The sidecar lock does not
    help: it serializes writers, and this loss happens inside the lock.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse state pin moved verbatim; no test in this module calls monkeypatch.undo()
    def _isolated_state(self, tmp_path, monkeypatch):
        self.state_file = tmp_path / "backup.json"
        # Captured BEFORE the failure is injected, so assertions can read the
        # file the code under test could not.
        self.real_read_text = Path.read_text
        monkeypatch.setattr(backup, "_state_path", lambda: self.state_file)
        yield

    def _on_disk(self) -> dict:
        return json.loads(self.real_read_text(self.state_file, encoding="utf-8"))

    def _guarded_read(self):
        """A ``Path.read_text`` that fails for the state file only -- a transient
        EACCES, e.g. a Windows scanner holding the handle between the open and
        the read."""
        real = self.real_read_text
        target = self.state_file

        def guarded(path_self, *args, **kwargs):
            if Path(path_self) == target:
                raise PermissionError(13, "Permission denied")
            return real(path_self, *args, **kwargs)

        return guarded

    def _break_reads(self, monkeypatch):
        monkeypatch.setattr(Path, "read_text", self._guarded_read())

    def test_a_transient_read_failure_does_not_wipe_the_other_account(self, monkeypatch):
        backup.set_nightly(ACCOUNT, True)
        assert self._on_disk()["accounts"][ACCOUNT]["nightly"] is True
        # Captured through read_bytes, which `_break_reads` does not patch, so the
        # comparison below is against the real pre-failure bytes.
        before = self.state_file.read_bytes()

        self._break_reads(monkeypatch)
        with pytest.raises(OSError):
            backup.set_nightly(OTHER_ACCOUNT, True)

        # The strongest form of the invariant: the file was not rewritten AT ALL.
        # `_locked_state_update` rewrites the whole document from whatever the
        # in-lock read returned, so a lenient read that collapses OSError to `{}`
        # publishes an empty base over live state. Byte equality rules out a
        # partial write and a dropped run record too, not just a surviving flag.
        assert self.state_file.read_bytes() == before
        # And the same harm in human terms: the first account's authorization to
        # run unattended paid uploads is still on disk.
        assert self._on_disk()["accounts"][ACCOUNT]["nightly"] is True

    def test_a_missing_file_is_still_a_first_write(self):
        # The one failure where an empty base IS the truth -- this must keep
        # working, so the guard above cannot be "refuse whenever the read fails".
        assert not self.state_file.exists()
        backup.set_nightly(ACCOUNT, True)
        assert self._on_disk()["accounts"][ACCOUNT]["nightly"] is True

    def test_a_corrupt_file_still_repairs_on_write(self):
        # Deliberate existing behaviour (see `_account_state`): a corrupted
        # document is replaced by the mutation rather than crashing it. Pinned
        # here so the unreadable-file guard is not mistaken for a licence to
        # start failing on corruption too.
        self.state_file.write_text("{not json", encoding="utf-8")
        backup.set_nightly(ACCOUNT, True)
        assert self._on_disk()["accounts"][ACCOUNT]["nightly"] is True

    def test_a_completed_run_reports_itself_without_publishing_over_unread_state(self, monkeypatch):
        # `_record_run` runs AFTER the archive is already in the bucket. Raising
        # would 500 a request whose upload succeeded and send the operator back
        # to the button for a duplicate -- the same harm the corrupt-`runs`
        # branch avoids. It must neither raise nor destroy the other account.
        backup.set_nightly(ACCOUNT, True)
        self._break_reads(monkeypatch)

        record = backup._record_run(OTHER_ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/x.tar.gz", 7)

        assert record["key"] == "snapshots/x.tar.gz"
        assert record["bytes"] == 7
        assert self._on_disk()["accounts"][ACCOUNT]["nightly"] is True

    def test_the_log_names_the_read_when_the_read_is_what_failed(self, caplog):
        backup.set_nightly(ACCOUNT, True)
        with (
            caplog.at_level(logging.ERROR),
            mock.patch.object(Path, "read_text", self._guarded_read()),
        ):
            backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/x.tar.gz", 7)

        assert "could not be read" in caplog.text
        assert "could not be written" not in caplog.text

    def test_a_run_lost_to_a_transient_read_failure_does_not_re_upload(self):
        # A PERSISTING read failure needs no guard: `due_for_nightly` asks
        # `nightly_enabled` first, which reads through `read_state`, so an
        # unreadable file collapses authorization to False and the loop goes
        # quiet by itself. The repeat belongs to a read failure that CLEARS --
        # authorization comes back on the next wake, the stamp is still missing,
        # and the loop would upload the same archive again.
        backup.set_nightly(ACCOUNT, True)
        with mock.patch.object(Path, "read_text", self._guarded_read()):
            assert backup.due_for_nightly(ACCOUNT) is False  # quiet while unreadable
            backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/x.tar.gz", 7)

        # Reads work again: authorization is back, the stamp never landed.
        assert backup.nightly_enabled(ACCOUNT) is True
        assert backup.KIND_SNAPSHOT not in self._on_disk()["accounts"][ACCOUNT].get("runs", {})
        assert backup.due_for_nightly(ACCOUNT) is False


class TestALostRunWriteDoesNotReUploadForever:
    """A run whose state WRITE failed must not leave the nightly loop due.

    ``_record_run`` deliberately does not raise: the archive is already in the
    bucket, so a 500 would send the operator back to the button for a duplicate
    upload. On its own, though, not raising is a worse bug than the one it
    avoids. ``due_for_nightly`` reads due-ness from the PERSISTED stamp and
    ``hooks._run_once`` calls it on every wake, so a write that never landed
    leaves the loop permanently due -- it re-uploads, unattended and billable, on
    every wake, behind one log line nobody reads.

    Holding the run in process-local memory bounds that to at most one extra
    upload per gateway restart, which is honest: the archive really is in the
    bucket, and this process really did put it there.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse state pin moved verbatim; no test in this module calls monkeypatch.undo()
    def _isolated_state(self, tmp_path, monkeypatch):
        self.state_file = tmp_path / "backup.json"
        monkeypatch.setattr(backup, "_state_path", lambda: self.state_file)
        yield

    def _on_disk(self) -> dict:
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def _full_disk(self):
        """The read succeeds and the WRITE fails. This is the case the single
        ``except OSError`` swallowed while its log line blamed the read."""

        def raiser(_state):
            raise OSError(errno.ENOSPC, "No space left on device")

        return mock.patch.object(backup, "write_state", raiser)

    def test_the_fact_is_read_through_the_same_overlay_as_the_candidate_set(self):
        """Both halves of one decision must come from one snapshot.

        The sweep's candidate set and version set both merge this process's held run
        records. A predicate reading only the persisted document put the two halves on
        different snapshots BY CONSTRUCTION: a concurrent run's key was already a live
        candidate while the fact protecting it was invisible.

        MUTATION: drop the overlay branch from the predicate and this reddens.
        """
        backup._unpersisted_runs.clear()
        try:
            with self._full_disk():
                backup._record_run(
                    ACCOUNT,
                    backup.KIND_SESSIONS,
                    "sessions/wide.tar.gz",
                    7,
                    conversations_retained=True,
                )
            # The persisted document does NOT carry the fact -- the write failed, so
            # the file may not even exist yet.
            assert not self.state_file.exists() or (
                backup.SESSIONS_CONVERSATIONS_RETAINED_KEY
                not in self._on_disk()["accounts"].get(ACCOUNT, {})
            )
            # The sweep's own overlay sees the held key, so the predicate must too.
            assert "sessions/wide.tar.gz" in backup.uploaded_keys(ACCOUNT)
            assert backup.a_retained_archive_carries_conversations(ACCOUNT) is True
        finally:
            backup._unpersisted_runs.clear()

    def test_the_gate_rechecks_the_fact_before_deleting(self):
        """The candidate set is built outside the lock; the fact is re-read inside it.

        Two same-account sessions runs can overlap, so a wide run in ANOTHER process can
        land its archive and its fact after this sweep chose its candidates. Re-reading
        inside the hold that already re-reads the keep count catches that, and refusing
        costs a kept archive until the next sweep rather than the only copy.

        MUTATION: remove the re-check, or pass the flag unconditionally from a run that
        carried conversations, and this reddens.
        """
        backup._unpersisted_runs.clear()
        try:
            backup.set_retention_keep(ACCOUNT, 1)
            # A wide run lands its fact AFTER a narrow sweep would have chosen candidates.
            backup._record_run(
                ACCOUNT,
                backup.KIND_SESSIONS,
                "sessions/wide.tar.gz",
                7,
                conversations_retained=True,
            )
            # `_authorize_upload` and the delete are BOTH mocked, and that is not
            # belt-and-braces: unmocked, this reaches the real AWS CLI against whatever
            # profile the host happens to have and then a real `delete_object_versions`.
            # A unit test must not touch host credentials, the network, or an object
            # store. The re-check under test runs before either, so mocking them cannot
            # weaken the assertion -- it only stops the failure path from doing damage.
            with (
                mock.patch.object(backup, "_authorize_upload", lambda *a, **k: None),
                mock.patch.object(backup.storage, "delete_object_versions") as deleted,
                pytest.raises(backup._RetentionCountWithdrawn) as caught,
            ):
                backup._delete_under_the_retention_gate(
                    ACCOUNT,
                    1,
                    "profile",
                    "region",
                    "bucket",
                    [("sessions/old.tar.gz", "v1")],
                    caller="test",
                    recheck_conversations_retained=True,
                )
            assert "conversations" in caught.value.reason
            # Refused BEFORE the irreversible call, not after it.
            assert deleted.call_count == 0
            # And the refusal must be AUDITABLE as a refusal. Carried on the exception
            # rather than re-derived from the reason string at the handler, because an
            # unrecognised reason falls to the `successful` branch with an empty error --
            # which would make a REFUSED permanent delete byte-identical in the SEL record
            # to a healthy sweep that retired nothing, while the caller-side decline for
            # this very condition records `failed` plus the reason.
            #
            # MUTATION: drop `audit_as_failure=True` from the raise, or drop the flag from
            # the handler's condition, and this reddens.
            assert caught.value.audit_as_failure is True
        finally:
            backup._unpersisted_runs.clear()

    def test_a_lost_write_still_recovers_the_conversations_retained_fact(self):
        """The safety fact must survive the write that loses the run record.

        Held only on the account, it would vanish on an `ENOSPC` or read-only-filesystem
        write while the archive it protects stayed in the drive -- and the only thing
        that could set it again is another conversation-bearing run, which a narrowed
        scope makes impossible. The sweep would then read False and erase the only copy,
        with no recovery. So the record carries it and the merge puts it back.

        MUTATION: stop putting the marker on the record, or gate the merge's restore on
        `_run_is_newer`, and this reddens.
        """
        backup._unpersisted_runs.clear()
        try:
            with self._full_disk():
                backup._record_run(
                    ACCOUNT,
                    backup.KIND_SESSIONS,
                    "sessions/x.tar.gz",
                    7,
                    conversations_retained=True,
                )
            # The overlay already answers True (see the overlay test above), so what this
            # test pins is the DURABLE half: the fact must reach disk on the next
            # successful write, because the overlay dies with this process and the
            # archive it protects does not.
            assert backup.a_retained_archive_carries_conversations(ACCOUNT) is True
            assert (
                backup.SESSIONS_CONVERSATIONS_RETAINED_KEY
                not in self._on_disk().get("accounts", {}).get(ACCOUNT, {})
                if self.state_file.exists()
                else True
            )

            # Any later successful state update merges the held record -- and must bring
            # the fact with it, not just the run.
            backup.set_nightly(ACCOUNT, True)
            assert (
                self._on_disk()["accounts"][ACCOUNT][backup.SESSIONS_CONVERSATIONS_RETAINED_KEY]
                is True
            )
        finally:
            backup._unpersisted_runs.clear()

    def test_a_superseded_run_still_sets_the_conversations_retained_fact(self):
        """Which record wins the slot says nothing about what the drive holds.

        A superseded run still PUT a conversation-bearing archive in the bucket, and the
        sweep erases objects rather than records, so the fact is true whichever record
        the document keeps.

        MUTATION: move the set back inside the `if not superseded:` branch and this
        reddens.
        """
        backup._unpersisted_runs.clear()
        try:
            backup._record_run(ACCOUNT, backup.KIND_SESSIONS, "sessions/a.tar.gz", 7)
            # Make the stored record look NEWER than the next one this process writes.
            state = self._on_disk()
            stored = state["accounts"][ACCOUNT]["runs"][backup.KIND_SESSIONS]
            stored["sequence"] = stored["sequence"] + 50
            state["accounts"][ACCOUNT].pop(backup.SESSIONS_CONVERSATIONS_RETAINED_KEY, None)
            self.state_file.write_text(json.dumps(state), encoding="utf-8")

            backup._record_run(
                ACCOUNT,
                backup.KIND_SESSIONS,
                "sessions/b.tar.gz",
                7,
                conversations_retained=True,
            )

            after = self._on_disk()["accounts"][ACCOUNT]
            # The record was NOT replaced -- proving this run really was superseded.
            assert after["runs"][backup.KIND_SESSIONS]["key"] == "sessions/a.tar.gz"
            # And the fact is set anyway.
            assert after[backup.SESSIONS_CONVERSATIONS_RETAINED_KEY] is True
        finally:
            backup._unpersisted_runs.clear()

    def test_the_recovery_merge_can_never_lower_the_fact(self):
        """Monotonic by contract: a held record without the marker must not clear it.

        The fact is only ever set, and the merge is the one place a reader might be
        tempted to assign it from the record instead. A conversation-bearing archive that
        reached the drive is not undone by a later run that carried none.

        MUTATION: assign the fact from the record in `_merge_pending`
        (`entry[KEY] = record.get(...)`) instead of setting it only when true, and this
        reddens.
        """
        backup._unpersisted_runs.clear()
        try:
            with self._full_disk():
                backup._record_run(
                    ACCOUNT,
                    backup.KIND_SESSIONS,
                    "sessions/wide.tar.gz",
                    7,
                    conversations_retained=True,
                )
            backup.set_nightly(ACCOUNT, True)
            assert backup.a_retained_archive_carries_conversations(ACCOUNT) is True

            # A later run carrying NO conversations, held and then merged.
            with self._full_disk():
                backup._record_run(ACCOUNT, backup.KIND_SESSIONS, "sessions/narrow.tar.gz", 7)
            backup.set_nightly(ACCOUNT, True)
            assert backup.a_retained_archive_carries_conversations(ACCOUNT) is True
        finally:
            backup._unpersisted_runs.clear()

    def test_a_lost_write_does_not_leave_the_nightly_loop_due(self):
        backup.set_nightly(ACCOUNT, True)
        with self._full_disk():
            backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/x.tar.gz", 7)

        # Nothing reached disk -- the stamp the loop reads is genuinely absent.
        assert backup.KIND_SNAPSHOT not in self._on_disk()["accounts"][ACCOUNT].get("runs", {})
        # And yet the loop must not re-upload an archive that is already there.
        assert backup.due_for_nightly(ACCOUNT) is False

    def test_the_completed_run_still_reports_itself_to_the_caller(self):
        backup.set_nightly(ACCOUNT, True)
        with self._full_disk():
            record = backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/x.tar.gz", 7)

        # No raise: the upload succeeded, so the handler must not 500 the
        # operator into pressing the button again for a duplicate.
        assert record["key"] == "snapshots/x.tar.gz"
        assert record["bytes"] == 7

    def test_the_held_run_is_what_the_panel_reads_too(self):
        # `due_for_nightly` reads through `last_runs`, so that is where the
        # overlay lands -- and showing it is truthful, not a white lie: the
        # archive is in the bucket.
        backup.set_nightly(ACCOUNT, True)
        with self._full_disk():
            backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/x.tar.gz", 7)

        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT]["key"] == "snapshots/x.tar.gz"

    def test_a_held_success_hides_the_failure_it_superseded(self):
        """The status projections must describe the same latest attempt.

        The successful upload is held in memory when its state write fails, and
        ``last_runs`` already serves it so the schedule does not upload again.  Serving
        the older persisted failure beside that success reports a healthy account as
        failing until some unrelated later state write happens to drain the overlay.
        """
        backup.set_nightly(ACCOUNT, True)
        witness = backup.nightly_run_witness(ACCOUNT, backup.KIND_SNAPSHOT)
        backup.record_nightly_failure(
            ACCOUNT, backup.KIND_SNAPSHOT, "mount gone", run_witness=witness
        )
        assert backup.KIND_SNAPSHOT in backup.nightly_failures(ACCOUNT)
        # Stamp the failure strictly before the success, so the order does not rest on
        # two writes landing on different ticks of a coarse clock (Windows resolves
        # `datetime.now` to ~15 ms, so the row and the held run can share a microsecond).
        state = backup.read_state()
        row = state["accounts"][ACCOUNT][backup.NIGHTLY_FAILURE_STATE_KEY][backup.KIND_SNAPSHOT]
        earlier = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat(
            timespec="microseconds"
        )
        row["at"] = earlier
        row["since"] = earlier
        backup.write_state(state)

        with self._full_disk():
            backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/x.tar.gz", 7)

        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT]["key"] == "snapshots/x.tar.gz"
        assert backup.nightly_failures(ACCOUNT) == {}

    def test_the_log_names_the_write_not_the_read(self, caplog):
        backup.set_nightly(ACCOUNT, True)
        with caplog.at_level(logging.ERROR), self._full_disk():
            backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/x.tar.gz", 7)

        # The old line said the state file "could not be read", which sends
        # whoever reads it to check permissions on what is really a full disk.
        assert "could not be written" in caplog.text
        assert "could not be read" not in caplog.text

    def test_a_later_successful_write_takes_over_from_memory(self):
        # The memory entry is a stopgap, not a second source of truth: once a
        # write lands, disk answers and the entry is dropped.
        backup.set_nightly(ACCOUNT, True)
        with self._full_disk():
            backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/lost.tar.gz", 7)
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT]["key"] == "snapshots/lost.tar.gz"

        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/kept.tar.gz", 9)

        runs = self._on_disk()["accounts"][ACCOUNT]["runs"]
        assert runs[backup.KIND_SNAPSHOT]["key"] == "snapshots/kept.tar.gz"
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT]["key"] == "snapshots/kept.tar.gz"
        assert (str(self.state_file), ACCOUNT, backup.KIND_SNAPSHOT) not in backup._unpersisted_runs

    def test_an_entry_never_answers_for_a_different_state_document(self, tmp_path, monkeypatch):
        # The memory key carries the state FILE, so a held run is a claim about
        # one document only. A relocated data home reads its own truth -- and
        # this is also what keeps the tests hermetic with no reset hook.
        backup.set_nightly(ACCOUNT, True)
        with self._full_disk():
            backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/x.tar.gz", 7)
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT]["key"] == "snapshots/x.tar.gz"

        elsewhere = tmp_path / "moved" / "backup.json"
        monkeypatch.setattr(backup, "_state_path", lambda: elsewhere)
        assert backup.last_runs(ACCOUNT) == {}

    def test_an_older_acknowledgement_does_not_evict_a_newer_held_run(self):
        # A stale acknowledgement cannot clear a subsequently held record.
        backup.set_nightly(ACCOUNT, True)
        persisted = backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/older.tar.gz", 7)
        newer = {
            "key": "snapshots/newer.tar.gz",
            "bytes": 11,
            "at": dt.datetime(2099, 1, 1, tzinfo=dt.timezone.utc).isoformat(
                timespec="microseconds"
            ),
        }
        backup._remember_unpersisted(ACCOUNT, backup.KIND_SNAPSHOT, newer)
        backup._forget_unpersisted(ACCOUNT, backup.KIND_SNAPSHOT, persisted)

        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT]["key"] == "snapshots/newer.tar.gz"

    def test_a_stale_held_run_is_still_evicted(self):
        # The counterpart: monotonic must not become "never evict", or the entry
        # would outlive the write that supersedes it.
        backup.set_nightly(ACCOUNT, True)
        stale = {
            "key": "snapshots/stale.tar.gz",
            "bytes": 3,
            "at": dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc).isoformat(
                timespec="microseconds"
            ),
        }
        backup._remember_unpersisted(ACCOUNT, backup.KIND_SNAPSHOT, stale)

        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/fresh.tar.gz", 7)

        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT]["key"] == "snapshots/fresh.tar.gz"
        assert (str(self.state_file), ACCOUNT, backup.KIND_SNAPSHOT) not in backup._unpersisted_runs

    def test_the_run_is_stamped_inside_the_lock_not_before(self):
        # Two concurrent runs can stamp in one order and acquire the sidecar lock
        # in the other, so a stamp taken BEFORE the lock does not order the
        # writes: the older-stamped record can write last and the ledger then
        # names the wrong archive. It also undermines everything that compares
        # these stamps -- the overlay's newest-wins and the monotonic eviction --
        # both of which assume stamp order equals write order.
        #
        # Asserted without threads: delay the locked section, capture a moment
        # from inside it, and require the record's stamp to be no earlier. A
        # stamp taken before the lock is necessarily earlier than that moment.
        backup.set_nightly(ACCOUNT, True)
        observed = {}
        real_read = backup._read_state_for_update

        def slow_read():
            # flake-ok: pre-existing 10 ms delay and wall-clock read inside the patched locked read; moved verbatim and the stamp is compared with that moment
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing delay inside the patched locked read moved verbatim
            time.sleep(0.01)
            observed["inside"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")
            return real_read()

        with mock.patch.object(backup, "_read_state_for_update", slow_read):
            record = backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/x.tar.gz", 7)

        assert record["at"] >= observed["inside"]
        # And the stamp that reached disk is that same authoritative one.
        on_disk = self._on_disk()["accounts"][ACCOUNT]["runs"][backup.KIND_SNAPSHOT]
        assert on_disk["at"] == record["at"]

    def test_a_failed_read_keeps_the_provisional_stamp(self):
        # `mutate` never runs when the read fails, so the pre-lock stamp is all
        # there is. It must still be a usable timestamp: the held record is
        # ordered against the persisted one, and `due_for_nightly` parses it.
        backup.set_nightly(ACCOUNT, True)
        real_read = backup._read_state_for_update

        def broken_read():
            raise backup._StateUnreadable(13, "Permission denied")

        with mock.patch.object(backup, "_read_state_for_update", broken_read):
            record = backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/x.tar.gz", 7)

        assert dt.datetime.fromisoformat(record["at"]).tzinfo is not None
        assert backup.due_for_nightly(ACCOUNT) is False
        assert real_read is backup._read_state_for_update  # patch scoped, not leaked

    def test_an_unresolvable_data_dir_neither_raises_nor_loses_the_run(self):
        # `_state_path()` is not a pure path join: it goes through `app_data_dir`,
        # whose last statement is mkdir(parents=True, exist_ok=True), so resolving
        # it RAISES on a read-only filesystem or EACCES. That is the same broken
        # filesystem this overlay exists to survive, and the read is already
        # guarded (`read_state` swallows OSError) -- so an unguarded key
        # derivation absorbs the failure once and then raises on the very next
        # statement, from inside `_record_run`'s own except handler.
        #
        # What the redness means when this fails: a backup that finished uploading
        # reports as a failure because the machine's app-data directory went
        # read-only, which is the defect this PR exists to remove.
        def unresolvable():
            raise OSError(errno.EROFS, "Read-only file system")

        with mock.patch.object(backup, "_state_path", unresolvable):
            record = backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/x.tar.gz", 7)

            # The completed upload still reports to its caller.
            assert record["key"] == "snapshots/x.tar.gz"
            assert record["bytes"] == 7

            # And the status read returns rather than raising -- the sentinel key
            # is consistent within the process, so the overlay still answers.
            runs = backup.last_runs(ACCOUNT)
            assert runs[backup.KIND_SNAPSHOT]["key"] == "snapshots/x.tar.gz"

    def test_only_the_exact_same_time_run_is_acknowledged(self):
        # Only the exact acknowledged run is removed, not another same-time run.
        stamp = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc).isoformat(timespec="microseconds")
        held = {"key": "snapshots/tie.tar.gz", "bytes": 5, "at": stamp}
        backup._remember_unpersisted(ACCOUNT, backup.KIND_SNAPSHOT, held)
        backup._forget_unpersisted(
            ACCOUNT, backup.KIND_SNAPSHOT, {**held, "key": "snapshots/other.tar.gz"}
        )
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT] == held

        backup._forget_unpersisted(ACCOUNT, backup.KIND_SNAPSHOT, dict(held))

        assert (backup._state_key(), ACCOUNT, backup.KIND_SNAPSHOT) not in backup._unpersisted_runs

    @pytest.mark.parametrize("backwards", [False, True], ids=["equal", "backwards"])
    def test_two_runs_in_the_same_second_are_distinguishable(self, monkeypatch, backwards):
        first_time = dt.datetime(2026, 9, 19, 11, 23, 39, 817045, tzinfo=dt.timezone.utc)
        second_time = first_time - dt.timedelta(microseconds=int(backwards))
        clock = mock.Mock(wraps=dt.datetime)
        clock.now.return_value = first_time
        monkeypatch.setattr(backup, "dt", mock.Mock(datetime=clock, timezone=dt.timezone))
        backup.set_nightly(ACCOUNT, True)
        first = backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/a.tar.gz", 1)
        clock.now.return_value = second_time
        second = backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/b.tar.gz", 2)

        assert first["at"] == first_time.isoformat(timespec="microseconds")
        assert second["at"] == second_time.isoformat(timespec="microseconds")
        assert first["process"] == second["process"]
        assert first["sequence"] < second["sequence"]
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT] == second
        assert dt.datetime.fromisoformat(second["at"]).tzinfo is not None

    def test_the_later_of_two_same_second_runs_wins_the_overlay(self):
        # The concrete harm from an unorderable stamp: the first upload persists,
        # the second fails its write in the same second, and the panel reports
        # the first archive as the last run.
        backup.set_nightly(ACCOUNT, True)
        persisted = backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/first.tar.gz", 1)
        held = {
            "key": "snapshots/second.tar.gz",
            "bytes": 2,
            # One microsecond later: same second, genuinely newer.
            "at": (
                dt.datetime.fromisoformat(persisted["at"]) + dt.timedelta(microseconds=1)
            ).isoformat(timespec="microseconds"),
        }
        backup._remember_unpersisted(ACCOUNT, backup.KIND_SNAPSHOT, held)

        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT]["key"] == "snapshots/second.tar.gz"

    @pytest.mark.parametrize("backwards", [False, True], ids=["equal", "backwards"])
    @pytest.mark.parametrize("failure", ["read", "write"])
    @pytest.mark.parametrize("recover", ["same-kind", "sibling-kind", "toggle"])
    def test_run_identity_recovery(self, monkeypatch, backwards, failure, recover):
        now = dt.datetime(2026, 9, 19, 11, 23, 39, 817045, tzinfo=dt.timezone.utc)
        clock = mock.Mock(wraps=dt.datetime)
        clock.now.return_value = now
        monkeypatch.setattr(backup, "dt", mock.Mock(datetime=clock, timezone=dt.timezone))
        backup.set_nightly(ACCOUNT, True)
        first = backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/a.tar.gz", 1, "a")
        clock.now.return_value = now - dt.timedelta(hours=int(backwards))
        target = "_read_state_for_update" if failure == "read" else "write_state"
        with mock.patch.object(backup, target, side_effect=OSError(errno.EIO, "injected")):
            second = backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/b.tar.gz", 2, "b")
        assert second["at"] == clock.now.return_value.isoformat(timespec="microseconds")
        assert self._on_disk()["accounts"][ACCOUNT]["runs"][backup.KIND_SNAPSHOT] == first
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT] == second
        assert backup.uploaded_objects(ACCOUNT)[second["key"]] == "b"
        assert backup.due_for_nightly(ACCOUNT, now=now) is False
        backup._forget_unpersisted(ACCOUNT, backup.KIND_SNAPSHOT, first)
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT] == second

        # A second failed recovery must not acknowledge or discard either upload.
        with mock.patch.object(backup, "write_state", side_effect=OSError(errno.EIO, "injected")):
            with pytest.raises(OSError):
                backup.set_nightly(ACCOUNT, True)
        assert backup.uploaded_objects(ACCOUNT)[second["key"]] == "b"
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT] == second

        expected = second
        if recover == "toggle":
            backup.set_nightly(ACCOUNT, True)
        else:
            kind = backup.KIND_SNAPSHOT if recover == "same-kind" else backup.KIND_SESSIONS
            third = backup._record_run(ACCOUNT, kind, "recovery/c.tar.gz", 3, "c")
            if recover == "same-kind":
                expected = third
        disk = self._on_disk()["accounts"][ACCOUNT]
        assert disk["runs"][backup.KIND_SNAPSHOT] == expected
        assert disk["uploads"][first["key"]] == "a"
        assert disk["uploads"][second["key"]] == "b"
        assert (backup._state_key(), ACCOUNT, backup.KIND_SNAPSHOT) not in backup._unpersisted_runs
        assert (backup._state_key(), ACCOUNT) not in backup._unpersisted_uploads
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT] == expected
        assert backup.due_for_nightly(ACCOUNT, now=now) is False

    @pytest.mark.parametrize("legacy", [False, True], ids=["prior-process", "legacy"])
    @pytest.mark.parametrize("backwards", [False, True], ids=["equal", "backwards"])
    @pytest.mark.parametrize(
        "pending_kind,record_new",
        [(None, True), ("snapshot", True), ("sessions", True), ("snapshot", False)],
    )
    def test_locked_run_and_recovery_ordering(
        self, monkeypatch, legacy, backwards, pending_kind, record_new
    ):
        now = dt.datetime(2026, 9, 19, 11, 23, 39, 817045, tzinfo=dt.timezone.utc)
        old = {
            "key": "old.tar.gz",
            "at": (now + dt.timedelta(seconds=int(backwards))).isoformat(timespec="microseconds"),
        }
        if not legacy:
            old.update(process="prior-process", sequence=7)
        backup.write_state(
            {
                "accounts": {
                    ACCOUNT: {
                        "nightly": True,
                        "runs": {backup.KIND_SNAPSHOT: old},
                        "uploads": {old["key"]: "old-fingerprint"},
                    }
                }
            }
        )
        clock = mock.Mock(wraps=dt.datetime)
        clock.now.return_value = now
        monkeypatch.setattr(backup, "dt", mock.Mock(datetime=clock, timezone=dt.timezone))
        if pending_kind is not None:
            with self._full_disk():
                pending = backup._record_run(ACCOUNT, pending_kind, "pending.tar.gz", 1, "pending")
            pending_before = dict(pending)
        expected_uploads = {"old.tar.gz": "old-fingerprint"}
        expected_run = old
        if record_new:
            expected_run = backup._record_run(
                ACCOUNT, backup.KIND_SNAPSHOT, "new.tar.gz", 2, "new-fingerprint"
            )
            assert expected_run["at"] == now.isoformat(timespec="microseconds")
            expected_uploads["new.tar.gz"] = "new-fingerprint"
        else:
            # Recovery alone is not a new run: preserve the foreign/legacy
            # wall-time fallback, but migrate the pending fingerprint.
            backup.set_nightly(ACCOUNT, True)
        disk = self._on_disk()["accounts"][ACCOUNT]
        assert disk["runs"][backup.KIND_SNAPSHOT] == expected_run
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT] == expected_run
        if pending_kind is not None:
            assert pending == pending_before
            expected_uploads["pending.tar.gz"] = "pending"
            assert (backup._state_key(), ACCOUNT, pending_kind) not in backup._unpersisted_runs
            if pending_kind == backup.KIND_SESSIONS:
                assert disk["runs"][pending_kind] == pending
        assert disk["uploads"] == expected_uploads
        assert backup.uploaded_objects(ACCOUNT) == expected_uploads
        assert (backup._state_key(), ACCOUNT) not in backup._unpersisted_uploads
        assert backup.due_for_nightly(ACCOUNT, now=now) is False

    def test_run_identity_exact_acknowledgement(self, monkeypatch):
        now = dt.datetime(2026, 9, 19, tzinfo=dt.timezone.utc)
        clock = mock.Mock(wraps=dt.datetime)
        clock.now.return_value = now
        monkeypatch.setattr(backup, "dt", mock.Mock(datetime=clock, timezone=dt.timezone))
        with self._full_disk():
            first = backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "same.tar.gz", 1, "a")
            second = backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "same.tar.gz", 1, "a")
        assert first["at"] == second["at"]
        backup._forget_unpersisted(ACCOUNT, backup.KIND_SNAPSHOT, first)
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT] == second
        backup._forget_unpersisted(ACCOUNT, backup.KIND_SNAPSHOT, dict(second))
        assert backup.last_runs(ACCOUNT) == {}
        # Run acknowledgement cannot silently acknowledge a fingerprint too.
        assert backup.uploaded_objects(ACCOUNT)[second["key"]] == "a"

    def test_run_identity_pending_metadata_is_bounded_and_recovered(self, monkeypatch):
        monkeypatch.setattr(backup, "MAX_REMEMBERED_UPLOADS", 3)
        with self._full_disk():
            for index in range(5):
                last = backup._record_run(
                    ACCOUNT, backup.KIND_SNAPSHOT, f"snapshots/{index}.tar.gz", index, str(index)
                )
        expected = {f"snapshots/{i}.tar.gz": str(i) for i in range(2, 5)}
        assert backup.uploaded_objects(ACCOUNT) == expected
        assert backup.last_runs(ACCOUNT) == {backup.KIND_SNAPSHOT: last}
        backup.set_nightly(ACCOUNT, True)
        assert self._on_disk()["accounts"][ACCOUNT]["uploads"] == expected
        assert backup.uploaded_objects(ACCOUNT) == expected

    def test_run_identity_different_process_sequences_are_not_comparable(self):
        earlier = {"process": "other", "sequence": 999, "at": "2026-09-18"}
        later = {"process": "this", "sequence": 1, "at": "2026-09-19"}
        assert backup._run_is_newer(later, earlier)
        assert not backup._run_is_newer(earlier, later)
        # Tied foreign/legacy wall times retain the disk selection, not a
        # fabricated total ordering by random process identity or sequence.
        assert not backup._run_is_newer({**earlier, "at": later["at"]}, later)

    def test_run_identity_concurrent_failure_then_recovery(self, monkeypatch):
        """Two runs in flight, the first one's state write lost, and the second
        write carries it.

        The last assertion reads :func:`_merge_pending`'s recovery, and that can only
        carry a run the in-memory hold ALREADY holds. Registration now runs inside
        the sidecar lock (``_locked_state_update``'s ``on_in_lock_failure``), so no
        contender can publish before the hold exists; before that it ran in
        ``_record_run_locked``'s ``except OSError``, after the lock was released,
        and this assertion then depended on scheduling.

        ``held`` is the handshake that positions the second write after that
        registration, which is the ordering this test's final assertion depends on.
        Gating instead on the second thread's own start states only that the thread
        is running, which leaves the ordering to the machine. With the hand-off
        inside the lock the handshake is redundant, and it is kept so this test
        does not depend on where the hold is taken. The window a released lock used
        to leave is
        :meth:`test_a_contender_cannot_publish_between_a_failed_write_and_its_hold`'s
        subject; here it is excluded.

        The two runs are still genuinely concurrent -- the contender is submitted
        while the failed writer is parked, and both take their sequence from
        ``_run_lock`` -- but nothing here asserts anything about the two contending
        for the file lock, which is why gating the second one costs no coverage. That
        contention is
        :meth:`TestSessionsArchiveLayerBGateMidBuild.test_a_long_holder_does_not_refuse_a_waiting_contender`'s
        subject, and single-threaded recovery ordering is
        :meth:`test_a_later_successful_write_takes_over_from_memory`'s.
        """
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event

        entered, release, held = Event(), Event(), Event()
        now = dt.datetime(2026, 9, 19, tzinfo=dt.timezone.utc)
        clock = mock.Mock(wraps=dt.datetime)
        clock.now.return_value = now
        monkeypatch.setattr(backup, "dt", mock.Mock(datetime=clock, timezone=dt.timezone))
        real_write = backup.write_state
        real_remember = backup._remember_unpersisted

        def writer(state):
            if not entered.is_set():
                entered.set()
                assert release.wait(10), "test did not release the failed writer"
                raise OSError(errno.EIO, "injected")
            real_write(state)

        def remember(account, kind, record):
            # Delegates first, so the event states the hold EXISTS rather than that
            # the handler is entered -- the contender's `_merge_pending` reads the
            # map, not the call.
            real_remember(account, kind, record)
            held.set()

        def second_run():
            assert held.wait(10), "the failed run was never held in memory"
            return backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "second.tar.gz", 2, "b")

        monkeypatch.setattr(backup, "write_state", writer)
        monkeypatch.setattr(backup, "_remember_unpersisted", remember)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(
                backup._record_run, ACCOUNT, backup.KIND_SNAPSHOT, "first.tar.gz", 1, "a"
            )
            try:
                assert entered.wait(10)
                second_future = pool.submit(second_run)
            finally:
                release.set()
            first = first_future.result(timeout=default_timeout())
            second = second_future.result(timeout=default_timeout())
        assert first["at"] == second["at"]
        assert first["sequence"] < second["sequence"]
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT] == second
        assert self._on_disk()["accounts"][ACCOUNT]["uploads"] == {
            "first.tar.gz": "a",
            "second.tar.gz": "b",
        }

    @pytest.mark.parametrize("failure_stage", ["read", "write"])
    def test_run_identity_failed_state_update_hand_off_runs_inside_the_state_lock(
        self, monkeypatch, failure_stage
    ):
        # A run whose state update fails must hand its record to `_remember_unpersisted`
        # BEFORE the sidecar lock is released -- i.e. before any second run-record
        # writer can read and persist state. If the hand-off ran AFTER the lock
        # released (as the original code did, from the outer except handler), a second
        # writer taking the lock in that gap would `_merge_pending` in nothing (the
        # first run is not held yet) and persist only its own record, stranding the
        # first upload in memory alone -- forgotten on restart, reopening the
        # unattended re-upload. The fix runs the hand-off inside `_locked_state_update`
        # while the sidecar lock is STILL HELD, so no reader between a failed step and
        # the hand-off can miss the first run.
        #
        # Parameterized by which step raises, because the gap is the SAME for every
        # step taken after the lock is acquired -- not the write alone. The upload
        # happens BEFORE `_record_run` is called, so a completed-upload record exists
        # whichever step fails; the READ case is the sibling defect a write-only
        # handoff left open. `_read_state_for_update` runs inside the lock, so its
        # `_StateUnreadable` must fire the in-lock handoff exactly as a failed write
        # does. This is the case the write-only callback missed: it wrapped only
        # `write_state`, so a read failure fell through to the outer except and handed
        # off with the lock already released.
        #
        # This proves the property DIRECTLY, on the calling thread, with no second
        # thread, no sleep, and no elapsed-time assumption -- so it cannot pass
        # vacuously on a contended runner where a worker was simply never scheduled.
        # `_state_lock` is wrapped in a tracker that records whether its body is
        # active, and the patched `_remember_unpersisted` reads that flag at the
        # moment the hand-off fires:
        #
        #   * Fixed production calls the hand-off from inside `_locked_state_update`'s
        #     `with _state_lock():` block, so the flag is True. The patched remember
        #     records the run first, and only AFTER the first `_record_run` has
        #     returned (lock released) does the test drive the second run in. The
        #     second's `_merge_pending` reads the held first run, so both uploads land.
        #
        #   * Old production (write-only handoff, or no handoff at all) calls remember
        #     from the outer except handler, after `_locked_state_update` has already
        #     released the lock, so the flag is False. To reproduce the exact loss
        #     deterministically, the patched remember then runs the second
        #     `_record_run` to completion BEFORE handing the first run to real remember
        #     -- exactly the interleaving the released lock permits. The second
        #     persists a document the first run is absent from, and the final on-disk
        #     assertion (both uploads present) fails at pytest call phase.
        now = dt.datetime(2026, 9, 19, tzinfo=dt.timezone.utc)
        clock = mock.Mock(wraps=dt.datetime)
        clock.now.return_value = now
        monkeypatch.setattr(backup, "dt", mock.Mock(datetime=clock, timezone=dt.timezone))
        real_state_lock = backup._state_lock
        real_write = backup.write_state
        real_read = backup._read_state_for_update
        real_remember = backup._remember_unpersisted

        state_lock_depth = 0

        @contextlib.contextmanager
        def tracking_state_lock():
            nonlocal state_lock_depth
            with real_state_lock():
                state_lock_depth += 1
                try:
                    yield
                finally:
                    state_lock_depth -= 1

        first_failed = False

        def reader():
            # The FIRST read fails (the losing run); every later read -- the second
            # run's -- returns the real document. Only patched for failure_stage="read".
            nonlocal first_failed
            if not first_failed:
                first_failed = True
                raise backup._StateUnreadable(errno.EIO, "injected")
            return real_read()

        def writer(state):
            # The FIRST write fails (the losing run); every later write -- the second
            # run's -- goes to disk for real. Only patched for failure_stage="write".
            nonlocal first_failed
            if not first_failed:
                first_failed = True
                raise OSError(errno.EIO, "injected")
            return real_write(state)

        second_done = False
        handed_off_inside_lock = None

        def remembering(account, kind, record):
            # Fires once, for the first (failed) run's hand-off. Snapshot whether the
            # sidecar lock body is active at this instant -- the property under test.
            nonlocal second_done, handed_off_inside_lock
            if handed_off_inside_lock is None:
                handed_off_inside_lock = state_lock_depth > 0
                if not handed_off_inside_lock:
                    # OLD production interleaving: the lock is already released, so a
                    # second writer can read and persist state before this run is held.
                    # Drive it to completion FIRST, then hold the first run -- the loss.
                    second_done = True
                    backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "second.tar.gz", 2, "b")
            return real_remember(account, kind, record)

        monkeypatch.setattr(backup, "_state_lock", tracking_state_lock)
        if failure_stage == "read":
            monkeypatch.setattr(backup, "_read_state_for_update", reader)
        else:
            monkeypatch.setattr(backup, "write_state", writer)
        monkeypatch.setattr(backup, "_remember_unpersisted", remembering)

        first = backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "first.tar.gz", 1, "a")
        # The hand-off fired, and it fired while the sidecar lock body was still
        # active. This is the direct proof, independent of the on-disk outcome, and it
        # holds for BOTH stages: `_read_state_for_update` runs inside the lock, so its
        # failure must reach the in-lock handoff just as `write_state`'s does.
        assert handed_off_inside_lock is True, (
            "the failed state-update hand-off ran outside the state lock; a concurrent "
            "writer could read and persist state in that window without the first run "
            "held"
        )
        # FIXED production reached remember inside the lock, so it did NOT drive the
        # second run from within; run it now, after the first has released the lock.
        if not second_done:
            second = backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "second.tar.gz", 2, "b")
        else:  # pragma: no cover - only the old-production interleaving reaches here
            second = backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT]

        assert first["at"] == second["at"]
        assert first["sequence"] < second["sequence"]
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT] == second
        # Both uploads persist: the first was held before the second could read state,
        # so the second's `_merge_pending` carried it into the committed document.
        assert self._on_disk()["accounts"][ACCOUNT]["uploads"] == {
            "first.tar.gz": "a",
            "second.tar.gz": "b",
        }

    def test_a_contender_cannot_publish_between_a_failed_write_and_its_hold(self, monkeypatch):
        """A contender parked on the sidecar lock while a run's write fails cannot
        publish a document that run is missing from.

        The lost-run hold is taken inside the sidecar lock, so the contender's
        ``_merge_pending`` always carries the failed run and BOTH uploads are on
        disk as soon as the contender returns. No later update is needed, which is
        what makes this restart-safe: if the gateway restarts right after the
        contender, the in-memory hold is gone and only the disk remains.

        When the hold was taken after the lock was released, the contender could
        publish first; the archive then lived in memory alone until the NEXT
        successful update, and a restart in between re-uploaded it. The patched
        ``_remember_unpersisted`` pins that losing order whenever it runs outside
        the lock (it waits for the contender to publish), so this test fails
        deterministically against that code. Inside the lock it does not wait:
        the contender cannot reach ``write_state`` until the lock is released, so
        waiting there would only time out. The lock depth is tracked per thread,
        so a contender that holds the lock cannot be mistaken for the failed
        writer holding it.
        """
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event, local

        entered, release, published = Event(), Event(), Event()
        now = dt.datetime(2026, 9, 19, tzinfo=dt.timezone.utc)
        clock = mock.Mock(wraps=dt.datetime)
        clock.now.return_value = now
        monkeypatch.setattr(backup, "dt", mock.Mock(datetime=clock, timezone=dt.timezone))
        real_state_lock = backup._state_lock
        real_write = backup.write_state
        real_remember = backup._remember_unpersisted
        holder = local()

        @contextlib.contextmanager
        def tracking_state_lock():
            with real_state_lock():
                holder.depth = getattr(holder, "depth", 0) + 1
                try:
                    yield
                finally:
                    holder.depth -= 1

        def writer(state):
            if not entered.is_set():
                entered.set()
                assert release.wait(10), "test did not release the failed writer"
                raise OSError(errno.EIO, "injected")
            real_write(state)
            published.set()

        handed_off_inside_lock = []

        def remember(account, kind, record):
            inside = getattr(holder, "depth", 0) > 0
            handed_off_inside_lock.append(inside)
            if not inside:
                # The lock is already released, so pin the order it permits: the
                # contender publishes before this run is held.
                assert published.wait(10), "the contender never published"
            real_remember(account, kind, record)

        monkeypatch.setattr(backup, "_state_lock", tracking_state_lock)
        monkeypatch.setattr(backup, "write_state", writer)
        monkeypatch.setattr(backup, "_remember_unpersisted", remember)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(
                backup._record_run, ACCOUNT, backup.KIND_SNAPSHOT, "first.tar.gz", 1, "a"
            )
            try:
                assert entered.wait(10)
                second_future = pool.submit(
                    backup._record_run, ACCOUNT, backup.KIND_SNAPSHOT, "second.tar.gz", 2, "b"
                )
            finally:
                release.set()
            first = first_future.result(timeout=default_timeout())
            second = second_future.result(timeout=default_timeout())

        # The contender's own document already carries the failed run.
        assert self._on_disk()["accounts"][ACCOUNT]["uploads"] == {
            "first.tar.gz": "a",
            "second.tar.gz": "b",
        }
        assert handed_off_inside_lock == [True]
        assert first["sequence"] < second["sequence"]
        # A restart drops the in-memory hold. The disk alone still knows both
        # archives, so the nightly loop does not upload the first one again.
        backup._unpersisted_runs.clear()
        assert backup.uploaded_keys(ACCOUNT) == {"first.tar.gz", "second.tar.gz"}
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT] == second


@pytest.mark.parametrize("basename", ["backup.tar.gz", "备份.tar.gz", "x" * 255])
def test_staging_name_preserves_digest_and_byte_budget(basename):
    key = "snapshots/install/" + basename
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    prefix = digest + "-"
    budget = backup.STAGING_NAME_MAX_BYTES - len(prefix)
    expected = prefix + basename.encode("utf-8")[:budget].decode("utf-8", "ignore")
    assert backup._staging_name(key) == expected
    assert len(expected.encode("utf-8")) <= backup.STAGING_NAME_MAX_BYTES


class TestCostsCacheBranches:
    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse cache pin moved verbatim; no test in this module calls monkeypatch.undo()
    def _isolated_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(costs, "_cache_path", lambda account: tmp_path / f"{account}.json")
        yield

    def test_absent_cache_reads_as_none(self, tmp_path):
        # No file at all is the common first-load case: read_cached must return
        # None (route then renders "no data yet"), not raise.
        assert costs.read_cached(ACCOUNT) is None

    def test_corrupt_json_reads_as_none(self, tmp_path):
        # A hand-edited/truncated cache that is not valid JSON must read as "no
        # cache" so the console route survives a garbled file on disk.
        (tmp_path / f"{ACCOUNT}.json").write_text("{not valid", encoding="utf-8")
        assert costs.read_cached(ACCOUNT) is None

    def test_is_fresh_false_when_stamp_key_missing(self):
        # A cache dict with no fetchedAt cannot be dated, so it is never fresh
        # (the route falls through to a re-fetch under consent).
        assert costs.is_fresh({"monthToDate": 1.0}) is False

    def test_is_fresh_false_when_stamp_is_non_string(self):
        # A corrupted stamp carrying a list/number makes fromisoformat raise
        # TypeError; that must read as not-fresh, not blow up the route.
        assert costs.is_fresh({"fetchedAt": [2026]}) is False

    def test_naive_stamp_is_treated_as_utc_and_recent_reads_fresh(self):
        # A hand-edited timezone-less stamp would make the age subtraction raise
        # TypeError against an aware now(); it is coerced to UTC instead. A
        # naive stamp for "just now" must therefore read as fresh.
        just_now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        assert costs.is_fresh({"fetchedAt": just_now.isoformat()}) is True

    def test_naive_old_stamp_is_utc_and_reads_stale(self):
        # The same coercion, but an old naive stamp: coerced to UTC it is well
        # past the 24h TTL, so it reads stale rather than raising.
        old = dt.datetime(2000, 1, 1, 0, 0, 0)
        assert costs.is_fresh({"fetchedAt": old.isoformat()}) is False
