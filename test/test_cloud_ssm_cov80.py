"""Coverage for the SSM plugin-install plan and the polling/liveness edge paths.

The local ``session-manager-plugin`` prerequisite is resolved per host — the
one-liner hint, the download plan, and the installer sequence all branch on
platform and available package manager, and every one of those branches is a
place where a wrong answer silently blocks tunnels. Also pinned: ``run_command``'s
tolerance of a mid-poll unparseable invocation, and ``wait_for_local_port``'s
refusal to accept a listener that appeared after the SSM child died.
"""

from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import pytest

from kiro_crew.cloud import aws, ssm


def _stub_bin(name: str) -> str:
    """An absolute ``which``-style stub path that is valid on every platform.

    Absolute because the PATH-hijack guards refuse a relative binary path, and with a
    directory component because some call sites take ``Path(x).name`` -- a bare name
    would make that assertion vacuous. Rooted at ``tempfile.gettempdir()``, the
    portable root the cross-platform gate recommends, rather than a ``/usr/bin``
    literal that does not exist on Windows. Nothing is created or executed here.
    """
    return str(Path(tempfile.gettempdir()) / "stub-bin" / name)


@pytest.fixture
def fake_platform(monkeypatch):
    """Pin platform.system()/machine() and a controllable ``which`` allowlist."""

    def _apply(system: str, machine: str = "x86_64", available: tuple[str, ...] = ()) -> None:
        monkeypatch.setattr(ssm.platform, "system", lambda: system)
        monkeypatch.setattr(ssm.platform, "machine", lambda: machine)
        monkeypatch.setattr(
            ssm.shutil,
            "which",
            lambda name: _stub_bin(name) if name in available else None,
        )

    return _apply


class TestInstallCommandHint:
    def test_macos_prefers_brew_when_present(self, fake_platform) -> None:
        fake_platform("Darwin", "arm64", available=("brew",))
        assert ssm.session_manager_plugin_install_command() == (
            "brew install --cask session-manager-plugin"
        )

    def test_macos_without_brew_downloads_the_pkg_into_a_private_dir(self, fake_platform) -> None:
        fake_platform("Darwin", "arm64")
        cmd = ssm.session_manager_plugin_install_command()
        assert "mac_arm64/session-manager-plugin.pkg" in cmd
        assert 'd="$(mktemp -d)"' in cmd
        assert '"$d/session-manager-plugin.pkg"' in cmd

    def test_macos_intel_uses_the_non_arm_package(self, fake_platform) -> None:
        fake_platform("Darwin", "x86_64")
        assert "latest/mac/session-manager-plugin.pkg" in (
            ssm.session_manager_plugin_install_command()
        )

    def test_debian_host_gets_a_deb_one_liner(self, fake_platform) -> None:
        fake_platform("Linux", "x86_64", available=("dpkg",))
        cmd = ssm.session_manager_plugin_install_command()
        assert "ubuntu_64bit/session-manager-plugin.deb" in cmd
        assert 'd="$(mktemp -d)"' in cmd

    def test_debian_arm_host_gets_the_arm_deb(self, fake_platform) -> None:
        fake_platform("Linux", "aarch64", available=("dpkg",))
        assert "ubuntu_arm64/session-manager-plugin.deb" in (
            ssm.session_manager_plugin_install_command()
        )

    def test_rpm_host_installs_straight_from_the_url(self, fake_platform) -> None:
        fake_platform("Linux", "x86_64", available=("dnf",))
        cmd = ssm.session_manager_plugin_install_command()
        assert cmd.startswith("sudo dnf install -y ")
        assert "linux_64bit/session-manager-plugin.rpm" in cmd

    def test_linux_without_a_package_manager_has_no_one_liner(self, fake_platform) -> None:
        fake_platform("Linux", "x86_64")
        assert ssm.session_manager_plugin_install_command() == ""

    def test_unsupported_platform_has_no_one_liner(self, fake_platform) -> None:
        fake_platform("Windows", "x86_64", available=("dpkg", "brew"))
        assert ssm.session_manager_plugin_install_command() == ""


class TestInstallPlan:
    def test_macos_plan_symlinks_into_usr_local_bin(self, fake_platform, tmp_path) -> None:
        fake_platform("Darwin", "arm64")
        plan = ssm._session_manager_plugin_install_plan(tmp_path)
        assert plan is not None
        url, package_path, commands = plan
        assert url.startswith("https://s3.amazonaws.com/")
        assert package_path == tmp_path / "session-manager-plugin.pkg"
        assert commands[0][:2] == ["sudo", "installer"]
        assert commands[-1][:2] == ["sudo", "ln"]

    def test_non_posix_platform_has_no_plan(self, fake_platform, tmp_path) -> None:
        fake_platform("Windows", "x86_64")
        assert ssm._session_manager_plugin_install_plan(tmp_path) is None

    def test_dpkg_plan_installs_the_deb(self, fake_platform, tmp_path) -> None:
        fake_platform("Linux", "x86_64", available=("dpkg",))
        plan = ssm._session_manager_plugin_install_plan(tmp_path)
        assert plan is not None
        _url, package_path, commands = plan
        assert commands == [["sudo", "dpkg", "-i", str(package_path)]]

    def test_rpm_plan_prefers_a_resolver_when_available(self, fake_platform, tmp_path) -> None:
        fake_platform("Linux", "aarch64", available=("rpm", "yum"))
        plan = ssm._session_manager_plugin_install_plan(tmp_path)
        assert plan is not None
        url, package_path, commands = plan
        assert "linux_arm64" in url
        assert commands == [["sudo", _stub_bin("yum"), "install", "-y", str(package_path)]]

    def test_rpm_plan_falls_back_to_rpm_upgrade(self, fake_platform, tmp_path) -> None:
        fake_platform("Linux", "x86_64", available=("rpm",))
        plan = ssm._session_manager_plugin_install_plan(tmp_path)
        assert plan is not None
        _url, package_path, commands = plan
        assert commands == [["sudo", "rpm", "-Uvh", str(package_path)]]

    def test_linux_without_any_package_tool_has_no_plan(self, fake_platform, tmp_path) -> None:
        fake_platform("Linux", "x86_64")
        assert ssm._session_manager_plugin_install_plan(tmp_path) is None


class TestInstallSessionManagerPlugin:
    def test_already_installed_short_circuits(self, monkeypatch) -> None:
        monkeypatch.setattr(ssm, "session_manager_plugin_installed", lambda: True)
        result = ssm.install_session_manager_plugin()
        assert result.ok is True
        assert "already installed" in result.message

    def test_unplannable_platform_returns_the_doc_hint(self, monkeypatch) -> None:
        monkeypatch.setattr(ssm, "session_manager_plugin_installed", lambda: False)
        monkeypatch.setattr(ssm, "_session_manager_plugin_install_plan", lambda _tmp: None)
        result = ssm.install_session_manager_plugin()
        assert result.ok is False
        assert "session-manager-working-with-install-plugin" in result.message

    def test_download_failure_is_reported(self, monkeypatch) -> None:
        monkeypatch.setattr(ssm, "session_manager_plugin_installed", lambda: False)
        monkeypatch.setattr(
            ssm,
            "_session_manager_plugin_install_plan",
            lambda tmp: ("https://example.invalid/p.deb", tmp / "p.deb", []),
        )

        def _boom(_url, _dest):
            raise OSError("network unreachable")

        monkeypatch.setattr(ssm, "_download_file", _boom)
        result = ssm.install_session_manager_plugin()
        assert result.ok is False
        assert "Could not download" in result.message

    def test_failed_install_command_is_reported_with_its_argv(self, monkeypatch) -> None:
        monkeypatch.setattr(ssm, "session_manager_plugin_installed", lambda: False)
        monkeypatch.setattr(
            ssm,
            "_session_manager_plugin_install_plan",
            lambda tmp: (
                "https://example.invalid/p.deb",
                tmp / "p.deb",
                [["sudo", "dpkg", "-i", "p.deb"]],
            ),
        )
        monkeypatch.setattr(ssm, "_download_file", lambda _u, _d: None)
        monkeypatch.setattr(ssm, "_run_install_command", lambda argv: (1, "", "dpkg: refused"))
        result = ssm.install_session_manager_plugin()
        assert result.ok is False
        assert "sudo dpkg -i p.deb" in result.message
        assert "dpkg: refused" in result.message

    def test_installer_that_leaves_no_binary_is_not_a_success(self, monkeypatch) -> None:
        monkeypatch.setattr(ssm, "session_manager_plugin_installed", lambda: False)
        monkeypatch.setattr(
            ssm,
            "_session_manager_plugin_install_plan",
            lambda tmp: ("https://example.invalid/p.deb", tmp / "p.deb", []),
        )
        monkeypatch.setattr(ssm, "_download_file", lambda _u, _d: None)
        result = ssm.install_session_manager_plugin()
        assert result.ok is False
        assert "was not found on PATH" in result.message

    def test_successful_install_reports_ok(self, monkeypatch) -> None:
        states = iter([False, True])
        monkeypatch.setattr(ssm, "session_manager_plugin_installed", lambda: next(states))
        monkeypatch.setattr(
            ssm,
            "_session_manager_plugin_install_plan",
            lambda tmp: ("https://example.invalid/p.deb", tmp / "p.deb", [["sudo", "true"]]),
        )
        monkeypatch.setattr(ssm, "_download_file", lambda _u, _d: None)
        monkeypatch.setattr(ssm, "_run_install_command", lambda argv: (0, "", ""))
        result = ssm.install_session_manager_plugin()
        assert result.ok is True
        assert result.message == "session-manager-plugin installed"


class TestPluginPrerequisite:
    def test_require_raises_with_the_install_hint(self, monkeypatch) -> None:
        monkeypatch.setattr(ssm, "session_manager_plugin_installed", lambda: False)
        with pytest.raises(aws.AWSError) as exc:
            ssm.require_session_manager_plugin()
        assert "session-manager-plugin is not installed" in str(exc.value)

    def test_require_is_silent_when_present(self, monkeypatch) -> None:
        monkeypatch.setattr(ssm, "session_manager_plugin_installed", lambda: True)
        assert ssm.require_session_manager_plugin() is None

    def test_installed_probe_uses_path_lookup(self, monkeypatch) -> None:
        monkeypatch.setattr(ssm.shutil, "which", lambda name: None)
        assert ssm.session_manager_plugin_installed() is False
        monkeypatch.setattr(ssm.shutil, "which", lambda name: _stub_bin(name))
        assert ssm.session_manager_plugin_installed() is True


class TestSmallHelpers:
    def test_download_refuses_non_https(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="non-HTTPS"):
            ssm._download_file("http://example.invalid/p.deb", tmp_path / "p.deb")

    def test_download_streams_the_body_to_disk(self, monkeypatch, tmp_path) -> None:
        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_exc) -> None:
                return None

        monkeypatch.setattr(
            urllib.request, "urlopen", lambda url, timeout=0: _Resp(b"package-bytes")
        )
        dest = tmp_path / "p.deb"
        ssm._download_file("https://example.invalid/p.deb", dest)
        assert dest.read_bytes() == b"package-bytes"

    def test_missing_installer_binary_reports_127(self, monkeypatch) -> None:
        def _boom(argv, check=False):
            raise FileNotFoundError("no dpkg here")

        monkeypatch.setattr(ssm.subprocess, "run", _boom)
        rc, out, err = ssm._run_install_command(["dpkg", "-i", "p.deb"])
        assert (rc, out) == (127, "")
        assert "no dpkg here" in err

    def test_install_command_returncode_is_passed_through(self, monkeypatch) -> None:
        class _Proc:
            returncode = 4

        monkeypatch.setattr(ssm.subprocess, "run", lambda argv, check=False: _Proc())
        assert ssm._run_install_command(["sudo", "true"]) == (4, "", "")

    def test_install_error_uses_the_last_stderr_line(self) -> None:
        msg = ssm._install_error(["sudo", "dpkg"], "warning\nreal failure\n")
        assert msg == "`sudo dpkg` failed: real failure"

    def test_install_error_falls_back_without_stderr(self) -> None:
        assert "installer returned non-zero" in ssm._install_error(["sudo", "dpkg"], "   ")

    def test_json_str_list_renders_a_json_array(self) -> None:
        assert ssm._json_str_list(["echo 'hi'"]) == "[\"echo 'hi'\"]"

    def test_shell_quoting_escapes_embedded_quotes(self) -> None:
        assert ssm._shq("it's") == "'it'\\''s'"

    def test_normalized_arch_maps_aarch64_to_arm64(self, monkeypatch) -> None:
        monkeypatch.setattr(ssm.platform, "machine", lambda: "AArch64")
        assert ssm._normalized_arch() == "arm64"
        monkeypatch.setattr(ssm.platform, "machine", lambda: "AMD64")
        assert ssm._normalized_arch() == "x86_64"


class _FakeSocket:
    """Fake TCP socket whose connect_ex outcome is scripted."""

    def __init__(self, results: list[int]) -> None:
        self._results = results
        self.timeouts: list[float] = []

    def __call__(self, *_a, **_k) -> "_FakeSocket":
        return self

    def __enter__(self) -> "_FakeSocket":
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def settimeout(self, secs: float) -> None:
        self.timeouts.append(secs)

    def connect_ex(self, _addr) -> int:
        return self._results.pop(0) if self._results else 1


class TestPortProbes:
    def test_occupied_port_is_not_free(self, monkeypatch) -> None:
        import socket

        monkeypatch.setattr(socket, "socket", _FakeSocket([0]))
        assert ssm.port_is_free(5599) is False

    def test_unused_port_is_free(self, monkeypatch) -> None:
        import socket

        monkeypatch.setattr(socket, "socket", _FakeSocket([111]))
        assert ssm.port_is_free(5599) is True

    def test_wait_returns_true_once_the_tunnel_accepts(self, monkeypatch) -> None:
        import socket

        monkeypatch.setattr(socket, "socket", _FakeSocket([111, 0]))
        monkeypatch.setattr(ssm, "_sleep", lambda _s: None)
        assert ssm.wait_for_local_port(5599, timeout=5.0) is True

    def test_wait_gives_up_after_the_timeout(self, monkeypatch) -> None:
        import socket

        naps: list[float] = []
        monkeypatch.setattr(socket, "socket", _FakeSocket([111, 111]))
        monkeypatch.setattr(ssm, "_sleep", lambda secs: naps.append(secs))
        assert ssm.wait_for_local_port(5599, timeout=1.0) is False
        assert naps == [0.5, 0.5]

    def test_wait_refuses_a_listener_that_outlived_the_ssm_child(self, monkeypatch) -> None:
        import socket

        class _Dead:
            def poll(self) -> int:
                return 1

        monkeypatch.setattr(
            socket, "socket", lambda *a, **k: pytest.fail("probed after the child exited")
        )
        assert ssm.wait_for_local_port(5599, timeout=5.0, proc=_Dead()) is False


class _FakeProc:
    def __init__(self, *, pid: int = 4242, poll: int | None = None, wait_exc=None) -> None:
        self.pid = pid
        self._poll = poll
        self._wait_exc = wait_exc
        self.terminated = False
        self.killed = False
        self.waited = 0

    def poll(self) -> int | None:
        return self._poll

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        self.waited += 1
        if self._wait_exc is not None:
            raise self._wait_exc
        return 0


class TestKillPortForward:
    def test_none_and_already_exited_are_noops(self) -> None:
        assert ssm.kill_port_forward(None) is None
        proc = _FakeProc(poll=0)
        ssm.kill_port_forward(proc)
        assert proc.terminated is False

    def test_windows_tree_kill_is_preferred(self, monkeypatch) -> None:
        calls: list[list[str]] = []

        def _run(argv, **_kwargs):
            calls.append(argv)

            class _R:
                returncode = 0

            return _R()

        monkeypatch.setattr(ssm.os, "name", "nt")
        monkeypatch.setattr(ssm.subprocess, "run", _run)
        proc = _FakeProc()
        ssm.kill_port_forward(proc)
        assert calls == [["taskkill", "/T", "/F", "/PID", "4242"]]
        assert proc.terminated is False

    def test_windows_taskkill_failure_falls_back_to_terminate(self, monkeypatch) -> None:
        monkeypatch.setattr(ssm.os, "name", "nt")
        monkeypatch.setattr(
            ssm.subprocess, "run", lambda argv, **k: (_ for _ in ()).throw(OSError("no taskkill"))
        )
        monkeypatch.setattr(
            ssm.os,
            "getpgid",
            lambda pid: (_ for _ in ()).throw(ProcessLookupError()),
            # Windows has no os.getpgid; these tests SIMULATE the nt branch and must
            # still run there. Same convention as test_cloud_ssm.py.
            raising=False,
        )
        proc = _FakeProc()
        ssm.kill_port_forward(proc)
        assert proc.terminated is True

    def test_windows_pidless_process_falls_back(self, monkeypatch) -> None:
        monkeypatch.setattr(ssm.os, "name", "nt")
        monkeypatch.setattr(
            ssm.subprocess, "run", lambda argv, **k: pytest.fail("taskkill without a pid")
        )
        monkeypatch.setattr(
            ssm.os,
            "getpgid",
            lambda pid: (_ for _ in ()).throw(ProcessLookupError()),
            # Windows has no os.getpgid; these tests SIMULATE the nt branch and must
            # still run there. Same convention as test_cloud_ssm.py.
            raising=False,
        )

        class _Pidless:
            def poll(self) -> None:
                return None

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout: float | None = None) -> int:
                return 0

        proc = _Pidless()
        ssm.kill_port_forward(proc)
        assert getattr(proc, "terminated", False) is True

    def test_windows_taskkill_that_leaves_the_child_alive_falls_back(self, monkeypatch) -> None:
        monkeypatch.setattr(ssm.os, "name", "nt")

        class _R:
            returncode = 0

        monkeypatch.setattr(ssm.subprocess, "run", lambda argv, **k: _R())
        monkeypatch.setattr(
            ssm.os,
            "getpgid",
            lambda pid: (_ for _ in ()).throw(ProcessLookupError()),
            # Windows has no os.getpgid; these tests SIMULATE the nt branch and must
            # still run there. Same convention as test_cloud_ssm.py.
            raising=False,
        )
        proc = _FakeProc(wait_exc=subprocess.TimeoutExpired(cmd="taskkill", timeout=5))
        ssm.kill_port_forward(proc)
        assert proc.terminated is True

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX group signal path")
    def test_posix_signals_the_whole_group(self, monkeypatch) -> None:
        signalled: list[tuple[int, int]] = []
        monkeypatch.setattr(ssm.os, "name", "posix")
        monkeypatch.setattr(ssm.os, "getpgid", lambda pid: 999)
        monkeypatch.setattr(ssm.os, "killpg", lambda pgid, sig: signalled.append((pgid, sig)))
        proc = _FakeProc()
        ssm.kill_port_forward(proc)
        assert signalled and signalled[0][0] == 999
        assert proc.terminated is False


class TestRunCommand:
    def test_invalid_run_as_user_is_refused_before_any_call(self, monkeypatch) -> None:
        monkeypatch.setattr(
            ssm.aws,
            "checked_json",
            lambda *a, **k: pytest.fail("sent a command with an unvalidated run_as"),
        )
        with pytest.raises(aws.AWSError, match="invalid run_as"):
            ssm.run_command("i-0abc", "echo hi", run_as="root; rm -rf /")

    def test_missing_command_id_is_an_error(self, monkeypatch) -> None:
        monkeypatch.setattr(ssm.aws, "checked_json", lambda *a, **k: {"Command": {}})
        with pytest.raises(aws.AWSError, match="no CommandId"):
            ssm.run_command("i-0abc", "echo hi")

    def test_non_dict_send_response_is_an_error(self, monkeypatch) -> None:
        monkeypatch.setattr(ssm.aws, "checked_json", lambda *a, **k: ["unexpected"])
        with pytest.raises(aws.AWSError, match="no CommandId"):
            ssm.run_command("i-0abc", "echo hi")

    def test_unparseable_invocation_keeps_polling_then_times_out(self, monkeypatch) -> None:
        monkeypatch.setattr(
            ssm.aws, "checked_json", lambda *a, **k: {"Command": {"CommandId": "cmd-1"}}
        )
        monkeypatch.setattr(ssm.aws, "run_aws", lambda *a, **k: (0, "{not json", "poll noise"))
        naps: list[int] = []
        monkeypatch.setattr(ssm, "_sleep", lambda secs: naps.append(secs))
        result = ssm.run_command("i-0abc", "echo hi", total_wait=6)
        assert result.status == "TimedOut"
        assert result.exit_code == -1
        assert naps == [3, 3]

    def test_non_integer_response_code_degrades_to_minus_one(self, monkeypatch) -> None:
        monkeypatch.setattr(
            ssm.aws, "checked_json", lambda *a, **k: {"Command": {"CommandId": "cmd-1"}}
        )
        payload = '{"Status": "Failed", "ResponseCode": null, "StandardErrorContent": "boom"}'
        monkeypatch.setattr(ssm.aws, "run_aws", lambda *a, **k: (0, payload, ""))
        result = ssm.run_command("i-0abc", "echo hi")
        assert (result.status, result.exit_code, result.stderr) == ("Failed", -1, "boom")
        assert result.ok is False

    def test_successful_invocation_is_ok(self, monkeypatch) -> None:
        sent: dict[str, object] = {}

        def _checked_json(argv, profile, region, action=""):
            sent["argv"] = argv
            return {"Command": {"CommandId": "cmd-1"}}

        payload = '{"Status": "Success", "ResponseCode": 0, "StandardOutputContent": "done"}'
        monkeypatch.setattr(ssm.aws, "checked_json", _checked_json)
        monkeypatch.setattr(ssm.aws, "run_aws", lambda *a, **k: (0, payload, ""))
        result = ssm.run_command("i-0abc", "printf done")
        assert result.ok is True
        assert result.stdout == "done"
        # The remote script is base64-wrapped so SSM cannot mangle newlines.
        assert "base64 -d | sudo -u ec2-user -i bash" in str(sent["argv"])

    def test_poll_failure_then_timeout_returns_the_stderr(self, monkeypatch) -> None:
        monkeypatch.setattr(
            ssm.aws, "checked_json", lambda *a, **k: {"Command": {"CommandId": "cmd-1"}}
        )
        monkeypatch.setattr(ssm.aws, "run_aws", lambda *a, **k: (255, "", "InvocationDoesNotExist"))
        monkeypatch.setattr(ssm, "_sleep", lambda _s: None)
        result = ssm.run_command("i-0abc", "echo hi", total_wait=0)
        assert result.status == "TimedOut"
        assert result.stderr == "InvocationDoesNotExist"


class TestInstanceIsManaged:
    def test_online_ping_status_is_managed(self, monkeypatch) -> None:
        monkeypatch.setattr(ssm.aws, "run_aws", lambda *a, **k: (0, "Online\n", ""))
        assert ssm.instance_is_managed("i-0abc") is True

    def test_offline_or_failed_call_is_not_managed(self, monkeypatch) -> None:
        monkeypatch.setattr(ssm.aws, "run_aws", lambda *a, **k: (0, "ConnectionLost\n", ""))
        assert ssm.instance_is_managed("i-0abc") is False
        monkeypatch.setattr(ssm.aws, "run_aws", lambda *a, **k: (255, "", "denied"))
        assert ssm.instance_is_managed("i-0abc") is False


def test_wrap_remote_command_is_single_line(tmp_path: Path) -> None:
    wrapped = ssm._wrap_remote_command("set -e\necho hi\n", "ec2-user")
    assert "\n" not in wrapped
    assert wrapped.startswith("echo ")
