"""Coverage for the thinly-tested command dispatchers in ``kiro_crew.cli_server``.

Everything here injects a fake at the seam the product actually reaches:

* ``subprocess.run`` / ``os.execvp`` — the CLI shells out to git, journalctl and
  tail. A CI runner has no installed systemd unit, no launchd, and (for the
  update path) no writable checkout, so every spawn is replaced by a recorder.
* ``current_platform`` / ``svc_linux.UNIT_PATH`` / ``svc_macos.STDOUT_LOG`` —
  patched so the platform-specific log sources are exercised on any host without
  a ``skipif``. Nothing here reads a real journal or plist.
* ``sel()`` — replaced with a recorder so the audit contract can be asserted
  directly instead of inferred from the security log on disk.

The gateway/taskrunner coroutines are driven with ``asyncio.run`` (the repo does
not enable pytest-asyncio auto mode).
"""

import argparse
import asyncio
import os
import subprocess
import sys
import types
import urllib.error
from pathlib import Path

import pytest

from kiro_crew import cli_server, platform_compat
from kiro_crew.config.loader import _DEFAULT_PORT
from kiro_crew.dashboard.handlers.core import DASHBOARD_HTML_NOT_FOUND_MARKER
from kiro_crew.platform.update_layout import InstallLayout
from kiro_crew.service import linux as svc_linux
from kiro_crew.service import macos as svc_macos
from kiro_crew.service.common import Platform


class _SelRecorder:
    """Stand-in for :func:`kiro_crew.sel.sel` that records audit calls."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def log_api_access(self, **kw) -> None:
        self.calls.append(kw)

    @property
    def operations(self) -> list[str]:
        return [c.get("operation", "") for c in self.calls]


@pytest.fixture
def sel_rec(monkeypatch):
    rec = _SelRecorder()
    monkeypatch.setattr(cli_server, "sel", lambda: rec)
    return rec


class _Resp:
    """Minimal ``urlopen`` result usable as a context manager."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self, n: int = -1) -> bytes:
        return self._payload if n is None or n < 0 else self._payload[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


# --------------------------------------------------------------------------
# resolve_client_port_ex — malformed env values must fall through, not raise
# --------------------------------------------------------------------------


class TestResolveClientPortExFallThrough:
    """A non-numeric port env var degrades to the next source, never ValueError."""

    @pytest.fixture(autouse=True)
    def _no_other_sources(self, monkeypatch):
        monkeypatch.setattr(cli_server, "_config_url_port", lambda: None)
        monkeypatch.setattr(cli_server, "_marker_port", lambda: None)

    def test_garbage_kirocrew_port_falls_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_PORT", "not-a-number")
        monkeypatch.delenv("KIROCREW_BOUND_PORT", raising=False)
        assert cli_server.resolve_client_port_ex(None) == (_DEFAULT_PORT, False)

    def test_garbage_bound_port_falls_to_default(self, monkeypatch) -> None:
        monkeypatch.delenv("KIROCREW_PORT", raising=False)
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "")
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "12x4")
        assert cli_server.resolve_client_port_ex(None) == (_DEFAULT_PORT, False)

    def test_garbage_kirocrew_port_still_honours_bound_port(self, monkeypatch) -> None:
        """The fall-through lands on the NEXT source, not straight on the default."""
        monkeypatch.setenv("KIROCREW_PORT", "oops")
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "9931")
        assert cli_server.resolve_client_port_ex(None) == (9931, True)


# --------------------------------------------------------------------------
# _probe_dashboard_health
# --------------------------------------------------------------------------


class TestProbeDashboardHealth:
    """Warns only when the served body carries the stale-dashboard marker."""

    def test_stale_marker_warns_on_stderr(self, monkeypatch, capsys) -> None:
        body = f"<html>{DASHBOARD_HTML_NOT_FOUND_MARKER}</html>".encode()
        monkeypatch.setattr(cli_server, "loopback_urlopen", lambda *a, **k: _Resp(body))
        cli_server._probe_dashboard_health(5476)
        assert "stale dashboard" in capsys.readouterr().err

    def test_healthy_body_is_silent(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(
            cli_server, "loopback_urlopen", lambda *a, **k: _Resp(b"<html>ok</html>")
        )
        cli_server._probe_dashboard_health(5476)
        assert capsys.readouterr().err == ""

    def test_network_error_is_swallowed(self, monkeypatch, capsys) -> None:
        def boom(*a, **k):
            raise urllib.error.URLError("refused")

        monkeypatch.setattr(cli_server, "loopback_urlopen", boom)
        cli_server._probe_dashboard_health(5476)  # must not raise
        assert capsys.readouterr().err == ""


# --------------------------------------------------------------------------
# _logout
# --------------------------------------------------------------------------


class TestLogout:
    """Every failure mode exits non-zero with an operator-facing reason."""

    @pytest.fixture
    def secret_home(self, monkeypatch, tmp_path):
        (tmp_path / ".local_secret").write_text("s3cr3t\n", encoding="utf-8", newline="\n")
        monkeypatch.setattr(cli_server, "config_dir", lambda: tmp_path)
        return tmp_path

    def test_missing_secret_reports_gateway_down(self, monkeypatch, tmp_path, capsys) -> None:
        monkeypatch.setattr(cli_server, "config_dir", lambda: tmp_path)
        with pytest.raises(SystemExit) as exc:
            cli_server._logout(5476)
        assert exc.value.code == 1
        assert "Gateway not running" in capsys.readouterr().out

    def test_ok_response_reports_success(self, monkeypatch, secret_home, capsys) -> None:
        monkeypatch.setattr(cli_server, "loopback_urlopen", lambda *a, **k: _Resp(b'{"ok": true}'))
        cli_server._logout(5476)
        assert "revoked" in capsys.readouterr().out

    def test_not_ok_response_surfaces_error_field(self, monkeypatch, secret_home, capsys) -> None:
        monkeypatch.setattr(
            cli_server,
            "loopback_urlopen",
            lambda *a, **k: _Resp(b'{"ok": false, "error": "store locked"}'),
        )
        with pytest.raises(SystemExit) as exc:
            cli_server._logout(5476)
        assert exc.value.code == 1
        assert "store locked" in capsys.readouterr().out

    def test_http_error_reports_status_code(self, monkeypatch, secret_home, capsys) -> None:
        def boom(*a, **k):
            raise urllib.error.HTTPError("u", 403, "no", {}, None)  # type: ignore[arg-type]

        monkeypatch.setattr(cli_server, "loopback_urlopen", boom)
        with pytest.raises(SystemExit) as exc:
            cli_server._logout(5476)
        assert exc.value.code == 1
        assert "HTTP 403" in capsys.readouterr().out

    def test_connection_refused_reports_gateway_down(
        self, monkeypatch, secret_home, capsys
    ) -> None:
        def boom(*a, **k):
            raise urllib.error.URLError("refused")

        monkeypatch.setattr(cli_server, "loopback_urlopen", boom)
        with pytest.raises(SystemExit) as exc:
            cli_server._logout(5476)
        assert exc.value.code == 1
        assert "Gateway not running" in capsys.readouterr().out


# --------------------------------------------------------------------------
# _stop
# --------------------------------------------------------------------------


class TestStopViaService:
    """No ``--port`` means the service manager gets first refusal."""

    def test_service_stop_short_circuits_port_scan(self, monkeypatch, sel_rec, capsys) -> None:
        monkeypatch.setattr(cli_server, "resolve_client_port", lambda p: 5476)
        monkeypatch.setattr(cli_server.service_controller, "stop_service", lambda: True)

        def unreachable(port):  # pragma: no cover - asserts the short-circuit
            raise AssertionError("port scan must not run when the service stopped")

        monkeypatch.setattr(platform_compat, "find_listening_pids", unreachable)
        cli_server._stop(None)
        assert "Stopped kirocrew service" in capsys.readouterr().out
        assert sel_rec.calls[0]["resources"].endswith("via=service")

    def test_explicit_port_bypasses_the_service(self, monkeypatch, sel_rec, capsys) -> None:
        monkeypatch.setattr(cli_server, "resolve_client_port", lambda p: 8123)

        def unreachable():  # pragma: no cover - asserts the bypass
            raise AssertionError("service must not be consulted for an explicit port")

        monkeypatch.setattr(cli_server.service_controller, "stop_service", unreachable)
        monkeypatch.setattr(platform_compat, "find_listening_pids", lambda port: [])
        with pytest.raises(SystemExit) as exc:
            cli_server._stop(8123)
        assert exc.value.code == 1
        assert "No Kiro Crew gateway currently running on port 8123" in capsys.readouterr().out
        assert sel_rec.calls[-1]["outcome"] == "no_target"


class TestStopOnWindows:
    """The Windows branch kills the whole tree via ``platform_compat``."""

    @pytest.fixture(autouse=True)
    def _windows(self, monkeypatch):
        monkeypatch.setattr(cli_server, "resolve_client_port", lambda p: 5476)
        monkeypatch.setattr(cli_server.service_controller, "stop_service", lambda: False)
        monkeypatch.setattr(platform_compat, "find_listening_pids", lambda port: [4242])
        monkeypatch.setattr(cli_server, "_is_kirocrew_process", lambda pid: True)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(cli_server, "_pid_exited", lambda pid: True)
        monkeypatch.setattr("time.sleep", lambda s: None)

    def test_tree_kill_reports_terminated(self, monkeypatch, sel_rec, capsys) -> None:
        seen: list[int] = []
        monkeypatch.setattr(
            platform_compat, "kill_process_tree", lambda pid, sig: seen.append(pid)
        )
        cli_server._stop(None)
        out = capsys.readouterr().out
        assert seen == [4242]
        assert "Terminated gateway (pid 4242)" in out
        assert sel_rec.calls[-1]["outcome"] == "allowed"

    def test_already_gone_pid_is_not_reported_as_stopped(
        self, monkeypatch, sel_rec, capsys
    ) -> None:
        def gone(pid, sig):
            raise ProcessLookupError

        monkeypatch.setattr(platform_compat, "kill_process_tree", gone)
        with pytest.raises(SystemExit) as exc:
            cli_server._stop(None)
        assert exc.value.code == 1
        assert "process already exited" in capsys.readouterr().out

    def test_permission_error_asks_for_sudo_and_exits(self, monkeypatch, sel_rec, capsys) -> None:
        def denied(pid, sig):
            raise PermissionError

        monkeypatch.setattr(platform_compat, "kill_process_tree", denied)
        with pytest.raises(SystemExit) as exc:
            cli_server._stop(None)
        assert exc.value.code == 1
        assert "No permission to stop pid 4242" in capsys.readouterr().out
        assert sel_rec.calls[-1]["outcome"] == "denied"

    def test_generic_taskkill_failure_rechecks_liveness(self, monkeypatch, capsys) -> None:
        """An OSError from taskkill is only "denied" if the pid is still alive."""

        def oserr(pid, sig):
            raise OSError("taskkill exit 1")

        monkeypatch.setattr(platform_compat, "kill_process_tree", oserr)
        monkeypatch.setattr(platform_compat, "pid_exists", lambda pid: True)
        with pytest.raises(SystemExit) as exc:
            cli_server._stop(None)
        assert exc.value.code == 1
        assert "No permission to stop pid 4242" in capsys.readouterr().out

    def test_generic_taskkill_failure_on_dead_pid_is_not_denied(
        self, monkeypatch, capsys
    ) -> None:
        def oserr(pid, sig):
            raise OSError("taskkill exit 1")

        monkeypatch.setattr(platform_compat, "kill_process_tree", oserr)
        monkeypatch.setattr(platform_compat, "pid_exists", lambda pid: False)
        with pytest.raises(SystemExit) as exc:
            cli_server._stop(None)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "process already exited" in out
        assert "No permission" not in out


# --------------------------------------------------------------------------
# _service_cmd / _sandbox_cmd
# --------------------------------------------------------------------------


class TestServiceCmd:
    """Each action forwards to the controller and audits the resulting rc."""

    @pytest.mark.parametrize(
        "action,fn,operation",
        [
            ("install", "install_service", "service_install"),
            ("uninstall", "uninstall_service", "service_uninstall"),
            ("status", "service_status", "service_status"),
        ],
    )
    def test_action_forwards_and_audits(
        self, monkeypatch, sel_rec, action, fn, operation
    ) -> None:
        monkeypatch.setattr(cli_server.service_controller, fn, lambda: 0)
        rc = cli_server._service_cmd(argparse.Namespace(service_action=action))
        assert rc == 0
        assert sel_rec.operations == [operation]
        assert sel_rec.calls[0]["outcome"] == "allowed"

    def test_nonzero_rc_is_audited_as_error(self, monkeypatch, sel_rec) -> None:
        monkeypatch.setattr(cli_server.service_controller, "install_service", lambda: 3)
        assert cli_server._service_cmd(argparse.Namespace(service_action="install")) == 3
        assert sel_rec.calls[0]["outcome"] == "error"
        assert "rc=3" in sel_rec.calls[0]["resources"]

    def test_unknown_action_prints_usage_and_returns_2(self, sel_rec, capsys) -> None:
        assert cli_server._service_cmd(argparse.Namespace(service_action=None)) == 2
        assert "Usage: kirocrew service" in capsys.readouterr().err
        assert sel_rec.calls == []


class TestSandboxCmd:
    """Profile writes are audited; the read-only status probe is not."""

    def test_install_profile_forwards_path_and_audits(self, monkeypatch, sel_rec) -> None:
        seen: list[str | None] = []
        monkeypatch.setattr(
            cli_server.service_controller,
            "install_launcher_profile",
            lambda p: seen.append(p) or 0,
        )
        rc = cli_server._sandbox_cmd(
            argparse.Namespace(sandbox_action="install-profile", path="/opt/app.AppImage")
        )
        assert rc == 0
        assert seen == ["/opt/app.AppImage"]
        assert sel_rec.operations == ["sandbox_profile_install"]
        assert "/opt/app.AppImage" in sel_rec.calls[0]["resources"]

    def test_install_profile_without_path_records_appimage_placeholder(
        self, monkeypatch, sel_rec
    ) -> None:
        monkeypatch.setattr(cli_server.service_controller, "install_launcher_profile", lambda p: 1)
        assert (
            cli_server._sandbox_cmd(argparse.Namespace(sandbox_action="install-profile", path=None))
            == 1
        )
        assert sel_rec.calls[0]["outcome"] == "error"
        assert "$APPIMAGE" in sel_rec.calls[0]["resources"]

    def test_remove_profile_audits(self, monkeypatch, sel_rec) -> None:
        monkeypatch.setattr(cli_server.service_controller, "remove_launcher_profile", lambda: 0)
        assert cli_server._sandbox_cmd(argparse.Namespace(sandbox_action="remove-profile")) == 0
        assert sel_rec.operations == ["sandbox_profile_remove"]

    def test_status_is_not_audited(self, monkeypatch, sel_rec) -> None:
        monkeypatch.setattr(
            cli_server.service_controller, "sandbox_profile_status", lambda p: 7
        )
        rc = cli_server._sandbox_cmd(argparse.Namespace(sandbox_action="status", path=None))
        assert rc == 7
        assert sel_rec.calls == []

    def test_unknown_action_prints_usage_and_returns_2(self, sel_rec, capsys) -> None:
        assert cli_server._sandbox_cmd(argparse.Namespace(sandbox_action="bogus")) == 2
        assert "Usage: kirocrew sandbox" in capsys.readouterr().err
        assert sel_rec.calls == []


# --------------------------------------------------------------------------
# _logs_cmd
# --------------------------------------------------------------------------


class _ExecCalled(Exception):
    """Raised by the ``os.execvp`` stub — the real call never returns."""

    def __init__(self, file: str, argv: list[str]) -> None:
        super().__init__(file)
        self.file = file
        self.argv = argv


@pytest.fixture
def fake_execvp(monkeypatch):
    def _execvp(file, argv):
        raise _ExecCalled(file, list(argv))

    monkeypatch.setattr(os, "execvp", _execvp)


class TestLogsCmdSystemd:
    """Journal first, sudo only as a fallback, and never a blind sudo prompt."""

    @pytest.fixture(autouse=True)
    def _systemd(self, monkeypatch, tmp_path):
        unit = tmp_path / "kirocrew.service"
        unit.write_text("[Unit]\n", encoding="utf-8", newline="\n")
        monkeypatch.setattr(cli_server, "current_platform", lambda: Platform.SYSTEMD)
        monkeypatch.setattr(svc_linux, "UNIT_PATH", unit)

    def test_unprivileged_journal_is_execed_when_probe_returns_rows(
        self, monkeypatch, sel_rec, fake_execvp
    ) -> None:
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "-- Logs begin --\n", ""),
        )
        with pytest.raises(_ExecCalled) as exc:
            cli_server._logs_cmd(argparse.Namespace(follow=True, lines=42))
        assert exc.value.file == "journalctl"
        assert exc.value.argv[0] == "journalctl"
        assert "-f" in exc.value.argv
        assert "42" in exc.value.argv
        # Audited BEFORE the exec, or the record would never be written.
        assert sel_rec.operations == ["logs"]
        assert "follow=True lines=42" in sel_rec.calls[0]["resources"]

    def test_empty_probe_without_tty_refuses_instead_of_hanging_on_sudo(
        self, monkeypatch, sel_rec, capsys
    ) -> None:
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "denied")
        )
        monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
        with pytest.raises(SystemExit) as exc:
            cli_server._logs_cmd(argparse.Namespace(follow=False, lines=10))
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "stdin is not a TTY" in err
        assert "systemd-journal" in err

    def test_empty_probe_with_tty_falls_back_to_sudo_journalctl(
        self, monkeypatch, sel_rec, fake_execvp
    ) -> None:
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "   \n", "")
        )
        monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
        with pytest.raises(_ExecCalled) as exc:
            cli_server._logs_cmd(argparse.Namespace(follow=True, lines=5))
        assert exc.value.file == "sudo"
        assert exc.value.argv[:2] == ["sudo", "journalctl"]
        assert exc.value.argv[-1] == "-f"
        assert "--no-pager" in exc.value.argv

    def test_missing_unit_falls_through_to_the_plain_log_file(
        self, monkeypatch, tmp_path, sel_rec, fake_execvp
    ) -> None:
        monkeypatch.setattr(svc_linux, "UNIT_PATH", tmp_path / "absent.service")
        log = tmp_path / "gateway.log"
        log.write_text("hi\n", encoding="utf-8", newline="\n")
        monkeypatch.setattr(cli_server, "config_dir", lambda: tmp_path)

        def unreachable(*a, **k):  # pragma: no cover - proves no journal probe
            raise AssertionError("journalctl must not be probed without an installed unit")

        monkeypatch.setattr(subprocess, "run", unreachable)
        with pytest.raises(_ExecCalled) as exc:
            cli_server._logs_cmd(argparse.Namespace(follow=False, lines=3))
        assert exc.value.file == "tail"
        assert exc.value.argv == ["tail", "-n", "3", str(log)]


class TestLogsCmdOtherSources:
    """launchd stdout file, plain log file, and the "nothing to tail" refusal."""

    @pytest.fixture
    def launchd(self, monkeypatch, tmp_path):
        """An installed launchd agent: a plist on disk and a non-empty stdout log.

        Both paths are patched even when a test only cares about one of them,
        because the real ones are consulted otherwise and a CI runner has
        neither.
        """
        plist = tmp_path / "crew.plist"
        plist.write_text("<plist/>\n", encoding="utf-8", newline="\n")
        stdout_log = tmp_path / "launchd-gateway.log"
        stdout_log.write_text("x\n", encoding="utf-8", newline="\n")
        monkeypatch.setattr(cli_server, "current_platform", lambda: Platform.LAUNCHD)
        monkeypatch.setattr(svc_macos, "PLIST_PATH", plist)
        monkeypatch.setattr(svc_macos, "STDOUT_LOG", stdout_log)
        return types.SimpleNamespace(plist=plist, stdout_log=stdout_log)

    def test_launchd_stdout_log_is_tailed(self, sel_rec, fake_execvp, launchd) -> None:
        with pytest.raises(_ExecCalled) as exc:
            cli_server._logs_cmd(argparse.Namespace(follow=True, lines=8))
        assert exc.value.argv == ["tail", "-n", "8", "-f", str(launchd.stdout_log)]

    def test_launchd_without_an_installed_plist_falls_through(
        self, monkeypatch, tmp_path, sel_rec, fake_execvp, launchd
    ) -> None:
        """A foreground gateway on macOS reaches the config-dir log, not the agent's."""
        launchd.plist.unlink()
        fallback = tmp_path / "fallback" / "gateway.log"
        fallback.parent.mkdir()
        fallback.write_text("real\n", encoding="utf-8", newline="\n")
        monkeypatch.setattr(cli_server, "config_dir", lambda: fallback.parent)
        with pytest.raises(_ExecCalled) as exc:
            cli_server._logs_cmd(argparse.Namespace(follow=False, lines=4))
        assert exc.value.argv == ["tail", "-n", "4", str(fallback)]

    def test_launchd_with_an_empty_stdout_log_falls_through(
        self, monkeypatch, tmp_path, sel_rec, fake_execvp, launchd
    ) -> None:
        """A 0-byte agent log satisfies exists(), so size is what gates the branch."""
        launchd.stdout_log.write_text("", encoding="utf-8", newline="\n")
        fallback = tmp_path / "fallback" / "gateway.log"
        fallback.parent.mkdir()
        fallback.write_text("real\n", encoding="utf-8", newline="\n")
        monkeypatch.setattr(cli_server, "config_dir", lambda: fallback.parent)
        with pytest.raises(_ExecCalled) as exc:
            cli_server._logs_cmd(argparse.Namespace(follow=False, lines=4))
        assert exc.value.argv == ["tail", "-n", "4", str(fallback)]

    def test_no_log_source_at_all_exits_with_guidance(
        self, monkeypatch, tmp_path, sel_rec, capsys
    ) -> None:
        monkeypatch.setattr(cli_server, "current_platform", lambda: Platform.UNSUPPORTED)
        monkeypatch.setattr(cli_server, "config_dir", lambda: tmp_path)
        with pytest.raises(SystemExit) as exc:
            cli_server._logs_cmd(argparse.Namespace(follow=False, lines=100))
        assert exc.value.code == 1
        assert "No gateway logs found" in capsys.readouterr().err
        assert sel_rec.operations == ["logs"]

    def test_zero_lines_argument_falls_back_to_the_default(
        self, monkeypatch, tmp_path, sel_rec, fake_execvp
    ) -> None:
        """``lines=0`` is falsy, so the product substitutes 100 rather than tailing nothing."""
        monkeypatch.setattr(cli_server, "current_platform", lambda: Platform.UNSUPPORTED)
        monkeypatch.setattr(cli_server, "config_dir", lambda: tmp_path)
        (tmp_path / "gateway.log").write_text("x\n", encoding="utf-8", newline="\n")
        with pytest.raises(_ExecCalled) as exc:
            cli_server._logs_cmd(argparse.Namespace(follow=False, lines=0))
        assert exc.value.argv[:3] == ["tail", "-n", "100"]


# --------------------------------------------------------------------------
# _gateway
# --------------------------------------------------------------------------


class _FakeCfg:
    """Config double for ``_gateway`` — records ``save()`` instead of writing."""

    saved = 0

    def save(self) -> None:
        type(self).saved += 1

    @classmethod
    def load(cls) -> "_FakeCfg":
        return cls()


class TestGateway:
    """Start-up reconciliation runs, then ``run_gateway`` receives the flags verbatim."""

    @pytest.fixture
    def gw(self, monkeypatch, tmp_path):
        captured: dict = {}

        async def _run_gateway(cfg, **kw):
            captured["cfg"] = cfg
            captured["kw"] = kw

        monkeypatch.setattr(cli_server, "run_gateway", _run_gateway)
        monkeypatch.setattr(cli_server, "activate_mise", lambda: [])
        monkeypatch.setattr(cli_server, "ensure_dev_dist_symlink", lambda: tmp_path / "dist")
        monkeypatch.setattr(cli_server, "_should_reconcile_launchd_launcher", lambda: False)
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(cli_server, "config_path", lambda: cfg_file)

        class _Cfg(_FakeCfg):
            saved = 0

        monkeypatch.setattr(cli_server, "KiroCrewConfig", _Cfg)
        monkeypatch.setattr("kiro_crew.cli._node_ok", lambda: True)
        return captured, _Cfg

    def test_flags_are_forwarded_to_run_gateway(self, gw) -> None:
        captured, cfg_cls = gw
        asyncio.run(
            cli_server._gateway(
                no_dashboard=True, no_crons=True, no_open=True, port_override="9001"
            )
        )
        assert captured["kw"]["no_dashboard"] is True
        assert captured["kw"]["no_crons"] is True
        assert captured["kw"]["port_override"] == "9001"
        # Existing config file -> no default config written.
        assert cfg_cls.saved == 0

    def test_missing_config_is_created_before_load(self, gw, monkeypatch, tmp_path, capsys) -> None:
        captured, cfg_cls = gw
        monkeypatch.setattr(cli_server, "config_path", lambda: tmp_path / "absent.json")
        asyncio.run(cli_server._gateway())
        assert cfg_cls.saved == 1
        assert "Created default config" in capsys.readouterr().out

    def test_missing_dist_warns_but_still_starts(self, gw, monkeypatch, caplog) -> None:
        captured, _ = gw
        monkeypatch.setattr(cli_server, "ensure_dev_dist_symlink", lambda: None)
        with caplog.at_level("WARNING", logger="kiro_crew.cli_server"):
            asyncio.run(cli_server._gateway(no_dashboard=False))
        assert any("dist/ not found" in r.message for r in caplog.records)
        assert "kw" in captured

    def test_slack_only_skips_the_dist_check(self, gw, monkeypatch) -> None:
        captured, _ = gw

        def unreachable():  # pragma: no cover - proves the skip
            raise AssertionError("dist must not be resolved in slack-only mode")

        monkeypatch.setattr(cli_server, "ensure_dev_dist_symlink", unreachable)
        asyncio.run(cli_server._gateway(no_dashboard=True))
        assert captured["kw"]["no_dashboard"] is True

    def test_stale_node_triggers_ensure_node(self, gw, monkeypatch) -> None:
        calls: list[str] = []
        monkeypatch.setattr("kiro_crew.cli._node_ok", lambda: False)
        monkeypatch.setattr("kiro_crew.cli._ensure_node", lambda *a: calls.append("ensured"))
        asyncio.run(cli_server._gateway(no_dashboard=True))
        assert calls == ["ensured"]

    def test_mise_activation_is_logged_when_it_changes_the_env(
        self, gw, monkeypatch, caplog
    ) -> None:
        monkeypatch.setattr(cli_server, "activate_mise", lambda: ["PATH"])
        with caplog.at_level("INFO", logger="kiro_crew.cli_server"):
            asyncio.run(cli_server._gateway(no_dashboard=True))
        assert any("Activated mise" in r.message for r in caplog.records)

    def test_launchd_launcher_repair_failure_is_non_fatal(self, gw, monkeypatch, caplog) -> None:
        captured, _ = gw
        monkeypatch.setattr(cli_server, "_should_reconcile_launchd_launcher", lambda: True)

        def boom():
            raise OSError("read-only Application Support")

        monkeypatch.setattr(svc_macos, "ensure_live_program", boom)
        with caplog.at_level("WARNING", logger="kiro_crew.cli_server"):
            asyncio.run(cli_server._gateway(no_dashboard=True))
        assert any("live-gateway launcher" in r.message for r in caplog.records)
        assert "kw" in captured  # the gateway still started


# --------------------------------------------------------------------------
# _run_task
# --------------------------------------------------------------------------


class _FakeStore:
    def __init__(self, *a, **kw) -> None:
        self.kwargs = kw
        self.inited = False
        self.builtins_synced = False

    def init(self) -> None:
        self.inited = True

    def sync_builtins(self) -> None:
        # _run_task now syncs builtin skills through the explicit seam
        # (SkillsLoader(install_builtins=False) + to_thread(sync_builtins)).
        self.builtins_synced = True


class _FakeVectorStore(_FakeStore):
    embed_fn = None
    embed_fn_factory = None


class _FakeSessions:
    def __init__(self, cfg, provider_factory=None) -> None:
        self.cfg = cfg
        self.pool_started = False
        self.closed = False

    async def start_pool(self) -> None:
        self.pool_started = True

    async def close_all(self) -> None:
        self.closed = True


class _Result:
    def __init__(self, status: str, error: str = "") -> None:
        self.status = status
        self.error = error
        self.name = "demo"
        self.task_id = "t-1"
        self.tasks = [1, 2, 3]


@pytest.fixture
def taskrunner_env(monkeypatch, tmp_path):
    """Replace every collaborator ``_run_task`` constructs, and expose the spies."""
    from kiro_crew.config import KiroCrewConfig

    state: dict = {"vector": None, "sessions": None, "runner_kwargs": None, "observed": []}

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cls()))
    monkeypatch.setattr(cli_server, "KiroCrewConfig", KiroCrewConfig)
    monkeypatch.setattr(cli_server, "build_provider_factory", lambda cfg: object())

    def _sessions(cfg, provider_factory=None):
        state["sessions"] = _FakeSessions(cfg, provider_factory)
        return state["sessions"]

    monkeypatch.setattr(cli_server, "SessionManager", _sessions)
    monkeypatch.setattr(cli_server, "MemoryStore", _FakeStore)

    def _vector(**kw):
        state["vector"] = _FakeVectorStore(**kw)
        return state["vector"]

    monkeypatch.setattr(cli_server, "VectorMemoryStore", _vector)
    monkeypatch.setattr(cli_server, "ConversationLog", _FakeStore)
    monkeypatch.setattr(cli_server, "LessonStore", _FakeStore)
    monkeypatch.setattr(cli_server, "SkillsLoader", _FakeStore)
    monkeypatch.setattr(cli_server, "HistoryConsolidator", lambda **kw: object())
    monkeypatch.setattr(cli_server, "hooks_config_from_config_dict", lambda h: {})
    monkeypatch.setattr(cli_server, "HookManager", lambda hc: object())
    monkeypatch.setattr(cli_server, "ContextBuilder", lambda **kw: object())
    monkeypatch.setattr(
        cli_server, "register_skill_read_observer", lambda ctx: state["observed"].append(ctx)
    )
    monkeypatch.setattr(cli_server, "_session_work_dir", lambda key: tmp_path)
    monkeypatch.setattr(cli_server, "make_sync_embed_fn", lambda: (lambda text: [0.0]))
    monkeypatch.setattr(cli_server, "model_file_present", lambda: True)
    monkeypatch.setattr(cli_server, "store_embedding_space_is_stale", lambda vs: False)

    def _install_runner(result: _Result) -> None:
        class _Runner:
            def __init__(self, **kw) -> None:
                state["runner_kwargs"] = kw

            async def run(self, spec_path, name=""):
                state["ran"] = (spec_path, name)
                return result

        monkeypatch.setattr(cli_server, "TaskRunner", _Runner)

    state["install_runner"] = _install_runner
    return state


def _spec(tmp_path: Path) -> Path:
    spec = tmp_path / "task.md"
    spec.write_text("# Task\n\n- step one\n", encoding="utf-8", newline="\n")
    return spec


class TestRunTask:
    """Spec validation, embedding degradation, and the three terminal statuses."""

    def test_missing_spec_exits_before_building_anything(self, tmp_path, capsys) -> None:
        args = argparse.Namespace(spec=str(tmp_path / "nope.md"))
        with pytest.raises(SystemExit) as exc:
            asyncio.run(cli_server._run_task(args))
        assert exc.value.code == 1
        assert "Spec file not found" in capsys.readouterr().err

    def test_completed_task_wires_runner_and_closes_sessions(
        self, taskrunner_env, tmp_path, capsys
    ) -> None:
        taskrunner_env["install_runner"](_Result("completed"))
        spec = _spec(tmp_path)
        args = argparse.Namespace(
            spec=str(spec), no_test=True, fresh=False, timeout=90, name="my-run"
        )
        asyncio.run(cli_server._run_task(args))
        kw = taskrunner_env["runner_kwargs"]
        assert kw["auto_test"] is False  # --no-test inverts into auto_test
        assert kw["fresh"] is False
        assert kw["global_timeout"] == 90.0
        assert taskrunner_env["ran"] == (spec.resolve(), "my-run")
        assert taskrunner_env["sessions"].pool_started is True
        assert taskrunner_env["sessions"].closed is True
        assert taskrunner_env["observed"]  # skill-read observer registered
        out = capsys.readouterr().out
        assert "Task completed" in out and "(3 steps)" in out

    def test_builtin_sync_runs_through_the_explicit_seam(
        self, taskrunner_env, tmp_path, monkeypatch
    ) -> None:
        # _run_task runs on a loop, where construction-time sync skips
        # itself: the loader must be built with install_builtins=False and
        # have sync_builtins driven through the explicit off-loop seam.
        created: list[_FakeStore] = []

        class _SpyLoader(_FakeStore):
            def __init__(self, *a, **kw) -> None:
                super().__init__(*a, **kw)
                created.append(self)

        monkeypatch.setattr(cli_server, "SkillsLoader", _SpyLoader)
        taskrunner_env["install_runner"](_Result("completed"))
        args = argparse.Namespace(
            spec=str(_spec(tmp_path)), no_test=True, fresh=False, timeout=90, name=""
        )
        asyncio.run(cli_server._run_task(args))
        assert any(
            inst.kwargs.get("install_builtins") is False and inst.builtins_synced
            for inst in created
        )

    def test_failed_builtin_sync_does_not_gate_the_task(
        self, taskrunner_env, tmp_path, monkeypatch
    ) -> None:
        # A read-only skills dir (or any sync error) must degrade to the
        # skills already on disk, not kill the run before it starts.
        class _BrokenLoader(_FakeStore):
            def sync_builtins(self) -> None:
                raise OSError("skills dir unavailable")

        monkeypatch.setattr(cli_server, "SkillsLoader", _BrokenLoader)
        taskrunner_env["install_runner"](_Result("completed"))
        spec = _spec(tmp_path)
        args = argparse.Namespace(
            spec=str(spec), no_test=True, fresh=False, timeout=90, name=""
        )
        asyncio.run(cli_server._run_task(args))  # must not raise
        assert taskrunner_env["ran"] == (spec.resolve(), "")

    def test_fresh_flag_is_forwarded_and_announced(
        self, taskrunner_env, tmp_path, capsys
    ) -> None:
        taskrunner_env["install_runner"](_Result("completed"))
        args = argparse.Namespace(
            spec=str(_spec(tmp_path)), no_test=False, fresh=True, timeout=0, name=""
        )
        asyncio.run(cli_server._run_task(args))
        assert taskrunner_env["runner_kwargs"]["fresh"] is True
        assert taskrunner_env["runner_kwargs"]["auto_test"] is True
        assert "Running spec (fresh)" in capsys.readouterr().out

    def test_failed_task_exits_one_with_the_error(
        self, taskrunner_env, tmp_path, capsys
    ) -> None:
        taskrunner_env["install_runner"](_Result("failed", error="step 2 blew up"))
        args = argparse.Namespace(
            spec=str(_spec(tmp_path)), no_test=False, fresh=False, timeout=0, name=""
        )
        with pytest.raises(SystemExit) as exc:
            asyncio.run(cli_server._run_task(args))
        assert exc.value.code == 1
        assert "step 2 blew up" in capsys.readouterr().err

    def test_cancelled_task_exits_one(self, taskrunner_env, tmp_path, capsys) -> None:
        taskrunner_env["install_runner"](_Result("cancelled"))
        args = argparse.Namespace(
            spec=str(_spec(tmp_path)), no_test=False, fresh=False, timeout=0, name=""
        )
        with pytest.raises(SystemExit) as exc:
            asyncio.run(cli_server._run_task(args))
        assert exc.value.code == 1
        assert "cancelled" in capsys.readouterr().out

    def test_embed_fn_is_bound_when_the_model_is_present(self, taskrunner_env, tmp_path) -> None:
        taskrunner_env["install_runner"](_Result("completed"))
        args = argparse.Namespace(
            spec=str(_spec(tmp_path)), no_test=False, fresh=False, timeout=0, name=""
        )
        asyncio.run(cli_server._run_task(args))
        assert taskrunner_env["vector"].embed_fn is not None
        assert taskrunner_env["vector"].embed_fn_factory is not None

    def test_absent_model_degrades_to_keyword_search_without_downloading(
        self, taskrunner_env, tmp_path, monkeypatch, capsys
    ) -> None:
        monkeypatch.setattr(cli_server, "model_file_present", lambda: False)

        def unreachable(vs):  # pragma: no cover - staleness needs an embed_fn
            raise AssertionError("staleness must not be probed without an embed_fn")

        monkeypatch.setattr(cli_server, "store_embedding_space_is_stale", unreachable)
        taskrunner_env["install_runner"](_Result("completed"))
        args = argparse.Namespace(
            spec=str(_spec(tmp_path)), no_test=False, fresh=False, timeout=0, name=""
        )
        asyncio.run(cli_server._run_task(args))
        assert taskrunner_env["vector"].embed_fn is None
        # The factory stays wired even without the model.
        assert taskrunner_env["vector"].embed_fn_factory is not None
        assert "keyword search for this run" in capsys.readouterr().err

    def test_stale_vector_space_clears_both_embed_hooks(
        self, taskrunner_env, tmp_path, monkeypatch, capsys
    ) -> None:
        monkeypatch.setattr(cli_server, "store_embedding_space_is_stale", lambda vs: True)
        taskrunner_env["install_runner"](_Result("completed"))
        args = argparse.Namespace(
            spec=str(_spec(tmp_path)), no_test=False, fresh=False, timeout=0, name=""
        )
        asyncio.run(cli_server._run_task(args))
        vector = taskrunner_env["vector"]
        assert vector.embed_fn is None
        assert vector.embed_fn_factory is None
        assert "Embedding model changed" in capsys.readouterr().err


# --------------------------------------------------------------------------
# _update — git checkout path
# --------------------------------------------------------------------------


class _GitStub:
    """Routes ``subprocess.run`` by argv prefix so each branch is reachable."""

    def __init__(self, **rc: int) -> None:
        self.rc = rc
        self.calls: list[list[str]] = []
        self.status_out = ""

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(argv, self.rc.get("rev_parse", 0), "main\n", "")
        if argv[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(argv, self.rc.get("fetch", 0), "", "no remote")
        if argv[:2] == ["git", "diff"]:
            return subprocess.CompletedProcess(argv, self.rc.get("diff", 1), "", "")
        if argv[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(argv, 0, self.status_out, "")
        if argv[:2] == ["git", "reset"]:
            return subprocess.CompletedProcess(argv, self.rc.get("reset", 0), "", "dirty")
        if argv[0] == "kiro-cli":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "pip" in argv:
            return subprocess.CompletedProcess(argv, self.rc.get("pip", 0), "", "wheel error")
        return subprocess.CompletedProcess(argv, self.rc.get("setup", 0), "", "")


@pytest.fixture
def git_checkout(monkeypatch, tmp_path):
    """A KIROCREW_PROJECT_DIR that looks like a git checkout, with git stubbed out."""
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    # ``.git/HEAD``, not just ``.git/``: the install shape is derived by asking
    # git and falling back to the on-disk markers of a working tree's own root,
    # and a bare ``.git`` directory is refused by both on purpose.
    (proj / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))
    # The git lane requires provenance as well as the path probe; this fixture
    # exercises the git path with a fabricated tree the test process does not
    # run from, so provenance is declared rather than derived.
    monkeypatch.setattr(
        "kiro_crew.platform.update_capability.running_from_checkout",
        lambda root, **kw: True,
    )
    monkeypatch.setattr(
        "kiro_crew.platform.update_governance.resolve_remote_url",
        lambda p, remote="", branch="": "https://github.com/kirodotdev/KiroCrew.git",
    )
    monkeypatch.setattr(
        "kiro_crew.platform.update_governance.update_blocked_reason", lambda url: ""
    )
    monkeypatch.setattr(cli_server.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli_server, "build_frontend_sync", lambda p: None)
    monkeypatch.setattr("kiro_crew.cli._ensure_node", lambda *a: None)
    return proj


class TestUpdateGitPath:
    """Every early-exit branch of the git update, plus the full success path."""

    def test_branch_detection_failure_exits(self, monkeypatch, git_checkout, capsys) -> None:
        monkeypatch.setattr(subprocess, "run", _GitStub(rev_parse=128))
        with pytest.raises(SystemExit) as exc:
            cli_server._update()
        assert exc.value.code == 1
        assert "Could not determine current branch" in capsys.readouterr().out

    def test_detached_head_is_treated_as_mainline_and_pin_is_checked_first(
        self, monkeypatch, git_checkout, capsys
    ) -> None:
        stub = _GitStub()

        def _run(argv, **kw):
            if argv[:2] == ["git", "rev-parse"]:
                return subprocess.CompletedProcess(argv, 0, "HEAD\n", "")
            return stub(argv, **kw)

        monkeypatch.setattr(subprocess, "run", _run)
        monkeypatch.setattr(
            "kiro_crew.platform.update_governance.update_blocked_reason",
            lambda url: "remote not on the fleet allowlist",
        )
        with pytest.raises(SystemExit) as exc:
            cli_server._update()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "blocked by security policy" in out
        assert "fleet allowlist" in out
        # Blocked before any fetch touched the tree.
        assert not any(c[:2] == ["git", "fetch"] for c in stub.calls)

    def test_fetch_failure_exits(self, monkeypatch, git_checkout, capsys) -> None:
        monkeypatch.setattr(subprocess, "run", _GitStub(fetch=1))
        with pytest.raises(SystemExit) as exc:
            cli_server._update()
        assert exc.value.code == 1
        assert "git fetch failed" in capsys.readouterr().out

    def test_no_new_commits_returns_without_resetting(
        self, monkeypatch, git_checkout, capsys
    ) -> None:
        stub = _GitStub(diff=0)
        monkeypatch.setattr(subprocess, "run", stub)
        cli_server._update()
        assert "Already up to date" in capsys.readouterr().out
        assert not any(c[:2] == ["git", "reset"] for c in stub.calls)

    def test_local_changes_prompt_and_abort_leaves_tree_alone(
        self, monkeypatch, git_checkout, capsys
    ) -> None:
        stub = _GitStub()
        stub.status_out = " M src/a.py\n?? scratch.txt\n"
        monkeypatch.setattr(subprocess, "run", stub)
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")
        with pytest.raises(SystemExit) as exc:
            cli_server._update()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "will be discarded" in out
        assert " M src/a.py" in out
        assert "?? scratch.txt" not in out  # untracked files are preserved, not listed
        assert "Aborted." in out
        assert not any(c[:2] == ["git", "reset"] for c in stub.calls)

    def test_local_changes_confirmed_proceeds_to_reset(
        self, monkeypatch, git_checkout, capsys
    ) -> None:
        stub = _GitStub()
        stub.status_out = " M src/a.py\n"
        monkeypatch.setattr(subprocess, "run", stub)
        monkeypatch.setattr("builtins.input", lambda prompt="": "Y")
        cli_server._update()
        assert any(c[:2] == ["git", "reset"] for c in stub.calls)
        assert "Kiro Crew updated!" in capsys.readouterr().out

    def test_reset_failure_exits(self, monkeypatch, git_checkout, capsys) -> None:
        monkeypatch.setattr(subprocess, "run", _GitStub(reset=1))
        with pytest.raises(SystemExit) as exc:
            cli_server._update()
        assert exc.value.code == 1
        assert "git reset failed" in capsys.readouterr().out

    def test_pip_install_failure_exits(self, monkeypatch, git_checkout, capsys) -> None:
        monkeypatch.setattr(subprocess, "run", _GitStub(pip=1))
        with pytest.raises(SystemExit) as exc:
            cli_server._update()
        assert exc.value.code == 1
        assert "Install failed" in capsys.readouterr().out

    def test_success_updates_kiro_cli_and_refreshes_agent_config(
        self, monkeypatch, git_checkout, capsys
    ) -> None:
        stub = _GitStub()
        monkeypatch.setattr(subprocess, "run", stub)
        monkeypatch.setattr(cli_server.shutil, "which", lambda name: "/usr/bin/kiro-cli")
        built: list[Path] = []
        monkeypatch.setattr(cli_server, "build_frontend_sync", lambda p: built.append(p))
        cli_server._update()
        out = capsys.readouterr().out
        assert "Kiro Crew updated!" in out
        assert "Agent config refreshed" in out
        assert built == [git_checkout]
        assert ["kiro-cli", "update"] in stub.calls
        assert any("setup" in c for c in stub.calls)

    def test_agent_config_refresh_failure_only_warns(
        self, monkeypatch, git_checkout, capsys
    ) -> None:
        monkeypatch.setattr(subprocess, "run", _GitStub(setup=1))
        cli_server._update()  # non-fatal
        out = capsys.readouterr().out
        assert "Kiro Crew updated!" in out
        assert "Agent config refresh failed" in out


class TestUpdateWheelDispatch:
    """No git checkout means the wheel path gets a correctly shaped layout."""

    def test_layout_is_built_from_the_distribution(self, monkeypatch, capsys) -> None:
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
        # Both bindings: the capability derives WHO owns the update from the
        # stamp, and cli_server names the layout kind from its own import.
        monkeypatch.setattr("kiro_crew.platform.update_capability.distribution", lambda: "wheel")
        monkeypatch.setattr("kiro_crew.cli_server.distribution", lambda: "wheel")
        seen: list[InstallLayout] = []
        monkeypatch.setattr(cli_server, "_update_wheel", lambda layout: seen.append(layout))
        cli_server._update()
        assert len(seen) == 1
        assert seen[0].kind == "wheel"
        assert seen[0].is_git is False
        assert seen[0].is_externally_managed is False

    def test_unknown_distribution_defaults_to_wheel(self, monkeypatch) -> None:
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
        monkeypatch.setattr("kiro_crew.platform.update_capability.distribution", lambda: "")
        monkeypatch.setattr("kiro_crew.cli_server.distribution", lambda: "")
        seen: list[InstallLayout] = []
        monkeypatch.setattr(cli_server, "_update_wheel", lambda layout: seen.append(layout))
        cli_server._update()
        assert seen[0].kind == "wheel"


# --------------------------------------------------------------------------
# _update_wheel — feed validation and installer failures
# --------------------------------------------------------------------------


_LAYOUT = InstallLayout(
    kind="wheel", proj="", is_git=False, is_externally_managed=False, guidance=""
)


@pytest.fixture
def wheel_feed(monkeypatch):
    """Pin the CDN bases, channel, and installer command; return a payload setter."""
    monkeypatch.setattr(
        "kiro_crew.platform.update_layout.cdn_bases",
        lambda: ("https://cdn.example.com", "https://cdn.example.com"),
    )
    monkeypatch.setattr("kiro_crew.platform.update_layout.release_channel", lambda: "stable")
    monkeypatch.setattr(
        "kiro_crew.platform.update_layout.wheel_update_command",
        lambda channel=None: "curl -fsSL https://cdn.example.com/cli.sh | sh",
    )
    monkeypatch.setattr(
        "kiro_crew.platform.update_governance.update_blocked_reason", lambda url: ""
    )

    def _no_network(*a, **k):
        raise AssertionError("the feed must not be fetched on this path")

    # Default-deny so a validation branch that is supposed to exit BEFORE the
    # fetch can never reach the real CDN from a test runner.
    monkeypatch.setattr("urllib.request.urlopen", _no_network)

    def _serve(payload: bytes) -> None:
        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp(payload))

    return _serve


class TestUpdateWheelFeedValidation:
    """A hostile or broken feed must never reach the installer."""

    def test_pinned_feed_base_blocks_the_update(self, monkeypatch, wheel_feed, capsys) -> None:
        monkeypatch.setattr(
            "kiro_crew.platform.update_governance.update_blocked_reason",
            lambda url: "source pinned to an internal mirror",
        )
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        assert "internal mirror" in capsys.readouterr().out

    def test_pinned_artifact_base_blocks_even_when_the_feed_is_allowed(
        self, monkeypatch, wheel_feed, capsys
    ) -> None:
        monkeypatch.setattr(
            "kiro_crew.platform.update_layout.cdn_bases",
            lambda: ("https://feed.example.com", "https://artifacts.example.com"),
        )
        monkeypatch.setattr(
            "kiro_crew.platform.update_governance.update_blocked_reason",
            lambda url: "" if "feed" in url else "artifact host not allowed",
        )
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        assert "artifact host not allowed" in capsys.readouterr().out

    def test_cdn_base_with_shell_metacharacters_is_refused(
        self, monkeypatch, wheel_feed, capsys
    ) -> None:
        monkeypatch.setattr(
            "kiro_crew.platform.update_layout.cdn_bases",
            lambda: ("https://cdn.example.com;id", "https://cdn.example.com"),
        )
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        assert "disallowed characters" in capsys.readouterr().out

    def test_oversized_feed_is_refused(self, wheel_feed, capsys) -> None:
        wheel_feed(b"x" * (65536 + 10))
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        assert "too large" in capsys.readouterr().out

    def test_non_json_feed_is_refused(self, wheel_feed, capsys) -> None:
        wheel_feed(b"<html>404</html>")
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        assert "not valid JSON" in capsys.readouterr().out

    def test_json_array_feed_is_refused(self, wheel_feed, capsys) -> None:
        wheel_feed(b"[]")
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        assert "unexpected format" in capsys.readouterr().out

    def test_channel_mismatch_is_refused(self, wheel_feed, capsys) -> None:
        wheel_feed(
            b'{"schema": "kirocrew-cli-artifact-manifest-v1", '
            b'"channel": "insider", "version": "9.9.9"}'
        )
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        assert "Feed channel mismatch" in capsys.readouterr().out

    def test_missing_version_is_refused(self, wheel_feed, capsys) -> None:
        wheel_feed(b'{"schema": "kirocrew-cli-artifact-manifest-v1", "channel": "stable"}')
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        assert "No version in release feed" in capsys.readouterr().out


class TestUpdateWheelInstaller:
    """Once the feed is trusted, the installer's failure modes stay actionable."""

    @pytest.fixture(autouse=True)
    def _newer_feed(self, wheel_feed, monkeypatch):
        wheel_feed(
            b'{"schema": "kirocrew-cli-artifact-manifest-v1", '
            b'"channel": "stable", "version": "999.0.0"}'
        )
        monkeypatch.setattr(sys, "platform", "linux")

    def test_windows_refuses_the_posix_installer(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "not supported on Windows" in out
        assert "cli.sh | sh" in out  # manual command still printed

    def test_missing_sh_prints_the_manual_command(self, monkeypatch, capsys) -> None:
        def boom(*a, **k):
            raise FileNotFoundError("sh")

        monkeypatch.setattr(subprocess, "run", boom)
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "'sh' not found" in out
        assert "cli.sh | sh" in out

    def test_installer_timeout_prints_the_manual_command(self, monkeypatch, capsys) -> None:
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="sh", timeout=300)

        monkeypatch.setattr(subprocess, "run", boom)
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        assert "timed out" in capsys.readouterr().out

    def test_installer_nonzero_exit_is_surfaced(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 17)
        )
        with pytest.raises(SystemExit) as exc:
            cli_server._update_wheel(_LAYOUT)
        assert exc.value.code == 1
        assert "exited with code 17" in capsys.readouterr().out

    def test_success_reports_the_new_version_and_restart_hint(self, monkeypatch, capsys) -> None:
        seen: list[list[str]] = []

        def _run(argv, **kw):
            seen.append(list(argv))
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr(subprocess, "run", _run)
        cli_server._update_wheel(_LAYOUT)
        out = capsys.readouterr().out
        assert "updated to 999.0.0" in out
        assert "kirocrew restart" in out
        assert seen[0][:2] == ["sh", "-c"]

    def test_unparseable_remote_version_updates_anyway(
        self, monkeypatch, wheel_feed, capsys
    ) -> None:
        """``_is_newer`` returning None must fail OPEN — an update is safer than a stall."""
        wheel_feed(
            b'{"schema": "kirocrew-cli-artifact-manifest-v1", '
            b'"channel": "stable", "version": "not-a-version"}'
        )
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0)
        )
        cli_server._update_wheel(_LAYOUT)
        out = capsys.readouterr().out
        assert "Could not compare versions" in out
        assert "updated to not-a-version" in out

    def test_already_latest_returns_without_running_the_installer(
        self, monkeypatch, wheel_feed, capsys
    ) -> None:
        from kiro_crew import __version__ as local_version

        wheel_feed(
            b'{"schema": "kirocrew-cli-artifact-manifest-v1", "channel": "stable", '
            + f'"version": "{local_version}"'.encode()
            + b"}"
        )

        def unreachable(*a, **k):  # pragma: no cover - proves the early return
            raise AssertionError("installer must not run when already current")

        monkeypatch.setattr(subprocess, "run", unreachable)
        cli_server._update_wheel(_LAYOUT)
        assert "Already on the latest version" in capsys.readouterr().out
