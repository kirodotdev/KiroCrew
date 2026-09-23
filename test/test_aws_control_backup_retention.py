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
"""

from __future__ import annotations

import datetime as dt
import inspect
import json
import logging
import threading
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from kiro_crew.apps.builtins.aws_control.backend import backup, storage
from kiro_crew.deploy.engine import AWSError

ACCOUNT = "111122223333"
PROFILE = "p"
REGION = "us-east-1"
BUCKET = "kirocrew-drive-0123456789ab"
INSTALL = "a" * 32


def _version(key: str, modified: str, version_id: str = "", **over: Any) -> dict[str, Any]:
    """One ``list_object_versions`` row, in the shape storage returns."""
    row = {
        "key": key,
        "versionId": version_id or f"v-{key}-{modified}",
        "modified": modified,
        "size": 10,
        "latest": True,
        "deleteMarker": False,
    }
    row.update(over)
    return row


def _archives(kind: str, stamps: list[str], install: str = INSTALL) -> list[dict[str, Any]]:
    """One archive per stamp, newest stamp last, under one install's prefix."""
    sub = backup.KIND_SUBPATHS[kind]
    return [
        _version(f"{sub}/{install}/kirocrew-snapshot-{s}.tar.gz", f"2026-01-{s}T00:00:00Z")
        for s in stamps
    ]


def _key_of(row: dict[str, Any]) -> str:
    return str(row["key"])


class _Drive:
    """A fake versioned drive: records what retention asked it to do."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.listed: list[str] = []
        self.deleted: list[tuple[str, str]] = []
        self.list_error: Exception | None = None
        self.delete_error: Exception | None = None

    def list_object_versions(self, profile, region, bucket, section, subpath, *, account):
        assert section == "backup"
        assert account == ACCOUNT
        self.listed.append(subpath)
        if self.list_error is not None:
            raise self.list_error
        # The real call is prefix-scoped; the fake honours that so a test cannot
        # accidentally pass by being handed rows the real listing would not return.
        return [row for row in self.rows if _key_of(row).startswith(f"{subpath}/")]

    def delete_object_versions(self, profile, region, bucket, section, versions, *, account):
        assert section == "backup"
        assert account == ACCOUNT
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.extend(versions)
        return len(versions)

    @property
    def deleted_keys(self) -> list[str]:
        return [key for key, _ in self.deleted]


@pytest.fixture
def drive(monkeypatch):
    """A fake drive wired into ``backup.storage``, with the live gate stubbed out."""
    made = _Drive([])
    monkeypatch.setattr(backup.storage, "list_object_versions", made.list_object_versions)
    monkeypatch.setattr(backup.storage, "delete_object_versions", made.delete_object_versions)
    monkeypatch.setattr(backup, "_authorize_upload", lambda *a, **k: None)
    return made


@pytest.fixture
def state(tmp_path, monkeypatch):
    """Isolated backup state, and a helper to set the retention override."""
    path = tmp_path / "backup.json"
    monkeypatch.setattr(backup, "_state_path", lambda: path)

    def _set(value: Any) -> None:
        # Merged, not overwritten: the sweep also reads this account's `uploads`
        # record, and a setter that replaced the document would erase it.
        _write_account({backup.RETENTION_KEEP_STATE_KEY: value})

    return _set


def _write_account(entry: dict[str, Any]) -> None:
    """Merge ``entry`` into this account's sub-dict of the isolated state file."""
    path = backup._state_path()
    doc: dict[str, Any] = {}
    if path.exists():
        doc = json.loads(path.read_text(encoding="utf-8"))
    doc.setdefault("accounts", {}).setdefault(ACCOUNT, {}).update(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _own(versions: dict[str, str]) -> None:
    """Record ``versions`` (key -> the VersionId this install wrote) as ours.

    Two maps, because the code reads two: ``uploads`` is key -> body fingerprint,
    and ``upload_versions`` is key -> version id. Retention retires only a version
    whose id is in the second one, so a sweep test that plants archives has to say
    WHICH version is its own -- which is the guard, not a fixture detail.
    """
    _write_account(
        {
            "uploads": {key: f"fp-{key}" for key in versions},
            "upload_versions": dict(versions),
        }
    )


def _prune(
    drive: _Drive,
    kind: str = backup.KIND_SNAPSHOT,
    newest: str = "",
    *,
    owned: set[str] | None = None,
) -> dict[str, Any]:
    """Run the sweep against ``drive``.

    ``owned`` is what gets recorded as this install's own uploads. It defaults to
    every key the fake drive holds, because the ordinary case is a drive holding
    only our archives and most tests here are about AGE, not ownership. A test
    about ownership passes the subset it means.
    """
    newest = newest or _key_of(drive.rows[-1])
    owned_keys = {_key_of(row) for row in drive.rows} if owned is None else owned
    # The FIRST non-marker row for each owned key is the one this install wrote;
    # anything planted after it in `drive.rows` stands for another writer's version.
    recorded: dict[str, str] = {}
    for row in drive.rows:
        key = _key_of(row)
        if key in owned_keys and key not in recorded and not row.get("deleteMarker"):
            recorded[key] = str(row["versionId"])
    _own(recorded)
    return backup._prune_remote_archives(
        ACCOUNT,
        PROFILE,
        REGION,
        BUCKET,
        kind,
        INSTALL,
        newest,
        caller=backup.CALLER_SCHEDULED,
    )


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


@pytest.fixture
def audit(monkeypatch):
    """Collect the SEL events the sweep files, and let a test make SEL fail."""
    logged: list[dict[str, Any]] = []
    recorder = mock.Mock()
    recorder.log_api_access.side_effect = lambda **kw: logged.append(kw)
    monkeypatch.setattr(backup, "sel", lambda: recorder)
    return logged


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


class TestUnclaimedArchivesAreReported:
    """An archive no sweep can ever reclaim is a cost, so it is on the record.

    A key this install wrote but recorded no version for is never deleted and holds
    no ``keep`` slot, so its bytes are billed forever with nothing in the app naming
    them. Counting them is the difference between a cost an owner can see and one
    they can only find on an invoice.

    Built without ``_prune``, which records a version for every owned key by design.
    """

    def _sweep(self, drive, rows, recorded, newest):
        _write_account(
            {
                "uploads": {_key_of(row): "fp" for row in rows},
                "upload_versions": recorded,
            }
        )
        return backup._prune_remote_archives(
            ACCOUNT,
            PROFILE,
            REGION,
            BUCKET,
            backup.KIND_SNAPSHOT,
            INSTALL,
            newest,
            caller=backup.CALLER_SCHEDULED,
        )

    def test_an_unrecorded_archive_is_counted_with_its_bytes(self, drive, state):
        state(1)
        rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        drive.rows = rows
        orphan = _key_of(rows[0])
        recorded = {_key_of(row): str(row["versionId"]) for row in rows[1:]}
        expected = sum(int(row["size"]) for row in rows if _key_of(row) == orphan)
        # Without this the byte assertion below could pass on a fixture that carries
        # no sizes, which would report coverage it does not have.
        assert expected > 0
        out = self._sweep(drive, rows, recorded, _key_of(rows[-1]))
        assert out["unclaimed"] == 1
        assert out["unclaimedBytes"] == expected
        # It is a report, not a new refusal: the recorded archive past `keep` is
        # still retired in the same sweep.
        assert drive.deleted == [(_key_of(rows[1]), str(rows[1]["versionId"]))]

    def test_every_version_under_an_unrecorded_key_is_counted_because_each_is_billed(
        self, drive, state
    ):
        state(1)
        rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        # A second version planted under the same key, standing for a co-writer's
        # overwrite. Both are billed, so both are the cost this reports.
        extra = dict(rows[0])
        extra["versionId"] = "v-other"
        extra["latest"] = False
        drive.rows = [rows[0], extra, rows[1]]
        recorded = {_key_of(rows[1]): str(rows[1]["versionId"])}
        expected = int(rows[0]["size"]) + int(extra["size"])
        out = self._sweep(drive, [rows[0], rows[1]], recorded, _key_of(rows[1]))
        assert out["unclaimed"] == 1
        assert out["unclaimedBytes"] == expected

    def test_a_sweep_that_aborts_still_reports_what_it_cannot_own(self, drive, state):
        # The case that motivated this: a drive holding only archives from before
        # versions were recorded. The sweep refuses to act on that listing, and the
        # refusal is exactly when the owner most needs the cost named.
        state(1)
        rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        drive.rows = rows
        out = self._sweep(drive, rows, {}, _key_of(rows[1]))
        assert out["skipped"]
        assert out["unclaimed"] == 2
        assert out["unclaimedBytes"] == sum(int(row["size"]) for row in rows)
        assert drive.deleted == []

    def test_the_counts_reach_the_audit_event(self, drive, state, audit):
        state(1)
        rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        drive.rows = rows
        recorded = {_key_of(row): str(row["versionId"]) for row in rows[1:]}
        out = self._sweep(drive, rows, recorded, _key_of(rows[-1]))
        assert len(audit) == 1
        assert "unclaimed=1" in audit[0]["resources"]
        assert f"unclaimedBytes={out['unclaimedBytes']}" in audit[0]["resources"]

    def test_a_drive_of_only_recorded_archives_reports_none(self, drive, state, audit):
        state(3)
        rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        drive.rows = rows
        recorded = {_key_of(row): str(row["versionId"]) for row in rows}
        out = self._sweep(drive, rows, recorded, _key_of(rows[-1]))
        assert out["unclaimed"] == 0
        assert out["unclaimedBytes"] == 0
        assert "unclaimed=0" in audit[0]["resources"]

    def test_a_key_the_install_no_longer_remembers_is_not_counted(self, drive, state, audit):
        """The disclosed limit: this is a floor ON the remembered set, not over the prefix.

        A key with neither an ``uploads`` entry nor a version record leaves
        :func:`retention_owned_keys` entirely, so the sweep filters it out before this
        measurement and it reads 0 here however many bytes it holds. That stays the
        contract: this pair is read against the ``keep`` count to say what retention will
        collect out of the set it can SEE, and absorbing an object nothing has a record of
        would make it a figure that answers neither question.

        ``unrecorded`` counts it beside this pair and claims nothing about whose it is,
        so the object is visible without being attributed to anyone. Both halves are
        asserted here so neither can drift into the other.
        """
        state(3)
        rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        drive.rows = rows
        forgotten = rows[0]
        remembered = rows[1:]
        # The control: this key carries bytes, so a count that included it would differ
        # from the count below. Without it a zero could mean an empty fixture.
        assert int(forgotten["size"]) > 0
        recorded = {_key_of(row): str(row["versionId"]) for row in remembered}
        out = self._sweep(drive, remembered, recorded, _key_of(rows[-1]))
        assert out["unclaimed"] == 0
        assert out["unclaimedBytes"] == 0
        assert "unclaimed=0" in audit[0]["resources"]
        # The other pair, which is where it does land.
        assert out["unrecorded"] == 1
        assert out["unrecordedBytes"] == int(forgotten["size"])
        # Nor is it deleted: it holds no `keep` slot and deletion draws only from `live`.
        assert drive.deleted == []


class TestTheUnclaimedFloorReachesTheStatusRead:
    """The audit event is not where an operator decides whether retention is working.

    The sweep already measured the archives it can never retire, but the counts landed
    only in a SEL event and a log line. An operator who enables a count to bound their
    bill reads the status endpoint, sees the count, and has no way to see the part of
    the bill the count will never touch -- which is the whole reason the bill does not
    fall. So the last sweep's pair is persisted and served beside it.

    Built on ``TestUnclaimedArchivesAreReported._sweep`` for the same reason that class
    is: ``_prune`` records a version for every owned key by design, so an unclaimed key
    cannot be produced through it.
    """

    _sweep = TestUnclaimedArchivesAreReported._sweep

    def _one_unrecorded(self, drive):
        """A drive whose oldest archive has no recorded version. Returns its rows."""
        rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        drive.rows = rows
        return rows

    def test_the_counts_are_served_by_the_status_reader(self, drive, state):
        state(1)
        rows = self._one_unrecorded(drive)
        recorded = {_key_of(row): str(row["versionId"]) for row in rows[1:]}
        out = self._sweep(drive, rows, recorded, _key_of(rows[-1]))
        # Guards the assertions below against a fixture that measured nothing: a
        # reader agreeing with an all-zero outcome would prove nothing at all.
        assert out["unclaimed"] == 1
        assert out["unclaimedBytes"] > 0
        served = backup.retention_unclaimed(ACCOUNT)
        assert served[backup.KIND_SNAPSHOT]["archives"] == out["unclaimed"]
        assert served[backup.KIND_SNAPSHOT]["bytes"] == out["unclaimedBytes"]

    def test_the_measurement_is_stamped_so_its_staleness_is_readable(self, drive, state):
        state(1)
        rows = self._one_unrecorded(drive)
        recorded = {_key_of(row): str(row["versionId"]) for row in rows[1:]}
        self._sweep(drive, rows, recorded, _key_of(rows[-1]))
        at = backup.retention_unclaimed(ACCOUNT)[backup.KIND_SNAPSHOT]["at"]
        # Parsed rather than merely truthy: the value exists to tell a reader WHEN the
        # listing was measured, and a string no clock can be read out of does not.
        assert dt.datetime.fromisoformat(at).tzinfo is not None

    def test_a_listing_the_sweep_refused_to_act_on_is_not_published(self, drive, state):
        # The one direction that must not be published. Past the gate the sweep has
        # declined to trust this listing about age, so it cannot be trusted about how
        # many keys it omitted either -- and an UNDERCOUNT served as the floor reads as
        # "nothing unreclaimable here", which is worse than serving nothing. The audit
        # event still carries the number, where `failed` says how much to trust it.
        state(1)
        rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        drive.rows = rows
        out = self._sweep(drive, rows, {}, _key_of(rows[1]))
        assert out["skipped"]
        assert out["unclaimed"] == 2
        assert backup.retention_unclaimed(ACCOUNT) == {}

    def test_a_measured_zero_is_published_so_absence_means_never_measured(self, drive, state):
        # Storing only a non-zero floor would make an absent kind mean either "no
        # floor" and "never swept", and those two want opposite things from a reader.
        state(3)
        rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        drive.rows = rows
        recorded = {_key_of(row): str(row["versionId"]) for row in rows}
        self._sweep(drive, rows, recorded, _key_of(rows[-1]))
        served = backup.retention_unclaimed(ACCOUNT)
        assert served[backup.KIND_SNAPSHOT] == {
            "archives": 0,
            "bytes": 0,
            "at": served[backup.KIND_SNAPSHOT]["at"],
        }

    def test_nothing_is_served_before_any_sweep_has_measured(self, state):
        state(3)
        assert backup.retention_unclaimed(ACCOUNT) == {}

    def test_each_kind_keeps_its_own_measurement(self, drive, state):
        # Per kind because the sweep is per kind: one number for both would report a
        # snapshot floor on the sessions row, and the two prefixes are swept
        # separately with their own listings.
        state(1)
        snap = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        drive.rows = snap
        self._sweep(
            drive,
            snap,
            {_key_of(row): str(row["versionId"]) for row in snap[1:]},
            _key_of(snap[-1]),
        )
        sessions = _archives(backup.KIND_SESSIONS, ["01", "02"])
        drive.rows = sessions
        _write_account({"uploads": {_key_of(row): "fp" for row in sessions}})
        backup._prune_remote_archives(
            ACCOUNT,
            PROFILE,
            REGION,
            BUCKET,
            backup.KIND_SESSIONS,
            INSTALL,
            _key_of(sessions[-1]),
            caller=backup.CALLER_SCHEDULED,
        )
        served = backup.retention_unclaimed(ACCOUNT)
        assert served[backup.KIND_SNAPSHOT]["archives"] == 1
        # `upload_versions` was left carrying only the snapshot keys, so every
        # sessions key is unrecorded -- but the newest one then has no recorded
        # version either, so that sweep aborts and publishes nothing.
        assert backup.KIND_SESSIONS not in served

    def test_a_state_write_failure_does_not_fail_the_sweep(self, drive, state, monkeypatch):
        # The sweep's contract: the archive is already off-host and the run already
        # recorded, so nothing this write can fail at may turn a successful backup
        # into a failed one. The SEL event carries the same pair regardless.
        state(1)
        rows = self._one_unrecorded(drive)
        recorded = {_key_of(row): str(row["versionId"]) for row in rows[1:]}

        def _boom(*a, **k):
            raise OSError("no space left on device")

        monkeypatch.setattr(backup, "write_state", _boom)
        out = self._sweep(drive, rows, recorded, _key_of(rows[-1]))
        # The sweep still ran and still deleted: the recorder is a satellite of it.
        assert out["unclaimed"] == 1
        assert drive.deleted == [(_key_of(rows[1]), str(rows[1]["versionId"]))]

    def test_a_corrupted_measurement_reads_as_nothing_measured(self, state):
        # A polled endpoint must not raise on a state file somebody hand-edited, and
        # every other reader in this module answers a corrupted level as empty.
        state(3)
        _write_account({backup.RETENTION_UNCLAIMED_STATE_KEY: "not a map"})
        assert backup.retention_unclaimed(ACCOUNT) == {}
        _write_account({backup.RETENTION_UNCLAIMED_STATE_KEY: {backup.KIND_SNAPSHOT: "nope"}})
        assert backup.retention_unclaimed(ACCOUNT) == {}

    def test_the_reader_does_not_claim_a_console_renderer(self):
        # The same disclosure `retention_keep` carries, for the same reason: a
        # docstring implying a panel sends the next reader looking for one.
        doc = backup.retention_unclaimed.__doc__ or ""
        assert "NO console renderer" in doc


class TestVersionRecordsOutliveThePanelHistory:
    """A version record's lifetime is the ARCHIVE's, not the 20-per-kind listing's.

    ``MAX_REMEMBERED_UPLOADS`` is a number chosen for a panel, so it must not decide
    what retention is able to retire. An install pushing nightly with retention off
    (the shipped default) passes that bound on its 201st push; every archive behind it
    keeps its version record, so a keep count enabled later can still reach it. Without
    a recorded version the ownership test refuses an archive and its bytes are billed
    for as long as the bucket keeps it, which is the floor these tests hold at zero.

    A record ends only when a listing the sweep trusted proves its object is gone, with
    ``MAX_RECORDED_VERSIONS`` as a ceiling rather than a horizon.
    """

    def _sweep(self, drive, *, kind=backup.KIND_SNAPSHOT, newest="", install=INSTALL):
        return backup._prune_remote_archives(
            ACCOUNT,
            PROFILE,
            REGION,
            BUCKET,
            kind,
            install,
            newest or _key_of(drive.rows[-1]),
            caller=backup.CALLER_SCHEDULED,
        )

    def _recorded(self):
        entry = json.loads(backup._state_path().read_text(encoding="utf-8"))
        return entry["accounts"][ACCOUNT]["upload_versions"]

    def test_a_record_survives_its_key_falling_off_the_uploads_bound(self):
        # The bug, at the write site: a push past the panel bound must not take the
        # oldest version record with it.
        entry: dict[str, Any] = {}
        keys = [
            f"snapshots/{INSTALL}/a-{n:04d}.tar.gz"
            for n in range(backup.MAX_REMEMBERED_UPLOADS + 1)
        ]
        for key in keys:
            backup._merge_uploads(entry, {key: f"fp-{key}"}, {key: f"v-{key}"})
        assert len(entry["uploads"]) == backup.MAX_REMEMBERED_UPLOADS
        assert keys[0] not in entry["uploads"]
        # The control: the key really did fall off the panel half, so the assertion
        # below is about the version half and not about an unfilled fixture.
        assert keys[0] in entry["upload_versions"]
        assert len(entry["upload_versions"]) == backup.MAX_REMEMBERED_UPLOADS + 1

    def test_the_record_map_has_its_own_backstop(self):
        # A ceiling, so a pathological document cannot grow without limit -- and the
        # oldest go first, because the newest archives are the ones a keep count keeps.
        entry: dict[str, Any] = {}
        keys = [
            f"snapshots/{INSTALL}/b-{n:05d}.tar.gz" for n in range(backup.MAX_RECORDED_VERSIONS + 5)
        ]
        for key in keys:
            backup._merge_uploads(entry, {key: "fp"}, {key: f"v-{key}"})
        assert len(entry["upload_versions"]) == backup.MAX_RECORDED_VERSIONS
        assert keys[0] not in entry["upload_versions"]
        assert keys[-1] in entry["upload_versions"]

    def test_the_backstop_sits_above_the_panel_bound_or_it_is_not_a_backstop(self):
        # A ceiling at or below the panel bound would reinstate the same cliff through
        # a differently named constant.
        assert backup.MAX_RECORDED_VERSIONS > backup.MAX_REMEMBERED_UPLOADS

    def test_a_held_records_map_is_bounded_the_same_way(self, state):
        # `_remember_unpersisted` mirrors `_merge_uploads`, so a push whose state write
        # failed must not be the path that re-imposes the cliff.
        for n in range(backup.MAX_REMEMBERED_UPLOADS + 1):
            key = f"snapshots/{INSTALL}/c-{n:04d}.tar.gz"
            backup._remember_unpersisted(
                ACCOUNT,
                backup.KIND_SNAPSHOT,
                {
                    "key": key,
                    "fingerprint": "fp",
                    "version": f"v-{key}",
                    "at": "2026-01-01T00:00:00Z",
                },
            )
        held = backup.uploaded_versions(ACCOUNT)
        assert f"snapshots/{INSTALL}/c-0000.tar.gz" in held
        assert len(held) == backup.MAX_REMEMBERED_UPLOADS + 1

    def test_an_archive_whose_panel_record_aged_out_is_now_retired(self, drive, state):
        """The reclaim this whole change exists for, end to end.

        The oldest archive is absent from ``uploads`` (its panel entry aged out) and
        present in ``upload_versions``. Before this change the sweep filtered it out of
        the listing on the ``uploads`` membership test alone, so it could never be a
        candidate however old it was. It is retired here on the SAME proof as any other
        archive: the recorded id is the version the listing shows as current.
        """
        state(1)
        rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        drive.rows = rows
        aged_out = _key_of(rows[0])
        _write_account(
            {
                # The panel half remembers only the newest two.
                "uploads": {_key_of(row): "fp" for row in rows[1:]},
                # The version half remembers all three.
                "upload_versions": {_key_of(row): str(row["versionId"]) for row in rows},
            }
        )
        out = self._sweep(drive)
        assert aged_out in drive.deleted_keys
        # And on proof, not on presence: the version erased is the recorded one.
        assert (aged_out, str(rows[0]["versionId"])) in drive.deleted
        assert out["retired"] == 2
        # It is not double-counted as a floor: it was owned, so it is neither unclaimed
        # nor unrecorded.
        assert out["unclaimed"] == 0
        assert out["unrecorded"] == 0

    def test_a_record_is_dropped_once_a_trusted_listing_proves_the_object_is_gone(
        self, drive, state
    ):
        state(3)
        rows = _archives(backup.KIND_SNAPSHOT, ["02", "03"])
        drive.rows = rows
        gone = f"snapshots/{INSTALL}/kirocrew-snapshot-01.tar.gz"
        _write_account(
            {
                "uploads": {_key_of(row): "fp" for row in rows},
                "upload_versions": {
                    **{_key_of(row): str(row["versionId"]) for row in rows},
                    gone: "v-gone",
                },
            }
        )
        # The control: the record is there to begin with, so its absence below is the
        # prune and not an unfilled fixture.
        assert gone in self._recorded()
        self._sweep(drive)
        assert gone not in self._recorded()
        # Kept: the listing shows these, so it proves nothing about them being gone.
        assert set(self._recorded()) == {_key_of(row) for row in rows}

    def test_a_listing_that_raises_prunes_nothing(self, drive, state, monkeypatch):
        """A failed listing is not evidence of absence.

        This is also the partial-data case. ``storage.list_object_versions`` walks the
        whole token chain and RAISES rather than returning a first page -- including when
        a prefix holds more versions than it will retain -- so partial data reaches the
        sweep as an exception, never as a short list that would read as proof.
        """
        state(3)
        rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        recorded = {_key_of(row): str(row["versionId"]) for row in rows}
        _write_account(
            {"uploads": {_key_of(row): "fp" for row in rows}, "upload_versions": recorded}
        )
        drive.rows = rows

        def _boom(*a, **k):
            raise RuntimeError("listing too large to retain")

        monkeypatch.setattr(backup.storage, "list_object_versions", _boom)
        out = self._sweep(drive, newest=_key_of(rows[-1]))
        assert out["skipped"]
        assert self._recorded() == recorded
        assert drive.deleted == []

    def test_a_listing_the_sweep_refused_to_act_on_prunes_nothing(self, drive, state):
        # The other untrusted-listing shape: complete, but not showing the archive this
        # run just uploaded, so it cannot be trusted about what else it omitted.
        state(3)
        rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        drive.rows = rows
        absent = f"snapshots/{INSTALL}/kirocrew-snapshot-09.tar.gz"
        recorded = {**{_key_of(row): str(row["versionId"]) for row in rows}, absent: "v-absent"}
        _write_account(
            {"uploads": {_key_of(row): "fp" for row in rows}, "upload_versions": recorded}
        )
        out = self._sweep(drive, newest=absent)
        assert out["skipped"]
        assert self._recorded() == recorded

    def test_a_record_for_another_kind_is_never_pruned(self, drive, state):
        # A snapshot listing is evidence about the snapshot folder only.
        state(3)
        rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        drive.rows = rows
        sessions_key = f"sessions/{INSTALL}/kirocrew-sessions-01.tar.gz"
        _write_account(
            {
                "uploads": {_key_of(row): "fp" for row in rows},
                "upload_versions": {
                    **{_key_of(row): str(row["versionId"]) for row in rows},
                    sessions_key: "v-sessions",
                },
            }
        )
        self._sweep(drive)
        assert sessions_key in self._recorded()

    def test_a_record_for_another_install_is_never_pruned(self, drive, state):
        state(3)
        rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        drive.rows = rows
        other = f"snapshots/{'b' * 32}/kirocrew-snapshot-01.tar.gz"
        _write_account(
            {
                "uploads": {_key_of(row): "fp" for row in rows},
                "upload_versions": {
                    **{_key_of(row): str(row["versionId"]) for row in rows},
                    other: "v-other",
                },
            }
        )
        self._sweep(drive)
        assert other in self._recorded()

    def test_a_push_recorded_while_the_listing_ran_is_never_pruned(self, drive, state, monkeypatch):
        # A manual run racing the nightly loop is a documented case in this module, and
        # its record legitimately names an object the in-flight listing cannot show. The
        # prune is eligible only for records that existed before the listing began.
        state(3)
        rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        drive.rows = rows
        landed = f"snapshots/{INSTALL}/kirocrew-snapshot-99.tar.gz"
        _write_account(
            {
                "uploads": {_key_of(row): "fp" for row in rows},
                "upload_versions": {_key_of(row): str(row["versionId"]) for row in rows},
            },
        )
        real = backup.storage.list_object_versions

        def _list_then_push(*a, **k):
            out = real(*a, **k)
            _write_account(
                {
                    "upload_versions": {
                        **{_key_of(row): str(row["versionId"]) for row in rows},
                        landed: "v-landed",
                    }
                }
            )
            return out

        monkeypatch.setattr(backup.storage, "list_object_versions", _list_then_push)
        self._sweep(drive)
        assert landed in self._recorded()

    def test_a_corrupted_record_map_is_left_alone_rather_than_emptied(self, drive, state):
        """Driven DIRECTLY, because the sweep cannot reach this branch.

        A corrupted ``upload_versions`` means :func:`uploaded_versions` reads empty, so no
        key can pass ``_current_version_is_ours`` and the sweep refuses at its
        trusted-listing gate before the prune runs -- which the test below pins. Driving
        the sweep here therefore passed with the guard replaced by an unconditional wipe:
        it proved the gate, not the guard. So the guard is exercised through its own
        function, and what it protects is real either way -- publishing an empty map from
        here would throw away every version record on the strength of one bad read, and
        rebuilding a corrupted level is `_merge_uploads`'s decision, not this one.
        """
        state(3)
        rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        _write_account(
            {"uploads": {_key_of(row): "fp" for row in rows}, "upload_versions": "not a map"}
        )
        backup._prune_recorded_versions(
            ACCOUNT,
            backup.KIND_SNAPSHOT,
            INSTALL,
            {_key_of(row) for row in rows},
            eligible={_key_of(row) for row in rows},
        )
        assert self._recorded() == "not a map"

    def test_a_corrupted_record_map_stops_the_sweep_before_the_prune(self, drive, state):
        # The reachability claim the test above rests on, pinned rather than asserted in
        # prose: with no readable version record the listing cannot show this run's own
        # archive as ours, so the sweep refuses and never reaches the prune.
        state(3)
        rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        drive.rows = rows
        _write_account(
            {"uploads": {_key_of(row): "fp" for row in rows}, "upload_versions": "not a map"}
        )
        assert backup.uploaded_versions(ACCOUNT) == {}
        out = self._sweep(drive)
        assert out["skipped"]
        assert drive.deleted == []

    def test_the_restore_path_still_reads_only_the_fingerprint_record(self):
        # The widened set is retention's. The restore question is whether this install
        # vouches for these BYTES, which a version id does not answer.
        assert "classify_key" in (backup.retention_owned_keys.__doc__ or "")
        src = inspect.getsource(backup.classify_key)
        assert "retention_owned_keys" not in src


class TestTheBackstopSaysWhatItDropped:
    """A bound bounds every field it retains, and its overflow is COUNTED out loud.

    ``MAX_RECORDED_VERSIONS`` is the one place left that can still drop a version
    record without a listing having proved anything. What it drops is not display
    history: it is the proof that makes an archive retireable, so those archives stop
    being collectable. Silently, a truncated tail reads exactly like a population that
    never held those records -- and with retention off the sweep returns before any
    listing, so no later measurement covers them either.

    The cap is monkeypatched small rather than exercised at 5001 entries: the number is
    pinned by its own sibling test above, while what these assert is the MECHANISM, and
    a five-thousand-entry fixture would buy nothing but runtime.
    """

    def test_the_persisted_map_counts_and_names_what_it_drops(self, monkeypatch, caplog):
        monkeypatch.setattr(backup, "MAX_RECORDED_VERSIONS", 3)
        entry: dict[str, Any] = {}
        versions = {f"snapshots/{INSTALL}/k{n:02d}.tar.gz": f"v{n:02d}" for n in range(6)}
        with caplog.at_level("WARNING", logger=backup.logger.name):
            backup._merge_uploads(entry, {k: "fp" for k in versions}, versions)

        # Dropped down to the cap, oldest first.
        kept = entry["upload_versions"]
        assert len(kept) == 3
        assert sorted(kept) == sorted(list(versions)[3:])

        # The COUNT is said out loud, and it is the number actually dropped -- which is
        # only true if it was computed BEFORE the drop.
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        overflow = [m for m in warnings if "MAX_RECORDED_VERSIONS" in m]
        assert len(overflow) == 1
        assert "3" in overflow[0]
        assert "retire" in overflow[0]

    def test_the_persisted_map_is_silent_when_nothing_overflows(self, monkeypatch, caplog):
        # The healthy side, so the warning is a report of a real eviction rather than
        # noise on every upload.
        monkeypatch.setattr(backup, "MAX_RECORDED_VERSIONS", 3)
        entry: dict[str, Any] = {}
        versions = {f"snapshots/{INSTALL}/k{n:02d}.tar.gz": f"v{n:02d}" for n in range(3)}
        with caplog.at_level("WARNING", logger=backup.logger.name):
            backup._merge_uploads(entry, {k: "fp" for k in versions}, versions)
        assert len(entry["upload_versions"]) == 3
        assert [
            r.getMessage() for r in caplog.records if "MAX_RECORDED_VERSIONS" in r.getMessage()
        ] == []

    def test_the_recovery_map_counts_and_names_its_own_drops(self, monkeypatch, caplog, state):
        # The mirrored site. Named as the RECOVERY map, so a reader of the log can tell
        # the two evictions apart rather than seeing one message twice.
        monkeypatch.setattr(backup, "MAX_RECORDED_VERSIONS", 3)
        with caplog.at_level("WARNING", logger=backup.logger.name):
            for n in range(6):
                backup._remember_unpersisted(
                    ACCOUNT,
                    backup.KIND_SNAPSHOT,
                    {
                        "key": f"snapshots/{INSTALL}/held{n:02d}.tar.gz",
                        "bytes": 1,
                        "at": f"2030-01-01T00:00:{n:02d}.000000+00:00",
                        "fingerprint": f"fp{n:02d}",
                        "version": f"v{n:02d}",
                    },
                )
        held = backup._unpersisted_versions[(backup._state_key(), ACCOUNT)]
        assert len(held) == 3
        recovery = [
            r.getMessage()
            for r in caplog.records
            if "MAX_RECORDED_VERSIONS" in r.getMessage() and "recovery map" in r.getMessage()
        ]
        assert recovery
        assert "1" in recovery[0]


class TestUnrecordedObjectsAreCountedWithoutBeingClaimed:
    """The other half of the bill, counted and left strictly alone.

    ``unclaimed`` is a floor on the archives this install REMEMBERS, and it reads 0 for
    an object the sweep has no record of at all -- those are filtered out before that
    measurement is taken. So before this pair they were counted nowhere: an operator
    enabling a keep count saw a zero floor and a bill that did not fall.

    The count asserts nothing about whose they are, and nothing acts on it. Two things
    land in it and this code cannot separate them: this install's own archives whose
    records aged out under the old bound, and another writer's objects under a prefix
    that is co-writable by design.
    """

    _sweep = TestUnclaimedArchivesAreReported._sweep

    def _with_a_stranger(self, drive):
        """A listing holding one object the state file has no record of. Returns rows."""
        rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        stranger = _version(
            f"snapshots/{INSTALL}/kirocrew-snapshot-00.tar.gz", "2026-01-00T00:00:00Z"
        )
        drive.rows = [stranger, *rows]
        return rows, stranger

    def test_an_object_with_no_record_is_counted_and_left_alone(self, drive, state):
        state(1)
        rows, stranger = self._with_a_stranger(drive)
        # The control: it carries bytes, so a count that missed it would differ.
        assert int(stranger["size"]) > 0
        recorded = {_key_of(row): str(row["versionId"]) for row in rows}
        out = self._sweep(drive, rows, recorded, _key_of(rows[-1]))
        assert out["unrecorded"] == 1
        assert out["unrecordedBytes"] == int(stranger["size"])
        # Counted is not claimed: it holds no keep slot and is never deleted.
        assert _key_of(stranger) not in drive.deleted_keys

    def test_the_unclaimed_pair_still_reads_zero_for_them(self, drive, state):
        # The preserved contract, pinned from the other side: `unclaimed` is a floor on
        # the remembered set, so it must NOT absorb this count.
        state(1)
        rows, _ = self._with_a_stranger(drive)
        recorded = {_key_of(row): str(row["versionId"]) for row in rows}
        out = self._sweep(drive, rows, recorded, _key_of(rows[-1]))
        assert out["unrecorded"] == 1
        assert out["unclaimed"] == 0
        assert out["unclaimedBytes"] == 0

    def test_every_version_under_an_unrecorded_key_is_counted_because_each_is_billed(
        self, drive, state
    ):
        state(1)
        rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        stranger = _version(
            f"snapshots/{INSTALL}/kirocrew-snapshot-00.tar.gz", "2026-01-00T00:00:00Z"
        )
        older = dict(stranger)
        older["versionId"] = "v-older"
        older["latest"] = False
        drive.rows = [stranger, older, *rows]
        recorded = {_key_of(row): str(row["versionId"]) for row in rows}
        out = self._sweep(drive, rows, recorded, _key_of(rows[-1]))
        # One KEY, both versions' bytes.
        assert out["unrecorded"] == 1
        assert out["unrecordedBytes"] == int(stranger["size"]) + int(older["size"])

    def test_the_label_sidecar_is_not_counted(self, drive, state):
        # This app writes it on purpose and another install reads it. Counting it would
        # put a permanent phantom object in every operator's floor.
        state(1)
        rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        label = _version(f"snapshots/{INSTALL}/{backup.LABEL_OBJECT_NAME}", "2026-01-01T00:00:00Z")
        drive.rows = [label, *rows]
        recorded = {_key_of(row): str(row["versionId"]) for row in rows}
        out = self._sweep(drive, rows, recorded, _key_of(rows[-1]))
        assert out["unrecorded"] == 0
        assert _key_of(label) not in drive.deleted_keys

    def test_the_counts_are_served_by_the_status_reader(self, drive, state):
        state(1)
        rows, stranger = self._with_a_stranger(drive)
        recorded = {_key_of(row): str(row["versionId"]) for row in rows}
        out = self._sweep(drive, rows, recorded, _key_of(rows[-1]))
        assert out["unrecorded"] == 1
        served = backup.retention_unrecorded(ACCOUNT)
        assert served[backup.KIND_SNAPSHOT]["objects"] == out["unrecorded"]
        assert served[backup.KIND_SNAPSHOT]["bytes"] == out["unrecordedBytes"]
        assert dt.datetime.fromisoformat(served[backup.KIND_SNAPSHOT]["at"]).tzinfo is not None

    def test_the_leaf_is_named_objects_because_archives_would_be_a_claim(self, drive, state):
        # The install id in a key is a string any co-writer can type, so "archives"
        # would assert something no reader here has checked.
        state(1)
        rows, _ = self._with_a_stranger(drive)
        self._sweep(drive, rows, {_key_of(r): str(r["versionId"]) for r in rows}, _key_of(rows[-1]))
        row = backup.retention_unrecorded(ACCOUNT)[backup.KIND_SNAPSHOT]
        assert set(row) == {"objects", "bytes", "at"}

    def test_the_reader_claims_neither_ownership_nor_reclaim(self):
        # Same shape as the console-renderer disclosure beside it: the wording is the
        # contract, so it is pinned rather than left to survive the next edit by luck.
        doc = backup.retention_unrecorded.__doc__ or ""
        assert "NOT a claim of ownership" in doc
        assert "NOT a reclaim estimate" in doc
        assert "NO console renderer" in doc

    def test_the_counts_reach_the_audit_event(self, drive, state, audit):
        state(1)
        rows, stranger = self._with_a_stranger(drive)
        recorded = {_key_of(row): str(row["versionId"]) for row in rows}
        self._sweep(drive, rows, recorded, _key_of(rows[-1]))
        assert "unrecorded=1" in audit[0]["resources"]
        # Appended last, so it can never displace the count that says whether archives
        # were erased.
        assert "unclaimed=0" in audit[0]["resources"]

    def test_a_measured_zero_is_published_so_absence_means_never_measured(self, drive, state):
        state(3)
        rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        drive.rows = rows
        recorded = {_key_of(row): str(row["versionId"]) for row in rows}
        self._sweep(drive, rows, recorded, _key_of(rows[-1]))
        served = backup.retention_unrecorded(ACCOUNT)[backup.KIND_SNAPSHOT]
        assert served["objects"] == 0
        assert served["bytes"] == 0

    def test_nothing_is_served_before_any_sweep_has_measured(self, state):
        state(3)
        assert backup.retention_unrecorded(ACCOUNT) == {}

    def test_a_listing_the_sweep_refused_to_act_on_is_not_published(self, drive, state):
        # An undercount served as a floor reads as "nothing unaccounted here", which is
        # worse than serving nothing at all.
        state(1)
        rows, stranger = self._with_a_stranger(drive)
        out = self._sweep(drive, rows, {}, _key_of(rows[-1]))
        assert out["skipped"]
        assert out["unrecorded"] == 1
        assert backup.retention_unrecorded(ACCOUNT) == {}

    def test_a_corrupted_measurement_reads_as_nothing_measured(self, state):
        state(3)
        _write_account({backup.RETENTION_UNRECORDED_STATE_KEY: "not a map"})
        assert backup.retention_unrecorded(ACCOUNT) == {}
        _write_account({backup.RETENTION_UNRECORDED_STATE_KEY: {backup.KIND_SNAPSHOT: "nope"}})
        assert backup.retention_unrecorded(ACCOUNT) == {}

    def test_a_state_write_failure_does_not_fail_the_sweep(self, drive, state, monkeypatch):
        state(1)
        rows, _ = self._with_a_stranger(drive)
        recorded = {_key_of(row): str(row["versionId"]) for row in rows}

        def _boom(*a, **k):
            raise OSError("no space left on device")

        monkeypatch.setattr(backup, "write_state", _boom)
        out = self._sweep(drive, rows, recorded, _key_of(rows[-1]))
        assert out["unrecorded"] == 1
        assert drive.deleted == [(_key_of(rows[0]), str(rows[0]["versionId"]))]

    def test_a_key_the_listing_shows_only_as_a_delete_marker_is_not_counted(self, drive, state):
        # A marker is not an object: nothing is stored under that key and nothing is
        # billed, so counting it would put a phantom in the floor that no later
        # listing can remove -- the same harm the label sidecar is excluded for.
        state(1)
        rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        marker = _version(
            f"snapshots/{INSTALL}/kirocrew-snapshot-00.tar.gz",
            "2026-01-00T00:00:00Z",
            deleteMarker=True,
            size=0,
        )
        drive.rows = [marker, *rows]
        recorded = {_key_of(row): str(row["versionId"]) for row in rows}
        # The premise: it IS under our folder and the state has no record of it, so
        # the only reason not to count it is that it is a marker.
        assert _key_of(marker).startswith(f"snapshots/{INSTALL}/")
        assert _key_of(marker) not in recorded
        out = self._sweep(drive, rows, recorded, _key_of(rows[-1]))
        assert out["unrecorded"] == 0
        assert out["unrecordedBytes"] == 0

    def test_a_real_version_under_a_marker_still_counts_its_own_bytes(self, drive, state):
        # The other side, so the fix is a marker exclusion and not a key exclusion:
        # those bytes exist and are billed whatever sits on top of them.
        state(1)
        rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        stranger_key = f"snapshots/{INSTALL}/kirocrew-snapshot-00.tar.gz"
        marker = _version(
            stranger_key, "2026-01-00T00:00:00Z", "v-marker", deleteMarker=True, size=0
        )
        buried = _version(stranger_key, "2026-01-00T00:00:00Z", "v-buried", latest=False)
        drive.rows = [marker, buried, *rows]
        assert int(buried["size"]) > 0
        recorded = {_key_of(row): str(row["versionId"]) for row in rows}
        out = self._sweep(drive, rows, recorded, _key_of(rows[-1]))
        assert out["unrecorded"] == 1
        assert out["unrecordedBytes"] == int(buried["size"])

    def test_the_prune_still_keeps_a_record_whose_key_is_only_a_marker(self, drive, state):
        # The asymmetry, pinned on purpose. The count excludes a marker; the prune's
        # set does NOT, because that set decides whether a RECORD survives and its two
        # directions cost differently -- a record wrongly kept costs document space, a
        # record wrongly dropped is unrecoverable proof.
        state(1)
        rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        gone_key = f"snapshots/{INSTALL}/kirocrew-snapshot-00.tar.gz"
        marker = _version(gone_key, "2026-01-00T00:00:00Z", deleteMarker=True, size=0)
        drive.rows = [marker, *rows]
        recorded = {_key_of(row): str(row["versionId"]) for row in rows}
        recorded[gone_key] = "v-gone"
        self._sweep(drive, rows, recorded, _key_of(rows[-1]))
        doc = json.loads(backup._state_path().read_text(encoding="utf-8"))
        # `.get` so a pruned record fails as an assertion about the value rather than
        # as a KeyError, which would read like a crash instead of a verdict.
        assert doc["accounts"][ACCOUNT].get("upload_versions", {}).get(gone_key) == "v-gone"


class TestSetRetentionKeep:
    """The writer, and the round trip that makes the feature reachable at all.

    A switch nothing can set removes the harm for nobody, so the end-to-end path
    matters more than the reader in isolation: write a count, and the very next sweep
    must prune to it; clear it, and the next sweep must keep everything again.
    """

    def test_writing_a_count_turns_the_next_sweep_on(self, drive, state, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03", "04"])
        # Off first, asserted rather than assumed, so the change below is the cause.
        assert _prune(drive)["keep"] == "off"
        assert drive.deleted == []
        backup.set_retention_keep(ACCOUNT, 2)
        result = _prune(drive)
        assert result["keep"] == 2
        assert result["retired"] == 2

    def test_clearing_it_turns_the_next_sweep_back_off(self, drive, state, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        backup.set_retention_keep(ACCOUNT, 2)
        backup.set_retention_keep(ACCOUNT, None)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03", "04"])
        result = _prune(drive)
        assert result["keep"] == "off"
        assert drive.deleted == []

    def test_clearing_removes_the_key_rather_than_storing_a_sentinel(
        self, state, tmp_path, monkeypatch
    ):
        # One representation of off, so nothing downstream has to know a second one.
        path = tmp_path / "backup.json"
        monkeypatch.setattr(backup, "_state_path", lambda: path)
        backup.set_retention_keep(ACCOUNT, 3)
        backup.set_retention_keep(ACCOUNT, None)
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert backup.RETENTION_KEEP_STATE_KEY not in doc["accounts"][ACCOUNT]

    def test_it_does_not_touch_the_nightly_grant(self, state, tmp_path, monkeypatch):
        path = tmp_path / "backup.json"
        monkeypatch.setattr(backup, "_state_path", lambda: path)
        backup.set_nightly(ACCOUNT, True)
        backup.set_retention_keep(ACCOUNT, 4)
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["accounts"][ACCOUNT]["nightly"] is True
        assert doc["accounts"][ACCOUNT][backup.RETENTION_KEEP_STATE_KEY] == 4

    def test_true_is_refused_at_the_write_as_well_as_the_read(self, state, tmp_path, monkeypatch):
        # The route validates too, but this key's whole safety rests on the bool screen,
        # so a direct caller must not be able to store keep=1 by sending a flag.
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        with pytest.raises(ValueError):
            backup.set_retention_keep(ACCOUNT, True)

    def test_a_non_integer_is_refused(self, state, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        with pytest.raises(ValueError):
            backup.set_retention_keep(ACCOUNT, "3")

    def test_an_out_of_range_count_is_refused_rather_than_clamped(
        self, state, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        with pytest.raises(ValueError):
            backup.set_retention_keep(ACCOUNT, 0)
        with pytest.raises(ValueError):
            backup.set_retention_keep(ACCOUNT, -1)
        # No upper bound: a count larger than the archives that exist keeps all of
        # them, so refusing it would refuse an operator over nothing.
        backup.set_retention_keep(ACCOUNT, 10_000)
        assert backup.retention_keep(ACCOUNT) == 10_000

    def test_the_public_reader_reports_the_effective_count(self, state, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        assert backup.retention_keep(ACCOUNT) is None
        backup.set_retention_keep(ACCOUNT, 6)
        assert backup.retention_keep(ACCOUNT) == 6


class TestNonUtf8StateIsUnreadableNotAnException:
    """Bytes that are not UTF-8 are the "exists but could not be read" case.

    `read_text(encoding="utf-8")` raises `UnicodeDecodeError`, which is a `ValueError`
    and NOT an `OSError`, so a reader catching only `OSError` lets it escape. That
    matters most at the sweep, which resolves its count before entering its own
    best-effort handler and whose call site promises retention cannot fail the run: an
    escaping decode error would report a backup already off-host as failed.

    Both readers treat it as unreadable, and neither overwrites the file. The sweep
    answers `({}, False)` and keeps every archive; the read-modify-write abandons the
    mutation. Repair-on-write covers a document that DECODED and then failed to parse,
    which these bytes never did.
    """

    def _corrupt(self, tmp_path, monkeypatch):
        path = tmp_path / "backup.json"
        monkeypatch.setattr(backup, "_state_path", lambda: path)
        # Invalid UTF-8 rather than merely invalid JSON: 0x80 is a continuation byte
        # with no lead byte, so the decode fails before any parse is attempted.
        path.write_bytes(b'{"accounts": {"x": "\x80\x81"}}')
        return path

    def test_the_checked_reader_answers_unreadable(self, tmp_path, monkeypatch):
        self._corrupt(tmp_path, monkeypatch)
        assert backup._read_state_checked() == ({}, False)

    def test_the_sweep_keeps_everything_instead_of_raising(self, drive, tmp_path, monkeypatch):
        self._corrupt(tmp_path, monkeypatch)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
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
        # Returning at all is half the assertion: the defect was an exception escaping.
        assert result["keep"] == "off"
        assert result["skipped"] == "the retention setting could not be read"
        assert drive.deleted == []

    def test_the_sweep_still_audits_the_unreadable_state_as_a_failure(
        self, drive, tmp_path, monkeypatch, audit
    ):
        # The escaping exception skipped this branch entirely, so an operator whose
        # configured count stopped being readable got no audit entry at all.
        self._corrupt(tmp_path, monkeypatch)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02"])
        backup._prune_remote_archives(
            ACCOUNT,
            PROFILE,
            REGION,
            BUCKET,
            backup.KIND_SNAPSHOT,
            INSTALL,
            _key_of(drive.rows[-1]),
            caller=backup.CALLER_SCHEDULED,
        )
        assert [entry["outcome"] for entry in audit] == ["failed"]
        assert "could not be read" in audit[0]["error"]

    def test_the_write_path_abandons_the_mutation_and_leaves_the_bytes_alone(
        self, tmp_path, monkeypatch
    ):
        # Repair-on-write is for a document that PARSED to nothing usable. These bytes
        # never reached the parser, so publishing over them would replace every
        # account's toggles, retention count and run history -- including the
        # `upload_versions` records the sweep's ownership test reads -- on the strength
        # of a document nobody read.
        path = self._corrupt(tmp_path, monkeypatch)
        before = path.read_bytes()
        with pytest.raises(OSError) as caught:
            backup.set_retention_keep(ACCOUNT, 3)
        assert isinstance(caught.value, backup._StateUnreadable)
        assert path.read_bytes() == before

    def test_the_abandoning_error_stays_an_oserror_for_the_existing_handler(
        self, tmp_path, monkeypatch
    ):
        # `set_nightly` lets this reach its own handler, which is written against
        # `OSError`, so the decode case must not arrive as a bare `ValueError`.
        self._corrupt(tmp_path, monkeypatch)
        with pytest.raises(OSError):
            backup.set_nightly(ACCOUNT, True)

    def test_a_document_that_decoded_but_did_not_parse_is_still_repaired(
        self, tmp_path, monkeypatch
    ):
        # The other side of the split, so the change above does not quietly widen into
        # abandoning every corrupt file: this one decoded, so it is the corrupt kind.
        path = tmp_path / "backup.json"
        monkeypatch.setattr(backup, "_state_path", lambda: path)
        path.write_text("{not json at all", encoding="utf-8")
        backup.set_retention_keep(ACCOUNT, 3)
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["accounts"][ACCOUNT][backup.RETENTION_KEEP_STATE_KEY] == 3

    def test_a_decode_error_is_not_swallowed_as_a_bare_value_error(self, tmp_path, monkeypatch):
        # The catch names UnicodeDecodeError, not ValueError, so a surprising
        # ValueError from the read stays loud instead of reading as "unreadable".
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        (tmp_path / "backup.json").write_text("{}", encoding="utf-8")

        def _boom(*_a, **_k):
            raise ValueError("not a decode problem")

        monkeypatch.setattr(Path, "read_text", _boom)
        with pytest.raises(ValueError):
            backup._read_state_checked()


class TestTheStatusFieldDoesNotClaimAConsumerItDoesNotHave:
    """`retentionKeep` is readable over HTTP and nothing renders it.

    The count is set and read by raw request only. A docstring promising a console
    control sends the next reader looking for a panel that is not there, and makes the
    shipped surface sound larger than it is.
    """

    def test_the_reader_does_not_claim_a_console_renderer(self):
        doc = backup.retention_keep.__doc__ or ""
        assert "console renders" not in doc
        # The absence is stated rather than merely unclaimed, so the gap is disclosed
        # where someone deciding whether to build the panel will find it.
        assert "NO\n    console renderer ships" in doc or "NO console renderer" in doc

    def test_the_count_survives_a_round_trip_without_any_frontend(self, tmp_path, monkeypatch):
        # Why the field stays despite having no renderer: without it the route is
        # write-only and an operator cannot confirm what was stored.
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        backup.set_retention_keep(ACCOUNT, 4)
        assert backup.retention_keep(ACCOUNT) == 4
        backup.set_retention_keep(ACCOUNT, None)
        assert backup.retention_keep(ACCOUNT) is None


class TestARetentionChangeDuringTheListingStopsTheDelete:
    """The count is re-read at the delete, not trusted from before the listing.

    `list_object_versions` is a network round trip after a build that may have taken
    minutes. An owner who switches retention off inside that window has chosen to keep
    these versions, and the deletion is permanent, so a count read before the listing
    cannot authorize a delete that happens after it. The consent gate beside it already
    works this way.
    """

    def _flip_during_listing(self, new_value):
        """Return a patched lister that changes the stored count as it serves rows."""
        original = backup.storage.list_object_versions

        def flipping(*args, **kwargs):
            rows = original(*args, **kwargs)
            backup.set_retention_keep(ACCOUNT, new_value)
            return rows

        return flipping

    def test_switching_retention_off_mid_sweep_deletes_nothing(
        self, drive, state, monkeypatch, audit
    ):
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        monkeypatch.setattr(backup.storage, "list_object_versions", self._flip_during_listing(None))
        result = _prune(drive)
        assert drive.deleted == []
        assert result["retired"] == 0
        # An owner changing their mind is not a failure of the sweep.
        assert [entry["outcome"] for entry in audit] == ["successful"]

    def test_raising_the_count_mid_sweep_deletes_nothing(self, drive, state, monkeypatch):
        # keep=1 selected the two older archives; keep=3 protects them, so the
        # candidate set computed under the old count is stale.
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        monkeypatch.setattr(backup.storage, "list_object_versions", self._flip_during_listing(3))
        result = _prune(drive)
        assert drive.deleted == []
        assert "changed before deletion" in result["skipped"]

    def test_lowering_the_count_mid_sweep_still_deletes_the_selected_set(
        self, drive, state, monkeypatch
    ):
        # The other direction is safe and must NOT refuse: keep=1 authorizes everything
        # keep=2 selected, so refusing here would be a sweep that never runs whenever
        # the setting happens to be touched.
        state(2)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        monkeypatch.setattr(backup.storage, "list_object_versions", self._flip_during_listing(1))
        result = _prune(drive)
        assert drive.deleted != []
        assert result["retired"] >= 1

    def test_the_delete_is_not_serialized_behind_the_run_lock(self):
        # The fix must not be "hold `_run_lock` through the purge": that lock also
        # serializes `last_runs`, so a status read would stall for the length of a
        # network deletion. Matching the ACQUISITION rather than the bare name, which
        # the comment explaining this decision also contains.
        source = inspect.getsource(backup._prune_remote_archives)
        assert "with _run_lock" not in source


class TestTheWriterAndTheSweepShareOneGate:
    """A lock only one side takes protects nothing.

    The sweep's final count check and its delete are one critical section. That is
    worth nothing unless `set_retention_keep` takes the same lock, so this pins the
    writer's half by BEHAVIOUR -- it must not be able to complete while the gate is
    held -- rather than by reading the source for a lock name.
    """

    def test_setting_the_count_waits_for_the_gate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        finished = threading.Event()

        def write():
            backup.set_retention_keep(ACCOUNT, 3)
            finished.set()

        with backup._RETENTION_GATE:
            worker = threading.Thread(target=write, daemon=True)
            worker.start()
            # Held: the writer must be parked, not racing ahead of a sweep that has
            # already checked the count and is about to call S3.
            assert not finished.wait(timeout=0.5)
        # Released: it proceeds, so the wait above was the lock and not a dead thread.
        assert finished.wait(timeout=5)
        worker.join(timeout=5)
        assert backup.retention_keep(ACCOUNT) == 3

    def test_the_gate_is_not_the_run_lock(self):
        # `_run_lock` also serializes `last_runs`, so sharing it would stall a status
        # read for the length of a purge. They must be different objects.
        assert backup._RETENTION_GATE is not backup._run_lock

    def test_consent_is_rechecked_with_the_gate_already_held(self, drive, state, monkeypatch):
        # Acquiring these locks can wait on another purge, so a consent check taken
        # before the wait can be stale by the time S3 is called. Asserting the gate is
        # ALREADY held when authorization runs is what shows the check moved inside
        # rather than merely being called twice.
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        held: list[bool] = []
        real = backup._authorize_upload

        def recording(*args, **kwargs):
            held.append(backup._RETENTION_GATE.locked())
            return real(*args, **kwargs)

        monkeypatch.setattr(backup, "_authorize_upload", recording)
        _prune(drive)
        # The sweep authorizes before the listing too, so the LAST one is the one that
        # must be inside: that is the one immediately before the delete. Asserting the
        # FIRST one is OUTSIDE is what makes this non-vacuous -- the two calls differ in
        # lock state within a single run, so `True` is not something every call returns.
        assert held[0] is False
        assert held[-1] is True
        assert len(held) >= 2
        assert drive.deleted != []

    def test_the_delete_takes_the_same_sidecar_lock_the_state_writes_take(
        self, drive, state, monkeypatch
    ):
        # A `threading.Lock` orders threads and nothing else. This module documents a
        # second install sharing one bucket and one state file by design, so ordering
        # against another PROCESS needs the state file's own sidecar lock -- the same
        # path `_locked_state_update` takes. Asserting the path is what shows the two
        # are the same lock rather than two locks that merely both exist.
        state(1)
        drive.rows = _archives(backup.KIND_SNAPSHOT, ["01", "02", "03"])
        opened: list[str] = []
        real = backup.open_lock_file

        def recording(path, *args, **kwargs):
            opened.append(str(path))
            return real(path, *args, **kwargs)

        monkeypatch.setattr(backup, "open_lock_file", recording)
        _prune(drive)
        expected = str(backup._state_path().with_suffix(".lock"))
        assert expected in opened
        # And it is held for the delete, not merely opened somewhere in the sweep.
        assert drive.deleted != []

    def test_one_call_writes_the_state_exactly_once(self, tmp_path, monkeypatch):
        # A SECOND write outside the gate is not merely redundant: with a concurrent
        # clear it can land afterwards and restore the count, turning retention back on
        # so a later push deletes archives the operator had just stopped protecting.
        # Counting the writes is what catches that; asserting the final value does not,
        # because both writes store the same thing when nothing races them.
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        real = backup._locked_state_update
        calls = []

        def counting(mutate):
            calls.append(mutate)
            return real(mutate)

        monkeypatch.setattr(backup, "_locked_state_update", counting)
        backup.set_retention_keep(ACCOUNT, 3)
        assert len(calls) == 1

    def test_clearing_also_writes_exactly_once(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        backup.set_retention_keep(ACCOUNT, 3)
        real = backup._locked_state_update
        calls = []

        def counting(mutate):
            calls.append(mutate)
            return real(mutate)

        monkeypatch.setattr(backup, "_locked_state_update", counting)
        backup.set_retention_keep(ACCOUNT, None)
        assert len(calls) == 1
        assert backup.retention_keep(ACCOUNT) is None
