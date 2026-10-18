"""Tests for how the ``aws_control`` backup uses its own upload records.

``TestRecordedVersionRecovery`` covers ``backup.restore_download`` making one
pinned read of the version this install recorded when the current object fails
its fingerprint, and accepting those bytes when the re-taken fingerprint matches.
It also covers refusals, what is left staged, and cases that skip the pinned
read, including a failing consent, app or identity check, a missing, ``null`` or
option-shaped version id, a record with no fingerprint, and ``foreign_ok``.

``TestRememberedArchives`` covers ``backup.remembered_archives``, the per-kind
count of upload records, including where it reads lower or higher than the drive.
"""

from __future__ import annotations

import errno
import hashlib
import io
import json
import tarfile
from pathlib import Path
from unittest import mock

import pytest
from aws_control_backup_helpers import (
    ACCOUNT,
    ARCHIVE_BYTES,
    ARCHIVE_FINGERPRINT,
)

from kiro_crew.apps.builtins.aws_control.backend import backup


def _tar_gz(payload: bytes = b"restored") -> bytes:
    """A real ``tar.gz``, so these tests read as the restore they describe.

    ``ARCHIVE_BYTES`` is deliberately not one -- the tests that use it are about the
    fingerprint, which does not care what the bytes are, and neither does the
    recovery: the re-taken fingerprint is its whole test. Building a genuine
    archive here rather than committing a binary keeps what is being asserted
    visible, and makes the contrast with the malformed-archive case explicit.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="crew/x.txt")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


RECOVERED_BYTES = _tar_gz()
# nosemgrep: python.lang.security.insecure-hash-algorithms-md5.insecure-hash-algorithm-md5 -- a fixture fingerprint in the upload record's MD5 shape and not a security digest
RECOVERED_FINGERPRINT = hashlib.md5(RECOVERED_BYTES).hexdigest()


class TestRecordedVersionRecovery:
    """An overwrite at a recorded key does not hide this install's own archive.

    One drive is reachable by every install pointed at the account, and versioning
    is on for exactly that reason: when a co-writer overwrites a key this install
    recorded, our bytes stay on the drive as a noncurrent version. A read that names
    no version fetches whatever is current, so it fails the fingerprint and leaves
    our archive present but unnamed.

    The recovery makes ONE more read, of the version this install recorded writing,
    and accepts it only on the same evidence the current-version read uses: the same
    fingerprint, re-taken over the bytes that arrive on that read. Every other
    outcome is the refusal the operator already gets, so nothing here can widen what
    a restore accepts.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse state pin moved verbatim; no test in this module calls monkeypatch.undo()
    def _isolated_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        monkeypatch.setattr(
            "kiro_crew.apps.builtins.aws_control.backend.backup.app_data_dir",
            lambda name: tmp_path / "appdata",
        )
        # The recovery authorizes its extra read immediately before making it, with
        # the same four questions the paid upload is gated on, so these tests have to
        # stand up the precondition the route establishes in production -- without it
        # every recovery here declines and the class would assert the gate rather than
        # the mechanism. Each gate test below revokes exactly one of the four, which
        # is what keeps them pinned individually rather than papered over.
        monkeypatch.setattr(
            "kiro_crew.deploy.engine._checked",
            lambda args, profile, *, action="", timeout=30, extra_visible_dirs=(): (
                '{"Account": "%s"}' % ACCOUNT
            ),
        )
        monkeypatch.setattr("kiro_crew.apps.manager.is_app_enabled", lambda name: True)
        monkeypatch.setattr(
            "kiro_crew.aws_consent.read_grant",
            lambda service: mock.Mock(profile="p", region="us-west-2", account=ACCOUNT),
        )
        backup._unpersisted_runs.clear()
        yield
        backup._unpersisted_runs.clear()

    def _recorded(self, fingerprint, version=""):
        mine = backup.install_identity()["id"]
        key = f"snapshots/{mine}/a.tar.gz"
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, key, 10, fingerprint, version)
        return key

    def _download(self, key, *, current, by_version=None, raises=None, **kwargs):
        """Serve ``current`` for an unpinned read, ``by_version`` for a pinned one.

        Returns the version ids the primitive was asked for, in order, so a test can
        assert BOTH that recovery happened and that it happened exactly once -- and
        that a read it must not make was never made.
        """
        asked = []

        def fake_get(
            profile, region, bucket, section, k, dest, *, account, version="", timeout=600
        ):
            asked.append(version)
            if not version:
                Path(dest).write_bytes(current)
                return
            if raises is not None:
                raise raises
            if by_version is None:
                # Reached only when a test that forbids a pinned read got one. Named
                # rather than left to fail on the write, so the red says what broke
                # instead of surfacing as a TypeError from this fake.
                raise AssertionError(
                    f"a pinned read was made for version {version!r}, but this test "
                    "expects the recorded version never to be reached"
                )
            Path(dest).write_bytes(by_version)

        with mock.patch.object(backup.storage, "get_file", side_effect=fake_get):
            try:
                result = backup.restore_download(
                    "p", "us-west-2", "bkt", key, account=ACCOUNT, **kwargs
                )
            except backup.UnprovenArchive as exc:
                return exc, asked
        return result, asked

    def test_a_matching_current_version_is_served_without_a_second_read(self):
        # The unchanged path. A recorded version exists, and precisely because the
        # current object matches, nothing reaches for it: recovery is reached only
        # through the fingerprint failure, so a healthy restore costs one request
        # exactly as before.
        key = self._recorded(ARCHIVE_FINGERPRINT, "v-current")
        result, asked = self._download(key, current=ARCHIVE_BYTES)
        assert result["origin"] == backup.ORIGIN_SELF
        assert asked == [""]

    def test_an_overwrite_falls_back_to_the_version_this_install_recorded(self, tmp_path):
        key = self._recorded(RECOVERED_FINGERPRINT, "v-ours")
        result, asked = self._download(
            key, current=b"somebody elses archive", by_version=RECOVERED_BYTES
        )
        assert result["origin"] == backup.ORIGIN_SELF
        # Exactly one extra read, pinned to the recorded id and nothing else.
        assert asked == ["", "v-ours"]
        # The bytes handed back are the RECOVERED ones, not the overwrite.
        assert Path(result["path"]).read_bytes() == RECOVERED_BYTES
        # And the reported length describes them. Measured on the first read, this
        # would report the overwriting object's size.
        assert result["bytes"] == len(RECOVERED_BYTES)

    def test_a_recovered_restore_says_so_in_its_reply(self):
        # The overwrite is the operator's to know about: it outlives this restore,
        # and the next upload to the key can be overwritten the same way. The reply
        # is the only surface the dashboard reads, so it carries the fact.
        key = self._recorded(RECOVERED_FINGERPRINT, "v-ours")
        result, _asked = self._download(
            key, current=b"somebody elses archive", by_version=RECOVERED_BYTES
        )
        assert result["recovered"] is True

    def test_a_restore_that_needed_no_recovery_does_not_claim_one(self):
        key = self._recorded(ARCHIVE_FINGERPRINT, "v-current")
        result, asked = self._download(key, current=ARCHIVE_BYTES)
        assert asked == [""]
        assert result["recovered"] is False

    def test_an_overridden_overwrite_is_not_reported_as_recovered(self):
        # Under the override the current bytes are handed back unverified, so no
        # recorded version was fetched and the reply must not say one was.
        key = self._recorded(RECOVERED_FINGERPRINT, "v-ours")
        result, asked = self._download(key, current=b"somebody elses archive", foreign_ok=True)
        assert asked == [""]
        assert result["recovered"] is False

    def test_a_recorded_version_that_is_gone_returns_the_existing_refusal(self):
        key = self._recorded(RECOVERED_FINGERPRINT, "v-ours")
        exc, asked = self._download(
            key,
            current=b"somebody elses archive",
            raises=backup.AWSError("An error occurred (NoSuchVersion)"),
        )
        assert isinstance(exc, backup.UnprovenArchive)
        assert exc.origin == backup.ORIGIN_UNVERIFIED
        assert asked == ["", "v-ours"]

    def test_a_deleted_recorded_version_is_not_retried_past_the_one_read(self):
        # A delete marker or an expired version answers the same way, and the point
        # is that it stops there: no walk of the version list, no second attempt.
        key = self._recorded(RECOVERED_FINGERPRINT, "v-ours")
        exc, asked = self._download(key, current=b"foreign", raises=OSError(errno.EIO, "gone"))
        assert isinstance(exc, backup.UnprovenArchive)
        assert asked.count("v-ours") == 1

    def test_a_recorded_version_whose_bytes_do_not_match_is_refused(self):
        # Not a failed recovery -- a second set of foreign bytes, discarded exactly
        # like the first. This is the branch that would let foreign bytes through if
        # the fingerprint were not re-taken on the pinned read.
        key = self._recorded(RECOVERED_FINGERPRINT, "v-ours")
        exc, asked = self._download(key, current=b"foreign one", by_version=_tar_gz(b"foreign two"))
        assert isinstance(exc, backup.UnprovenArchive)
        assert exc.origin == backup.ORIGIN_UNVERIFIED
        assert asked == ["", "v-ours"]

    def test_a_staged_copy_that_cannot_be_hashed_is_refused_not_raised(self, tmp_path):
        # The fingerprint is READ from the staged file, so it fails for the same
        # reasons the transfer does. Outside the guard it escapes this helper, which
        # the route answers with a 500 where the contract promises the existing
        # refusal -- and it leaves behind the file this function staged, because the
        # caller's cleanup owns only its own temp file.
        key = self._recorded(RECOVERED_FINGERPRINT, "v-ours")
        real = backup._body_fingerprint
        seen = []

        def flaky(path):
            seen.append(path)
            # The FIRST call hashes the current object, and it must still work: the
            # mismatch it reports is what reaches the recovery at all.
            if len(seen) == 1:
                return real(path)
            raise OSError(errno.EIO, "staged copy unreadable")

        with mock.patch.object(backup, "_body_fingerprint", flaky):
            exc, asked = self._download(
                key, current=b"somebody elses archive", by_version=RECOVERED_BYTES
            )
        assert isinstance(exc, backup.UnprovenArchive)
        assert exc.origin == backup.ORIGIN_UNVERIFIED
        # The pinned read DID happen, so this is the hash failing, not the transfer.
        assert asked == ["", "v-ours"]
        staging = tmp_path / "appdata" / "restore"
        assert list(staging.glob("*")) == []

    def test_the_recovery_does_not_run_when_the_grant_names_another_profile(self):
        # The extra read is the one AWS call in a restore the caller did not ask for,
        # and the first read can take minutes -- long enough for the owner to re-confirm
        # S3 for a different credential source while it is in flight. The route's
        # pre-flight ran before that decision existed, so it cannot speak for it, and
        # the grant's profile and region are checked against the same snapshot its
        # account is.
        key = self._recorded(RECOVERED_FINGERPRINT, "v-ours")
        with mock.patch(
            "kiro_crew.aws_consent.read_grant",
            return_value=mock.Mock(profile="other", region="us-west-2", account=ACCOUNT),
        ):
            exc, asked = self._download(
                key, current=b"somebody elses archive", by_version=RECOVERED_BYTES
            )
        assert isinstance(exc, backup.UnprovenArchive)
        assert exc.origin == backup.ORIGIN_UNVERIFIED
        # `by_version` was served, so a read would have succeeded and been visible:
        # what is asserted is that it was never made.
        assert asked == [""]

    def test_the_recovery_does_not_run_once_the_app_is_disabled(self):
        # The other half of the same window. A disabled app is the owner switching the
        # whole surface off, which must stop a read the restore is spending on their
        # behalf just as a withdrawn grant does.
        key = self._recorded(RECOVERED_FINGERPRINT, "v-ours")
        with mock.patch("kiro_crew.apps.manager.is_app_enabled", return_value=False):
            exc, asked = self._download(
                key, current=b"somebody elses archive", by_version=RECOVERED_BYTES
            )
        assert isinstance(exc, backup.UnprovenArchive)
        assert asked == [""]

    def test_the_recovery_does_not_run_when_the_profile_points_at_another_account(self):
        # `is_granted` matches profile and region and deliberately NOT the account, so
        # a profile repointed during the first download would otherwise reach AWS
        # under a consent the owner never gave for THIS account. The live probe is
        # what closes that, and it runs before the read rather than after it.
        key = self._recorded(RECOVERED_FINGERPRINT, "v-ours")
        with mock.patch(
            "kiro_crew.deploy.engine._checked", return_value='{"Account": "999988887777"}'
        ):
            exc, asked = self._download(
                key, current=b"somebody elses archive", by_version=RECOVERED_BYTES
            )
        assert isinstance(exc, backup.UnprovenArchive)
        assert exc.origin == backup.ORIGIN_UNVERIFIED
        assert asked == [""]

    def test_the_recovery_does_not_run_when_the_grant_names_another_account(self):
        # The live connection can point at the right account while the RECORDED grant
        # belongs to a different one configured under the same profile name. That is a
        # separate question from whether any S3 consent exists, which is why the grant
        # is read and compared rather than trusted because `is_granted` said yes.
        key = self._recorded(RECOVERED_FINGERPRINT, "v-ours")
        with mock.patch(
            "kiro_crew.aws_consent.read_grant",
            return_value=mock.Mock(profile="p", region="us-west-2", account="999988887777"),
        ):
            exc, asked = self._download(
                key, current=b"somebody elses archive", by_version=RECOVERED_BYTES
            )
        assert isinstance(exc, backup.UnprovenArchive)
        assert asked == [""]

    def test_the_grant_is_read_exactly_once_for_the_whole_check(self):
        # The two-snapshot race, pinned. Profile, region and account are all checked
        # against ONE read: grant reads are unlocked while writes take the consent
        # lock, so a second read can return a different record and let each half of the
        # check pass against a different one, turning a refusal into an allow.
        key = self._recorded(RECOVERED_FINGERPRINT, "v-ours")
        reads = []

        def counting(service):
            reads.append(service)
            return mock.Mock(profile="p", region="us-west-2", account=ACCOUNT)

        with mock.patch("kiro_crew.aws_consent.read_grant", counting):
            result, asked = self._download(
                key, current=b"somebody elses archive", by_version=RECOVERED_BYTES
            )
        assert result["origin"] == backup.ORIGIN_SELF
        assert asked == ["", "v-ours"]
        assert len(reads) == 1

    def test_the_recovery_does_not_run_when_the_grant_is_gone(self):
        # A grant that cannot be read names no account, so it cannot be verified
        # against anything and is refused for the same reason a mismatched one is.
        key = self._recorded(RECOVERED_FINGERPRINT, "v-ours")
        with mock.patch("kiro_crew.aws_consent.read_grant", return_value=None):
            exc, asked = self._download(
                key, current=b"somebody elses archive", by_version=RECOVERED_BYTES
            )
        assert isinstance(exc, backup.UnprovenArchive)
        assert asked == [""]

    def test_an_unanswerable_identity_probe_is_refused_not_raised(self):
        # The probe is itself an AWS call and fails on its own terms. Allowed to
        # raise, it would escape as a 500 where the contract promises the refusal this
        # caller already had.
        key = self._recorded(RECOVERED_FINGERPRINT, "v-ours")
        with mock.patch(
            "kiro_crew.deploy.engine._checked",
            side_effect=backup.AWSError("sts:GetCallerIdentity failed"),
        ):
            exc, asked = self._download(
                key, current=b"somebody elses archive", by_version=RECOVERED_BYTES
            )
        assert isinstance(exc, backup.UnprovenArchive)
        assert asked == [""]

    def test_a_recorded_version_that_is_not_a_readable_archive_is_still_returned(self):
        # A fingerprint match settles it, and nothing else is asked of the bytes.
        # These ARE the bytes this install uploaded, and the upload side pushes
        # payloads it cannot read, so an own archive can legitimately be malformed.
        # The current-version read hands such an archive back; refusing it only here
        # would give the operator their own file when nobody overwrote the key and a
        # refusal when somebody did, for identical bytes.
        malformed = b"not a gzip stream at all"
        # nosemgrep: python.lang.security.insecure-hash-algorithms-md5.insecure-hash-algorithm-md5 -- the recorded fingerprint in the upload record's MD5 shape and not a security digest
        key = self._recorded(hashlib.md5(malformed).hexdigest(), "v-ours")
        result, asked = self._download(key, current=b"foreign", by_version=malformed)
        assert result["origin"] == backup.ORIGIN_SELF
        assert Path(result["path"]).read_bytes() == malformed
        assert asked == ["", "v-ours"]

    def test_no_recorded_version_means_no_second_read_at_all(self):
        # The pre-existing behaviour, unchanged: an unversioned bucket, or a run
        # recorded before versions were, has nothing to recover from and must not
        # spend a request discovering that.
        key = self._recorded(RECOVERED_FINGERPRINT, "")
        exc, asked = self._download(key, current=b"somebody elses archive")
        assert isinstance(exc, backup.UnprovenArchive)
        assert exc.origin == backup.ORIGIN_UNVERIFIED
        assert asked == [""]

    def test_a_suspended_versioning_null_id_is_not_treated_as_a_version(self):
        # S3 gives "null" to every object written while versioning is SUSPENDED, and
        # an overwrite there REPLACES that version. So two different bodies at one
        # key both report "null" and the id cannot name one of them -- reaching for
        # it would fetch the overwrite and call it recovered.
        key = self._recorded(RECOVERED_FINGERPRINT, "null")
        exc, asked = self._download(key, current=b"somebody elses archive")
        assert isinstance(exc, backup.UnprovenArchive)
        assert asked == [""]

    def test_a_record_with_no_fingerprint_recovers_nothing(self):
        # Unknown is not a pass, and it stays not a pass: with no fingerprint there
        # is no evidence a recovered version could be checked against, so recovery
        # must not run at all rather than run and compare against "".
        key = self._recorded("", "v-ours")
        exc, asked = self._download(key, current=ARCHIVE_BYTES)
        assert isinstance(exc, backup.UnprovenArchive)
        assert asked == [""]

    def test_a_version_id_shaped_like_a_cli_option_never_reaches_the_read(self):
        # The id travels as its own argv element after `--version-id`. There is no
        # shell, so this is not about metacharacters -- it is the AWS CLI's own
        # option grammar: a leading `-` starts another option, so a stored
        # `--profile` would silently repoint the call. It is refused locally and the
        # read is never attempted.
        key = self._recorded(RECOVERED_FINGERPRINT, "--profile=evil")
        exc, asked = self._download(key, current=b"somebody elses archive")
        assert isinstance(exc, backup.UnprovenArchive)
        assert exc.origin == backup.ORIGIN_UNVERIFIED
        assert asked == [""]

    def test_a_refused_recovery_leaves_nothing_staged(self, tmp_path):
        # Two temp files exist during a recovery attempt, so a refusal has two
        # chances to leave one behind.
        key = self._recorded(RECOVERED_FINGERPRINT, "v-ours")
        exc, _asked = self._download(
            key, current=b"foreign one", by_version=_tar_gz(b"foreign two")
        )
        assert isinstance(exc, backup.UnprovenArchive)
        staging = tmp_path / "appdata" / "restore"
        assert list(staging.glob("*")) == []

    def test_a_successful_recovery_leaves_only_the_restored_file(self, tmp_path):
        key = self._recorded(RECOVERED_FINGERPRINT, "v-ours")
        result, _asked = self._download(key, current=b"foreign", by_version=RECOVERED_BYTES)
        staging = tmp_path / "appdata" / "restore"
        assert [p.name for p in staging.glob("*")] == [Path(result["path"]).name]

    def test_recovery_is_not_attempted_for_an_archive_that_is_not_ours(self):
        # Recovery hangs off the fingerprint failure, which only a recorded key
        # reaches. A co-tenant's key is refused before any transfer, so a planted
        # object cannot make an un-overridden restore pay for even the first read,
        # let alone a second.
        other = "snapshots/" + "0" * 32 + "/planted.tar.gz"
        with mock.patch.object(backup.storage, "get_file") as get_file:
            with pytest.raises(backup.UnprovenArchive):
                backup.restore_download("p", "us-west-2", "bkt", other, account=ACCOUNT)
        get_file.assert_not_called()

    def test_the_override_still_accepts_the_overwrite(self):
        # Disaster recovery is the case where every archive is foreign, so
        # `foreign_ok` must still hand back the overwrite it was asked for, labelled
        # for what it is.
        key = self._recorded(RECOVERED_FINGERPRINT, "v-ours")
        result, asked = self._download(
            key,
            current=b"somebody elses archive",
            foreign_ok=True,
        )
        assert result["origin"] == backup.ORIGIN_UNVERIFIED
        assert Path(result["path"]).read_bytes() == b"somebody elses archive"
        assert asked == [""]

    def test_the_override_is_not_quietly_redirected_to_the_recorded_version(self):
        # The override means "take whatever is current at this key without proof", so
        # under it the mismatch does not refuse and there is no refusal to rescue.
        # Reaching for the recorded version anyway would hand this caller DIFFERENT
        # bytes than it accepted, labelled a proven self archive instead of an
        # unverified one -- silently changing what the override means for exactly the
        # key class this change is about. `by_version` is served here, so a recovery
        # attempt would succeed and be visible: what is asserted is that it is never
        # made.
        key = self._recorded(RECOVERED_FINGERPRINT, "v-ours")
        result, asked = self._download(
            key,
            current=b"somebody elses archive",
            by_version=RECOVERED_BYTES,
            foreign_ok=True,
        )
        assert asked == [""]
        assert result["origin"] == backup.ORIGIN_UNVERIFIED
        assert Path(result["path"]).read_bytes() == b"somebody elses archive"


class TestRememberedArchives:
    """The count that keeps a one-slot run record from reading as a one-archive drive.

    ``runs`` holds one record per kind, so a second nightly overwrites the first
    while both archives stay in the bucket. ``uploads`` keeps both, and
    ``remembered_archives`` is what lets a surface say so without a paid listing.

    It is a COUNT OF RECORDS and these cases pin that too: the record map is
    bounded, it covers this install alone, and only a listing knows what the
    drive holds.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse state pin moved verbatim; no test in this module calls monkeypatch.undo()
    def _isolated_state(self, tmp_path, monkeypatch):
        self.state_file = tmp_path / "backup.json"
        monkeypatch.setattr(backup, "_state_path", lambda: self.state_file)
        yield

    def _on_disk(self) -> dict:
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def test_a_second_run_of_one_kind_is_counted_though_the_record_is_overwritten(self):
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/i1/a.tar.gz", 1, "a")
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/i1/b.tar.gz", 2, "b")

        # The ledger keeps the newer run ALONE, which is the design, not the defect.
        assert self._on_disk()["accounts"][ACCOUNT]["runs"][backup.KIND_SNAPSHOT]["key"] == (
            "snapshots/i1/b.tar.gz"
        )
        # Both archives are recorded, so the count is the one reading that says so.
        assert backup.remembered_archives(ACCOUNT)[backup.KIND_SNAPSHOT] == 2

    def test_each_kind_counts_only_its_own_archives(self):
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/i1/a.tar.gz", 1, "a")
        backup._record_run(ACCOUNT, backup.KIND_SESSIONS, "sessions/i1/s.tar.gz", 2, "s")

        counts = backup.remembered_archives(ACCOUNT)
        assert counts[backup.KIND_SNAPSHOT] == 1
        assert counts[backup.KIND_SESSIONS] == 1

    def test_every_kind_is_reported_and_an_unrecorded_kind_reads_zero(self):
        # Unlike the sweep-stored counts beside it, this one is derived on every
        # call: there is no "never measured" state, so absence must not be the
        # answer for a kind that simply has nothing recorded.
        assert backup.remembered_archives(ACCOUNT) == {
            backup.KIND_SNAPSHOT: 0,
            backup.KIND_SESSIONS: 0,
        }
        assert set(backup.remembered_archives(ACCOUNT)) == set(backup.KIND_SUBPATHS)

    def test_a_legacy_key_without_an_install_segment_still_counts_for_its_kind(self):
        # A key written before the install-id namespace carries the kind subpath
        # and nothing else. Its archive is in the drive, so dropping it would
        # under-report exactly the install that upgraded.
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/old.tar.gz", 1, "o")
        assert backup.remembered_archives(ACCOUNT)[backup.KIND_SNAPSHOT] == 1

    def test_a_key_under_no_known_subpath_is_counted_for_no_kind(self):
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "recovery/c.tar.gz", 1, "c")
        assert backup.remembered_archives(ACCOUNT) == {
            backup.KIND_SNAPSHOT: 0,
            backup.KIND_SESSIONS: 0,
        }

    def test_a_subpath_that_only_starts_with_a_kinds_text_is_not_absorbed(self):
        # Attribution reads the first SEGMENT, so a sibling folder whose name
        # begins with a kind's subpath cannot be counted as that kind.
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots-old/i1/a.tar.gz", 1, "a")
        assert backup.remembered_archives(ACCOUNT)[backup.KIND_SNAPSHOT] == 0

    def test_a_push_whose_state_write_failed_is_counted(self):
        # The archive is in the bucket whether or not the record landed, and the
        # in-process overlay is what the ownership read already trusts for that.
        def raiser(_state):
            raise OSError(errno.ENOSPC, "No space left on device")

        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/i1/a.tar.gz", 1, "a")
        with mock.patch.object(backup, "write_state", raiser):
            backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/i1/b.tar.gz", 2, "b")

        assert "snapshots/i1/b.tar.gz" not in self._on_disk()["accounts"][ACCOUNT]["uploads"]
        assert backup.remembered_archives(ACCOUNT)[backup.KIND_SNAPSHOT] == 2

    def test_the_count_reads_low_when_the_record_map_is_full(self, monkeypatch):
        # The oldest record is dropped once the map is full, so an install that
        # keeps pushing reports fewer archives than the drive holds. A reader
        # cannot treat this number as an inventory.
        monkeypatch.setattr(backup, "MAX_REMEMBERED_UPLOADS", 2)
        for seq, name in enumerate(("a", "b", "c"), start=1):
            backup._record_run(
                ACCOUNT, backup.KIND_SNAPSHOT, f"snapshots/i1/{name}.tar.gz", seq, name
            )

        assert backup.remembered_archives(ACCOUNT)[backup.KIND_SNAPSHOT] == 2

    def test_the_count_reads_high_once_retention_deleted_an_archive(self):
        # The other direction, and the one a "floor" reading would get wrong.
        # `_prune_recorded_versions` clears the version record a trusted listing
        # proves is gone, and clears ONLY that: the `uploads` key stays, so this
        # count keeps naming an archive the drive does not hold. The row is
        # worded for this, and it is why only a listing can answer the question.
        for seq, name in enumerate(("a", "b"), start=1):
            backup._record_run(
                ACCOUNT, backup.KIND_SNAPSHOT, f"snapshots/i1/{name}.tar.gz", seq, name
            )
        assert backup.remembered_archives(ACCOUNT)[backup.KIND_SNAPSHOT] == 2

        keys = {"snapshots/i1/a.tar.gz", "snapshots/i1/b.tar.gz"}
        backup._prune_recorded_versions(
            ACCOUNT,
            backup.KIND_SNAPSHOT,
            "i1",
            {"snapshots/i1/b.tar.gz"},
            eligible=keys,
        )

        entry = self._on_disk()["accounts"][ACCOUNT]
        # The version record for the deleted archive is gone ...
        assert "snapshots/i1/a.tar.gz" not in entry.get("upload_versions", {})
        # ... while its upload record survives, which is what this counts.
        assert "snapshots/i1/a.tar.gz" in entry["uploads"]
        assert backup.remembered_archives(ACCOUNT)[backup.KIND_SNAPSHOT] == 2

    def test_a_hand_edited_record_map_reads_as_nothing_recorded_rather_than_raising(self):
        # This value is served on a polled endpoint, so a corrupted document must
        # not raise. A list is the pre-fingerprint shape and its keys still count.
        backup.set_nightly(ACCOUNT, True)
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        state["accounts"][ACCOUNT]["uploads"] = ["snapshots/i1/a.tar.gz"]
        self.state_file.write_text(json.dumps(state), encoding="utf-8")
        assert backup.remembered_archives(ACCOUNT)[backup.KIND_SNAPSHOT] == 1

        state["accounts"][ACCOUNT]["uploads"] = "not a map"
        self.state_file.write_text(json.dumps(state), encoding="utf-8")
        assert backup.remembered_archives(ACCOUNT) == {
            backup.KIND_SNAPSHOT: 0,
            backup.KIND_SESSIONS: 0,
        }
