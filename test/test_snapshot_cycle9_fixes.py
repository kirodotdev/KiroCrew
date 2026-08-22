"""Tests for the cycle-9 review findings.

All three were shortfalls in the previous cycle's fixes, and two share one shape: a check
written so that it cannot fail (a vacuous comparison) or a parse that answers "empty"
when it means "unreadable".
"""

from __future__ import annotations

import argparse
import json

import pytest
from test_snapshot import _setup_fake_kirocrew
from test_snapshot_remote import FakeAws

from kiro_crew import backup_cli
from kiro_crew import snapshot as snap
from kiro_crew import snapshot_remote as remote
from kiro_crew.deploy import engine

ACCOUNT = "123456789012"
NO_POLICY = (255, "", "An error occurred (NoSuchBucketPolicy) when calling GetBucketPolicy")
OUR_TAG = (0, json.dumps({"TagSet": [{"Key": "kirocrew:backup", "Value": "true"}]}), "")


def _fake(extra: dict | None = None) -> FakeAws:
    answers = {
        "sts get-caller-identity": (0, ACCOUNT + "\n", ""),
        "s3api get-public-access-block": (
            0,
            json.dumps(
                {
                    "PublicAccessBlockConfiguration": {
                        "BlockPublicAcls": True,
                        "IgnorePublicAcls": True,
                        "BlockPublicPolicy": True,
                        "RestrictPublicBuckets": True,
                    }
                }
            ),
            "",
        ),
        "s3api get-bucket-encryption": (
            0,
            json.dumps(
                {
                    "ServerSideEncryptionConfiguration": {
                        "Rules": [
                            {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                        ]
                    }
                }
            ),
            "",
        ),
        "s3api get-bucket-versioning": (0, json.dumps({"Status": "Enabled"}), ""),
        # Hardening sets ownership, so verification reads it back:
        # BucketOwnerEnforced disables ACLs and BPA does not cover that.
        "s3api get-bucket-ownership-controls": (
            0,
            json.dumps(
                {"OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}}
            ),
            "",
        ),
        "s3api get-bucket-policy": NO_POLICY,
        "s3api get-bucket-tagging": OUR_TAG,
    }
    answers.update(extra or {"s3api head-bucket": (1, "", "Not Found")})
    return FakeAws(answers)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


def _authorize(account: str = ACCOUNT, region: str = "us-west-2"):
    """setup_destination requires an out-of-band authorization; seed one per call."""
    token = remote.authorization_token_path()
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(json.dumps({"account": account, "region": region}))
    return token


class TestABucketBoundTokenCannotBeSpentOnTheDefault:
    """The first version compared `approved_bucket != (bucket or approved_bucket)`.

    That `or` made the comparison vacuous whenever `--bucket` was omitted: it compared
    the approved bucket against itself and passed. A token pinned to one bucket therefore
    authorized the DEFAULT one. Writing a conditional default inside a comparison is a
    reliable way to produce a check that cannot fail.
    """

    def _args(self, **kw) -> argparse.Namespace:
        base = dict(aws_profile="default", region="us-west-2", bucket=None)
        base.update(kw)
        return argparse.Namespace(**base)

    def _prepared(self, monkeypatch):
        monkeypatch.setattr(backup_cli, "_resolve_aws_profile", lambda _n: ("p", "us-west-2"))
        monkeypatch.setattr(remote, "caller_account", lambda _p: ACCOUNT)
        calls: list[str] = []

        def fake_setup(*_a, **_k):
            calls.append("ran")
            return (
                remote.Destination(
                    bucket="b", region="us-west-2", account=ACCOUNT, created_at="now"
                ),
                True,
                {
                    "block_public_access": {"a": True, "b": True, "c": True, "d": True},
                    "sse": "AES256",
                    "versioning": "Enabled",
                },
            )

        monkeypatch.setattr(remote, "setup_destination", fake_setup)
        return calls

    def _token(self, payload: dict) -> None:
        t = remote.authorization_token_path()
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text(json.dumps(payload))

    def test_omitting_the_bucket_flag_does_not_bypass_the_binding(self, home, monkeypatch, capsys):
        calls = self._prepared(monkeypatch)
        self._token(
            {
                "account": ACCOUNT,
                "region": "us-west-2",
                "bucket": "a-bucket-that-is-not-the-default",
            }
        )
        rc = backup_cli.setup_main(self._args())  # no --bucket
        out = capsys.readouterr().out
        assert rc == 1, "a bucket-pinned token authorized the default bucket"
        assert calls == []
        assert "bucket" in out
        # The message names the bucket that WOULD have been used, not just the approved.
        assert remote.default_bucket_name(ACCOUNT, "us-west-2") in out

    def test_a_token_pinned_to_the_default_is_accepted(self, home, monkeypatch):
        calls = self._prepared(monkeypatch)
        self._token(
            {
                "account": ACCOUNT,
                "region": "us-west-2",
                "bucket": remote.default_bucket_name(ACCOUNT, "us-west-2"),
            }
        )
        assert backup_cli.setup_main(self._args()) == 0
        assert calls == ["ran"]

    def test_an_explicit_bucket_still_has_to_match(self, home, monkeypatch):
        calls = self._prepared(monkeypatch)
        self._token({"account": ACCOUNT, "region": "us-west-2", "bucket": "approved"})
        assert backup_cli.setup_main(self._args(bucket="different")) == 1
        assert calls == []


class TestUnparseableAwsOutputNeverReadsAsEmpty:
    """`output = text` in a profile is enough to make json.loads fail. Answering "empty"
    there is worse than failing: the caller then REPLACES the configuration, deleting
    what it could not read."""

    def _recorded(self, monkeypatch):
        """Record a destination first, so the OWNERSHIP tag read is skipped.

        Both the ownership check and the tag-merge read call get-bucket-tagging, so an
        arbitrary bucket cannot tell which one refused — the same ambiguity that let an
        earlier version of a test pass with the guard removed.
        """
        first = _fake()
        monkeypatch.setattr(engine, "run_aws", first)
        _authorize()
        dest, _created, _r = remote.setup_destination("p", "us-west-2")
        return dest

    def test_unparseable_tags_refuse(self, home, monkeypatch):
        dest = self._recorded(monkeypatch)
        fake = _fake({"s3api head-bucket": (0, "", "")})
        fake.answers["s3api get-bucket-tagging"] = (0, "TAGS\tkirocrew:backup\ttrue", "")
        monkeypatch.setattr(engine, "run_aws", fake)
        with pytest.raises(remote.DestinationError) as e:
            _authorize()
            remote.setup_destination("p", "us-west-2", bucket=dest.bucket)
        assert "could not be parsed" in str(e.value)
        assert fake.argv_for("s3api put-bucket-tagging") == []

    def test_unparseable_lifecycle_refuses(self, home, monkeypatch):
        dest = self._recorded(monkeypatch)
        fake = _fake({"s3api head-bucket": (0, "", "")})
        fake.answers["s3api get-bucket-lifecycle-configuration"] = (0, "RULES\tid\t30", "")
        monkeypatch.setattr(engine, "run_aws", fake)
        with pytest.raises(remote.DestinationError) as e:
            _authorize()
            remote.setup_destination("p", "us-west-2", bucket=dest.bucket)
        assert "could not be parsed" in str(e.value)
        assert fake.argv_for("s3api put-bucket-lifecycle-configuration") == []

    def test_a_wrongly_shaped_tagset_refuses(self, home, monkeypatch):
        dest = self._recorded(monkeypatch)
        fake = _fake({"s3api head-bucket": (0, "", "")})
        fake.answers["s3api get-bucket-tagging"] = (0, json.dumps({"TagSet": "oops"}), "")
        monkeypatch.setattr(engine, "run_aws", fake)
        with pytest.raises(remote.DestinationError) as e:
            _authorize()
            remote.setup_destination("p", "us-west-2", bucket=dest.bucket)
        assert "unexpected shape" in str(e.value)

    def test_every_parsed_read_pins_the_output_format(self, home, monkeypatch):
        """Belt as well as braces: refusing on an unparseable answer is the safety net,
        but pinning the format means a merely-unusual profile never trips it."""
        fake = _fake()
        monkeypatch.setattr(engine, "run_aws", fake)
        try:
            _authorize()
            remote.setup_destination("p", "us-west-2")
        except remote.DestinationError:
            pass
        parsed_reads = [
            c
            for c in fake.calls
            if c[:1] == ["s3api"]
            and c[1].startswith("get-")
            and c[1] != "get-bucket-policy"
            or (c[:1] == ["s3api"] and c[1] == "get-bucket-policy")
        ]
        assert parsed_reads, "no reads were made"
        for call in parsed_reads:
            assert "--output" in call and "json" in call, f"unpinned read: {call}"


class TestAnUnsafeDestinationRootRefusesBeforeAnyMutation:
    """The staging side was fixed last cycle; the RESTORE side kept skipping.

    `_backup_and_copy` swaps the databases before the tree loops run, so skipping an
    unsafe markdown tree left memory split between two versions — and the command
    reported success. Validation is now hoisted ahead of every mutation.
    """

    def _bundle(self, tmp_path, monkeypatch):
        home = tmp_path / "src"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        _setup_fake_kirocrew(home)
        md = home / "workspace" / "memory"
        md.mkdir(parents=True, exist_ok=True)
        (md / "preferences.md").write_text("from the bundle")
        out = tmp_path / "out"
        assert snap.snapshot_main([str(out), "--components", "memory"]) == 0
        return next(out.glob("kirocrew-snapshot-*.tar.gz"))

    def _linked_dest(self, tmp_path, name: str, tree: str):
        """A seeded data home whose *tree* root is a symlink pointing outside it.

        Order matters: seed FIRST, then replace the real directory with the link.
        Creating the link first makes the seeding write straight through it, which is a
        fixture bug that quietly invalidates the assertion about the outside directory.
        """
        import shutil

        dest = tmp_path / name
        dest.mkdir()
        _setup_fake_kirocrew(dest)
        outside = tmp_path / f"outside-{name}"
        outside.mkdir()
        target = dest / tree
        if target.is_dir():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(outside, target_is_directory=True)
        return dest, outside

    def test_replace_refuses_before_replacing_the_databases(self, tmp_path, monkeypatch):
        bundle = self._bundle(tmp_path, monkeypatch)
        try:
            dest, _outside = self._linked_dest(tmp_path, "dest", "workspace/memory")
        except (OSError, NotImplementedError):
            pytest.skip("cannot create a directory symlink on this platform")
        original = (dest / "memory.db").read_bytes() if (dest / "memory.db").is_file() else None
        monkeypatch.setenv("KIROCREW_HOME", str(dest))

        rc = snap.restore_main(
            [str(bundle), "--components", "memory", "--mode", "replace", "--force"]
        )
        assert rc == 1
        # The database was NOT swapped, and no rollback directory was created.
        if original is not None:
            assert (dest / "memory.db").read_bytes() == original
        assert list(dest.glob("pre-restore-*")) == []

    def test_merge_refuses_too(self, tmp_path, monkeypatch):
        bundle = self._bundle(tmp_path, monkeypatch)
        try:
            dest, outside = self._linked_dest(tmp_path, "dest2", "workspace/knowledge")
        except (OSError, NotImplementedError):
            pytest.skip("cannot create a directory symlink on this platform")
        monkeypatch.setenv("KIROCREW_HOME", str(dest))
        rc = snap.restore_main(
            [str(bundle), "--components", "memory", "--mode", "merge", "--force"]
        )
        assert rc == 1
        assert list(outside.iterdir()) == []

    def test_a_clean_destination_still_restores(self, tmp_path, monkeypatch):
        """The refusal must not fire on an ordinary home."""
        bundle = self._bundle(tmp_path, monkeypatch)
        dest = tmp_path / "clean"
        dest.mkdir()
        _setup_fake_kirocrew(dest)
        monkeypatch.setenv("KIROCREW_HOME", str(dest))
        assert (
            snap.restore_main(
                [str(bundle), "--components", "memory", "--mode", "replace", "--force"]
            )
            == 0
        )
        assert (dest / "workspace" / "memory" / "preferences.md").read_text() == ("from the bundle")

    def test_the_refusal_is_a_message_not_a_traceback(self, tmp_path, monkeypatch, capsys):
        bundle = self._bundle(tmp_path, monkeypatch)
        try:
            dest, _outside = self._linked_dest(tmp_path, "dest3", "workspace/memory")
        except (OSError, NotImplementedError):
            pytest.skip("cannot create a directory symlink on this platform")
        monkeypatch.setenv("KIROCREW_HOME", str(dest))
        snap.restore_main([str(bundle), "--components", "memory", "--mode", "replace", "--force"])
        out = capsys.readouterr().out
        assert "do not resolve inside the data home" in out
        assert "Nothing has been changed" in out
