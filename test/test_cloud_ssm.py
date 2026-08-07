"""Unit tests for SSM primitives (cloud/ssm.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.cloud import aws, ssm


class TestArgvBuilders:
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

    def test_interactive_session_argv(self):
        argv = ssm.build_interactive_session_argv("i-0abc", "dev", "us-east-1")
        assert argv == [
            "aws",
            "ssm",
            "start-session",
            "--target",
            "i-0abc",
            "--region",
            "us-east-1",
            "--profile",
            "dev",
        ]


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
        import time

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
            start_new_session=platform_compat.IS_POSIX,
            **(
                {}
                if platform_compat.IS_POSIX
                else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            ),
        )
        for _ in range(50):
            if pidfile.exists() and pidfile.read_text(encoding="utf-8").strip():
                break
            time.sleep(0.1)
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
        for _ in range(50):
            if not _alive(child_pid):
                break
            time.sleep(0.1)
        assert not _alive(child_pid), "plugin child (same tree) must be killed"

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


class TestManaged:
    def test_online(self, monkeypatch):
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (0, "Online\n", ""))
        assert ssm.instance_is_managed("i-0abc", "dev") is True

    def test_not_online(self, monkeypatch):
        monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (0, "ConnectionLost\n", ""))
        assert ssm.instance_is_managed("i-0abc", "dev") is False


class TestShellQuote:
    def test_quotes_single_quotes(self):
        assert ssm._shq("it's") == "'it'\\''s'"

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
