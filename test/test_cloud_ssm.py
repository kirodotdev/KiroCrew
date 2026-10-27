"""Unit tests for SSM primitives (cloud/ssm.py)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.cloud import aws, ssm
from kiro_crew.testing.wait import wait_until

#: How long a killed tree's descendant may take to leave the process table.
#: Well under the 30 s the stand-in descendants sleep, so a test that waited on
#: a descendant's natural exit instead of the kill fails.
_DESCENDANT_KILL_SECS = 5.0

#: The ownership fix ``_plugin_ownership_remedy`` offers for AWS's own tree.
_PKG_TREE_REMEDY = "sudo chown -R root:wheel /usr/local/sessionmanagerplugin"


def _pkg_tree_refusal() -> ssm.PluginRefusal:
    """A plugin refused inside AWS's install tree, with its known ownership fix."""
    return ssm.PluginRefusal(
        path="/usr/local/sessionmanagerplugin/bin/session-manager-plugin",
        reason="executable is owned by another user (uid 1501)",
        remedy_command=_PKG_TREE_REMEDY,
        code="other_owner",
    )


class TestArgvBuilders:
    @pytest.fixture(autouse=True)
    def _bare_resolver(self, monkeypatch):
        """Pin the shared resolver to the bare name so argv-shape assertions
        stay deterministic across hosts (with/without an installed CLI)."""
        monkeypatch.setattr(ssm, "resolve_aws_bin", lambda: "aws")

    def test_port_forward_argv(self):
        argv = ssm.build_port_forward_argv("i-0abc", 5476, 5599, "dev", "us-east-1")
        assert argv[:3] == ["aws", "ssm", "start-session"]
        assert "--target" in argv and "i-0abc" in argv
        assert "AWS-StartPortForwardingSession" in argv
        assert "portNumber=5476,localPortNumber=5599" in argv
        assert "--profile" in argv and "dev" in argv
        assert "--region" in argv and "us-east-1" in argv

    def test_port_forward_argv_no_profile(self):
        argv = ssm.build_port_forward_argv("i-0abc", 5476, 5599)
        assert "--profile" not in argv
        assert "--region" not in argv

    def test_ecs_argv_differs_from_ec2_only_in_the_target(self):
        """One argv serves both lanes, so the EC2 command cannot drift.

        The Fargate lane forwards into the task with the SAME
        ``AWS-StartPortForwardingSession`` document as the EC2 lane -- there is no
        per-lane document and no per-lane branch. Asserted as an element-wise
        comparison rather than by spot-checking membership: if a future change
        adds a Fargate-only flag, or swaps the document for the ToRemoteHost one
        (which takes a caller-chosen ``host`` and so can be pointed at a third
        machine), exactly one of these assertions fails.
        """
        task = "0123456789abcdef0123456789abcdef"
        ecs_target = f"ecs:mycluster_{task}_{task}-1234567890"

        ec2 = ssm.build_port_forward_argv("i-0abc", 5476, 5599, "dev", "us-east-1")
        ecs = ssm.build_port_forward_argv(ecs_target, 5476, 5599, "dev", "us-east-1")

        assert len(ec2) == len(ecs)
        differing = [i for i, (a, b) in enumerate(zip(ec2, ecs)) if a != b]
        assert differing == [ec2.index("--target") + 1]
        assert ecs[differing[0]] == ecs_target
        # The document is the plain one for both, and carries no `host` parameter.
        assert "AWS-StartPortForwardingSession" in ecs
        assert "AWS-StartPortForwardingSessionToRemoteHost" not in ecs
        assert not any(arg.startswith("host=") or ",host=" in arg for arg in ecs)
        assert "portNumber=5476,localPortNumber=5599" in ecs

    def test_argv_heads_resolved_absolutely_under_minimal_path(self, monkeypatch, tmp_path):
        """``build_port_forward_argv`` must resolve the CLI absolutely under a
        GUI-launched gateway's minimal PATH via the deploy engine's shared
        well-known-dirs resolver."""
        import os as _os

        if _os.name == "nt":
            pytest.skip("fallback install dirs are POSIX literals; dead on Windows by design")
        from kiro_crew import github_runner
        from kiro_crew.deploy import engine

        fake_aws = tmp_path / "aws"
        fake_aws.write_text("#!/bin/sh\n")
        fake_aws.chmod(0o755)
        empty_bin = tmp_path / "emptybin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", str(empty_bin))
        monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(tmp_path),))
        monkeypatch.setattr(github_runner, "validate_provider_executable", lambda c: c)
        # Undo this class's bare-name pin: this test exercises the real resolver.
        monkeypatch.setattr(ssm, "resolve_aws_bin", engine.resolve_aws_bin)

        pf = ssm.build_port_forward_argv("i-0abc", 5476, 5599, "dev", "us-east-1")
        assert pf[0] == str(fake_aws)
        assert pf[1:3] == ["ssm", "start-session"]


class TestOpenPortForward:
    def test_tunnel_output_is_devnull_not_pipe(self, monkeypatch):
        # The long-lived tunnel child must NOT use PIPE: no caller drains the
        # pipes (they block on wait()), so a filled OS pipe buffer would
        # silently freeze the tunnel mid-session.
        import subprocess

        captured: dict = {}

        def fake_popen(argv, **kwargs):
            captured.update(kwargs, argv=argv)
            return object()

        monkeypatch.setattr(ssm, "require_session_manager_plugin", lambda: None)
        monkeypatch.setattr(ssm.subprocess, "Popen", fake_popen)
        ssm.open_port_forward("i-0abc", 5476, 5599, "dev", "us-east-1")
        assert captured["stdout"] == subprocess.DEVNULL
        assert captured["stderr"] == subprocess.DEVNULL
        assert captured["start_new_session"] is True

    def test_capture_stdout_pipes_and_drains_into_a_bounded_buffer(self, monkeypatch):
        # The sibling cloud.connect path opts in to capture so a failed tunnel
        # can name its cause. stdout is PIPEd (stderr still DEVNULL) and a daemon
        # thread drains it for the child's whole life, so the pipe never fills.
        import io
        import subprocess

        captured: dict = {}
        notice = b"Exiting session with sessionId: user-0123456789abcdef.\n"

        class _Proc:
            def __init__(self):
                self.stdout = io.BytesIO(notice)

        def fake_popen(argv, **kwargs):
            captured.update(kwargs, argv=argv)
            return _Proc()

        monkeypatch.setattr(ssm, "require_session_manager_plugin", lambda: None)
        monkeypatch.setattr(ssm.subprocess, "Popen", fake_popen)
        proc = ssm.open_port_forward("i-0abc", 5476, 5599, "dev", "us-east-1", capture_stdout=True)
        assert captured["stdout"] == subprocess.PIPE
        assert captured["stderr"] == subprocess.DEVNULL
        # The drain thread exposes a shared buffer and lands the notice at EOF.
        # The fake stdout is a finite BytesIO, so the drain reaches EOF and the
        # thread ends on its own; join it to read the result without a sleep.
        assert hasattr(proc, "stdout_buf")
        proc.stdout_drain.join(timeout=5)
        assert not proc.stdout_drain.is_alive()
        assert "Exiting session" in proc.stdout_buf[0]

    def test_capture_buffer_is_capped(self, monkeypatch):
        import io

        big = b"x" * (ssm._MAX_FORWARD_STDOUT * 3)

        class _Proc:
            def __init__(self):
                self.stdout = io.BytesIO(big)

        monkeypatch.setattr(ssm, "require_session_manager_plugin", lambda: None)
        monkeypatch.setattr(ssm.subprocess, "Popen", lambda *a, **k: _Proc())
        proc = ssm.open_port_forward("i-0abc", 5476, 5599, "dev", "us-east-1", capture_stdout=True)
        proc.stdout_drain.join(timeout=5)
        assert not proc.stdout_drain.is_alive()
        assert len(proc.stdout_buf[0]) == ssm._MAX_FORWARD_STDOUT

    def test_child_env_can_find_the_session_manager_plugin(self, monkeypatch, tmp_path):
        """The child needs its OWN widened PATH, not just a resolved argv head.

        Resolving ``aws`` absolutely does not help the CLI find
        ``session-manager-plugin``: it looks that up by name against the child's
        inherited PATH at start-session time, which under a GUI-launched gateway
        is the minimal launchd one — so the tunnel died inside a correctly
        resolved ``aws``.
        """
        from kiro_crew.deploy import engine

        captured: dict = {}

        def fake_popen(argv, **kwargs):
            captured.update(kwargs, argv=argv)
            return object()

        # tmp_path stand-ins for the inherited minimal PATH and the plugin's real
        # install dir: a host literal would flake and is unrunnable on Windows.
        # `aws` sits on the inherited PATH so the head resolves absolutely (a PATH
        # hit needs no provenance check) and the widening is therefore offered.
        # Windows resolves executables by PATHEXT, not the exec bit, so the planted
        # file has to differ there or the head would fall back to the bare name and
        # the widening would (correctly) be withheld.
        inherited = tmp_path / "sysbin"
        inherited.mkdir()
        if os.name == "nt":
            fake_aws = inherited / "aws.cmd"
            fake_aws.write_text("@echo off\n")
            monkeypatch.setenv("PATHEXT", ".cmd")
        else:
            fake_aws = inherited / "aws"
            fake_aws.write_text("#!/bin/sh\n")
            fake_aws.chmod(0o755)
        install_dir = tmp_path / "install"
        monkeypatch.setenv("PATH", str(inherited))
        monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(install_dir),))
        monkeypatch.setattr(ssm, "require_session_manager_plugin", lambda: None)
        monkeypatch.setattr(ssm.subprocess, "Popen", fake_popen)

        ssm.open_port_forward("i-0abc", 5476, 5599, "dev", "us-east-1")

        assert captured["argv"][0] == str(fake_aws)  # absolute head
        child_path = captured["env"]["PATH"].split(os.pathsep)
        assert str(install_dir) in child_path  # the plugin's install dir
        assert child_path.index(str(inherited)) < child_path.index(str(install_dir))

    @pytest.mark.skipif(os.name == "nt", reason="provenance branch is dead on Windows")
    def test_refused_aws_binary_is_not_put_back_on_the_child_path(self, monkeypatch, tmp_path):
        """A provenance-REFUSED aws must not become reachable again via the env.

        The resolver falls back to the bare name exactly when it found a candidate
        in the install dirs and ``validate_provider_executable`` refused it, and
        that refusal is enforced ONLY by the bare name failing execvp against a
        PATH those dirs are absent from. Widening the child's PATH would put the
        refused binary back in execvp's reach and hand it AWS credentials — a
        fail-closed rejection silently turned into an execution.
        """
        from kiro_crew import github_runner
        from kiro_crew.deploy import engine

        captured: dict = {}

        def fake_popen(argv, **kwargs):
            captured.update(kwargs, argv=argv)
            return object()

        inherited = tmp_path / "sysbin"  # deliberately contains no aws
        inherited.mkdir()
        install_dir = tmp_path / "install"
        install_dir.mkdir()
        planted = install_dir / "aws"
        planted.write_text("#!/bin/sh\n")
        planted.chmod(0o755)
        monkeypatch.setenv("PATH", str(inherited))
        monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(install_dir),))

        def _refuse(_candidate):
            raise ValueError("planted shim")

        monkeypatch.setattr(github_runner, "validate_provider_executable", _refuse)
        monkeypatch.setattr(ssm, "require_session_manager_plugin", lambda: None)
        monkeypatch.setattr(ssm.subprocess, "Popen", fake_popen)

        ssm.open_port_forward("i-0abc", 5476, 5599, "dev", "us-east-1")

        assert captured["argv"][0] == "aws"  # refused -> bare name
        # The dir holding the refused binary must NOT be on the child's PATH.
        assert str(install_dir) not in captured["env"]["PATH"].split(os.pathsep)
        assert captured["env"]["PATH"] == str(inherited)


class TestSessionManagerPluginProbe:
    """The probe must agree with what the spawn actually does.

    Reported against a shipped desktop build: the plugin was installed at
    /usr/local/bin/session-manager-plugin and worked in a shell, but the
    launchd-launched gateway inherits /usr/bin:/bin:/usr/sbin:/sbin, so a bare
    shutil.which() missed it and every SSM tunnel was refused by the
    prerequisite gate before one was attempted. A symlink into the minimal PATH
    is not a workaround on macOS — those dirs are all SIP-restricted.
    """

    @pytest.mark.skipif(
        os.name == "nt",
        reason="provenance validation needs POSIX uid semantics; the fallback "
        "dir branch is dead on Windows by design",
    )
    def test_probe_finds_plugin_in_install_dir_under_minimal_path(self, monkeypatch, tmp_path):
        from kiro_crew.deploy import engine

        plugin = tmp_path / "session-manager-plugin"
        plugin.write_text("#!/bin/sh\n")
        plugin.chmod(0o755)
        empty_bin = tmp_path / "emptybin"
        empty_bin.mkdir()
        # A PATH that cannot see the plugin, standing in hermetically for the
        # minimal launchd one (a literal /usr/bin would flake on a host that has
        # the real plugin installed there).
        monkeypatch.setenv("PATH", str(empty_bin))
        monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(tmp_path),))

        from kiro_crew import github_runner

        monkeypatch.setattr(github_runner, "validate_provider_executable", lambda c: c)

        assert ssm.session_manager_plugin_installed() is True

    def test_probe_false_when_the_plugin_is_absent_everywhere(self, monkeypatch, tmp_path):
        """Genuinely missing still reports missing — the actionable install hint
        must not be traded away for the false-negative fix."""
        from kiro_crew.deploy import engine

        empty_bin = tmp_path / "emptybin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", str(empty_bin))
        monkeypatch.setattr(engine, "_AWS_BIN_DIRS", ())

        assert ssm.session_manager_plugin_installed() is False
        with pytest.raises(aws.AWSError):
            ssm.require_session_manager_plugin()

    def test_no_refusal_to_explain_when_the_plugin_is_absent(self, monkeypatch, tmp_path):
        from kiro_crew.deploy import engine

        empty_bin = tmp_path / "emptybin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", str(empty_bin))
        monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(tmp_path / "nothing-here"),))
        monkeypatch.setattr(engine, "_REFUSED_AWS_BIN_DIRS", set())

        assert ssm.session_manager_plugin_refusal() is None

    @pytest.mark.skipif(
        os.name == "nt",
        reason="provenance validation needs POSIX uid semantics; the fallback "
        "dir branch is dead on Windows by design",
    )
    def test_a_refused_plugin_reports_its_path_and_reason(self, monkeypatch, tmp_path):
        """Found-but-refused is not "not installed": the install command would put
        the same refused file back, so the probe names the file and the reason."""
        from kiro_crew import github_runner
        from kiro_crew.deploy import engine

        install = tmp_path / "brew"
        install.mkdir()
        plugin = install / "session-manager-plugin"
        plugin.write_text("#!/bin/sh\n")
        plugin.chmod(0o755)
        empty_bin = tmp_path / "emptybin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", str(empty_bin))
        monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(install),))
        monkeypatch.setattr(engine, "_REFUSED_AWS_BIN_DIRS", set())

        def _refuse(candidate):
            raise ValueError("executable parent is owned by another user (uid 1501)")

        monkeypatch.setattr(github_runner, "validate_provider_executable", _refuse)

        assert ssm.session_manager_plugin_installed() is False
        refusal = ssm.session_manager_plugin_refusal()
        assert refusal is not None
        assert refusal.path == str(plugin)
        assert refusal.reason == "executable parent is owned by another user (uid 1501)"
        # Outside AWS's package prefix no ownership command is known to fix it.
        assert refusal.remedy_command == ""

    @pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics")
    def test_a_refused_plugin_is_not_reported_as_not_installed_at_tunnel_start(
        self, monkeypatch, tmp_path
    ):
        """The tunnel gate and the installer name the refusal, not "not
        installed" -- reinstalling puts the same refused file back."""
        from kiro_crew import github_runner
        from kiro_crew.deploy import engine

        install = tmp_path / "pkgbin"
        install.mkdir()
        plugin = install / "session-manager-plugin"
        plugin.write_text("#!/bin/sh\n")
        plugin.chmod(0o755)
        empty_bin = tmp_path / "emptybin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", str(empty_bin))
        monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(install),))
        monkeypatch.setattr(engine, "_REFUSED_AWS_BIN_DIRS", set())
        reason = "executable is owned by another user (uid 1501)"

        def _refuse(candidate):
            raise ValueError(reason)

        monkeypatch.setattr(github_runner, "validate_provider_executable", _refuse)

        with pytest.raises(aws.AWSError) as excinfo:
            ssm.require_session_manager_plugin()
        assert "not installed" not in str(excinfo.value)
        assert str(plugin) in str(excinfo.value)
        assert reason in str(excinfo.value)

    def test_a_plugin_refused_in_aws_install_tree_is_not_reinstalled(self, monkeypatch):
        """AWS's installer rewrites the same file and keeps its ownership."""
        refusal = _pkg_tree_refusal()
        monkeypatch.setattr(ssm, "session_manager_plugin_installed", lambda: False)
        monkeypatch.setattr(ssm, "session_manager_plugin_refusal", lambda: refusal)

        def _no_install(*_a, **_k):
            raise AssertionError("a plugin refused in AWS's tree must not be reinstalled")

        monkeypatch.setattr(ssm, "_session_manager_plugin_install_plan", _no_install)

        assert ssm.reinstall_cannot_clear_refusal() is True
        result = ssm.install_session_manager_plugin()
        assert result.ok is False
        assert refusal.path in result.message
        assert refusal.remedy_command in result.message

    def test_a_refusal_with_no_known_fix_still_installs(self, monkeypatch):
        """A refused copy outside AWS's tree (a second account's Homebrew) can be
        shadowed by a fresh AWS install, so the installer still runs."""
        refusal = ssm.PluginRefusal(
            path="/opt/homebrew/bin/session-manager-plugin",
            reason="executable is owned by another user (uid 1501)",
            remedy_command="",
        )
        monkeypatch.setattr(ssm, "session_manager_plugin_installed", lambda: False)
        monkeypatch.setattr(ssm, "session_manager_plugin_refusal", lambda: refusal)
        planned: list = []

        def _plan(tmpdir):
            planned.append(tmpdir)
            return None

        monkeypatch.setattr(ssm, "_session_manager_plugin_install_plan", _plan)

        assert ssm.reinstall_cannot_clear_refusal() is False
        ssm.install_session_manager_plugin()
        assert planned

    def test_unavailable_message_carries_a_known_remedy(self, monkeypatch):
        refusal = _pkg_tree_refusal()
        monkeypatch.setattr(ssm, "session_manager_plugin_refusal", lambda: refusal)

        message = ssm.session_manager_plugin_unavailable_message()
        assert "not installed" not in message
        assert refusal.path in message
        assert refusal.remedy_command in message
        assert "reinstall it from AWS first" in message

    def test_unavailable_message_without_a_remedy_takes_the_per_code_next_step(self, monkeypatch):
        """A file another account owns, outside AWS's tree, gets the table's step
        (install AWS's copy), never the generic "fix the ownership" sentence."""
        refusal = ssm.PluginRefusal(
            path="/opt/homebrew/bin/session-manager-plugin",
            reason="executable is owned by another user (uid 1501)",
            remedy_command="",
            code="other_owner",
        )
        monkeypatch.setattr(ssm, "session_manager_plugin_refusal", lambda: refusal)

        message = ssm.session_manager_plugin_unavailable_message()
        assert message.endswith(ssm.REFUSAL_NEXT_STEP["other_owner"])
        assert ssm.REFUSAL_NEXT_STEP[""] not in message

    def test_unavailable_message_is_the_install_hint_when_absent(self, monkeypatch):
        monkeypatch.setattr(ssm, "session_manager_plugin_refusal", lambda: None)

        assert (
            ssm.session_manager_plugin_unavailable_message()
            == ssm.session_manager_plugin_install_hint()
        )

    @pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics")
    def test_an_accepted_plugin_has_no_refusal(self, monkeypatch, tmp_path):
        from kiro_crew import github_runner
        from kiro_crew.deploy import engine

        plugin = tmp_path / "session-manager-plugin"
        plugin.write_text("#!/bin/sh\n")
        plugin.chmod(0o755)
        empty_bin = tmp_path / "emptybin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", str(empty_bin))
        monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(tmp_path),))
        monkeypatch.setattr(engine, "_REFUSED_AWS_BIN_DIRS", set())
        monkeypatch.setattr(github_runner, "validate_provider_executable", lambda c: c)

        assert ssm.session_manager_plugin_refusal() is None

    @pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics")
    def test_aws_package_tree_owned_by_a_third_account_gets_the_chown_remedy(
        self, monkeypatch, tmp_path
    ):
        """AWS's macOS .pkg stamps its install tree with the packager's uid. The
        remedy for that exact state is making the tree root-owned."""
        prefix = tmp_path / "sessionmanagerplugin"
        (prefix / "bin").mkdir(parents=True)
        binary = prefix / "bin" / "session-manager-plugin"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        link_dir = tmp_path / "brew"
        link_dir.mkdir()
        link = link_dir / "session-manager-plugin"
        link.symlink_to(binary)
        monkeypatch.setattr(ssm, "_AWS_PKG_PLUGIN_PREFIX", str(prefix))
        # The files belong to the test user; a gateway running as a different
        # uid sees them as owned by a third account.
        monkeypatch.setattr(os, "geteuid", lambda: binary.stat().st_uid + 1)
        monkeypatch.setattr(ssm.platform, "system", lambda: "Darwin")

        reason = "executable parent is owned by another user (uid 1501)"
        assert ssm._plugin_ownership_remedy(str(link), reason) == (
            f"sudo chown -R root:wheel {prefix}"
        )

    @pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics")
    @pytest.mark.parametrize(
        "reason",
        [
            "provider execution is disabled for a root gateway",
            "executable is not root-owned",
            "executable parent is writable by the gateway user",
            "executable parent is world-writable",
            "executable hierarchy is not accessible",
        ],
    )
    def test_no_chown_remedy_for_a_refusal_ownership_cannot_clear(
        self, monkeypatch, tmp_path, reason
    ):
        """The tree is in the third-account state the command fixes, but the
        refusal is for something else, so the command would leave it refused."""
        prefix = tmp_path / "sessionmanagerplugin"
        (prefix / "bin").mkdir(parents=True)
        binary = prefix / "bin" / "session-manager-plugin"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        monkeypatch.setattr(ssm, "_AWS_PKG_PLUGIN_PREFIX", str(prefix))
        monkeypatch.setattr(os, "geteuid", lambda: binary.stat().st_uid + 1)

        assert ssm._plugin_ownership_remedy(str(binary), reason) == ""

    @staticmethod
    def _aws_package_install(monkeypatch, tmp_path) -> tuple[Path, Path]:
        """AWS's package layout with the plugin dir as the resolver's install dir."""
        from kiro_crew.deploy import engine

        prefix = tmp_path / "sessionmanagerplugin"
        (prefix / "bin").mkdir(parents=True)
        binary = prefix / "bin" / "session-manager-plugin"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        empty_bin = tmp_path / "emptybin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", str(empty_bin))
        monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(prefix / "bin"),))
        monkeypatch.setattr(engine, "_REFUSED_AWS_BIN_DIRS", set())
        monkeypatch.setattr(ssm, "_AWS_PKG_PLUGIN_PREFIX", str(prefix))
        monkeypatch.setattr(ssm.platform, "system", lambda: "Darwin")
        return prefix, binary

    @pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics")
    def test_the_real_check_third_account_refusal_gets_the_chown_remedy(
        self, monkeypatch, tmp_path
    ):
        """Runs the provenance check itself, so its ownership wording and the
        remedy's category match cannot drift apart."""
        from kiro_crew import github_runner

        prefix, binary = self._aws_package_install(monkeypatch, tmp_path)
        monkeypatch.setattr(github_runner, "strict_provider_bins", lambda: False)
        monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())
        monkeypatch.setattr(os, "geteuid", lambda: binary.stat().st_uid + 1)

        refusal = ssm.session_manager_plugin_refusal()
        assert refusal is not None
        assert "is owned by another user (uid " in refusal.reason
        assert refusal.remedy_command == f"sudo chown -R root:wheel {prefix}"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics")
    def test_the_real_check_root_gateway_refusal_gets_no_chown_remedy(self, monkeypatch, tmp_path):
        """A root gateway is refused before ownership is read; making the tree
        root-owned leaves that refusal in place, so no command is offered."""
        _prefix, binary = self._aws_package_install(monkeypatch, tmp_path)
        monkeypatch.setattr(os, "geteuid", lambda: 0)

        refusal = ssm.session_manager_plugin_refusal()
        assert refusal is not None
        assert refusal.reason == "provider execution is disabled for a root gateway"
        assert refusal.remedy_command == ""
        assert refusal.path == str(binary)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics")
    def test_the_real_check_strict_policy_refusal_gets_no_chown_remedy(self, monkeypatch, tmp_path):
        """The strict policy refuses on its own terms (root-owned AND not writable
        by the gateway user), so the third-account command is not offered."""
        from kiro_crew import github_runner

        _prefix, binary = self._aws_package_install(monkeypatch, tmp_path)
        monkeypatch.setattr(github_runner, "strict_provider_bins", lambda: True)
        monkeypatch.setattr(os, "geteuid", lambda: binary.stat().st_uid + 1)

        refusal = ssm.session_manager_plugin_refusal()
        assert refusal is not None
        assert "is owned by another user" not in refusal.reason
        assert refusal.remedy_command == ""

    @pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics")
    def test_the_real_check_third_account_refusal_carries_its_code_and_owner(
        self, monkeypatch, tmp_path
    ):
        """The code is read from the check's own wording, so these run the check:
        a wording change that broke the match fails here, not in a translation."""
        import pwd

        from kiro_crew import github_runner

        _prefix, binary = self._aws_package_install(monkeypatch, tmp_path)
        monkeypatch.setattr(github_runner, "strict_provider_bins", lambda: False)
        monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())
        owner_uid = binary.stat().st_uid
        monkeypatch.setattr(os, "geteuid", lambda: owner_uid + 1)

        refusal = ssm.session_manager_plugin_refusal()
        assert refusal is not None
        assert refusal.code == "other_owner"
        assert refusal.owner_uid == owner_uid
        # The owner is the account running the tests, named when this host knows it.
        try:
            expected_name = pwd.getpwuid(owner_uid).pw_name
        except KeyError:
            expected_name = ""
        assert refusal.owner == expected_name

    @pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics")
    def test_the_real_check_root_gateway_refusal_carries_its_code(self, monkeypatch, tmp_path):
        self._aws_package_install(monkeypatch, tmp_path)
        monkeypatch.setattr(os, "geteuid", lambda: 0)

        refusal = ssm.session_manager_plugin_refusal()
        assert refusal is not None
        assert (refusal.code, refusal.owner, refusal.owner_uid) == ("root_gateway", "", None)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics")
    def test_the_real_check_strict_policy_refusal_carries_its_code(self, monkeypatch, tmp_path):
        from kiro_crew import github_runner

        _prefix, binary = self._aws_package_install(monkeypatch, tmp_path)
        monkeypatch.setattr(github_runner, "strict_provider_bins", lambda: True)
        monkeypatch.setattr(os, "geteuid", lambda: binary.stat().st_uid + 1)

        refusal = ssm.session_manager_plugin_refusal()
        assert refusal is not None
        assert refusal.code == "strict_policy"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics")
    def test_the_real_check_world_writable_refusal_carries_its_code(self, monkeypatch, tmp_path):
        from kiro_crew import github_runner

        _prefix, binary = self._aws_package_install(monkeypatch, tmp_path)
        monkeypatch.setattr(github_runner, "strict_provider_bins", lambda: False)
        monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())
        binary.chmod(0o777)

        refusal = ssm.session_manager_plugin_refusal()
        assert refusal is not None
        assert refusal.reason == "executable is world-writable"
        assert refusal.code == "world_writable"

    @pytest.mark.parametrize(
        ("reason", "expected"),
        [
            ("executable is owned by another user (uid 502)", ("other_owner", 502)),
            ("executable parent is owned by another user (uid 1501)", ("other_owner", 1501)),
            ("executable parent is world-writable", ("world_writable", None)),
            ("provider execution is disabled for a root gateway", ("root_gateway", None)),
            ("executable is not root-owned", ("strict_policy", None)),
            ("executable parent is writable by the gateway user", ("strict_policy", None)),
            ("path must be canonical and contain no symlinks", ("strict_policy", None)),
            ("executable hierarchy is not accessible", ("", None)),
            ("executable is inside the agent-writable tree /work", ("", None)),
            # Matched whole: a reason that only contains a known one is not it.
            ("note: executable is world-writable", ("", None)),
            ("executable is owned by another user (uid 502) and more", ("", None)),
        ],
    )
    def test_refusal_reasons_map_to_stable_codes(self, reason, expected):
        assert ssm._classify_refusal(reason) == expected

    def test_an_owner_uid_with_no_account_has_no_name(self, monkeypatch):
        """AWS's macOS package stamps its tree with the packager's uid, which
        usually names no account on the user's Mac."""
        pwd = pytest.importorskip("pwd")

        def _missing(uid):
            raise KeyError(uid)

        monkeypatch.setattr(pwd, "getpwuid", _missing)
        assert ssm._account_name(1501) == ""

    @pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics")
    def test_a_refusal_with_the_chown_remedy_names_the_file_inside_that_folder(
        self, monkeypatch, tmp_path
    ):
        """The resolver finds the bin-directory link; the command targets the
        package folder. The row names the real file so both point at one place."""
        from kiro_crew import github_runner
        from kiro_crew.deploy import engine

        prefix = tmp_path / "sessionmanagerplugin"
        (prefix / "bin").mkdir(parents=True)
        binary = prefix / "bin" / "session-manager-plugin"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        link_dir = tmp_path / "brew"
        link_dir.mkdir()
        (link_dir / "session-manager-plugin").symlink_to(binary)
        empty_bin = tmp_path / "emptybin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", str(empty_bin))
        monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(link_dir),))
        monkeypatch.setattr(engine, "_REFUSED_AWS_BIN_DIRS", set())
        monkeypatch.setattr(ssm, "_AWS_PKG_PLUGIN_PREFIX", str(prefix))
        monkeypatch.setattr(os, "geteuid", lambda: binary.stat().st_uid + 1)
        monkeypatch.setattr(ssm.platform, "system", lambda: "Darwin")

        def _refuse(candidate):
            raise ValueError("executable is owned by another user (uid 1501)")

        monkeypatch.setattr(github_runner, "validate_provider_executable", _refuse)

        refusal = ssm.session_manager_plugin_refusal()
        assert refusal is not None
        assert refusal.remedy_command == f"sudo chown -R root:wheel {prefix}"
        assert refusal.path == os.path.realpath(binary)
        assert Path(refusal.path).is_relative_to(Path(os.path.realpath(prefix)))

    @pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics")
    def test_a_later_aws_copy_with_the_chown_remedy_is_reported_over_an_earlier_refusal(
        self, monkeypatch, tmp_path
    ):
        """A second account's Homebrew copy sorts first; once AWS's package is
        installed too, the row must name AWS's copy and its command, or the
        "install AWS's copy" step would leave the same row on screen."""
        from kiro_crew import github_runner
        from kiro_crew.deploy import engine

        prefix = tmp_path / "sessionmanagerplugin"
        (prefix / "bin").mkdir(parents=True)
        binary = prefix / "bin" / "session-manager-plugin"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        brew_dir = tmp_path / "opt-homebrew-bin"
        brew_dir.mkdir()
        brew_copy = brew_dir / "session-manager-plugin"
        brew_copy.write_text("#!/bin/sh\n")
        brew_copy.chmod(0o755)
        link_dir = tmp_path / "usr-local-bin"
        link_dir.mkdir()
        (link_dir / "session-manager-plugin").symlink_to(binary)
        empty_bin = tmp_path / "emptybin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", str(empty_bin))
        monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(brew_dir), str(link_dir)))
        monkeypatch.setattr(engine, "_REFUSED_AWS_BIN_DIRS", set())
        monkeypatch.setattr(ssm, "_AWS_PKG_PLUGIN_PREFIX", str(prefix))
        monkeypatch.setattr(os, "geteuid", lambda: binary.stat().st_uid + 1)
        monkeypatch.setattr(ssm.platform, "system", lambda: "Darwin")

        def _refuse(candidate):
            raise ValueError("executable is owned by another user (uid 1501)")

        monkeypatch.setattr(github_runner, "validate_provider_executable", _refuse)

        refusal = ssm.session_manager_plugin_refusal()
        assert refusal is not None
        assert refusal.remedy_command == f"sudo chown -R root:wheel {prefix}"
        assert refusal.path == os.path.realpath(binary)
        # With no fixable copy, the first refusal is still the one reported.
        (link_dir / "session-manager-plugin").unlink()
        refusal = ssm.session_manager_plugin_refusal()
        assert refusal is not None
        assert (refusal.path, refusal.remedy_command) == (str(brew_copy), "")

    @pytest.mark.skipif(os.name == "nt", reason="AWS's package prefix is a POSIX path")
    def test_the_package_prefix_is_where_aws_installs_the_plugin(self):
        assert ssm._AWS_PKG_PLUGIN_PREFIX == "/usr/local/sessionmanagerplugin"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics")
    def test_no_chown_remedy_when_the_package_tree_is_already_trusted(self, monkeypatch, tmp_path):
        prefix = tmp_path / "sessionmanagerplugin"
        (prefix / "bin").mkdir(parents=True)
        binary = prefix / "bin" / "session-manager-plugin"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        monkeypatch.setattr(ssm, "_AWS_PKG_PLUGIN_PREFIX", str(prefix))
        monkeypatch.setattr(os, "geteuid", lambda: binary.stat().st_uid)

        reason = "executable is owned by another user (uid 1501)"
        assert ssm._plugin_ownership_remedy(str(binary), reason) == ""
        # And a binary outside the package prefix never gets the command.
        other = tmp_path / "elsewhere"
        other.write_text("#!/bin/sh\n")
        monkeypatch.setattr(os, "geteuid", lambda: other.stat().st_uid + 1)
        assert ssm._plugin_ownership_remedy(str(other), reason) == ""

    def test_open_port_forward_refused_under_agent_session(self, monkeypatch):
        # The streaming tunnel bypasses run_aws, so it carries its own
        # human-action guard: an agent session must not open a tunnel.
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "sess-1")
        monkeypatch.setattr(
            ssm.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn tunnel")
        )
        with pytest.raises(ssm.aws.CloudActionDenied):
            ssm.open_port_forward("i-0abc", 5476, 5599, "dev", "us-east-1")


class TestKillPortForward:
    def test_kills_whole_group_incl_plugin_child(self, tmp_path):
        # The tunnel is spawned start_new_session=True, so the plugin child is in
        # the wrapper's group. kill_port_forward must reap the WHOLE tree — a
        # plain terminate() would leave the plugin (holding the forwarded port)
        # alive. The mechanism differs per platform (POSIX killpg vs. Windows
        # `taskkill /T`) but the invariant is the same: no descendant survives.
        import subprocess
        import sys

        pidfile = tmp_path / "child.pid"
        script = (
            "import subprocess,sys,time;"
            "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
            f"open({str(pidfile)!r},'w').write(str(c.pid));"
            "time.sleep(30)"
        )
        # Group isolation the platform way: bare `start_new_session=True` is a no-op on
        # Windows, and `taskkill /T` (the Windows branch of kill_port_forward) needs the
        # wrapper to own a real process group for the grandchild to be in its tree.
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            # A child inherits pytest's CWD (the checkout) unless told otherwise.
            cwd=tmp_path,
            start_new_session=platform_compat.IS_POSIX,
            **(
                {}
                if platform_compat.IS_POSIX
                else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            ),
        )
        wait_until(
            lambda: pidfile.exists() and pidfile.read_text(encoding="utf-8").strip(),
            describe=lambda: f"{pidfile} was never written",
        )
        child_pid = int(pidfile.read_text(encoding="utf-8").strip())

        # `platform_compat.pid_exists`, NOT `os.kill(pid, 0)`: on Windows signal 0
        # is signal.CTRL_C_EVENT, which CPython routes to
        # GenerateConsoleCtrlEvent — a console-group signal whose return value
        # says nothing about whether pid exists. The shim uses OpenProcess +
        # GetExitCodeProcess there and os.kill(pid, 0) on POSIX.
        _alive = platform_compat.pid_exists

        assert _alive(child_pid)
        ssm.kill_port_forward(proc)
        assert proc.poll() is not None
        # Bounded well under the child's own 30 s sleep, so its natural exit can
        # never stand in for the kill.
        wait_until(
            lambda: not _alive(child_pid),
            timeout=_DESCENDANT_KILL_SECS,
            describe=lambda: "plugin child (same tree) must be killed",
        )

    def test_escalates_to_kill_when_terminate_does_not_reap(self, monkeypatch):
        """The SIGKILL escalation must actually run — on Windows too.

        Windows defines no ``signal.SIGKILL``, so naming it as the escalation's
        call argument raises AttributeError *inside* the handler's own
        ``try``/``except Exception``, which swallows it and skips ``proc.kill()``
        entirely. Windows is the platform that reaches this code (via the
        taskkill fall-through), so the escalation silently became a no-op exactly
        where it was needed and the plugin kept the forwarded port bound. Signal
        numbers therefore come from ``platform_compat``, which defines both on
        every platform. Runs on any platform: taskkill is stubbed to fail so the
        Windows branch falls through, and the group signal is stubbed to fail so
        the per-process terminate/kill escalation is what gets exercised.
        """
        import subprocess as sp

        called = {"terminate": False, "kill": False}

        class Stubborn:
            """terminate() never reaps it; only .kill() would."""

            # A pid that is NOT this process: the escalation under test is the
            # per-process one, so `killpg` must never actually fire (see the
            # getpgid stub below).
            pid = 4321

            def poll(self):
                return None

            def terminate(self):
                called["terminate"] = True

            def kill(self):
                called["kill"] = True

            def wait(self, timeout=None):
                raise sp.TimeoutExpired("aws", timeout or 5)

        # taskkill unavailable -> the Windows branch falls through to escalation.
        monkeypatch.setattr(
            ssm.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no taskkill"))
        )
        # Force the group signal to fail so the per-process path is what runs.
        # Mandatory on POSIX, where `os.killpg`/`os.getpgid` are REAL: pid 4321
        # may well belong to an unrelated live process, and the group lookup
        # would then succeed and SIGKILL a stranger's process group instead of
        # reaching the `proc.kill()` this test asserts on.
        monkeypatch.setattr(
            ssm.os,
            "getpgid",
            lambda pid: (_ for _ in ()).throw(ProcessLookupError()),
            raising=False,
        )
        monkeypatch.setattr(
            ssm.os,
            "killpg",
            lambda pgid, sig: (_ for _ in ()).throw(ProcessLookupError()),
            raising=False,
        )
        ssm.kill_port_forward(Stubborn())

        assert called["terminate"], "graceful terminate should be attempted first"
        assert called["kill"], "escalation must reach proc.kill(), else the port stays bound"

    def test_none_and_dead_are_noops(self):
        ssm.kill_port_forward(None)

        class Dead:
            def poll(self):
                return 0

        ssm.kill_port_forward(Dead())


class TestPortChecks:
    def test_port_is_free_true_when_nothing_listening(self, monkeypatch):
        import socket

        class FakeSock:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def settimeout(self, _t):
                pass

            def connect_ex(self, _addr):
                return 111  # ECONNREFUSED -> nothing listening

        monkeypatch.setattr(socket, "socket", lambda *a, **k: FakeSock())
        assert ssm.port_is_free(5599) is True

    def test_port_is_free_false_when_occupied(self, monkeypatch):
        import socket

        class FakeSock:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def settimeout(self, _t):
                pass

            def connect_ex(self, _addr):
                return 0  # accepted -> something is already listening

        monkeypatch.setattr(socket, "socket", lambda *a, **k: FakeSock())
        assert ssm.port_is_free(5599) is False

    def test_wait_for_local_port_bails_when_child_exited(self, monkeypatch):
        # If the SSM child has died, a listener on that port is NOT our tunnel,
        # so the wait must return False without ever probing the socket.
        monkeypatch.setattr(ssm, "_sleep", lambda *_a: None)

        class DeadProc:
            def poll(self):
                return 1  # already exited

        import socket

        def _boom(*a, **k):  # pragma: no cover - must not be called
            raise AssertionError("must not probe the socket once the child is dead")

        monkeypatch.setattr(socket, "socket", _boom)
        assert ssm.wait_for_local_port(5599, proc=DeadProc()) is False


class TestSessionManagerPlugin:
    def test_require_session_manager_plugin_raises_when_missing(self, monkeypatch):
        monkeypatch.setattr(ssm, "session_manager_plugin_installed", lambda: False)
        with pytest.raises(aws.AWSError, match="session-manager-plugin"):
            ssm.require_session_manager_plugin()

    def test_install_session_manager_plugin_downloads_and_runs_plan(self, monkeypatch):
        installed = iter([False, True])
        downloaded: list[tuple[str, Path]] = []
        commands: list[list[str]] = []

        monkeypatch.setattr(ssm, "session_manager_plugin_installed", lambda: next(installed))
        monkeypatch.setattr(
            ssm,
            "_session_manager_plugin_install_plan",
            lambda tmpdir: (
                "https://example.com/session-manager-plugin.deb",
                tmpdir / "session-manager-plugin.deb",
                [["sudo", "dpkg", "-i", str(tmpdir / "session-manager-plugin.deb")]],
            ),
        )
        monkeypatch.setattr(ssm, "_download_file", lambda url, dest: downloaded.append((url, dest)))
        monkeypatch.setattr(
            ssm, "_run_install_command", lambda argv: commands.append(argv) or (0, "", "")
        )

        result = ssm.install_session_manager_plugin()

        assert result.ok is True
        assert downloaded[0][0] == "https://example.com/session-manager-plugin.deb"
        assert commands == [["sudo", "dpkg", "-i", str(downloaded[0][1])]]

    def test_install_plan_uses_macos_arm64_pkg(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ssm.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(ssm.platform, "machine", lambda: "arm64")

        plan = ssm._session_manager_plugin_install_plan(tmp_path)

        assert plan is not None
        url, package_path, commands = plan
        assert "mac_arm64/session-manager-plugin.pkg" in url
        assert package_path.name == "session-manager-plugin.pkg"
        assert commands[0][:3] == ["sudo", "installer", "-pkg"]

    def test_install_plan_uses_ubuntu_arm64_deb(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ssm.platform, "system", lambda: "Linux")
        monkeypatch.setattr(ssm.platform, "machine", lambda: "aarch64")
        monkeypatch.setattr(
            ssm.shutil, "which", lambda name: "/usr/bin/dpkg" if name == "dpkg" else None
        )

        plan = ssm._session_manager_plugin_install_plan(tmp_path)

        assert plan is not None
        url, package_path, commands = plan
        assert "ubuntu_arm64/session-manager-plugin.deb" in url
        assert package_path.name == "session-manager-plugin.deb"
        assert commands == [["sudo", "dpkg", "-i", str(package_path)]]


class TestRunCommand:
    def test_success(self, monkeypatch):
        monkeypatch.setattr(ssm, "_sleep", lambda *_a: None)

        def fake_json(args, profile="", region="", *, action, timeout=aws.DEFAULT_TIMEOUT):
            return {"Command": {"CommandId": "cmd-1"}}

        def fake_run(args, profile="", region="", *, timeout=aws.DEFAULT_TIMEOUT):
            inv = {
                "Status": "Success",
                "StandardOutputContent": "hello",
                "StandardErrorContent": "",
                "ResponseCode": 0,
            }
            return (0, json.dumps(inv), "")

        monkeypatch.setattr(aws, "checked_json", fake_json)
        monkeypatch.setattr(aws, "run_aws", fake_run)
        res = ssm.run_command("i-0abc", "echo hello", "dev", "us-east-1")
        assert res.ok is True
        assert res.stdout == "hello"

    def test_failed_exit_code(self, monkeypatch):
        monkeypatch.setattr(ssm, "_sleep", lambda *_a: None)
        monkeypatch.setattr(aws, "checked_json", lambda *a, **k: {"Command": {"CommandId": "c"}})

        def fake_run(args, profile="", region="", *, timeout=aws.DEFAULT_TIMEOUT):
            inv = {
                "Status": "Failed",
                "StandardOutputContent": "",
                "StandardErrorContent": "boom",
                "ResponseCode": 2,
            }
            return (0, json.dumps(inv), "")

        monkeypatch.setattr(aws, "run_aws", fake_run)
        res = ssm.run_command("i-0abc", "false", "dev")
        assert res.ok is False
        assert res.status == "Failed"
        assert res.exit_code == 2

    def test_no_command_id_raises(self, monkeypatch):
        monkeypatch.setattr(aws, "checked_json", lambda *a, **k: {"Command": {}})
        with pytest.raises(aws.AWSError, match="CommandId"):
            ssm.run_command("i-0abc", "echo", "dev")

    def test_invalid_run_as_rejected(self, monkeypatch):
        # run_as is interpolated into `sudo -u <run_as>`; a bad value must be
        # rejected before it reaches the command string.
        monkeypatch.setattr(aws, "checked_json", lambda *a, **k: pytest.fail("must reject first"))
        with pytest.raises(aws.AWSError, match="run_as"):
            ssm.run_command("i-0abc", "echo hi", "dev", run_as="root; rm -rf /")

    def test_default_run_as_ok(self):
        assert ssm._USERNAME_RE.match("ec2-user")
        assert not ssm._USERNAME_RE.match("bad user")
        assert not ssm._USERNAME_RE.match("-leadingdash")

    def test_run_as_rejects_trailing_newline(self, monkeypatch):
        """A trailing newline must not reach the remote command string.

        ``run_as`` is interpolated into the single-line string
        ``_wrap_remote_command`` builds (``… | sudo -u {run_as} -i bash``), where a
        newline ends that line and begins a second shell statement. The pattern
        was anchored with ``$``, which also matches just before a trailing
        newline, so ``"ec2-user\\n"`` passed this check; it is now ``\\Z``.

        No caller threads user input into ``run_as`` today -- every one passes the
        default or a registry value ``validate_ssm_run_as`` has already bounded --
        so this was latent rather than exploitable. It is pinned because
        :func:`run_command`'s docstring promises the surface stays closed if a
        caller ever does, and under ``$`` that promise did not hold.
        """
        assert not ssm._USERNAME_RE.match("ec2-user\n")
        monkeypatch.setattr(aws, "checked_json", lambda *a, **k: pytest.fail("must reject first"))
        with pytest.raises(aws.AWSError, match="run_as"):
            ssm.run_command("i-0abc", "echo hi", "dev", run_as="ec2-user\n")

    def test_wrapped_command_never_contains_a_newline_from_run_as(self):
        """The property the anchor protects, asserted on the built string itself."""
        wrapped = ssm._wrap_remote_command("echo hi", "ec2-user")
        assert "\n" not in wrapped


class TestManaged:
    def test_online(self, monkeypatch):
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (0, "Online\n", ""))
        assert ssm.instance_is_managed("i-0abc", "dev") is True

    def test_not_online(self, monkeypatch):
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (0, "ConnectionLost\n", ""))
        assert ssm.instance_is_managed("i-0abc", "dev") is False


class TestShellQuote:
    def test_json_str_list(self):
        assert ssm._json_str_list(["a", "b"]) == '["a", "b"]'


class TestWrapRemoteCommand:
    def test_multiline_script_is_base64_single_line(self):
        # SSM strips newlines from multi-line commands; base64-wrapping keeps
        # the script intact and produces a single line SSM can't mangle.
        import base64

        script = "set -e\necho one\necho two\nexit 0"
        wrapped = ssm._wrap_remote_command(script, "ec2-user")
        assert "\n" not in wrapped
        assert "base64 -d" in wrapped
        assert "sudo -u ec2-user -i bash" in wrapped
        # The embedded payload decodes back to the exact original script.
        token = wrapped.split()[1]
        assert base64.b64decode(token).decode() == script


class TestAwsCliRefusal:
    """The ``aws`` CLI's found-but-refused reading, the plugin's twin."""

    @staticmethod
    def _install(monkeypatch, tmp_path) -> Path:
        """An ``aws`` reachable only through the resolver's install dirs."""
        from kiro_crew.deploy import engine

        install = tmp_path / "sessionmanagerplugin" / "bin"
        install.mkdir(parents=True)
        cli = install / "aws"
        cli.write_text("#!/bin/sh\n")
        cli.chmod(0o755)
        empty_bin = tmp_path / "emptybin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", str(empty_bin))
        monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(install),))
        monkeypatch.setattr(engine, "_REFUSED_AWS_BIN_DIRS", set())
        # Even inside the tree the plugin's ownership command targets, no command
        # is offered for the CLI: none is known to clear its refusal.
        monkeypatch.setattr(ssm, "_AWS_PKG_PLUGIN_PREFIX", str(tmp_path / "sessionmanagerplugin"))
        return cli

    def test_no_refusal_when_the_cli_is_absent(self, monkeypatch, tmp_path):
        from kiro_crew.deploy import engine

        empty_bin = tmp_path / "emptybin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", str(empty_bin))
        monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(tmp_path / "nothing-here"),))
        monkeypatch.setattr(engine, "_REFUSED_AWS_BIN_DIRS", set())

        assert ssm.aws_cli_refusal() is None

    @pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics")
    def test_an_accepted_cli_has_no_refusal(self, monkeypatch, tmp_path):
        from kiro_crew import github_runner

        self._install(monkeypatch, tmp_path)
        monkeypatch.setattr(github_runner, "validate_provider_executable", lambda c: c)

        assert ssm.aws_cli_refusal() is None

    @pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics")
    def test_the_real_check_third_account_refusal_names_the_cli_with_no_command(
        self, monkeypatch, tmp_path
    ):
        from kiro_crew import github_runner

        cli = self._install(monkeypatch, tmp_path)
        monkeypatch.setattr(github_runner, "strict_provider_bins", lambda: False)
        monkeypatch.setattr(github_runner, "agent_writable_roots", lambda: ())
        monkeypatch.setattr(os, "geteuid", lambda: cli.stat().st_uid + 1)

        refusal = ssm.aws_cli_refusal()
        assert refusal is not None
        assert refusal.path == str(cli)
        assert "is owned by another user (uid " in refusal.reason
        assert refusal.code == "other_owner"
        assert refusal.owner_uid == cli.stat().st_uid
        assert refusal.remedy_command == ""

    @pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics")
    def test_the_real_check_root_gateway_refusal_carries_its_code(self, monkeypatch, tmp_path):
        cli = self._install(monkeypatch, tmp_path)
        monkeypatch.setattr(os, "geteuid", lambda: 0)

        refusal = ssm.aws_cli_refusal()
        assert refusal is not None
        assert refusal.path == str(cli)
        assert refusal.code == "root_gateway"
        assert refusal.remedy_command == ""
