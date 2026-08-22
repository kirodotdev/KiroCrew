"""Tests for the cycle-4 review findings.

Four independent defects: a refusal that spent the one-shot authorization, terminal
escape sequences reaching stdout from an untrusted manifest, an explicit component list
naming nothing, and a control that hardening set but verification never read back.
"""

from __future__ import annotations

import json
import tarfile

import pytest
from test_snapshot import _setup_fake_kirocrew

from kiro_crew import snapshot as snap
from kiro_crew import snapshot_remote as remote


@pytest.fixture
def home(tmp_path, monkeypatch):
    d = tmp_path / "home"
    d.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(d))
    _setup_fake_kirocrew(d)
    return d


class TestAReadOnlyRefusalDoesNotSpendTheAuthorization:
    """The token is one-shot and deleted on use. A refusal raised after consumption
    charges a fresh out-of-band token for a bucket problem the operator can just fix.
    """

    def _seed(self, home):
        token = remote.authorization_token_path()
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text(
            json.dumps({"account": "123456789012", "region": "us-west-2"}),
            encoding="utf-8",
        )
        return token

    def test_an_untagged_existing_bucket_leaves_the_token_in_place(self, home, monkeypatch):
        token = self._seed(home)
        monkeypatch.setattr(remote, "bucket_exists", lambda *a, **k: True)
        monkeypatch.setattr(remote, "_existing_encryption", lambda *a, **k: None)
        monkeypatch.setattr(remote, "_uses_kms", lambda *a, **k: False)
        monkeypatch.setattr(remote, "bucket_is_one_of_ours", lambda *a, **k: False)
        with pytest.raises(remote.DestinationError):
            remote._refuse_unusable_existing_bucket("some-bucket", "p")
        assert token.is_file(), "a read-only refusal must not consume the authorization"

    def test_a_policy_bearing_existing_bucket_leaves_the_token_in_place(self, home, monkeypatch):
        token = self._seed(home)
        monkeypatch.setattr(remote, "_existing_encryption", lambda *a, **k: None)
        monkeypatch.setattr(remote, "_uses_kms", lambda *a, **k: False)
        monkeypatch.setattr(remote, "bucket_is_one_of_ours", lambda *a, **k: True)
        monkeypatch.setattr(remote, "has_no_bucket_policy", lambda *a, **k: False)
        with pytest.raises(remote.DestinationError):
            remote._refuse_unusable_existing_bucket("some-bucket", "p")
        assert token.is_file()

    def test_a_kms_bucket_leaves_the_token_in_place(self, home, monkeypatch):
        token = self._seed(home)
        # The real shape _existing_encryption returns, so _uses_kms runs for real
        # rather than against a stub that would accept anything.
        monkeypatch.setattr(
            remote,
            "_existing_encryption",
            lambda *a, **k: {
                "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms"}}]
            },
        )
        with pytest.raises(remote.DestinationError):
            remote._refuse_unusable_existing_bucket("some-bucket", "p")
        assert token.is_file()

    def test_every_refusal_lives_in_the_one_preflight_function(self):
        """Structural: the ordering is what regressed twice, so the checks are kept
        together rather than at the sites they guard."""
        import inspect

        src = inspect.getsource(remote.setup_destination)
        pre = src.index("_refuse_unusable_existing_bucket")
        consume = src.index("consume_authorization(account, region, name)")
        assert pre < consume, "the pre-flight must run before consumption"
        after = src[consume:]
        for leaked in ("bucket_is_one_of_ours", "has_no_bucket_policy", "_uses_kms"):
            assert leaked not in after, (
                f"{leaked} is checked after the authorization is consumed; move it into "
                "_refuse_unusable_existing_bucket"
            )


class TestManifestOutputCannotDriveTheTerminal:
    def test_escape_sequences_in_manifest_fields_are_neutralised(self, tmp_path, capsys):
        snapdir = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        snapdir.mkdir()
        hostile = "\x1b[2J\x1b[Hrestored OK"
        (snapdir / "MANIFEST.json").write_text(
            json.dumps(
                {
                    "created_at": hostile,
                    "user": hostile,
                    "hostname": hostile,
                    "purpose": hostile,
                    "components": {hostile: hostile},
                }
            ),
            encoding="utf-8",
        )
        snap._print_manifest(snapdir)
        out = capsys.readouterr().out
        assert "\x1b" not in out, "an escape sequence from the manifest reached stdout"

    def test_ordinary_fields_still_render(self, tmp_path, capsys):
        snapdir = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        snapdir.mkdir()
        (snapdir / "MANIFEST.json").write_text(
            json.dumps({"created_at": "2026-01-01", "purpose": "backup"}),
            encoding="utf-8",
        )
        snap._print_manifest(snapdir)
        out = capsys.readouterr().out
        assert "2026-01-01" in out and "backup" in out


class TestAnExplicitButEmptyComponentListIsRefused:
    def test_snapshot_refuses_and_writes_nothing(self, home, tmp_path, capsys):
        out = tmp_path / "out"
        rc = snap.snapshot_main([str(out), "--components", ","])
        assert rc == 1
        assert "names no components" in capsys.readouterr().out
        assert not list(out.glob("*.tar.gz")) if out.exists() else True

    def test_snapshot_refusal_precedes_any_pruning(self, home, tmp_path):
        """The damage was retention counting an empty bundle as the newest backup."""
        out = tmp_path / "out"
        out.mkdir()
        existing = out / "kirocrew-snapshot-20260101T000000Z.tar.gz"
        with tarfile.open(existing, "w:gz") as tf:
            payload = tmp_path / "p"
            payload.mkdir()
            tf.add(str(payload), arcname="kirocrew-snapshot-20260101T000000Z")
        assert snap.snapshot_main([str(out), "--components", ",", "--keep", "1"]) == 1
        assert existing.is_file(), "a refused run must not prune a real backup"

    def test_restore_refuses_rather_than_reporting_a_no_op(self, home, tmp_path, capsys):
        bundle = tmp_path / "b.tar.gz"
        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        payload.mkdir()
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)
        rc = snap.restore_main([str(bundle), "--components", " , ", "--force"])
        assert rc == 1
        assert "names no components" in capsys.readouterr().out

    def test_a_real_selection_still_works(self, home, tmp_path):
        out = tmp_path / "out"
        assert snap.snapshot_main([str(out), "--components", "memory"]) == 0


class TestOwnershipIsVerifiedNotJustApplied:
    def _report(self, **over):
        base = {
            "block_public_access": {
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
            "sse": "AES256",
            "versioning": "Enabled",
            "ownership": "BucketOwnerEnforced",
        }
        base.update(over)
        return base

    def test_a_fully_hardened_bucket_passes(self):
        assert remote.is_fully_private(self._report()) is True

    def test_removing_bucket_owner_enforced_fails_verification(self):
        assert remote.is_fully_private(self._report(ownership="ObjectWriter")) is False

    def test_a_missing_ownership_control_fails_verification(self):
        assert remote.is_fully_private(self._report(ownership=None)) is False

    def test_the_read_back_asks_aws_for_ownership(self, monkeypatch):
        asked: list[str] = []

        def fake(args, profile, timeout=30):
            asked.append(" ".join(args[:2]))
            if args[1] == "get-bucket-ownership-controls":
                return (
                    0,
                    json.dumps(
                        {
                            "OwnershipControls": {
                                "Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]
                            }
                        }
                    ),
                    "",
                )
            return (1, "", "nope")

        monkeypatch.setattr(remote.engine, "run_aws", fake)
        report = remote.verify_bucket_private("b", "p")
        assert "s3api get-bucket-ownership-controls" in asked
        assert report["ownership"] == "BucketOwnerEnforced"
