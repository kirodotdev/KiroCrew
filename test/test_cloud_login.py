"""Unit tests for kiro-cli login over SSM (cloud/login.py)."""

from __future__ import annotations

from kiro_crew.cloud import login, ssm


class _DummyProcess:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False
        self.waited = False

    def poll(self):
        return 0 if self.terminated or self.killed else None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waited = True
        return 0


class TestParseLoginOutput:
    def test_device_code_url_and_code(self):
        text = (
            "To sign in, open the following URL:\n"
            "  https://device.sso.us-east-1.amazonaws.com/?user_code=WXYZ-1234\n"
            "and enter the code: WXYZ-1234\n"
        )
        p = login.parse_login_output(text)
        assert p.url == "https://device.sso.us-east-1.amazonaws.com/?user_code=WXYZ-1234"
        assert p.code == "WXYZ-1234"
        assert p.actionable is True
        assert p.already_logged_in is False

    def test_prefers_complete_url_over_bare_verification_uri(self):
        # kiro-cli prints the bare verification_uri FIRST and the code-embedded
        # verification_uri_complete second. We must surface the complete one so the
        # user deep-links to the approve screen instead of a generic sign-in page.
        text = (
            "To sign in, open:\n"
            "  https://device.sso.us-east-1.amazonaws.com/\n"
            "and enter code ABCD-1234, or open:\n"
            "  https://device.sso.us-east-1.amazonaws.com/?user_code=ABCD-1234\n"
        )
        p = login.parse_login_output(text)
        assert p.url == "https://device.sso.us-east-1.amazonaws.com/?user_code=ABCD-1234"
        assert p.code == "ABCD-1234"

    def test_already_logged_in(self):
        p = login.parse_login_output("You are already logged in as user@example.com")
        assert p.already_logged_in is True
        assert p.actionable is True

    def test_social_login_ports(self):
        text = "Waiting for authentication on http://localhost:49153/callback ..."
        p = login.parse_login_output(text)
        assert 49153 in p.ports

    def test_social_login_ports_from_127_and_port_text(self):
        text = "Forward 127.0.0.1:49153 and callback port 49154"
        p = login.parse_login_output(text)
        assert p.ports == [49153, 49154]

    def test_url_trailing_punctuation_stripped(self):
        p = login.parse_login_output("Open https://example.com/verify).")
        assert p.url == "https://example.com/verify"


class TestLoginLogPermissions:
    """The remote login log/FIFO under /tmp capture the device-code URL+code /
    OAuth callback details; they must be created owner-only (umask 077) so a
    second local user on the box can't read them from world-readable /tmp."""

    def test_device_login_command_sets_restrictive_umask(self):
        cmd = login._device_login_command(replace_existing=False)
        # umask 077 must precede the log-creating nohup redirect.
        assert "umask 077" in cmd
        assert cmd.index("umask 077") < cmd.index("login --use-device-flow")

    def test_callback_login_command_hardens_log_and_fifo(self):
        cmd = login._callback_login_command()
        assert "umask 077" in cmd
        # umask must precede both the mkfifo and the log redirect.
        assert cmd.index("umask 077") < cmd.index("mkfifo")
        assert "chmod 600" in cmd  # belt-and-suspenders on the FIFO


class TestIsLoggedIn:
    def test_true(self, monkeypatch):
        monkeypatch.setattr(
            ssm,
            "run_command",
            lambda *a, **k: ssm.CommandResult("Success", "user@example.com", "", 0),
        )
        assert login.is_logged_in("i-0abc", "dev") is True

    def test_false_when_noauth(self, monkeypatch):
        monkeypatch.setattr(
            ssm,
            "run_command",
            lambda *a, **k: ssm.CommandResult("Success", "__NOAUTH__", "", 0),
        )
        assert login.is_logged_in("i-0abc", "dev") is False

    def test_true_when_kiro_auth_token_present(self, monkeypatch):
        monkeypatch.setattr(
            ssm,
            "run_command",
            lambda *a, **k: ssm.CommandResult("Success", login._TOKEN_PRESENT_SENTINEL, "", 0),
        )
        assert login.is_logged_in("i-0abc", "dev") is True

    def test_false_when_not_logged_in_message_and_nonzero_exit(self, monkeypatch):
        # `kiro-cli whoami` prints "Not logged in" and exits non-zero. The check
        # captures stderr (2>&1) and propagates the exit code, so a non-zero
        # result must be logged-out even though stdout has no sentinel.
        monkeypatch.setattr(
            ssm,
            "run_command",
            lambda *a, **k: ssm.CommandResult("Failed", "Not logged in", "", 1),
        )
        assert login.is_logged_in("i-0abc", "dev") is False

    def test_false_when_nonzero_exit_even_without_marker(self, monkeypatch):
        # Exit status is authoritative: any non-zero result is logged-out, even
        # if stdout happens to lack a known failure marker (prevents a stale SSO
        # token file from being mistaken for a valid session).
        monkeypatch.setattr(
            ssm,
            "run_command",
            lambda *a, **k: ssm.CommandResult("Failed", "some unexpected text", "", 1),
        )
        assert login.is_logged_in("i-0abc", "dev") is False


class TestStartDeviceLogin:
    def test_short_circuits_when_logged_in(self, monkeypatch):
        monkeypatch.setattr(login, "is_logged_in", lambda *a, **k: True)
        p = login.start_device_login("i-0abc", "dev", open_browser=False)
        assert p.already_logged_in is True

    def test_scrapes_and_returns_prompt(self, monkeypatch):
        monkeypatch.setattr(login, "is_logged_in", lambda *a, **k: False)
        url = "https://device.sso.example.com/verify?user_code=ABCD-9999"
        out = f"Open {url} code: ABCD-9999"
        monkeypatch.setattr(
            ssm, "run_command", lambda *a, **k: ssm.CommandResult("Success", out, "", 0)
        )
        opened = {}
        monkeypatch.setattr(login, "_browser_open_supported", lambda: True)
        monkeypatch.setattr(login.webbrowser, "open", lambda u, **_k: opened.update(url=u))
        p = login.start_device_login("i-0abc", "dev", open_browser=True)
        assert p.code == "ABCD-9999"
        assert opened["url"] == url

    def test_starts_background_login_without_timeout_killing_prompt(self, monkeypatch):
        monkeypatch.setattr(login, "is_logged_in", lambda *a, **k: False)
        captured = {}

        def fake_run_command(_instance_id, command, *_args, **_kwargs):
            captured["command"] = command
            return ssm.CommandResult("Success", "Open https://example.com code: ABCD-9999", "", 0)

        monkeypatch.setattr(ssm, "run_command", fake_run_command)
        login.start_device_login("i-0abc", "dev", open_browser=False)
        assert "nohup" in captured["command"]
        assert "kiro-cli login --use-device-flow" in captured["command"]
        assert "timeout 20" not in captured["command"]

    def test_headless_browser_open_returns_false(self, monkeypatch):
        called = {"opened": False}
        monkeypatch.setattr(login, "_browser_open_supported", lambda: False)
        monkeypatch.setattr(login.webbrowser, "open", lambda *_a, **_k: called.update(opened=True))
        assert login._open_browser("https://example.com") is False
        assert called["opened"] is False

    def test_falls_back_to_automated_callback_port_login(self, monkeypatch):
        monkeypatch.setattr(login, "is_logged_in", lambda *a, **k: False)
        commands: list[str] = []
        opened: list[str] = []
        proc = _DummyProcess()
        forwards: list[tuple[str, int, int, str, str]] = []

        def fake_run_command(instance_id, command, profile="", region="", **_kwargs):
            commands.append(command)
            if "kiro-cli login --use-device-flow" in command:
                return ssm.CommandResult("Success", "device flow unavailable", "", 0)
            if "mkfifo" in command:
                return ssm.CommandResult(
                    "Success",
                    "Forward http://localhost:49153/callback, then press Enter",
                    "",
                    0,
                )
            if "exec 4<>" in command:
                return ssm.CommandResult(
                    "Success",
                    "Open https://auth.example.com/start?session=abc",
                    "",
                    0,
                )
            raise AssertionError(command)

        def fake_open_port_forward(instance_id, remote_port, local_port, profile, region):
            forwards.append((instance_id, remote_port, local_port, profile, region))
            return proc

        monkeypatch.setattr(ssm, "run_command", fake_run_command)
        monkeypatch.setattr(ssm, "port_is_free", lambda *_a, **_k: True)
        monkeypatch.setattr(ssm, "open_port_forward", fake_open_port_forward)
        monkeypatch.setattr(ssm, "wait_for_local_port", lambda port, **_k: port == 49153)
        monkeypatch.setattr(login, "_open_browser", lambda url: opened.append(url) or True)

        p = login.start_device_login("i-0abc", "dev", "us-west-2", open_browser=True)

        assert p.url == "https://auth.example.com/start?session=abc"
        assert p.ports == [49153]
        assert p.browser_opened is True
        assert p.port_forward is proc
        assert forwards == [("i-0abc", 49153, 49153, "dev", "us-west-2")]
        assert opened == ["https://auth.example.com/start?session=abc"]
        assert any("kiro-cli login --use-device-flow" in cmd for cmd in commands)
        # kiro-cli is invoked via the resolved "$KIRO" absolute path now.
        assert any('"$KIRO" login <&3' in cmd for cmd in commands)
        assert any("printf '\\n' >&4" in cmd for cmd in commands)

        p.close()
        assert proc.terminated is True
        assert proc.waited is True

    def test_callback_no_url_reaps_tunnel_and_leaves_no_port_forward(self, monkeypatch):
        # The social-login callback tunnel comes up, but the continued step yields
        # NO usable URL. start_device_login must reap the SSM child itself and
        # return a url-less prompt with port_forward UNSET — otherwise it hands
        # back a live tunnel attached to a url-less prompt that no-url callers
        # (e.g. wizard._verify_operational) may drop without close(), orphaning
        # the child + loopback port.
        monkeypatch.setattr(login, "is_logged_in", lambda *a, **k: False)
        proc = _DummyProcess()
        reaped: list[object] = []

        def fake_run_command(_instance_id, command, *_args, **_kwargs):
            if "kiro-cli login --use-device-flow" in command:
                return ssm.CommandResult("Success", "device flow unavailable", "", 0)
            if "mkfifo" in command:
                return ssm.CommandResult(
                    "Success", "Forward http://localhost:49153/callback, then press Enter", "", 0
                )
            if "exec 4<>" in command:
                # continued step: NO URL in the output
                return ssm.CommandResult("Success", "still waiting, no url yet", "", 0)
            raise AssertionError(command)

        monkeypatch.setattr(ssm, "run_command", fake_run_command)
        monkeypatch.setattr(ssm, "port_is_free", lambda *_a, **_k: True)
        monkeypatch.setattr(ssm, "open_port_forward", lambda *_a, **_k: proc)
        monkeypatch.setattr(ssm, "wait_for_local_port", lambda port, **_k: port == 49153)
        # _close_process delegates here; record the reap.
        monkeypatch.setattr(ssm, "kill_port_forward", lambda p: reaped.append(p))

        p = login.start_device_login("i-0abc", "dev", "us-west-2", open_browser=True)

        assert not p.url  # no usable URL
        assert p.port_forward is None  # NOT handed back a live tunnel
        assert reaped == [proc]  # the tunnel child was reaped in-function
        assert p.error  # a retry hint is surfaced

    def test_callback_port_forward_failure_reports_error(self, monkeypatch):
        monkeypatch.setattr(login, "is_logged_in", lambda *a, **k: False)
        proc = _DummyProcess()

        def fake_run_command(_instance_id, command, *_args, **_kwargs):
            if "kiro-cli login --use-device-flow" in command:
                return ssm.CommandResult("Success", "", "", 0)
            if "mkfifo" in command:
                return ssm.CommandResult("Success", "Use localhost:49153", "", 0)
            raise AssertionError(command)

        monkeypatch.setattr(ssm, "run_command", fake_run_command)
        monkeypatch.setattr(ssm, "port_is_free", lambda *_a, **_k: True)
        monkeypatch.setattr(ssm, "open_port_forward", lambda *_a, **_k: proc)
        monkeypatch.setattr(ssm, "wait_for_local_port", lambda *_a, **_k: False)

        p = login.start_device_login("i-0abc", "dev", "us-west-2", open_browser=False)

        assert p.url == ""
        assert p.ports == [49153]
        assert "SSM callback port-forward did not become ready" in p.error
        assert "49153" in login.social_login_hint(p)
        assert proc.terminated is True

    def test_callback_refuses_when_port_occupied(self, monkeypatch):
        # If the callback port is already taken, refuse before spawning — the
        # OAuth code must never be routed to a foreign local listener.
        monkeypatch.setattr(login, "is_logged_in", lambda *a, **k: False)

        def fake_run_command(_instance_id, command, *_args, **_kwargs):
            if "kiro-cli login --use-device-flow" in command:
                return ssm.CommandResult("Success", "", "", 0)
            if "mkfifo" in command:
                return ssm.CommandResult("Success", "Use localhost:49153", "", 0)
            raise AssertionError(command)

        monkeypatch.setattr(ssm, "run_command", fake_run_command)
        monkeypatch.setattr(ssm, "port_is_free", lambda *_a, **_k: False)

        def _boom(*_a, **_k):  # pragma: no cover - must not spawn on occupied port
            raise AssertionError("must not open a port-forward on an occupied port")

        monkeypatch.setattr(ssm, "open_port_forward", _boom)
        p = login.start_device_login("i-0abc", "dev", "us-west-2", open_browser=False)
        assert p.ports == [49153]
        assert "already in use" in p.error


class TestWaitUntilLoggedIn:
    def test_returns_true_when_becomes_logged_in(self, monkeypatch):
        monkeypatch.setattr(ssm, "_sleep", lambda *_a: None)
        seq = iter([False, False, True])
        monkeypatch.setattr(login, "is_logged_in", lambda *a, **k: next(seq))
        assert login.wait_until_logged_in("i-0abc", "dev", attempts=5) is True

    def test_returns_false_when_never(self, monkeypatch):
        monkeypatch.setattr(ssm, "_sleep", lambda *_a: None)
        monkeypatch.setattr(login, "is_logged_in", lambda *a, **k: False)
        assert login.wait_until_logged_in("i-0abc", "dev", attempts=3) is False
