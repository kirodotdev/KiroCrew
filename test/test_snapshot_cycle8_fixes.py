"""Tests for the cycle-8 review findings.

The theme is failing open: three of these were guards that answered "cannot tell" as if
it were "nothing to protect", and two were partial work reported as complete.
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
AES = (
    0,
    json.dumps(
        {
            "ServerSideEncryptionConfiguration": {
                "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
            }
        }
    ),
    "",
)


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
        "s3api get-bucket-encryption": AES,
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


class TestTheAuthorizationNamesItsDestination:
    """A blank permission slip is not an authorization.

    With several profiles registered, an operator could create the token intending one
    account while the caller consumes it with `--aws-profile other` — and every later
    backup would go somewhere never approved. The token has to answer "authorized to
    send WHERE", not merely "authorized".
    """

    def _args(self, **kw) -> argparse.Namespace:
        base = dict(aws_profile="default", region="us-west-2", bucket=None)
        base.update(kw)
        return argparse.Namespace(**base)

    def _prepared(self, home, monkeypatch, account=ACCOUNT):
        monkeypatch.setattr(backup_cli, "_resolve_aws_profile", lambda _n: ("p", "us-west-2"))
        monkeypatch.setattr(remote, "caller_account", lambda _p: account)
        calls: list[str] = []

        def fake_setup(*_a, **_k):
            calls.append("ran")
            return (
                remote.Destination(
                    bucket="b", region="us-west-2", account=account, created_at="now"
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

    def _write_token(self, payload) -> None:
        token = remote.authorization_token_path()
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text(payload if isinstance(payload, str) else json.dumps(payload))

    def test_a_token_for_another_account_is_refused(self, home, monkeypatch, capsys):
        calls = self._prepared(home, monkeypatch, account="111122223333")
        self._write_token({"account": ACCOUNT, "region": "us-west-2"})
        rc = backup_cli.setup_main(self._args())
        out = capsys.readouterr().out
        assert rc == 1
        assert calls == [], "setup ran against an unauthorized account"
        assert "does not match this invocation" in out
        assert "account" in out

    def test_a_token_for_another_region_is_refused(self, home, monkeypatch, capsys):
        calls = self._prepared(home, monkeypatch)
        self._write_token({"account": ACCOUNT, "region": "eu-west-1"})
        assert backup_cli.setup_main(self._args()) == 1
        assert calls == []
        assert "region" in capsys.readouterr().out

    def test_a_matching_token_is_accepted(self, home, monkeypatch):
        calls = self._prepared(home, monkeypatch)
        self._write_token({"account": ACCOUNT, "region": "us-west-2"})
        assert backup_cli.setup_main(self._args()) == 0
        assert calls == ["ran"]

    def test_a_bucket_named_in_the_token_must_match(self, home, monkeypatch, capsys):
        calls = self._prepared(home, monkeypatch)
        self._write_token({"account": ACCOUNT, "region": "us-west-2", "bucket": "approved-bucket"})
        assert backup_cli.setup_main(self._args(bucket="some-other-bucket")) == 1
        assert calls == []
        assert "bucket" in capsys.readouterr().out

    def test_an_unparseable_token_is_refused(self, home, monkeypatch, capsys):
        calls = self._prepared(home, monkeypatch)
        self._write_token("not json at all")
        assert backup_cli.setup_main(self._args()) == 1
        assert calls == []
        assert "could not be read as JSON" in capsys.readouterr().out

    def test_the_refusal_shows_what_to_write(self, home, monkeypatch, capsys):
        """An operator who has never seen this needs the exact content, and needs to be
        told to check it rather than paste it blindly."""
        self._prepared(home, monkeypatch)
        backup_cli.setup_main(self._args())
        out = capsys.readouterr().out
        assert ACCOUNT in out and "us-west-2" in out
        assert "check they are" in out


class TestAnUnreadableEncryptionConfigDoesNotDowngrade:
    def test_a_denied_read_refuses_instead_of_assuming_no_kms(self, home, monkeypatch):
        """Treating "cannot read" as "no encryption" is how a missing permission becomes
        a silent downgrade: the preservation step concludes there is no customer-managed
        key, and hardening then replaces SSE-KMS with AES256."""
        fake = _fake({"s3api head-bucket": (0, "", "")})
        fake.answers["s3api get-bucket-encryption"] = (255, "", "AccessDenied")
        monkeypatch.setattr(engine, "run_aws", fake)
        with pytest.raises(remote.DestinationError) as e:
            _authorize()
            remote.setup_destination("p", "us-west-2")
        assert "silently downgrade" in str(e.value)
        assert "s3:GetBucketEncryption" in str(e.value)

    def test_a_confirmed_absent_config_is_fine(self, home, monkeypatch):
        fake = _fake({"s3api head-bucket": (0, "", "")})
        fake.answers["s3api get-bucket-encryption"] = (
            255,
            "",
            "An error occurred (ServerSideEncryptionConfigurationNotFoundError)",
        )
        monkeypatch.setattr(engine, "run_aws", fake)
        # Reaches the verification read-back, which is what should fail here rather than
        # the preservation probe.
        with pytest.raises(remote.DestinationError) as e:
            _authorize()
            remote.setup_destination("p", "us-west-2")
        assert "does not report itself private" in str(e.value)

    def test_malformed_json_refuses(self, home, monkeypatch):
        fake = _fake({"s3api head-bucket": (0, "", "")})
        fake.answers["s3api get-bucket-encryption"] = (0, "{not json", "")
        monkeypatch.setattr(engine, "run_aws", fake)
        with pytest.raises(remote.DestinationError) as e:
            _authorize()
            remote.setup_destination("p", "us-west-2")
        assert "could not be parsed" in str(e.value)


class TestNewlinesCannotForgeListingLines:
    def test_a_newline_in_a_key_is_escaped(self):
        """The first version of the sanitizer excluded tab and newline as "harmless
        whitespace". A newline lets a key print FORGED lines, so the operator sees
        backup entries that do not exist."""
        out = remote.safe_for_terminal("backups/h/a.tar.gz\nbackups/h/fake.tar.gz")
        assert "\n" not in out
        assert "\\x0a" in out

    def test_a_tab_is_escaped(self):
        out = remote.safe_for_terminal("backups/h/a\tb.tar.gz")
        assert "\t" not in out
        assert "\\x09" in out

    def test_the_listing_cannot_be_forged(self, home, monkeypatch, capsys):
        dest = remote.Destination(
            bucket="my-backups", region="us-west-2", account=ACCOUNT, created_at="now"
        )
        remote._save_destination(dest)
        monkeypatch.setattr(backup_cli, "_resolve_aws_profile", lambda _n: ("p", "us-west-2"))
        monkeypatch.setattr(
            remote,
            "list_backups",
            lambda *_a, **_k: {"h": ["backups/h/real.tar.gz\n    s3://x/forged.tar.gz"]},
        )
        backup_cli.list_main(argparse.Namespace(aws_profile=None, region=None))
        out = capsys.readouterr().out
        assert "forged" in out, "the value should still be visible, just inert"
        # One printed line per real key: the newline must not have created a second.
        key_lines = [ln for ln in out.splitlines() if "tar.gz" in ln and "s3://" in ln]
        assert len(key_lines) == 1, key_lines


class TestReplaceSavesEveryTreeBeforeReplacingAny:
    def test_the_rollback_copy_is_taken_before_any_database_is_swapped(self):
        """Backing up and replacing tree-by-tree meant a failure partway through left
        some trees replaced and no rollback copy of the rest — with the databases already
        swapped. Half old, half new, and no complete copy of either.

        The first revision of this fix split the passes but left BOTH of them after the
        database swap, and said so in its own comment. The rollback set has to be
        complete before the first mutation, so the save now precedes the component loop
        that calls `_backup_and_copy`.
        """
        import inspect

        setup = inspect.getsource(snap._do_replace)
        # The mutations moved into their own function so ONE handler can recover the
        # whole rollback set; the ordering claim spans both, so both are read.
        muts = inspect.getsource(snap._do_replace_mutations)
        save_at = setup.index("_copytree_rollback(d, backup / tree")
        guard_at = setup.index("_do_replace_mutations(")
        assert (
            save_at < guard_at
        ), "the memory trees are saved after the mutation phase has already started"
        swap_at = muts.index("_backup_and_copy(mc, backup, snap, comp, installed)")
        replace_at = muts.index("_replace_tree_root(")
        assert swap_at < replace_at, "the replace pass moved ahead of the database swap"
        # And the recovery is wired to the phase, not to one tree.
        assert "_restore_everything_from_rollback(backup, mc, targets, installed)" in setup

    def test_a_failed_tree_replace_still_leaves_a_complete_rollback_copy(
        self, tmp_path, monkeypatch
    ):
        """The behavioural half: if the replace pass dies, the ORIGINAL tree must be
        recoverable even though the databases have already moved."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        _setup_fake_kirocrew(home)
        md = home / "workspace" / "memory"
        md.mkdir(parents=True, exist_ok=True)
        (md / "preferences.md").write_text("original")
        out = tmp_path / "out"
        assert snap.snapshot_main([str(out), "--components", "memory"]) == 0
        bundle = next(out.glob("kirocrew-snapshot-*.tar.gz"))
        (md / "preferences.md").write_text("changed since the backup")

        real = snap._clear_tree_root

        def boom(d):
            raise OSError("disk full partway through the replace")

        monkeypatch.setattr(snap, "_clear_tree_root", boom)
        try:
            snap.restore_main([str(bundle), "--mode", "replace", "--force"])
        except OSError:
            pass
        finally:
            monkeypatch.setattr(snap, "_clear_tree_root", real)

        saved = list(home.glob("pre-restore-*/workspace/memory/preferences.md"))
        assert saved, "no rollback copy of the tree was taken before the swap"
        assert (
            saved[0].read_text() == "changed since the backup"
        ), "the rollback copy does not hold the state that was live before the restore"

    def test_a_normal_replace_still_works(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        _setup_fake_kirocrew(home)
        md = home / "workspace" / "memory"
        md.mkdir(parents=True, exist_ok=True)
        (md / "preferences.md").write_text("original")
        out = tmp_path / "out"
        assert snap.snapshot_main([str(out), "--components", "memory"]) == 0
        bundle = next(out.glob("kirocrew-snapshot-*.tar.gz"))

        (md / "preferences.md").write_text("changed since the backup")
        rc = snap.restore_main([str(bundle), "--mode", "replace", "--force"])
        assert rc == 0
        assert (md / "preferences.md").read_text() == "original"
