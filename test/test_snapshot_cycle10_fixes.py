"""Tests for the cycle-10 review findings.

Both are about a partial failure leaving worse state than no attempt: hardening that
fails halfway had already discarded the operator's encryption key, and the upload retry
did not cover the one failure it was written for.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from test_snapshot_remote import FakeAws

from kiro_crew import snapshot_remote as remote
from kiro_crew.deploy import engine

ACCOUNT = "123456789012"
NO_POLICY = (255, "", "An error occurred (NoSuchBucketPolicy) when calling GetBucketPolicy")

# The upload re-asserts every protection setup established, not just the absence of a
# policy, so a stub that only answers about the policy reads as a weakened bucket.
INTACT_CONTROLS: dict[str, tuple[int, str, str]] = {
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
                    "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
                }
            }
        ),
        "",
    ),
    "s3api get-bucket-versioning": (0, json.dumps({"Status": "Enabled"}), ""),
    "s3api get-bucket-ownership-controls": (
        0,
        json.dumps({"OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}}),
        "",
    ),
}
OUR_TAG = (0, json.dumps({"TagSet": [{"Key": "kirocrew:backup", "Value": "true"}]}), "")
KMS = {"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms"}}]}


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
            json.dumps({"ServerSideEncryptionConfiguration": KMS}),
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
    answers.update(extra or {"s3api head-bucket": (0, "", "")})
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
    _authorize()
    return tmp_path


class TestAKmsBucketIsRefusedRatherThanRestored:
    """Hardening writes AES256 first, so preserving an operator's KMS default meant
    overwriting it and putting it back. Any failure between those two steps leaves the
    bucket downgraded -- and if the bucket was ALREADY recorded by an earlier setup, the
    record survives the failed run, so later uploads land somewhere readable without the
    key the operator chose. The window is the design, so the bucket is refused instead.
    """

    def test_a_kms_bucket_never_reaches_hardening(self, home, monkeypatch):
        fake = _fake()
        fake.answers["s3api head-bucket"] = (0, "", "")
        fake.answers["s3api get-bucket-encryption"] = (
            0,
            json.dumps({"ServerSideEncryptionConfiguration": KMS}),
            "",
        )
        hardened: list[str] = []

        monkeypatch.setattr(engine, "run_aws", fake)
        monkeypatch.setattr(engine, "harden_bucket", lambda *a, **k: hardened.append("ran"))
        with pytest.raises(remote.DestinationError) as e:
            remote.setup_destination("p", "us-west-2")
        assert "SSE-KMS" in str(e.value)
        assert hardened == [], "hardening ran on a bucket we cannot safely harden"

    def test_the_refusal_names_the_way_out(self, home, monkeypatch):
        """A refusal the operator cannot act on is a dead end, not a guard."""
        fake = _fake()
        fake.answers["s3api head-bucket"] = (0, "", "")
        fake.answers["s3api get-bucket-encryption"] = (
            0,
            json.dumps({"ServerSideEncryptionConfiguration": KMS}),
            "",
        )
        monkeypatch.setattr(engine, "run_aws", fake)
        with pytest.raises(remote.DestinationError) as e:
            remote.setup_destination("p", "us-west-2")
        msg = str(e.value)
        assert "--bucket" in msg
        assert "Nothing was changed and nothing was recorded" in msg

    def test_a_non_kms_bucket_is_still_hardened(self, home, monkeypatch):
        """The refusal must be narrow: AES256 and unset both stay supported."""
        fake = _fake()
        fake.answers["s3api head-bucket"] = (0, "", "")
        fake.answers["s3api get-bucket-encryption"] = (
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
        )
        hardened: list[str] = []
        monkeypatch.setattr(engine, "run_aws", fake)
        monkeypatch.setattr(engine, "harden_bucket", lambda *a, **k: hardened.append("ran"))
        remote.setup_destination("p", "us-west-2")
        assert hardened == ["ran"]


class TestATimeoutIsRetried:
    """The retry was written for transient network failures, and for a large upload a
    timeout IS the transient network failure — but `run_aws` RAISES on timeout instead of
    returning non-zero, so it escaped the loop that was meant to absorb it."""

    def _dest(self) -> remote.Destination:
        return remote.Destination(
            bucket="my-backups", region="us-west-2", account=ACCOUNT, created_at="now"
        )

    def test_a_timeout_is_retried_not_propagated(self, tmp_path, monkeypatch):
        bundle = tmp_path / "b.tar.gz"
        bundle.write_bytes(b"x")
        puts = {"n": 0}

        def flaky(args, profile, timeout=30):
            verb = " ".join(args[:2])
            if verb == "s3api get-bucket-policy":
                return NO_POLICY
            if " ".join(args[:2]) in INTACT_CONTROLS:
                return INTACT_CONTROLS[" ".join(args[:2])]
            puts["n"] += 1
            if puts["n"] < 3:
                raise subprocess.TimeoutExpired(cmd="aws", timeout=timeout)
            return 0, "", ""

        monkeypatch.setattr(engine, "run_aws", flaky)
        url = remote.upload(bundle, self._dest(), "p", sleep=lambda _s: None)
        # The subject here is the retry, not the key. Object names carry a uniqueness
        # suffix so two same-second backups cannot collapse into one object.
        assert url.endswith(".tar.gz")
        assert "/b-" in url
        assert puts["n"] == 3, "a timeout was not retried"

    def test_a_persistent_timeout_ends_as_a_clean_refusal(self, tmp_path, monkeypatch):
        bundle = tmp_path / "b.tar.gz"
        bundle.write_bytes(b"x")

        def always_slow(args, profile, timeout=30):
            if " ".join(args[:2]) == "s3api get-bucket-policy":
                return NO_POLICY
            if " ".join(args[:2]) in INTACT_CONTROLS:
                return INTACT_CONTROLS[" ".join(args[:2])]
            raise subprocess.TimeoutExpired(cmd="aws", timeout=timeout)

        monkeypatch.setattr(engine, "run_aws", always_slow)
        with pytest.raises(remote.DestinationError) as e:
            remote.upload(bundle, self._dest(), "p", sleep=lambda _s: None)
        # A clean message naming the cause, not a TimeoutExpired traceback.
        assert "attempts" in str(e.value)
        assert "budget" in str(e.value)

    def test_a_permissions_failure_still_short_circuits(self, tmp_path, monkeypatch):
        """Adding timeout handling must not make an authorization failure retryable."""
        bundle = tmp_path / "b.tar.gz"
        bundle.write_bytes(b"x")
        puts = {"n": 0}

        def denied(args, profile, timeout=30):
            if " ".join(args[:2]) == "s3api get-bucket-policy":
                return NO_POLICY
            if " ".join(args[:2]) in INTACT_CONTROLS:
                return INTACT_CONTROLS[" ".join(args[:2])]
            puts["n"] += 1
            return 255, "", "An error occurred (AccessDenied) when calling PutObject"

        monkeypatch.setattr(engine, "run_aws", denied)
        with pytest.raises(remote.DestinationError):
            remote.upload(bundle, self._dest(), "p", sleep=lambda _s: None)
        assert puts["n"] == 1
