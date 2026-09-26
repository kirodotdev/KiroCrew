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

import contextlib
import datetime as dt
import errno
import hashlib
import io
import json
import logging
import sqlite3
import tarfile
import threading
import time
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.aws_control.backend import backup, costs
from kiro_crew.config import loader

ACCOUNT = "111122223333"

#: Sentinel for `_store`'s `scope` argument. A sentinel rather than a default string
#: because `None` has to mean "write NO scope marker" -- the legacy grant shape -- and
#: that is a value a caller passes deliberately, not the absence of an argument.
_SCOPED = object()

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


# ---------------------------------------------------------------------------
# run_snapshot_backup / run_sessions_backup — the whole push path
# ---------------------------------------------------------------------------


class TestRunSnapshotBackup:
    @pytest.fixture(autouse=True)
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
        # granted. `TestSessionsArchiveLayerBGate` owns the withheld direction.
        monkeypatch.setattr(backup, "sessions_layer_b_enabled", lambda account: True)
        # Isolate the kiro-cli conversation export so this test's exact
        # archive-name assertion reflects the two transcript halves only. The
        # export itself is covered in test_aws_control_backup_conversations.py.
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (None, ""))

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
        assert record["layer_b"] is True
        assert backup.last_runs(ACCOUNT)[backup.KIND_SESSIONS]["bytes"] > 0


# ---------------------------------------------------------------------------
# Layer B in the sessions archive -- the operator's standing permission
# ---------------------------------------------------------------------------


class TestSessionsArchiveLayerBGate:
    """The kiro-cli half rides only when the operator has permitted it.

    Layer B is the byte-exact unredacted model context window. The archive used
    to carry it because taring the directory reached it, not because any
    permission was consulted, while the file-export path required an operator
    opt-in for the same payload. These pin both directions of the gate and the
    reading a restore depends on.
    """

    @pytest.fixture(autouse=True)
    def _isolated_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        # The terminal conversation export rides the same permission, and these
        # tests assert EXACT archive member lists. Left unstubbed, a host that
        # happens to have a kiro-cli store would add a `conversations/` root on the
        # permitted path and redden an assertion about the transcript halves. The
        # two tests that are about the export point this at a synthetic store of
        # their own.
        #
        # The empty reason here is a STUB CONVENIENCE, not a pair the resolver can
        # return: real absence reports `store_absent`, which suppresses the retention
        # sweep. These tests assert member lists and the permission gate, so the reason
        # is immaterial to them and a reasonless pair keeps them from also asserting
        # suppression. Do not read it as the resolver's contract.
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (None, ""))
        yield

    @staticmethod
    def _synthetic_store(tmp_path, monkeypatch, *, rows: int = 3):
        """Point the export at a synthetic conversation store. Returns its path.

        Shaped like the real one only as far as the export reads it: the
        allowlisted table plus a token-bearing table that must never ride. The
        export's own properties are pinned in
        test_aws_control_backup_conversations.py; what these tests add is whether
        the call site consults the permission at all.
        """
        db = tmp_path / "data.sqlite3"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "CREATE TABLE conversations_v2 (conversation_id TEXT PRIMARY KEY, value TEXT)"
            )
            conn.executemany(
                "INSERT INTO conversations_v2 (conversation_id, value) VALUES (?, ?)",
                [(f"conv-{i}", json.dumps({"turn": i})) for i in range(rows)],
            )
            conn.execute("CREATE TABLE auth_kv (k TEXT, bearer_token TEXT)")
            conn.execute(
                "INSERT INTO auth_kv (k, bearer_token) VALUES (?, ?)",
                ("idc:default", "SECRET-BEARER-TOKEN-must-not-leak"),
            )
            conn.commit()
        finally:
            conn.close()
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (db, ""))
        return db

    @staticmethod
    def _both_halves(tmp_path, monkeypatch):
        """Populate both halves and point the module at them. Returns the cli dir."""
        crew = tmp_path / "crew_home" / backup.SESSIONS_DIR_NAME
        crew.mkdir(parents=True)
        (crew / "t.jsonl").write_bytes(b"transcript\n")
        cli = tmp_path / "cli_sessions"
        cli.mkdir(parents=True)
        # Both Layer B files, named as kiro-cli names them: the envelope and the
        # events blob. Withholding must withhold BOTH, not whichever one a
        # narrower filter happened to match.
        (cli / "abc.json").write_bytes(b"{}\n")
        (cli / "abc.jsonl").write_bytes(b"{}\n")
        monkeypatch.setattr(backup, "data_home", lambda: tmp_path / "crew_home")
        monkeypatch.setattr(backup, "kiro_sessions_dir", lambda: cli)
        return cli

    @staticmethod
    def _store(tmp_path, value, account: str = ACCOUNT, *, scope: Any = _SCOPED) -> None:
        """Write the permission straight into the state document.

        Written as raw JSON rather than through :func:`set_sessions_layer_b`, so a
        malformed or hand-mangled value can be offered to the reader -- which is
        the case the withhold-on-anything-unparseable claim is about, and one the
        boolean-validating writer cannot produce.

        The scope marker is written by default, because a grant made through the
        owner-gated writer carries one and that is the state most of these tests are
        about. Pass ``scope=None`` for a grant recorded without it, which covers the
        ``cli`` half only.
        """
        entry: dict[str, Any] = {backup.SESSIONS_LAYER_B_KEY: value}
        if scope is _SCOPED:
            entry[backup.SESSIONS_LAYER_B_SCOPE_KEY] = (
                backup.SESSIONS_LAYER_B_SCOPE_WITH_CONVERSATIONS
            )
        elif scope is not None:
            entry[backup.SESSIONS_LAYER_B_SCOPE_KEY] = scope
        (tmp_path / "backup.json").write_text(
            json.dumps({"accounts": {account: entry}}),
            encoding="utf-8",
        )

    @staticmethod
    def _run_capturing_names(monkeypatch):
        """Run a sessions backup with the push mocked. Returns (record, names)."""
        captured: dict[str, Any] = {}

        def fake_put(
            profile, region, bucket, section, key, local_path, *, account=None, timeout=None
        ):
            if key.endswith(backup.LABEL_OBJECT_NAME):
                return
            with tarfile.open(local_path) as tar:
                captured["names"] = sorted(tar.getnames())

        with (
            mock.patch.object(backup, "_authorize_upload"),
            mock.patch.object(backup.storage, "put_file", side_effect=fake_put),
        ):
            record = backup.run_sessions_backup(
                ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
            )
        return record, captured.get("names", [])

    def _skip_without_pinning(self):
        if not backup._CAN_PIN_TRAVERSAL:
            pytest.skip(
                "descriptor-pinned traversal is unavailable here, so the backup"
                " refuses by design -- TestRefusalWithoutPinnedTraversal covers that"
            )

    # -- the block path ----------------------------------------------------

    def test_layer_b_is_withheld_by_default(self, tmp_path, monkeypatch):
        """No permission recorded: the transcript rides and the kiro-cli half does not."""
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        # No state file at all is the default an operator who never chose has, so
        # the permission is read through the real function rather than stubbed --
        # this pins the DEFAULT, which is the whole claim.

        record, names = self._run_capturing_names(monkeypatch)

        assert names == ["crew/t.jsonl"]
        # Stated, not inferred from an absent key: this is what tells a reader the
        # archive cannot resume a session with full fidelity.
        assert record["layer_b"] is False

    def test_a_non_boolean_permission_withholds(self, tmp_path, monkeypatch):
        """A value this function cannot understand must not widen what leaves."""
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        # "true" is a string, and `bool("true")` is True -- the exact coercion that
        # would turn a hand-mangled store into an unintended grant.
        self._store(tmp_path, "true")

        record, names = self._run_capturing_names(monkeypatch)

        assert names == ["crew/t.jsonl"]
        assert record["layer_b"] is False

    def test_an_unreadable_store_withholds_rather_than_failing(self, tmp_path, monkeypatch):
        """A state file that cannot be parsed withholds; the backup still runs."""
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        (tmp_path / "backup.json").write_text("{ not json", encoding="utf-8")

        record, names = self._run_capturing_names(monkeypatch)

        assert names == ["crew/t.jsonl"]
        assert record["layer_b"] is False

    # -- the allow path ----------------------------------------------------

    def test_the_permission_lets_both_halves_ride(self, tmp_path, monkeypatch):
        """With the recorded permission the archive carries Layer B, byte-exact."""
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)

        record, names = self._run_capturing_names(monkeypatch)

        assert names == ["cli/abc.json", "cli/abc.jsonl", "crew/t.jsonl"]
        assert record["layer_b"] is True

    # -- the terminal conversation export rides the SAME permission ---------

    def test_withholding_also_withholds_the_conversation_export(self, tmp_path, monkeypatch):
        """A withheld run carries no `conversations/` root, store present or not.

        The export reads the terminal's own conversation store, which is the same
        data class as Layer B -- what a model actually held, unredacted, not what
        was displayed with display-time redaction applied. An export that rode on
        the crew half's terms would put that content in the bucket of an install
        whose operator withheld exactly it, and an object already uploaded cannot
        be un-sent.

        Mutation-verified: drop the `if layer_b` guard at the export call site and
        this test reddens on the `conversations/` members; the permitted direction
        below still passes.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._synthetic_store(tmp_path, monkeypatch)
        # No permission recorded at all -- the default an operator who never chose
        # has, read through the real function rather than stubbed.

        record, names = self._run_capturing_names(monkeypatch)

        assert names == ["crew/t.jsonl"]
        assert record["layer_b"] is False

    def test_the_permission_lets_the_conversation_export_ride(self, tmp_path, monkeypatch):
        """With the permission the `conversations/` root rides and is recorded."""
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._synthetic_store(tmp_path, monkeypatch)
        self._store(tmp_path, True)

        record, names = self._run_capturing_names(monkeypatch)

        assert names == sorted(
            [
                "crew/t.jsonl",
                "cli/abc.json",
                "cli/abc.jsonl",
                backup._CONVERSATIONS_DB_ARCNAME,
                backup._CONVERSATIONS_MANIFEST_ARCNAME,
            ]
        )
        assert record["layer_b"] is True

    def test_an_emitted_but_empty_export_is_not_discarded_as_nothing_to_archive(
        self, tmp_path, monkeypatch
    ):
        """Zero rows but a real `conversations/` root still uploads.

        The export carries a present-but-empty allowlisted table so a restore sees
        the real schema, which means rows and emitted members disagree: two members,
        zero rows. `run_sessions_backup` measures content with both, because
        measuring it by rows alone would let the "no session files to archive" guard
        throw away members the tar already holds.

        Both transcript halves are empty here, so rows alone cannot carry the run
        past that guard -- the members have to.

        MUTATION: guard on `count == 0` alone, or fold members into the row count,
        and this reddens with RuntimeError("no session files to archive").
        """
        self._skip_without_pinning()
        crew = tmp_path / "crew_home" / backup.SESSIONS_DIR_NAME
        crew.mkdir(parents=True)
        empty_cli = tmp_path / "cli_sessions"
        empty_cli.mkdir(parents=True)
        monkeypatch.setattr(backup, "data_home", lambda: tmp_path / "crew_home")
        monkeypatch.setattr(backup, "kiro_sessions_dir", lambda: empty_cli)
        self._synthetic_store(tmp_path, monkeypatch, rows=0)
        self._store(tmp_path, True)

        record, names = self._run_capturing_names(monkeypatch)

        assert names == sorted(
            [backup._CONVERSATIONS_DB_ARCNAME, backup._CONVERSATIONS_MANIFEST_ARCNAME]
        )
        # The archive carries a conversations root, so the record must say so even
        # though no row and no cli file rode.
        assert record["layer_b"] is True

    def test_a_store_refused_for_a_link_is_not_reported_as_absent(self, tmp_path, monkeypatch):
        """A candidate this host HAS but the lookup refuses reports a reason.

        `_kiro_cli_conversation_db` declines a candidate whose leaf or ANY ancestor is
        a link, and the ancestor walk runs from `/` down and includes the home
        directory -- so an ordinary symlinked `~/.local/share`, or a symlinked home,
        takes that exit on every run. Returning a bare `None` for it made "I refused
        to read a store that is there" identical to "there is no store", and only the
        second is safe to prune on: the first means an earlier archive may hold
        conversations this one does not.

        MUTATION: return `None, ""` from the rejection branch and this reddens twice
        -- the reason is absent AND the sweep runs.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)
        monkeypatch.setattr(backup, "_store_relocated_outside_the_fence", lambda: False)
        monkeypatch.setattr(
            backup, "_kiro_cli_conversation_db", lambda: (None, "store_rejected_link")
        )

        with mock.patch.object(backup, "_prune_remote_archives") as prune:
            record, _ = self._run_capturing_names(monkeypatch)

        assert record["conversations_skipped"] == "store_rejected_link"
        assert prune.call_count == 0

    def test_a_declined_sweep_still_files_its_audit_event(self, tmp_path, monkeypatch):
        """Suppressing the sweep suppresses the DELETION, never the audit.

        `_audit_retention` is only reachable from inside `_prune_remote_archives`, so
        skipping the sweep without filing an event would leave the one path in the app
        that erases object versions permanently with no SEL record at all -- and that
        function's contract is that every terminal outcome files one, "including the
        ones that deleted nothing". A decline is a terminal outcome. It is also what
        keeps the accumulation visible: while the condition persists the archives pile
        up past the keep count, and one event per run naming the reason is how an
        auditor sees that instead of inferring it from a sweep that never ran.

        MUTATION: drop the `_audit_retention` call from the suppressed branch and this
        reddens.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)
        monkeypatch.setattr(backup, "_store_relocated_outside_the_fence", lambda: True)
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (None, ""))

        with mock.patch.object(backup, "_prune_remote_archives") as prune:
            with mock.patch.object(backup, "_audit_retention") as audit:
                self._run_capturing_names(monkeypatch)

        assert prune.call_count == 0
        assert audit.call_count == 1
        # The event must name why, or an auditor sees a sweep that did nothing and
        # cannot tell it from one that was never configured.
        assert audit.call_args.kwargs["result"] == "failed"
        assert "store_relocated_outside_fence" in audit.call_args.kwargs["error"]

    def test_a_relocated_store_is_reported_not_silently_skipped(self, tmp_path, monkeypatch):
        """A store the environment re-roots is skipped AND named in the run record.

        Skipping it is the security decision (`test_an_env_relocated_store_is_not
        _consulted` in the conversations suite pins that). Skipping it SILENTLY is a
        separate failure: an operator whose store lives outside home would read a
        successful run and believe the archive holds their terminal conversations
        when it holds none. So the run record carries `conversations_skipped`.

        The detection is a path-set comparison and touches no file, which is why it
        is safe to run against a root outside the fence.

        MUTATION: drop the `_store_relocated_outside_the_fence()` branch and return a
        bare `_ConversationExport(0, 0)`, or stop threading `conversations_skipped`
        into the record, and this reddens on the missing key.
        """
        self._skip_without_pinning()
        crew = tmp_path / "crew_home" / backup.SESSIONS_DIR_NAME
        crew.mkdir(parents=True)
        (crew / "t.jsonl").write_bytes(b"transcript\n")
        cli = tmp_path / "cli_sessions"
        cli.mkdir(parents=True)
        (cli / "abc.json").write_bytes(b"{}\n")
        monkeypatch.setattr(backup, "data_home", lambda: tmp_path / "crew_home")
        monkeypatch.setattr(backup, "kiro_sessions_dir", lambda: cli)
        self._store(tmp_path, True)
        # A relocation the resolver will not read: the env names a root away from
        # home, and no store exists at the fenced location.
        # Pin the FLAG, not the environment. `XDG_DATA_HOME` only makes the detector
        # true on a POSIX-but-not-darwin host: the DARWIN rows of
        # `identity_stores.IDENTITY_STORE_ROOTS` carry `env_var=None`, so on the macOS
        # leg setenv leaves the flag False and every assertion below on
        # `conversations_skipped` would raise KeyError. The subject of this test is what
        # the caller DOES with a relocation, so the detector is stubbed and its own
        # behaviour is tested in `test_aws_control_backup_conversations.py`, which
        # carries the linux guard.
        monkeypatch.setattr(backup, "_store_relocated_outside_the_fence", lambda: True)
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (None, ""))

        record, names = self._run_capturing_names(monkeypatch)

        # The rest of the archive is still correct and still uploads -- the refusal
        # is reported, never escalated into a failed backup.
        assert names == ["cli/abc.json", "crew/t.jsonl"]
        assert record["conversations_skipped"] == "store_relocated_outside_fence"

    def test_a_run_that_skipped_nothing_carries_no_skip_key(self, tmp_path, monkeypatch):
        """No relocation means no `conversations_skipped` key at all.

        Keeps the record's shape unchanged on the common path, so the key's presence
        is itself the signal rather than a value a reader has to interpret.

        MUTATION: always set the key (even empty) and this reddens.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)

        record, names = self._run_capturing_names(monkeypatch)

        assert "conversations_skipped" not in record

    @staticmethod
    def _run_twice_over_an_unchanged_tree(monkeypatch):
        """Run the sessions backup twice. Returns both records.

        The second run can only take the unchanged-skip branch if the baseline probe
        can PROVE the first upload: `_unchanged_baseline` HEADs the recorded key and
        requires the version and length it recorded. So `put_file` returns a version
        and `head_object_meta` answers with what was actually uploaded -- a stub that
        returned nothing would send the second run down the upload path and the test
        would pass for the wrong reason.
        """
        uploaded: dict[str, int] = {}

        def fake_put(
            profile, region, bucket, section, key, local_path, *, account=None, timeout=None
        ):
            if key.endswith(backup.LABEL_OBJECT_NAME):
                return None
            uploaded[key] = Path(local_path).stat().st_size
            return "v1"

        def fake_head(profile, region, bucket, section, key, *, account):
            size = uploaded.get(key)
            return None if size is None else {"ContentLength": size, "VersionId": "v1"}

        records = []
        with (
            mock.patch.object(backup, "_authorize_upload"),
            mock.patch.object(backup.storage, "put_file", side_effect=fake_put),
            mock.patch.object(backup.storage, "head_object_meta", side_effect=fake_head),
        ):
            for _ in range(2):
                records.append(
                    backup.run_sessions_backup(
                        ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
                    )
                )
        return records

    def test_the_skip_key_survives_an_unchanged_run(self, tmp_path, monkeypatch):
        """An unchanged run must not erase the coverage facts of the run before it.

        `_record_skip` REPLACES the run slot, so a field it does not forward is gone.
        The sessions path takes that branch whenever the tree has not moved, which is
        the ordinary nightly case -- so without forwarding, the first unchanged run
        would quietly restore an assertion of complete coverage over a run that had
        reported a gap.

        Runs the same backup twice. The second takes the unchanged-skip branch (it
        reports `uploaded` false), and the skip key must still be there.

        MUTATION: drop `conversations_skipped` from the `_record_skip` call and this
        reddens on the second record.
        """
        self._skip_without_pinning()
        crew = tmp_path / "crew_home" / backup.SESSIONS_DIR_NAME
        crew.mkdir(parents=True)
        (crew / "t.jsonl").write_bytes(b"transcript\n")
        cli = tmp_path / "cli_sessions"
        cli.mkdir(parents=True)
        (cli / "abc.json").write_bytes(b"{}\n")
        monkeypatch.setattr(backup, "data_home", lambda: tmp_path / "crew_home")
        monkeypatch.setattr(backup, "kiro_sessions_dir", lambda: cli)
        self._store(tmp_path, True)
        # Pin the FLAG, not the environment. `XDG_DATA_HOME` only makes the detector
        # true on a POSIX-but-not-darwin host: the DARWIN rows of
        # `identity_stores.IDENTITY_STORE_ROOTS` carry `env_var=None`, so on the macOS
        # leg setenv leaves the flag False and every assertion below on
        # `conversations_skipped` would raise KeyError. The subject of this test is what
        # the caller DOES with a relocation, so the detector is stubbed and its own
        # behaviour is tested in `test_aws_control_backup_conversations.py`, which
        # carries the linux guard.
        monkeypatch.setattr(backup, "_store_relocated_outside_the_fence", lambda: True)

        first, second = self._run_twice_over_an_unchanged_tree(monkeypatch)

        assert first["conversations_skipped"] == "store_relocated_outside_fence"
        assert second.get("uploaded") is False, "second run should take the skip branch"
        assert second["conversations_skipped"] == "store_relocated_outside_fence"
        assert second["layer_b"] is True

    def test_a_stale_store_at_the_fixed_anchor_does_not_mask_a_relocation(
        self, tmp_path, monkeypatch
    ):
        """A leftover at the fixed anchor must not be exported once the env has moved.

        The relocation check runs BEFORE the fixed-candidate lookup. Ordered the other
        way, a file still sitting at the old anchor after the environment says the
        store moved would be exported and the run would report complete coverage,
        because the relocation is only visible when the fixed lookup finds nothing.
        `identity_stores.selected_store` arbitrates this same leftover-in-the-abandoned-
        root state, so it is a state the codebase already expects.

        Here BOTH exist: a readable store at the fixed anchor AND an active relocation.

        MUTATION: move the relocation check back under `if db is None` and this reddens
        -- the stale store's members appear in the archive and the skip key is absent.
        """
        self._skip_without_pinning()
        crew = tmp_path / "crew_home" / backup.SESSIONS_DIR_NAME
        crew.mkdir(parents=True)
        (crew / "t.jsonl").write_bytes(b"transcript\n")
        cli = tmp_path / "cli_sessions"
        cli.mkdir(parents=True)
        monkeypatch.setattr(backup, "data_home", lambda: tmp_path / "crew_home")
        monkeypatch.setattr(backup, "kiro_sessions_dir", lambda: cli)
        # A perfectly readable store at the fixed anchor -- the stale leftover.
        self._synthetic_store(tmp_path, monkeypatch, rows=5)
        self._store(tmp_path, True)
        # And an active relocation saying the real store lives elsewhere.
        # Pin the FLAG, not the environment. `XDG_DATA_HOME` only makes the detector
        # true on a POSIX-but-not-darwin host: the DARWIN rows of
        # `identity_stores.IDENTITY_STORE_ROOTS` carry `env_var=None`, so on the macOS
        # leg setenv leaves the flag False and every assertion below on
        # `conversations_skipped` would raise KeyError. The subject of this test is what
        # the caller DOES with a relocation, so the detector is stubbed and its own
        # behaviour is tested in `test_aws_control_backup_conversations.py`, which
        # carries the linux guard.
        monkeypatch.setattr(backup, "_store_relocated_outside_the_fence", lambda: True)

        record, names = self._run_capturing_names(monkeypatch)

        assert names == ["crew/t.jsonl"], "the stale store must not be exported"
        assert record["conversations_skipped"] == "store_relocated_outside_fence"

    def test_an_unreadable_store_is_named_in_the_record(self, tmp_path, monkeypatch):
        """A store that cannot be read is reported, not silently skipped.

        The same standard the relocation path is held to. An operator whose store went
        unreadable for one run must be able to see that from the record, because the
        archive still uploads and still reports success on its transcript halves.

        MUTATION: return a bare `_ConversationExport(0, 0)` from the
        `except (OSError, sqlite3.Error)` branch and this reddens.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)
        # Not a SQLite file at all, so the read raises inside the export.
        junk = tmp_path / "data.sqlite3"
        junk.write_bytes(b"this is not a database\n")
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (junk, ""))

        record, names = self._run_capturing_names(monkeypatch)

        assert not [n for n in names if n.startswith("conversations/")]
        assert record["conversations_skipped"] == "store_unreadable"

    def test_an_incomplete_export_does_not_let_retention_retire_the_older_archive(
        self, tmp_path, monkeypatch
    ):
        """An export that came up short must not trigger the retention sweep.

        Retention protects only the key THIS run uploaded, so at `keep=1` retiring the
        previous archive erases the one copy that still held the conversations, and
        `delete_object_versions` erases versions outright. The gap here recovers on the
        next successful run; the retired object does not.

        MUTATION: call `_prune_remote_archives` unconditionally and this reddens.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)
        junk = tmp_path / "data.sqlite3"
        junk.write_bytes(b"not a database\n")
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (junk, ""))

        with mock.patch.object(backup, "_prune_remote_archives") as prune:
            record, _ = self._run_capturing_names(monkeypatch)

        assert record["conversations_skipped"] == "store_unreadable"
        assert prune.call_count == 0

    def test_a_relocation_also_stops_retention(self, tmp_path, monkeypatch):
        """A relocation suppresses the sweep too, for the same reason as the others.

        The tempting argument is that a relocation is a standing property of the
        install, so no archive ever held those conversations and pruning loses nothing.
        That premise is false: the flag is read from THIS PROCESS's environment, and a
        daemon-launched run and a shell-launched run can disagree about
        `XDG_DATA_HOME` with the operator relocating nothing. So one run can write a
        complete archive and the next can omit the conversations, and pruning at
        `keep=1` would erase the only copy that held them with no recovery.

        MUTATION: restore the old exclusion -- gate the sweep on
        `conversations.skipped != "store_relocated_outside_fence"` -- and this reddens.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)
        # Pin the FLAG, not the environment. `XDG_DATA_HOME` only makes the detector
        # true on a POSIX-but-not-darwin host: the DARWIN rows of
        # `identity_stores.IDENTITY_STORE_ROOTS` carry `env_var=None`, so on the macOS
        # leg setenv leaves the flag False and every assertion below on
        # `conversations_skipped` would raise KeyError. The subject of this test is what
        # the caller DOES with a relocation, so the detector is stubbed and its own
        # behaviour is tested in `test_aws_control_backup_conversations.py`, which
        # carries the linux guard.
        monkeypatch.setattr(backup, "_store_relocated_outside_the_fence", lambda: True)

        with mock.patch.object(backup, "_prune_remote_archives") as prune:
            record, _ = self._run_capturing_names(monkeypatch)

        assert record["conversations_skipped"] == "store_relocated_outside_fence"
        assert prune.call_count == 0

    def test_a_discovery_failure_reports_and_does_not_fail_the_backup(self, tmp_path, monkeypatch):
        """An exception resolving the store reports a skip; the archive still uploads.

        Both discovery calls reach `Path.home()`, which raises when the home directory
        cannot be determined. Unguarded, that exception would propagate out of
        `run_sessions_backup` and throw away a correct transcript archive over a
        missing sub-member -- the opposite of this function's stated best-effort
        contract.

        Also asserts retention is suppressed, since a discovery failure is transient
        and an earlier archive may hold conversations this one does not.

        MUTATION: remove the try/except around discovery and this reddens with the
        RuntimeError instead of returning a record.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)

        def _boom():
            raise RuntimeError("home directory cannot be determined")

        monkeypatch.setattr(backup, "_store_relocated_outside_the_fence", _boom)

        with mock.patch.object(backup, "_prune_remote_archives") as prune:
            record, names = self._run_capturing_names(monkeypatch)

        assert names == ["cli/abc.json", "cli/abc.jsonl", "crew/t.jsonl"]
        assert record["conversations_skipped"] == "store_discovery_failed"
        assert prune.call_count == 0

    def test_a_run_that_reported_nothing_still_prunes(self, tmp_path, monkeypatch):
        """The suppression must not over-reach: no reason means retention still runs.

        The sweep is suppressed by a predicate on `conversations_skipped`, so this is
        the other direction of that predicate and it needs its own test. The case that
        may stay reasonless is a run that READ everything the host holds, so this one
        exports a real store successfully: nothing an earlier archive could hold is
        missing from this archive, and retention must behave exactly as it did before
        this feature existed.

        A no-store run does NOT belong here, even though it also carries zero rows --
        see `test_an_absent_store_also_stops_retention`. A successful export is the
        case the claim is actually true of.

        MUTATION: suppress unconditionally and this reddens.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._synthetic_store(tmp_path, monkeypatch)
        self._store(tmp_path, True)

        with mock.patch.object(backup, "_prune_remote_archives") as prune:
            record, _ = self._run_capturing_names(monkeypatch)

        assert "conversations_skipped" not in record
        assert prune.call_count == 1
        # And the in-lock re-check is NOT armed for this run. It carried the
        # conversations itself, so it set the fact -- arming the re-check here would make
        # every wide run refuse its own sweep, and retention would never run again on the
        # installs that actually export conversations.
        #
        # MUTATION: pass `recheck_conversations_retained=True` unconditionally from the
        # sessions sweep and this reddens.
        assert prune.call_args.kwargs["recheck_conversations_retained"] is False

    def test_a_grant_without_a_scope_marker_withholds_the_conversations(
        self, tmp_path, monkeypatch
    ):
        """A grant recorded before the export existed covers the `cli` half only.

        The grant is one boolean with no scope in it, so reading it as also authorizing
        `conversations_v2` would ship every interactive kiro-cli use on the host
        off-host on a consent that named this product's session files. An object in a
        bucket cannot be recalled, so the export waits for a re-confirmation through
        the existing owner-gated writer.

        The `cli` half still rides, which is the half this change never touched.

        MUTATION: gate the export on `layer_b` alone instead of on the scope and this
        reddens -- a `conversations/` member appears.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._synthetic_store(tmp_path, monkeypatch)
        self._store(tmp_path, True, scope=None)

        with mock.patch.object(backup, "_prune_remote_archives") as prune:
            record, names = self._run_capturing_names(monkeypatch)

        assert names == ["cli/abc.json", "cli/abc.jsonl", "crew/t.jsonl"]
        # Recorded as the GRANT's scope, never as a skip reason. A skip reason here
        # would suppress the retention sweep on every install that granted Layer B
        # before this export existed, all at once.
        assert record["layer_b_scope"] == "cli"
        assert "conversations_skipped" not in record
        # And pruning is SAFE: no released version wrote an archive carrying
        # conversations, so there is nothing an older archive holds that this run does
        # not. Verified against this PR's base and against main, whose sessions archive
        # has exactly the `crew` and `cli` roots.
        assert prune.call_count == 1

    def test_a_scoped_grant_exports_the_conversations(self, tmp_path, monkeypatch):
        """A grant carrying the scope marker authorizes the export, and it runs.

        This is the other direction of the same gate: re-confirming through the
        existing owner-gated writer stamps the scope, so the operator reaches the wider
        payload without a new endpoint or a new control.

        MUTATION: require any other scope literal and this reddens -- no
        `conversations/` member appears.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._synthetic_store(tmp_path, monkeypatch)
        self._store(tmp_path, True)

        record, names = self._run_capturing_names(monkeypatch)

        assert any(name.startswith("conversations/") for name in names)
        # No scope line: the grant reaches the payload, so there is nothing to explain.
        assert "layer_b_scope" not in record
        assert "conversations_skipped" not in record

    def test_a_legacy_record_can_never_read_as_the_wider_scope(self, tmp_path):
        """Every stored shape but the exact marker reads as `cli`-only.

        This is the whole point of the marker rather than an edge case in it. A stored
        value this code does not understand must not widen what leaves the machine,
        which is the posture `sessions_layer_b_enabled` already takes on an
        unparseable grant.

        MUTATION: accept a truthy marker instead of comparing it exactly, and the
        `"conversations"` and `True` rows redden.
        """
        for stored, covered in (
            (_SCOPED, True),
            (None, False),
            ("cli", False),
            ("conversations", False),
            ("CLI+CONVERSATIONS", False),
            (True, False),
            (["cli", "conversations"], False),
            ({"cli": True}, False),
        ):
            self._store(tmp_path, True, scope=stored)
            assert backup.layer_b_grant_covers_conversations(ACCOUNT) is covered, stored

        # The grant itself still governs: a withheld permission covers nothing, however
        # the scope reads.
        self._store(tmp_path, False)
        assert backup.layer_b_grant_covers_conversations(ACCOUNT) is False

    def test_an_enable_that_names_no_scope_never_widens(self, tmp_path):
        """Only a caller that NAMES the scope gets it, transition or not.

        A transition test -- stamp when the grant goes from off to on -- closes an
        idempotent retry but not a FIRST enable from a client still rendering older
        copy: the operator reads the narrower description and the grant covers the
        whole host. The request itself is the only place the decision can be carried,
        so the scope must be named.

        MUTATION: stamp on any enable, or only on the off-to-on transition, and the
        first assertion reddens.
        """
        # A fresh enable naming nothing. This is the stale-client case.
        backup.set_sessions_layer_b(ACCOUNT, True)
        assert backup.layer_b_grant_covers_conversations(ACCOUNT) is False

        # Named, and only then.
        backup.set_sessions_layer_b(
            ACCOUNT, True, scope=backup.SESSIONS_LAYER_B_SCOPE_WITH_CONVERSATIONS
        )
        assert backup.layer_b_grant_covers_conversations(ACCOUNT) is True

        # An unrecognised name records the narrower grant rather than failing.
        backup.set_sessions_layer_b(ACCOUNT, False)
        backup.set_sessions_layer_b(ACCOUNT, True, scope="cli+everything")
        assert backup.layer_b_grant_covers_conversations(ACCOUNT) is False

    def test_an_unrecognised_scope_clears_an_already_wide_marker(self, tmp_path):
        """Naming a scope this code does not know is not the same as naming none.

        An absent field is no statement about scope, so it leaves the marker alone. A
        caller that NAMED one said what it wanted and it was not the conversation
        export, so an already-wide grant must not stay wide for it -- otherwise the
        grant widens for a request that asked for something else entirely, which is the
        one thing this field exists to stop.

        MUTATION: collapse absent and unrecognised into one branch that leaves the
        marker alone, and this reddens.
        """
        backup.set_sessions_layer_b(
            ACCOUNT, True, scope=backup.SESSIONS_LAYER_B_SCOPE_WITH_CONVERSATIONS
        )
        assert backup.layer_b_grant_covers_conversations(ACCOUNT) is True

        backup.set_sessions_layer_b(ACCOUNT, True, scope="cli+everything")
        assert backup.layer_b_grant_covers_conversations(ACCOUNT) is False

    def test_an_enable_that_names_no_scope_does_not_narrow_either(self, tmp_path):
        """A grant the operator did make survives a caller that forgot to repeat it.

        Absent means "no request to widen", not "request to narrow". Treating a bare
        re-enable as a withdrawal would revoke a real consent on every idempotent retry
        from an older client, which the operator never asked for.

        MUTATION: drop the stored marker when no scope is named and this reddens.
        """
        backup.set_sessions_layer_b(
            ACCOUNT, True, scope=backup.SESSIONS_LAYER_B_SCOPE_WITH_CONVERSATIONS
        )
        backup.set_sessions_layer_b(ACCOUNT, True)
        assert backup.layer_b_grant_covers_conversations(ACCOUNT) is True

    def test_an_unscoped_enable_clears_a_marker_left_on_a_disabled_grant(self, tmp_path):
        """A marker is only valid for the grant that was in force when it was written.

        The document can hold ``enabled=false`` together with a marker, because the two
        keys are stored independently and a state file is written by whichever build of
        this app is installed at the time. Preserving the marker on any
        unscoped enable then turns a bare ``{"enabled": true}`` into a host-wide grant
        the operator never named, and the archive that follows cannot be recalled.

        So an enable that RE-ESTABLISHES the grant clears it, and only an already-on
        grant may keep it -- which
        ``test_an_enable_that_names_no_scope_does_not_narrow_either`` pins separately.

        MUTATION: preserve the marker whenever no scope is named, and this reddens while
        that sibling test stays green.
        """
        self._store(tmp_path, False)
        backup.set_sessions_layer_b(ACCOUNT, True)
        assert backup.layer_b_grant_covers_conversations(ACCOUNT) is False

    def test_an_unscoped_enable_over_a_corrupted_grant_clears_the_marker(self, tmp_path):
        """A stored value the reader will not accept as ON is a transition, not a keep.

        The reader requires the grant to be exactly ``True``, so a corrupted value is
        OFF to every consumer. Deciding "was it already on" by truthiness instead would
        let such a document count as on and keep a marker the operator cannot be shown
        to have named, which is the unsafe direction for an off-host upload.

        MUTATION: compare the previous value by truthiness rather than ``is True``, and
        this reddens.
        """
        self._store(tmp_path, "yes")
        backup.set_sessions_layer_b(ACCOUNT, True)
        assert backup.layer_b_grant_covers_conversations(ACCOUNT) is False

    def test_a_withdrawal_reports_withheld_even_with_a_marker_present(self, tmp_path, monkeypatch):
        """A withdrawn grant covers nothing, whatever a marker says.

        `_audit_layer_b_grant` reads the resulting marker, and today the disable branch
        removes it, so the event happens to be truthful. That makes the event's
        correctness depend on a decision taken elsewhere in the same function rather
        than on the pair `layer_b_grant_covers_conversations` actually reads. An event
        reporting `conversations=allowed` for a withdrawal would misstate the one thing
        a consent review comes to it for.

        Driven through the writer with a marker already stored, so the audit sees the
        combination the coupling hides.

        MUTATION: derive `covered` from the marker alone and this reddens.
        """
        events: list[dict[str, Any]] = []

        class _Sel:
            def log_api_access(self, **kw: Any) -> None:
                events.append(kw)

        self._store(tmp_path, True)
        monkeypatch.setattr(backup, "sel", lambda: _Sel())
        monkeypatch.setattr(
            backup,
            "_locked_state_update",
            lambda mutate: mutate({"accounts": {ACCOUNT: {}}}),
        )
        backup._audit_layer_b_grant(
            ACCOUNT, False, backup.SESSIONS_LAYER_B_SCOPE_WITH_CONVERSATIONS
        )

        grants = [e for e in events if e["operation"] == "aws_control.backup_layer_b_grant"]
        assert len(grants) == 1
        assert "grant=withdrawn" in grants[0]["resources"]
        assert "conversations=withheld" in grants[0]["resources"]

    def test_the_grant_write_is_audited_in_both_directions(self, tmp_path, monkeypatch):
        """A widening and a narrowing both file a SEL event naming what was decided.

        The route's own event records the operation and the path, not which way the
        decision went, so learning what the grant became would mean reading the state
        file -- the on-disk dependency the decision audit exists to remove. A narrowing
        is filed on the same footing: a review reconstructing what an archive was
        allowed to carry needs the revocation as much as the grant.

        MUTATION: drop the `_audit_layer_b_grant` call, or file only the enable, and
        this reddens.
        """
        events: list[dict[str, Any]] = []

        class _Sel:
            def log_api_access(self, **kw: Any) -> None:
                events.append(kw)

        monkeypatch.setattr(backup, "sel", lambda: _Sel())
        backup.set_sessions_layer_b(
            ACCOUNT, True, scope=backup.SESSIONS_LAYER_B_SCOPE_WITH_CONVERSATIONS
        )
        backup.set_sessions_layer_b(ACCOUNT, False)

        grants = [e for e in events if e["operation"] == "aws_control.backup_layer_b_grant"]
        assert len(grants) == 2
        assert "grant=granted" in grants[0]["resources"]
        assert "conversations=allowed" in grants[0]["resources"]
        assert "grant=withdrawn" in grants[1]["resources"]
        assert "conversations=withheld" in grants[1]["resources"]

    def test_the_scope_decision_reaches_the_sel_event(self, tmp_path, monkeypatch):
        """The audit must answer the consent question without the run record.

        That is the whole reason this event exists, so an event saying only
        `layer_b=allowed` describes a run that shipped the terminal conversations
        identically to one that withheld them. The run record carries `layer_b_scope`,
        but reading the scope from disk would put the audit back on the dependency it is
        here to remove.

        MUTATION: drop `conversations` from the event's `resources` and this reddens.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._synthetic_store(tmp_path, monkeypatch)
        self._store(tmp_path, True, scope=None)

        events: list[dict[str, Any]] = []

        class _Sel:
            def log_api_access(self, **kw: Any) -> None:
                events.append(kw)

        monkeypatch.setattr(backup, "sel", lambda: _Sel())
        self._run_capturing_names(monkeypatch)

        decisions = [e for e in events if e["operation"] == "aws_control.backup_layer_b_decision"]
        assert len(decisions) == 1
        assert "layer_b=allowed" in decisions[0]["resources"]
        assert "conversations=withheld" in decisions[0]["resources"]

    def test_the_writer_stamps_the_scope_and_a_disable_drops_it(self, tmp_path):
        """A named scope is stored under the exact on-disk key, and a disable drops it.

        No new endpoint and no new control: the owner-gated path that already records
        the decision carries the scope as a field of the same request. A disable removes
        the marker so a later enable cannot inherit a scope from a decision that was
        withdrawn.

        MUTATION: stop stamping a named scope and the second assertion reddens; stop
        dropping it on disable and the last reddens.
        """
        assert backup.layer_b_grant_covers_conversations(ACCOUNT) is False
        backup.set_sessions_layer_b(
            ACCOUNT, True, scope=backup.SESSIONS_LAYER_B_SCOPE_WITH_CONVERSATIONS
        )
        assert backup.layer_b_grant_covers_conversations(ACCOUNT) is True

        # The stored value is pinned as a LITERAL, not through the constant, because it
        # is an on-disk contract. Comparing it to the constant only proves the writer
        # and the reader agree with each other -- they would still agree after the
        # literal changed, and every grant an operator had already re-confirmed would
        # then read as legacy and silently stop exporting conversations.
        state = json.loads((tmp_path / "backup.json").read_text(encoding="utf-8"))
        assert state["accounts"][ACCOUNT][backup.SESSIONS_LAYER_B_SCOPE_KEY] == "cli+conversations"

        backup.set_sessions_layer_b(ACCOUNT, False)
        state = json.loads((tmp_path / "backup.json").read_text(encoding="utf-8"))
        assert backup.SESSIONS_LAYER_B_SCOPE_KEY not in state["accounts"][ACCOUNT]

    def test_the_scope_line_survives_an_unchanged_run(self, tmp_path, monkeypatch):
        """An unchanged run must not erase the scope line either.

        `_record_skip` REPLACES the run slot, so a coverage fact it does not forward is
        gone -- the same trap `conversations_skipped` fell into. Without forwarding, the
        first ordinary nightly run would drop the one line explaining why a
        Layer-B-enabled archive holds no conversations.

        MUTATION: drop `layer_b_scope` from the `_record_skip` call and this reddens on
        the second record.
        """
        self._skip_without_pinning()
        crew = tmp_path / "crew_home" / backup.SESSIONS_DIR_NAME
        crew.mkdir(parents=True)
        (crew / "t.jsonl").write_bytes(b"transcript\n")
        cli = tmp_path / "cli_sessions"
        cli.mkdir(parents=True)
        (cli / "abc.json").write_bytes(b"{}\n")
        monkeypatch.setattr(backup, "data_home", lambda: tmp_path / "crew_home")
        monkeypatch.setattr(backup, "kiro_sessions_dir", lambda: cli)
        self._store(tmp_path, True, scope=None)

        first, second = self._run_twice_over_an_unchanged_tree(monkeypatch)

        assert first["layer_b_scope"] == "cli"
        assert second["layer_b_scope"] == "cli"
        assert second["uploaded"] is False

    def test_a_conversation_bearing_run_records_the_fact_and_still_prunes(
        self, tmp_path, monkeypatch
    ):
        """An archive that carries conversations is recorded, and pruning still runs.

        Recording it is what lets a LATER run know an older archive is the only copy.
        This run prunes normally, and that is the point rather than an exception: the
        newest archive holds the conversations, so retiring older ones loses nothing --
        which is what keeps retention from freezing forever once the fact is set.

        MUTATION: stop passing `conversations_retained` and the first assertion reddens.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._synthetic_store(tmp_path, monkeypatch)
        self._store(tmp_path, True)

        with mock.patch.object(backup, "_prune_remote_archives") as prune:
            self._run_capturing_names(monkeypatch)

        state = json.loads((tmp_path / "backup.json").read_text(encoding="utf-8"))
        assert state["accounts"][ACCOUNT][backup.SESSIONS_CONVERSATIONS_RETAINED_KEY] is True
        assert prune.call_count == 1

    def test_a_run_with_no_conversations_keeps_an_older_archive_that_has_them(
        self, tmp_path, monkeypatch
    ):
        """The scope can narrow with nothing wrong, and the sweep must not retire the copy.

        An in-scope run uploads conversations; the grant's scope is then narrowed; the
        next run's archive omits them. That path sets NO skip reason -- a narrowed scope
        is a policy decline, not a failed read -- so the `skipped` suppression cannot see
        it, and at `keep=1` the sweep would retire the only archive holding them with no
        recovery.

        The reader is stubbed because this pins what the CALLER does with the fact; the
        fact's own recording is pinned by
        `test_a_conversation_bearing_run_records_the_fact_and_still_prunes`.

        MUTATION: remove the second condition and this reddens, while
        `test_an_absent_store_also_stops_retention` stays green -- the two conditions are
        independent.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._synthetic_store(tmp_path, monkeypatch)
        # A legacy grant: Layer B on, scope not covering conversations, so this run
        # carries none and reports no skip reason.
        self._store(tmp_path, True, scope=None)
        monkeypatch.setattr(backup, "a_retained_archive_carries_conversations", lambda _a: True)

        with mock.patch.object(backup, "_prune_remote_archives") as prune:
            record, names = self._run_capturing_names(monkeypatch)

        assert not any(name.startswith("conversations/") for name in names)
        assert "conversations_skipped" not in record
        assert prune.call_count == 0

    def test_a_run_with_no_conversations_and_none_retained_still_prunes(
        self, tmp_path, monkeypatch
    ):
        """The second condition must not over-suppress either.

        An install that never carried conversations has nothing for an older archive to
        hold, so retention behaves exactly as it did before this feature existed. Without
        this direction the new condition would freeze retention on every install that
        has Layer B on and no conversation scope, which is the unbounded accumulation the
        suppression exists to prevent rather than an instance of it.

        MUTATION: suppress whenever this run carries no conversations, regardless of the
        fact, and this reddens.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._synthetic_store(tmp_path, monkeypatch)
        self._store(tmp_path, True, scope=None)
        monkeypatch.setattr(backup, "a_retained_archive_carries_conversations", lambda _a: False)

        with mock.patch.object(backup, "_prune_remote_archives") as prune:
            record, _ = self._run_capturing_names(monkeypatch)

        assert "conversations_skipped" not in record
        assert prune.call_count == 1

    def test_an_absent_store_also_stops_retention(self, tmp_path, monkeypatch):
        """No store anywhere suppresses the sweep, because an earlier archive may hold.

        The tempting argument is that a host with no store has no conversations, so
        pruning loses nothing. It reasons about the store's state NOW, while retention
        decides the fate of an archive written EARLIER. A store wiped to clear
        corruption, dropped by a reinstall, or on a volume not mounted at nightly-run
        time was present when that archive was written, so at `keep=1` the sweep would
        erase the only copy holding those rows, and `delete_object_versions` leaves no
        recovery.

        MUTATION: carve this reason out of the caller's guard -- gate the sweep on
        `conversations.skipped != "store_absent"` -- and this reddens. That the resolver
        reports absence at all is pinned separately, by
        `test_a_host_with_no_store_reports_absence` in
        test_aws_control_backup_conversations.py, because this class stubs the resolver
        out by design.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)
        monkeypatch.setattr(backup, "_store_relocated_outside_the_fence", lambda: False)
        monkeypatch.setattr(backup, "_kiro_cli_conversation_db", lambda: (None, "store_absent"))

        with mock.patch.object(backup, "_prune_remote_archives") as prune:
            record, _ = self._run_capturing_names(monkeypatch)

        assert record["conversations_skipped"] == "store_absent"
        assert prune.call_count == 0

    def test_conversation_rows_alone_are_recorded_as_layer_b(self, tmp_path, monkeypatch):
        """An empty kiro-cli directory still records Layer B when rows rode.

        The record must describe the ARCHIVE. A permitted run on a host whose
        kiro-cli session directory is empty adds no `cli/` member but still carries
        unredacted terminal context under `conversations/`, so a record reading
        `layer_b=False` beside those rows would send a restore looking for a
        fidelity the object holds and describe one it does not.

        Mutation-verified: record `layer_b_files > 0` alone and this test reddens.
        """
        self._skip_without_pinning()
        crew = tmp_path / "crew_home" / backup.SESSIONS_DIR_NAME
        crew.mkdir(parents=True)
        (crew / "t.jsonl").write_bytes(b"transcript\n")
        empty_cli = tmp_path / "cli_sessions"
        empty_cli.mkdir(parents=True)
        monkeypatch.setattr(backup, "data_home", lambda: tmp_path / "crew_home")
        monkeypatch.setattr(backup, "kiro_sessions_dir", lambda: empty_cli)
        self._synthetic_store(tmp_path, monkeypatch)
        self._store(tmp_path, True)

        record, names = self._run_capturing_names(monkeypatch)

        assert not [n for n in names if n.startswith("cli/")]
        assert backup._CONVERSATIONS_DB_ARCNAME in names
        assert record["layer_b"] is True

    # -- the permission is NOT reachable from agent-writable config ---------

    def test_an_agent_writable_config_key_cannot_grant_layer_b(self, tmp_path, monkeypatch):
        """``config.json`` must not be able to self-grant this permission.

        ``config.json`` is writable by any auto-approved agent shell, so a
        permission honoured from there is one a prompt-injected agent can grant
        itself -- and the resulting upload of unredacted model context cannot be
        recalled. Every spelling this gate might read is offered here at once, so
        moving the read into ``config.json`` under any of them reddens this test.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        monkeypatch.setattr(
            loader,
            "_raw_config",
            lambda: {
                "dashboard": {
                    "backup_include_layer_b": True,
                    "sessions_include_layer_b": True,
                    backup.SESSIONS_LAYER_B_KEY: True,
                }
            },
        )

        record, names = self._run_capturing_names(monkeypatch)

        assert names == ["crew/t.jsonl"]
        assert record["layer_b"] is False

    def test_the_export_permission_does_not_grant_the_backup(self, tmp_path, monkeypatch):
        """Two destinations, two decisions: the export key must not carry here.

        A downloaded file can be handed to another person; this archive lands in a
        bucket the operator owns. Sharing one answer would mean an operator who
        enabled the file export silently enabled unredacted context into their
        bucket as well.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        monkeypatch.setattr(
            loader, "_raw_config", lambda: {"dashboard": {"export_include_layer_b": True}}
        )

        record, names = self._run_capturing_names(monkeypatch)

        assert names == ["crew/t.jsonl"]
        assert record["layer_b"] is False

    # -- the grant is per account ------------------------------------------

    def test_a_grant_for_one_account_does_not_carry_to_another(self, tmp_path, monkeypatch):
        """The risk this permission prices is the destination bucket.

        Granting for one account must not grant for another the operator adds
        later, so the answer is stored per account like the nightly bit beside it.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True, account="999999999999")

        record, names = self._run_capturing_names(monkeypatch)

        assert names == ["crew/t.jsonl"]
        assert record["layer_b"] is False

    # -- the permission lives behind the agent file floor -------------------

    def test_the_permission_lives_inside_the_fenced_state_directory(self):
        """The store must sit on the keystone floor, not in agent-writable config.

        ``STATE_DIR_LEAF`` is registered in ``security._CREW_SECRET_LEAVES``, so an
        agent's file tools and shell forms refuse every path in there -- which is
        what makes this a permission rather than a preference. A move out of that
        directory would silently un-protect it, so the placement is pinned.
        """
        from kiro_crew import security

        assert backup.STATE_DIR_LEAF in security._CREW_SECRET_LEAVES
        # Read through ``app_data_dir`` rather than ``_state_path``, which this
        # class's fixture redirects into ``tmp_path``. The leaf is always
        # '/'-joined (a catalog key, not a local path), so compare against the
        # posix form or this fails on Windows.
        assert backup.app_data_dir(backup.APP_NAME).as_posix().endswith(backup.STATE_DIR_LEAF)

    # -- the writer ---------------------------------------------------------

    def test_the_writer_records_both_directions(self, tmp_path):
        """``set_sessions_layer_b`` is the recorded answer the reader reads back."""
        assert backup.sessions_layer_b_enabled(ACCOUNT) is False
        backup.set_sessions_layer_b(ACCOUNT, True)
        assert backup.sessions_layer_b_enabled(ACCOUNT) is True
        backup.set_sessions_layer_b(ACCOUNT, False)
        assert backup.sessions_layer_b_enabled(ACCOUNT) is False

    def test_the_writer_keeps_the_nightly_bit_beside_it(self, tmp_path):
        """Recording one permission must not publish over the other.

        Both live in the same account sub-dict, and the write is a
        read-modify-write of the whole document, so a base that dropped the
        sibling would silently disable unattended backups.
        """
        backup.set_nightly(ACCOUNT, True)
        backup.set_sessions_layer_b(ACCOUNT, True)
        assert backup.nightly_enabled(ACCOUNT) is True
        assert backup.sessions_layer_b_enabled(ACCOUNT) is True

    # -- a revocation landing mid-build must not ship ------------------------

    def test_a_revocation_during_the_build_refuses_the_upload(self, tmp_path, monkeypatch):
        """Enabled at the read, revoked while the tar is written: nothing ships.

        The permission is read once so the archive and its record cannot
        disagree, which leaves a window: these bytes would otherwise upload under
        a permission the operator has withdrawn, and an object in a bucket cannot
        be recalled. Refusing closes the window without breaking the invariant --
        nothing is uploaded and nothing is recorded, so there is no record to
        disagree with anything.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        answers = iter([True, False])

        monkeypatch.setattr(backup, "sessions_layer_b_enabled", lambda account: next(answers))
        uploaded: list[str] = []

        def fake_put(
            profile, region, bucket, section, key, local_path, *, account=None, timeout=None
        ):
            uploaded.append(key)

        with (
            mock.patch.object(backup, "_authorize_upload"),
            mock.patch.object(backup.storage, "put_file", side_effect=fake_put),
            mock.patch.object(backup, "_record_run") as record_run,
        ):
            with pytest.raises(RuntimeError, match="withdrawn while this archive"):
                backup.run_sessions_backup(
                    ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
                )

        # Refused BEFORE any byte left and with no run record written -- a record
        # here would tell a reader an archive exists that does not.
        assert uploaded == []
        record_run.assert_not_called()

    def test_a_grant_landing_during_the_build_does_not_refuse(self, tmp_path, monkeypatch):
        """Only the revoked direction refuses.

        A grant arriving mid-build leaves an archive without Layer B, which is the
        withholding default and needs no refusal: the next run picks the grant up.
        Refusing here would turn an operator enabling the feature into a failed
        backup.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        answers = iter([False, True])
        monkeypatch.setattr(backup, "sessions_layer_b_enabled", lambda account: next(answers))

        record, names = self._run_capturing_names(monkeypatch)

        assert names == ["crew/t.jsonl"]
        assert record["layer_b"] is False

    def test_a_scope_withdrawn_mid_build_refuses_the_upload(self, tmp_path, monkeypatch):
        """The grant staying ON does not mean it still covers the conversations.

        A disable followed by an enable that names no scope leaves the permission on
        with the marker gone, so the grant recheck passes while the conversations
        already written into this tar sit outside what the grant now covers -- and the
        object cannot be recalled once it is PUT.

        MUTATION: drop the scope half of the recheck and this reddens; the archive
        uploads.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._synthetic_store(tmp_path, monkeypatch)
        self._store(tmp_path, True)

        # Covered when the run starts, withdrawn by the time the recheck runs. The grant
        # itself never goes off, so only the scope half can catch this.
        calls: list[int] = []

        def _covers(account):
            calls.append(1)
            return len(calls) == 1

        monkeypatch.setattr(backup, "layer_b_grant_covers_conversations", _covers)

        with (
            mock.patch.object(backup, "_authorize_upload"),
            mock.patch.object(backup.storage, "put_file") as put,
        ):
            with pytest.raises(RuntimeError, match="conversation scope was withdrawn"):
                backup.run_sessions_backup(
                    ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
                )

        assert backup.sessions_layer_b_enabled(ACCOUNT) is True
        put.assert_not_called()

    def test_the_withdrawal_check_runs_after_the_authorization_call(self, tmp_path, monkeypatch):
        """The last thing before the upload, so no network call sits in the window.

        ``_authorize_upload`` goes to the network. A check placed in front of it
        leaves that whole round trip inside the window it exists to close: the
        permission can be withdrawn while the authorization is in flight and the
        bytes still ship. Pinned as an ORDER, because both orders pass every
        other test in this class.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        order: list[str] = []

        # Withdrawn only AFTER the authorization has run. With the check in front
        # of the authorization it still reads True and the archive uploads.
        def _reader(account):
            return "authorize" not in order

        monkeypatch.setattr(backup, "sessions_layer_b_enabled", _reader)

        with (
            mock.patch.object(
                backup, "_authorize_upload", side_effect=lambda *a, **k: order.append("authorize")
            ),
            mock.patch.object(
                backup.storage, "put_file", side_effect=lambda *a, **k: order.append("upload")
            ),
        ):
            with pytest.raises(RuntimeError, match="withdrawn while this archive"):
                backup.run_sessions_backup(
                    ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
                )

        assert order == ["authorize"]

    def test_the_upload_holds_the_setter_lock_across_the_permission_read(
        self, tmp_path, monkeypatch
    ):
        """A withdrawal cannot overtake the upload it was meant to stop.

        Rechecking the permission is not enough on its own: the reader takes no
        lock, so a withdrawal committing between the recheck and ``put_file``
        still shipped the bytes, and bytes in a bucket cannot be recalled.
        Holding the SETTER'S own lock across both makes the withdrawal land wholly
        before the block or wholly after it.

        The setter's lock is the sidecar FILE lock, taken exclusively by
        ``_state_lock`` (through ``set_sessions_layer_b`` ->
        ``_locked_state_update``). ``_upload_lock`` holds that same file lock and
        deliberately NOT ``_run_lock``, so this probes the file lock -- a
        concurrent revocation is another thread taking it, so its availability
        answers whether this call sits inside the setter's critical section.
        Non-blocking on purpose: a blocking acquisition would deadlock against the
        very lock this asserts is held.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)
        seen: dict[str, bool] = {}

        def _file_lock_is_free_to_another_thread() -> bool:
            answer: dict[str, bool] = {}
            lock_path = backup._state_path().with_suffix(".lock")

            def probe() -> None:
                # A non-blocking exclusive take of the setter's file lock. It is a
                # fresh descriptor, so this is exactly the contention a concurrent
                # `set_sessions_layer_b` in another process or thread would meet.
                try:
                    with backup.open_lock_file(lock_path) as fd:
                        with backup.file_lock(fd, exclusive=True, wait=False):
                            answer["free"] = True
                except OSError:
                    # BlockingIOError (an OSError) is the "held by someone else"
                    # signal wait=False raises; anything else also means not free.
                    answer["free"] = False

            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(timeout=5)
            assert not thread.is_alive(), "the probe thread blocked instead of answering"
            return answer["free"]

        def fake_put(
            profile, region, bucket, section, key, local_path, *, account=None, timeout=None
        ):
            # Keyed by which object is being written. The label is uploaded after
            # the lock is released, on purpose -- a caption must not hold the
            # permission's critical section -- so reading only "the last put"
            # would report the label's answer and pass with no lock at all.
            which = "label" if key.endswith(backup.LABEL_OBJECT_NAME) else "archive"
            seen[which] = _file_lock_is_free_to_another_thread()

        with (
            mock.patch.object(backup, "_authorize_upload"),
            mock.patch.object(backup.storage, "put_file", side_effect=fake_put),
        ):
            record = backup.run_sessions_backup(
                ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
            )

        # The archive key was written at all, so this is the ALLOW path rather than
        # a refusal that never reached the upload.
        assert seen["archive"] is False
        assert record["layer_b"] is True

    def test_an_attended_withheld_upload_does_not_hold_the_setter_lock(self, tmp_path, monkeypatch):
        """An owner-initiated WITHHELD run uploads without holding the state lock.

        The mirror of the sibling above, and the one shape with no permission read
        left inside the block. The lock exists to order this block against the
        SETTERS. The recheck it orders is
        ``layer_b and not sessions_layer_b_enabled(account)``, which short-circuits
        on its first operand when ``layer_b`` is False; the unattended grant, the
        other read a setter can overtake, is re-read for ``CALLER_SCHEDULED`` alone
        and so is not read here. What is left -- ``is_app_enabled``,
        ``aws_consent``, STS -- lives outside this module's state file. An exclusive
        hold would order nothing.

        Scoped to ``CALLER_OWNER`` deliberately, and pinned from the other side by
        ``test_a_scheduled_withheld_upload_holds_the_setter_lock``: a scheduled run
        reaches this block with ``layer_b`` False too, and it must still hold.

        What the hold would cost is measured across accounts. The lock file is
        ``_state_path()``'s sidecar, one path for every account, so a hold here puts
        every other account's state write behind this upload for up to
        ``_STATE_LOCK_TIMEOUT_SECS`` -- including the toggles an operator reaches
        for, ``set_sessions_layer_b`` and ``set_retention_keep``. Probed the same
        way as the sibling, from another thread on a fresh descriptor and without
        blocking, so the answer is exactly the contention a concurrent writer meets.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, False)
        seen: dict[str, bool] = {}

        def _file_lock_is_free_to_another_thread() -> bool:
            answer: dict[str, bool] = {}
            lock_path = backup._state_path().with_suffix(".lock")

            def probe() -> None:
                try:
                    with backup.open_lock_file(lock_path) as fd:
                        with backup.file_lock(fd, exclusive=True, wait=False):
                            answer["free"] = True
                except OSError:
                    answer["free"] = False

            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(timeout=5)
            assert not thread.is_alive(), "the probe thread blocked instead of answering"
            return answer["free"]

        def fake_put(
            profile, region, bucket, section, key, local_path, *, account=None, timeout=None
        ):
            # Keyed the same way as the sibling: the label is uploaded after the
            # block, so reading "the last put" would pass with any lock at all.
            which = "label" if key.endswith(backup.LABEL_OBJECT_NAME) else "archive"
            seen[which] = _file_lock_is_free_to_another_thread()

        with (
            mock.patch.object(backup, "_authorize_upload"),
            mock.patch.object(backup.storage, "put_file", side_effect=fake_put),
        ):
            record = backup.run_sessions_backup(
                ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
            )

        assert seen["archive"] is True, "the withheld upload held the state lock"
        # The withheld path, and one that reached the upload: the crew half alone
        # rode, so this is not a refusal that never got as far as `put_file`.
        assert record["layer_b"] is False

    def test_a_scheduled_withheld_upload_holds_the_setter_lock(self, tmp_path, monkeypatch):
        """A SCHEDULED withheld run holds the lock, because it still has a grant to lose.

        The withheld default does not make this block safe to leave unlocked. The
        crew display half rides on every run, permitted or not, and for a scheduled
        caller that half is authorized by the unattended grant, which
        ``_authorize_upload`` re-reads inside this block and
        ``set_nightly_sessions`` writes under this very sidecar lock. Unlocked, a
        revocation committing between that read and ``put_file`` is not ordered
        against the PUT, and the transcript ships after the owner withdrew the
        grant -- an exposure with no recovery, since an object on S3 cannot be
        taken back.

        The counterpart of
        ``test_an_attended_withheld_upload_does_not_hold_the_setter_lock``: same
        withheld archive, same probe, opposite answer, and the caller is the only
        difference between them. Discriminating in the direction that matters -- a
        predicate keyed on ``layer_b`` alone fails this test.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, False)
        seen: dict[str, bool] = {}

        def _file_lock_is_free_to_another_thread() -> bool:
            answer: dict[str, bool] = {}
            lock_path = backup._state_path().with_suffix(".lock")

            def probe() -> None:
                try:
                    with backup.open_lock_file(lock_path) as fd:
                        with backup.file_lock(fd, exclusive=True, wait=False):
                            answer["free"] = True
                except OSError:
                    answer["free"] = False

            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(timeout=5)
            assert not thread.is_alive(), "the probe thread blocked instead of answering"
            return answer["free"]

        def fake_put(
            profile, region, bucket, section, key, local_path, *, account=None, timeout=None
        ):
            which = "label" if key.endswith(backup.LABEL_OBJECT_NAME) else "archive"
            seen[which] = _file_lock_is_free_to_another_thread()

        with (
            mock.patch.object(backup, "_authorize_upload"),
            mock.patch.object(backup.storage, "put_file", side_effect=fake_put),
        ):
            record = backup.run_sessions_backup(
                ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_SCHEDULED
            )

        assert seen["archive"] is False, "the scheduled withheld upload left the state lock free"
        assert record["layer_b"] is False

    def test_a_status_read_is_not_blocked_by_an_in_flight_upload(self, tmp_path, monkeypatch):
        """The status surface stays live during a PUT.

        ``last_runs`` takes ``_run_lock`` and the dashboard's backup-status read
        goes through it. Holding ``_run_lock`` across the hour-long PUT would block
        every account's status read behind one account's upload. ``_upload_lock``
        holds only the sidecar file lock, so this asserts
        that from inside ``put_file`` -- while the upload is in flight -- a status
        read completes without blocking. Its counterpart, that the setter STILL
        cannot interleave, is pinned by the sibling test above; together they show
        the scope is exact: the reader is free, the writer is not.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)
        seen: dict[str, bool] = {}

        def _status_read_completes() -> bool:
            answer: dict[str, bool] = {}

            def probe() -> None:
                # The real status read. It must return, not park, while the upload
                # holds `_upload_lock`. A `_run_lock` held across the PUT would
                # hang this until the join timeout.
                backup.last_runs(ACCOUNT)
                answer["done"] = True

            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(timeout=5)
            return not thread.is_alive() and answer.get("done", False)

        def fake_put(
            profile, region, bucket, section, key, local_path, *, account=None, timeout=None
        ):
            if key.endswith(backup.LABEL_OBJECT_NAME):
                return
            seen["archive"] = _status_read_completes()

        with (
            mock.patch.object(backup, "_authorize_upload"),
            mock.patch.object(backup.storage, "put_file", side_effect=fake_put),
        ):
            backup.run_sessions_backup(ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER)

        assert seen.get("archive") is True, "the status read blocked behind the in-flight upload"

    def test_a_status_read_is_not_blocked_by_a_writer_parked_behind_an_upload(
        self, tmp_path, monkeypatch
    ):
        """The status surface stays live while a WRITER is parked behind the upload.

        The sibling above has no concurrent writer, so it cannot observe this and
        passes either way. Omitting ``_run_lock`` from ``_upload_lock`` is not on its
        own enough to free the status read, because the stall arrives through a
        contending state WRITER rather than through the upload. A ``_state_lock`` that
        took ``_run_lock`` BEFORE parking on the sidecar file lock would leave a second
        account's run finishing during this upload holding ``_run_lock`` for the
        upload's whole duration; ``last_runs`` queues on ``_run_lock``, so every
        account's status read would queue behind that writer, bounded only by
        ``_STATE_LOCK_TIMEOUT_SECS``.

        Driven with a real second-account :func:`_record_run` rather than a hand-held
        lock, so it is the production path that parks. The park point is known
        deterministically rather than slept on: ``file_lock`` is spied on and signals
        when the writer's own thread reaches it, and under the old order ``_run_lock``
        was necessarily already held by then, because ``_state_lock`` took it before
        ``open_lock_file``. So the probe runs at exactly the moment the old order held
        the reader out.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)
        other_account = "444455556666"
        parked = threading.Event()
        real_file_lock = backup.file_lock
        seen: dict[str, bool] = {}
        writer: dict[str, Any] = {}

        @contextlib.contextmanager
        def spy_file_lock(fd, **kwargs):
            if threading.current_thread().name == "lock-order-writer":
                # About to block in the kernel on the lock the upload holds. Under the
                # old order `_run_lock` is already held at this point, because
                # `_state_lock` took it before `open_lock_file`.
                parked.set()
            with real_file_lock(fd, **kwargs):
                yield

        def record_a_second_accounts_run() -> None:
            try:
                backup._record_run(other_account, backup.KIND_SESSIONS, "k", 1, "fp", "v", tree="t")
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                writer["error"] = repr(exc)
            finally:
                # So a writer that failed before reaching the file lock releases the
                # wait below with a reported error instead of a bare timeout.
                parked.set()

        def fake_put(
            profile, region, bucket, section, key, local_path, *, account=None, timeout=None
        ):
            if key.endswith(backup.LABEL_OBJECT_NAME):
                return
            thread = threading.Thread(
                target=record_a_second_accounts_run, name="lock-order-writer", daemon=True
            )
            writer["thread"] = thread
            thread.start()
            assert parked.wait(timeout=10), "the writer never reached the file lock"
            assert "error" not in writer, writer
            answer: dict[str, bool] = {}

            def probe() -> None:
                # The real status read. It must return while the writer sits on the
                # file lock; under the old order it parked on the writer's `_run_lock`.
                backup.last_runs(ACCOUNT)
                answer["done"] = True

            probe_thread = threading.Thread(target=probe, daemon=True)
            probe_thread.start()
            probe_thread.join(timeout=5)
            seen["archive"] = not probe_thread.is_alive() and answer.get("done", False)

        try:
            with (
                mock.patch.object(backup, "_authorize_upload"),
                mock.patch.object(backup, "file_lock", spy_file_lock),
                mock.patch.object(backup.storage, "put_file", side_effect=fake_put),
            ):
                backup.run_sessions_backup(
                    ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
                )
        finally:
            # Bounded, and in `finally`, so a failing assertion above cannot leave a
            # thread parked on a lock with an hour-long ceiling.
            thread = writer.get("thread")
            if thread is not None:
                thread.join(timeout=15)
                assert not thread.is_alive(), (
                    "the parked writer never completed after the upload released the" " file lock"
                )

        assert "error" not in writer, writer
        assert (
            seen.get("archive") is True
        ), "the status read blocked behind a writer parked on the sidecar file lock"

    def test_the_run_sequence_is_bumped_under_the_run_lock(self, tmp_path, monkeypatch):
        """``(process, sequence)`` stays unique now that the callers drop ``_run_lock``.

        The outer ``_run_lock`` in :func:`_record_run` and :func:`_record_skip` was the
        only thing serialising ``_run_sequence``, and removing it is what frees the
        status read -- so the bump has to move under its own narrow hold in the SAME
        change. ``(process, sequence)`` is the identity the compare-and-set inside
        ``mutate`` reads and the pair :func:`_run_is_newer` orders by, so two records
        sharing a sequence would let a stale baseline pass a check it must fail.

        Asserted by holding ``_run_lock`` and showing the bump cannot proceed. It is
        deterministic in both directions: the writer's REQUEST for the lock is the
        signal, and a bump left outside the hold necessarily precedes that request,
        because it is the first statement of :func:`_record_run_locked` and the only
        later acquisition is the one inside :func:`_state_lock`.
        """
        asked = threading.Event()
        real_lock = backup._run_lock

        class _WatchedLock:
            """Delegates to the real ``RLock``, so reentrancy is the real thing."""

            def _note(self) -> None:
                if threading.current_thread().name == "sequence-writer":
                    asked.set()

            def __enter__(self):
                self._note()
                return real_lock.__enter__()

            def __exit__(self, *exc):
                return real_lock.__exit__(*exc)

            def acquire(self, *args, **kwargs):
                self._note()
                return real_lock.acquire(*args, **kwargs)

            def release(self):
                return real_lock.release()

        outcome: dict[str, Any] = {}

        def record_a_run() -> None:
            try:
                backup._record_run(ACCOUNT, backup.KIND_SESSIONS, "k", 1, "fp", "v", tree="t")
                outcome["wrote"] = True
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                outcome["error"] = repr(exc)

        thread = threading.Thread(target=record_a_run, name="sequence-writer", daemon=True)
        before = backup._run_sequence
        try:
            with mock.patch.object(backup, "_run_lock", _WatchedLock()):
                with real_lock:
                    thread.start()
                    assert asked.wait(timeout=10), "the writer never asked for _run_lock"
                    assert backup._run_sequence == before, (
                        "_run_sequence advanced while another thread held _run_lock, so"
                        " the bump is outside the lock and two records can share one"
                        " sequence"
                    )
                thread.join(timeout=15)
        finally:
            thread.join(timeout=15)
            assert not thread.is_alive(), "the writer never completed"

        assert "error" not in outcome, outcome
        assert outcome.get("wrote") is True
        assert backup._run_sequence == before + 1

    def test_a_lower_sequence_run_does_not_clobber_a_newer_one(self, tmp_path, monkeypatch):
        """Write order can differ from sequence order, and the newer run must win.

        ``_run_lock`` is released before the sidecar file lock is taken, so the
        ``_run_sequence`` bump and the state write are not one critical section: two
        same-kind runs in this process can reach the file lock in an order that differs
        from their sequence order. Without a guard the loser persists last and the slot
        holds the older key, tree and ``layer_b``, so the status surface and the next
        baseline comparison both read a superseded run. A manual run overlapping a
        nightly wake for one account reaches this, because the nightly loop calls
        ``work`` directly and so does not take the Job SDK's ``(kind, account)`` dedupe.

        The inversion is produced rather than simulated: the low-sequence writer bumps
        first, parks in ``file_lock``, and is released only after the high-sequence
        writer has committed. Its upload metadata must still be merged, because the
        object is in the bucket either way and retention reads the versions map.
        """
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        real_file_lock = backup.file_lock
        bumped = threading.Event()
        release_low = threading.Event()
        low: dict[str, Any] = {}

        @contextlib.contextmanager
        def spy_file_lock(fd, **kwargs):
            if threading.current_thread().name == "low-seq-writer":
                # Its sequence is already taken; hold it out of the file lock until the
                # higher-sequenced run has committed.
                bumped.set()
                assert release_low.wait(timeout=10), "the high-seq writer never released"
            with real_file_lock(fd, **kwargs):
                yield

        def write_low() -> None:
            try:
                backup._record_run(
                    ACCOUNT, backup.KIND_SESSIONS, "key-low", 1, "fp-low", "v-low", tree="tree-low"
                )
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                low["error"] = repr(exc)

        thread = threading.Thread(target=write_low, name="low-seq-writer", daemon=True)
        try:
            with mock.patch.object(backup, "file_lock", spy_file_lock):
                thread.start()
                assert bumped.wait(timeout=10), "the low-seq writer never took a sequence"
                # Bumps to a HIGHER sequence and commits first.
                high = backup._record_run(
                    ACCOUNT,
                    backup.KIND_SESSIONS,
                    "key-high",
                    2,
                    "fp-high",
                    "v-high",
                    tree="tree-high",
                )
                release_low.set()
                thread.join(timeout=15)
        finally:
            release_low.set()
            thread.join(timeout=15)
            assert not thread.is_alive(), "the low-seq writer never completed"

        assert "error" not in low, low
        stored = backup.last_runs(ACCOUNT)[backup.KIND_SESSIONS]
        assert stored["key"] == "key-high", "a lower-sequence run overwrote a newer one"
        assert stored["tree"] == "tree-high"
        assert stored["sequence"] == high["sequence"]
        # Both objects are really in the bucket, so retention must see both versions
        # even though only one run record survives.
        assert set(backup.uploaded_objects(ACCOUNT)) == {"key-low", "key-high"}
        assert backup.uploaded_versions(ACCOUNT)["key-low"] == "v-low"

    def test_a_superseded_run_keeps_the_nightly_backoff(self, tmp_path, monkeypatch):
        """A run that loses the slot must not end the retry backoff.

        ``_record_run_locked``'s mutate writes the run record into the ``runs`` slot
        under the supersession guard and clears the failure count under that same
        guard, because the two answer one question: a record this document has already
        superseded is not evidence of anything, so it must not retire a count a later
        failure legitimately accumulated. :func:`_merge_pending` states that reason at
        the other place a run record and this clear travel together.

        Gating the clear or leaving it ungated produces identical state everywhere but
        one window, which is why no other case in the suite tells the two apart: the
        winner clears the backoff in its own mutate, so a failure has to land BETWEEN
        the winner's commit and the loser's for the loser to be the one that meets it.

        Reaching that window takes the witness as well. ``record_nightly_failure``
        compares the run slot against the witness read before its attempt, so a failure
        whose witness predates the winner's commit is refused and writes nothing. The
        witness here is therefore read AFTER the winner commits, and the row is asserted
        PRESENT before the loser is released -- without that the assertion below would
        hold for the trivial reason that no row ever existed.

        The inversion is produced rather than simulated, the same way the clobber test
        above builds it: the low-sequence writer bumps first, parks in ``file_lock``, and
        is released only once the high-sequence writer has committed. That is what shows
        the window is reachable by two real runs rather than by an edited document.

        MUTATION: move the ``_clear_nightly_failure`` call in ``_record_run_locked`` back
        out of the ``if not superseded:`` branch and this reddens on the final assertion.
        """
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        real_file_lock = backup.file_lock
        bumped = threading.Event()
        release_low = threading.Event()
        low: dict[str, Any] = {}

        @contextlib.contextmanager
        def spy_file_lock(fd, **kwargs):
            if threading.current_thread().name == "low-seq-writer":
                # Its sequence is already taken; hold it out of the file lock until the
                # higher-sequenced run has committed AND a failure has been recorded.
                bumped.set()
                assert release_low.wait(timeout=10), "the high-seq writer never released"
            with real_file_lock(fd, **kwargs):
                yield

        def write_low() -> None:
            try:
                backup._record_run(
                    ACCOUNT, backup.KIND_SESSIONS, "key-low", 1, "fp-low", "v-low", tree="tree-low"
                )
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                low["error"] = repr(exc)

        thread = threading.Thread(target=write_low, name="low-seq-writer", daemon=True)
        try:
            with mock.patch.object(backup, "file_lock", spy_file_lock):
                thread.start()
                assert bumped.wait(timeout=10), "the low-seq writer never took a sequence"
                # Bumps to a HIGHER sequence and commits first, clearing the backoff in
                # its own mutate -- so the row the loser meets has to be written after.
                backup._record_run(
                    ACCOUNT,
                    backup.KIND_SESSIONS,
                    "key-high",
                    2,
                    "fp-high",
                    "v-high",
                    tree="tree-high",
                )
                witness = backup.nightly_run_witness(ACCOUNT, backup.KIND_SESSIONS)
                assert witness is not None, "the winner left no identifiable run record"
                recorded = backup.record_nightly_failure(
                    ACCOUNT, backup.KIND_SESSIONS, "eio", run_witness=witness
                )
                assert recorded, "the compare-and-set refused a witness read after the commit"
                assert backup.nightly_failures(ACCOUNT).get(backup.KIND_SESSIONS), (
                    "the window was never entered: with no failure row on the account"
                    " there is nothing for the loser to preserve, and this case cannot"
                    " tell the two placements apart"
                )
                release_low.set()
                thread.join(timeout=15)
        finally:
            release_low.set()
            thread.join(timeout=15)
            assert not thread.is_alive(), "the low-seq writer never completed"

        assert "error" not in low, low
        # The supersession really happened, so the run under test is the LOSER's: the
        # slot still holds the winner's record and the loser's write was refused.
        assert backup.last_runs(ACCOUNT)[backup.KIND_SESSIONS]["key"] == "key-high"
        surviving = backup.nightly_failures(ACCOUNT).get(backup.KIND_SESSIONS)
        assert surviving, (
            "a run whose own record was refused as stale retired the failure count"
            " anyway. That count was accumulated by a failure recorded AFTER the"
            " winning run committed, so nothing about it is over -- and the next"
            " nightly wake reads the account as due on the strength of a run that was"
            " too stale to write a key"
        )
        assert surviving["consecutive"] == 1, surviving

    def test_the_authorization_runs_inside_the_lock_it_will_upload_under(
        self, tmp_path, monkeypatch
    ):
        """Consent cannot go stale between the check and the upload.

        Acquiring the lock AFTER authorizing left a blocking wait between them: a
        concurrent account's backup can hold this lock across its own upload, so a
        consent withdrawal landing during that wait was never re-read, because the
        Layer B recheck does not cover consent. Authorizing inside the lock closes
        it, and this pins that ordering rather than the refusal it enables, because
        both orderings pass every other case in this class.

        The lock is the sidecar FILE lock ``_upload_lock`` holds (not ``_run_lock``,
        which this block does not take), so the probe is a non-blocking exclusive
        take of that file lock: unavailable means we are inside the section.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)
        # A LIST, not a single value: `_publish_label` authorizes again after the
        # lock is released, on purpose -- a caption must not hold the permission's
        # critical section -- so reading only "the last authorize" would report the
        # label's answer and pass with no lock around the archive at all.
        seen: list[bool] = []

        def _held_by_us() -> bool:
            answer: dict[str, bool] = {}
            lock_path = backup._state_path().with_suffix(".lock")

            def probe() -> None:
                try:
                    with backup.open_lock_file(lock_path) as fd:
                        with backup.file_lock(fd, exclusive=True, wait=False):
                            answer["free"] = True
                except OSError:
                    # BlockingIOError (an OSError) is the "held by someone else"
                    # signal wait=False raises; anything else also means not free.
                    answer["free"] = False

            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(timeout=5)
            assert not thread.is_alive(), "the probe thread blocked instead of answering"
            return not answer["free"]

        def fake_authorize(*args, **kwargs):
            seen.append(_held_by_us())

        with (
            mock.patch.object(backup, "_authorize_upload", side_effect=fake_authorize),
            # `return_value`, not a bare stub: `put_file` returns the version id S3
            # assigns and `_record_run` persists it, so a `MagicMock` reaches
            # `json.dumps` and the run dies on serialization rather than on anything
            # this test is about.
            mock.patch.object(backup.storage, "put_file", return_value="v-test"),
        ):
            backup.run_sessions_backup(ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER)

        # The archive's authorization is the first one and it must be inside the
        # lock. Anything after it belongs to the label and is outside by design.
        assert seen, "the authorization never ran"
        assert seen[0] is True

    def test_a_refused_authorization_releases_the_lock_it_took(self, tmp_path, monkeypatch):
        """The authorization refusal is inside the lock too, so it must release it.

        `_authorize_upload` raises from within the critical section now. A file
        lock leaked on that path would wedge every later backup and every
        revocation, which is worse than the staleness the wider scope exists to
        prevent. The held lock is the sidecar FILE lock (`_upload_lock`), so the
        release is probed by taking that file lock again from another thread.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        self._store(tmp_path, True)

        def refuse(*args, **kwargs):
            raise RuntimeError("consent withdrawn; upload refused")

        with (
            mock.patch.object(backup, "_authorize_upload", side_effect=refuse),
            mock.patch.object(backup.storage, "put_file") as put,
        ):
            with pytest.raises(RuntimeError, match="consent withdrawn"):
                backup.run_sessions_backup(
                    ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
                )

        # Nothing shipped, and the file lock is free again. Probed from another
        # thread on a fresh descriptor for the same reason as the sibling test.
        put.assert_not_called()
        released: dict[str, bool] = {}
        lock_path = backup._state_path().with_suffix(".lock")

        def probe() -> None:
            try:
                with backup.open_lock_file(lock_path) as fd:
                    with backup.file_lock(fd, exclusive=True, wait=False):
                        released["free"] = True
            except OSError:
                released["free"] = False

        thread = threading.Thread(target=probe)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive(), "the probe thread blocked instead of answering"
        assert released["free"] is True

    def test_a_refused_upload_releases_the_lock_it_took(self, tmp_path, monkeypatch):
        """The refusal path must not leave the lock held.

        The refusal raises from inside the critical section. A file lock leaked
        there would be worse than the race it closes: every later backup and every
        revocation takes the same sidecar file lock, so they would all wait forever
        on a permission decision that already finished. Release comes from the
        ``with`` structure rather than from an explicit unlock, and this asserts
        that structure holds on the exception path too.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        answers = iter([True, False, False, False])
        monkeypatch.setattr(backup, "sessions_layer_b_enabled", lambda account: next(answers))

        with (
            mock.patch.object(backup, "_authorize_upload"),
            mock.patch.object(backup.storage, "put_file"),
        ):
            with pytest.raises(RuntimeError, match="withdrawn while this archive"):
                backup.run_sessions_backup(
                    ACCOUNT, "p", "us-west-2", "bkt", caller=backup.CALLER_OWNER
                )

        # Probed from another thread on a fresh descriptor, without blocking.
        # Asserting by acquiring the file lock in THIS thread would HANG on a leak
        # rather than fail, since the file lock is per-descriptor and a second
        # exclusive take blocks. A non-blocking probe from a thread that holds
        # nothing answers cleanly either way.
        released: dict[str, bool] = {}
        lock_path = backup._state_path().with_suffix(".lock")

        def probe() -> None:
            try:
                with backup.open_lock_file(lock_path) as fd:
                    with backup.file_lock(fd, exclusive=True, wait=False):
                        released["free"] = True
            except OSError:
                released["free"] = False

        thread = threading.Thread(target=probe)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive(), "the probe thread blocked instead of answering"
        assert released["free"] is True

    def test_the_lock_ceiling_outlasts_the_upload_it_is_held_across(self):
        """The state lock must not refuse a contender while the holder works.

        ``file_lock``'s default ceiling is sized for a sub-second read plus an
        atomic rename, and it requires any caller that can hold the lock longer
        to override it. This gate holds the lock across the authorization and a
        PUT allowed ``_PUSH_TIMEOUT_SECS``, so the default would expire on a
        contender that is merely waiting. That is not a cosmetic failure: the
        contender sees ``OSError``, which ``_record_run`` absorbs by keeping the
        run in memory alone, so a short-lived process that exits before the next
        successful write loses a completed upload's record and leaves the nightly
        loop due. Asserting the value reaches ``file_lock`` rather than only
        asserting the arithmetic, because a constant nobody passes is the exact
        defect this pins.
        """
        seen: list[float | None] = []
        real = backup.file_lock

        @contextlib.contextmanager
        def spy(fd, **kwargs):
            seen.append(kwargs.get("timeout"))
            with real(fd, **kwargs):
                yield

        with mock.patch.object(backup, "file_lock", spy):
            backup._locked_state_update(lambda state: None)

        assert seen, "the state lock did not reach file_lock"
        assert seen[0] is not None, "the state lock took file_lock's default ceiling"
        assert seen[0] >= backup._PUSH_TIMEOUT_SECS

    def test_a_long_holder_does_not_refuse_a_waiting_contender(self, monkeypatch):
        """A holder outliving the default ceiling must not refuse a contender.

        The value assertion above cannot show the consequence, so this drives it:
        the default ceiling is patched down, a holder keeps the sidecar lock past
        it, and the contender must still acquire once the holder releases. The
        holder takes the file lock directly rather than through ``_state_lock`` so
        that the ceiling is the only thing under test -- ``_state_lock`` would also
        take ``_run_lock``, which this says nothing about. Patching
        reaches only the default: ``file_lock`` reads the module ceiling solely
        when no ``timeout`` is passed, so this fails exactly when the explicit one
        is missing.
        """
        monkeypatch.setattr(platform_compat, "_LOCK_TIMEOUT_SECS", 0.05)
        lock_path = backup._state_path().with_suffix(".lock")
        backup._state_path().parent.mkdir(parents=True, exist_ok=True)
        holding = threading.Event()
        release = threading.Event()

        def holder() -> None:
            with platform_compat.open_lock_file(lock_path) as fd:
                with platform_compat.file_lock(fd, exclusive=True, required=True):
                    holding.set()
                    release.wait(timeout=10)

        outcome: dict[str, Any] = {}

        def contender() -> None:
            try:
                backup._locked_state_update(lambda state: None)
                outcome["acquired"] = True
            except OSError as exc:
                outcome["acquired"] = False
                outcome["error"] = repr(exc)

        holder_thread = threading.Thread(target=holder)
        holder_thread.start()
        try:
            assert holding.wait(timeout=10), "the holder never took the lock"
            contender_thread = threading.Thread(target=contender)
            contender_thread.start()
            # Outlive the patched ceiling while still held, which is the condition
            # that made the unbounded default refuse a contender that was only
            # waiting.
            time.sleep(0.3)
            release.set()
            contender_thread.join(timeout=15)
            assert not contender_thread.is_alive(), "the contender never returned"
        finally:
            release.set()
            holder_thread.join(timeout=10)

        assert outcome.get("acquired") is True, outcome

    def test_both_layer_b_decisions_reach_the_audit_log(self, tmp_path, monkeypatch):
        """Allow and withhold each leave a SEL event, not only the refusals.

        The permission decides whether unredacted context leaves the machine, and
        the ALLOW direction is the one that ships the bytes -- so an audit that
        covered only refusals would leave exactly the interesting decision
        unrecorded. Asserted for both directions in one test, because a helper
        that fires on one and not the other is the failure to catch.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        events: list[tuple[str, str]] = []

        class _Log:
            def log_api_access(self, **kw):
                events.append((kw["operation"], kw["resources"]))

        with mock.patch.object(backup, "sel", lambda: _Log()):
            self._store(tmp_path, True)
            self._run_capturing_names(monkeypatch)
            self._store(tmp_path, False)
            self._run_capturing_names(monkeypatch)

        decisions = [r for op, r in events if op == "aws_control.backup_layer_b_decision"]
        assert len(decisions) == 2
        assert "layer_b=allowed" in decisions[0]
        assert "layer_b=withheld" in decisions[1]
        # The account rides on the event, so a multi-account drive's log says
        # WHICH bucket the decision was about.
        assert all(f"account={ACCOUNT}" in r for r in decisions)

    # -- the permission is read once ---------------------------------------

    def test_the_archive_and_its_record_come_from_the_same_read(self, tmp_path, monkeypatch):
        """The record echoes the read that decided the contents, never a later one.

        The archive and the run record must describe the same bytes. Deriving the
        record from a fresh read would let a write landing mid-build produce a tar
        that carries Layer B alongside a record saying it does not -- and the
        record is what a restore trusts. The revocation recheck before the upload
        may REFUSE on a later answer; it may not change what the record says about
        an archive that shipped.
        """
        self._skip_without_pinning()
        self._both_halves(tmp_path, monkeypatch)
        calls: list[int] = []

        # The first two answers are the contents decision and the revocation
        # recheck. The third exists only to be wrong: anything that reads the
        # permission again to fill the RECORD would pick up this False.
        answers = iter([True, True, False, False])

        def _once(account):
            calls.append(1)
            return next(answers)

        monkeypatch.setattr(backup, "sessions_layer_b_enabled", _once)

        record, names = self._run_capturing_names(monkeypatch)

        assert len(calls) == 2
        assert names == ["cli/abc.json", "cli/abc.jsonl", "crew/t.jsonl"]
        assert record["layer_b"] is True

    # -- the record describes the archive, not the permission ---------------

    def test_a_permitted_run_with_no_cli_files_records_no_layer_b(self, tmp_path, monkeypatch):
        """Granted but nothing to add: the record must say what the archive holds.

        An absent or empty kiro-cli directory is an ordinary state on a fresh or
        CLI-idle install. The crew half alone carries the run past the empty-archive
        guard, so a record taken from the PERMISSION would file a crew-only archive
        as carrying Layer B. A run record is written once and nothing corrects it
        afterwards, so a restore reading that record would go looking for a fidelity
        the object does not hold.
        """
        self._skip_without_pinning()
        cli = self._both_halves(tmp_path, monkeypatch)
        for leftover in cli.iterdir():
            leftover.unlink()
        self._store(tmp_path, True)

        record, names = self._run_capturing_names(monkeypatch)

        # The permission is genuinely granted, so this is not the withhold path:
        # the cli tree was walked and simply had nothing in it.
        assert backup.sessions_layer_b_enabled(ACCOUNT) is True
        assert names == ["crew/t.jsonl"]
        assert record["layer_b"] is False

    # -- the snapshot record keeps its shape --------------------------------

    def test_a_snapshot_record_carries_no_layer_b_key(self, tmp_path, monkeypatch):
        """The question does not arise for a snapshot, so its record does not answer it."""
        record = backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/x/a.tar.gz", 1, "f")
        assert "layer_b" not in record


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
        carry a run the in-memory hold ALREADY holds. Registration happens in
        ``_record_run_locked``'s ``except OSError``, which is OUTSIDE the sidecar
        lock: :func:`_locked_state_update` releases it the moment ``write_state``
        raises, and the handler runs after. So a contender can take the lock and
        publish in the window before the hold exists, and then only its own key
        reaches the document.

        ``held`` is the handshake that positions the second write after that
        registration, which is the ordering this test's final assertion depends on.
        Gating instead on the second thread's own start states only that the thread
        is running, which leaves the ordering to the machine: idle it lands one way,
        on a loaded parallel shard the other. The window itself is
        :meth:`test_a_contender_that_wins_the_hold_window_still_loses_nothing`'s
        subject; here it is excluded.

        The two runs are still genuinely concurrent -- the contender is submitted
        while the failed writer is parked, and both take their sequence from
        ``_run_lock`` -- but nothing here asserts anything about the two contending
        for the file lock, which is why gating the second one costs no coverage. That
        contention is
        :meth:`TestSessionsArchiveLayerBGate.test_a_long_holder_does_not_refuse_a_waiting_contender`'s
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
            first = first_future.result(timeout=10)
            second = second_future.result(timeout=10)
        assert first["at"] == second["at"]
        assert first["sequence"] < second["sequence"]
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT] == second
        assert self._on_disk()["accounts"][ACCOUNT]["uploads"] == {
            "first.tar.gz": "a",
            "second.tar.gz": "b",
        }

    def test_a_contender_that_wins_the_hold_window_still_loses_nothing(self, monkeypatch):
        """The window the sibling test excludes, asserted instead of sampled.

        Registration of a lost run is outside the sidecar lock, so a contender can
        publish between the failed write and the hold that recovers it. Nothing in
        the module orders those two, and nothing has to: the archive is in the
        bucket, the run reaches the hold either way, and recovery is promised
        against the NEXT successful state update rather than a simultaneous one.

        Forcing the contender to win makes that window deterministic rather than a
        property of how loaded the machine is. What must hold is that the run is
        never LOST -- ``last_runs`` and ``uploaded_keys`` read it through the
        overlay and the next update persists it -- and that the run record's own
        slot goes to the newer run, not to the recovered one. Due-ness is asserted
        here only as the loop's state after the window, not as a pin on the hold:
        the contender's stamp is on disk, so it settles the answer on its own.
        :meth:`test_a_lost_write_does_not_leave_the_nightly_loop_due` is where the
        held run carries that pin, because there nothing reaches disk at all.

        MUTATION: drop the ``_merge_pending`` recovery, or the overlay merge in
        ``uploaded_keys``, and this reddens. It is also the test to change, rather
        than to work around, if the hold ever moves inside the lock: the loss
        asserted below is this design's, not a requirement on it.
        """
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event

        entered, release, published = Event(), Event(), Event()
        now = dt.datetime(2026, 9, 19, tzinfo=dt.timezone.utc)
        clock = mock.Mock(wraps=dt.datetime)
        clock.now.return_value = now
        monkeypatch.setattr(backup, "dt", mock.Mock(datetime=clock, timezone=dt.timezone))
        real_write = backup.write_state
        real_remember = backup._remember_unpersisted
        # Granted BEFORE `write_state` is patched, so this write lands for real and is
        # not the one the injection fails. Without it `due_for_nightly` returns at its
        # own first statement on absent consent and never reaches the stamp reader,
        # which would make the assertion below pass for a reason that has nothing to
        # do with any run.
        backup.set_nightly(ACCOUNT, True)

        def writer(state):
            if not entered.is_set():
                entered.set()
                assert release.wait(10), "test did not release the failed writer"
                raise OSError(errno.EIO, "injected")
            real_write(state)
            published.set()

        def remember(account, kind, record):
            # Hold the registration until the contender's write has landed. This is
            # the losing order, pinned: on an idle machine the handler wins this
            # race, on a loaded shard it does not.
            assert published.wait(10), "the contender never published"
            real_remember(account, kind, record)

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
            first = first_future.result(timeout=10)
            second = second_future.result(timeout=10)

        # The contender's own document cannot carry a hold that does not exist yet.
        # This is the map that window produces, and it is not a lost upload.
        assert self._on_disk()["accounts"][ACCOUNT]["uploads"] == {"second.tar.gz": "b"}

        # Held, and so still answered to every reader that merges the overlay.
        assert backup._unpersisted_runs[(backup._state_key(), ACCOUNT, backup.KIND_SNAPSHOT)] == (
            first
        )
        assert backup.uploaded_keys(ACCOUNT) == {"first.tar.gz", "second.tar.gz"}
        # The newer run owns the slot; recovery does not hand it to the older one.
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT] == second
        # Consent is granted and the contender's stamp is on disk at `now`, so this
        # reaches the stamp reader and answers not-due. It does NOT isolate the held
        # run's contribution -- the persisted run alone settles due-ness here. The
        # held run's own pin is
        # `test_a_lost_write_does_not_leave_the_nightly_loop_due`, where nothing
        # reaches disk and the overlay is the only possible answer.
        assert backup.due_for_nightly(ACCOUNT, now=now) is False

        # The promise is the NEXT successful update, and this is it.
        backup.set_nightly(ACCOUNT, True)
        assert self._on_disk()["accounts"][ACCOUNT]["uploads"] == {
            "first.tar.gz": "a",
            "second.tar.gz": "b",
        }
        assert (
            backup._state_key(),
            ACCOUNT,
            backup.KIND_SNAPSHOT,
        ) not in backup._unpersisted_runs


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
        # The race a reviewer found on the first head. Both writers serialize under the
        # sidecar lock, but each mutate re-reads fresh state, so an unconditional write
        # here lands AFTER a concurrent manual success cleared the count and records a
        # failure against a kind that just succeeded. Measured cost: not a withheld
        # attempt (the raced write restarts at 1, which earns zero delay) but a false
        # `nightly_failures` row for an account that just backed up. The witness is read
        # BEFORE the attempt, so the run that lands inside it is detectable.
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
        assert backup.nightly_failures(ACCOUNT)[backup.KIND_SNAPSHOT]["consecutive"] == 4

        # Any later successful state update drains the overlay through `_merge_pending`.
        backup.set_nightly(ACCOUNT, True)
        persisted = backup._account_view(ACCOUNT).get("runs", {}).get(backup.KIND_SNAPSHOT)
        assert persisted and persisted["key"] == "snapshots/i/held.tar.gz"  # run recovered
        assert backup.nightly_failures(ACCOUNT) == {}  # ...and the count went with it

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


def _tar_gz(payload: bytes = b"restored") -> bytes:
    """A real ``tar.gz``, so these tests read as the restore they describe.

    ``ARCHIVE_BYTES`` is deliberately not one -- the tests above are about the
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
