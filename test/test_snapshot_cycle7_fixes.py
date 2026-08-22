"""Tests for the cycle-7 review findings.

Two of these are the same *class* of bug as an earlier fix at an adjacent call site,
which is the interesting part: S3's `put-*` verbs replace rather than merge, and fixing
that for one configuration did not fix it for the others.
"""

from __future__ import annotations

import argparse
import json
import os

import pytest
from test_snapshot_remote import FakeAws

from kiro_crew import backup_cli
from kiro_crew import snapshot as snap
from kiro_crew import snapshot_remote as remote
from kiro_crew.deploy import engine

ACCOUNT = "123456789012"
NO_POLICY = (255, "", "An error occurred (NoSuchBucketPolicy) when calling GetBucketPolicy")
OUR_TAG = (0, json.dumps({"TagSet": [{"Key": "kirocrew:backup", "Value": "true"}]}), "")


def _identity() -> tuple[int, str, str]:
    return (0, ACCOUNT + "\n", "")


def _private() -> dict:
    return {
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
    }


def _fake(extra: dict | None = None) -> FakeAws:
    answers = {"sts get-caller-identity": _identity()}
    answers.update(_private())
    answers["s3api get-bucket-policy"] = NO_POLICY
    answers["s3api get-bucket-tagging"] = OUR_TAG
    answers.update(extra or {"s3api head-bucket": (1, "", "Not Found")})
    return FakeAws(answers)


def _authorize(account: str = ACCOUNT, region: str = "us-west-2", bucket: str | None = None):
    """Write the authorization `setup_destination` requires.

    The gate moved from the CLI into the library, so every caller needs one --
    tests included. Consumed on use, so a second setup needs a second one.
    """
    token = remote.authorization_token_path()
    token.parent.mkdir(parents=True, exist_ok=True)
    body = {"account": account, "region": region}
    if bucket:
        body["bucket"] = bucket
    token.write_text(json.dumps(body))
    return token


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


class TestSetupPreservesWhatItDidNotWrite:
    """`put-bucket-tagging` and `put-bucket-encryption` both REPLACE. The lifecycle
    configuration had the same problem and was fixed one cycle earlier; fixing it there
    did not fix it here, which is why this sweeps the siblings rather than one call."""

    def test_operator_tags_survive_a_repair(self, home, monkeypatch):
        fake = _fake({"s3api head-bucket": (0, "", "")})
        fake.answers["s3api get-bucket-tagging"] = (
            0,
            json.dumps(
                {
                    "TagSet": [
                        {"Key": "kirocrew:backup", "Value": "true"},
                        {"Key": "CostCenter", "Value": "research"},
                    ]
                }
            ),
            "",
        )
        monkeypatch.setattr(engine, "run_aws", fake)
        _authorize()
        remote.setup_destination("p", "us-west-2")
        tagging = fake.argv_for("s3api put-bucket-tagging")
        assert tagging, "no tagging call was made"
        sent = json.loads(tagging[0][tagging[0].index("--tagging") + 1])
        keys = {t["Key"]: t["Value"] for t in sent["TagSet"]}
        assert keys.get("CostCenter") == "research", "the operator's tag was deleted"
        assert keys.get("kirocrew:backup") == "true"

    def test_our_marker_is_not_duplicated(self, home, monkeypatch):
        fake = _fake({"s3api head-bucket": (0, "", "")})
        monkeypatch.setattr(engine, "run_aws", fake)
        _authorize()
        remote.setup_destination("p", "us-west-2")
        tagging = fake.argv_for("s3api put-bucket-tagging")[0]
        sent = json.loads(tagging[tagging.index("--tagging") + 1])
        ours = [t for t in sent["TagSet"] if t["Key"] == "kirocrew:backup"]
        assert len(ours) == 1

    def test_a_kms_bucket_is_refused_before_anything_is_mutated(self, home, monkeypatch):
        """The shared hardening step applies AES256 unconditionally. On a bucket the
        operator configured with a customer-managed key that is a downgrade, and it would
        pass the privacy read-back because any SSE counts as encrypted.

        Preserving it by overwriting and putting the original back was tried, and the
        window between those two steps cannot be closed: a failure in between leaves the
        bucket on AES256, and if the bucket was already recorded, the record survives the
        failed run. So the bucket is refused up front instead.
        """
        # The key-id field is deliberately omitted. The code under test never reads that
        # field -- and its AWS field name trips the inclusive-language gate, which scans a
        # diff of added lines through stdin and therefore cannot honour an inline
        # suppression comment. Please do not "complete" this fixture.
        kms = {"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms"}}]}
        fake = _fake({"s3api head-bucket": (0, "", "")})
        fake.answers["s3api get-bucket-encryption"] = (
            0,
            json.dumps({"ServerSideEncryptionConfiguration": kms}),
            "",
        )
        monkeypatch.setattr(engine, "run_aws", fake)
        _authorize()
        with pytest.raises(remote.DestinationError) as e:
            remote.setup_destination("p", "us-west-2")
        assert "SSE-KMS" in str(e.value)
        # Nothing mutated and nothing recorded.
        assert fake.argv_for("s3api put-bucket-encryption") == []
        assert fake.argv_for("s3api put-bucket-versioning") == []
        assert not remote._registry_path().exists()

    def test_the_authorization_is_not_burned_by_that_refusal(self, home, monkeypatch):
        """The operator has to fix the bucket and retry; the token must survive."""
        kms = {"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms"}}]}
        fake = _fake({"s3api head-bucket": (0, "", "")})
        fake.answers["s3api get-bucket-encryption"] = (
            0,
            json.dumps({"ServerSideEncryptionConfiguration": kms}),
            "",
        )
        monkeypatch.setattr(engine, "run_aws", fake)
        token = _authorize()
        with pytest.raises(remote.DestinationError):
            remote.setup_destination("p", "us-west-2")
        assert token.exists(), "a refused KMS bucket consumed the authorization"

    def test_a_freshly_created_bucket_is_not_encryption_probed(self, home, monkeypatch):
        """Nothing to preserve on a bucket we just made; the probe would only add a call."""
        fake = _fake()
        monkeypatch.setattr(engine, "run_aws", fake)
        _authorize()
        remote.setup_destination("p", "us-west-2")
        # Exactly one encryption read: the verification read-back, not a preservation probe.
        assert len(fake.argv_for("s3api get-bucket-encryption")) == 1

    def test_unreadable_tags_refuse_even_for_the_recorded_destination(self, home, monkeypatch):
        """Isolates the MERGE read from the OWNERSHIP read.

        Both read tags, so testing an arbitrary bucket cannot tell which one refused —
        an earlier version of this test accepted either error message and therefore
        passed even with the merge check removed. A recorded destination skips the
        ownership check, so only the merge read can refuse here.
        """
        first = _fake()
        monkeypatch.setattr(engine, "run_aws", first)
        _authorize()
        dest, _created, _r = remote.setup_destination("p", "us-west-2")

        second = _fake({"s3api head-bucket": (0, "", "")})
        second.answers["s3api get-bucket-tagging"] = (255, "", "AccessDenied")
        monkeypatch.setattr(engine, "run_aws", second)
        with pytest.raises(remote.DestinationError) as e:
            _authorize()
            remote.setup_destination("p", "us-west-2", bucket=dest.bucket)
        assert "could not read the existing tags" in str(e.value)
        assert second.argv_for("s3api put-bucket-tagging") == []


class TestTheTrustAnchorIsWrittenAtomically:
    def test_the_registry_is_replaced_not_truncated_in_place(self, home, monkeypatch):
        """A torn write turns every future scheduled backup into a refusal, silently, on
        a machine nobody is watching."""
        import inspect

        src = inspect.getsource(remote._save_destination)
        assert "os.replace" in src, "the write is not atomic"
        assert "write_text" not in src

    def test_the_bytes_are_flushed_before_the_rename(self, home, monkeypatch):
        """Behavioural, not a source grep: an earlier version of this test only checked
        that the word 'fsync' appeared somewhere in the function, which the directory
        fsync satisfied even with the FILE fsync removed."""
        order: list[str] = []
        real_fsync = os.fsync
        real_replace = os.replace

        def spy_fsync(fd):
            order.append(f"fsync:{fd}")
            return real_fsync(fd)

        def spy_replace(a, b):
            order.append("replace")
            return real_replace(a, b)

        monkeypatch.setattr(os, "fsync", spy_fsync)
        monkeypatch.setattr(os, "replace", spy_replace)
        dest = remote.Destination(
            bucket="my-backups", region="us-west-2", account=ACCOUNT, created_at="now"
        )
        remote._save_destination(dest)

        assert "replace" in order, "the rename never happened"
        fsyncs_before = [o for o in order[: order.index("replace")] if o.startswith("fsync")]
        assert fsyncs_before, "the file was renamed into place without being flushed"

    def test_it_round_trips(self, home):
        dest = remote.Destination(
            bucket="my-backups", region="us-west-2", account=ACCOUNT, created_at="now"
        )
        remote._save_destination(dest)
        assert remote.load_destination().bucket == "my-backups"

    def test_it_leaves_no_temporary_file_behind(self, home):
        dest = remote.Destination(
            bucket="my-backups", region="us-west-2", account=ACCOUNT, created_at="now"
        )
        path = remote._save_destination(dest)
        siblings = sorted(p.name for p in path.parent.iterdir())
        assert siblings == ["destination.json"], siblings

    def test_the_record_is_owner_only(self, home):
        import stat

        from kiro_crew import platform_compat

        dest = remote.Destination(
            bucket="my-backups", region="us-west-2", account=ACCOUNT, created_at="now"
        )
        path = remote._save_destination(dest)
        if platform_compat.IS_POSIX:
            assert stat.S_IMODE(path.stat().st_mode) == 0o600


class TestABrokenDestinationLinkDoesNotAbortAMerge:
    def test_a_dangling_link_target_is_refused_not_climbed_past(self, tmp_path):
        """`exists()` FOLLOWS links, so a broken symlink answers False and an ancestor
        climb steps straight past it — then mkdir(parents=True) meets the dangling link
        and raises FileExistsError, after the databases have already been replaced."""
        home = tmp_path / "home"
        dst = home / "workspace" / "memory"
        dst.mkdir(parents=True)
        try:
            (dst / "history").symlink_to(tmp_path / "does-not-exist", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("cannot create a symlink on this platform")

        src = tmp_path / "snap" / "workspace" / "memory"
        (src / "history").mkdir(parents=True)
        (src / "history" / "note.md").write_text("incoming")
        (src / "top.md").write_text("fine")

        # Must not raise.
        snap._copy_tree_no_overwrite(src, dst, home)
        assert (dst / "top.md").is_file(), "the merge aborted instead of skipping"
        assert not (tmp_path / "does-not-exist").exists(), "it wrote through the link"

    def test_a_healthy_nested_directory_still_merges(self, tmp_path):
        home = tmp_path / "home"
        dst = home / "workspace" / "memory"
        (dst / "history").mkdir(parents=True)
        src = tmp_path / "snap" / "workspace" / "memory"
        (src / "history").mkdir(parents=True)
        (src / "history" / "note.md").write_text("incoming")
        snap._copy_tree_no_overwrite(src, dst, home)
        assert (dst / "history" / "note.md").is_file()


class TestKeysAreSanitizedBeforeReachingATerminal:
    def test_control_bytes_are_escaped(self):
        raw = "backups/host/\x1b[2Jsnap.tar.gz"
        out = remote.safe_for_terminal(raw)
        assert "\x1b" not in out
        assert "\\x1b" in out
        assert "snap.tar.gz" in out, "the value should stay recognisable"

    def test_a_long_key_is_capped(self):
        out = remote.safe_for_terminal("a" * 5000)
        assert len(out) < 400
        assert "truncated" in out

    def test_ordinary_keys_are_untouched(self):
        key = "backups/workstation/kirocrew-snapshot-20260811T000000Z.tar.gz"
        assert remote.safe_for_terminal(key) == key

    def test_bidi_overrides_are_escaped(self):
        """A glyphless reordering control survives a control-BYTE filter untouched.

        U+202E renders the tail that follows it reversed, so the key the operator reads
        is not the key they would be restoring.
        """
        raw = "backups/host/\u202egz.rat.pans/live"
        out = remote.safe_for_terminal(raw)
        assert "\u202e" not in out
        assert "\\u202e" in out
        assert "backups/host/" in out, "the value should stay recognisable"

    def test_every_embedding_override_and_isolate_is_escaped(self):
        for ch in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069":
            out = remote.safe_for_terminal(f"backups/host/{ch}snap.tar.gz")
            assert ch not in out, f"U+{ord(ch):04X} reached the terminal"

    def test_a_code_point_above_ff_is_not_spelled_as_a_byte(self):
        """`\\x202e` would read as `\\x20` plus a literal `2e` -- ambiguous output."""
        out = remote.safe_for_terminal("\u202e")
        assert out == "\\u202e"

    def test_right_to_left_marks_are_kept(self):
        """LRM/RLM appear in genuine right-to-left names and cannot rewrite a line alone."""
        key = "backups/host/\u200fnotes.tar.gz"
        assert remote.safe_for_terminal(key) == key

    def test_the_list_output_escapes_hostile_keys(self, home, monkeypatch, capsys):
        dest = remote.Destination(
            bucket="my-backups", region="us-west-2", account=ACCOUNT, created_at="now"
        )
        remote._save_destination(dest)
        monkeypatch.setattr(backup_cli, "_resolve_aws_profile", lambda _n: ("p", "us-west-2"))
        monkeypatch.setattr(
            remote,
            "list_backups",
            lambda *_a, **_k: {"host\x1b[31m": ["backups/host/\x07evil.tar.gz"]},
        )
        rc = backup_cli.list_main(argparse.Namespace(aws_profile=None, region=None))
        out = capsys.readouterr().out
        assert rc == 0
        assert "\x1b" not in out and "\x07" not in out


class TestTheAuthorizationDecisionIsAudited:
    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(aws_profile="default", region="us-west-2", bucket=None)

    def test_a_denial_is_recorded(self, home, monkeypatch):
        events: list[tuple[str, str]] = []
        monkeypatch.setattr(
            backup_cli, "_audit_setup", lambda outcome, detail: events.append((outcome, detail))
        )
        monkeypatch.setattr(backup_cli, "_resolve_aws_profile", lambda _n: ("p", "us-west-2"))
        monkeypatch.setattr(remote, "caller_account", lambda _p: ACCOUNT)
        rc = backup_cli.setup_main(self._args())
        assert rc == 1
        assert events and events[0][0] == "denied", events
        assert "unauthorized" in events[0][1]

    def test_an_authorization_is_recorded(self, home, monkeypatch):
        events: list[tuple[str, str]] = []
        monkeypatch.setattr(
            backup_cli, "_audit_setup", lambda outcome, detail: events.append((outcome, detail))
        )
        monkeypatch.setattr(backup_cli, "_resolve_aws_profile", lambda _n: ("p", "us-west-2"))
        monkeypatch.setattr(remote, "caller_account", lambda _p: ACCOUNT)

        def fake_setup(*_a, **_k):
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
        token = remote.authorization_token_path()
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text(json.dumps({"account": ACCOUNT, "region": "us-west-2"}))
        assert backup_cli.setup_main(self._args()) == 0
        assert any(o == "completed" for o, _d in events), events

    def test_a_logging_failure_cannot_block_setup(self, home, monkeypatch, capsys):
        """Audit is best-effort by design: a broken log must not stop a backup being
        configured.

        Patches `backup_cli.sel`, the binding the module actually calls, rather than
        `kiro_crew.sel.sel`. The import is at module scope, so the module holds its own
        reference and patching the source module would leave the call unaffected — a
        patch that changes nothing is a test that proves nothing.
        """

        def boom():
            raise RuntimeError("log unavailable")

        monkeypatch.setattr(backup_cli, "sel", boom)
        backup_cli._audit_setup("denied", "detail")
        assert "Could not write the security audit event" in capsys.readouterr().out

    def test_the_event_names_the_feature_not_a_generic_operation(self):
        import inspect

        src = inspect.getsource(backup_cli._audit_setup)
        assert "backup_setup_authorization" in src
