"""AWS Control backup/costs — the paths the P0 suite's backup classes leave uncovered.

``test_aws_control_app_hardening.py`` already pins the security-critical traversal (symlink
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

This module keeps the ``_authorize_upload`` branches and the two push paths. The
other cases are in sibling modules: the unpinned-walk refusal, ``list_remote_backups``
and the ``restore_download`` guard in ``test_aws_control_backup_ownership.py``;
``due_for_nightly``'s bad stamp and the retry backoff in
``test_aws_control_backup_retry_backoff.py``; the ``costs`` cache and the run state
in ``test_aws_control_backup_run_state.py``; the Layer B gate in
``test_aws_control_backup_layer_b.py`` and ``test_aws_control_backup_layer_b_mid_build.py``;
and the upload records in ``test_aws_control_backup_upload_records.py``. The shared
constants live in ``aws_control_backup_helpers.py``.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path
from unittest import mock

import pytest
from aws_control_backup_helpers import (
    ACCOUNT,
)

from kiro_crew.apps.builtins.aws_control.backend import backup

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
                backup._authorize_upload(
                    ACCOUNT,
                    "p",
                    "us-west-2",
                    caller=backup.CALLER_OWNER,
                    payload_kind=backup.KIND_SNAPSHOT,
                )

    def test_unparseable_sts_output_reads_as_no_account_and_refuses(self):
        # A garbled STS response must not be trusted as a match: it decodes to
        # an empty account, which can never equal the requested one, so the
        # upload is refused rather than proceeding on unknown identity.
        with mock.patch("kiro_crew.deploy.engine._checked", return_value="not json"):
            with pytest.raises(RuntimeError, match="no longer points at"):
                backup._authorize_upload(
                    ACCOUNT,
                    "p",
                    "us-west-2",
                    caller=backup.CALLER_OWNER,
                    payload_kind=backup.KIND_SNAPSHOT,
                )

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
                backup._authorize_upload(
                    ACCOUNT,
                    "p",
                    "us-west-2",
                    caller=backup.CALLER_OWNER,
                    payload_kind=backup.KIND_SNAPSHOT,
                )

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
                backup._authorize_upload(
                    ACCOUNT,
                    "p",
                    "us-west-2",
                    caller=backup.CALLER_OWNER,
                    payload_kind=backup.KIND_SNAPSHOT,
                )

    def test_profile_region_and_account_are_checked_against_one_grant_read(self):
        # The stored grant can change between two reads. The first record matches
        # this profile and region but names another account; the second names this
        # account under another profile. Neither record allows the upload, so the
        # gate must judge one record, not a half of each.
        from kiro_crew import aws_consent

        records = [
            aws_consent.Grant(
                service=aws_consent.SERVICE_S3,
                profile="p",
                region="us-west-2",
                account="999988887777",
                arn="",
                granted_at="",
            ),
            aws_consent.Grant(
                service=aws_consent.SERVICE_S3,
                profile="other",
                region="eu-west-1",
                account=ACCOUNT,
                arn="",
                granted_at="",
            ),
        ]
        with (
            mock.patch(
                "kiro_crew.deploy.engine._checked",
                return_value=json.dumps({"Account": ACCOUNT}),
            ),
            mock.patch("kiro_crew.apps.manager.is_app_enabled", return_value=True),
            mock.patch("kiro_crew.aws_consent.read_grant", side_effect=records) as read,
        ):
            with pytest.raises(RuntimeError, match="does not name this account"):
                backup._authorize_upload(
                    ACCOUNT,
                    "p",
                    "us-west-2",
                    caller=backup.CALLER_OWNER,
                    payload_kind=backup.KIND_SNAPSHOT,
                )
        assert read.call_count == 1


# ---------------------------------------------------------------------------
# run_snapshot_backup / run_sessions_backup — the whole push path
# ---------------------------------------------------------------------------


class TestRunSnapshotBackup:
    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse state pin; no test in this module calls monkeypatch.undo()
    def _isolated_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        # A successful push ends with the retention sweep, which LISTS the drive.
        # Stubbed here rather than per test: the sweep swallows its own failures by
        # design, so an unstubbed test would attempt a real CLI call and still
        # pass. Retention's own behaviour is covered in
        # test_aws_control_backup_retention.py.
        monkeypatch.setattr(backup.storage, "list_object_versions", lambda *a, **k: [])
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
            mock.patch.object(backup.storage, "put_file", return_value="v-test") as put_file,
        ):
            record = backup.run_snapshot_backup(
                ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
            )

        # ONE authorization per S3 operation, and that is the contract: the
        # archive PUT and each of the two label PUTs sit immediately after their
        # own gate, so none of them runs on a decision that went stale during
        # another's round trip.
        #
        # THREE, not four: this fixture configures no retention count, so retention
        # is off and the sweep declines before asking for anything. That is the
        # shipped default, and it is worth pinning as an absence rather than as a
        # smaller number -- an off sweep costs no authorization round trip and no
        # listing call, so a fourth gate appearing here would mean the sweep had
        # started doing cloud work on an install that never asked for it.
        #
        # Each write also NAMES what it carries, which is what lets the gate
        # re-read the unattended grant for the right payload. The archive is the
        # snapshot's payload; the two label writes are one caption published under
        # both prefixes and are nobody's payload, so they name none.
        #
        # The write gates are checked field by field rather than against a whole
        # expected call: they now carry `payload_kind` as well, so an equality
        # against a fixed call would assert the ABSENCE of that argument, which is
        # the opposite of what the line above pins.
        assert authz.call_count == 3
        write_gates = [c for c in authz.call_args_list if "operation" not in c.kwargs]
        assert len(write_gates) == 3
        sweep_gates = [c for c in authz.call_args_list if "operation" in c.kwargs]
        assert sweep_gates == []
        assert [c.kwargs["payload_kind"] for c in authz.call_args_list] == [
            backup.KIND_SNAPSHOT,
            None,
            None,
        ]
        for call in authz.call_args_list:
            assert call.args == (ACCOUNT, "p", "us-west-2")
            assert call.kwargs["caller"] == backup.CALLER_OWNER
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
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse state pin; no test in this module calls monkeypatch.undo()
    def _isolated_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        # See TestRunSnapshotBackup: the push ends with a drive listing, and the
        # sweep swallows its own failures, so leaving it unstubbed would attempt a
        # real CLI call from a passing test.
        monkeypatch.setattr(backup.storage, "list_object_versions", lambda *a, **k: [])
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
        # The kiro-cli conversation export is a third source; this
        # test is about the two transcript halves being empty, so isolate it to
        # None rather than reading whatever store the test host happens to have.
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (None, ""))
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
        # The kiro-cli half is Layer B and rides only on the operator's standing
        # permission, so this both-halves path is asserted WITH that permission
        # granted. `TestSessionsArchiveLayerBGatePermission` owns the withheld direction.
        monkeypatch.setattr(backup, "sessions_layer_b_enabled", lambda account: True)
        # Isolate the kiro-cli conversation export so this test's exact
        # archive-name assertion reflects the two transcript halves only. The
        # export itself is covered in test_aws_control_backup_conversations.py.
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (None, ""))

        pushed: dict[str, str] = {}

        def fake_put(
            profile,
            region,
            bucket,
            section,
            key,
            local_path,
            *,
            account=None,
            timeout=None,
            body_fd=None,
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
        assert record["layer_b"] is True
        assert backup.last_runs(ACCOUNT)[backup.KIND_SESSIONS]["bytes"] > 0
