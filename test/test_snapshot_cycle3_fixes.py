"""Tests for the cycle-3 review findings.

All three are cases where a partial or unrecognised state was treated as an
acceptable outcome: a tree the archive lacks was left behind, a rollback directory
was merged with another restore's, and a dual-layer KMS bucket read as "not KMS".
"""

from __future__ import annotations

import json
import shutil
import tarfile
from datetime import datetime, timezone

import pytest
from test_snapshot import _setup_fake_kirocrew

from kiro_crew import snapshot as snap
from kiro_crew import snapshot_remote as remote
from kiro_crew.deploy import engine

ACCOUNT = "123456789012"
NO_POLICY = (255, "", "An error occurred (NoSuchBucketPolicy) when calling GetBucketPolicy")


def _peek(bundle, tmp_path):
    """Unpack a bundle so a test can assert on what it actually contains."""
    dest = tmp_path / "peek"
    if not dest.is_dir():
        dest.mkdir()
        with tarfile.open(bundle) as tf:
            tf.extractall(dest)
    return next(p for p in dest.iterdir() if p.is_dir())


def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    _setup_fake_kirocrew(home)
    return home


def _authorize(account: str = ACCOUNT, region: str = "us-west-2"):
    """setup_destination requires an out-of-band authorization; seed one."""
    token = remote.authorization_token_path()
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(json.dumps({"account": account, "region": region}))
    return token


class TestReplaceDoesNotKeepATreeTheArchiveLacks:
    """A bundle without `workspace/knowledge` used to leave the destination's own
    knowledge tree in place, so a "replace" produced restored memory mixed with stale
    notes — and reported success. Replace means the destination matches the archive."""

    def test_a_tree_absent_from_the_bundle_is_removed(self, tmp_path, monkeypatch):
        home = _home(tmp_path, monkeypatch)
        md = home / "workspace" / "memory"
        md.mkdir(parents=True, exist_ok=True)
        (md / "preferences.md").write_text("original")
        # The fixture ships a knowledge tree; remove it so the bundle genuinely
        # cannot carry one. Without this the case under test does not exist.
        shutil.rmtree(home / "workspace" / "knowledge")
        out = tmp_path / "out"
        assert snap.snapshot_main([str(out), "--components", "memory"]) == 0
        bundle = next(out.glob("kirocrew-snapshot-*.tar.gz"))
        assert not (
            _peek(bundle, tmp_path) / "workspace" / "knowledge"
        ).exists(), "the bundle carries a knowledge tree, so this test proves nothing"

        # The destination has since grown one.
        kd = home / "workspace" / "knowledge"
        kd.mkdir(parents=True, exist_ok=True)
        (kd / "stale.md").write_text("not in the bundle")

        assert (
            snap.restore_main(
                [str(bundle), "--mode", "replace", "--force", "--components", "memory"]
            )
            == 0
        )
        assert not (
            kd / "stale.md"
        ).exists(), "a tree the bundle does not carry survived a replace restore"
        assert (md / "preferences.md").read_text() == "original"

    def test_the_removed_tree_is_still_in_the_rollback_copy(self, tmp_path, monkeypatch):
        """Clearing is only defensible because the state is recoverable."""
        home = _home(tmp_path, monkeypatch)
        md = home / "workspace" / "memory"
        md.mkdir(parents=True, exist_ok=True)
        (md / "preferences.md").write_text("original")
        shutil.rmtree(home / "workspace" / "knowledge")
        out = tmp_path / "out"
        assert snap.snapshot_main([str(out), "--components", "memory"]) == 0
        bundle = next(out.glob("kirocrew-snapshot-*.tar.gz"))

        kd = home / "workspace" / "knowledge"
        kd.mkdir(parents=True, exist_ok=True)
        (kd / "stale.md").write_text("not in the bundle")

        assert (
            snap.restore_main(
                [str(bundle), "--mode", "replace", "--force", "--components", "memory"]
            )
            == 0
        )
        saved = list(home.glob("pre-restore-*/workspace/knowledge/stale.md"))
        assert saved and saved[0].read_text() == "not in the bundle"

    def test_a_bundle_that_has_the_tree_still_replaces_it(self, tmp_path, monkeypatch):
        home = _home(tmp_path, monkeypatch)
        md = home / "workspace" / "memory"
        md.mkdir(parents=True, exist_ok=True)
        (md / "preferences.md").write_text("original")
        kd = home / "workspace" / "knowledge"
        kd.mkdir(parents=True, exist_ok=True)
        (kd / "note.md").write_text("from the bundle")
        out = tmp_path / "out"
        assert snap.snapshot_main([str(out), "--components", "memory"]) == 0
        bundle = next(out.glob("kirocrew-snapshot-*.tar.gz"))

        (kd / "note.md").write_text("changed since the backup")
        assert (
            snap.restore_main(
                [str(bundle), "--mode", "replace", "--force", "--components", "memory"]
            )
            == 0
        )
        assert (kd / "note.md").read_text() == "from the bundle"


class TestTwoRestoresInOneSecondKeepSeparateRollbackSets:
    """The rollback directory name is second-granular, so two restores inside one second
    resolve to the same name. One directory holding two pre-restore states would roll back
    to neither, so each restore gets its own — allocated, not merged, and not crashed."""

    def test_the_second_restore_gets_its_own_rollback_set(self, tmp_path, monkeypatch):
        home = _home(tmp_path, monkeypatch)
        md = home / "workspace" / "memory"
        md.mkdir(parents=True, exist_ok=True)
        (md / "preferences.md").write_text("original")
        out = tmp_path / "out"
        assert snap.snapshot_main([str(out), "--components", "memory"]) == 0
        bundle = next(out.glob("kirocrew-snapshot-*.tar.gz"))

        frozen = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        class _Frozen:
            @staticmethod
            def now(tz=None):
                return frozen

        monkeypatch.setattr(snap, "datetime", _Frozen)

        (md / "preferences.md").write_text("live state A")
        assert (
            snap.restore_main(
                [str(bundle), "--mode", "replace", "--force", "--components", "memory"]
            )
            == 0
        )
        assert (md / "preferences.md").read_text() == "original"

        # Second restore in the SAME frozen second. Earlier this raised an uncaught
        # FileExistsError; a collision has to be resolved, not thrown, because a crash is
        # not the clean abort it was described as.
        (md / "preferences.md").write_text("live state B")
        assert (
            snap.restore_main(
                [str(bundle), "--mode", "replace", "--force", "--components", "memory"]
            )
            == 0
        )

        saved = sorted(p.name for p in home.glob("pre-restore-*"))
        assert len(saved) == 2, f"each restore needs its own rollback set: {saved}"
        # And neither set holds the other's state: A was saved by the first restore, B by
        # the second, so the two directories differ in content.
        contents = {
            (home / name / "workspace" / "memory" / "preferences.md").read_text() for name in saved
        }
        assert contents == {"live state A", "live state B"}, contents


def _extract(bundle, home, tmp_path):
    """Unpack a bundle so _do_replace can be called directly."""
    import tarfile

    dest = tmp_path / f"x{bundle.stat().st_mtime_ns}"
    dest.mkdir()
    with tarfile.open(bundle) as tf:
        tf.extractall(dest)
    inner = next(p for p in dest.iterdir() if p.is_dir())
    return inner, home, ["memory"]


class TestDualLayerKmsIsRecognised:
    """S3 spells KMS two ways. Matching only `aws:kms` read a DSSE-KMS bucket as not
    KMS, so hardening's AES256 stood and the operator's dual-layer setting was gone."""

    @pytest.mark.parametrize("algo", ["aws:kms", "aws:kms:dsse"])
    def test_both_kms_spellings_count_as_kms(self, algo):
        cfg = {"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": algo}}]}
        assert remote._uses_kms(cfg) is True

    @pytest.mark.parametrize("algo", ["AES256", "", None])
    def test_non_kms_is_not_mistaken_for_kms(self, algo):
        cfg = {"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": algo}}]}
        assert remote._uses_kms(cfg) is False

    def test_a_dsse_bucket_is_refused_like_any_other_kms_bucket(self, tmp_path, monkeypatch):
        """Recognising the second spelling is what makes the refusal cover it. Before,
        `aws:kms:dsse` read as not-KMS and the bucket was silently downgraded."""
        from test_snapshot_remote import FakeAws

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        dsse = {"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms:dsse"}}]}
        fake = FakeAws(
            {
                "sts get-caller-identity": (0, ACCOUNT + "\n", ""),
                "s3api head-bucket": (0, "", ""),
                "s3api get-bucket-encryption": (
                    0,
                    json.dumps({"ServerSideEncryptionConfiguration": dsse}),
                    "",
                ),
            }
        )
        monkeypatch.setattr(engine, "run_aws", fake)
        monkeypatch.setattr(engine, "harden_bucket", lambda *a, **k: None)
        _authorize()
        with pytest.raises(remote.DestinationError) as e:
            remote.setup_destination("p", "us-west-2")
        assert "SSE-KMS" in str(e.value)
        assert fake.argv_for("s3api put-bucket-encryption") == []
