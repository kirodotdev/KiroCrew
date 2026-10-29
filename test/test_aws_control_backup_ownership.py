"""AWS Control backup ownership: archive descent, install identity, restore gate.

Covers ``_add_tree`` skipping hard-linked files, the refusal to archive when
descriptor-pinned traversal is unavailable, ``_install_folders`` reading
install prefixes, ``list_remote_backups`` and ``classify_key`` deciding archive
origin, and the install id, kept apart from its display label.

It also pins that ``restore_download`` refuses an archive this install cannot
prove it uploaded, by local upload record and body fingerprint, unless the
caller passes ``foreign_ok``, and refuses a staging directory that resolves
outside app storage. Shared constants come from ``aws_control_backup_helpers.py``.
"""

from __future__ import annotations

import errno
import hashlib
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
        # in prose explaining why the pinned descent replaces it. Every module of the
        # engine is read, not only the facade: the descent lives in ``backup_parts``.
        import ast

        assert not hasattr(backup, "_add_tree_by_name")
        facade = Path(backup.__file__)
        sources = [facade, *sorted((facade.parent / "backup_parts").glob("*.py"))]
        assert len(sources) > 2
        calls = [
            f"{source.name}:{node.lineno}"
            for source in sources
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8")))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "walk"
        ]
        assert calls == []


# ---------------------------------------------------------------------------
# list_remote_backups
# ---------------------------------------------------------------------------


class TestInstallFoldersReasonAboutAbsence:
    """``_install_folders`` answers "is another install writing here" by what it finds.

    So it must read the COMPLETE, unredacted set of install prefixes and refuse an
    answer it could not read, rather than report "nothing found". Patched at
    ``backup._checked`` -- the one AWS call it makes -- so no listing reaches AWS.
    """

    MINE = "0123456789abcdef0123456789abcdef"
    OTHER = "fedcba9876543210fedcba9876543210"

    def _folders(self, monkeypatch, out):
        calls = []

        def fake_checked(argv, profile, *, action, timeout):
            calls.append((argv, profile, action, timeout))
            return out

        monkeypatch.setattr(backup, "_checked", fake_checked)
        found = backup._install_folders(
            "p", "us-east-1", "b", backup.KIND_SNAPSHOT, account=ACCOUNT
        )
        return found, calls

    def test_only_install_ids_directly_under_the_kind_prefix_count(self, monkeypatch):
        rows = [
            f"backup/snapshots/{self.MINE}/",
            f"backup/snapshots/{self.OTHER}/",
            "backup/snapshots/not-an-install/",
            f"backup/sessions/{self.OTHER.replace('f', 'e')}/",
            17,
        ]
        found, calls = self._folders(monkeypatch, json.dumps(rows))
        assert found == {self.MINE, self.OTHER}
        [(argv, profile, action, timeout)] = calls
        assert (profile, action, timeout) == ("p", "s3:ListBucket", 60)
        # The projection with no --max-items is what lets the CLI merge every page.
        assert "--max-items" not in argv
        assert argv[argv.index("--prefix") + 1] == "backup/snapshots/"
        assert argv[argv.index("--expected-bucket-owner") + 1] == ACCOUNT

    def test_an_empty_answer_is_an_empty_set(self, monkeypatch):
        assert self._folders(monkeypatch, "")[0] == set()
        assert self._folders(monkeypatch, "null")[0] == set()

    def test_an_unreadable_answer_is_refused_rather_than_read_as_unshared(self, monkeypatch):
        with pytest.raises(backup.AWSError, match="refusing to report the prefix as unshared"):
            self._folders(monkeypatch, "{not json")


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

    def test_the_id_is_not_the_telemetry_install_id(self, tmp_path, monkeypatch):
        # Reusing `beacon.install_id()` would materialise a TELEMETRY identity on
        # a host that opted out of telemetry, because that function creates the
        # file it reads. The technique is worth copying; the value is not.
        from kiro_crew import beacon
        from kiro_crew.config.loader import config_dir

        # Unpatched, minting the backup id leaves no telemetry id file behind.
        identity = backup.install_identity()
        assert backup._INSTALL_ID_RE.match(identity["id"])
        assert not (config_dir() / beacon.INSTALL_ID_FILE).exists()
        # A second fresh mint, with the telemetry reader patched to catch any call.
        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "second.json")
        backup._fallback_identity.clear()
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
