"""Tests for deploy_web.engine — deterministic deploy flow with mocked aws CLI."""
from __future__ import annotations

import json
import os
import re
import sys
import threading

import pytest

from kiro_crew.deploy import engine
from kiro_crew.testing.wait import default_timeout


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


@pytest.mark.skipif(os.name == "nt", reason="fallback branch is dead on Windows")
def test_aws_bin_refused_dir_does_not_shadow_a_good_later_dir(monkeypatch, tmp_path):
    """A refusal ends that DIR, not the search.

    Reported from a macOS host where Homebrew is managed by a second
    unprivileged account (Workbrew): /opt/homebrew/bin/aws is owned by
    ``workbrew`` and correctly refused, but it also sorts first, so stopping at
    the refusal never reached the root-owned official-pkg /usr/local/bin/aws and
    every SSM tunnel died at gateway start with "[Errno 2] ... 'aws'". Installing
    the official pkg — the documented remedy — could not fix it.
    """
    bad_dir = tmp_path / "brew"
    good_dir = tmp_path / "usrlocal"
    empty_bin = tmp_path / "emptybin"
    for d in (bad_dir, good_dir, empty_bin):
        d.mkdir()
    for d in (bad_dir, good_dir):
        binary = d / "aws"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(bad_dir), str(good_dir)))
    monkeypatch.setattr(engine, "_REFUSED_AWS_BIN_DIRS", set())

    from kiro_crew import github_runner

    def _refuse_only_bad(candidate):
        if candidate.startswith(str(bad_dir)):
            raise ValueError("owned by another unprivileged account")
        return candidate

    monkeypatch.setattr(github_runner, "validate_provider_executable", _refuse_only_bad)

    assert engine.resolve_aws_bin() == str(good_dir / "aws")
    # The refused dir is remembered so it stays off a credential-bearing
    # child's PATH; the accepted one must not be recorded. Keyed by (name, dir).
    assert engine._REFUSED_AWS_BIN_DIRS == {("aws", str(bad_dir))}


@pytest.mark.skipif(os.name == "nt", reason="fallback branch is dead on Windows")
def test_aws_spawn_env_omits_a_refused_dir_while_widening_for_a_good_one(monkeypatch, tmp_path):
    """Walking past a refusal must not smuggle the refused dir onto the child.

    A resolved absolute head does not imply every install dir was clean, so
    the widening has to drop the individual dirs that were refused — otherwise
    the shim just rejected as the argv head becomes reachable again through the
    CLI's own onward session-manager-plugin lookup, with AWS credentials.
    """
    bad_dir = tmp_path / "brew"
    good_dir = tmp_path / "usrlocal"
    empty_bin = tmp_path / "emptybin"
    for d in (bad_dir, good_dir, empty_bin):
        d.mkdir()
    for d in (bad_dir, good_dir):
        binary = d / "aws"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(bad_dir), str(good_dir)))
    monkeypatch.setattr(engine, "_REFUSED_AWS_BIN_DIRS", set())

    from kiro_crew import github_runner

    def _refuse_only_bad(candidate):
        if candidate.startswith(str(bad_dir)):
            raise ValueError("owned by another unprivileged account")
        return candidate

    monkeypatch.setattr(github_runner, "validate_provider_executable", _refuse_only_bad)
    monkeypatch.setattr(github_runner, "validate_provider_directory", lambda candidate: candidate)

    aws_bin = engine.resolve_aws_bin()
    child_path = engine.aws_spawn_env(aws_bin)["PATH"].split(os.pathsep)

    assert str(good_dir) in child_path  # the plugin's real install dir
    assert str(bad_dir) not in child_path  # refused: must stay unreachable
    assert child_path.index(str(empty_bin)) < child_path.index(str(good_dir))


@pytest.mark.skipif(os.name == "nt", reason="fallback branch is dead on Windows")
def test_aws_bin_every_dir_refused_still_returns_bare_name(monkeypatch, tmp_path):
    """Exhausting the list is still fail-closed: no dir may be executed, and the
    prior not-found/execvp error is what the caller gets."""
    first = tmp_path / "brew"
    second = tmp_path / "usrlocal"
    empty_bin = tmp_path / "emptybin"
    for d in (first, second, empty_bin):
        d.mkdir()
    for d in (first, second):
        binary = d / "aws"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(first), str(second)))
    monkeypatch.setattr(engine, "_REFUSED_AWS_BIN_DIRS", set())

    from kiro_crew import github_runner

    def _refuse(candidate):
        raise ValueError("planted shim")

    monkeypatch.setattr(github_runner, "validate_provider_executable", _refuse)

    assert engine.resolve_aws_bin() == "aws"
    # A bare head means the env stays unwidened, so neither dir is reachable.
    assert engine.aws_spawn_env("aws").get("PATH", "") == str(empty_bin)


@pytest.mark.skipif(os.name == "nt", reason="fallback branch is dead on Windows")
def test_aws_bin_repaired_dir_is_cleared_from_the_refused_set(monkeypatch, tmp_path):
    """Fixing the install must take effect without a gateway restart: the
    resolver re-validates on every call, so a dir that passes again is cleared
    and its PATH entry comes back."""
    install = tmp_path / "brew"
    empty_bin = tmp_path / "emptybin"
    for d in (install, empty_bin):
        d.mkdir()
    binary = install / "aws"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(install),))
    monkeypatch.setattr(engine, "_REFUSED_AWS_BIN_DIRS", set())

    from kiro_crew import github_runner

    ownership_fixed = {"yet": False}

    def _validate(candidate):
        if not ownership_fixed["yet"]:
            raise ValueError("owned by another unprivileged account")
        return candidate

    monkeypatch.setattr(github_runner, "validate_provider_executable", _validate)

    assert engine.resolve_aws_bin() == "aws"
    assert engine._REFUSED_AWS_BIN_DIRS == {("aws", str(install))}

    ownership_fixed["yet"] = True

    assert engine.resolve_aws_bin() == str(binary)
    assert engine._REFUSED_AWS_BIN_DIRS == set()


@pytest.mark.skipif(os.name == "nt", reason="fallback branch is dead on Windows")
def test_aws_pass_does_not_clear_a_refused_plugin_in_the_same_dir(monkeypatch, tmp_path):
    """A passing resolve of one name must not re-admit a dir refused for another.

    resolve_aws_tool_bin is generic over name: aws and session-manager-plugin
    share one install dir. In a mixed-ownership dir a brew-owned
    session-manager-plugin is refused while a root-owned aws passes. Keying the
    refusal by (name, dir) means the aws pass cannot discard the plugin's refusal,
    so aws_spawn_env still drops that dir and the refused plugin cannot be reached
    through the CLI's by-name onward lookup with AWS credentials.
    """
    mixed = tmp_path / "brew"
    good = tmp_path / "usrlocal"
    empty_bin = tmp_path / "emptybin"
    for d in (mixed, good, empty_bin):
        d.mkdir()
    # Both tools exist in the mixed dir; only aws also exists in the good dir.
    for name in ("aws", "session-manager-plugin"):
        b = mixed / name
        b.write_text("#!/bin/sh\n")
        b.chmod(0o755)
    for name in ("aws", "session-manager-plugin"):
        b = good / name
        b.write_text("#!/bin/sh\n")
        b.chmod(0o755)
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(mixed), str(good)))
    monkeypatch.setattr(engine, "_REFUSED_AWS_BIN_DIRS", set())

    from kiro_crew import github_runner

    def _refuse_mixed_plugin(candidate):
        # Only the plugin in the mixed dir is refused; aws there passes.
        if candidate == str(mixed / "session-manager-plugin"):
            raise ValueError("plugin owned by another unprivileged account")
        return candidate

    monkeypatch.setattr(github_runner, "validate_provider_executable", _refuse_mixed_plugin)
    monkeypatch.setattr(github_runner, "validate_provider_directory", lambda candidate: candidate)

    # Resolve the plugin: refused in the mixed dir, accepted in the good dir.
    assert engine.resolve_aws_tool_bin("session-manager-plugin") == str(
        good / "session-manager-plugin"
    )
    assert ("session-manager-plugin", str(mixed)) in engine._REFUSED_AWS_BIN_DIRS

    # Now resolve aws: it passes in the mixed dir (its own name is clean there).
    assert engine.resolve_aws_bin() == str(mixed / "aws")
    # The aws pass must NOT have cleared the plugin's refusal for the mixed dir.
    assert ("session-manager-plugin", str(mixed)) in engine._REFUSED_AWS_BIN_DIRS

    # So the credential-bearing child's PATH still excludes the mixed dir: the
    # refused plugin cannot come back through the CLI's by-name onward lookup.
    child_path = engine.aws_spawn_env(str(mixed / "aws"))["PATH"].split(os.pathsep)
    assert str(mixed) not in child_path
    assert str(good) in child_path


def test_aws_bin_prefers_path_then_falls_back_to_bare_name(monkeypatch):
    """When the CLI is nowhere on PATH or the extra dirs, fall back to the bare
    'aws' so the prior 'not found' behaviour/error is preserved."""
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", ())

    assert engine.resolve_aws_bin() == "aws"
    assert engine._aws(["s3", "ls"], "")[0] == "aws"


# --- the same minimal-PATH gap for session-manager-plugin -------------


@pytest.mark.skipif(
    os.name == "nt",
    reason="the fallback install dirs are macOS path literals and provenance "
    "validation needs POSIX uid semantics; the fallback branch is dead on "
    "Windows by design",
)
def test_aws_tool_bin_resolves_session_manager_plugin_from_extra_dirs(monkeypatch, tmp_path):
    """The plugin installs into the SAME dirs as the CLI, so the same resolver
    must find it: AWS's macOS .pkg symlinks it into /usr/local/bin, which a
    Finder-launched gateway's minimal PATH does not contain."""
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


@pytest.mark.skipif(os.name == "nt", reason="fallback branch is dead on Windows")
def test_resolver_reports_the_candidates_this_walk_refused(monkeypatch, tmp_path):
    """The refusal list is what lets a caller say "found but refused" instead of
    "not installed"; it reports the walk's own decision and changes none of it."""
    refused_dir, good_dir, empty_bin = tmp_path / "brew", tmp_path / "pkg", tmp_path / "e"
    for d in (refused_dir, good_dir, empty_bin):
        d.mkdir()
    refused = refused_dir / "session-manager-plugin"
    refused.write_text("#!/bin/sh\n")
    refused.chmod(0o755)
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(refused_dir), str(good_dir)))
    monkeypatch.setattr(engine, "_REFUSED_AWS_BIN_DIRS", set())

    from kiro_crew import github_runner

    def _validate(candidate):
        if candidate.startswith(str(refused_dir)):
            raise ValueError("executable is owned by another user (uid 1501)")
        return candidate

    monkeypatch.setattr(github_runner, "validate_provider_executable", _validate)

    resolved, refusals = engine.resolve_aws_tool_bin_with_refusals("session-manager-plugin")
    assert resolved == "session-manager-plugin"
    assert refusals == (
        engine.AwsToolRefusal(str(refused), "executable is owned by another user (uid 1501)"),
    )
    assert engine._REFUSED_AWS_BIN_DIRS == {("session-manager-plugin", str(refused_dir))}

    accepted = good_dir / "session-manager-plugin"
    accepted.write_text("#!/bin/sh\n")
    accepted.chmod(0o755)
    resolved, refusals = engine.resolve_aws_tool_bin_with_refusals("session-manager-plugin")
    assert resolved == str(accepted)
    assert [r.path for r in refusals] == [str(refused)]


@pytest.mark.skipif(os.name == "nt", reason="fallback branch is dead on Windows")
def test_resolver_refusals_come_from_this_walk_not_the_shared_set(monkeypatch, tmp_path):
    """A candidate removed since an earlier refusal is not reported as refused."""
    gone_dir, empty_bin = tmp_path / "brew", tmp_path / "e"
    gone_dir.mkdir()
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(gone_dir),))
    monkeypatch.setattr(
        engine, "_REFUSED_AWS_BIN_DIRS", {("session-manager-plugin", str(gone_dir))}
    )

    assert engine.resolve_aws_tool_bin_with_refusals("session-manager-plugin") == (
        "session-manager-plugin",
        (),
    )


def test_aws_spawn_env_appends_install_dirs_after_inherited_path(monkeypatch, tmp_path):
    """APPEND, never prepend: the inherited PATH keeps first claim on every name.

    This is the whole trust argument for widening a credential-bearing child's
    PATH — it can only make an unresolvable lookup succeed, never
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
    monkeypatch.setattr(engine, "_TRUSTED_AWS_BIN_DIRS", ((str(brew), str(brew)), (str(pkg), str(pkg))))

    parts = engine.aws_spawn_env(str(pkg / "aws"))["PATH"].split(os.pathsep)

    assert parts == [str(system), str(extra_system), str(brew), str(pkg)]


def test_resolver_excludes_untrusted_candidate_free_dir_from_spawn_path(monkeypatch, tmp_path):
    inherited = tmp_path / "sysbin"
    inherited.mkdir()
    aws = inherited / ("aws.cmd" if os.name == "nt" else "aws")
    aws.write_text("@echo off\n" if os.name == "nt" else "#!/bin/sh\n")
    if os.name != "nt":
        aws.chmod(0o755)
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    monkeypatch.setenv("PATH", str(inherited))
    if os.name == "nt":
        monkeypatch.setenv("PATHEXT", ".cmd")
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(fallback),))
    monkeypatch.setattr(engine, "_TRUSTED_AWS_BIN_DIRS", ())

    from kiro_crew import github_runner

    def _refuse_directory(candidate):
        assert candidate == str(fallback)
        raise ValueError("directory is owned by another account")

    monkeypatch.setattr(github_runner, "validate_provider_directory", _refuse_directory)

    assert engine.resolve_aws_bin() == str(aws)
    assert engine.aws_spawn_env(str(aws))["PATH"] == str(inherited)


def test_resolver_admits_trusted_candidate_free_dir_to_spawn_path(monkeypatch, tmp_path):
    inherited = tmp_path / "sysbin"
    inherited.mkdir()
    aws = inherited / ("aws.cmd" if os.name == "nt" else "aws")
    aws.write_text("@echo off\n" if os.name == "nt" else "#!/bin/sh\n")
    if os.name != "nt":
        aws.chmod(0o755)
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    monkeypatch.setenv("PATH", str(inherited))
    if os.name == "nt":
        monkeypatch.setenv("PATHEXT", ".cmd")
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(fallback),))
    monkeypatch.setattr(engine, "_TRUSTED_AWS_BIN_DIRS", ())

    from kiro_crew import github_runner

    checked = []

    def _accept_directory(candidate):
        checked.append(candidate)
        return candidate

    monkeypatch.setattr(github_runner, "validate_provider_directory", _accept_directory)

    assert engine.resolve_aws_bin() == str(aws)
    assert checked == [str(fallback)]
    assert engine.aws_spawn_env(str(aws))["PATH"].split(os.pathsep) == [
        str(inherited),
        str(fallback),
    ]


def test_trust_refresh_keeps_the_previous_snapshot_visible_until_publish(monkeypatch, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    directories = (str(first), str(second))
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", directories)
    monkeypatch.setattr(engine, "_TRUSTED_AWS_BIN_DIRS", tuple((directory, directory) for directory in directories))
    monkeypatch.setenv("PATH", str(tmp_path / "sysbin"))
    from kiro_crew import github_runner

    snapshots = []

    def _accept_directory(candidate):
        snapshots.append(engine.tool_spawn_env(str(tmp_path / "ssh"))["PATH"].split(os.pathsep))
        return candidate

    monkeypatch.setattr(github_runner, "validate_provider_directory", _accept_directory)

    engine._refresh_aws_bin_dir_trust()

    assert snapshots == [
        [str(tmp_path / "sysbin"), *directories],
        [str(tmp_path / "sysbin"), *directories],
    ]
    assert engine._TRUSTED_AWS_BIN_DIRS == tuple((directory, directory) for directory in directories)


def test_trust_refresh_serializes_validation_and_publish(monkeypatch, tmp_path):
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(fallback),))
    monkeypatch.setattr(engine, "_TRUSTED_AWS_BIN_DIRS", ())
    monkeypatch.setattr(engine, "_WARNED_AWS_BIN_DIR_REFUSALS", set())
    from kiro_crew import github_runner

    first_validation_started = threading.Event()
    second_refresh_started = threading.Event()
    second_refresh_done = threading.Event()
    release_first = threading.Event()
    errors = []

    def _validate(candidate):
        assert candidate == str(fallback)
        if threading.current_thread().name == "trust-refresh-first":
            first_validation_started.set()
            assert release_first.wait(default_timeout())
            return candidate
        raise ValueError("directory trust refused")

    def _run_refresh(started=None, done=None):
        if started is not None:
            started.set()
        try:
            engine._refresh_aws_bin_dir_trust()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            if done is not None:
                done.set()

    monkeypatch.setattr(github_runner, "validate_provider_directory", _validate)
    first = threading.Thread(target=_run_refresh, name="trust-refresh-first")
    second = threading.Thread(
        target=_run_refresh,
        args=(second_refresh_started, second_refresh_done),
        name="trust-refresh-second",
    )

    first.start()
    assert first_validation_started.wait(default_timeout())
    second.start()
    assert second_refresh_started.wait(default_timeout())
    # Negative check: a short bound can only let it pass early, never fail it.
    assert not second_refresh_done.wait(0.2), "the second refresh must wait for the first refresh's lock"

    release_first.set()
    first.join(timeout=default_timeout())
    second.join(timeout=default_timeout())

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert engine._TRUSTED_AWS_BIN_DIRS == ()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_trust_refresh_publishes_a_fallback_symlinks_canonical_target(monkeypatch, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    fallback = tmp_path / "fallback"
    fallback.symlink_to(target, target_is_directory=True)
    inherited = tmp_path / "sysbin"
    monkeypatch.setenv("PATH", str(inherited))
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(fallback),))
    monkeypatch.setattr(engine, "_TRUSTED_AWS_BIN_DIRS", ())
    from kiro_crew import github_runner

    monkeypatch.setattr(github_runner.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())
    monkeypatch.setattr(github_runner, "check_provider_path_component", lambda *a, **kw: None)

    engine._refresh_aws_bin_dir_trust()

    assert engine._TRUSTED_AWS_BIN_DIRS == ((str(fallback), str(target)),)
    assert engine.tool_spawn_env(str(inherited / "aws"))["PATH"].split(os.pathsep) == [
        str(inherited),
        str(target),
    ]


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_retargeting_a_fallback_link_does_not_change_the_child_path(monkeypatch, tmp_path):
    first_target = tmp_path / "first-target"
    second_target = tmp_path / "second-target"
    first_target.mkdir()
    second_target.mkdir()
    fallback = tmp_path / "fallback"
    fallback.symlink_to(first_target, target_is_directory=True)
    inherited = tmp_path / "sysbin"
    monkeypatch.setenv("PATH", str(inherited))
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(fallback),))
    monkeypatch.setattr(engine, "_TRUSTED_AWS_BIN_DIRS", ())
    from kiro_crew import github_runner

    monkeypatch.setattr(github_runner.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())
    monkeypatch.setattr(github_runner, "check_provider_path_component", lambda *a, **kw: None)

    engine._refresh_aws_bin_dir_trust()
    fallback.unlink()
    fallback.symlink_to(second_target, target_is_directory=True)

    child_path = engine.tool_spawn_env(str(inherited / "aws"))["PATH"].split(os.pathsep)
    assert child_path == [str(inherited), str(first_target)]
    assert str(second_target) not in child_path


@pytest.mark.parametrize("present", ["original", "canonical"])
def test_spawn_env_deduplicates_original_and_canonical_spellings(monkeypatch, tmp_path, present):
    original = str(tmp_path / "fallback-link")
    canonical = str(tmp_path / "fallback-target")
    inherited = original if present == "original" else canonical
    monkeypatch.setenv("PATH", inherited)
    monkeypatch.setattr(engine, "_TRUSTED_AWS_BIN_DIRS", ((original, canonical),))

    assert engine.tool_spawn_env(str(tmp_path / "aws"))["PATH"] == inherited


@pytest.mark.skipif(os.name == "nt", reason="POSIX resolver walk")
def test_provider_directory_refuses_an_inconsistent_resolver_walk(monkeypatch, tmp_path):
    from kiro_crew import github_runner

    directory = tmp_path / "trusted"
    directory.mkdir()
    monkeypatch.setattr(github_runner.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())
    monkeypatch.setattr(
        github_runner.platform_compat,
        "traversed_components_and_links",
        lambda _path: ([tmp_path / "different"], []),
    )

    with pytest.raises(ValueError, match="directory path did not resolve consistently"):
        github_runner.validate_provider_directory(str(directory))


def test_missing_fallback_directory_does_not_warn(monkeypatch, tmp_path, caplog):
    missing = tmp_path / "missing"
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(missing),))
    monkeypatch.setattr(engine, "_TRUSTED_AWS_BIN_DIRS", ((str(missing), str(missing)),))

    engine._refresh_aws_bin_dir_trust()

    assert engine._TRUSTED_AWS_BIN_DIRS == ()
    assert not [record for record in caplog.records if record.levelname == "WARNING"]


def test_existing_refused_fallback_directory_warns_once(monkeypatch, tmp_path, caplog):
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(fallback),))
    monkeypatch.setattr(engine, "_WARNED_AWS_BIN_DIR_REFUSALS", set())
    from kiro_crew import github_runner

    def _refuse_directory(_candidate):
        raise ValueError("directory trust refused")

    monkeypatch.setattr(github_runner, "validate_provider_directory", _refuse_directory)
    caplog.set_level("WARNING", logger=engine.logger.name)

    engine._refresh_aws_bin_dir_trust()
    engine._refresh_aws_bin_dir_trust()

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == engine.logger.name and record.levelname == "WARNING"
    ]
    assert warnings == [
        f"not appending AWS tooling directory {fallback} to child PATH: directory trust refused"
    ]


def test_aws_spawn_env_excludes_a_never_checked_dir(monkeypatch, tmp_path):
    inherited = tmp_path / "sysbin"
    fallback = tmp_path / "fallback"
    monkeypatch.setenv("PATH", str(inherited))
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(fallback),))
    monkeypatch.setattr(engine, "_TRUSTED_AWS_BIN_DIRS", ())

    assert engine.aws_spawn_env(str(inherited / "aws"))["PATH"] == str(inherited)


def test_aws_spawn_env_reads_no_filesystem(monkeypatch, tmp_path):
    inherited = tmp_path / "sysbin"
    fallback = tmp_path / "fallback"
    monkeypatch.setenv("PATH", str(inherited))
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(fallback),))
    monkeypatch.setattr(engine, "_TRUSTED_AWS_BIN_DIRS", ((str(fallback), str(fallback)),))

    def _filesystem_call(*_args, **_kwargs):
        raise AssertionError("spawn env touched the filesystem")

    monkeypatch.setattr(engine.os, "access", _filesystem_call)
    monkeypatch.setattr(engine.os, "stat", _filesystem_call)
    monkeypatch.setattr(engine.shutil, "which", _filesystem_call)

    assert engine.aws_spawn_env(str(inherited / "aws"))["PATH"].split(os.pathsep) == [
        str(inherited),
        str(fallback),
    ]


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
    monkeypatch.setattr(engine, "_TRUSTED_AWS_BIN_DIRS", ((str(brew), str(brew)), (str(pkg), str(pkg))))

    assert engine.aws_spawn_env(str(pkg / "aws"))["PATH"] == inherited


def test_aws_spawn_env_with_empty_inherited_path_has_no_leading_separator(monkeypatch, tmp_path):
    """An empty PATH must not produce a leading separator — that reads as "."
    (the child's cwd) to the exec lookup, which would be a search-path hole."""
    brew, pkg = tmp_path / "brew", tmp_path / "pkg"
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(brew), str(pkg)))
    monkeypatch.setattr(engine, "_TRUSTED_AWS_BIN_DIRS", ((str(brew), str(brew)), (str(pkg), str(pkg))))

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
    monkeypatch.setattr(engine, "_TRUSTED_AWS_BIN_DIRS", ((str(pkg), str(pkg)),))

    env = engine.aws_spawn_env(str(pkg / "aws"))

    assert env["AWS_PROFILE"] == "kauai"
    assert env["PATH"] == os.pathsep.join([str(system), str(pkg)])
    # Everything except PATH is carried through untouched.
    assert {k: v for k, v in env.items() if k != "PATH"} == {
        k: v for k, v in os.environ.items() if k != "PATH"
    }


def _isolate_provider_directory_walk(monkeypatch, directory):
    from kiro_crew import github_runner

    monkeypatch.setattr(github_runner.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())
    monkeypatch.setattr(github_runner, "strict_provider_bins", lambda: False)
    monkeypatch.setattr(
        github_runner.platform_compat,
        "traversed_components_and_links",
        lambda _path: ([directory], []),
    )
    monkeypatch.setattr(
        github_runner,
        "check_provider_path_component",
        lambda path, *, label, uid, strict: None,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_provider_directory_translates_a_cyclic_symlink_to_value_error(monkeypatch, tmp_path):
    from kiro_crew import github_runner

    first = tmp_path / "cycle-first"
    second = tmp_path / "cycle-second"
    first.symlink_to(second, target_is_directory=True)
    second.symlink_to(first, target_is_directory=True)
    _isolate_provider_directory_walk(monkeypatch, first)
    monkeypatch.setattr(
        github_runner.platform_compat,
        "group_write_admits_another_account",
        lambda path, gid, *, trust_root=False: None,
    )

    with pytest.raises(ValueError, match="path does not resolve"):
        github_runner.validate_provider_directory(str(first))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_trust_refresh_skips_a_cyclic_symlink_and_publishes_good_directory(monkeypatch, tmp_path):
    from kiro_crew import github_runner

    first = tmp_path / "cycle-first"
    second = tmp_path / "cycle-second"
    first.symlink_to(second, target_is_directory=True)
    second.symlink_to(first, target_is_directory=True)
    good = tmp_path / "good"
    good.mkdir()
    _isolate_provider_directory_walk(monkeypatch, good)
    monkeypatch.setattr(
        github_runner.platform_compat,
        "group_write_admits_another_account",
        lambda path, gid, *, trust_root=False: None,
    )
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(first), str(good)))
    monkeypatch.setattr(engine, "_TRUSTED_AWS_BIN_DIRS", ())
    monkeypatch.setattr(engine, "_WARNED_AWS_BIN_DIR_REFUSALS", set())

    engine._refresh_aws_bin_dir_trust()

    assert engine._TRUSTED_AWS_BIN_DIRS == ((str(good), str(good)),)
    assert all(original != str(first) for original, _canonical in engine._TRUSTED_AWS_BIN_DIRS)


@pytest.mark.skipif(os.name == "nt", reason="POSIX group-write semantics")
def test_provider_directory_refuses_a_group_shared_component(monkeypatch, tmp_path):
    from kiro_crew import github_runner

    directory = tmp_path / "shared"
    directory.mkdir(mode=0o775)
    directory.chmod(0o775)
    _isolate_provider_directory_walk(monkeypatch, directory)
    asked = []

    def _admits(path, gid, *, trust_root=False):
        asked.append((path, gid, trust_root))
        return "group 'admin', shared with 1 other account(s)"

    monkeypatch.setattr(
        github_runner.platform_compat, "group_write_admits_another_account", _admits
    )

    with pytest.raises(ValueError, match="group 'admin', shared with 1 other account"):
        github_runner.validate_provider_directory(str(directory))

    assert asked == [(directory, directory.stat().st_gid, True)]


@pytest.mark.skipif(os.name == "nt", reason="POSIX group-write semantics")
def test_provider_directory_accepts_a_group_holding_the_user_and_root(
    monkeypatch, tmp_path
):
    from kiro_crew import github_runner

    directory = tmp_path / "single-admin"
    directory.mkdir(mode=0o775)
    directory.chmod(0o775)
    _isolate_provider_directory_walk(monkeypatch, directory)
    asked = []

    def _admits(path, gid, *, trust_root=False):
        asked.append((path, gid, trust_root))
        return None

    monkeypatch.setattr(
        github_runner.platform_compat, "group_write_admits_another_account", _admits
    )

    assert github_runner.validate_provider_directory(str(directory)) == str(directory)
    assert asked == [(directory, directory.stat().st_gid, True)]


@pytest.mark.skipif(os.name == "nt", reason="POSIX group-write semantics")
def test_provider_directory_skips_the_membership_question_for_a_sticky_parent(
    monkeypatch, tmp_path
):
    from kiro_crew import github_runner

    parent = tmp_path / "sticky"
    parent.mkdir(mode=0o1777)
    parent.chmod(0o1777)
    directory = parent / "bin"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    _isolate_provider_directory_walk(monkeypatch, directory)
    monkeypatch.setattr(
        github_runner.platform_compat,
        "traversed_components_and_links",
        lambda _path: ([parent, directory], []),
    )

    def _not_asked(*args, **kwargs):
        raise AssertionError("a sticky parent does not need a group membership check")

    monkeypatch.setattr(
        github_runner.platform_compat, "group_write_admits_another_account", _not_asked
    )

    assert github_runner.validate_provider_directory(str(directory)) == str(directory)


@pytest.mark.skipif(os.name == "nt", reason="POSIX group-write semantics")
def test_provider_directory_asks_about_a_sticky_group_writable_path_entry(monkeypatch, tmp_path):
    from kiro_crew import github_runner

    directory = tmp_path / "sticky-entry"
    directory.mkdir(mode=0o1775)
    directory.chmod(0o1775)
    _isolate_provider_directory_walk(monkeypatch, directory)
    monkeypatch.setattr(
        github_runner.platform_compat,
        "group_write_admits_another_account",
        lambda path, gid, *, trust_root=False: "group 'staff', shared with 2 other account(s)",
    )

    with pytest.raises(ValueError, match="group 'staff', shared with 2 other account"):
        github_runner.validate_provider_directory(str(directory))


@pytest.mark.skipif(os.name == "nt", reason="POSIX world-write semantics")
def test_provider_directory_refuses_a_sticky_world_writable_path_entry(monkeypatch, tmp_path):
    from kiro_crew import github_runner

    directory = tmp_path / "shared-tmp"
    directory.mkdir(mode=0o1777)
    directory.chmod(0o1777)
    _isolate_provider_directory_walk(monkeypatch, directory)
    monkeypatch.setattr(
        github_runner.platform_compat,
        "group_write_admits_another_account",
        lambda path, gid, *, trust_root=False: None,
    )

    with pytest.raises(ValueError, match="writable by every account"):
        github_runner.validate_provider_directory(str(directory))


@pytest.mark.skipif(os.name == "nt", reason="POSIX group-write semantics")
def test_provider_directory_refuses_an_unresolvable_group(monkeypatch, tmp_path):
    from kiro_crew import github_runner

    directory = tmp_path / "unknown-group"
    directory.mkdir(mode=0o775)
    directory.chmod(0o775)
    _isolate_provider_directory_walk(monkeypatch, directory)
    monkeypatch.setattr(
        github_runner.platform_compat,
        "group_write_admits_another_account",
        lambda path, gid, *, trust_root=False: f"a group (gid {gid}) this host cannot resolve",
    )

    with pytest.raises(ValueError, match="group .* cannot resolve"):
        github_runner.validate_provider_directory(str(directory))


@pytest.mark.skipif(os.name == "nt", reason="POSIX group-write semantics")
def test_trust_refresh_keeps_a_group_shared_dir_off_the_child_path(monkeypatch, tmp_path):
    from kiro_crew import github_runner

    inherited = tmp_path / "sysbin"
    inherited.mkdir()
    fallback = tmp_path / "shared"
    fallback.mkdir(mode=0o775)
    fallback.chmod(0o775)
    _isolate_provider_directory_walk(monkeypatch, fallback)
    monkeypatch.setenv("PATH", str(inherited))
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(fallback),))
    monkeypatch.setattr(engine, "_TRUSTED_AWS_BIN_DIRS", ())
    monkeypatch.setattr(engine, "_WARNED_AWS_BIN_DIR_REFUSALS", set())
    monkeypatch.setattr(
        github_runner.platform_compat,
        "group_write_admits_another_account",
        lambda path, gid, *, trust_root=False: "group 'admin', shared with 1 other account(s)",
    )

    engine._refresh_aws_bin_dir_trust()

    assert engine._TRUSTED_AWS_BIN_DIRS == ()
    assert engine.tool_spawn_env(str(inherited / "ssh"))["PATH"] == str(inherited)


def test_provider_directory_uses_posix_component_policy(monkeypatch, tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX ownership semantics")
    from kiro_crew import github_runner

    directory = tmp_path / "trusted"
    directory.mkdir()
    checked = []
    monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())
    monkeypatch.setattr(github_runner.os, "geteuid", lambda: 4242)
    monkeypatch.setattr(github_runner.os, "getuid", lambda: 4242)

    def _check(path, *, label, uid, strict):
        checked.append((path, label, uid, strict))

    monkeypatch.setattr(github_runner, "check_provider_path_component", _check)
    monkeypatch.setattr(github_runner, "strict_provider_bins", lambda: True)

    assert github_runner.validate_provider_directory(str(directory)) == str(directory)
    assert checked
    assert checked[-1][0] == directory
    assert all(
        label == "directory" and uid == 4242 and strict
        for _path, label, uid, strict in checked
    )


def test_provider_directory_uses_windows_acl_policy(monkeypatch, tmp_path):
    from kiro_crew import github_runner

    directory = tmp_path / "trusted"
    directory.mkdir()
    checked = []
    monkeypatch.setattr(github_runner.sys, "platform", "win32")
    monkeypatch.setattr(github_runner.platform_compat, "current_user_sid", lambda: "S-1-5-21-me")
    monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())

    def _check(path, *, label, me_sid, strict):
        checked.append((path, label, me_sid, strict))

    monkeypatch.setattr(github_runner, "check_provider_path_component_windows", _check)

    assert github_runner.validate_provider_directory(str(directory)) == str(directory)
    assert checked
    assert checked[0][0] == directory
    assert all(
        label == "directory" and sid == "S-1-5-21-me" and not strict
        for _path, label, sid, strict in checked
    )


@pytest.mark.skipif(os.name == "nt", reason="fallback branch is dead on Windows")
def test_run_aws_passes_the_widened_spawn_env_to_the_child(monkeypatch, tmp_path):
    """``deploy.engine.run_aws`` hands the child the widened spawn env.

    The AWS CLI finds a profile's bare ``credential_process`` helper by name on
    the child's PATH. AWS Control's drive and backup calls and ``aws configure
    list-profiles`` spawn the CLI through this chokepoint, and a desktop-app
    gateway's PATH is the minimal launchd one, so the child needs
    ``aws_spawn_env(argv[0])``, the env ``cloud.aws.run_aws`` passes too.
    ``run_limited`` forwards kwargs untouched to ``subprocess.run`` and sets no
    PATH of its own, so the env captured here is the one the child gets.
    """
    captured: dict[str, object] = {}

    def fake_run_limited(argv, **kwargs):  # noqa: ANN001
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")

        class _P:
            returncode = 0
            stdout = "{}"
            stderr = ""

        return _P()

    # wrap_argv/cgroup_scope_argv touch the real sandbox; stub them to identity
    # so the test asserts only the env threading, like the cloud/aws test does.
    monkeypatch.setattr(engine, "wrap_argv", lambda argv, **kw: (list(argv), None))
    monkeypatch.setattr(engine, "cgroup_scope_argv", lambda argv: argv)
    monkeypatch.setattr(engine, "run_limited", fake_run_limited)
    # Resolve aws to an absolute head so aws_spawn_env is allowed to widen.
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    aws = pkg / "aws"
    aws.write_text("#!/bin/sh\n")
    aws.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path / "emptybin"))
    (tmp_path / "emptybin").mkdir()
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(pkg),))
    monkeypatch.setattr(engine, "_REFUSED_AWS_BIN_DIRS", set())
    from kiro_crew import github_runner

    monkeypatch.setattr(github_runner, "validate_provider_executable", lambda c: c)
    monkeypatch.setattr(github_runner, "validate_provider_directory", lambda candidate: candidate)

    rc, out, err = engine.run_aws(["sts", "get-caller-identity"], "kauai")

    assert rc == 0
    env = captured["env"]
    assert env is not None, "run_aws must pass env= so the child's PATH is widened"
    # The resolved absolute aws is the argv head; its install dir is on the env.
    assert str(pkg) in env["PATH"].split(os.pathsep)


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
