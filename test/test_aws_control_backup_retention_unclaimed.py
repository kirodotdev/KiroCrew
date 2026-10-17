"""Cloud backup retention: what the sweep counts but can never retire.

The unclaimed-archive count and bytes on the audit event and the status read, the
version records that outlive the panel history, the bounded backstop that says what
it dropped, and the unrecorded objects counted without being claimed.

Part of the retention suite; ``test_aws_control_backup_retention.py`` says which AWS layer
each kind of test fakes, and no test reaches a network, a profile or a bucket.
"""

from __future__ import annotations

import datetime as dt
import inspect
import json
from typing import Any

import aws_control_backup_retention_helpers
from aws_control_backup_retention_helpers import (
    ACCOUNT,
    BUCKET,
    INSTALL,
    PROFILE,
    REGION,
    _archives,
    _key_of,
    _version,
    _write_account,
)

from kiro_crew.apps.builtins.aws_control.backend import backup

# The shared fixtures, bound here by name so pytest registers them for this module.
audit = aws_control_backup_retention_helpers.audit
drive = aws_control_backup_retention_helpers.drive
state = aws_control_backup_retention_helpers.state


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
