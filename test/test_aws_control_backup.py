"""AWS Control backup/costs — the paths the P0 suite's backup classes leave uncovered.

``test_aws_control_app.py`` already pins the security-critical traversal (symlink
and junction refusals, descriptor-pinned descent, FIFO/O_NONBLOCK non-hang, the
restore staging checks, the teardown stop gate, and the corrupt-state shape
guards). This file covers what those do not exercise: the two whole-run push
paths (``run_snapshot_backup`` / ``run_sessions_backup``) with ``_authorize_upload``
fully mocked, the ``_authorize_upload`` account-mismatch and consent branches, the
name-based archive fallback that only Windows runs at runtime, ``list_remote_backups``,
the ``restore_download`` resolve-outside-storage guard, ``due_for_nightly``'s
malformed-timestamp branch, and the ``costs`` cache read/freshness branches.

Every fixture that touches the filesystem stays inside ``tmp_path`` so nothing
escapes into the real data home; the two run paths mock the S3 ``put_file`` and
the authorization so no network or live STS is ever reached.
"""

from __future__ import annotations

import datetime as dt
import errno
import json
import logging
import tarfile
import time
from pathlib import Path
from unittest import mock

import pytest

from kiro_crew.apps.builtins.aws_control.backend import backup, costs

ACCOUNT = "111122223333"


# ---------------------------------------------------------------------------
# _authorize_upload — the branches the teardown test in the P0 suite skips over
# ---------------------------------------------------------------------------


class TestAuthorizeUpload:
    @pytest.fixture(autouse=True)
    def _stop_cleared(self):
        # The stop signal is process-global; a leaked set() from another test
        # would make every authorize here raise "shutting down". Bracket it.
        backup.clear_stop()
        yield
        backup.clear_stop()

    def test_upload_refused_when_profile_now_points_at_another_account(self):
        # The live STS check is FIRST and is what makes a profile repointed
        # mid-build refuse: the recorded account and the account the profile
        # resolves to today no longer agree, so the bytes must not leave.
        with mock.patch(
            "kiro_crew.deploy.engine._checked",
            return_value=json.dumps({"Account": "999988887777"}),
        ):
            with pytest.raises(RuntimeError, match="no longer points at"):
                backup._authorize_upload(ACCOUNT, "p", "us-west-2", caller=backup.CALLER_OWNER)

    def test_unparseable_sts_output_reads_as_no_account_and_refuses(self):
        # A garbled STS response must not be trusted as a match: it decodes to
        # an empty account, which can never equal the requested one, so the
        # upload is refused rather than proceeding on unknown identity.
        with mock.patch("kiro_crew.deploy.engine._checked", return_value="not json"):
            with pytest.raises(RuntimeError, match="no longer points at"):
                backup._authorize_upload(ACCOUNT, "p", "us-west-2", caller=backup.CALLER_OWNER)

    def test_upload_refused_when_app_disabled_during_build(self):
        # STS agrees, but the app was disabled while the archive built: the
        # local check catches it before put_file.
        with (
            mock.patch(
                "kiro_crew.deploy.engine._checked",
                return_value=json.dumps({"Account": ACCOUNT}),
            ),
            mock.patch("kiro_crew.apps.manager.is_app_enabled", return_value=False),
        ):
            with pytest.raises(RuntimeError, match="was disabled"):
                backup._authorize_upload(ACCOUNT, "p", "us-west-2", caller=backup.CALLER_OWNER)

    def test_upload_refused_when_s3_consent_no_longer_holds(self):
        # STS agrees and the app is on, but S3 consent was withdrawn: the
        # withdrawal reason is surfaced so the audit trail says why.
        with (
            mock.patch(
                "kiro_crew.deploy.engine._checked",
                return_value=json.dumps({"Account": ACCOUNT}),
            ),
            mock.patch("kiro_crew.apps.manager.is_app_enabled", return_value=True),
            mock.patch("kiro_crew.aws_consent.is_granted", return_value=(False, "expired")),
        ):
            with pytest.raises(RuntimeError, match="consent no longer holds.*expired"):
                backup._authorize_upload(ACCOUNT, "p", "us-west-2", caller=backup.CALLER_OWNER)


# ---------------------------------------------------------------------------
# run_snapshot_backup / run_sessions_backup — the whole push path
# ---------------------------------------------------------------------------


class TestRunSnapshotBackup:
    @pytest.fixture(autouse=True)
    def _isolated_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        yield

    def test_failed_snapshot_build_raises_before_any_upload(self):
        # A non-zero rc from the snapshot engine must abort with a clear error
        # and never reach authorization or put_file.
        with (
            mock.patch.object(backup, "snapshot_main", return_value=3),
            mock.patch.object(backup, "_authorize_upload") as authz,
            mock.patch.object(backup.storage, "put_file") as put_file,
        ):
            with pytest.raises(RuntimeError, match="snapshot build failed"):
                backup.run_snapshot_backup(
                    ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
                )
        authz.assert_not_called()
        put_file.assert_not_called()

    def test_snapshot_build_producing_no_archive_raises(self):
        # rc==0 but the engine left no tarball in the temp dir: the glob is
        # empty and the run must fail rather than push nothing.
        with (
            mock.patch.object(backup, "snapshot_main", return_value=0),
            mock.patch.object(backup.storage, "put_file") as put_file,
        ):
            with pytest.raises(RuntimeError, match="produced no archive"):
                backup.run_snapshot_backup(
                    ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
                )
        put_file.assert_not_called()

    def test_snapshot_success_pushes_entropy_keyed_archive_and_records_run(self):
        # The engine names by second-resolution timestamp; the PUSHED key must
        # carry its own entropy (the _stamp shape) so a racing pair cannot
        # collide on one key. The run record is written under the account.
        def fake_snapshot(argv):
            out_dir = Path(argv[0])
            archive = out_dir / "kirocrew-snapshot-20260101T000000Z.tar.gz"
            with tarfile.open(archive, "w:gz"):
                pass  # an empty-but-valid gzip tar; only its bytes matter here
            return 0

        with (
            mock.patch.object(backup, "snapshot_main", side_effect=fake_snapshot),
            mock.patch.object(backup, "_authorize_upload") as authz,
            mock.patch.object(backup.storage, "put_file") as put_file,
        ):
            record = backup.run_snapshot_backup(
                ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
            )

        authz.assert_called_once_with(ACCOUNT, "p", "us-west-2", caller=backup.CALLER_OWNER)
        put_file.assert_called_once()
        # Same long-timeout contract as the sessions push: the declared
        # `_PUSH_TIMEOUT_SECS` has to REACH the uploader, not sit unread.
        assert put_file.call_args.kwargs["timeout"] == backup._PUSH_TIMEOUT_SECS
        pushed_key = put_file.call_args.args[4]
        assert pushed_key.startswith("snapshots/kirocrew-snapshot-")
        # Entropy suffix means the pushed key is NOT the engine's file name.
        assert "20260101T000000Z" not in pushed_key
        assert record["key"] == pushed_key
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT]["key"] == pushed_key


class TestRunSessionsBackup:
    @pytest.fixture(autouse=True)
    def _isolated_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        yield

    def test_empty_session_dirs_raise_before_upload(self, tmp_path, monkeypatch):
        if not backup._CAN_PIN_TRAVERSAL:
            pytest.skip(
                "descriptor-pinned traversal is unavailable here, so the backup"
                " refuses by design -- TestRefusalWithoutPinnedTraversal covers that"
            )
        # Both halves resolve to empty/absent dirs: the archive holds nothing,
        # and pushing an empty tarball would be a misleading "backup", so the
        # run refuses instead.
        monkeypatch.setattr(backup, "data_home", lambda: tmp_path / "missing_home")
        monkeypatch.setattr(backup, "kiro_sessions_dir", lambda: tmp_path / "missing_cli")
        with (
            mock.patch.object(backup, "_authorize_upload") as authz,
            mock.patch.object(backup.storage, "put_file") as put_file,
        ):
            with pytest.raises(RuntimeError, match="no session files to archive"):
                backup.run_sessions_backup(
                    ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
                )
        authz.assert_not_called()
        put_file.assert_not_called()

    def test_success_tars_both_halves_and_records_run(self, tmp_path, monkeypatch):
        if not backup._CAN_PIN_TRAVERSAL:
            pytest.skip(
                "descriptor-pinned traversal is unavailable here, so the backup"
                " refuses by design -- TestRefusalWithoutPinnedTraversal covers that"
            )
        # A file in each half must land under its own prefix, and the pushed
        # key is the archive's own stamped name under sessions/.
        crew = tmp_path / "crew_home" / backup.SESSIONS_DIR_NAME
        crew.mkdir(parents=True)
        (crew / "t.jsonl").write_bytes(b"transcript\n")
        cli = tmp_path / "cli_sessions"
        cli.mkdir(parents=True)
        (cli / "replay.log").write_bytes(b"replay\n")

        monkeypatch.setattr(backup, "data_home", lambda: tmp_path / "crew_home")
        monkeypatch.setattr(backup, "kiro_sessions_dir", lambda: cli)

        pushed: dict[str, str] = {}

        def fake_put(
            profile, region, bucket, section, key, local_path, *, account=None, timeout=None
        ):
            pushed["key"] = key
            pushed["local"] = local_path
            pushed["timeout"] = timeout
            # The names inside the archive prove both halves were tarred.
            with tarfile.open(local_path) as tar:
                pushed["names"] = sorted(tar.getnames())

        with (
            mock.patch.object(backup, "_authorize_upload"),
            mock.patch.object(backup.storage, "put_file", side_effect=fake_put),
        ):
            record = backup.run_sessions_backup(
                ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
            )

        assert pushed["key"].startswith("sessions/sessions-")
        # A multi-GB sessions archive on a slow uplink needs the long timeout, not
        # `put_file`'s own 600s default -- passing it is the whole point of
        # `_PUSH_TIMEOUT_SECS` existing.
        assert pushed["timeout"] == backup._PUSH_TIMEOUT_SECS
        assert pushed["names"] == ["cli/replay.log", "crew/t.jsonl"]
        assert record["key"] == pushed["key"]
        assert backup.last_runs(ACCOUNT)[backup.KIND_SESSIONS]["bytes"] > 0


# ---------------------------------------------------------------------------
# Hard links — a regular file pointing at someone else's inode
# ---------------------------------------------------------------------------


class TestHardLinkedFilesAreNotArchived:
    def test_a_hard_link_to_an_outside_secret_is_skipped(self, tmp_path):
        """A hard link passes every OTHER check in the descent by construction.

        It is a regular file, it is not a symlink so O_NOFOLLOW admits it, it has
        no reparse point, and it opens relative to the pinned descriptor exactly
        like a real session file -- while naming another file's inode. The link
        COUNT is the only thing that separates them.
        """
        import io
        import os

        if not backup._CAN_PIN_TRAVERSAL:
            pytest.skip(
                "descriptor-pinned traversal is unavailable here, so _add_tree refuses"
                " by design -- TestRefusalWithoutPinnedTraversal covers that"
            )

        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "credentials"
        secret.write_bytes(b"aws_secret_access_key = TOPSECRET")

        root = tmp_path / "sessions"
        root.mkdir()
        (root / "real.json").write_bytes(b"{}")
        try:
            os.link(secret, root / "notes.json")
        except (OSError, NotImplementedError, AttributeError):
            pytest.skip("platform cannot create hard links")

        archive = tmp_path / "out.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            count = backup._add_tree(tar, root, "crew")

        with tarfile.open(archive) as tar:
            names = sorted(tar.getnames())
            blobs = b"".join((tar.extractfile(n) or io.BytesIO()).read() for n in names)
        # Only the genuine single-linked file is archived.
        assert count == 1
        assert names == ["crew/real.json"]
        # And the secret's bytes are nowhere in the archive.
        assert b"TOPSECRET" not in blobs

    def test_an_ordinary_single_linked_file_is_still_archived(self, tmp_path):
        # The link-count test must not reject normal files.
        if not backup._CAN_PIN_TRAVERSAL:
            pytest.skip(
                "descriptor-pinned traversal is unavailable here, so _add_tree refuses"
                " by design -- TestRefusalWithoutPinnedTraversal covers that"
            )
        root = tmp_path / "sessions"
        (root / "nested").mkdir(parents=True)
        (root / "a.json").write_bytes(b"{}")
        (root / "nested" / "b.json").write_bytes(b"[]")

        archive = tmp_path / "out.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            count = backup._add_tree(tar, root, "crew")
        with tarfile.open(archive) as tar:
            assert sorted(tar.getnames()) == ["crew/a.json", "crew/nested/b.json"]
        assert count == 2


# ---------------------------------------------------------------------------
# Refusal when the traversal cannot be pinned to descriptors
# ---------------------------------------------------------------------------


class TestRefusalWithoutPinnedTraversal:
    """There is no name-based fallback, and that is the security property.

    A platform without ``openat`` cannot make the link check and the open one
    operation, so a walk of these agent-writable directories leaves a window in
    which a directory swapped for a junction to ``~/.aws`` is archived -- and this
    archive is uploaded unattended. These pin that the code refuses instead of
    degrading, on every platform, by forcing the capability flag off.
    """

    def test_add_tree_refuses_rather_than_walking_by_name(self, tmp_path):
        root = tmp_path / "sessions"
        (root / "nested").mkdir(parents=True)
        (root / "a.json").write_bytes(b"{}")

        archive = tmp_path / "out.tar.gz"
        with mock.patch.object(backup, "_CAN_PIN_TRAVERSAL", False):
            with tarfile.open(archive, "w:gz") as tar:
                with pytest.raises(RuntimeError) as exc:
                    backup._add_tree(tar, root, "crew")
        assert "openat" in str(exc.value)

        # Nothing was archived: refusing must not produce a partial tar that
        # looks like a successful backup.
        with tarfile.open(archive) as tar:
            assert tar.getnames() == []

    def test_the_run_refuses_before_it_touches_the_filesystem(self):
        # The refusal is stated at the entry point, so a failed run record says
        # what is missing instead of surfacing an empty-archive error from deeper
        # down. put_file must never be reached.
        with (
            mock.patch.object(backup, "_CAN_PIN_TRAVERSAL", False),
            mock.patch.object(backup.storage, "put_file") as put,
            mock.patch.object(backup, "_authorize_upload") as authz,
        ):
            with pytest.raises(RuntimeError) as exc:
                backup.run_sessions_backup(
                    "123456789012", "p", "us-west-2", "b", caller=backup.CALLER_OWNER
                )
        assert "refused" in str(exc.value)
        put.assert_not_called()
        authz.assert_not_called()

    def test_no_name_based_walk_remains_in_the_module(self):
        # The fallback was deleted rather than left unreachable: an unreachable
        # walk is one refactor away from being reachable again. Checked on the AST
        # rather than the text, because the module legitimately MENTIONS os.walk
        # in prose explaining why the pinned descent replaces it.
        import ast

        assert not hasattr(backup, "_add_tree_by_name")
        tree = ast.parse(Path(backup.__file__).read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "walk"
        ]
        assert calls == []


# ---------------------------------------------------------------------------
# list_remote_backups
# ---------------------------------------------------------------------------


class TestListRemoteBackups:
    def test_lists_both_kinds_newest_first_and_caps_the_page(self):
        # Each kind is sorted by key descending (newest stamps first) and the
        # page is capped at 20 so a bucket with thousands of archives cannot
        # flood the console.
        def fake_list(profile, region, bucket, section, sub, *, account):
            return {"files": [{"key": f"{sub}/{i:03d}.tar.gz"} for i in range(25)]}

        with mock.patch.object(backup.storage, "list_section", side_effect=fake_list):
            result = backup.list_remote_backups("p", "us-west-2", "bkt", account="111122223333")

        assert set(result) == {backup.KIND_SNAPSHOT, backup.KIND_SESSIONS}
        snaps = result[backup.KIND_SNAPSHOT]
        assert len(snaps) == 20
        # Descending: the highest-numbered (newest) key comes first.
        assert snaps[0]["key"] == "snapshots/024.tar.gz"
        assert snaps[-1]["key"] == "snapshots/005.tar.gz"


# ---------------------------------------------------------------------------
# restore_download — the resolve-outside-storage guard
# ---------------------------------------------------------------------------


class TestRestoreDownloadResolveGuard:
    def test_staging_resolving_outside_app_storage_is_refused(self, tmp_path, monkeypatch):
        # is_link_or_junction can pass (restore is a plain dir at first glance)
        # yet a COMPONENT above it be a link, so restore/ resolves elsewhere.
        # The resolve() comparison after mkdir is the only check that catches a
        # swap higher up; without it the S3 bytes would land outside app storage.
        base = tmp_path / "appdata"
        base.mkdir()

        # Make resolve() report a path outside base for the staging dir, while
        # is_link_or_junction and is_dir both report a benign real directory.
        real_restore = base / "restore"

        def fake_resolve(self, *a, **k):
            if self == real_restore:
                return tmp_path / "escaped" / "restore"
            return Path(str(self))

        monkeypatch.setattr(backup, "app_data_dir", lambda name: base)
        with (
            mock.patch.object(backup, "is_link_or_junction", return_value=False),
            mock.patch.object(Path, "resolve", fake_resolve),
            mock.patch.object(backup.storage, "get_file") as get_file,
        ):
            with pytest.raises(ValueError, match="resolves outside app storage"):
                backup.restore_download(
                    "p", "us-west-2", "b", "snapshots/a.tar.gz", account="111122223333"
                )
        get_file.assert_not_called()


# ---------------------------------------------------------------------------
# due_for_nightly — the malformed-timestamp branch
# ---------------------------------------------------------------------------


class TestDueForNightlyBadStamp:
    @pytest.fixture(autouse=True)
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

    def test_a_persisting_run_does_not_evict_a_newer_held_run(self):
        # The pop happens after the sidecar lock is released, so a run that
        # failed its write can cache a NEWER record in the window between another
        # run's write and that pop. An unconditional pop would evict it and the
        # panel would report the older archive while the newer upload has no
        # record anywhere. Two gateway processes hold separate caches, so no file
        # lock can close this -- only monotonic eviction can.
        backup.set_nightly(ACCOUNT, True)
        newer = {
            "key": "snapshots/newer.tar.gz",
            "bytes": 11,
            "at": dt.datetime(2099, 1, 1, tzinfo=dt.timezone.utc).isoformat(
                timespec="microseconds"
            ),
        }
        backup._remember_unpersisted(ACCOUNT, backup.KIND_SNAPSHOT, newer)

        # This one persists, and its stamp is older than the held record's.
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/older.tar.gz", 7)

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

    def test_a_held_run_with_an_equal_stamp_is_evicted(self):
        # Windows clock granularity is far above a microsecond, so two
        # back-to-back runs can stamp IDENTICALLY -- this is what reddened
        # `test_a_later_successful_write_takes_over_from_memory` on the Windows
        # shard while it passed on Linux. On a tie the entry must go: the two
        # records are simultaneous and the persisted one is on disk, and keeping
        # the held copy would make it immortal for the life of the process
        # because no later write can ever compare greater. Only a STRICTLY newer
        # held record is protected. Asserted directly rather than through the
        # clock, so it pins the rule on every platform.
        stamp = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc).isoformat(timespec="microseconds")
        backup._remember_unpersisted(
            ACCOUNT,
            backup.KIND_SNAPSHOT,
            {"key": "snapshots/tie.tar.gz", "bytes": 5, "at": stamp},
        )

        backup._forget_unpersisted(ACCOUNT, backup.KIND_SNAPSHOT, stamp)

        assert (backup._state_key(), ACCOUNT, backup.KIND_SNAPSHOT) not in backup._unpersisted_runs

    def test_two_runs_in_the_same_second_are_distinguishable(self):
        # `_stamp` already carries entropy because a manual run can race the
        # nightly loop into the same second. The run record has to be orderable
        # at that resolution too, or the overlay cannot tell which of two uploads
        # is the later one: at `timespec="seconds"` these compare EQUAL.
        backup.set_nightly(ACCOUNT, True)
        first = backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/a.tar.gz", 1)
        second = backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/b.tar.gz", 2)

        assert first["at"] != second["at"]
        assert first["at"] < second["at"]
        # And it still parses as a timestamp, which `due_for_nightly` relies on.
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


class TestCostsCacheBranches:
    @pytest.fixture(autouse=True)
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
