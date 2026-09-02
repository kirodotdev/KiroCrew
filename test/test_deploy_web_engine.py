"""Tests for deploy_web.engine — deterministic deploy flow with mocked aws CLI."""
from __future__ import annotations

import json
import os
import re

import pytest

from kiro_crew.deploy import engine


class FakeAWS:
    """Records aws CLI calls and returns canned responses keyed by a matcher.

    Monkeypatched in for engine.run_aws. Each call records the argv (sans the
    leading 'aws'/profile) so tests can assert ordering and content.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.account = "123456789012"

    def __call__(self, args, profile, timeout=30):  # noqa: ANN001
        self.calls.append(list(args))
        sub = args[0]
        if sub == "resourcegroupstaggingapi":
            return 0, json.dumps({"ResourceTagMappingList": []}), ""
        if sub == "sts":
            return 0, json.dumps({"Account": self.account}), ""
        if sub == "s3api" and args[1] == "create-bucket":
            return 0, "", ""
        if sub == "cloudfront" and args[1] == "create-origin-access-control":
            return 0, json.dumps({"OriginAccessControl": {"Id": "OAC123"}}), ""
        if sub == "cloudfront" and args[1] == "create-distribution-with-tags":
            return 0, json.dumps({"Distribution": {
                "Id": "DIST123",
                "ARN": "arn:aws:cloudfront::123456789012:distribution/DIST123",
                "DomainName": "d111abc.cloudfront.net",
            }}), ""
        if sub == "cloudfront" and args[1] == "get-distribution":
            return 0, json.dumps({"Distribution": {
                "Status": "Deployed", "DomainName": "d111abc.cloudfront.net"}}), ""
        # s3api put-*, s3 sync, cloudfront create-invalidation, etc.
        return 0, "{}", ""

    def actions(self) -> list[str]:
        """Compact action labels in call order, for ordering assertions."""
        labels = []
        for a in self.calls:
            labels.append(f"{a[0]} {a[1]}" if len(a) > 1 else a[0])
        return labels


@pytest.fixture
def fake(monkeypatch):
    f = FakeAWS()
    monkeypatch.setattr(engine, "run_aws", f)
    return f


@pytest.mark.skipif(
    os.name == "nt",
    reason="the fallback install dirs are macOS path literals and provenance "
    "validation needs POSIX uid semantics; the fallback branch is dead on "
    "Windows by design (PATH-hit and bare-name behaviour are covered below)",
)
def test_aws_bin_resolved_absolutely_from_extra_dirs(monkeypatch, tmp_path):
    """A Finder-launched gateway has a minimal PATH; _aws must still resolve the
    CLI absolutely from a well-known install dir instead of emitting a bare
    'aws' that fails execvp inside the sandbox."""
    fake_aws = tmp_path / "aws"
    fake_aws.write_text("#!/bin/sh\n")
    fake_aws.chmod(0o755)
    # Simulate the minimal launchd-style PATH *hermetically*: a PATH that does
    # not contain aws (a literal /usr/bin would flake on hosts where the real
    # CLI is installed there), and point the well-known dirs at our temp bin so
    # the resolver can only find it through the fallback search.
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(tmp_path),))

    # Isolate the provenance chokepoint (it has its own dedicated tests and its
    # verdict depends on host /tmp ownership): assert the resolver routes the
    # fallback hit through it and returns its canonical result.
    from kiro_crew import github_runner

    validated = []

    def _passthrough(candidate):
        validated.append(candidate)
        return candidate

    monkeypatch.setattr(github_runner, "validate_provider_executable", _passthrough)

    argv = engine._aws(["sts", "get-caller-identity"], "kauai")

    assert argv[0] == str(fake_aws)  # absolute, not a bare "aws"
    assert argv[1:] == ["sts", "get-caller-identity", "--profile", "kauai"]
    assert validated == [str(fake_aws)]  # fallback hit went through provenance


def test_aws_bin_path_hit_wins_without_fallback(monkeypatch, tmp_path):
    """A PATH hit is the pre-existing trust class (execvp already ran exactly
    this binary) and resolves absolutely with no fallback-dir involvement."""
    if os.name == "nt":
        # Windows resolves executables by PATHEXT extension, not the exec bit.
        fake_aws = tmp_path / "aws.cmd"
        fake_aws.write_text("@echo off\n")
        monkeypatch.setenv("PATHEXT", ".cmd")
    else:
        fake_aws = tmp_path / "aws"
        fake_aws.write_text("#!/bin/sh\n")
        fake_aws.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", ())

    assert engine.resolve_aws_bin() == str(fake_aws)


@pytest.mark.skipif(os.name == "nt", reason="fallback branch is dead on Windows")
def test_aws_bin_fallback_hit_failing_provenance_returns_bare_name(monkeypatch, tmp_path):
    """A fallback-dir hit that fails the repo's executable-provenance check is
    refused: fall back to the bare name (the prior not-found behaviour) instead
    of executing a possibly planted shim inside the credential sandbox."""
    fake_aws = tmp_path / "aws"
    fake_aws.write_text("#!/bin/sh\n")
    fake_aws.chmod(0o755)
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(tmp_path),))

    from kiro_crew import github_runner

    def _refuse(candidate):
        raise ValueError("planted shim")

    monkeypatch.setattr(github_runner, "validate_provider_executable", _refuse)

    assert engine.resolve_aws_bin() == "aws"


def test_aws_bin_prefers_path_then_falls_back_to_bare_name(monkeypatch):
    """When the CLI is nowhere on PATH or the extra dirs, fall back to the bare
    'aws' so the prior 'not found' behaviour/error is preserved."""
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", ())

    assert engine.resolve_aws_bin() == "aws"
    assert engine._aws(["s3", "ls"], "")[0] == "aws"


# --- #5392: the same minimal-PATH gap for session-manager-plugin -------------


@pytest.mark.skipif(
    os.name == "nt",
    reason="the fallback install dirs are macOS path literals and provenance "
    "validation needs POSIX uid semantics; the fallback branch is dead on "
    "Windows by design",
)
def test_aws_tool_bin_resolves_session_manager_plugin_from_extra_dirs(monkeypatch, tmp_path):
    """The plugin installs into the SAME dirs as the CLI, so the same resolver
    must find it: AWS's macOS .pkg symlinks it into /usr/local/bin, which a
    Finder-launched gateway's minimal PATH does not contain (#5392)."""
    fake_plugin = tmp_path / "session-manager-plugin"
    fake_plugin.write_text("#!/bin/sh\n")
    fake_plugin.chmod(0o755)
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(tmp_path),))

    from kiro_crew import github_runner

    validated = []

    def _passthrough(candidate):
        validated.append(candidate)
        return candidate

    monkeypatch.setattr(github_runner, "validate_provider_executable", _passthrough)

    assert engine.resolve_aws_tool_bin("session-manager-plugin") == str(fake_plugin)
    # The fallback hit is routed through provenance exactly like the CLI's is —
    # these dirs are user-writable, and the plugin is executed by a child that
    # holds AWS credentials and a live tunnel.
    assert validated == [str(fake_plugin)]


@pytest.mark.skipif(os.name == "nt", reason="fallback branch is dead on Windows")
def test_aws_tool_bin_refused_provenance_falls_back_to_that_tool_s_name(monkeypatch, tmp_path):
    """The bare-name fallback is the REQUESTED tool, not a hardcoded "aws".

    Pins the generalization: returning "aws" here would make a refused plugin
    shim silently spawn the CLI under the plugin's name.
    """
    fake_plugin = tmp_path / "session-manager-plugin"
    fake_plugin.write_text("#!/bin/sh\n")
    fake_plugin.chmod(0o755)
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(tmp_path),))

    from kiro_crew import github_runner

    def _refuse(candidate):
        raise ValueError("planted shim")

    monkeypatch.setattr(github_runner, "validate_provider_executable", _refuse)

    assert engine.resolve_aws_tool_bin("session-manager-plugin") == "session-manager-plugin"


def test_aws_spawn_env_appends_install_dirs_after_inherited_path(monkeypatch, tmp_path):
    """APPEND, never prepend: the inherited PATH keeps first claim on every name.

    This is the whole trust argument for widening a credential-bearing child's
    PATH — it can only make a previously-unresolvable lookup succeed, never
    re-point one the child already resolved.

    Dirs are tmp_path stand-ins for the real ``/opt/homebrew/bin`` and
    ``/usr/local/bin``, per this file's existing convention: a host path literal
    both flakes on hosts that really have the tool there and is unrunnable on
    Windows.
    """
    system, extra_system = tmp_path / "sysbin", tmp_path / "sysbin2"
    brew, pkg = tmp_path / "brew", tmp_path / "pkg"
    monkeypatch.setenv("PATH", os.pathsep.join([str(system), str(extra_system)]))
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(brew), str(pkg)))

    parts = engine.aws_spawn_env(str(pkg / "aws"))["PATH"].split(os.pathsep)

    assert parts == [str(system), str(extra_system), str(brew), str(pkg)]


def test_aws_spawn_env_refuses_to_widen_for_a_bare_argv_head(monkeypatch, tmp_path):
    """A bare head means provenance REFUSED a candidate in these dirs.

    That refusal is enforced ONLY by the bare name failing ``execvp`` against a
    PATH the dirs are absent from. Widening would put the refused binary back on
    the child's PATH and hand it AWS credentials — turning a fail-closed rejection
    into an execution. The env therefore comes back untouched.
    """
    system, brew, pkg = tmp_path / "sysbin", tmp_path / "brew", tmp_path / "pkg"
    inherited = str(system)
    monkeypatch.setenv("PATH", inherited)
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(brew), str(pkg)))

    env = engine.aws_spawn_env("aws")  # what resolve_aws_bin returns on refusal

    assert env["PATH"] == inherited
    assert str(pkg) not in env["PATH"]
    # A relative head is the same hazard: execvp would still search PATH.
    assert engine.aws_spawn_env("bin/aws")["PATH"] == inherited


def test_aws_spawn_env_is_inert_when_dirs_are_already_on_path(monkeypatch, tmp_path):
    """A terminal-launched gateway already carries the dirs: same env, no dupes.

    Keeps the fix scoped to the minimal-PATH case it exists for instead of
    reshuffling a PATH that already worked.
    """
    brew, pkg = tmp_path / "brew", tmp_path / "pkg"
    inherited = os.pathsep.join([str(tmp_path / "sysbin"), str(brew), str(pkg)])
    monkeypatch.setenv("PATH", inherited)
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(brew), str(pkg)))

    assert engine.aws_spawn_env(str(pkg / "aws"))["PATH"] == inherited


def test_aws_spawn_env_with_empty_inherited_path_has_no_leading_separator(monkeypatch, tmp_path):
    """An empty PATH must not produce a leading separator — that reads as "."
    (the child's cwd) to the exec lookup, which would be a search-path hole."""
    brew, pkg = tmp_path / "brew", tmp_path / "pkg"
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(brew), str(pkg)))

    path = engine.aws_spawn_env(str(pkg / "aws"))["PATH"]

    assert path == os.pathsep.join([str(brew), str(pkg)])
    assert not path.startswith(os.pathsep)


def test_aws_spawn_env_preserves_the_rest_of_the_environment(monkeypatch, tmp_path):
    """Only PATH changes. Passing env= to a spawn REPLACES the child's whole
    environment, so dropping anything here would break credential resolution
    (the CLI needs HOME/AWS_* to find ~/.aws and the active profile)."""
    system, pkg = tmp_path / "sysbin", tmp_path / "pkg"
    monkeypatch.setenv("PATH", str(system))
    monkeypatch.setenv("AWS_PROFILE", "kauai")
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(pkg),))

    env = engine.aws_spawn_env(str(pkg / "aws"))

    assert env["AWS_PROFILE"] == "kauai"
    assert env["PATH"] == os.pathsep.join([str(system), str(pkg)])
    # Everything except PATH is carried through untouched.
    assert {k: v for k, v in env.items() if k != "PATH"} == {
        k: v for k, v in os.environ.items() if k != "PATH"
    }


def test_random_bucket_name_format():
    name = engine.random_bucket_name()
    assert name.startswith("kirocrew-web-")
    suffix = name[len("kirocrew-web-"):]
    assert re.fullmatch(r"[0-9a-f]{12}", suffix), name
    # No account id, opaque
    assert "123456789012" not in name
    # Distinct each call
    assert engine.random_bucket_name() != name


def test_first_deploy_full_flow_and_ordering(fake):
    result = engine.deploy("cr-dashboard", "/tmp/site", profile="p", region="us-west-2")
    assert result["reused"] is False
    assert result["url"] == "https://d111abc.cloudfront.net/"
    assert result["bucket"].startswith("kirocrew-web-")
    assert result["distribution_id"] == "DIST123"

    acts = fake.actions()
    # Ordering gotcha (§4): create-distribution MUST precede put-bucket-policy.
    assert "cloudfront create-distribution-with-tags" in acts
    assert "s3api put-bucket-policy" in acts
    assert acts.index("cloudfront create-distribution-with-tags") < acts.index("s3api put-bucket-policy")
    # OAC created before distribution; sync + invalidate happen after policy.
    assert acts.index("cloudfront create-origin-access-control") < acts.index("cloudfront create-distribution-with-tags")
    assert acts.index("s3api put-bucket-policy") < acts.index("s3 sync")
    assert acts.index("s3 sync") < acts.index("cloudfront create-invalidation")


def test_bucket_policy_pins_distribution_arn(fake):
    engine.deploy("site-x", "/tmp/site", profile="p")
    policy_call = next(c for c in fake.calls if c[0] == "s3api" and c[1] == "put-bucket-policy")
    policy = json.loads(policy_call[policy_call.index("--policy") + 1])
    cond = policy["Statement"][0]["Condition"]["StringEquals"]
    assert cond["AWS:SourceArn"] == "arn:aws:cloudfront::123456789012:distribution/DIST123"


def test_distribution_tagged_at_creation(fake):
    engine.deploy("tagged-site", "/tmp/site", profile="p")
    call = next(c for c in fake.calls if c[1] == "create-distribution-with-tags")
    payload = json.loads(call[call.index("--distribution-config-with-tags") + 1])
    tags = {t["Key"]: t["Value"] for t in payload["Tags"]["Items"]}
    assert tags[engine.TAG_MANAGED] == "true"
    assert tags[engine.TAG_SITE] == "tagged-site"


def test_redeploy_is_idempotent_reuses_infra(monkeypatch):
    f = FakeAWS()

    def fake_run(args, profile, timeout=30):  # noqa: ANN001
        if args[0] == "resourcegroupstaggingapi":
            return 0, json.dumps({"ResourceTagMappingList": [
                {"ResourceARN": "arn:aws:s3:::kirocrew-web-deadbeef0001"},
                {"ResourceARN": "arn:aws:cloudfront::123456789012:distribution/DISTOLD"},
            ]}), ""
        return f(args, profile, timeout)

    monkeypatch.setattr(engine, "run_aws", fake_run)
    result = engine.deploy("existing", "/tmp/site", profile="p")
    assert result["reused"] is True
    assert result["bucket"] == "kirocrew-web-deadbeef0001"
    assert result["distribution_id"] == "DISTOLD"
    acts = f.actions()
    # Re-deploy must NOT create new infra — only sync + invalidate (+ status read).
    assert "s3api create-bucket" not in acts
    assert "cloudfront create-distribution-with-tags" not in acts
    assert "s3 sync" in acts and "cloudfront create-invalidation" in acts


def test_access_denied_maps_to_statement(monkeypatch):
    def denied(args, profile, timeout=30):  # noqa: ANN001
        if args[0] == "resourcegroupstaggingapi":
            return 0, json.dumps({"ResourceTagMappingList": []}), ""
        if args[0] == "s3api" and args[1] == "create-bucket":
            return 255, "", "An error occurred (AccessDenied) ... s3:CreateBucket denied"
        return 0, "{}", ""

    monkeypatch.setattr(engine, "run_aws", denied)
    with pytest.raises(engine.AWSError) as ei:
        engine.deploy("denied-site", "/tmp/site", profile="p")
    assert ei.value.missing_statement == "S3BucketLevel"


def test_map_access_denied_direct():
    assert engine.map_access_denied("boom AccessDenied cloudfront:CreateInvalidation") == "CloudFrontManageTagged"
    assert engine.map_access_denied("all good") is None


def test_bucket_name_409_retry(monkeypatch):
    f = FakeAWS()
    seen = {"n": 0}

    def run(args, profile, timeout=30):  # noqa: ANN001
        if args[0] == "resourcegroupstaggingapi":
            return 0, json.dumps({"ResourceTagMappingList": []}), ""
        if args[0] == "s3api" and args[1] == "create-bucket":
            seen["n"] += 1
            if seen["n"] == 1:
                return 1, "", "An error occurred (BucketAlreadyExists)"
            return 0, "", ""
        return f(args, profile, timeout)

    monkeypatch.setattr(engine, "run_aws", run)
    result = engine.deploy("retry-site", "/tmp/site", profile="p")
    assert result["reused"] is False
    assert seen["n"] == 2  # retried once after the 409


def test_partial_deploy_recovery_reuses_bucket(fake, monkeypatch):
    """Bucket tagged but distribution missing (partial prior deploy): reuse the
    existing bucket instead of allocating a new one that would orphan it."""
    monkeypatch.setattr(
        engine, "find_site_by_tag",
        lambda sid, p, region=engine.DEFAULT_REGION: {
            "bucket": "kirocrew-web-deadbeef0000", "distribution_id": "", "distribution_arn": ""},
    )
    result = engine.deploy("cr-dash", "/tmp/site", profile="p", region="us-west-2")
    # Reused the tagged bucket; created the missing distribution (not a full reuse).
    assert result["bucket"] == "kirocrew-web-deadbeef0000"
    assert result["reused"] is False
    # No new bucket was allocated — the existing one was not orphaned.
    assert "s3api create-bucket" not in fake.actions()
    # Distribution creation + sync still ran.
    assert "cloudfront create-distribution-with-tags" in fake.actions()
