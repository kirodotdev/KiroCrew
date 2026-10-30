"""AWS Control hardening pins: persisted-state shape, stale account mappings, link following,
egress redaction, audit and partition handling on the drive, backup and restore paths, the bill
scoped to one account, and the region a registration records.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

import pytest
from aws_control_app_helpers import (  # noqa: F401 -- fixtures are used by name
    ACCOUNT,
    _enabled_owner_env,
    _fresh_snapshot,
    _payload,
    _registered,
    _request,
)

from kiro_crew import aws_consent
from kiro_crew.apps.builtins.aws_control.backend import accounts as accounts_mod
from kiro_crew.apps.builtins.aws_control.backend import routes as routes_mod


class TestStateShapeGuards:
    """Round-9 pins: corrupted persisted JSON reads as empty, never a 500."""

    def test_costs_cache_decoding_to_a_list_reads_as_no_cache(self, tmp_path, monkeypatch):
        from kiro_crew.apps.builtins.aws_control.backend import costs

        monkeypatch.setattr(costs, "_cache_path", lambda account: tmp_path / "c.json")
        (tmp_path / "c.json").write_text('["not", "a", "dict"]', encoding="utf-8")
        assert costs.read_cached(ACCOUNT) is None

    def test_backup_state_with_scalar_accounts_reads_as_empty(self, tmp_path, monkeypatch):
        import json as _json

        from kiro_crew.apps.builtins.aws_control.backend import backup

        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        (tmp_path / "backup.json").write_text(
            _json.dumps({"accounts": "corrupt"}), encoding="utf-8"
        )
        assert backup.nightly_enabled(ACCOUNT) is False
        assert backup.last_runs(ACCOUNT) == {}
        (tmp_path / "backup.json").write_text(
            _json.dumps({"accounts": {ACCOUNT: {"runs": "corrupt"}}}), encoding="utf-8"
        )
        assert backup.last_runs(ACCOUNT) == {}


class TestStaleMappingGuard:
    def test_repointed_profile_is_refused_not_executed(self):
        # The snapshot said this profile belongs to ACCOUNT, but the live
        # probe now resolves a DIFFERENT account: the operation must refuse
        # instead of running against whatever the profile points at today.
        handlers = _registered()
        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(
                routes_mod.accounts_mod,
                "resolve_account_profile",
                AsyncMock(return_value=("prof", "us-west-2")),
            ),
            mock.patch.object(
                routes_mod.aws_consent,
                "probe_identity",
                AsyncMock(return_value=aws_consent.Identity(ok=True, account="444455556666")),
            ),
            mock.patch.object(routes_mod.storage_mod, "find_drive") as find,
        ):
            resp = asyncio.run(
                handlers[("GET", "/drive/{account}")](  # type: ignore[operator]
                    _request("GET", f"/drive/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        assert resp.status == 409
        assert _payload(resp)["code"] == "account_mismatch"
        find.assert_not_called()

    def test_a_cached_identity_cannot_authorize_a_read(self):
        """The probe must not answer an authorization question from memory.

        The test above mocks ``probe_identity`` outright, so it proves the refusal
        and says nothing about WHERE the identity came from. The real function
        memoises per ``(profile, region)`` for 30 seconds, which is why this one
        drives the actual cache: it primes an answer of ACCOUNT, repoints the
        underlying credentials at another account, and then asks for ACCOUNT.

        Against a cached probe the route reads ACCOUNT's inventory out of the
        OTHER account, and nothing anywhere reports a problem: the caller asked
        for an account they own, the check passed, and the rows came back. That is
        disclosure, and it is silent, which is why the assertion below is that no
        AWS read was attempted at all.
        """
        handlers = _registered()
        other = "444455556666"
        answer = {"account": ACCOUNT}

        def fake_run(args, profile, region):
            return 0, json.dumps({"Account": answer["account"], "Arn": "arn:aws:iam::x:user/y"}), ""

        async def drive():
            # Prime the cache the way an ordinary earlier request would.
            primed = await aws_consent.probe_identity("prof", "us-west-2")
            assert primed.account == ACCOUNT
            # The profile is now a different account. An operator switching a role
            # or an SSO session does exactly this, and nothing tells the console.
            answer["account"] = other
            return await handlers[("GET", "/drive/{account}")](  # type: ignore[operator]
                _request("GET", f"/drive/{ACCOUNT}", match_info={"account": ACCOUNT})
            )

        aws_consent._probe_cache.clear()
        try:
            with (
                mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
                mock.patch.object(
                    routes_mod.accounts_mod,
                    "resolve_account_profile",
                    AsyncMock(return_value=("prof", "us-west-2")),
                ),
                mock.patch.object(aws_consent, "_aws_cli_resolvable", return_value=True),
                mock.patch.object(aws_consent, "_run_aws", side_effect=fake_run),
                mock.patch.object(
                    routes_mod.storage_mod,
                    "find_drive",
                    side_effect=AssertionError(
                        "find_drive ran: the identity was taken from the probe cache, "
                        "so the account was never re-derived before the read"
                    ),
                ) as find_drive,
            ):
                resp = asyncio.run(drive())
        finally:
            aws_consent._probe_cache.clear()

        assert resp.status == 409
        assert _payload(resp)["code"] == "account_mismatch"
        find_drive.assert_not_called()


class TestRound16Hardening:
    """Round-16 pins: egress redaction, corrupt-shape survival, grant audit."""

    def test_profile_and_account_names_are_redacted_on_the_way_out(self):
        # The profile registry is an agent-writable store, so a planted
        # credential in a profile NAME must not reach the dashboard verbatim.
        planted = "prof-AKIAIOSFODNN7EXAMPLE"
        view = accounts_mod.ProfileView(
            name=planted,
            region="us-west-2",
            kind="other",
            identity_ok=False,
            account="",
            arn="",
            detail=f"probe failed for {planted}",
        )
        out = view.to_dict()
        assert "AKIAIOSFODNN7EXAMPLE" not in out["name"]
        assert "AKIAIOSFODNN7EXAMPLE" not in out["detail"]

        acct = accounts_mod.AccountView(account=ACCOUNT, profiles=[view])
        assert "AKIAIOSFODNN7EXAMPLE" not in acct.to_dict()["name"]

    def test_corrupt_runs_map_does_not_lose_a_completed_upload(self, tmp_path, monkeypatch):
        # The archive is already in S3 by the time the ledger is written: a
        # non-dict `runs` must be replaced, not raise (500 + no ledger entry
        # would make the next retry upload a duplicate).
        import json as _json

        from kiro_crew.apps.builtins.aws_control.backend import backup

        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        (tmp_path / "backup.json").write_text(
            _json.dumps({"accounts": {ACCOUNT: {"runs": "corrupt"}}}), encoding="utf-8"
        )
        entry = backup._record_run(ACCOUNT, "memory", "backup/mem.tar.gz", 1234)
        assert entry["key"] == "backup/mem.tar.gz"
        assert backup.last_runs(ACCOUNT)["memory"]["bytes"] == 1234

    def test_download_presign_is_audited_as_a_grant(self):
        # A presign mints a bearer URL: the audit trail must be able to answer
        # "which keys had a URL minted", so record the grant (key, never URL).
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=True)
            ),
            mock.patch.object(
                routes_mod.storage_mod, "find_drive", return_value="kirocrew-drive-abc"
            ),
            mock.patch.object(routes_mod.storage_mod, "head_object_meta", return_value={}),
            mock.patch.object(routes_mod.storage_mod, "presign", return_value="https://signed"),
            mock.patch.object(routes_mod, "_audit") as audit,
        ):
            req = _request(
                "GET",
                f"/drive/{ACCOUNT}/download?section=drive&key=a.txt",
                match_info={"account": ACCOUNT},
            )
            resp = asyncio.run(
                handlers[("GET", "/drive/{account}/download")](req)  # type: ignore[operator]
            )
        assert resp.status == 200
        calls = [c.args for c in audit.call_args_list]
        assert ("drive_download", "drive/a.txt", "granted") in calls
        # The signed URL itself is never an audit resource.
        assert not any("https://signed" in str(a) for a in calls)


class TestRound17Hardening:
    """Round-17 pins: no link following in backup/restore, whole-field egress."""

    def test_archive_does_not_walk_a_symlinked_directory(self, tmp_path):
        from kiro_crew.apps.builtins.aws_control.backend import backup as _bk

        if not _bk._CAN_PIN_TRAVERSAL:
            pytest.skip(
                "descriptor-pinned traversal is unavailable here, so the backup"
                " refuses by design -- TestRefusalWithoutPinnedTraversal covers that"
            )
        import tarfile as _tarfile

        from kiro_crew.apps.builtins.aws_control.backend import backup

        # A secret outside the tree, reachable only through a planted link.
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "credentials").write_text("secret", encoding="utf-8")

        root = tmp_path / "sessions"
        root.mkdir()
        (root / "real.json").write_text("{}", encoding="utf-8")
        (root / "escape").symlink_to(outside, target_is_directory=True)
        (root / "escape-file").symlink_to(outside / "credentials")

        archive = tmp_path / "out.tar.gz"
        with _tarfile.open(archive, "w:gz") as tar:
            count = backup._add_tree(tar, root, "crew")

        with _tarfile.open(archive) as tar:
            names = tar.getnames()
        assert count == 1
        assert names == ["crew/real.json"]
        assert not any("credentials" in n for n in names)

    def test_restore_refuses_a_symlinked_destination(self, tmp_path, monkeypatch):
        from kiro_crew.apps.builtins.aws_control.backend import backup

        staging = tmp_path / "restore"
        staging.mkdir(parents=True)
        target = tmp_path / "victim.txt"
        target.write_text("original", encoding="utf-8")
        # Named the way the code names it -- the staging filename is derived from the
        # whole key, so the guard is only exercised if the decoy sits where the
        # download would actually land.
        (staging / backup._staging_name("snapshots/a.tar.gz")).symlink_to(target)

        monkeypatch.setattr(backup, "app_data_dir", lambda name: tmp_path)
        with mock.patch.object(backup.storage, "get_file") as get_file:
            with pytest.raises(ValueError):
                backup.restore_download(
                    "p",
                    "us-west-2",
                    "b",
                    "snapshots/a.tar.gz",
                    account="111122223333",
                    # Flat legacy-shaped key: the gate refuses anything it cannot prove is
                    # this install's own, and this test is about the staging path, not
                    # ownership, so it states the override.
                    foreign_ok=True,
                )
        get_file.assert_not_called()
        assert target.read_text(encoding="utf-8") == "original"

    def test_restore_writes_through_a_temp_file_then_replaces(self, tmp_path, monkeypatch):
        from pathlib import Path

        from kiro_crew.apps.builtins.aws_control.backend import backup

        monkeypatch.setattr(backup, "app_data_dir", lambda name: tmp_path)
        seen: list[str] = []

        def fake_get(profile, region, bucket, section, key, dest, *, account=None):
            seen.append(dest)
            Path(dest).write_text("payload", encoding="utf-8")

        with mock.patch.object(backup.storage, "get_file", side_effect=fake_get):
            out = backup.restore_download(
                "p",
                "us-west-2",
                "b",
                "snapshots/a.tar.gz",
                account="111122223333",
                # Flat legacy-shaped key: the gate refuses anything it cannot prove is
                # this install's own, and this test is about the staging path, not
                # ownership, so it states the override.
                foreign_ok=True,
            )

        final = tmp_path / "restore" / backup._staging_name("snapshots/a.tar.gz")
        assert out["path"] == str(final)
        assert final.read_text(encoding="utf-8") == "payload"
        # Not the bare basename: two installs can name an archive the same, and the
        # staged copies must not land on one file.
        assert final.name != "a.tar.gz" and final.name.endswith("-a.tar.gz")
        # The download target was NOT the final name.
        assert seen and seen[0] != str(final)
        # No temp residue.
        assert [p.name for p in (tmp_path / "restore").iterdir()] == [final.name]

    def test_the_staged_name_stays_within_the_filesystem_limit(self):
        """A near-max key segment must still produce a file, not ENAMETOOLONG.

        The prefix is added to a basename the route's validator already allows up to
        255 characters, so an unbounded name overruns NAME_MAX and the restore fails
        with an OSError instead of staging anything. Only a co-tenant or the console
        can place such a name: this app's own writer produces short fixed ones.
        """
        from kiro_crew.apps.builtins.aws_control.backend import backup

        longest = "x" * 255
        name = backup._staging_name(f"snapshots/{'a' * 32}/{longest}")
        assert len(name.encode("utf-8")) <= backup.STAGING_NAME_MAX_BYTES
        # Truncation must not undo the collision fix: the digest covers the whole key.
        other = backup._staging_name(f"snapshots/{'b' * 32}/{longest}")
        assert name != other
        assert len(other.encode("utf-8")) <= backup.STAGING_NAME_MAX_BYTES

    def test_same_named_archives_from_two_installs_stage_side_by_side(self, tmp_path, monkeypatch):
        """Namespacing creates this collision, so the staging name has to resolve it.

        Two installs each write ``kirocrew-snapshot-X.tar.gz`` under their own
        prefix. Restoring both must leave two files: a basename-only destination
        would have the second silently replace the first.
        """
        from pathlib import Path

        from kiro_crew.apps.builtins.aws_control.backend import backup

        monkeypatch.setattr(backup, "app_data_dir", lambda name: tmp_path)
        keys = [f"snapshots/{'a' * 32}/same.tar.gz", f"snapshots/{'b' * 32}/same.tar.gz"]

        def fake_get(profile, region, bucket, section, key, dest, *, account=None):
            Path(dest).write_text(key, encoding="utf-8")

        with mock.patch.object(backup.storage, "get_file", side_effect=fake_get):
            paths = [
                backup.restore_download(
                    "p", "us-west-2", "b", k, account="111122223333", foreign_ok=True
                )["path"]
                for k in keys
            ]

        assert len(set(paths)) == 2
        staged = sorted(p.name for p in (tmp_path / "restore").iterdir())
        assert len(staged) == 2, staged
        # Each file still holds the archive it was downloaded for.
        for key, path in zip(keys, paths):
            assert Path(path).read_text(encoding="utf-8") == key

    def test_every_string_display_field_is_redacted(self):
        planted = "AKIAIOSFODNN7EXAMPLE"
        view = accounts_mod.ProfileView(
            name=f"p-{planted}",
            region=f"us-west-2-{planted}",
            kind="other",
            identity_ok=True,
            account=f"{planted}",
            arn=f"arn:aws:sts::1:assumed-role/{planted}",
            detail=f"probe said {planted}",
        )
        out = view.to_dict()
        leaked = [k for k, v in out.items() if isinstance(v, str) and planted in v]
        assert leaked == []


class TestRound18Hardening:
    """Round-18 pins: nightly authorization is agent-write-protected, and the
    restore staging DIRECTORY cannot be a link out of app storage."""

    def test_backup_state_is_on_the_agent_file_tool_floor(self):
        # `nightly` in this file authorizes an unattended upload loop, so the
        # file must sit behind the shared agent file-tool floor. The whole DATA
        # DIRECTORY is fenced, not that one name, because an atomic write goes
        # through a temporary in the same directory before the rename -- fencing
        # only the final name leaves a writable path to the same bytes.
        from kiro_crew import security
        from kiro_crew.apps.builtins.aws_control.backend import backup

        assert backup.STATE_DIR_LEAF in security._CREW_SECRET_LEAVES
        # The leaf is always '/'-joined (a catalog key, not a local path), so
        # compare against the posix form or this fails on Windows. The state
        # file, its temporary, and its siblings all live under the fenced dir.
        assert (
            backup._state_path()
            .as_posix()
            .endswith(f"{backup.STATE_DIR_LEAF}/{backup._state_path().name}")
        )

    def test_restore_refuses_a_symlinked_staging_directory(self, tmp_path, monkeypatch):
        from kiro_crew.apps.builtins.aws_control.backend import backup

        # An agent plants `restore` as a link to somewhere outside app storage.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        base = tmp_path / "appdata"
        base.mkdir()
        (base / "restore").symlink_to(elsewhere, target_is_directory=True)

        monkeypatch.setattr(backup, "app_data_dir", lambda name: base)
        with mock.patch.object(backup.storage, "get_file") as get_file:
            with pytest.raises(ValueError):
                backup.restore_download(
                    "p",
                    "us-west-2",
                    "b",
                    "snapshots/a.tar.gz",
                    account="111122223333",
                    # Flat legacy-shaped key: the gate refuses anything it cannot prove is
                    # this install's own, and this test is about the staging path, not
                    # ownership, so it states the override.
                    foreign_ok=True,
                )
        get_file.assert_not_called()
        # Nothing was written through the link.
        assert list(elsewhere.iterdir()) == []


class TestRound19Hardening:
    """Round-19 pins: the archive streams from descriptors, and a download
    presign refuses a key that is not in the drive."""

    def test_archive_streams_from_a_descriptor_not_a_reopened_path(self, tmp_path):
        from kiro_crew.apps.builtins.aws_control.backend import backup as _bk

        if not _bk._CAN_PIN_TRAVERSAL:
            pytest.skip(
                "descriptor-pinned traversal is unavailable here, so the backup"
                " refuses by design -- TestRefusalWithoutPinnedTraversal covers that"
            )
        # `tar.add(path)` re-resolves the name it is handed, so a path that was
        # checked and then reopened is a swap window. Nothing in the walk may
        # call it: everything after the single O_NOFOLLOW open comes off the fd.
        import tarfile as _tarfile

        from kiro_crew.apps.builtins.aws_control.backend import backup

        root = tmp_path / "sessions"
        root.mkdir()
        # Bytes, not text: write_text newline-translates on Windows, so the
        # round-trip assertion below would compare b"line\r\n" to b"line\n".
        (root / "a.jsonl").write_bytes(b"line\n")
        (root / "b.json").write_bytes(b"{}")

        archive = tmp_path / "out.tar.gz"
        with _tarfile.open(archive, "w:gz") as tar:
            with mock.patch.object(tar, "add", side_effect=AssertionError("reopened by name")):
                count = backup._add_tree(tar, root, "crew")

        assert count == 2
        with _tarfile.open(archive) as tar:
            names = sorted(tar.getnames())
            assert names == ["crew/a.jsonl", "crew/b.json"]
            # Content really came through the fd, not an empty header.
            member = tar.extractfile("crew/a.jsonl")
            assert member is not None
            assert member.read() == b"line\n"

    def test_non_regular_entries_are_skipped_by_fstat(self, tmp_path):
        from kiro_crew.apps.builtins.aws_control.backend import backup as _bk

        if not _bk._CAN_PIN_TRAVERSAL:
            pytest.skip(
                "descriptor-pinned traversal is unavailable here, so the backup"
                " refuses by design -- TestRefusalWithoutPinnedTraversal covers that"
            )
        # Two things at once, and the second is why this test has a deadline:
        # a FIFO must be excluded from the archive, AND reaching it must not
        # hang. `os.open(fifo, O_RDONLY)` blocks until a writer appears, so
        # without O_NONBLOCK a single named pipe planted in an agent-writable
        # session directory wedges the backup thread forever and the fstat that
        # would have rejected it never runs. This test hung before that flag.
        import os
        import tarfile as _tarfile

        if not hasattr(os, "mkfifo"):
            pytest.skip("no FIFOs on this platform")

        from kiro_crew.apps.builtins.aws_control.backend import backup

        root = tmp_path / "sessions"
        root.mkdir()
        (root / "real.json").write_text("{}", encoding="utf-8")
        os.mkfifo(root / "pipe")  # noqa: S108 - test fixture, not a temp path

        archive = tmp_path / "out.tar.gz"
        with _tarfile.open(archive, "w:gz") as tar:
            count = backup._add_tree(tar, root, "crew")
        with _tarfile.open(archive) as tar:
            names = tar.getnames()
        assert count == 1
        assert names == ["crew/real.json"]

    def test_download_presign_refuses_a_missing_object(self):
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        with (
            p1,
            p2,
            p3,
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=True)
            ),
            mock.patch.object(
                routes_mod.storage_mod, "find_drive", return_value="kirocrew-drive-abc"
            ),
            mock.patch.object(routes_mod.storage_mod, "head_object_meta", return_value=None),
            mock.patch.object(routes_mod.storage_mod, "presign") as presign,
        ):
            req = _request(
                "GET",
                f"/drive/{ACCOUNT}/download?section=drive&key=gone.txt",
                match_info={"account": ACCOUNT},
            )
            resp = asyncio.run(
                handlers[("GET", "/drive/{account}/download")](req)  # type: ignore[operator]
            )
        assert resp.status == 404
        assert _payload(resp)["code"] == "object_missing"
        presign.assert_not_called()


class TestRound21Partition:
    """Round-21 pin: drive discovery is partition-independent."""

    @pytest.mark.parametrize(
        "partition",
        ["aws", "aws-us-gov", "aws-cn"],
    )
    def test_drive_is_discovered_on_every_partition(self, partition):
        # tag:GetResources returns a partition-qualified ARN. A hardcoded
        # `arn:aws:s3:::` prefix dropped the drive we created ourselves on
        # GovCloud and China, so the console reported no drive and a second
        # confirm would mint a second billable bucket.
        from kiro_crew.apps.builtins.aws_control.backend import storage

        name = "kirocrew-drive-abc123def456"
        payload = json.dumps(
            {"ResourceTagMappingList": [{"ResourceARN": f"arn:{partition}:s3:::{name}"}]}
        )
        with mock.patch.object(storage.engine, "run_aws", return_value=(0, payload, "")):
            assert storage.find_drive("p", "us-west-2", account="111122223333") == name

    def test_a_non_s3_arn_is_still_rejected(self):
        from kiro_crew.apps.builtins.aws_control.backend import storage

        payload = json.dumps(
            {
                "ResourceTagMappingList": [
                    {
                        "ResourceARN": "arn:aws:dynamodb:us-west-2:1:table/kirocrew-drive-abc123def456"
                    }
                ]
            }
        )
        with mock.patch.object(storage.engine, "run_aws", return_value=(0, payload, "")):
            assert storage.find_drive("p", "us-west-2", account="111122223333") is None

    def test_drive_policy_arns_are_partition_neutral(self):
        from kiro_crew.deploy import iam

        doc = iam.policy_document(tier="drive")
        arns = [
            r
            for st in doc["Statement"]
            for r in (st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]])
            if r.startswith("arn:")
        ]
        assert arns, "drive tier should scope some resources by ARN"
        # No statement may pin the commercial partition: that grants nothing in
        # GovCloud or China, where the owner's own drive lives.
        assert not [a for a in arns if a.startswith("arn:aws:")]
        # The bucket-name scoping itself must survive the change.
        assert all("kirocrew-drive-" in a for a in arns)


class TestRound22Hardening:
    """Round-22 pins: the unattended path is audited, and teardown stops a
    build before it can upload."""

    def test_teardown_refuses_an_upload_that_has_not_started(self):
        from kiro_crew.apps.builtins.aws_control.backend import backup

        # Everything else authorizes; only the teardown signal is set. A worker
        # thread cannot be killed, so this gate is what makes disabling the app
        # stop a backup that is still building its archive.
        with (
            mock.patch(
                "kiro_crew.deploy.engine._checked",
                return_value=json.dumps({"Account": ACCOUNT}),
            ),
            mock.patch("kiro_crew.apps.manager.is_app_enabled", return_value=True),
            mock.patch("kiro_crew.aws_consent.is_granted", return_value=(True, "")),
            mock.patch(
                "kiro_crew.aws_consent.read_grant",
                return_value=SimpleNamespace(account=ACCOUNT),
            ),
        ):
            backup.clear_stop()
            backup._authorize_upload(
                ACCOUNT,
                "p",
                "us-west-2",
                caller=backup.CALLER_OWNER,
                payload_kind=backup.KIND_SNAPSHOT,
            )  # no raise
            backup.signal_stop()
            try:
                with pytest.raises(RuntimeError, match="shutting down"):
                    backup._authorize_upload(
                        ACCOUNT,
                        "p",
                        "us-west-2",
                        caller=backup.CALLER_OWNER,
                        payload_kind=backup.KIND_SNAPSHOT,
                    )
            finally:
                backup.clear_stop()

    def test_reenabling_clears_the_teardown_signal(self):
        from kiro_crew.apps.builtins.aws_control.backend import backup

        backup.signal_stop()
        assert backup._STOP.is_set()
        backup.clear_stop()
        assert not backup._STOP.is_set()

    def test_nightly_backup_claims_the_run_through_the_sdk(self):
        from kiro_crew.apps.builtins.aws_control import hooks

        sdk = mock.Mock(start_async=AsyncMock(return_value="run-1"))
        with (
            mock.patch.object(
                hooks.accounts_mod,
                "resolve_default_account_profile",
                AsyncMock(return_value=("p", "us-west-2")),
            ),
            mock.patch.object(
                hooks.aws_consent,
                "probe_identity",
                AsyncMock(return_value=aws_consent.Identity(ok=True, account=ACCOUNT)),
            ),
            mock.patch.object(hooks.backup_mod, "due_for_nightly", return_value=True),
            mock.patch.object(hooks.backup_mod, "due_for_sessions_nightly", return_value=False),
            mock.patch.object(hooks.aws_consent, "refuse_and_log", AsyncMock(return_value=True)),
            mock.patch.object(hooks.backup_mod.storage, "find_drive", return_value="bkt"),
        ):
            asyncio.run(hooks._run_once(sdk))

        # The run is a RECORDED SDK run, claimed under the
        # account and the scheduled caller. The run's own SEL audit is the
        # runner's job, pinned in test_aws_control_backup_job.py.
        sdk.start_async.assert_awaited_once()
        call = sdk.start_async.await_args
        assert call.args[0] == hooks.backup_mod.KIND_SNAPSHOT
        assert call.kwargs["dedupe_key"] == ACCOUNT
        assert call.kwargs["params"] == {
            hooks.backup_mod.JOB_PARAM_CALLER: hooks.backup_mod.CALLER_SCHEDULED,
            hooks.backup_mod.JOB_PARAM_PROFILE: "p",
            hooks.backup_mod.JOB_PARAM_REGION: "us-west-2",
        }

    def test_a_refused_claim_does_not_crash_the_loop(self, caplog):
        from kiro_crew.apps.builtins.aws_control import hooks
        from kiro_crew.apps.job_sdk import JobError

        caplog.set_level(logging.INFO, logger=hooks.logger.name)
        sdk = mock.Mock(start_async=AsyncMock(side_effect=JobError("shutting down")))
        with (
            mock.patch.object(
                hooks.accounts_mod,
                "resolve_default_account_profile",
                AsyncMock(return_value=("p", "us-west-2")),
            ),
            mock.patch.object(
                hooks.aws_consent,
                "probe_identity",
                AsyncMock(return_value=aws_consent.Identity(ok=True, account=ACCOUNT)),
            ),
            mock.patch.object(hooks.backup_mod, "due_for_nightly", return_value=True),
            mock.patch.object(hooks.backup_mod, "due_for_sessions_nightly", return_value=False),
            mock.patch.object(hooks.aws_consent, "refuse_and_log", AsyncMock(return_value=True)),
            mock.patch.object(hooks.backup_mod.storage, "find_drive", return_value="bkt"),
        ):
            asyncio.run(hooks._run_once(sdk))  # swallowed, not raised

        assert [r.getMessage() for r in caplog.records if r.name == hooks.logger.name] == [
            f"aws-control nightly: could not claim {hooks.backup_mod.KIND_SNAPSHOT} run"
        ]
        sdk.start_async.assert_awaited_once()


class TestRound23Junctions:
    """Round-23 pin: link checks are junction-aware, not islink-only."""

    def test_root_link_is_refused_before_any_traversal(self, tmp_path):
        from kiro_crew.apps.builtins.aws_control.backend import backup as _bk

        if not _bk._CAN_PIN_TRAVERSAL:
            pytest.skip(
                "descriptor-pinned traversal is unavailable here, so the backup"
                " refuses by design -- TestRefusalWithoutPinnedTraversal covers that"
            )
        import tarfile as _tarfile

        from kiro_crew.apps.builtins.aws_control.backend import backup

        root = tmp_path / "sessions"
        root.mkdir()
        (root / "a.json").write_bytes(b"{}")
        archive = tmp_path / "out.tar.gz"
        with _tarfile.open(archive, "w:gz") as tar:
            with mock.patch.object(backup, "is_link_or_junction", return_value=True):
                assert backup._add_tree(tar, root, "crew") == 0

    def test_restore_uses_the_junction_aware_predicate(self, tmp_path, monkeypatch):
        from kiro_crew.apps.builtins.aws_control.backend import backup

        base = tmp_path / "appdata"
        (base / "restore").mkdir(parents=True)
        monkeypatch.setattr(backup, "app_data_dir", lambda name: base)
        with (
            mock.patch.object(backup, "is_link_or_junction", return_value=True),
            mock.patch.object(backup.storage, "get_file") as get_file,
        ):
            with pytest.raises(ValueError):
                backup.restore_download(
                    "p",
                    "us-west-2",
                    "b",
                    "snapshots/a.tar.gz",
                    account="111122223333",
                    # Flat legacy-shaped key: the gate refuses anything it cannot prove is
                    # this install's own, and this test is about the staging path, not
                    # ownership, so it states the override.
                    foreign_ok=True,
                )
        get_file.assert_not_called()


class TestRound24Hardening:
    """Round-24 pins: descriptor-pinned ancestors, and redaction before trimming."""

    def test_traversal_never_resolves_a_path_by_name(self, tmp_path):
        # The ancestor-swap window exists only if a child is reached by
        # re-resolving its path. On POSIX every open must go through dir_fd, so
        # a bare os.open with a path argument is a regression: patch os.open to
        # reject any call without dir_fd and the archive must still come out
        # complete.
        import os
        import tarfile as _tarfile

        from kiro_crew.apps.builtins.aws_control.backend import backup

        if not backup._CAN_PIN_TRAVERSAL:
            pytest.skip("platform has no openat; the name-based fallback is covered above")

        root = tmp_path / "sessions"
        (root / "nested" / "deep").mkdir(parents=True)
        (root / "a.json").write_bytes(b"a")
        (root / "nested" / "b.json").write_bytes(b"bb")
        (root / "nested" / "deep" / "c.json").write_bytes(b"ccc")

        real_open = os.open
        root_path = str(root)

        def only_pinned_or_root(path, flags, *args, **kwargs):
            # The root itself is opened by path (there is no parent fd yet);
            # everything below it must be relative to a directory descriptor.
            if kwargs.get("dir_fd") is None and str(path) != root_path:
                raise AssertionError(f"resolved by name: {path}")
            return real_open(path, flags, *args, **kwargs)

        archive = tmp_path / "out.tar.gz"
        with _tarfile.open(archive, "w:gz") as tar:
            with mock.patch.object(os, "open", side_effect=only_pinned_or_root):
                count = backup._add_tree(tar, root, "crew")

        with _tarfile.open(archive) as tar:
            names = sorted(tar.getnames())
            assert tar.extractfile("crew/nested/deep/c.json").read() == b"ccc"  # type: ignore[union-attr]
        assert count == 3
        assert names == [
            "crew/a.json",
            "crew/nested/b.json",
            "crew/nested/deep/c.json",
        ]

    def test_pinned_traversal_refuses_a_symlinked_subdirectory(self, tmp_path):
        from kiro_crew.apps.builtins.aws_control.backend import backup as _bk

        if not _bk._CAN_PIN_TRAVERSAL:
            pytest.skip(
                "descriptor-pinned traversal is unavailable here, so the backup"
                " refuses by design -- TestRefusalWithoutPinnedTraversal covers that"
            )
        import tarfile as _tarfile

        from kiro_crew.apps.builtins.aws_control.backend import backup

        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "credentials").write_bytes(b"secret")
        root = tmp_path / "sessions"
        root.mkdir()
        (root / "real.json").write_bytes(b"{}")
        (root / "escape").symlink_to(outside, target_is_directory=True)

        archive = tmp_path / "out.tar.gz"
        with _tarfile.open(archive, "w:gz") as tar:
            count = backup._add_tree(tar, root, "crew")
        with _tarfile.open(archive) as tar:
            names = tar.getnames()
        assert count == 1
        assert names == ["crew/real.json"]

    def test_depth_ceiling_prunes_instead_of_exhausting_descriptors(self, tmp_path):
        import tarfile as _tarfile

        from kiro_crew.apps.builtins.aws_control.backend import backup

        # The ceiling is a property of the PINNED descent specifically: it holds
        # one descriptor per level, so a pathological tree could exhaust the
        # process's fd budget. The name-based fallback holds none and therefore
        # needs no ceiling - adding one there would silently truncate a Windows
        # backup for no safety reason.
        if not backup._CAN_PIN_TRAVERSAL:
            pytest.skip("the depth ceiling guards the pinned descent's fd budget only")
        root = tmp_path / "sessions"
        deep = root
        for i in range(backup._MAX_TREE_DEPTH + 3):
            deep = deep / f"d{i}"
        deep.mkdir(parents=True)
        (deep / "buried.json").write_bytes(b"{}")
        (root / "top.json").write_bytes(b"{}")

        archive = tmp_path / "out.tar.gz"
        with _tarfile.open(archive, "w:gz") as tar:
            count = backup._add_tree(tar, root, "crew")
        with _tarfile.open(archive) as tar:
            names = tar.getnames()
        # The shallow file is archived; the one past the ceiling is pruned, and
        # the call returns rather than running the process out of descriptors.
        assert "crew/top.json" in names
        assert not any("buried.json" in n for n in names)
        assert count >= 1

    def test_stderr_is_redacted_before_it_is_trimmed(self):
        # Trimming first can cut a credential in half, and a half token matches
        # no redaction pattern - so the fragment survives every later pass. The
        # secret is placed so that a naive `err[:200]` would split it.
        from kiro_crew.deploy import engine

        secret = "AKIAIOSFODNN7EXAMPLE"
        padded = "x" * 190 + secret + " trailing context"
        out = engine._trimmed_stderr(padded)

        assert len(out) <= 200
        # Neither the whole token nor a recognisable head of it may survive.
        assert secret not in out
        assert secret[:10] not in out


class TestRound26Hardening:
    """Round-26 pins: fallback member names are '/'-joined, and the recorded
    consent must name the account being uploaded to."""

    def _authorizing_env(self, grant_account: str):
        """Everything authorizes except the grant's account, which is the variable."""
        grant = SimpleNamespace(
            service="s3",
            profile="p",
            region="us-west-2",
            account=grant_account,
            arn="arn:aws:sts::x:assumed-role/y",
            granted_at="2026-01-01T00:00:00Z",
        )
        return (
            mock.patch(
                "kiro_crew.deploy.engine._checked",
                return_value=json.dumps({"Account": ACCOUNT}),
            ),
            mock.patch("kiro_crew.apps.manager.is_app_enabled", return_value=True),
            mock.patch("kiro_crew.aws_consent.is_granted", return_value=(True, "")),
            mock.patch("kiro_crew.aws_consent.read_grant", return_value=grant),
        )

    def test_grant_for_another_account_refuses_the_upload(self):
        # The chain this closes: a backup for account A starts, the same profile
        # is repointed and consent is recorded for account B, then the profile
        # points back at A. `is_granted` matches profile+region and says yes, and
        # the live probe says A -- but the consent on file belongs to B, so the
        # owner never approved S3 use for A. Refuse.
        from kiro_crew.apps.builtins.aws_control.backend import backup

        p1, p2, p3, p4 = self._authorizing_env("999988887777")
        with p1, p2, p3, p4:
            backup.clear_stop()
            with pytest.raises(RuntimeError, match="does not name this account"):
                backup._authorize_upload(
                    ACCOUNT,
                    "p",
                    "us-west-2",
                    caller=backup.CALLER_OWNER,
                    payload_kind=backup.KIND_SNAPSHOT,
                )

    def test_grant_naming_no_account_refuses_the_upload(self):
        from kiro_crew.apps.builtins.aws_control.backend import backup

        p1, p2, p3, p4 = self._authorizing_env("")
        with p1, p2, p3, p4:
            backup.clear_stop()
            with pytest.raises(RuntimeError, match="does not name this account"):
                backup._authorize_upload(
                    ACCOUNT,
                    "p",
                    "us-west-2",
                    caller=backup.CALLER_OWNER,
                    payload_kind=backup.KIND_SNAPSHOT,
                )

    def test_matching_grant_account_allows_the_upload(self):
        from kiro_crew.apps.builtins.aws_control.backend import backup

        p1, p2, p3, p4 = self._authorizing_env(ACCOUNT)
        with p1, p2, p3, p4:
            backup.clear_stop()
            backup._authorize_upload(
                ACCOUNT,
                "p",
                "us-west-2",
                caller=backup.CALLER_OWNER,
                payload_kind=backup.KIND_SNAPSHOT,
            )  # no raise

    def test_grant_withdrawn_mid_build_refuses_the_upload(self):
        from kiro_crew.apps.builtins.aws_control.backend import backup

        with (
            mock.patch(
                "kiro_crew.deploy.engine._checked",
                return_value=json.dumps({"Account": ACCOUNT}),
            ),
            mock.patch("kiro_crew.apps.manager.is_app_enabled", return_value=True),
            mock.patch("kiro_crew.aws_consent.is_granted", return_value=(True, "")),
            mock.patch("kiro_crew.aws_consent.read_grant", return_value=None),
        ):
            backup.clear_stop()
            with pytest.raises(RuntimeError, match="withdrawn"):
                backup._authorize_upload(
                    ACCOUNT,
                    "p",
                    "us-west-2",
                    caller=backup.CALLER_OWNER,
                    payload_kind=backup.KIND_SNAPSHOT,
                )


class TestRound29Hardening:
    """Round-29 pins: the bill is scoped to ONE account, and registration keeps
    the region the profile itself declares."""

    def test_cost_query_filters_to_the_selected_account(self, tmp_path, monkeypatch):
        # A management (payer) profile returns the whole organization's spend
        # unless the query says otherwise, and this app would cache that as one
        # account's bill. The filter is what makes the figure mean what the page
        # claims. Asserting on the argv is the only way to pin it.
        from kiro_crew.apps.builtins.aws_control.backend import costs

        monkeypatch.setattr(costs, "_cache_path", lambda account: tmp_path / "c.json")
        payload = json.dumps({"ResultsByTime": []})
        with mock.patch.object(costs, "_checked", return_value=payload) as checked:
            costs.fetch_month_costs("p", "us-west-2", ACCOUNT)
        argv = checked.call_args.args[0]
        assert "--filter" in argv
        spec = json.loads(argv[argv.index("--filter") + 1])
        assert spec == {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [ACCOUNT]}}

    def test_registration_records_the_profiles_own_region(self):
        handlers = _registered()
        reg = {"version": 2, "profiles": [], "default": ""}

        @contextlib.contextmanager
        def _fake_locked():
            yield reg

        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(routes_mod, "is_owner_dashboard_request", return_value=True),
            mock.patch.object(
                routes_mod.deploy_profiles, "discover_aws_profiles", return_value=["eu-prof"]
            ),
            mock.patch.object(
                routes_mod.accounts_mod,
                "configured_region",
                AsyncMock(return_value="eu-central-1"),
            ),
            mock.patch.object(routes_mod.deploy_profiles, "locked_registry", _fake_locked),
            mock.patch.object(routes_mod.accounts_mod, "invalidate_cache"),
        ):
            req = _request("POST", "/profiles/register")
            req.json = AsyncMock(return_value={"names": ["eu-prof"]})  # type: ignore[method-assign]
            resp = asyncio.run(handlers[("POST", "/profiles/register")](req))  # type: ignore[operator]
        assert _payload(resp) == {"added": 1, "skipped": 0}
        # Not DEFAULT_REGION: registering a profile configured elsewhere with an
        # empty region would create its drive bucket in the wrong region.
        assert reg["profiles"][0]["region"] == "eu-central-1"
        # The account is still left for a live probe to resolve.
        assert reg["profiles"][0]["account"] == ""

    def test_a_profile_declaring_no_region_still_registers(self):
        handlers = _registered()
        reg = {"version": 2, "profiles": [], "default": ""}

        @contextlib.contextmanager
        def _fake_locked():
            yield reg

        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(routes_mod, "is_owner_dashboard_request", return_value=True),
            mock.patch.object(
                routes_mod.deploy_profiles, "discover_aws_profiles", return_value=["bare"]
            ),
            mock.patch.object(
                routes_mod.accounts_mod, "configured_region", AsyncMock(return_value="")
            ),
            mock.patch.object(routes_mod.deploy_profiles, "locked_registry", _fake_locked),
            mock.patch.object(routes_mod.accounts_mod, "invalidate_cache"),
        ):
            req = _request("POST", "/profiles/register")
            req.json = AsyncMock(return_value={"names": ["bare"]})  # type: ignore[method-assign]
            resp = asyncio.run(handlers[("POST", "/profiles/register")](req))  # type: ignore[operator]
        assert _payload(resp) == {"added": 1, "skipped": 0}
        # make_entry's own default applies only when the profile declares nothing.
        assert reg["profiles"][0]["region"] == routes_mod.deploy_profiles.DEFAULT_REGION

    def test_configured_region_refuses_a_value_that_is_not_a_region(self):
        # ~/.aws/config is operator-writable text and this value flows into an
        # argv, so a non-region string must read as "declares none" rather than
        # being trusted.
        from kiro_crew.apps.builtins.aws_control.backend import accounts

        for bad in ("not a region", "us-west-2; rm -rf ~", ""):
            with mock.patch.object(accounts.engine, "run_aws", return_value=(0, bad, "")):
                assert asyncio.run(accounts.configured_region("p")) == ""
        with mock.patch.object(
            accounts.engine, "run_aws", return_value=(0, "ap-southeast-2\n", "")
        ):
            assert asyncio.run(accounts.configured_region("p")) == "ap-southeast-2"


class TestRound36Hardening:
    """A dead nightly loop, and a credential-shaped profile name on a display card."""

    def test_a_timezone_less_last_run_reads_as_due_instead_of_crashing(self):
        # The nastier half of this class: a timezone-LESS ISO stamp parses fine,
        # so it escapes the try/except entirely and would raise TypeError on the
        # aware subtraction that sits OUTSIDE the guard -- in the nightly loop,
        # every wake, so a backup the owner enabled silently never ran.
        from kiro_crew.apps.builtins.aws_control.backend import backup

        with (
            mock.patch.object(backup, "nightly_enabled", return_value=True),
            mock.patch.object(
                backup,
                "last_runs",
                return_value={backup.KIND_SNAPSHOT: {"at": "2026-01-01T00:00:00"}},
            ),
        ):
            # Naive stamp far in the past: due, and no exception.
            assert backup.due_for_nightly(ACCOUNT) is True

    def test_a_recent_timezone_less_last_run_is_not_due(self):
        # The normalization must not make every naive stamp "due" -- it is read as
        # UTC, the same treatment costs.is_fresh and shares._prune already apply.
        import datetime as _dt

        from kiro_crew.apps.builtins.aws_control.backend import backup

        # flake-ok: pre-existing UTC wall-clock read, moved verbatim; the product reads the same clock and the due window is about a day
        recent = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat()
        with (
            mock.patch.object(backup, "nightly_enabled", return_value=True),
            mock.patch.object(
                backup, "last_runs", return_value={backup.KIND_SNAPSHOT: {"at": recent}}
            ),
        ):
            assert backup.due_for_nightly(ACCOUNT) is False

    def test_a_non_string_last_run_reads_as_due(self):
        from kiro_crew.apps.builtins.aws_control.backend import backup

        for bad in (5, ["2026-01-01"], None, {}):
            with (
                mock.patch.object(backup, "nightly_enabled", return_value=True),
                mock.patch.object(
                    backup, "last_runs", return_value={backup.KIND_SNAPSHOT: {"at": bad}}
                ),
            ):
                assert backup.due_for_nightly(ACCOUNT) is True

    def test_a_credential_shaped_profile_name_is_redacted_in_the_reconnect_command(self):
        # _PROFILE_RE is a shell-safety gate, not a secret gate: it admits
        # [A-Za-z0-9._@=+-] up to 128 chars, so an access key id passes it and
        # would render verbatim into the guidance card and the clipboard.
        from kiro_crew.apps.builtins.aws_control.backend import accounts

        key = "AKIAIOSFODNN7EXAMPLE"
        plan = accounts.reconnect_plan(accounts.KIND_SSO, key)
        assert key not in plan["command"]
        # Still a usable command shape, not an empty string.
        assert plan["command"].startswith("aws sso login --profile ")

    def test_an_ordinary_profile_name_survives_the_reconnect_command(self):
        from kiro_crew.apps.builtins.aws_control.backend import accounts

        plan = accounts.reconnect_plan(accounts.KIND_SSO, "team-prod")
        assert plan["command"] == "aws sso login --profile team-prod"
