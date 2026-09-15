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
import hashlib
import json
import logging
import tarfile
import time
from pathlib import Path
from unittest import mock

import pytest

from kiro_crew.apps.builtins.aws_control.backend import backup, costs

ACCOUNT = "111122223333"

#: What the fake downloads write, and the fingerprint a matching upload record
#: must carry -- the restore verdict is decided on the bytes that arrive.
ARCHIVE_BYTES = b"archive"
ARCHIVE_FINGERPRINT = hashlib.md5(ARCHIVE_BYTES).hexdigest()


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
        # resolves to today do not agree, so the bytes must not leave.
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

        # ONE authorization per S3 write, and that is the contract: the archive
        # PUT and each of the two label PUTs sit immediately after their own gate,
        # so none of them runs on a decision that went stale during another's
        # round trip.
        assert authz.call_count == 3
        for call in authz.call_args_list:
            assert call == mock.call(ACCOUNT, "p", "us-west-2", caller=backup.CALLER_OWNER)
        # Two pushes now: the archive, then this install's label sidecar beside it.
        # The label is what stops another install's rows reading as 32 hex
        # characters, and it is published from here because this is the one place
        # that already holds a bucket and a live authorization decision.
        pushed = {call.args[4]: call for call in put_file.call_args_list}
        install_id = backup.install_identity()["id"]
        # The label is written under BOTH kind prefixes, not only the kind that
        # triggered this run. The reader takes the first sidecar it finds across
        # kinds, so a rename followed by a backup of one kind would otherwise leave
        # the other prefix serving the old name.
        for sub in ("snapshots", "sessions"):
            assert f"{sub}/{install_id}/{backup.LABEL_OBJECT_NAME}" in pushed
        archive_keys = [k for k in pushed if not k.endswith(backup.LABEL_OBJECT_NAME)]
        assert len(archive_keys) == 1
        pushed_key = archive_keys[0]
        # Same long-timeout contract as the sessions push: the declared
        # `_PUSH_TIMEOUT_SECS` has to REACH the uploader, not sit unread.
        assert pushed[pushed_key].kwargs["timeout"] == backup._PUSH_TIMEOUT_SECS
        # The install id is its own key SEGMENT, which is what lets one delimited
        # listing name every install writing here instead of walking the prefix.
        install_id = backup.install_identity()["id"]
        assert pushed_key.startswith(f"snapshots/{install_id}/kirocrew-snapshot-")
        # Entropy suffix means the pushed key is NOT the engine's file name.
        assert "20260101T000000Z" not in pushed_key
        assert record["key"] == pushed_key
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT]["key"] == pushed_key

    def test_a_failed_label_push_still_reports_the_backup_as_done(self):
        # The label is a caption. A backup whose archive reached the bucket must
        # not be reported as failed because a hundred-byte display string did not
        # -- the reader degrades to the id on its own.
        def fake_snapshot(argv):
            archive = Path(argv[0]) / "kirocrew-snapshot-20260101T000000Z.tar.gz"
            with tarfile.open(archive, "w:gz"):
                pass
            return 0

        def fake_put(profile, region, bucket, section, key, local_path, **kwargs):
            if key.endswith(backup.LABEL_OBJECT_NAME):
                raise RuntimeError("label push failed")

        with (
            mock.patch.object(backup, "snapshot_main", side_effect=fake_snapshot),
            mock.patch.object(backup, "_authorize_upload"),
            mock.patch.object(backup.storage, "put_file", side_effect=fake_put),
        ):
            record = backup.run_snapshot_backup(
                ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
            )

        assert record["key"].startswith("snapshots/")
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT]["key"] == record["key"]


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
            if key.endswith(backup.LABEL_OBJECT_NAME):
                # The label sidecar rides along on the same push path; it is not
                # the archive and must not be mistaken for it here.
                pushed["label_key"] = key
                return
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

        install_id = backup.install_identity()["id"]
        assert pushed["key"].startswith(f"sessions/{install_id}/sessions-")
        assert pushed["label_key"] == f"sessions/{install_id}/{backup.LABEL_OBJECT_NAME}"
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
    @pytest.fixture(autouse=True)
    def _isolated_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        # The listing reads install ids through `_install_folders` -- one
        # implementation, and the complete unredacted one -- so a test that
        # only stubs `list_section` would reach AWS. Each test below sets
        # `self.folders` to the ids the bucket holds.
        self.folders: set[str] = set()
        monkeypatch.setattr(
            backup,
            "_install_folders",
            lambda profile, region, bucket, kind, *, account: set(self.folders),
        )
        yield

    def test_lists_own_and_legacy_archives_newest_first_and_caps_the_page(self):
        mine = backup.install_identity()["id"]
        self.folders = {mine}

        def fake_list(profile, region, bucket, section, sub, *, account):
            if "/" in sub:
                # This install's own prefix.
                return {
                    "files": [
                        {
                            "key": f"{sub}/kirocrew-snapshot-2026010{i % 10}T000000Z-aaa.tar.gz",
                            "modified": f"2026-02-{i + 1:02d}T00:00:00+00:00",
                        }
                        for i in range(25)
                    ],
                    "folders": [],
                }
            # The un-nested level: pre-namespace archives as FILES, every install
            # writing here as a FOLDER -- both answers from one call.
            return {
                "files": [
                    {
                        "key": f"{sub}/kirocrew-snapshot-20250101T00000{i}Z-bbb.tar.gz",
                        "modified": f"2025-01-0{i + 1}T00:00:00+00:00",
                    }
                    for i in range(2)
                ],
                "folders": [f"{sub}/{mine}"],
            }

        with mock.patch.object(backup.storage, "list_section", side_effect=fake_list):
            result = backup.list_remote_backups("p", "us-west-2", "bkt", account=ACCOUNT)

        snaps = result[backup.KIND_SNAPSHOT]
        assert len(snaps) == 20
        # Ordered by S3's own timestamp, so rows from two prefixes interleave by
        # time instead of clustering by install id -- clustering is how the newest
        # overall stops being at the top and the wrong archive gets picked. The
        # 2025 legacy archives are older than every 2026 one, so the cap drops
        # them and the newest 2026 row leads.
        assert snaps[0]["modified"] == "2026-02-25T00:00:00+00:00"
        assert snaps[0]["key"].startswith(f"snapshots/{mine}/")
        # Under our own prefix but with no local record of the upload, so the row
        # is reported as unverified rather than claimed -- a prefix is a folder
        # name any bucket writer can create.
        assert snaps[0]["origin"] == backup.ORIGIN_UNVERIFIED
        assert snaps[0]["install"] == mine
        # Recorded as uploaded by this install, the same row reads as ours.
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, snaps[0]["key"], 1)
        with mock.patch.object(backup.storage, "list_section", side_effect=fake_list):
            again = backup.list_remote_backups("p", "us-west-2", "bkt", account=ACCOUNT)
        assert again[backup.KIND_SNAPSHOT][0]["origin"] == backup.ORIGIN_SELF
        assert result["others"] == 0
        assert result["installs"][0]["id"] == mine

    def test_a_legacy_archive_is_never_claimed_as_this_installs(self):
        # Backward compatibility with a bite: pre-namespace archives keep being
        # listed, but as unknown origin. Claiming them would re-create the exact
        # mistake the namespace prevents.
        def fake_list(profile, region, bucket, section, sub, *, account):
            if "/" in sub:
                return {"files": [], "folders": []}
            return {"files": [{"key": f"{sub}/kirocrew-snapshot-old.tar.gz"}], "folders": []}

        with mock.patch.object(backup.storage, "list_section", side_effect=fake_list):
            result = backup.list_remote_backups("p", "us-west-2", "bkt", account=ACCOUNT)

        rows = result[backup.KIND_SNAPSHOT]
        assert [r["origin"] for r in rows] == [backup.ORIGIN_LEGACY]
        assert rows[0]["install"] == ""

    def test_another_installs_folder_is_reported_without_listing_its_archives(self):
        # The default view names the other install (so "another install writes
        # here" is visible) but does NOT enumerate its prefix: that costs paid
        # calls and is opt-in.
        mine = backup.install_identity()["id"]
        other = "a" * 32
        self.folders = {mine, other}
        listed: list[str] = []

        def fake_list(profile, region, bucket, section, sub, *, account):
            listed.append(sub)
            if "/" in sub:
                return {"files": [{"key": f"{sub}/one.tar.gz"}], "folders": []}
            return {"files": [], "folders": [f"{sub}/{mine}", f"{sub}/{other}"]}

        with mock.patch.object(backup.storage, "list_section", side_effect=fake_list):
            result = backup.list_remote_backups("p", "us-west-2", "bkt", account=ACCOUNT)

        assert result["others"] == 1
        assert {i["id"] for i in result["installs"]} == {mine, other}
        assert not any(sub.endswith(other) for sub in listed)
        # No label is fetched for an install whose rows are not being shown.
        assert [i for i in result["installs"] if i["id"] == other][0]["label"] == ""

    def test_include_others_enumerates_foreign_prefixes_and_reads_their_labels(self):
        # This is what makes a REPLACEMENT machine usable: it owns no archives, so
        # a view that only ever read its own prefix would show it nothing on the
        # one occasion the bucket holds the only surviving copy.
        mine = backup.install_identity()["id"]
        other = "b" * 32
        self.folders = {other}

        def fake_list(profile, region, bucket, section, sub, *, account):
            if sub.endswith(other):
                return {
                    "files": [
                        {"key": f"{sub}/theirs.tar.gz"},
                        {"key": f"{sub}/{backup.LABEL_OBJECT_NAME}"},
                    ],
                    "folders": [],
                }
            if "/" in sub:
                return {"files": [], "folders": []}
            return {"files": [], "folders": [f"{sub}/{other}"]}

        with (
            mock.patch.object(backup.storage, "list_section", side_effect=fake_list),
            mock.patch.object(backup, "read_remote_label", return_value="their laptop"),
        ):
            result = backup.list_remote_backups(
                "p", "us-west-2", "bkt", account=ACCOUNT, include_others=True
            )

        rows = result[backup.KIND_SNAPSHOT]
        assert [r["key"] for r in rows] == [f"snapshots/{other}/theirs.tar.gz"]
        assert rows[0]["origin"] == backup.ORIGIN_OTHER
        assert rows[0]["install"] == other
        # The label sidecar shares the prefix with the archives it labels. It is
        # not an archive and must never be offered for restore.
        assert all(backup.LABEL_OBJECT_NAME not in r["key"] for r in rows)
        assert [i for i in result["installs"] if i["id"] == other][0]["label"] == "their laptop"
        assert mine not in [r["install"] for r in rows]

    def test_more_other_installs_than_the_cap_are_reported_as_truncated(self):
        # A bounded expansion that says it is bounded, rather than silently
        # showing a subset of the machines writing here.
        ids = [f"{n:032x}" for n in range(backup.MAX_OTHER_INSTALLS + 3)]
        self.folders = set(ids)

        def fake_list(profile, region, bucket, section, sub, *, account):
            if "/" in sub:
                return {"files": [], "folders": []}
            return {"files": [], "folders": [f"{sub}/{i}" for i in ids]}

        with mock.patch.object(backup.storage, "list_section", side_effect=fake_list):
            result = backup.list_remote_backups("p", "us-west-2", "bkt", account=ACCOUNT)

        assert result["others"] == len(ids)
        assert result["truncated"] is True
        assert len(result["installs"]) == backup.MAX_OTHER_INSTALLS + 1  # + this install

    def test_a_hex_install_id_survives_the_display_listings_sanitisation(self):
        # `list_remote_backups` reads install ids out of `list_section`'s FOLDER
        # names, and that listing runs every name through the egress redactors --
        # which exist to change strings that look like secrets, and a 32-hex blob
        # is exactly that shape. If a redactor ever rewrote one, the id would stop
        # matching and another install's archives would read as absent: the drive
        # would look unshared. Pin the property the parse depends on.
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        for candidate in (backup.install_identity()["id"], "a" * 32, f"{7:032x}"):
            name, _ = redact_credentials(candidate)
            name, _ = redact_exfiltration_urls(name)
            assert name == candidate


# ---------------------------------------------------------------------------
# install identity — the id that decides, and the label that only displays
# ---------------------------------------------------------------------------


class TestInstallIdentity:
    @pytest.fixture(autouse=True)
    def _isolated_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        backup._fallback_identity.clear()
        yield
        backup._fallback_identity.clear()

    def test_the_id_is_minted_once_and_then_stable(self):
        first = backup.install_identity()
        assert backup._INSTALL_ID_RE.match(first["id"])
        assert backup.install_identity()["id"] == first["id"]
        # Stored at the TOP level, not per account: one machine must not become
        # two installs the first time a second account is connected.
        assert backup.read_state()[backup.INSTALL_KEY]["id"] == first["id"]

    def test_the_id_is_not_the_telemetry_install_id(self):
        # Reusing `beacon.install_id()` would materialise a TELEMETRY identity on
        # a host that opted out of telemetry, because that function creates the
        # file it reads. The technique is worth copying; the value is not.
        with mock.patch("kiro_crew.beacon.install_id") as beacon_id:
            backup.install_identity()
        beacon_id.assert_not_called()

    def test_the_default_label_names_no_machine_fact(self):
        identity = backup.install_identity()
        # Published to a shared bucket, so a default must not leak a hostname or
        # a user name. Four hex characters make two installs distinguishable,
        # which is all a default has to do.
        assert identity["label"] == f"install-{identity['id'][:4]}"

    def test_renaming_changes_the_label_and_never_the_id(self):
        before = backup.install_identity()
        after = backup.set_install_label("Raymond's laptop")
        assert after["label"] == "Raymond's laptop"
        assert after["id"] == before["id"]
        assert backup.install_identity()["label"] == "Raymond's laptop"

    def test_an_empty_or_unusable_label_falls_back_rather_than_storing_nothing(self):
        identity = backup.install_identity()
        assert backup.set_install_label("   ")["label"] == f"install-{identity['id'][:4]}"
        assert backup.set_install_label(None)["label"] == f"install-{identity['id'][:4]}"

    def test_an_unstorable_id_degrades_to_a_process_local_one_instead_of_failing(self):
        # An unwritable state file already lets a backup upload and hold its run
        # in memory (see `_record_run`). Refusing here would turn that into a
        # backup that stops running, which is a regression -- and a per-process id
        # still keeps one process's archives together and apart from another
        # install's.
        with mock.patch.object(
            backup, "_locked_state_update", side_effect=OSError(errno.EROFS, "ro")
        ):
            first = backup.install_identity()
            second = backup.install_identity()
        assert backup._INSTALL_ID_RE.match(first["id"])
        assert first == second


class TestLabelIsDisplayOnly:
    @pytest.fixture(autouse=True)
    def _isolated_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        yield

    def test_a_foreign_label_is_redacted_and_bounded_before_it_is_rendered(self):
        # A label read from the bucket is written by ANOTHER install, so it is
        # foreign-authored text arriving through the same door object names arrive
        # through -- and `storage.list_section` already runs those through these
        # redactors for exactly this reason.
        assert len(backup.sanitize_label("x" * 500)) == backup.LABEL_MAX_CHARS
        # Control characters are what turn one caption line into something that
        # overwrites the row above it, and they survive both redactors untouched.
        # The escape BYTE is what carries that power, so it is the byte that goes;
        # the printable remainder of a sequence is inert text.
        cleaned = backup.sanitize_label("lap\x1b[2Jtop\r\n")
        assert not any(not ch.isprintable() for ch in cleaned)
        assert "\x1b" not in cleaned and "\n" not in cleaned and "\r" not in cleaned
        assert cleaned.startswith("lap") and cleaned.endswith("top")
        assert backup.sanitize_label("") == ""
        assert backup.sanitize_label(12345) == ""
        leaked = backup.sanitize_label("key AKIAIOSFODNN7EXAMPLE here")
        assert "AKIAIOSFODNN7EXAMPLE" not in leaked

    def test_a_published_label_cannot_make_a_foreign_archive_restorable(self):
        # The whole point of the id/label split. A label is a string an install
        # writes about ITSELF into a bucket another install reads, so if it could
        # reach the gate an install could name itself into being restorable.
        mine = backup.install_identity()
        other = "c" * 32
        key = f"snapshots/{other}/theirs.tar.gz"
        with (
            mock.patch.object(
                backup, "read_remote_label", return_value=mine["label"]
            ) as read_label,
            mock.patch.object(backup.storage, "get_file") as get_file,
        ):
            with pytest.raises(backup.UnprovenArchive) as caught:
                backup.restore_download("p", "us-west-2", "bkt", key, account=ACCOUNT)
        assert caught.value.install_id == other
        # Not merely unpersuaded by the label -- it never asks for one.
        read_label.assert_not_called()
        get_file.assert_not_called()

    def test_an_unreadable_label_sidecar_degrades_to_the_empty_string(self):
        # A caption that could not be read must not break the listing that would
        # have told the operator whose archives these are.
        with mock.patch.object(
            backup.storage, "get_object_head_bytes", side_effect=RuntimeError("denied")
        ):
            assert (
                backup.read_remote_label(
                    "p", "r", "b", backup.KIND_SNAPSHOT, "d" * 32, account=ACCOUNT
                )
                == ""
            )
        with mock.patch.object(
            backup.storage, "get_object_head_bytes", return_value=(b"not json", 8)
        ):
            assert (
                backup.read_remote_label(
                    "p", "r", "b", backup.KIND_SNAPSHOT, "d" * 32, account=ACCOUNT
                )
                == ""
            )

    def test_the_label_read_is_range_bounded_not_a_full_download(self):
        # The object is written by another install, so its SIZE is that install's
        # choice: a plain get-object of a file named `_label.json` would let a
        # multi-gigabyte object be pulled onto this disk, on the owner's transfer
        # bill, to render one caption.
        with mock.patch.object(
            backup.storage,
            "get_object_head_bytes",
            return_value=(json.dumps({"label": "their box"}).encode(), 40),
        ) as head:
            label = backup.read_remote_label(
                "p", "r", "b", backup.KIND_SNAPSHOT, "e" * 32, account=ACCOUNT
            )
        assert label == "their box"
        assert head.call_args.kwargs["max_bytes"] <= 4096
        assert head.call_args.args[4] == f"snapshots/{'e' * 32}/{backup.LABEL_OBJECT_NAME}"

    def test_the_label_sidecar_cannot_be_named_by_a_restore_request(self):
        # `_label.json` starts with an underscore, and `validate_key` requires a
        # segment to START alphanumeric -- so the object is unreachable through
        # every route that validates a caller-supplied key. That is a rule, not a
        # name comparison somebody has to remember to write.
        from kiro_crew.apps.builtins.aws_control.backend import storage as storage_mod

        assert storage_mod.validate_key(f"snapshots/{'f' * 32}/{backup.LABEL_OBJECT_NAME}")


class TestObjectAuthentication:
    """A recorded key names a PATH; the fingerprint names the BYTES.

    On a drive other installs can write to, a key can be overwritten after this
    install recorded uploading it. Matching keys alone would then hand that
    overwrite back as ours with no confirmation, so the verdict is decided on the
    bytes that actually arrive -- which also leaves no window for an overwrite to
    land between a check and the transfer.
    """

    @pytest.fixture(autouse=True)
    def _isolated_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        monkeypatch.setattr(
            "kiro_crew.apps.builtins.aws_control.backend.backup.app_data_dir",
            lambda name: tmp_path / "appdata",
        )
        backup._unpersisted_runs.clear()
        yield
        backup._unpersisted_runs.clear()

    def test_the_fingerprint_is_the_s3_etag_of_a_single_part_upload(self, tmp_path):
        # `put_file` issues one `put-object`, and for a single-part upload under
        # AES256 the ETag is the hex MD5 of the body -- which is why this can be
        # computed locally with no extra call to AWS.
        body = tmp_path / "archive.tar.gz"
        body.write_bytes(b"some archive bytes")
        assert backup._body_fingerprint(body) == hashlib.md5(b"some archive bytes").hexdigest()

    def _download(self, key, content, **kwargs):
        def fake_get(profile, region, bucket, section, k, dest, *, account, timeout=600):
            Path(dest).write_bytes(content)

        with mock.patch.object(backup.storage, "get_file", side_effect=fake_get):
            return backup.restore_download("p", "us-west-2", "bkt", key, account=ACCOUNT, **kwargs)

    def _recorded(self, fingerprint):
        mine = backup.install_identity()["id"]
        key = f"snapshots/{mine}/a.tar.gz"
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, key, 10, fingerprint)
        return key

    def test_bytes_matching_the_record_confirm_the_archive_is_ours(self):
        key = self._recorded(ARCHIVE_FINGERPRINT)
        assert self._download(key, ARCHIVE_BYTES)["origin"] == backup.ORIGIN_SELF

    def test_an_overwritten_archive_is_no_longer_ours_and_needs_the_override(self):
        key = self._recorded(ARCHIVE_FINGERPRINT)
        with pytest.raises(backup.UnprovenArchive) as caught:
            self._download(key, b"somebody elses archive")
        assert caught.value.origin == backup.ORIGIN_UNVERIFIED
        result = self._download(key, b"somebody elses archive", foreign_ok=True)
        assert result["origin"] == backup.ORIGIN_UNVERIFIED

    def test_a_refused_restore_leaves_nothing_at_the_destination(self, tmp_path):
        # The refusal happens after the transfer, so the staged bytes must be
        # discarded and the destination left untouched -- otherwise a refusal would
        # still hand somebody an archive to apply.
        key = self._recorded(ARCHIVE_FINGERPRINT)
        with pytest.raises(backup.UnprovenArchive):
            self._download(key, b"not ours")
        staging = tmp_path / "appdata" / "restore"
        assert not (staging / "a.tar.gz").exists()
        assert list(staging.glob("*")) == []

    def test_a_planted_key_under_our_prefix_is_refused_without_downloading_it(self):
        """A co-writer must not be able to make an un-overridden restore pay for a GET.

        The install prefix is a listable folder name, so anyone who can write to the
        drive can put an object of any size under it. That key is absent from the
        upload ledger, which local state settles on its own -- so it is refused
        before the transfer, not after.
        """
        mine = backup.install_identity()["id"]
        planted = f"snapshots/{mine}/planted.tar.gz"
        with mock.patch.object(backup.storage, "get_file") as get_file:
            with pytest.raises(backup.UnprovenArchive) as caught:
                backup.restore_download("p", "us-west-2", "bkt", planted, account=ACCOUNT)
        assert caught.value.origin == backup.ORIGIN_UNVERIFIED
        get_file.assert_not_called()

    def test_a_record_with_no_fingerprint_fails_closed(self):
        # Unknown is not a pass. A key recorded before a fingerprint existed cannot
        # authenticate anything, so it takes the needs-an-override path.
        key = self._recorded("")
        with pytest.raises(backup.UnprovenArchive) as caught:
            self._download(key, ARCHIVE_BYTES)
        assert caught.value.origin == backup.ORIGIN_UNVERIFIED

    def test_verification_costs_no_extra_aws_call(self):
        # The fingerprint comes from a file already on disk, both when it is written
        # and when it is read back, so proving ownership adds no request.
        key = self._recorded(ARCHIVE_FINGERPRINT)
        with mock.patch.object(backup, "_checked") as checked:
            assert self._download(key, ARCHIVE_BYTES)["origin"] == backup.ORIGIN_SELF
        checked.assert_not_called()


class TestClassifyKey:
    def test_origin_comes_from_the_key_and_nothing_else(self):
        mine = "1" * 32
        other = "2" * 32
        own_key = f"snapshots/{mine}/a.tar.gz"
        # `self` requires a local record of having uploaded it -- see the class
        # below for why the prefix alone is not enough.
        assert backup.classify_key(own_key, mine, {own_key}) == (backup.ORIGIN_SELF, mine)
        assert backup.classify_key(f"sessions/{other}/a.tar.gz", mine) == (
            backup.ORIGIN_OTHER,
            other,
        )
        # No id segment: written before the namespace existed, so the origin is
        # genuinely unknown. Claiming it as this install's would re-create the
        # mistake the namespace prevents; calling it foreign would refuse an
        # operator their own pre-upgrade archive.
        assert backup.classify_key("snapshots/kirocrew-snapshot-x.tar.gz", mine) == (
            backup.ORIGIN_LEGACY,
            "",
        )
        # A folder someone made in the console is not an install id.
        assert backup.classify_key("snapshots/holiday-photos/a.tar.gz", mine) == (
            backup.ORIGIN_LEGACY,
            "",
        )


class TestSelfOwnershipIsProvenNotInferred:
    """An install id is a folder name, so a bucket writer can create one.

    The drive is shared by design, which puts a co-writer inside the operating
    envelope rather than outside it. If the prefix alone decided ownership, that
    co-writer could upload beneath this install's own prefix and the archive would
    come back classified as ours and restore with no confirmation -- the exact
    unwarned wrong-machine restore this whole change exists to stop, reintroduced
    through the mechanism meant to stop it.
    """

    @pytest.fixture(autouse=True)
    def _isolated_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        monkeypatch.setattr(
            "kiro_crew.apps.builtins.aws_control.backend.backup.app_data_dir",
            lambda name: tmp_path / "appdata",
        )
        backup._unpersisted_runs.clear()
        yield
        backup._unpersisted_runs.clear()

    def _download(self, key, **kwargs):
        def fake_get(profile, region, bucket, section, k, dest, *, account, timeout=600):
            Path(dest).write_bytes(b"archive")

        with mock.patch.object(backup.storage, "get_file", side_effect=fake_get):
            return backup.restore_download("p", "us-west-2", "bkt", key, account=ACCOUNT, **kwargs)

    def test_a_key_planted_under_our_own_prefix_is_not_treated_as_ours(self):
        mine = backup.install_identity()["id"]
        planted = f"snapshots/{mine}/kirocrew-snapshot-planted.tar.gz"
        # Nothing was ever recorded as uploaded, so the prefix is the ONLY thing
        # claiming this archive is ours -- and a prefix is forgeable.
        assert backup.uploaded_keys(ACCOUNT) == set()
        # The BACKEND refuses it, not just the dashboard. A confirmation dialog
        # binds only the client that shows it, so a caller reaching the endpoint
        # directly would otherwise restore a planted archive with no override.
        with pytest.raises(backup.UnprovenArchive) as caught:
            self._download(planted)
        assert caught.value.origin == backup.ORIGIN_UNVERIFIED
        assert caught.value.install_id == mine
        # With the override stated, it downloads and still reports what it is.
        assert self._download(planted, foreign_ok=True)["origin"] == backup.ORIGIN_UNVERIFIED

    def test_an_archive_this_install_recorded_uploading_is_ours(self):
        mine = backup.install_identity()["id"]
        key = f"snapshots/{mine}/kirocrew-snapshot-real.tar.gz"
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, key, 10, ARCHIVE_FINGERPRINT)
        assert backup.uploaded_objects(ACCOUNT)[key] == ARCHIVE_FINGERPRINT
        assert self._download(key)["origin"] == backup.ORIGIN_SELF

    def test_an_upload_whose_state_write_failed_still_counts_as_ours(self):
        # The archive really did reach the bucket; making the operator confirm an
        # archive this process uploaded minutes ago would be a false alarm.
        mine = backup.install_identity()["id"]
        key = f"snapshots/{mine}/kirocrew-snapshot-held.tar.gz"
        with mock.patch.object(
            backup, "_locked_state_update", side_effect=OSError(errno.ENOSPC, "full")
        ):
            backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, key, 10)
        assert key in backup.uploaded_keys(ACCOUNT)

    def test_the_remembered_set_is_bounded_and_drops_the_oldest(self):
        mine = backup.install_identity()["id"]
        keys = [
            f"snapshots/{mine}/a{i:04d}.tar.gz" for i in range(backup.MAX_REMEMBERED_UPLOADS + 5)
        ]
        for key in keys:
            backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, key, 1)
        remembered = backup.uploaded_keys(ACCOUNT)
        assert len(remembered) == backup.MAX_REMEMBERED_UPLOADS
        assert keys[-1] in remembered
        assert keys[0] not in remembered

    def test_re_recording_one_key_does_not_grow_the_set(self):
        mine = backup.install_identity()["id"]
        key = f"snapshots/{mine}/a.tar.gz"
        for _ in range(4):
            backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, key, 1, "abc123")
        assert backup.read_state()["accounts"][ACCOUNT]["uploads"] == {key: "abc123"}

    def test_the_upload_record_is_local_and_never_read_from_the_bucket(self):
        # What makes the record trustworthy is that no bucket writer can reach it.
        # If the gate ever consulted S3 for this answer, the forgery would be back.
        mine = backup.install_identity()["id"]
        key = f"snapshots/{mine}/a.tar.gz"
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, key, 1)
        with (
            mock.patch.object(backup.storage, "list_section") as listed,
            mock.patch.object(backup.storage, "get_object_head_bytes") as head,
        ):
            assert backup.uploaded_keys(ACCOUNT) == {key}
        listed.assert_not_called()
        head.assert_not_called()


class TestRestoreOwnershipGate:
    @pytest.fixture(autouse=True)
    def _isolated_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        monkeypatch.setattr(
            "kiro_crew.apps.builtins.aws_control.backend.backup.app_data_dir",
            lambda name: tmp_path / "appdata",
        )
        yield

    def _download(self, key, **kwargs):
        def fake_get(profile, region, bucket, section, k, dest, *, account, timeout=600):
            Path(dest).write_bytes(b"archive")

        with mock.patch.object(backup.storage, "get_file", side_effect=fake_get):
            return backup.restore_download("p", "us-west-2", "bkt", key, account=ACCOUNT, **kwargs)

    def test_this_installs_own_archive_downloads_with_no_override(self):
        mine = backup.install_identity()["id"]
        key = f"snapshots/{mine}/a.tar.gz"
        # Recorded as uploaded by THIS install, which is what makes it provably
        # ours rather than merely sitting under our prefix.
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, key, 10, ARCHIVE_FINGERPRINT)
        result = self._download(key)
        assert result["origin"] == backup.ORIGIN_SELF
        assert result["install"] == mine

    def test_a_foreign_archive_is_refused_until_the_caller_says_it_means_it(self):
        other = "9" * 32
        key = f"snapshots/{other}/a.tar.gz"
        with pytest.raises(backup.UnprovenArchive):
            self._download(key)
        # Overridable on purpose: on a replacement machine EVERY archive is
        # foreign, which is what disaster recovery IS, so a hard wall would block
        # the one case the backup exists for.
        result = self._download(key, foreign_ok=True)
        assert result["origin"] == backup.ORIGIN_OTHER
        assert result["install"] == other

    def test_a_legacy_archive_is_refused_too_until_the_caller_accepts_it(self):
        # Legacy is not a free pass. An archive with no id segment has an unknown
        # author, and "unknown" includes "somebody else" -- so the same override
        # applies. It stays overridable rather than forbidden because a
        # pre-upgrade archive really may be the operator's own.
        with pytest.raises(backup.UnprovenArchive) as caught:
            self._download("snapshots/kirocrew-snapshot-old.tar.gz")
        assert caught.value.origin == backup.ORIGIN_LEGACY
        result = self._download("snapshots/kirocrew-snapshot-old.tar.gz", foreign_ok=True)
        assert result["origin"] == backup.ORIGIN_LEGACY
        assert result["install"] == ""

    def test_only_a_proven_self_archive_needs_no_override(self):
        # One rule, stated once: prove it is ours, or say you accept it might not
        # be. The earlier draft refused only the foreign case and left the other
        # two to the dashboard, which put a safety property in one client.
        mine = backup.install_identity()["id"]
        proven = f"snapshots/{mine}/proven.tar.gz"
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, proven, 1, ARCHIVE_FINGERPRINT)
        unproven = [
            f"snapshots/{'9' * 32}/theirs.tar.gz",
            f"snapshots/{mine}/not-recorded.tar.gz",
            "snapshots/kirocrew-snapshot-flat.tar.gz",
        ]
        assert self._download(proven)["origin"] == backup.ORIGIN_SELF
        for key in unproven:
            with pytest.raises(backup.UnprovenArchive):
                self._download(key)
            assert self._download(key, foreign_ok=True)["path"]


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
                    "p",
                    "us-west-2",
                    "b",
                    "snapshots/a.tar.gz",
                    account="111122223333",
                    # A flat key is unproven, so the ownership gate would refuse it
                    # first and this test would pass for the wrong reason. The
                    # override gets past that gate so the STAGING guard is what is
                    # actually being exercised.
                    foreign_ok=True,
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
