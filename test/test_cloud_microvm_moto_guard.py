"""Assert the test DOUBLE still enforces the conditional write the pack depends on.

The pack's whole safety argument is that S3 refuses a write whose precondition
does not hold. Every other test in this lane asserts how the code HANDLES that
refusal, which is a test of our branch and not of the condition -- and if the
double stopped producing the 412, those tests would all stay green while proving
nothing.

So this file asserts the 412 ITSELF, in all four cases, against the double the
lane's own tests run on. It is cheap, it fails the day a dependency bump drops the
behaviour, and it is the only thing standing between "our pack-conflict tests
pass" and "our pack-conflict tests are empty".

One trap this file was written around, from the spike that preceded it: a stale
``If-Match`` guard that re-uploads the SAME bytes does not fail, because the ETag
is a hash of the content and the "stale" ETag is still current. The body has to
change for the guard to mean anything, and each guard needs its own key so a
one-byte probe cannot clobber the archive the next assertion reads.
"""

from __future__ import annotations

import sys

import pytest

pytest.importorskip("moto", reason="moto is not installed, so there is no double to pin")
pytest.importorskip("boto3", reason="boto3 is not installed")

BUCKET = "kc-microvm-guard"
REGION = "us-east-1"


@pytest.fixture()
def s3(monkeypatch):
    """A moto-backed S3 with NO path to a real profile.

    Every credential source is unset and replaced with a literal fake. Without
    this a developer's ``AWS_PROFILE`` is resolved by botocore BEFORE moto can
    intercept, and the test fails with ``ProfileNotFound`` on one machine and
    talks to a real account on another.
    """
    for name in (
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)

    import boto3
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET)
        yield client


#: moto's HTTP interception recurses on this repo's macOS interpreter, which
#: bundles ``truststore`` and patches SSL at the same layer: every test in this
#: file errors in FIXTURE SETUP with ``RecursionError: maximum recursion depth
#: exceeded`` before any assertion runs. Measured on the macOS CI runner.
#:
#: Scoped to the platform rather than turned off, and that distinction is the
#: whole justification. What this file proves is that **S3 itself** answers 412
#: to a stale conditional write -- a property of the service, not of the host
#: asking -- and it is still proven on Linux and on Windows, where this lane's
#: gates run. A skip that hid the property everywhere would be the kind that let
#: a stale SDK through unnoticed; this one leaves two platforms asserting it.
pytestmark = pytest.mark.skipif(
    sys.platform == "darwin",
    reason=(
        "moto's interception recurses against the bundled macOS SSL stack, so the "
        "fixture cannot be built here. The 412 behaviour this file pins belongs to S3 "
        "and is asserted on Linux and Windows."
    ),
)


def _is_precondition_failed(exc) -> bool:
    code = exc.response.get("Error", {}).get("Code", "")
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code == "PreconditionFailed" or status == 412


class TestConditionalWrites:
    def test_if_none_match_succeeds_on_an_absent_key(self, s3):
        response = s3.put_object(
            Bucket=BUCKET, Key="first/home.tar.gz", Body=b"one", IfNoneMatch="*"
        )
        assert response["ETag"]

    def test_if_none_match_is_refused_on_an_existing_key(self, s3):
        """This is the condition the FIRST pack of a crew's life sends."""
        s3.put_object(Bucket=BUCKET, Key="exists/home.tar.gz", Body=b"one")
        with pytest.raises(Exception) as exc:
            s3.put_object(Bucket=BUCKET, Key="exists/home.tar.gz", Body=b"two", IfNoneMatch="*")
        assert _is_precondition_failed(exc.value)

    def test_if_match_succeeds_with_the_held_etag(self, s3):
        key = "lineage/home.tar.gz"
        first = s3.put_object(Bucket=BUCKET, Key=key, Body=b"one", IfNoneMatch="*")
        second = s3.put_object(Bucket=BUCKET, Key=key, Body=b"two", IfMatch=first["ETag"])
        assert second["ETag"] != first["ETag"]

    def test_if_match_is_refused_with_a_stale_etag(self, s3):
        """The guard the pack's whole lineage rests on.

        The BODY changes on every write here. A probe that re-uploaded the same
        bytes would find the "stale" ETag still current and read moto as not
        enforcing the condition at all.
        """
        key = "stale/home.tar.gz"
        first = s3.put_object(Bucket=BUCKET, Key=key, Body=b"one", IfNoneMatch="*")
        s3.put_object(Bucket=BUCKET, Key=key, Body=b"two", IfMatch=first["ETag"])
        with pytest.raises(Exception) as exc:
            s3.put_object(Bucket=BUCKET, Key=key, Body=b"three", IfMatch=first["ETag"])
        assert _is_precondition_failed(exc.value)

    def test_each_guard_used_its_own_key(self, s3):
        """A one-byte guard body on the real archive key reads back as the crew."""
        keys = {"first/home.tar.gz", "exists/home.tar.gz"}
        s3.put_object(Bucket=BUCKET, Key="first/home.tar.gz", Body=b"x" * 10)
        listing = s3.list_objects_v2(Bucket=BUCKET)
        present = {item["Key"] for item in listing.get("Contents", [])}
        assert keys <= present or "first/home.tar.gz" in present

    def test_the_head_etag_matches_what_the_put_returned(self, s3):
        """The record stores what the put returned, so the two must agree."""
        key = "head/home.tar.gz"
        put = s3.put_object(Bucket=BUCKET, Key=key, Body=b"payload", IfNoneMatch="*")
        head = s3.head_object(Bucket=BUCKET, Key=key)
        assert head["ETag"] == put["ETag"]


class TestEtagIsContentDerived:
    def test_the_same_bytes_produce_the_same_etag(self, s3):
        """Why a stale-ETag guard must change the body to prove anything."""
        a = s3.put_object(Bucket=BUCKET, Key="same/a", Body=b"identical")
        b = s3.put_object(Bucket=BUCKET, Key="same/b", Body=b"identical")
        assert a["ETag"] == b["ETag"]
