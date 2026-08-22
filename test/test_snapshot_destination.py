"""Tests for the off-host destination: provisioned once, then written to.

The design under test replaced one where every backup run tried to decide, from inside
the write path, whether an arbitrary bucket was safe. These assert the two properties
that make that unnecessary:

1. A backup writes ONLY to a destination `setup` created and recorded — it cannot be
   pointed at a bucket by a caller, and it refuses when setup has not run.
2. Every object write carries `--expected-bucket-owner`, so S3 refuses the write if the
   bucket is no longer ours rather than this code trying to prove it is.
"""

from __future__ import annotations

import json

import pytest
from test_snapshot import _setup_fake_kirocrew
from test_snapshot_remote import FakeAws

from kiro_crew import snapshot_remote as remote
from kiro_crew.deploy import engine
from kiro_crew.snapshot import snapshot_main

# AWS's reserved documentation account ID -- never a real account.
ACCOUNT = "123456789012"


def _identity(account: str = ACCOUNT) -> tuple[int, str, str]:
    return (0, account + "\n", "")


def _private_report() -> dict:
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
        # Hardening sets this, so verification reads it back: BucketOwnerEnforced is
        # what disables ACLs, and BPA does not cover that.
        "s3api get-bucket-ownership-controls": (
            0,
            json.dumps(
                {"OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}}
            ),
            "",
        ),
    }


@pytest.fixture
def home(tmp_path, monkeypatch):
    d = tmp_path / "home"
    d.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(d))
    _authorize()
    return d


def _authorize(account: str = ACCOUNT, region: str = "us-west-2", bucket: str | None = None):
    """Write the out-of-band authorization `setup_destination` now requires.

    The gate lives in the library rather than the CLI, so every caller needs one --
    including tests. Seeded per invocation because setup CONSUMES it: a second setup
    needs a second authorization, which is the anti-replay property, not an accident.
    """
    token = remote.authorization_token_path()
    token.parent.mkdir(parents=True, exist_ok=True)
    body: dict[str, str] = {"account": account, "region": region}
    if bucket:
        body["bucket"] = bucket
    token.write_text(json.dumps(body))
    return token


NO_POLICY = (255, "", "An error occurred (NoSuchBucketPolicy) when calling GetBucketPolicy")
OUR_TAG = (0, json.dumps({"TagSet": [{"Key": "kirocrew:backup", "Value": "true"}]}), "")
NO_TAGS = (255, "", "An error occurred (NoSuchTagSet) when calling GetBucketTagging")


class TestSetupProvisionsAndRecords:
    def _fake(self, extra: dict | None = None) -> FakeAws:
        answers = {"sts get-caller-identity": _identity()}
        answers.update(_private_report())
        # A bucket this code created has no policy and carries our tag. Scripted by
        # default so the existing-bucket cases exercise the reuse path rather than a
        # refusal.
        answers["s3api get-bucket-policy"] = NO_POLICY
        answers["s3api get-bucket-tagging"] = OUR_TAG
        answers.update(extra or {"s3api head-bucket": (1, "", "Not Found")})
        return FakeAws(answers)

    def test_setup_creates_hardens_versions_and_records(self, home, monkeypatch):
        fake = self._fake()
        monkeypatch.setattr(engine, "run_aws", fake)
        dest, created, report = remote.setup_destination("p", "us-west-2")

        assert created is True
        verbs = [" ".join(c[:2]) for c in fake.calls]
        for expected in (
            "s3api create-bucket",
            "s3api put-public-access-block",
            "s3api put-bucket-ownership-controls",
            "s3api put-bucket-encryption",
            "s3api put-bucket-tagging",
            "s3api put-bucket-versioning",
            "s3api put-bucket-lifecycle-configuration",
        ):
            assert expected in verbs, expected
        assert remote.is_fully_private(report)

        # Recorded, so a later backup does not have to rediscover anything.
        loaded = remote.load_destination()
        assert loaded.bucket == dest.bucket
        assert loaded.account == ACCOUNT
        assert loaded.region == "us-west-2"

    def test_versioning_and_expiry_are_not_optional(self, home, monkeypatch):
        """Versioning makes an overwritten bundle recoverable; the lifecycle rule stops
        that history growing forever. Together they are also what answers retention."""
        fake = self._fake()
        monkeypatch.setattr(engine, "run_aws", fake)
        remote.setup_destination("p", "us-west-2")
        lifecycle = " ".join(fake.argv_for("s3api put-bucket-lifecycle-configuration")[0])
        assert "NoncurrentDays" in lifecycle
        assert str(remote.NONCURRENT_RETENTION_DAYS) in lifecycle
        versioning = " ".join(fake.argv_for("s3api put-bucket-versioning")[0])
        assert "Status=Enabled" in versioning

    def test_setup_is_idempotent_on_an_existing_bucket(self, home, monkeypatch):
        fake = self._fake({"s3api head-bucket": (0, "", "")})
        monkeypatch.setattr(engine, "run_aws", fake)
        _dest, created, _r = remote.setup_destination("p", "us-west-2")
        assert created is False
        # Controls are re-applied rather than assumed: a bucket weakened out of band is
        # repaired by re-running setup.
        verbs = [" ".join(c[:2]) for c in fake.calls]
        assert "s3api create-bucket" not in verbs
        assert "s3api put-public-access-block" in verbs

    def test_ownership_is_asserted_when_probing_for_the_bucket(self, home, monkeypatch):
        """A bucket deleted and re-created by someone else under the same name must read
        as absent, not as ours."""
        fake = self._fake({"s3api head-bucket": (0, "", "")})
        monkeypatch.setattr(engine, "run_aws", fake)
        remote.setup_destination("p", "us-west-2")
        head = " ".join(fake.argv_for("s3api head-bucket")[0])
        assert "--expected-bucket-owner" in head
        assert ACCOUNT in head

    def test_a_bucket_tagged_by_someone_else_is_refused(self, home, monkeypatch):
        """Tag PRESENCE is not the test -- the tag has to be OURS. A bucket carrying an
        unrelated tag set is somebody's real bucket, and setting up would replace its
        lifecycle configuration."""
        fake = self._fake({"s3api head-bucket": (0, "", "")})
        fake.answers["s3api get-bucket-tagging"] = (
            0,
            json.dumps(
                {
                    "TagSet": [
                        {"Key": "Project", "Value": "data-lake"},
                        {"Key": "Owner", "Value": "analytics"},
                    ]
                }
            ),
            "",
        )
        monkeypatch.setattr(engine, "run_aws", fake)
        with pytest.raises(remote.DestinationError) as e:
            remote.setup_destination("p", "us-west-2", bucket="someones-data-lake")
        assert "does not carry the" in str(e.value)
        verbs = [" ".join(c[:2]) for c in fake.calls]
        assert "s3api put-bucket-lifecycle-configuration" not in verbs

    def test_our_tag_with_a_different_value_does_not_count(self, home, monkeypatch):
        fake = self._fake({"s3api head-bucket": (0, "", "")})
        fake.answers["s3api get-bucket-tagging"] = (
            0,
            json.dumps({"TagSet": [{"Key": "kirocrew:backup", "Value": "false"}]}),
            "",
        )
        monkeypatch.setattr(engine, "run_aws", fake)
        with pytest.raises(remote.DestinationError):
            remote.setup_destination("p", "us-west-2", bucket="not-really-ours")

    def test_an_untagged_existing_bucket_is_refused(self, home, monkeypatch):
        """Setup REPLACES the bucket's lifecycle configuration. Adopting an unrelated
        bucket would discard its rules and start expiring its noncurrent objects on our
        schedule -- destroying someone's data in order to set up a backup."""
        fake = self._fake({"s3api head-bucket": (0, "", "")})
        fake.answers["s3api get-bucket-tagging"] = NO_TAGS
        monkeypatch.setattr(engine, "run_aws", fake)
        with pytest.raises(remote.DestinationError) as e:
            remote.setup_destination("p", "us-west-2", bucket="someones-data-lake")
        assert "does not carry the" in str(e.value)
        # Crucially: refused BEFORE any configuration was replaced.
        verbs = [" ".join(c[:2]) for c in fake.calls]
        assert "s3api put-bucket-lifecycle-configuration" not in verbs
        assert "s3api put-bucket-tagging" not in verbs
        assert "s3api put-bucket-versioning" not in verbs
        with pytest.raises(remote.DestinationNotConfigured):
            remote.load_destination()

    def test_unreadable_tags_refuse_rather_than_assume_ours(self, home, monkeypatch):
        fake = self._fake({"s3api head-bucket": (0, "", "")})
        fake.answers["s3api get-bucket-tagging"] = (255, "", "AccessDenied")
        monkeypatch.setattr(engine, "run_aws", fake)
        with pytest.raises(remote.DestinationError):
            remote.setup_destination("p", "us-west-2")

    def test_the_recorded_destination_skips_the_ownership_tag_check(self, home, monkeypatch):
        """Re-running setup on our own recorded bucket must not have to prove ownership
        by tag — it is already recorded, which is stronger evidence.

        Note what this does NOT claim any more. An earlier revision asserted repair
        worked with the tag call entirely unavailable. That stopped being true, for a
        good reason: setup now MERGES its marker into the existing tag set instead of
        replacing it, and merging requires reading. Reading tags in order to preserve
        them is a better trade than a repair path that silently deletes an operator's
        cost-allocation tag, so the ownership check is what got skipped, not the read.
        """
        first = self._fake()
        monkeypatch.setattr(engine, "run_aws", first)
        dest, created, _r = remote.setup_destination("p", "us-west-2")
        assert created is True

        second = self._fake({"s3api head-bucket": (0, "", "")})
        # Tags readable (so the merge can happen) but WITHOUT our marker — the ownership
        # check would refuse this bucket if it were applied to a recorded destination.
        second.answers["s3api get-bucket-tagging"] = (
            0,
            json.dumps({"TagSet": [{"Key": "Project", "Value": "x"}]}),
            "",
        )
        monkeypatch.setattr(engine, "run_aws", second)
        # A second setup needs a second authorization: the first was consumed. That is
        # the anti-replay property, so the test states it rather than working around it.
        _authorize(bucket=dest.bucket)
        _d, created2, _r2 = remote.setup_destination("p", "us-west-2", bucket=dest.bucket)
        assert created2 is False, "the recorded destination was refused on its own tag"

    def test_unreadable_tags_refuse_rather_than_replacing_them(self, home, monkeypatch):
        """Fail closed: applying only our marker would delete tags we cannot see."""
        fake = self._fake({"s3api head-bucket": (0, "", "")})
        fake.answers["s3api get-bucket-tagging"] = (255, "", "AccessDenied")
        monkeypatch.setattr(engine, "run_aws", fake)
        with pytest.raises(remote.DestinationError) as e:
            remote.setup_destination("p", "us-west-2")
        assert "could not read the existing tags" in str(e.value) or "does not carry the" in str(
            e.value
        )
        assert fake.argv_for("s3api put-bucket-tagging") == []

    def test_a_pre_existing_bucket_with_a_policy_is_refused(self, home, monkeypatch):
        """`--bucket` naming an existing bucket was the way the adopt path came back.

        Hardening sets Block Public Access, ownership and encryption -- none of which
        revoke a bucket policy -- so adopting a bucket that already grants read to
        another principal would publish the memory it is about to receive.
        """
        fake = self._fake({"s3api head-bucket": (0, "", "")})
        fake.answers["s3api get-bucket-policy"] = (
            0,
            json.dumps({"Policy": json.dumps({"Statement": [{"Effect": "Allow"}]})}),
            "",
        )
        monkeypatch.setattr(engine, "run_aws", fake)
        with pytest.raises(remote.DestinationError) as e:
            remote.setup_destination("p", "us-west-2", bucket="someone-elses-bucket")
        assert "carries a bucket policy" in str(e.value)
        assert "who is able to read it" in str(e.value)
        # Nothing recorded, and no object written.
        with pytest.raises(remote.DestinationNotConfigured):
            remote.load_destination()

    def test_an_unreadable_policy_is_refused_rather_than_assumed_absent(self, home, monkeypatch):
        """ "We could not tell" and "nobody else can read it" are different answers."""
        fake = self._fake({"s3api head-bucket": (0, "", "")})
        fake.answers["s3api get-bucket-policy"] = (255, "", "AccessDenied")
        monkeypatch.setattr(engine, "run_aws", fake)
        with pytest.raises(remote.DestinationError):
            remote.setup_destination("p", "us-west-2")

    def test_a_freshly_created_bucket_is_not_policy_probed_into_a_refusal(self, home, monkeypatch):
        """The create path must not be blocked by the guard meant for adoption."""
        fake = self._fake()  # head-bucket says absent -> create
        # Deliberately do NOT script get-bucket-policy favourably; a bucket we just
        # created must not be subjected to the adoption check at all.
        fake.answers["s3api get-bucket-policy"] = (0, json.dumps({"Policy": "{}"}), "")
        monkeypatch.setattr(engine, "run_aws", fake)
        dest, created, _r = remote.setup_destination("p", "us-west-2")
        assert created is True
        assert remote.load_destination().bucket == dest.bucket

    def test_setup_preserves_lifecycle_rules_it_did_not_write(self, home, monkeypatch):
        """put-bucket-lifecycle-configuration REPLACES the whole configuration. The user
        doc tells operators they may add their own expiry rule, and tells them re-running
        setup is the repair path — following both must not delete the first."""
        fake = self._fake({"s3api head-bucket": (0, "", "")})
        theirs = {
            "ID": "operator-expire-current",
            "Status": "Enabled",
            "Filter": {"Prefix": "backups/"},
            "Expiration": {"Days": 365},
        }
        fake.answers["s3api get-bucket-lifecycle-configuration"] = (
            0,
            json.dumps({"Rules": [theirs]}),
            "",
        )
        monkeypatch.setattr(engine, "run_aws", fake)
        remote.setup_destination("p", "us-west-2")
        put = fake.argv_for("s3api put-bucket-lifecycle-configuration")[0]
        sent = json.loads(put[put.index("--lifecycle-configuration") + 1])
        ids = [r["ID"] for r in sent["Rules"]]
        assert "operator-expire-current" in ids, "the operator's rule was discarded"
        assert remote.LIFECYCLE_RULE_ID in ids
        kept = next(r for r in sent["Rules"] if r["ID"] == "operator-expire-current")
        assert kept == theirs, "the preserved rule was altered"

    def test_our_own_rule_is_replaced_not_duplicated(self, home, monkeypatch):
        fake = self._fake({"s3api head-bucket": (0, "", "")})
        stale = {
            "ID": remote.LIFECYCLE_RULE_ID,
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "NoncurrentVersionExpiration": {"NoncurrentDays": 9999},
        }
        fake.answers["s3api get-bucket-lifecycle-configuration"] = (
            0,
            json.dumps({"Rules": [stale]}),
            "",
        )
        monkeypatch.setattr(engine, "run_aws", fake)
        remote.setup_destination("p", "us-west-2")
        put = fake.argv_for("s3api put-bucket-lifecycle-configuration")[0]
        sent = json.loads(put[put.index("--lifecycle-configuration") + 1])
        ours = [r for r in sent["Rules"] if r["ID"] == remote.LIFECYCLE_RULE_ID]
        assert len(ours) == 1, "our rule was duplicated"
        assert ours[0]["NoncurrentVersionExpiration"]["NoncurrentDays"] == (
            remote.NONCURRENT_RETENTION_DAYS
        )

    def test_an_unreadable_lifecycle_config_refuses_rather_than_overwriting(
        self, home, monkeypatch
    ):
        fake = self._fake({"s3api head-bucket": (0, "", "")})
        fake.answers["s3api get-bucket-lifecycle-configuration"] = (
            255,
            "",
            "AccessDenied",
        )
        monkeypatch.setattr(engine, "run_aws", fake)
        with pytest.raises(remote.DestinationError) as e:
            remote.setup_destination("p", "us-west-2")
        assert "risk discarding rules" in str(e.value)
        assert fake.argv_for("s3api put-bucket-lifecycle-configuration") == []

    def test_a_fresh_bucket_with_no_lifecycle_config_is_fine(self, home, monkeypatch):
        fake = self._fake()
        fake.answers["s3api get-bucket-lifecycle-configuration"] = (
            255,
            "",
            "An error occurred (NoSuchLifecycleConfiguration) when calling ...",
        )
        monkeypatch.setattr(engine, "run_aws", fake)
        dest, created, _r = remote.setup_destination("p", "us-west-2")
        assert created is True
        assert remote.load_destination().bucket == dest.bucket

    def test_nothing_is_recorded_when_the_bucket_does_not_verify(self, home, monkeypatch):
        answers = {
            "sts get-caller-identity": _identity(),
            "s3api head-bucket": (1, "", "Not Found"),
            **_private_report(),
            # Versioning silently not applied.
            "s3api get-bucket-versioning": (0, json.dumps({}), ""),
        }
        monkeypatch.setattr(engine, "run_aws", FakeAws(answers))
        with pytest.raises(remote.DestinationError) as e:
            remote.setup_destination("p", "us-west-2")
        assert "does not report itself private" in str(e.value)
        with pytest.raises(remote.DestinationNotConfigured):
            remote.load_destination()

    def test_an_unreadable_caller_identity_is_a_refusal(self, home, monkeypatch):
        monkeypatch.setattr(
            engine, "run_aws", FakeAws({"sts get-caller-identity": (255, "", "AccessDenied")})
        )
        with pytest.raises(remote.DestinationError) as e:
            remote.setup_destination("p", "us-west-2")
        assert "could not determine the AWS account" in str(e.value)

    def test_the_default_bucket_name_is_account_scoped(self):
        name = remote.default_bucket_name(ACCOUNT, "us-west-2")
        assert name.startswith(remote.BUCKET_PREFIX)
        assert ACCOUNT in name
        assert len(name) <= 63
        # Stable, so running setup twice finds the same bucket.
        assert name == remote.default_bucket_name(ACCOUNT, "us-west-2")
        # Regional, so setting up in a second region does not collide with the first.
        assert name != remote.default_bucket_name(ACCOUNT, "eu-west-1")

    def test_the_bucket_name_carries_no_operator_identity(self):
        """Bucket names are one global namespace anyone can probe for existence, so an
        identity there advertises whose bucket it is — and buys nothing, because the
        account ID already makes the name unique."""
        import getpass

        name = remote.default_bucket_name(ACCOUNT, "us-west-2")
        user = getpass.getuser().lower()
        if len(user) > 2:  # a 1-2 char username would match by coincidence
            assert user not in name


class TestBackupRefusesWithoutSetup:
    def test_load_destination_refuses_and_names_the_command(self, home):
        with pytest.raises(remote.DestinationNotConfigured) as e:
            remote.load_destination()
        assert "kirocrew backup setup" in str(e.value)

    def test_snapshot_to_s3_refuses_and_keeps_the_local_bundle(
        self, home, tmp_path, monkeypatch, capsys
    ):
        _setup_fake_kirocrew(home)
        out = tmp_path / "out"
        rc = snapshot_main([str(out), "--components", "memory", "--to-s3"])
        assert rc == 1
        assert "kirocrew backup setup" in capsys.readouterr().out
        # The local bundle is still written -- an unconfigured destination must not cost
        # the operator their backup.
        assert list(out.glob("kirocrew-snapshot-*.tar.gz"))

    def test_a_tampered_registry_is_refused_rather_than_used(self, home):
        reg = home / "backup" / "destination.json"
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text(
            json.dumps({"bucket": "Not A Bucket", "region": "us-west-2", "account": ACCOUNT})
        )
        with pytest.raises(remote.DestinationError) as e:
            remote.load_destination()
        assert "invalid" in str(e.value)

    def test_a_registry_naming_a_bad_account_is_refused(self, home):
        reg = home / "backup" / "destination.json"
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text(
            json.dumps({"bucket": "my-backups", "region": "us-west-2", "account": "not-an-account"})
        )
        with pytest.raises(remote.DestinationError):
            remote.load_destination()


class TestUploadIsOwnershipChecked:
    def _dest(self) -> remote.Destination:
        return remote.Destination(
            bucket="my-backups", region="us-west-2", account=ACCOUNT, created_at="now"
        )

    def _fake(self, answers: dict | None = None) -> FakeAws:
        """A fake whose bucket still reports every protection, so uploads reach the write.

        No policy is not sufficient on its own: the upload re-asserts the whole
        predicate setup established, so a bucket that cannot answer for its
        public-access block, encryption, versioning and ownership is refused.
        """
        base = {"s3api get-bucket-policy": NO_POLICY, **_private_report()}
        base.update(answers or {})
        return FakeAws(base)

    def test_every_write_carries_expected_bucket_owner(self, tmp_path, monkeypatch):
        """The one guarantee left in the hot path, and S3 enforces it — not this code."""
        bundle = tmp_path / "kirocrew-snapshot-20260811T000000Z.tar.gz"
        bundle.write_bytes(b"x" * 2048)
        fake = self._fake()
        monkeypatch.setattr(engine, "run_aws", fake)
        url = remote.upload(bundle, self._dest(), "p")
        put = fake.argv_for("s3api put-object")[0]
        assert "--expected-bucket-owner" in put
        assert ACCOUNT in put
        # The object key is NOT the bundle name: a snapshot is named to the second,
        # so the key carries a suffix to stop two same-second backups from becoming
        # one object. What must hold is that the URL still names this archive.
        assert url.endswith(".tar.gz")
        assert bundle.name.removesuffix(".tar.gz") in url

    def test_the_key_is_namespaced_per_host(self, tmp_path, monkeypatch):
        """Several machines can share one bucket without interleaving, and a restore can
        name which machine's backup it wants."""
        bundle = tmp_path / "b.tar.gz"
        bundle.write_bytes(b"x")
        fake = self._fake()
        monkeypatch.setattr(engine, "run_aws", fake)
        remote.upload(bundle, self._dest(), "p")
        put = " ".join(fake.argv_for("s3api put-object")[0])
        assert f"backups/{remote.host_id()}/b-" in put, put
        assert put.rstrip().count("backups/") >= 1

    def test_a_transient_failure_is_retried(self, tmp_path, monkeypatch):
        bundle = tmp_path / "b.tar.gz"
        bundle.write_bytes(b"x")
        puts = {"n": 0}

        controls = _private_report()

        def flaky(args, profile, timeout=30):
            verb = " ".join(args[:2])
            if verb == "s3api get-bucket-policy":
                return NO_POLICY
            if verb in controls:
                return controls[verb]
            # Only the write is counted, so the retry assertion measures retries rather
            # than however many reads the destination checks happen to make.
            puts["n"] += 1
            if puts["n"] < 3:
                return 1, "", "RequestTimeout: connection reset"
            return 0, "", ""

        monkeypatch.setattr(engine, "run_aws", flaky)
        remote.upload(bundle, self._dest(), "p", sleep=lambda _s: None)
        assert puts["n"] == 3, "a transient failure should be retried, not fatal"

    def test_a_permissions_failure_is_not_retried(self, tmp_path, monkeypatch):
        """An authorization failure will not fix itself; burning retries only delays the
        message."""
        bundle = tmp_path / "b.tar.gz"
        bundle.write_bytes(b"x")
        puts = {"n": 0}

        controls = _private_report()

        def denied(args, profile, timeout=30):
            verb = " ".join(args[:2])
            if verb == "s3api get-bucket-policy":
                return NO_POLICY
            if verb in controls:
                return controls[verb]
            puts["n"] += 1
            return 255, "", "An error occurred (AccessDenied) when calling PutObject"

        monkeypatch.setattr(engine, "run_aws", denied)
        with pytest.raises(remote.DestinationError) as e:
            remote.upload(bundle, self._dest(), "p", sleep=lambda _s: None)
        assert puts["n"] == 1
        assert "refused" in str(e.value)

    def test_a_policy_appearing_after_setup_refuses_the_upload(self, tmp_path, monkeypatch):
        """Setup's verification is a point-in-time fact. A bucket policy can be added
        afterwards, and Block Public Access does not stop a grant to a specific named
        account, so exposure is re-checked at the moment the memory is written."""
        bundle = tmp_path / "b.tar.gz"
        bundle.write_bytes(b"x")
        fake = self._fake({"s3api get-bucket-policy": (0, json.dumps({"Policy": "{}"}), "")})
        monkeypatch.setattr(engine, "run_aws", fake)
        with pytest.raises(remote.DestinationError) as e:
            remote.upload(bundle, self._dest(), "p")
        assert "acquired a bucket policy" in str(e.value)
        assert fake.argv_for("s3api put-object") == [], "refused too late — it wrote"

    def test_an_unreadable_policy_refuses_the_upload(self, tmp_path, monkeypatch):
        """Fails CLOSED, reversing an earlier deliberate choice to warn and proceed.

        The argument for proceeding was that a transient throttle should not cost a
        backup. The argument against, which wins: a profile simply lacking
        `s3:GetBucketPolicy` makes the answer permanently unknown, so every run would
        warn and proceed, the operator would learn to ignore the line, and the check
        would be decorative exactly when it matters. Refusing costs only the off-host
        copy — the local bundle is already written — and says what to fix.
        """
        bundle = tmp_path / "b.tar.gz"
        bundle.write_bytes(b"x")
        fake = self._fake({"s3api get-bucket-policy": (255, "", "AccessDenied")})
        monkeypatch.setattr(engine, "run_aws", fake)
        with pytest.raises(remote.DestinationError) as e:
            remote.upload(bundle, self._dest(), "p")
        assert "could not be read" in str(e.value)
        assert "s3:GetBucketPolicy" in str(e.value), "the refusal must name the fix"
        assert fake.argv_for("s3api put-object") == [], "it uploaded anyway"

    def test_an_oversized_bundle_is_refused_before_any_call(self, tmp_path, monkeypatch):
        """A single PutObject is capped at 5 GiB; say so instead of failing mid-transfer.

        The limit is lowered for the test rather than writing a 5 GiB file — and rather
        than patching `Path.stat`, which is global enough to break the test runner.
        """
        bundle = tmp_path / "huge.tar.gz"
        bundle.write_bytes(b"x" * 4096)
        monkeypatch.setattr(remote, "_MAX_SINGLE_PUT_BYTES", 1024)
        fake = FakeAws()
        monkeypatch.setattr(engine, "run_aws", fake)
        with pytest.raises(remote.DestinationError) as e:
            remote.upload(bundle, self._dest(), "p")
        assert "single-object limit" in str(e.value)
        assert fake.calls == []

    def test_upload_timeout_is_sized_from_the_bundle(self, tmp_path, monkeypatch):
        bundle = tmp_path / "big.tar.gz"
        bundle.write_bytes(b"x" * 5_000_000)
        fake = self._fake()
        monkeypatch.setattr(engine, "run_aws", fake)
        remote.upload(bundle, self._dest(), "p")
        put_timeouts = [
            t for c, t in zip(fake.calls, fake.timeouts) if " ".join(c[:2]) == "s3api put-object"
        ]
        assert put_timeouts[-1] == remote.timeout_for_bytes(5_000_000)
        assert put_timeouts[-1] > 30


class TestTheWritePathTakesNoBucketFromTheCaller:
    def test_snapshot_has_no_flag_that_names_a_bucket(self):
        """Structural: the only way to change where a backup goes is `backup setup`.

        A flag taking an arbitrary s3:// target is what forced the write path to judge
        bucket safety on every run. `--to` still EXISTS, but only as a rejection — it is
        defined so argparse cannot accept it as an abbreviation of `--to-s3` and silently
        write the bundle into a local directory named `s3:`.
        """
        import inspect

        from kiro_crew import snapshot as snap

        src = inspect.getsource(snap.snapshot_main)
        assert "--to-s3" in src
        assert "is no longer accepted" in src, "--to must be rejected, not honoured"
        # It must never reach the upload path.
        assert "args.to)" not in src.replace('getattr(args, "to", None)', "")

    def test_the_retired_flag_is_refused_with_a_migration_pointer(self, home, tmp_path, capsys):
        _setup_fake_kirocrew(home)
        rc = snapshot_main([str(tmp_path / "out"), "--to", "s3://someones-bucket/x"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "no longer accepted" in out
        assert "kirocrew backup setup" in out
        # And it must not have written a bundle into a directory named after the URL.
        assert not list(tmp_path.glob("s3:*"))

    def test_upload_bundle_reads_the_registry_not_the_args(self):
        import inspect

        from kiro_crew import snapshot as snap

        src = inspect.getsource(snap._upload_bundle)
        assert "load_destination()" in src
        assert "parse_s3_url" not in src
