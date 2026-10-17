"""Cloud backup retention: keep the newest N archives, erase the rest by VERSION.

Every AWS call in here is a fake. Two layers are faked, and which one a test uses
is the point of that test:

* the retention tests patch ``storage.list_object_versions`` /
  ``storage.delete_object_versions``, so they assert what retention DECIDES;
* the storage tests patch ``storage._checked``, the package's single AWS CLI
  chokepoint, so they assert the argv that would have gone to the CLI.

No test reaches a network, a profile or a bucket. The drive under test is
versioned, which is why "delete the object" is never the assertion: a delete
without a ``VersionId`` writes a marker and the bytes keep billing, so the tests
pin the version-pinned form specifically.

The suite is split by topic. This file holds what the sweep decides, the storage argv
and the audit trail; ``test_aws_control_backup_retention_unclaimed.py`` holds what the
sweep counts but can never retire, and ``test_aws_control_backup_retention_setting.py``
the retention count's writer, its gate and the last-sweep status read. The fakes they
share live in ``aws_control_backup_retention_helpers.py``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from unittest import mock

import aws_control_backup_retention_helpers
import pytest
from aws_control_backup_retention_helpers import (
    ACCOUNT,
    BUCKET,
    INSTALL,
    PROFILE,
    REGION,
    _archives,
    _key_of,
    _own,
    _prune,
    _version,
    _write_account,
)

from kiro_crew.apps.builtins.aws_control.backend import backup, storage
from kiro_crew.deploy.engine import AWSError

# The shared fixtures, bound here by name so pytest registers them for this module.
audit = aws_control_backup_retention_helpers.audit
drive = aws_control_backup_retention_helpers.drive
state = aws_control_backup_retention_helpers.state


class TestRecoveredUploadsKeepTheirVersion:
    """A state-write failure must not cost the version, or the key is never retired."""

    def test_a_recovered_upload_persists_its_version_not_only_its_fingerprint(self, state):
        # The held record carries the version; the fingerprint map that recovers it
        # did not. Recovering the key WITHOUT its version persists an archive
        # retention can never retire, and the key carries a timestamp and entropy
        # so it is never re-uploaded to self-correct: the bytes bill forever.
        #
        # Asserted against the state FILE rather than `uploaded_versions`, because
        # that reader also merges the in-process records and would answer correctly
        # from memory while the persisted document had lost the version.
        held_key = f"snapshots/{INSTALL}/kirocrew-snapshot-held.tar.gz"
        backup._remember_unpersisted(
            ACCOUNT,
            backup.KIND_SNAPSHOT,
            {
                "key": held_key,
                "bytes": 11,
                "at": "2030-01-01T00:00:00.000000+00:00",
                "fingerprint": "fp-held",
                "version": "v-held",
            },
        )
        # Any successful write drains the recovery overlay through `_merge_pending`.
        backup._record_run(
            ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/other.tar.gz", 5, "fp-other", "v-other"
        )
        doc = json.loads(backup._state_path().read_text(encoding="utf-8"))
        persisted = doc["accounts"][ACCOUNT]["upload_versions"]
        assert persisted[held_key] == "v-held"

    def test_a_held_version_is_readable_before_it_persists(self, state):
        # The other half: while the write is still failing, the version has to be
        # visible from memory, or the sweep cannot retire an upload it just made.
        #
        # TWO held uploads of one kind, and the assertion names the OLDER key.
        # `_unpersisted_runs` keeps only the newest run per kind, so the older
        # key's version lives in the by-key map alone. A single held upload sits
        # in both maps at once, so it cannot discriminate between them.
        first = f"snapshots/{INSTALL}/kirocrew-snapshot-first.tar.gz"
        second = f"snapshots/{INSTALL}/kirocrew-snapshot-second.tar.gz"
        for key, at, fingerprint, version in (
            (first, "2030-01-01T00:00:00.000000+00:00", "fp-first", "v-first"),
            (second, "2030-01-02T00:00:00.000000+00:00", "fp-second", "v-second"),
        ):
            backup._remember_unpersisted(
                ACCOUNT,
                backup.KIND_SNAPSHOT,
                {
                    "key": key,
                    "bytes": 11,
                    "at": at,
                    "fingerprint": fingerprint,
                    "version": version,
                },
            )
        # The premise, asserted rather than assumed: the newest-run map holds only
        # the second key, so the by-key map is the only thing that can answer for
        # the first one.
        assert first not in [record.get("key") for record in backup._unpersisted_runs.values()]
        versions = backup.uploaded_versions(ACCOUNT)
        assert versions[first] == "v-first"
        assert versions[second] == "v-second"


class TestHeldVersionsAreReleasedOncePersisted:
    """The other direction: a held version the state CARRIES must leave the overlay.

    ``_merge_pending`` copies the whole held-version map into every successful state
    update, so an entry that is never released is written back for the life of the
    process. That re-adds the records ``_prune_recorded_versions`` deleted on a trusted
    listing's proof, which would make the prune's decision silently temporary.

    The release paired with the fingerprint cannot cover this on its own: the two maps
    are bounded differently, so a held version outlives its ``uploads`` counterpart and
    after that eviction the paired condition can never match again.
    """

    def _orphan(self) -> str:
        """A held version whose fingerprint counterpart has been evicted. Returns its key.

        The eviction is driven through the real bound rather than by reaching into the
        map, because the bound's value IS the mechanism: a fixture that popped the entry
        by hand would still pass if the two maps were bounded together again.
        """
        held_key = f"snapshots/{INSTALL}/kirocrew-snapshot-orphan.tar.gz"
        backup._remember_unpersisted(
            ACCOUNT,
            backup.KIND_SNAPSHOT,
            {
                "key": held_key,
                "bytes": 11,
                "at": "2030-01-01T00:00:00.000000+00:00",
                "fingerprint": "fp-orphan",
                "version": "v-orphan",
            },
        )
        for n in range(backup.MAX_REMEMBERED_UPLOADS):
            backup._remember_unpersisted(
                ACCOUNT,
                backup.KIND_SNAPSHOT,
                {
                    "key": f"snapshots/{INSTALL}/kirocrew-snapshot-filler-{n:04d}.tar.gz",
                    "bytes": 1,
                    "at": f"2030-02-01T00:00:{n % 60:02d}.000000+00:00",
                    "fingerprint": f"fp-filler-{n}",
                    "version": f"v-filler-{n}",
                },
            )
        path = backup._state_key()
        # Both premises asserted, because the test says nothing if either fails: the
        # fingerprint is gone, and the version it arrived with is still held.
        assert held_key not in backup._unpersisted_uploads[(path, ACCOUNT)]
        assert backup._unpersisted_versions[(path, ACCOUNT)][held_key] == "v-orphan"
        return held_key

    @staticmethod
    def _persisted() -> dict[str, Any]:
        doc = json.loads(backup._state_path().read_text(encoding="utf-8"))
        return doc["accounts"][ACCOUNT].get("upload_versions", {})

    @staticmethod
    def _an_unrelated_successful_write(tag: str) -> None:
        backup._record_run(
            ACCOUNT, backup.KIND_SNAPSHOT, f"snapshots/other-{tag}.tar.gz", 5, f"fp-{tag}", tag
        )

    def test_a_held_version_outliving_its_fingerprint_is_still_released(self, state):
        held_key = self._orphan()
        self._an_unrelated_successful_write("v-one")
        # It reached the document, which is what makes the held copy redundant.
        assert self._persisted()[held_key] == "v-orphan"
        assert held_key not in backup._unpersisted_versions.get((backup._state_key(), ACCOUNT), {})

    def test_a_pruned_record_is_not_written_back_by_the_overlay(self, state):
        # The defect this pins, end to end and through the real prune: the record
        # persists, a trusted listing proves its object is gone, and a LATER state
        # update must not resurrect it from the recovery overlay.
        held_key = self._orphan()
        self._an_unrelated_successful_write("v-one")
        assert self._persisted()[held_key] == "v-orphan"

        backup._prune_recorded_versions(
            ACCOUNT, backup.KIND_SNAPSHOT, INSTALL, set(), eligible={held_key}
        )
        assert held_key not in self._persisted()

        # A push whose state write has not landed yet, which is the ordinary state of
        # this overlay. It is load-bearing rather than decoration: the write-back runs
        # per held FINGERPRINT, so with that map drained the map carrying the stale
        # version is never consulted and the resurrection this test exists for cannot
        # happen at all -- the test would pass against the defect.
        backup._remember_unpersisted(
            ACCOUNT,
            backup.KIND_SNAPSHOT,
            {
                "key": f"snapshots/{INSTALL}/kirocrew-snapshot-later.tar.gz",
                "bytes": 7,
                "at": "2030-03-01T00:00:00.000000+00:00",
                "fingerprint": "fp-later",
                "version": "v-later",
            },
        )
        assert backup._unpersisted_uploads[(backup._state_key(), ACCOUNT)]

        self._an_unrelated_successful_write("v-two")
        assert held_key not in self._persisted()

    def test_a_held_version_the_state_does_not_have_is_kept(self, state):
        # The direction that must NOT change: release is keyed on byte equality with
        # the persisted id, so a DIFFERENT id under the same key is exactly the record
        # the document lacks and the overlay has to keep answering for it.
        held_key = self._orphan()
        self._an_unrelated_successful_write("v-one")
        path = backup._state_key()
        backup._unpersisted_versions.setdefault((path, ACCOUNT), {})[held_key] = "v-newer"
        self._an_unrelated_successful_write("v-two")
        # `.get` rather than a subscript so a dropped entry fails as an assertion about
        # the value, not as a KeyError that reads like a crash.
        assert backup._unpersisted_versions.get((path, ACCOUNT), {}).get(held_key) == "v-newer"
        assert backup.uploaded_versions(ACCOUNT).get(held_key) == "v-newer"


class TestRetentionKeep:
    """Retention is off unless a usable count says otherwise.

    The count key IS the switch, so every way a stored value can be wrong has to
    answer "keep everything" rather than a number nobody wrote. A fallback number
    here would be a permanent delete performed on a guess.
    """

    def test_an_absent_key_is_off_not_a_default_count(self, state, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "absent.json")
        assert backup._retention_keep_for_sweep(ACCOUNT) == (None, "retention is not enabled")

    def test_there_is_no_count_that_applies_without_being_configured(self):
        # A deleting default is the thing this feature must not ship, so the symbol
        # that would carry one does not exist. Asserted rather than left implicit,
        # because re-adding it is the one change that would silently restore
        # default-on deletion.
        assert not hasattr(backup, "RETENTION_KEEP_DEFAULT")

    def test_a_configured_count_is_honoured(self, state):
        state(7)
        assert backup._retention_keep_for_sweep(ACCOUNT) == (7, "")

    def test_zero_is_clamped_to_the_floor_rather_than_read_as_off(self, state):
        # Zero IS a count somebody wrote, just an unusable one, so it clamps instead
        # of switching retention off: reading a typo as "off" would silently stop
        # doing the thing the operator asked for.
        state(0)
        assert backup._retention_keep_for_sweep(ACCOUNT) == (backup.RETENTION_KEEP_MIN, "")
        assert backup.RETENTION_KEEP_MIN == 1

    def test_a_negative_count_is_clamped_to_the_floor(self, state):
        state(-5)
        assert backup._retention_keep_for_sweep(ACCOUNT) == (backup.RETENTION_KEEP_MIN, "")

    def test_a_large_count_is_honoured_rather_than_refused_or_read_as_off(self, state):
        # There is no ceiling. A count larger than the number of archives that exist
        # keeps all of them, which is not a harm worth refusing an operator over, and
        # both alternatives were worse: clamping down deleted archives the stored
        # number named, and reading it as off ignored what they configured.
        state(10_000)
        assert backup._retention_keep_for_sweep(ACCOUNT) == (10_000, "")

    def test_a_large_count_deletes_nothing_because_it_keeps_everything(self, drive, state):
        state(10_000)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03", "04"])
        result = _prune(drive)
        assert drive.deleted == []
        # Off and "keeps everything" are different states and must read differently:
        # this one IS configured, and the status read has to say so.
        assert result["keep"] == 10_000

    def test_the_fixture_would_delete_under_a_small_count(self, drive, state):
        # The premise of the test above, asserted rather than assumed.
        state(2)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03", "04"])
        result = _prune(drive)
        assert len(drive.deleted) == 2
        assert result["retired"] == 2

    def test_no_ceiling_constant_remains(self):
        # Its docstring conceded it bounded no cost, and a constant that removes no
        # nameable harm is one more thing every future reader has to rule out.
        assert not hasattr(backup, "RETENTION_KEEP_MAX")

    def test_true_is_off_rather_than_keep_one(self, state):
        # `True` IS an int in Python, so an unguarded isinstance check would turn a
        # garbled state file into keep=1 -- the most destructive value there is.
        #
        # The reason is the ANOMALY one, not "not enabled": the key is present, so
        # somebody configured something that is not being honoured, and reporting that
        # as the ordinary off state would audit a success and leave them believing a
        # count is in force. Nothing is deleted either way -- this is about what the
        # operator is told.
        state(True)
        assert backup._retention_keep_for_sweep(ACCOUNT) == (
            None,
            "the retention setting could not be read",
        )

    def test_a_string_count_is_off_rather_than_a_fallback_number(self, state):
        state("3")
        assert backup._retention_keep_for_sweep(ACCOUNT) == (
            None,
            "the retention setting could not be read",
        )

    def test_a_float_count_is_off_rather_than_a_fallback_number(self, state):
        state(2.5)
        assert backup._retention_keep_for_sweep(ACCOUNT) == (
            None,
            "the retention setting could not be read",
        )

    def test_an_absent_key_is_the_ordinary_off_state_not_an_anomaly(self):
        # The other side of that split, so the change above does not turn every
        # unconfigured install into a warning.
        _write_account({"uploads": {}})
        assert backup._retention_keep_for_sweep(ACCOUNT) == (None, "retention is not enabled")

    def test_the_nightly_grant_never_enables_retention(self, state):
        # Authorizing unattended UPLOADS is not authorizing permanent DELETES, so
        # the count is never inferred from the adjacent grant in either direction.
        _write_account({"nightly": True})
        assert backup._retention_keep_for_sweep(ACCOUNT) == (None, "retention is not enabled")

    def test_turning_the_nightly_off_does_not_withdraw_a_configured_count(self, state):
        state(4)
        _write_account({"nightly": False})
        assert backup._retention_keep_for_sweep(ACCOUNT) == (4, "")


class TestKeepsTheNewest:
    """The rule no configuration may break."""

    def test_an_unconfigured_install_keeps_every_archive(self, drive, state, tmp_path, monkeypatch):
        # Clause one of the shipped behaviour, and the one an upgrade depends on: a
        # state file with no count configured deletes NOTHING, however many archives
        # are on the drive. A first run after an upgrade is exactly this case, so a
        # deleting default here would erase history on the strength of installing a
        # new version.
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "absent.json")
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03", "04", "05", "06"])
        result = _prune(drive)
        assert drive.deleted == []
        assert result["retired"] == 0
        assert result["keep"] == "off"
        assert result["skipped"] == "retention is not enabled"

    def test_a_configured_count_retires_the_rest(self, drive, state):
        # The other half: once turned on, it prunes to the count without asking
        # again. Turning it on is the whole consent.
        state(3)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03", "04", "05", "06"])
        result = _prune(drive)
        assert result["live"] == 6
        assert result["retired"] == 3
        survivors = {_key_of(r) for r in drive.rows} - set(drive.deleted_keys)
        assert survivors == {_key_of(r) for r in drive.rows[-3:]}

    def test_keep_one_still_leaves_the_archive_this_run_uploaded(self, drive, state):
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        newest = _key_of(drive.rows[-1])
        _prune(drive, newest=newest)
        assert newest not in drive.deleted_keys
        assert sorted(drive.deleted_keys) == sorted(_key_of(r) for r in drive.rows[:-1])

    def test_a_single_archive_is_never_deleted(self, drive, state):
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01"])
        result = _prune(drive)
        assert drive.deleted == []
        assert result["retired"] == 0

    def test_the_uploaded_key_survives_even_when_it_sorts_old(self, drive, state):
        # A clock that went backwards, or an S3 LastModified that does not agree
        # with the name, must not be able to delete the archive just written.
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["05", "06"])
        oldest = _key_of(drive.rows[0])
        _prune(drive, newest=oldest)
        assert oldest not in drive.deleted_keys

    def test_nothing_is_pruned_when_the_listing_omits_the_upload(self, drive, state):
        # The cloud form of the local `--keep` guard: a view missing the newest
        # object cannot be trusted about which objects are old.
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        result = _prune(drive, newest="snapshots/%s/kirocrew-snapshot-99.tar.gz" % INSTALL)
        assert drive.deleted == []
        assert result["retired"] == 0
        assert "does not show" in result["skipped"]


class TestScoping:
    """Per kind, per install, and never the label sidecar."""

    def test_each_kind_is_listed_under_its_own_prefix(self, drive, state):
        state(1)
        drive.rows = _archives(backup.KIND_SESSIONS, ["01", "02"])
        _prune(drive, kind=backup.KIND_SESSIONS)
        assert drive.listed == [f"{backup.KIND_SUBPATHS[backup.KIND_SESSIONS]}/{INSTALL}"]

    def test_one_kinds_burst_cannot_evict_the_other_kinds_only_copy(self, drive, state):
        state(1)
        # Both kinds present in the bucket; the sweep is asked about sessions.
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"]) + _archives(
            backup.KIND_SESSIONS, ["01", "02"]
        )
        sessions = [r for r in drive.rows if _key_of(r).startswith("sessions/")]
        _prune(drive, kind=backup.KIND_SESSIONS, newest=_key_of(sessions[-1]))
        assert all(key.startswith("sessions/") for key in drive.deleted_keys)
        assert drive.deleted_keys == [_key_of(sessions[0])]

    def test_another_installs_archives_are_never_listed_or_deleted(self, drive, state):
        state(1)
        other = "b" * 32
        mine = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        theirs = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"], install=other)
        drive.rows = theirs + mine
        _prune(drive, newest=_key_of(mine[-1]))
        assert drive.listed == [f"snapshots/{INSTALL}"]
        assert not any(other in key for key in drive.deleted_keys)
        assert drive.deleted_keys == [_key_of(mine[0])]

    def test_the_label_sidecar_holds_no_keep_slot_and_is_never_deleted(self, drive, state):
        state(2)
        archives = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        label = _version(f"snapshots/{INSTALL}/{backup.LABEL_OBJECT_NAME}", "2026-01-09T00:00:00Z")
        drive.rows = archives + [label]
        result = _prune(drive, newest=_key_of(archives[-1]))
        # 3 archives, not 4: the sidecar is not an archive.
        assert result["live"] == 3
        assert _key_of(label) not in drive.deleted_keys
        assert drive.deleted_keys == [_key_of(archives[0])]

    def test_a_delete_marked_key_holds_no_keep_slot_and_is_left_alone(self, drive, state):
        state(2)
        live = _archives(backup.KIND_SNAPSHOT, ["02", "03"])
        gone_key = f"snapshots/{INSTALL}/kirocrew-snapshot-01.tar.gz"
        marker = _version(gone_key, "2026-01-04T00:00:00Z", latest=True, deleteMarker=True)
        body = _version(gone_key, "2026-01-01T00:00:00Z", latest=False)
        drive.rows = [body, marker] + live
        result = _prune(drive, newest=_key_of(live[-1]))
        assert result["live"] == 2
        assert result["retired"] == 0
        # Its noncurrent bytes are the manual delete path's pre-existing cost, and
        # erasing them here would revoke the recoverability delete_key documents.
        assert drive.deleted == []


class TestVersionAware:
    """Retention must remove BYTES, not write delete markers."""

    def test_every_retired_key_is_deleted_by_version_id(self, drive, state):
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        _prune(drive)
        assert drive.deleted == [(_key_of(drive.rows[0]), drive.rows[0]["versionId"])]
        assert all(version for _, version in drive.deleted)

    def test_only_the_recorded_version_of_a_retired_key_is_erased(self, drive, state):
        # Retiring a key erases the ONE version this install recorded writing, not
        # everything the key carries. Nothing else under it is provably ours: the
        # record holds no marker ids.
        #
        # An older delete marker under a current version of ours is a real shape --
        # delete, then push again to the same key -- and the marker is left alone.
        # It carries no bytes, so leaving it costs nothing billable.
        state(1)
        old_key = f"snapshots/{INSTALL}/kirocrew-snapshot-01.tar.gz"
        rows = [
            _version(old_key, "2026-01-01T00:00:00Z", "m1", latest=False, deleteMarker=True),
            _version(old_key, "2026-01-02T00:00:00Z", "v1"),
        ]
        newest = _archives(backup.KIND_SNAPSHOT, ["09"])
        drive.rows = rows + newest
        result = _prune(drive, newest=_key_of(newest[-1]))
        assert [v for _, v in drive.deleted] == ["v1"]
        assert result["versions"] == 1

    def test_retention_never_reaches_the_marker_writing_delete(self, drive, state, monkeypatch):
        state(1)
        exploding = mock.Mock(side_effect=AssertionError("retention used a non-version delete"))
        monkeypatch.setattr(backup.storage, "delete_key", exploding)
        monkeypatch.setattr(backup.storage, "delete_prefix", exploding)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        _prune(drive)
        assert exploding.call_count == 0
        assert drive.deleted


class TestBestEffort:
    """A cleanup that fails must never turn a completed backup into a failure."""

    def test_a_failed_listing_is_logged_and_swallowed(self, drive, state):
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        drive.list_error = AWSError("access denied")
        result = _prune(drive)
        assert result["skipped"] == "cleanup failed"
        assert drive.deleted == []

    def test_a_failed_delete_is_logged_and_swallowed(self, drive, state):
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        drive.delete_error = AWSError("delete-objects could not remove 1 object(s)")
        result = _prune(drive)
        assert result["skipped"] == "cleanup failed"

    def test_a_withdrawn_consent_refuses_the_sweep_without_failing_the_run(
        self, drive, state, monkeypatch
    ):
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        monkeypatch.setattr(
            backup,
            "_authorize_upload",
            mock.Mock(side_effect=RuntimeError("S3 consent was withdrawn")),
        )
        result = _prune(drive)
        assert result["skipped"] == "authorization refused"
        assert drive.listed == []
        assert drive.deleted == []

    def test_the_sweep_re_authorizes_under_its_own_audit_operation(self, drive, state, monkeypatch):
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        gate = mock.Mock()
        monkeypatch.setattr(backup, "_authorize_upload", gate)
        _prune(drive)
        assert gate.call_args.kwargs["operation"] == backup.SEL_OP_RETENTION
        assert backup.SEL_OP_RETENTION != backup.SEL_OP_UPLOAD

    def test_a_refused_sweep_is_not_audited_as_a_denied_upload(self, monkeypatch):
        # The upload has already succeeded by the time retention runs, so the
        # refusal must not land in the upload's bucket in the SEL.
        logged: list[dict[str, Any]] = []
        recorder = mock.Mock()
        recorder.log_api_access.side_effect = lambda **kw: logged.append(kw)
        monkeypatch.setattr(backup, "sel", lambda: recorder)
        with pytest.raises(RuntimeError):
            backup._refuse_upload(
                ACCOUNT,
                "S3 consent no longer holds",
                caller=backup.CALLER_SCHEDULED,
                operation=backup.SEL_OP_RETENTION,
            )
        assert [entry["operation"] for entry in logged] == [backup.SEL_OP_RETENTION]


class TestAudit:
    """A permanent version purge that leaves no SEL entry reads as no purge."""

    def test_a_completed_purge_is_audited_with_its_counts(self, drive, state, audit):
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        _prune(drive)
        assert len(audit) == 1
        entry = audit[0]
        assert entry["operation"] == backup.SEL_OP_RETENTION
        assert entry["outcome"] == "successful"
        assert entry["caller"] == backup.CALLER_SCHEDULED
        assert f"account={ACCOUNT}" in entry["resources"]
        assert "retired=2" in entry["resources"]
        assert "versions=2" in entry["resources"]
        assert entry["error"] == ""

    def test_a_sweep_with_nothing_to_delete_is_still_audited(self, drive, state, audit):
        state(3)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        _prune(drive)
        assert [e["outcome"] for e in audit] == ["successful"]
        assert "retired=0" in audit[0]["resources"]

    def test_a_failed_listing_is_audited_as_failed_with_its_reason(self, drive, state, audit):
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        drive.list_error = AWSError("access denied")
        _prune(drive)
        assert [e["outcome"] for e in audit] == ["failed"]
        assert "access denied" in audit[0]["error"]

    def test_a_failed_delete_is_audited_as_failed(self, drive, state, audit):
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        drive.delete_error = AWSError("delete-objects could not remove 1 object(s)")
        _prune(drive)
        assert [e["outcome"] for e in audit] == ["failed"]
        assert "delete-objects" in audit[0]["error"]

    def test_refusing_an_untrustworthy_listing_is_audited_as_failed(self, drive, state, audit):
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        _prune(drive, newest="snapshots/%s/absent.tar.gz" % INSTALL)
        assert [e["outcome"] for e in audit] == ["failed"]
        assert "does not show the archive" in audit[0]["error"]

    def test_one_refusal_files_one_event_not_a_denial_and_a_failure(
        self, drive, state, audit, monkeypatch
    ):
        # The gate audits its own decision where the decision is made. A second
        # `failed` event from the sweep would read as two separate incidents.
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])

        def _refuse(*a: Any, **k: Any) -> None:
            backup._refuse_upload(
                ACCOUNT,
                "S3 consent no longer holds",
                caller=backup.CALLER_SCHEDULED,
                operation=backup.SEL_OP_RETENTION,
            )

        monkeypatch.setattr(backup, "_authorize_upload", _refuse)
        _prune(drive)
        assert [e["outcome"] for e in audit] == ["denied"]

    def test_an_audit_that_fails_does_not_undo_or_fail_the_sweep(self, drive, state, monkeypatch):
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        recorder = mock.Mock()
        recorder.log_api_access.side_effect = RuntimeError("SEL is unwritable")
        monkeypatch.setattr(backup, "sel", lambda: recorder)
        result = _prune(drive)
        assert result["retired"] == 1
        assert result["skipped"] == ""
        assert len(drive.deleted) == 1


class TestOrdering:
    """Upload, record, label, and only then delete."""

    def test_the_sweep_runs_after_the_push_and_the_run_record(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        monkeypatch.setattr(backup, "install_identity", lambda: {"id": INSTALL, "label": "box"})
        monkeypatch.setattr(backup, "_authorize_upload", lambda *a, **k: None)

        def fake_snapshot(argv: list[str]) -> int:
            (Path(argv[0]) / "kirocrew-snapshot-20260101T000000Z.tar.gz").write_bytes(b"payload")
            return 0

        monkeypatch.setattr(backup, "snapshot_main", fake_snapshot)
        monkeypatch.setattr(backup.snapshot, "prepare_redacted_copy", lambda *a, **k: None)

        parent = mock.Mock()
        monkeypatch.setattr(backup.storage, "put_file", parent.put_file)
        monkeypatch.setattr(backup, "_record_run", parent._record_run)
        monkeypatch.setattr(backup, "_publish_label", parent._publish_label)
        monkeypatch.setattr(backup, "_prune_remote_archives", parent._prune)
        parent._record_run.return_value = {"key": "k"}

        backup.run_snapshot_backup(ACCOUNT, PROFILE, REGION, BUCKET, caller=backup.CALLER_SCHEDULED)
        names = [name for name, _, _ in parent.mock_calls]
        assert names == ["put_file", "_record_run", "_publish_label", "_prune"]

    def test_the_sweep_is_handed_the_key_that_was_just_uploaded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        monkeypatch.setattr(backup, "install_identity", lambda: {"id": INSTALL, "label": "box"})
        monkeypatch.setattr(backup, "_authorize_upload", lambda *a, **k: None)
        monkeypatch.setattr(backup, "_publish_label", lambda *a, **k: None)
        monkeypatch.setattr(backup.storage, "put_file", lambda *a, **k: None)

        def fake_snapshot(argv: list[str]) -> int:
            (Path(argv[0]) / "kirocrew-snapshot-20260101T000000Z.tar.gz").write_bytes(b"payload")
            return 0

        monkeypatch.setattr(backup, "snapshot_main", fake_snapshot)
        monkeypatch.setattr(backup.snapshot, "prepare_redacted_copy", lambda *a, **k: None)
        sweep = mock.Mock()
        monkeypatch.setattr(backup, "_prune_remote_archives", sweep)

        record = backup.run_snapshot_backup(
            ACCOUNT, PROFILE, REGION, BUCKET, caller=backup.CALLER_SCHEDULED
        )
        args = sweep.call_args.args
        assert args[4] == backup.KIND_SNAPSHOT
        assert args[5] == INSTALL
        assert args[6] == record["key"]


def _row_payload(leaf: str, version: str, *, latest: bool = False) -> dict:
    """One version row in the shape the AWS CLI RETURNS, not the shape storage returns.

    The key carries the whole section prefix because ``list_object_versions`` filters
    on it. A bare leaf would be dropped by that filter before reaching any of the
    bounds below, so a test built on one would pass without ever exercising the thing
    it names.
    """
    return {
        "Key": f"backup/snapshots/{INSTALL}/{leaf}",
        "VersionId": version,
        "LastModified": "2030-01-01T00:00:00+00:00",
        "Size": 11,
        "IsLatest": latest,
    }


class TestStorageVersionListing:
    """The argv that would reach the AWS CLI."""

    def _list(self, payload: str, subpath: str = f"snapshots/{INSTALL}"):
        with mock.patch.object(storage, "_checked", return_value=payload) as checked:
            rows = storage.list_object_versions(
                PROFILE, REGION, BUCKET, "backup", subpath, account=ACCOUNT
            )
        return rows, checked.call_args.args[0]

    def test_it_asks_for_versions_scoped_to_the_folder_and_pinned_to_the_owner(self):
        _, argv = self._list(json.dumps({"Versions": []}))
        assert argv[:2] == ["s3api", "list-object-versions"]
        assert "--prefix" in argv
        assert argv[argv.index("--prefix") + 1] == f"backup/snapshots/{INSTALL}/"
        assert argv[argv.index("--expected-bucket-owner") + 1] == ACCOUNT

    def test_the_prefix_ends_in_a_slash_so_a_sibling_folder_is_out_of_reach(self):
        _, argv = self._list(json.dumps({"Versions": []}), subpath="snapshots/abc")
        assert argv[argv.index("--prefix") + 1] == "backup/snapshots/abc/"

    def test_it_pages_client_side_so_no_single_response_is_unbounded(self):
        # Paging the request is what bounds the response this process has to hold.
        # An auto-paginated listing bounds nothing, because the CLI joins every page
        # before handing any of it back, leaving no point at which a caller could
        # cap it. Completeness is not traded away for that: the token walk below
        # carries it, and a prefix too large to answer completely raises.
        _, argv = self._list(json.dumps({"Versions": []}))
        assert argv[argv.index("--max-items") + 1] == str(storage._VERSION_PAGE_ITEMS)

    def test_it_walks_the_token_chain_so_a_paged_answer_is_still_complete(self):
        first = json.dumps(
            {
                "Versions": [_row_payload("one", "v1")],
                "NextToken": "tok",
            }
        )
        second = json.dumps({"Versions": [_row_payload("two", "v2")]})
        with mock.patch.object(storage, "_checked", side_effect=[first, second]) as checked:
            rows = storage.list_object_versions(
                PROFILE, REGION, BUCKET, "backup", f"snapshots/{INSTALL}", account=ACCOUNT
            )
        # Both pages, and the second call resumed rather than restarting: without
        # the token it would re-read page one and loop forever on the same rows.
        assert [row["versionId"] for row in rows] == ["v1", "v2"]
        assert checked.call_args_list[1].args[0].count("--starting-token") == 1
        assert checked.call_args_list[1].args[0][-1] == "tok"

    def test_a_history_past_the_row_cap_raises_rather_than_answering_with_what_fits(
        self, monkeypatch
    ):
        # The cap is exercised at a small value so the payload stays cheap, and the
        # real one is asserted separately below so the two cannot drift apart.
        monkeypatch.setattr(storage, "_VERSION_ROWS_MAX", 2)
        payload = json.dumps(
            {"Versions": [_row_payload(f"a{n}", f"v{n}") for n in range(3)]},
        )
        with pytest.raises(AWSError, match="lifecycle rule"):
            self._list(payload)

    def test_the_row_cap_is_derived_from_the_delete_batch_rather_than_picked(self):
        assert storage._VERSION_ROWS_MAX == 10 * storage._DELETE_BATCH_MAX

    def test_an_over_long_key_drops_the_row_rather_than_being_cut_to_fit(self):
        # A shortened key names a DIFFERENT object, so the row is dropped. Nothing
        # this install wrote can reach the bound: `validate_key` refuses the same
        # length on the way in, so a row this drops was never ours to retire.
        long_leaf = "x" * (storage._MAX_KEY_LEN + 1)
        rows, _ = self._list(
            json.dumps(
                {
                    "Versions": [
                        _row_payload(long_leaf, "v-long"),
                        _row_payload("ok", "v-ok"),
                    ]
                }
            )
        )
        assert [row["versionId"] for row in rows] == ["v-ok"]

    def test_an_over_long_version_id_drops_the_row_rather_than_being_cut_to_fit(self):
        rows, _ = self._list(
            json.dumps(
                {
                    "Versions": [
                        _row_payload("one", "v" * (storage._MAX_VERSION_ID_LEN + 1)),
                        _row_payload("two", "v-ok"),
                    ]
                }
            )
        )
        assert [row["versionId"] for row in rows] == ["v-ok"]

    def test_an_over_long_modified_drops_the_row_too(self):
        # The comment on `_MAX_VERSION_ID_LEN` claims the bounds cover every
        # variable-length field a row retains. `modified` is retained and was not
        # covered, so the claim was false. It DROPS rather than emptying: an empty
        # timestamp sorts as oldest, which would make the row a likelier deletion
        # candidate -- the unsafe direction for a value that arrived malformed.
        rows, _ = self._list(
            json.dumps(
                {
                    "Versions": [
                        {
                            "Key": f"backup/snapshots/{INSTALL}/long.tar.gz",
                            "VersionId": "v-long",
                            "LastModified": "x" * (storage._MAX_MODIFIED_LEN + 1),
                            "Size": 10,
                            "IsLatest": True,
                        },
                        _row_payload("two", "v-ok"),
                    ]
                }
            )
        )
        assert [row["versionId"] for row in rows] == ["v-ok"]

    def test_the_modified_bound_leaves_room_for_a_real_timestamp(self):
        # A bound tight enough to drop real rows would lose deletion candidates
        # silently, which is not a safety measure.
        assert len("2026-01-01T00:00:00.000000Z") < storage._MAX_MODIFIED_LEN

    def test_versions_and_delete_markers_both_come_back_tagged(self):
        payload = json.dumps(
            {
                "Versions": [
                    {
                        "Key": f"backup/snapshots/{INSTALL}/a.tar.gz",
                        "VersionId": "v1",
                        "LastModified": "2026-01-01T00:00:00Z",
                        "Size": 5,
                        "IsLatest": True,
                    }
                ],
                "DeleteMarkers": [
                    {
                        "Key": f"backup/snapshots/{INSTALL}/b.tar.gz",
                        "VersionId": "m1",
                        "LastModified": "2026-01-02T00:00:00Z",
                        "IsLatest": True,
                    }
                ],
            }
        )
        rows, _ = self._list(payload)
        assert [(r["key"], r["versionId"], r["deleteMarker"]) for r in rows] == [
            (f"snapshots/{INSTALL}/a.tar.gz", "v1", False),
            (f"snapshots/{INSTALL}/b.tar.gz", "m1", True),
        ]

    def test_keys_come_back_section_relative_so_a_caller_can_compare_one(self):
        payload = json.dumps(
            {
                "Versions": [
                    {
                        "Key": f"backup/snapshots/{INSTALL}/a.tar.gz",
                        "VersionId": "v1",
                        "LastModified": "2026-01-01T00:00:00Z",
                    }
                ]
            }
        )
        rows, _ = self._list(payload)
        assert rows[0]["key"] == f"snapshots/{INSTALL}/a.tar.gz"
        assert not rows[0]["key"].startswith("backup/")

    def test_a_row_missing_its_version_id_is_dropped_rather_than_guessed(self):
        payload = json.dumps(
            {
                "Versions": [
                    {"Key": f"backup/snapshots/{INSTALL}/a.tar.gz", "VersionId": ""},
                    {"Key": f"backup/snapshots/{INSTALL}/b.tar.gz"},
                    {"VersionId": "v3"},
                    "not-an-object",
                    {
                        "Key": f"backup/snapshots/{INSTALL}/d.tar.gz",
                        "VersionId": "v4",
                        "LastModified": "2026-01-01T00:00:00Z",
                    },
                ]
            }
        )
        rows, _ = self._list(payload)
        assert [r["versionId"] for r in rows] == ["v4"]

    def test_an_unparseable_response_raises_rather_than_reading_as_empty(self):
        with pytest.raises(AWSError, match="could not be read as JSON"):
            self._list("<html>not json</html>")

    def test_a_whole_section_version_listing_is_refused(self):
        with mock.patch.object(storage, "_checked") as checked:
            for bad in ("", "/", "   "):
                with pytest.raises(ValueError, match="needs a folder"):
                    storage.list_object_versions(
                        PROFILE, REGION, BUCKET, "backup", bad, account=ACCOUNT
                    )
            assert checked.call_count == 0


class TestStorageVersionDelete:
    """Deletes that reclaim bytes, and the errors that hide inside a 200."""

    def _delete(self, versions, out: str = ""):
        with mock.patch.object(storage, "_checked", return_value=out) as checked:
            removed = storage.delete_object_versions(
                PROFILE, REGION, BUCKET, "backup", versions, account=ACCOUNT
            )
        return removed, checked

    def test_every_entry_carries_its_version_id_and_the_section_prefix(self):
        removed, checked = self._delete([("snapshots/x/a.tar.gz", "v1")])
        argv = checked.call_args.args[0]
        assert argv[:2] == ["s3api", "delete-objects"]
        payload = json.loads(argv[argv.index("--delete") + 1])
        assert payload["Objects"] == [{"Key": "backup/snapshots/x/a.tar.gz", "VersionId": "v1"}]
        assert payload["Quiet"] is True
        assert removed == 1

    def test_the_call_is_owner_pinned_and_json_pinned(self):
        _, checked = self._delete([("snapshots/x/a.tar.gz", "v1")])
        argv = checked.call_args.args[0]
        assert argv[argv.index("--expected-bucket-owner") + 1] == ACCOUNT
        assert argv[argv.index("--output") + 1] == "json"
        assert checked.call_args.kwargs["action"] == "s3:DeleteObjectVersion"

    def test_nothing_to_delete_makes_no_call(self):
        removed, checked = self._delete([])
        assert removed == 0
        assert checked.call_count == 0

    def test_an_entry_missing_either_half_is_skipped(self):
        removed, checked = self._delete([("", "v1"), ("snapshots/x/a.tar.gz", "")])
        assert removed == 0
        assert checked.call_count == 0

    def test_per_key_failures_inside_a_200_are_raised_not_counted(self):
        errors = json.dumps({"Errors": [{"Key": "backup/snapshots/x/a.tar.gz", "Code": "Denied"}]})
        with pytest.raises(AWSError, match="could not remove 1 object"):
            self._delete([("snapshots/x/a.tar.gz", "v1")], out=errors)

    def test_version_pinned_entries_stay_inside_the_argv_budget(self):
        # A version id roughly doubles an entry, which is why the batcher measures
        # serialized entries rather than keys.
        versions = [(f"snapshots/x/{'k' * 200}-{i}.tar.gz", "v" * 40) for i in range(500)]
        batches = storage._delete_entry_batches(
            [{"Key": storage.section_key("backup", k), "VersionId": v} for k, v in versions]
        )
        assert sum(len(batch) for batch in batches) == len(versions)
        for batch in batches:
            payload = json.dumps({"Objects": batch, "Quiet": True}, separators=(",", ":"))
            assert len(payload.encode()) <= storage._DELETE_PAYLOAD_MAX_BYTES
            assert len(payload.replace('"', '\\"').encode()) < storage._WINDOWS_CMDLINE_MAX

    def test_the_plain_key_batcher_still_returns_keys(self):
        assert storage._delete_batches(["a", "b"]) == [["a", "b"]]


class TestConsentIsLiveAtTheDelete:
    """A gate is good for the call that follows it, not for a later one."""

    def test_the_gate_runs_again_immediately_before_the_delete(self, drive, state, monkeypatch):
        # A `list_object_versions` round trip separates the sweep's opening gate
        # from the delete, so authorization has to be re-established after it.
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        calls: list[str] = []
        monkeypatch.setattr(backup, "_authorize_upload", lambda *a, **k: calls.append("gate"))
        real_list = drive.list_object_versions

        def listing(*a: Any, **k: Any) -> list[dict[str, Any]]:
            calls.append("list")
            return real_list(*a, **k)

        monkeypatch.setattr(backup.storage, "list_object_versions", listing)
        _prune(drive)
        assert calls == ["gate", "list", "gate"]

    def test_consent_withdrawn_after_the_listing_deletes_nothing(
        self, drive, state, audit, monkeypatch
    ):
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        seen = {"n": 0}

        def gate(*a: Any, **k: Any) -> None:
            seen["n"] += 1
            if seen["n"] > 1:
                raise RuntimeError("consent withdrawn")

        monkeypatch.setattr(backup, "_authorize_upload", gate)
        result = _prune(drive)
        assert drive.deleted == []
        assert result["skipped"] == "authorization withdrawn before deletion"
        # `_refuse_upload` raised, so it already filed the decision; filing again
        # here would record one withdrawal twice.
        assert audit == []


class TestOnlyOurOwnArchivesAreRetired:
    """The prefix is shared by design, so the listing is not proof of ownership."""

    def test_an_object_we_never_uploaded_is_never_deleted(self, drive, state):
        state(1)
        ours = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        foreign = _version(
            f"{backup.KIND_SUBPATHS[backup.KIND_SNAPSHOT]}/{INSTALL}/someone-else.tar.gz",
            "2026-01-01T00:00:00Z",
        )
        drive.rows = [foreign, *ours]
        _prune(drive, newest=_key_of(ours[-1]), owned={_key_of(row) for row in ours})
        assert _key_of(foreign) not in drive.deleted_keys
        assert drive.deleted_keys == [_key_of(ours[0])]

    def test_a_foreign_object_cannot_consume_a_keep_slot(self, drive, state):
        # If a co-writer's object counted toward `keep`, its upload would push one
        # of ours over the edge and delete an archive the owner asked to keep.
        state(2)
        ours = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        foreign = _version(
            f"{backup.KIND_SUBPATHS[backup.KIND_SNAPSHOT]}/{INSTALL}/theirs.tar.gz",
            "2026-01-03T00:00:00Z",
        )
        drive.rows = [foreign, *ours]
        _prune(drive, newest=_key_of(ours[-1]), owned={_key_of(row) for row in ours})
        assert drive.deleted == []


class TestRetentionOffKeepsEverything:
    """Both roads to off, and the one difference between them.

    Neither deletes. They are audited differently because not-enabled is the
    configured behaviour of an ordinary install, while a state file this process
    cannot read means an operator who DID configure a count is silently not getting
    it -- that is a fault, and filing it as success would bury it among every
    unconfigured install.
    """

    def test_a_corrupt_state_file_deletes_nothing_and_is_audited_as_failed(
        self, drive, state, audit
    ):
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03", "04"])
        _own({_key_of(row): str(row["versionId"]) for row in drive.rows})
        backup._state_path().write_text("{not json", encoding="utf-8")
        result = backup._prune_remote_archives(
            ACCOUNT,
            PROFILE,
            REGION,
            BUCKET,
            backup.KIND_SNAPSHOT,
            INSTALL,
            _key_of(drive.rows[-1]),
            caller=backup.CALLER_SCHEDULED,
        )
        assert drive.deleted == []
        assert result["skipped"] == "the retention setting could not be read"
        # Not a number: recording one would claim the sweep resolved a count it
        # never saw.
        assert result["keep"] == "off"
        assert [entry["outcome"] for entry in audit] == ["failed"]

    def test_an_absent_state_file_deletes_nothing_and_is_audited_as_successful(
        self, drive, tmp_path, monkeypatch, audit
    ):
        # Absent is not a fault. Nothing is configured, so keeping everything is the
        # behaviour the operator has, and the sweep succeeded at having nothing to do.
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "absent.json")
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03", "04"])
        result = _prune(drive)
        assert drive.deleted == []
        assert result["keep"] == "off"
        assert result["skipped"] == "retention is not enabled"
        assert [entry["outcome"] for entry in audit] == ["successful"]
        assert audit[0]["error"] == ""

    def test_an_unreadable_state_file_reads_as_unknowable_not_as_a_count(self, state):
        # Asserted directly on the reader rather than only through the sweep: an
        # operator who configured a count and meets a transient read failure must not
        # have archives erased by a number this process guessed.
        backup._state_path().write_text("{not json", encoding="utf-8")
        assert backup._retention_keep_for_sweep(ACCOUNT) == (
            None,
            "the retention setting could not be read",
        )

    def test_a_garbled_count_beside_intact_upload_records_deletes_nothing(self, drive, audit):
        # The end-to-end shape rather than the reader in isolation. A state file whose
        # account dict survives but whose count is garbage is the dangerous case: the
        # upload records are intact, so every older archive IS owned and reachable, and
        # a fallback number would have erased them. Off is what makes the pile safe,
        # not the ownership record.
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03", "04", "05"])
        rows = drive.rows
        _write_account(
            {
                backup.RETENTION_KEEP_STATE_KEY: "3",
                "uploads": {_key_of(row): "fp" for row in rows},
                "upload_versions": {_key_of(row): str(row["versionId"]) for row in rows},
            }
        )
        result = backup._prune_remote_archives(
            ACCOUNT,
            PROFILE,
            REGION,
            BUCKET,
            backup.KIND_SNAPSHOT,
            INSTALL,
            _key_of(rows[-1]),
            caller=backup.CALLER_SCHEDULED,
        )
        # The premise, asserted rather than assumed: all five keys are owned, so this
        # test would delete four of them under any fallback count.
        assert len(backup.uploaded_keys(ACCOUNT)) == 5
        assert drive.deleted == []
        assert result["keep"] == "off"
        # `"3"` is a value somebody WROTE, so the sweep keeping everything is not the
        # ordinary off state: it audits as the anomaly, which is how an operator finds
        # out the count they set is a string and is not in force.
        assert [entry["outcome"] for entry in audit] == ["failed"]

    def test_the_two_off_reasons_are_distinguishable(self, state, tmp_path, monkeypatch):
        # They share an outcome and must not share a reason, or an auditor reading
        # "nothing deleted" cannot tell a healthy unconfigured install from one whose
        # state file stopped being readable.
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "absent.json")
        off = backup._retention_keep_for_sweep(ACCOUNT)[1]
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "corrupt.json")
        (tmp_path / "corrupt.json").write_text("{not json", encoding="utf-8")
        unreadable = backup._retention_keep_for_sweep(ACCOUNT)[1]
        assert off and unreadable and off != unreadable


class TestUnauditedAuthorizationFailures:
    """A credential failure never reaches ``_refuse_upload``, so nothing filed it."""

    def test_a_credential_failure_is_audited_as_failed(self, drive, state, audit, monkeypatch):
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        monkeypatch.setattr(
            backup,
            "_authorize_upload",
            lambda *a, **k: (_ for _ in ()).throw(AWSError("expired token")),
        )
        result = _prune(drive)
        assert drive.deleted == []
        assert result["skipped"] == "authorization refused"
        assert [entry["outcome"] for entry in audit] == ["failed"]
        assert "expired token" in audit[0]["error"]

    def test_a_refusal_is_not_audited_twice(self, drive, state, audit, monkeypatch):
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        monkeypatch.setattr(
            backup,
            "_authorize_upload",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("consent withdrawn")),
        )
        _prune(drive)
        assert audit == []

    def test_an_aws_error_is_not_a_runtime_error(self):
        # The whole discriminator rests on this: `_refuse_upload` ends in a
        # RuntimeError, so anything that IS one was already filed. If AWSError ever
        # became a RuntimeError, a real credential failure would silently go
        # unaudited again, which is the defect this class exists to pin.
        assert not issubclass(AWSError, RuntimeError)


class TestPartialPurgesAreAuditedWithWhatWasErased:
    """A purge that failed halfway must not be recorded as having erased nothing."""

    def test_the_completed_count_survives_into_the_failed_audit(
        self, drive, state, audit, monkeypatch
    ):
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        monkeypatch.setattr(
            backup.storage,
            "delete_object_versions",
            lambda *a, **k: (_ for _ in ()).throw(
                storage.PartialVersionDelete(1, AWSError("throttled"))
            ),
        )
        result = _prune(drive)
        assert result["versions"] == 1
        assert [entry["outcome"] for entry in audit] == ["failed"]
        assert "versions=1" in audit[0]["resources"]

    def test_the_failure_log_does_not_claim_nothing_was_deleted(
        self, drive, state, audit, monkeypatch, caplog
    ):
        # The audit entry already carries the count, so a log line beside it saying
        # nothing was deleted contradicts the record and sends a reader looking for a
        # purge that did happen. Those bytes are unrecoverable, which is exactly the
        # sentence that must not be wrong.
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        monkeypatch.setattr(
            backup.storage,
            "delete_object_versions",
            lambda *a, **k: (_ for _ in ()).throw(
                storage.PartialVersionDelete(2, AWSError("throttled"))
            ),
        )
        with caplog.at_level(logging.WARNING, logger=backup.logger.name):
            result = _prune(drive)
        assert result["versions"] == 2
        text = caplog.text
        assert "nothing was deleted" not in text
        assert "erasing 2 object version(s)" in text

    def test_a_failure_before_any_delete_still_says_nothing_was_deleted(
        self, drive, state, monkeypatch, caplog
    ):
        # The other side of the same branch, so the fix above cannot have replaced one
        # false message with another: when the listing fails, nothing HAS been erased
        # and saying so is correct.
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        drive.list_error = AWSError("access denied")
        with caplog.at_level(logging.WARNING, logger=backup.logger.name):
            result = _prune(drive)
        assert result["versions"] == 0
        assert "nothing was deleted" in caplog.text

    def test_a_first_batch_failure_reports_no_partial(self, monkeypatch):
        # Nothing was erased, so the original error is the honest signal and
        # dressing it up as a partial would claim bytes are gone that are not.
        monkeypatch.setattr(
            storage, "_checked", lambda *a, **k: (_ for _ in ()).throw(AWSError("denied"))
        )
        with pytest.raises(AWSError):
            storage.delete_object_versions(
                PROFILE, REGION, BUCKET, "backup", [("k", "v")], account=ACCOUNT
            )

    def test_a_later_batch_failure_carries_the_earlier_count(self, monkeypatch):
        calls = {"n": 0}

        def flaky(*a: Any, **k: Any) -> dict[str, Any]:
            calls["n"] += 1
            if calls["n"] > 1:
                raise AWSError("throttled")
            return {}

        monkeypatch.setattr(storage, "_checked", flaky)
        monkeypatch.setattr(storage, "_raise_on_delete_errors", lambda out: None)
        monkeypatch.setattr(
            storage, "_delete_entry_batches", lambda entries: [entries[:2], entries[2:]]
        )
        with pytest.raises(storage.PartialVersionDelete) as caught:
            storage.delete_object_versions(
                PROFILE,
                REGION,
                BUCKET,
                "backup",
                [("a", "1"), ("b", "2"), ("c", "3")],
                account=ACCOUNT,
            )
        assert caught.value.removed == 2


class TestEveryVersionUnderAKeyMustBeOurs:
    """The record proves we wrote A version of a key, not every version of it."""

    def test_a_key_whose_current_version_is_foreign_is_neither_kept_nor_retired(self, drive, state):
        # `storage.get_file` reads whatever is CURRENT under a key unless it is handed
        # a version id, so that is where a restore starts. While a co-writer's version
        # is current, this sweep does not treat the key as a restorable copy: it holds
        # no keep slot, and it is not retired either.
        #
        # Our bytes under such a key are not unreachable any more -- the restore path
        # reads the recorded version when the current object fails the fingerprint --
        # so not counting the key is the CONSERVATIVE reading rather than the only one.
        # Retention's behaviour here is deliberately unchanged, which is what this
        # test pins.
        #
        # The overwrite is dated between archives 01 and 02 on purpose, so the two
        # readings disagree. Counting the key as live makes its newest version the
        # OLDEST of the three, which puts it past keep=1 and erases our version under
        # it; excluding the key leaves it alone. Without that dating both readings
        # delete the same set and the test proves nothing.
        state(1)
        rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        old = _key_of(rows[0])
        rows[0]["latest"] = False
        foreign = _version(old, "2026-01-01T12:00:00Z", version_id="z-theirs")
        drive.rows = [rows[0], foreign, rows[1], rows[2]]
        _prune(drive, newest=_key_of(rows[2]))
        deleted = [v for _, v in drive.deleted]
        assert str(rows[0]["versionId"]) not in deleted
        assert "z-theirs" not in deleted
        # 03 was just uploaded and holds the one keep slot, so 02 is the only retiree.
        assert deleted == [str(rows[1]["versionId"])]

    def test_an_overwritten_upload_aborts_the_sweep_with_its_own_reason(self, drive, state, audit):
        # A co-writer overwrites the key this run JUST uploaded. The archive the
        # sweep is about to trade older backups against is not the restorable copy
        # any more, so with keep=1 deleting anything would leave nothing a restore
        # can reach. It aborts.
        #
        # The reason is its own, not the missing-listing one: a listing that omits
        # the upload cannot be trusted about age at all, while this one shows the key
        # and says our version is not current. An auditor needs to tell them apart.
        state(1)
        rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        newest = _key_of(rows[1])
        rows[1]["latest"] = False
        foreign = _version(newest, "2026-01-02T12:00:00Z", version_id="z-theirs")
        drive.rows = [rows[0], rows[1], foreign]
        result = _prune(drive, newest=newest)
        assert drive.deleted == []
        assert result["skipped"] == (
            "the archive this run uploaded is not the current version of its key"
        )
        assert [entry["outcome"] for entry in audit] == ["failed"]

    def test_a_single_version_key_is_still_retired(self, drive, state):
        # The control: without this the guard could refuse everything and the two
        # assertions above would pass while retention did nothing at all.
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        _prune(drive)
        assert drive.deleted_keys == [_key_of(drive.rows[0])]

    def test_a_delete_marker_does_not_count_as_an_overwrite(self, drive, state):
        # Markers come from this app's own delete path and leave the bytes alone,
        # so counting them would make an ordinary manual delete freeze retention.
        state(1)
        rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        old = _key_of(rows[0])
        marker = _version(old, "2026-01-01T12:00:00Z", version_id="m-1", deleteMarker=True)
        marker["latest"] = False
        drive.rows = [rows[0], marker, rows[1]]
        _prune(drive, newest=_key_of(rows[1]), owned={old, _key_of(rows[1])})
        assert old in drive.deleted_keys

    def test_a_foreign_overwrite_cannot_displace_an_owned_archive_out_of_the_keep_slots(
        self, drive, state
    ):
        # The harm is one step removed from the overwritten key, which is what makes
        # it easy to miss: the co-writer's version is never deleted, but it makes ITS
        # key look like the newest archive on the drive, so a genuinely newer archive
        # of ours is pushed past `keep` and permanently erased. The operator is left
        # with fewer restorable backups than configured and nothing says why.
        #
        # Ordering by the age of OUR recorded version is what closes it: the
        # overwritten key is old, so it stays at the old end where it belongs.
        state(3)
        rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03", "04", "05"])
        oldest = _key_of(rows[0])
        rows[0]["latest"] = False
        # Dated after the newest archive on purpose: that is the inflation.
        foreign = _version(oldest, "2026-06-01T00:00:00Z", version_id="z-theirs")
        drive.rows = [rows[0], foreign] + rows[1:]
        _prune(drive, newest=_key_of(rows[-1]))
        deleted = [v for _, v in drive.deleted]
        # 05 just uploaded, 04 and 03 are the rest of keep=3, so 02 is the only
        # retiree. Counting the overwritten key as live instead would put it at the
        # top of the order and push 03 out of its slot to be erased.
        assert str(rows[2]["versionId"]) not in deleted
        assert str(rows[3]["versionId"]) not in deleted
        assert "z-theirs" not in deleted
        assert deleted == [str(rows[1]["versionId"])]

    def test_the_reader_returns_only_recorded_versions(self, state):
        # Replaces a test of the old count-based helper. What matters now is that a
        # key is absent from this map unless a version was actually recorded for it,
        # because the sweep reads absence as "do not touch".
        _own({"a": "v-a", "b": "v-b"})
        assert backup.uploaded_versions(ACCOUNT) == {"a": "v-a", "b": "v-b"}
        _write_account({"upload_versions": {"a": "v-a", "c": ""}})
        # An empty string is not a version: it is what `put_file` returns when the
        # response named none, and it must not read as one.
        assert "c" not in backup.uploaded_versions(ACCOUNT)

    def test_a_key_with_no_recorded_version_is_not_retired(self, drive, state):
        # The fail-closed direction, and the one that matters: a key this install
        # recorded by NAME but not by version cannot be proven ours, so it stays.
        # An unversioned bucket and a response that named no version both land here.
        #
        # So does every archive pushed before the version record existed, which is
        # why retention bounds forward growth and drains nothing already on a drive.
        #
        # Built without `_prune`, which records a version for every owned key by
        # design -- the point here is a key present in `uploads` and absent from
        # `upload_versions`, which that helper cannot produce.
        state(1)
        rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        drive.rows = rows
        _write_account(
            {
                "uploads": {_key_of(row): "fp" for row in rows},
                "upload_versions": {},
            }
        )
        backup._prune_remote_archives(
            ACCOUNT,
            PROFILE,
            REGION,
            BUCKET,
            backup.KIND_SNAPSHOT,
            INSTALL,
            _key_of(rows[1]),
            caller=backup.CALLER_SCHEDULED,
        )
        assert drive.deleted == []

    def test_a_null_recorded_id_from_suspended_versioning_is_not_ours(self):
        # "null" is the id S3 gives EVERY object written to a key while the bucket's
        # versioning is suspended, and an overwrite there REPLACES that version rather
        # than adding one. So a recorded "null" names no single version: a co-writer's
        # overwrite of the same key is "null" too. It must read as not-ours -- the same
        # fail-closed end as the empty id -- rather than letting two "null" ids compare
        # equal and pass a foreign object as our archive.
        row = _version("k", "2026-01-01T00:00:00Z", version_id="null")
        assert backup._current_version_is_ours([row], "null") is False

    def test_a_provable_recorded_id_against_a_null_current_version_is_not_ours(self):
        # The other side of the comparison. Our record proves one version by id, but
        # the current object under the key reports "null", which identifies nothing we
        # can match against. Routing the current id through the predicate too means an
        # unprovable current version reads as not-ours regardless of what we recorded.
        row = _version("k", "2026-01-01T00:00:00Z", version_id="null")
        assert backup._current_version_is_ours([row], "v-ours") is False

    def test_a_provable_matching_current_version_is_still_ours(self):
        # The control: the routing must not refuse a legitimate match. A current
        # version whose id both is provable and equals the record is ours.
        row = _version("k", "2026-01-01T00:00:00Z", version_id="v-ours")
        assert backup._current_version_is_ours([row], "v-ours") is True

    def test_put_file_reports_the_version_s3_assigned(self, monkeypatch):
        # Retention's whole ownership proof starts here: the id the uploader is
        # handed is the only value it owns outright, so it must survive the call.
        monkeypatch.setattr(storage, "_checked", lambda *a, **k: json.dumps({"VersionId": "v-new"}))
        assert (
            storage.put_file(PROFILE, REGION, BUCKET, "backup", "k", __file__, account=ACCOUNT)
            == "v-new"
        )

    def test_a_response_naming_no_version_reports_an_empty_one(self, monkeypatch):
        # An unversioned bucket answers without a VersionId. The upload succeeded,
        # so this cannot raise; it reports empty and the caller refuses to retire.
        monkeypatch.setattr(storage, "_checked", lambda *a, **k: json.dumps({}))
        assert (
            storage.put_file(PROFILE, REGION, BUCKET, "backup", "k", __file__, account=ACCOUNT)
            == ""
        )


class TestAMixedBatchReportsItsSuccesses:
    """A 200 response with Quiet names only failures, so the rest are erased."""

    def test_successes_inside_the_failing_batch_are_counted(self, monkeypatch):
        monkeypatch.setattr(
            storage,
            "_checked",
            lambda *a, **k: json.dumps({"Errors": [{"Key": "a", "Code": "AccessDenied"}]}),
        )
        with pytest.raises(storage.PartialVersionDelete) as caught:
            storage.delete_object_versions(
                PROFILE,
                REGION,
                BUCKET,
                "backup",
                [("a", "1"), ("b", "2"), ("c", "3")],
                account=ACCOUNT,
            )
        # Three entries, one named as failed, so two are gone and must be audited.
        assert caught.value.removed == 2

    def test_a_wholly_failed_batch_reports_no_partial(self, monkeypatch):
        errors = [{"Key": k, "Code": "AccessDenied"} for k in ("a", "b")]
        monkeypatch.setattr(storage, "_checked", lambda *a, **k: json.dumps({"Errors": errors}))
        with pytest.raises(storage.DeleteObjectsPartialFailure):
            storage.delete_object_versions(
                PROFILE, REGION, BUCKET, "backup", [("a", "1"), ("b", "2")], account=ACCOUNT
            )

    def test_the_folder_sweep_still_catches_it_as_an_aws_error(self):
        # The new type is a subclass precisely so every existing caller is
        # unaffected; a plain Exception here would change their behaviour.
        assert issubclass(storage.DeleteObjectsPartialFailure, AWSError)

    def test_the_mixed_count_reaches_the_retention_audit(self, drive, state, audit, monkeypatch):
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        monkeypatch.setattr(
            backup.storage,
            "delete_object_versions",
            lambda *a, **k: (_ for _ in ()).throw(
                storage.PartialVersionDelete(2, AWSError("mixed batch"))
            ),
        )
        _prune(drive)
        assert [entry["outcome"] for entry in audit] == ["failed"]
        assert "versions=2" in audit[0]["resources"]
