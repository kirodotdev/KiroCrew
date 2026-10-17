"""Tests for the Linux service's control paths and its status, stop and restart.

The basic install, uninstall, stop, status and is_active paths and the sudo
helper, the scope each verb reports on and acts in across both systemd scopes, the
restart confirmation window, the two health predicates, the alias holder, and
the stale loaded unit ``service status`` names.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest
from service_helpers import (  # noqa: F401 -- fixtures are found by name
    _DEAD,
    _NO_BUS,
    _OURS_UNIT_TEXT,
    _RUNNING,
    _UNIT,
    _clear_sudo_user,
    _fake_systemctl,
    _no_host_env_file,
    _no_host_unit_files,
    _restart_settle_without_waiting,
)

from kiro_crew.service.common import (
    RESTART_NOT_UP,
    RESTART_REFUSED,
    RESTART_UNCONFIRMED,
    SERVICE_NAME,
    Platform,
)


class TestLinuxControlPaths:
    """Cover uninstall, stop, status, is_active, and the sudo helper paths."""

    def test_uninstall_runs_full_teardown_when_unit_exists(self, tmp_path, monkeypatch):
        from kiro_crew.service import linux as svc_linux

        # Point UNIT_PATH at a real temp file so ``UNIT_PATH.exists()``
        # is True without monkeypatching ``Path.exists`` globally (which
        # would also affect pytest/fixture machinery).
        unit_path = tmp_path / "kirocrew.service"
        unit_path.write_text("")
        data_home = tmp_path / "crew-home"
        data_home.mkdir()
        sentinel = data_home / "memory.db"
        sentinel.write_text("user data")
        monkeypatch.setenv("KIROCREW_HOME", str(data_home))
        monkeypatch.setattr(svc_linux, "UNIT_PATH", unit_path)
        # A unit the system manager has LOADED (stopped): the teardown must stop
        # and disable it before the file goes.
        run = _fake_systemctl(system=_DEAD, user=None)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            svc_linux.uninstall()
        called = run.calls
        # Each step must use sudo since /etc/systemd/system requires root.
        assert ["sudo", "systemctl", "stop", f"{SERVICE_NAME}.service"] in called
        assert ["sudo", "systemctl", "disable", f"{SERVICE_NAME}.service"] in called
        assert any(
            c[:3] == ["sudo", "rm", "-f"] for c in called
        ), f"expected sudo rm of unit file; got {called}"
        assert ["sudo", "systemctl", "daemon-reload"] in called
        assert sentinel.read_text() == "user data"

    def test_is_active_returns_true_when_systemctl_says_active(self):
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=_RUNNING, user=None)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            assert svc_linux.is_active() is True
        # is_active must NOT use sudo (state is queryable as a regular user).
        assert all(
            "sudo" not in c for c in run.calls
        ), f"is_active must not call sudo; got {run.calls}"

    def test_is_active_returns_false_when_inactive(self):
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=_DEAD, user=None)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            assert svc_linux.is_active() is False

    def test_stop_invokes_systemctl_stop(self):
        from kiro_crew.service import linux as svc_linux

        # stop() acts on the scope(s) running the unit, so the system unit must
        # answer `show` first (unprivileged); the stop itself still goes through sudo.
        run = _fake_systemctl(system=_RUNNING, user=None)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            svc_linux.stop()
        assert ["sudo", "systemctl", "stop", f"{SERVICE_NAME}.service"] in run.calls
        assert not any("--user" in c and "stop" in c for c in run.calls), run.calls

    def test_restart_returns_true_on_success(self):
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=_RUNNING, user=None)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.restart()
        assert report.ok is True and [o.scope for o in report.outcomes] == ["system"]
        assert ["sudo", "systemctl", "restart", f"{SERVICE_NAME}.service"] in run.calls

    def test_restart_returns_false_on_nonzero_exit(self):
        # An unprivileged / failed systemctl restart exits non-zero (systemd
        # refuses a system-scope restart without root). restart() must report
        # that failure, not swallow it -- this is the crux of the false-success
        # bug: the outcome has to reach restart_service() and its caller.
        from kiro_crew.service import linux as svc_linux

        refused = subprocess.CompletedProcess([], 1, "", "Interactive authentication required")
        run = _fake_systemctl(system=_RUNNING, user=None, overrides={"restart": refused})
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.restart()
        assert report.ok is False
        (failure,) = report.failures
        assert failure.scope == "system" and failure.kind == RESTART_REFUSED
        assert "Interactive authentication required" in failure.reason
        assert failure.hint == "sudo systemctl restart kirocrew"
        assert ["sudo", "systemctl", "restart", f"{SERVICE_NAME}.service"] in run.calls

    def test_restart_invokes_systemctl_restart_atomic(self):
        # systemctl restart is preferred over stop+start: it's a single
        # atomic operation, smaller down-window, and the supervisor
        # stays in charge of the lifecycle the whole time.
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=_RUNNING, user=None)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            svc_linux.restart()
        called = run.calls
        assert ["sudo", "systemctl", "restart", f"{SERVICE_NAME}.service"] in called
        # And critically, NOT a stop+start pair — that would widen the
        # down-window and lose atomicity.
        assert not any(
            c[:3] == ["sudo", "systemctl", "stop"] for c in called
        ), f"restart() should be atomic, not stop+start; got {called}"

    def test_status_returns_systemctl_output(self):
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=_RUNNING, user=None)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            out = svc_linux.status()
        # The scope with a unit carries its `systemctl status` block.
        assert f"● {SERVICE_NAME}.service - Kiro Crew gateway (system scope block)" in out
        # status() must NOT use sudo.
        assert all("sudo" not in c for c in run.calls)

    def test_status_falls_back_to_stderr_when_stdout_empty(self):
        from kiro_crew.service import linux as svc_linux

        quiet = subprocess.CompletedProcess([], 3, "", "status printed to stderr\n")
        run = _fake_systemctl(system=_RUNNING, user=None, overrides={"status": quiet})
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            out = svc_linux.status()
        assert "status printed to stderr" in out

    def _run_responder(self, *steps_and_results: tuple):
        """Helper: route subprocess.run by inspecting the command being run.

        Each step is (argv_token_to_match, result_mock). The first step whose
        token equals one of the command's argv elements is returned. Anything
        unmatched returns a default-success mock.

        This is more robust than a positional list because ``render_unit``
        also calls ``subprocess.run`` (for ``id -gn``), and the count of
        calls during install is not stable.

        The match is whole-token, never substring: the unit-file write's argv
        carries a ``tempfile.mkstemp`` path, and that path inherits ``TMPDIR``,
        which the suite's per-test mode names after the test's own nodeid. A
        substring match on ``"restart"`` would then select the *write* step of
        ``test_install_propagates_failure_at_restart`` and the failure under
        test would never be reached.
        """
        ok = MagicMock(returncode=0, stdout="", stderr="")

        def respond(cmd_list, *_a, **_k):
            # subprocess.run is called positionally as run([...], **kwargs).
            # MagicMock side_effect receives the same args, so cmd_list is
            # the list of argv strings.
            tokens = list(cmd_list) if isinstance(cmd_list, list) else [str(cmd_list)]
            for needle, result in steps_and_results:
                if needle in tokens:
                    return result
            return ok

        return respond

    def test_install_propagates_failure_at_daemon_reload(self, monkeypatch):
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "tester")
        reload_failed = MagicMock(returncode=1, stdout="", stderr="systemctl: bad config")
        responder = self._run_responder(("daemon-reload", reload_failed))

        with (
            patch(
                "kiro_crew.service.common.shutil.which",
                return_value="/usr/local/bin/kirocrew",
            ),
            patch("kiro_crew.service.linux.subprocess.run", side_effect=responder),
        ):
            with pytest.raises(svc_linux.ServiceInstallError) as exc_info:
                svc_linux.install()
        assert "daemon-reload" in str(exc_info.value)

    def test_install_propagates_failure_at_enable(self, monkeypatch):
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "tester")
        enable_failed = MagicMock(returncode=1, stdout="", stderr="enable failed: unit invalid")
        responder = self._run_responder(
            ("enable", enable_failed),
        )

        with (
            patch(
                "kiro_crew.service.common.shutil.which",
                return_value="/usr/local/bin/kirocrew",
            ),
            patch("kiro_crew.service.linux.subprocess.run", side_effect=responder),
        ):
            with pytest.raises(svc_linux.ServiceInstallError) as exc_info:
                svc_linux.install()
        assert "enable" in str(exc_info.value)

    def test_install_propagates_failure_at_restart(self, monkeypatch):
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "tester")
        restart_failed = MagicMock(returncode=1, stdout="", stderr="job failed")
        responder = self._run_responder(("restart", restart_failed))

        with (
            patch(
                "kiro_crew.service.common.shutil.which",
                return_value="/usr/local/bin/kirocrew",
            ),
            patch("kiro_crew.service.linux.subprocess.run", side_effect=responder),
        ):
            with pytest.raises(svc_linux.ServiceInstallError) as exc_info:
                svc_linux.install()
        # Error should mention restart and journalctl pointer for debugging.
        msg = str(exc_info.value)
        assert "restart" in msg
        assert "journalctl" in msg

    def test_current_group_falls_back_to_username_when_id_fails(self, monkeypatch):
        """If `id -gn` is missing or errors, fall back to using the username
        as the group name. Better to fail loudly at systemd start than to
        guess wrong here."""
        from kiro_crew.service import linux as svc_linux

        # `id` resolves to a trusted path, but the exec itself errors.
        monkeypatch.setattr(svc_linux, "trusted_system_bin", lambda _n: "/usr/bin/id")
        with patch(
            "kiro_crew.service.linux.subprocess.run",
            side_effect=FileNotFoundError("id"),
        ):
            assert svc_linux._current_group("alice") == "alice"

    def test_current_group_falls_back_when_id_not_in_trusted_dirs(self, monkeypatch):
        """When `id` is not in a trusted system directory the probe never shells
        out (a PATH shim must not run as root); it falls back to the username."""
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setattr(svc_linux, "trusted_system_bin", lambda _n: None)
        assert svc_linux._current_group("alice") == "alice"


class TestLinuxServiceScopesStatus:
    """``status`` / ``is_active`` / ``uninstall`` (and the ``stop`` / ``restart``
    that ``is_active`` gates) see BOTH systemd scopes and name the one they
    report on, so a gateway running as the SELinux remedy's user unit is never
    reported as a dead system unit.

    The systemctl runner is mocked in every test: the systemd user manager is
    host state, and a real ``systemctl --user`` from here would touch the
    operator's own units.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture on the shared monkeypatch, moved verbatim; tracked for a de-flake pass
    def _not_root(self, monkeypatch, tmp_path):
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setattr(svc_linux.os, "geteuid", lambda: 1000, raising=False)
        monkeypatch.setenv("USER", "tester")
        # No system unit file unless a test writes one: uninstall's system-scope
        # probe is a stat of this path. The per-user unit's own path is the
        # remedy's layout under tmp_path, so a test that writes there owns the
        # file by path and one that writes elsewhere owns it by the marker.
        monkeypatch.setattr(svc_linux, "UNIT_PATH", tmp_path / "absent" / _UNIT)
        monkeypatch.setattr(
            svc_linux,
            "user_unit_file_path",
            lambda: tmp_path / ".config" / "systemd" / "user" / _UNIT,
        )

    @staticmethod
    def _user_calls(run):
        return [c for c in run.calls if "--user" in c]

    # -- status -------------------------------------------------------------

    def test_status_names_a_running_user_unit_when_the_system_unit_is_absent(self):
        """The reported bug: a running user-scope gateway read as 'inactive (dead)'."""
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=None, user=_RUNNING)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            out = svc_linux.status()
            active = svc_linux.is_active()

        assert "system scope: not installed" in out
        assert "user scope: active (running)" in out
        assert "inactive (dead)" not in out, out
        assert active is True
        assert any(c[:3] == ["systemctl", "--user", "show"] for c in run.calls), run.calls
        assert all("sudo" not in c for c in run.calls), run.calls

    def test_status_reports_both_scopes_as_not_installed(self):
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=None, user=None)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            out = svc_linux.status()
            active = svc_linux.is_active()

        assert "system scope: not installed" in out
        assert "user scope: not installed" in out
        assert "inactive" not in out, out
        assert "could not be found" not in out, out
        assert active is False

    def test_status_reports_an_unreachable_user_bus_as_not_reachable(self):
        """No session bus (root without a login session, a stripped environment):
        the user scope is 'not reachable from this shell', never 'inactive'."""
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=_RUNNING, user_bus_error=_NO_BUS)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            out = svc_linux.status()
            active = svc_linux.is_active()

        assert "system scope: active (running)" in out
        assert "user scope: not reachable from this shell" in out
        assert _NO_BUS in out
        assert "user scope: inactive" not in out
        assert "user scope: not installed" not in out
        assert active is True

    def test_status_keeps_the_system_block_and_adds_the_user_line(self):
        """A system unit that is active keeps its `systemctl status` block."""
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=_RUNNING, user=None)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            out = svc_linux.status()

        system_line = out.index("system scope: active (running)")
        block = out.index(f"● {_UNIT} - Kiro Crew gateway (system scope block)")
        user_line = out.index("user scope: not installed")
        assert system_line < block < user_line, out
        assert any(c[:3] == ["systemctl", "status", _UNIT] for c in run.calls), run.calls
        assert all("sudo" not in c for c in run.calls), run.calls

    def test_status_shows_an_installed_but_stopped_system_unit_beside_the_user_unit(self):
        """The reporter's host: a disabled system unit AND the real user unit."""
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=_DEAD, user=_RUNNING)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            out = svc_linux.status()
            active = svc_linux.is_active()

        assert "system scope: inactive (dead)" in out
        assert "user scope: active (running)" in out
        assert f"● {_UNIT} - Kiro Crew gateway (user scope block)" in out
        assert active is True

    def test_the_user_scope_spawn_carries_the_shared_backfilled_bus_env(
        self, monkeypatch, tmp_path
    ):
        """A shell spawned from a system unit (the gateway's own) inherits neither
        `XDG_RUNTIME_DIR` nor `DBUS_SESSION_BUS_ADDRESS`; the pod runtime already
        backfills both from the account's runtime dir when the bus socket exists.
        The service module's `systemctl --user` goes through the SAME resolver, so
        an `unreachable` reading is a host fact, never a missing-variable artifact.
        The system-scope spawn is left with the inherited environment."""
        from kiro_crew.pod import runtime as pod_runtime
        from kiro_crew.service import common as svc_common
        from kiro_crew.service import linux as svc_linux

        runtime_dir = tmp_path / "run"
        runtime_dir.mkdir()
        (runtime_dir / "bus").touch()
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        inner = _fake_systemctl(system=None, user=_RUNNING)
        envs: list[tuple[list[str], dict | None]] = []

        def run(argv, *a, **kw):
            envs.append((list(argv), kw.get("env")))
            return inner(argv, *a, **kw)

        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            svc_linux.status()

        user_envs = [env for argv, env in envs if "--user" in argv]
        assert user_envs, envs
        for env in user_envs:
            assert env is not None
            assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={runtime_dir / 'bus'}"
            assert env["XDG_RUNTIME_DIR"] == str(runtime_dir)
        assert all(env is None for argv, env in envs if "--user" not in argv), envs
        # One resolver, two callers: the pod runtime's env is the same mapping plus
        # its own locale pin, so the two spawn sites cannot diverge on the bus again.
        shared = svc_common.systemctl_user_env()
        assert pod_runtime._systemctl_env() == {**shared, "LC_ALL": "C"}
        assert pod_runtime._session_runtime_dir is svc_common.session_runtime_dir

    def test_status_never_queries_a_user_scope_from_a_root_shell(self, monkeypatch):
        """Under sudo the process is root: `systemctl --user` would reach ROOT's
        manager, not the account's, so the scope is reported unreachable and no
        user-scope systemctl is spawned at all."""
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setattr(svc_linux.os, "geteuid", lambda: 0, raising=False)
        monkeypatch.setenv("SUDO_USER", "tester")
        run = _fake_systemctl(system=None, user=_RUNNING)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            out = svc_linux.status()
            active = svc_linux.is_active()

        assert "user scope: not reachable from this shell" in out
        assert "tester" in out
        assert self._user_calls(run) == [], run.calls
        assert active is False

    # -- stop / restart follow the widened is_active -------------------------

    def test_stop_acts_on_the_scope_where_the_unit_runs(self):
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=None, user=_RUNNING)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            svc_linux.stop()

        assert ["systemctl", "--user", "stop", _UNIT] in run.calls, run.calls
        assert all("sudo" not in c for c in run.calls), run.calls

    def test_restart_acts_on_the_scope_where_the_unit_runs(self):
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=None, user=_RUNNING)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.restart()
        assert report.ok is True and [o.scope for o in report.outcomes] == ["user"]

        assert ["systemctl", "--user", "restart", _UNIT] in run.calls, run.calls
        assert all("sudo" not in c for c in run.calls), run.calls

    def test_restart_reports_false_when_no_scope_runs_the_unit(self):
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=None, user=None)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.restart()
        assert not report and report.attempted is False

        assert not any("restart" in c for c in run.calls), run.calls

    # -- restart confirms the unit came back up ------------------------------

    _LOOPING = {"ActiveState": "activating", "SubState": "auto-restart", "Result": "exit-code"}

    def test_restart_into_a_crash_loop_is_a_failure_not_a_refusal(self):
        """`Type=simple`: `systemctl --user restart` exits 0 the moment the process
        is forked, and a gateway that exits on start is then in `activating
        (auto-restart)`. That is a FAILED restart with the unit's real state and
        Result, not success — and not a refusal either: no restart hint, since a
        hand-run restart fails the same way; the remedy is the journal."""
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=None, user=_RUNNING, restart_lands_in=self._LOOPING)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.restart()
            # The reach predicate still sees the unit: `kirocrew stop` can reach it.
            assert svc_linux.is_active() is True
            assert svc_linux.is_up() is False

        assert ["systemctl", "--user", "restart", _UNIT] in run.calls, run.calls
        assert report.ok is False and report.attempted is True
        (failure,) = report.failures
        assert failure.scope == "user"
        assert failure.kind == RESTART_NOT_UP
        assert failure.hint == "journalctl --user -u kirocrew.service -n 50 --no-pager"
        assert "activating (auto-restart)" in failure.reason
        assert "last result: exit-code" in failure.reason
        assert "sudo" not in failure.reason

    def test_restart_keeps_reading_past_a_first_active_answer(self):
        """The fork-then-die shape: the first `show` after the restart still says
        `active (running)` (the SIGCHLD has not landed), the next one
        `activating (auto-restart)`. One reading is not confirmation; the window
        is walked and the death inside it is reported."""
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(
            system=None, user=_RUNNING, restart_lands_in=[_RUNNING, self._LOOPING]
        )
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.restart()

        assert report.ok is False
        assert "activating (auto-restart)" in report.failures[0].reason
        assert svc_linux.time.sleeps, "the settle window was not walked"

    def test_restart_waits_for_a_unit_still_starting(self):
        """`activating (start)` is a unit on its way up (a Type=notify/forking
        edit of the unit): not a failure, wait for it. Up by the deadline → ok."""
        from kiro_crew.service import linux as svc_linux

        starting = {"ActiveState": "activating", "SubState": "start"}
        run = _fake_systemctl(
            system=None, user=_RUNNING, restart_lands_in=[starting, starting, _RUNNING]
        )
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.restart()

        assert report.ok is True
        assert len(svc_linux.time.sleeps) >= 2

    def test_restart_reports_a_unit_still_starting_at_the_deadline(self):
        from kiro_crew.service import linux as svc_linux

        starting = {"ActiveState": "activating", "SubState": "start"}
        run = _fake_systemctl(system=None, user=_RUNNING, restart_lands_in=starting)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.restart()

        assert report.ok is False
        (failure,) = report.failures
        assert failure.kind == RESTART_NOT_UP
        assert "still activating (start)" in failure.reason
        assert f"{svc_linux._RESTART_SETTLE_SECS:g}s after the restart" in failure.reason
        # The whole window was walked before giving up.
        assert sum(svc_linux.time.sleeps) >= svc_linux._RESTART_SETTLE_SECS

    def test_restart_confirmed_up_stays_ok_after_the_window(self):
        """A live gateway: `active (running)` at every poll → ok, with the window
        walked (a single immediate reading proves nothing for Type=simple)."""
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=None, user=_RUNNING, restart_lands_in=_RUNNING)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.restart()

        assert report.ok is True
        assert sum(svc_linux.time.sleeps) >= svc_linux._RESTART_SETTLE_SECS

    def test_restart_catches_a_re_exec_the_state_reads_stepped_over(self):
        """`RestartSec=100ms`: the unit dies and is `active` again between two
        0.25 s reads, so every read says `active (running)`. The main process's
        start stamp changed between them, and that is a failed restart."""
        from kiro_crew.service import linux as svc_linux

        first = dict(_RUNNING, Started="5000000")
        again = dict(_RUNNING, Started="5300000")
        run = _fake_systemctl(system=None, user=_RUNNING, restart_lands_in=[first, again])
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.restart()

        assert report.ok is False
        (failure,) = report.failures
        assert failure.kind == RESTART_NOT_UP
        assert failure.hint == "journalctl --user -u kirocrew.service -n 50 --no-pager"
        assert "started again" in failure.reason
        assert "active (running)" in failure.reason

    def test_restart_with_a_steady_start_stamp_is_ok(self):
        """The same stamp at every read across the window: one exec, still up."""
        from kiro_crew.service import linux as svc_linux

        steady = dict(_RUNNING, Started="5000000")
        run = _fake_systemctl(system=None, user=_RUNNING, restart_lands_in=steady)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.restart()

        assert report.ok is True
        assert sum(svc_linux.time.sleeps) >= svc_linux._RESTART_SETTLE_SECS

    def test_restart_ignores_an_unset_start_stamp(self):
        """`0` is systemd's answer before the main process has ever been
        exec'd; going from `0` to a real stamp is the first exec, not a
        restart."""
        from kiro_crew.service import linux as svc_linux

        unset = dict(_RUNNING, Started="0")
        stamped = dict(_RUNNING, Started="5000000")
        run = _fake_systemctl(system=None, user=_RUNNING, restart_lands_in=[unset, stamped])
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.restart()

        assert report.ok is True

    def test_restart_into_a_start_limit_hit_is_a_failure(self):
        """Three crashes in the burst window: `failed` with Result=start-limit-hit."""
        from kiro_crew.service import linux as svc_linux

        limited = {"ActiveState": "failed", "SubState": "failed", "Result": "start-limit-hit"}
        run = _fake_systemctl(system=_RUNNING, user=None, restart_lands_in=limited)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.restart()

        assert report.ok is False
        (failure,) = report.failures
        assert failure.scope == "system" and failure.kind == RESTART_NOT_UP
        assert failure.hint == "sudo journalctl -u kirocrew.service -n 50 --no-pager"
        assert "failed (failed)" in failure.reason
        assert "start-limit-hit" in failure.reason

    def test_restart_reports_a_manager_that_vanished_mid_window_as_unconfirmed(self):
        """The bus goes away between the restart and the re-read: the unit's
        health is UNKNOWN — not "exits on start", not "restarted" — so the kind
        is unconfirmed and the remedy is to look, not a journal or a restart."""
        from kiro_crew.service import linux as svc_linux

        inner = _fake_systemctl(system=None, user=_RUNNING)

        def run(argv, *a, **k):
            tokens = list(argv)
            if (
                "--user" in tokens
                and "show" in tokens
                and "restart" in {t for c in inner.calls for t in c}
            ):
                inner.calls.append(tokens)
                return subprocess.CompletedProcess(tokens, 1, "", _NO_BUS + "\n")
            return inner(argv, *a, **k)

        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.restart()

        assert report.ok is False
        (failure,) = report.failures
        assert failure.kind == RESTART_UNCONFIRMED
        assert failure.hint == "kirocrew service status"
        assert "stopped answering after the restart" in failure.reason
        assert _NO_BUS in failure.reason
        assert "exits" not in failure.reason

    def test_a_nonzero_restart_that_left_the_unit_up_is_a_refusal(self):
        """`systemctl restart` exits non-zero and the unit is exactly as it was
        (still active): the manager did not run the job — an authorization
        refusal — so the remedy is the same restart with the privilege it needs."""
        from kiro_crew.service import linux as svc_linux

        refused = subprocess.CompletedProcess([], 1, "", "Interactive authentication required")
        run = _fake_systemctl(system=_RUNNING, user=None, overrides={"restart": refused})
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.restart()

        (failure,) = report.failures
        assert failure.kind == RESTART_REFUSED
        assert failure.hint == "sudo systemctl restart kirocrew"
        assert "refused the restart: Interactive authentication required" in failure.reason
        # Classified by the unit's state after the exit: one extra `show`, no sudo.
        assert [c for c in run.calls if "show" in c and "sudo" in c] == []

    def test_the_report_hint_does_not_depend_on_the_host_running_the_module(self, monkeypatch):
        """The module drives systemd by construction, so a system-scope refusal
        names `sudo systemctl restart kirocrew` whatever `current_platform()`
        says about the PROCESS building the report — the Windows and macOS test
        shards answer UNSUPPORTED / LAUNCHD there and must read the same hint."""
        from kiro_crew.service import common as svc_common
        from kiro_crew.service import linux as svc_linux

        refused = subprocess.CompletedProcess([], 1, "", "Interactive authentication required")
        run = _fake_systemctl(system=_RUNNING, user=_RUNNING, overrides={"restart": refused})
        for plat in (Platform.UNSUPPORTED, Platform.LAUNCHD):
            monkeypatch.setattr(svc_common, "current_platform", lambda p=plat: p)
            with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
                report = svc_linux.restart()
            assert [f.hint for f in report.failures] == [
                "sudo systemctl restart kirocrew",
                "systemctl --user restart kirocrew",
            ], plat

    def test_a_manager_that_vanishes_after_stop_keeps_the_unit_file(self, tmp_path):
        """`stop` returned 0, then the re-read that must confirm the unit stopped
        got no answer (the bus went away). No answer is not "not running": the
        unlink rests on the manager's word, so the file stays and the scope is
        unfinished — never an unverified unlink."""
        from kiro_crew.service import linux as svc_linux

        fragment = tmp_path / ".config" / "systemd" / "user" / _UNIT
        fragment.parent.mkdir(parents=True)
        fragment.write_text(_OURS_UNIT_TEXT, encoding="utf-8")
        inner = _fake_systemctl(system=None, user=_RUNNING, user_fragment=str(fragment))

        def run(argv, *a, **k):
            tokens = list(argv)
            stopped = any("stop" in c for c in inner.calls)
            if "--user" in tokens and "show" in tokens and stopped:
                inner.calls.append(tokens)
                return subprocess.CompletedProcess(tokens, 1, "", _NO_BUS + "\n")
            return inner(argv, *a, **k)

        with (
            patch("kiro_crew.service.linux.subprocess.run", side_effect=run),
            patch.object(
                svc_linux.os, "unlink", side_effect=AssertionError("unlinked without confirmation")
            ),
        ):
            report = svc_linux.uninstall()

        assert fragment.exists()
        assert report.user.startswith("left in place (the user manager stopped answering after")
        assert _NO_BUS in report.user and "was not removed" in report.user
        assert report.unfinished == {"user"}
        assert not any("daemon-reload" in c for c in inner.calls), inner.calls

    def test_a_nonzero_restart_whose_job_ran_and_failed_is_not_a_refusal(self):
        """`systemctl restart` also exits non-zero when the manager RAN the job and
        the job failed (a start limit already hit, an `ExecStartPre=` that
        exits non-zero, a `Type=notify` unit that never signalled): the old
        process is gone and the unit reads `failed`. A privilege-oriented
        "run it yourself" hint would be wrong — the job would fail again — so
        the kind is not-up and the remedy is the journal."""
        from kiro_crew.service import linux as svc_linux

        job_failed = subprocess.CompletedProcess(
            [], 1, "", f"Job for {_UNIT} failed because the control process exited with error code."
        )
        limited = {"ActiveState": "failed", "SubState": "failed", "Result": "start-limit-hit"}
        run = _fake_systemctl(
            system=None, user=_RUNNING, overrides={"restart": job_failed}, restart_lands_in=limited
        )
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.restart()

        (failure,) = report.failures
        assert failure.scope == "user" and failure.kind == RESTART_NOT_UP
        assert failure.hint == "journalctl --user -u kirocrew.service -n 50 --no-pager"
        assert "exited 1 (Job for kirocrew.service failed" in failure.reason
        assert "failed (failed) (last result: start-limit-hit)" in failure.reason
        assert "refused" not in failure.reason

    # -- the two predicates -------------------------------------------------

    @pytest.mark.parametrize("scope", ["system", "user"])
    def test_an_alias_is_never_stopped_restarted_or_counted(self, scope):
        """`Alias=kirocrew.service` on an operator's own unit: `show` on our name
        answers for THAT unit (`Id=shared.service`), and so would `stop` and
        `restart` issued on our name. The canonical-Id guard `uninstall` applies
        is one predicate for every verb: the alias is not selected, not counted
        as running or up, and the headline still says what it is."""
        from kiro_crew.service import linux as svc_linux

        kwargs = (
            dict(system=_RUNNING, user=None, system_id="shared.service")
            if scope == "system"
            else dict(system=None, user=_RUNNING, user_id="shared.service")
        )
        run = _fake_systemctl(**kwargs)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            assert svc_linux.is_active() is False
            assert svc_linux.is_up() is False
            svc_linux.stop()
            report = svc_linux.restart()
            headline = svc_linux.status()

        assert report.attempted is False
        assert not any("stop" in c for c in run.calls), run.calls
        assert not any("restart" in c for c in run.calls), run.calls
        assert f"{scope} scope: active (running), an alias of shared.service" in headline

    def test_a_running_alias_beside_our_stopped_unit_restarts_nothing(self):
        """Our unit stopped in one scope, an alias running in the other: neither
        is acted on — the stopped one is not running, the alias is not ours."""
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=_RUNNING, user=_DEAD, system_id="shared.service")
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.restart()
            assert svc_linux.is_active() is False
        assert report.attempted is False
        assert not any("restart" in c for c in run.calls), run.calls

    def test_an_answer_with_no_canonical_id_fails_closed(self, tmp_path):
        """`Id=` blank in `show`'s answer: the identity every verb would act on
        was never verified. Not counted, not stopped or restarted, and
        `uninstall` leaves the file in place naming why — the guard is exact
        equality with `kirocrew.service`, not "not an alias"."""
        from kiro_crew.service import linux as svc_linux

        fragment = tmp_path / ".config" / "systemd" / "user" / _UNIT
        fragment.parent.mkdir(parents=True)
        fragment.write_text(_OURS_UNIT_TEXT, encoding="utf-8")
        run = _fake_systemctl(system=None, user=_RUNNING, user_id="", user_fragment=str(fragment))
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            assert svc_linux.is_active() is False
            assert svc_linux.is_up() is False
            svc_linux.stop()
            assert svc_linux.restart().attempted is False
            report = svc_linux.uninstall()
        assert fragment.exists()
        assert report.user.startswith("left in place (the manager did not report")
        assert "canonical Id" in report.user
        assert report.unfinished == {"user"}
        assert not any(verb in c for c in run.calls for verb in ("stop", "disable", "restart"))

    def test_logs_predicates_do_not_follow_an_alias(self):
        """`kirocrew logs` picks the user journal off these two answers; a name
        that is an alias of another unit is not the per-user gateway, so neither
        answers True for it."""
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=None, user=_RUNNING, user_id="shared.service")
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            assert svc_linux.user_unit_installed() is False
            assert svc_linux.user_unit_active() is False
        run = _fake_systemctl(system=None, user=_RUNNING)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            assert svc_linux.user_unit_installed() is True
            assert svc_linux.user_unit_active() is True

    @pytest.mark.parametrize(
        "state, reach, up",
        [
            (_RUNNING, True, True),
            ({"ActiveState": "reloading", "SubState": "reload"}, True, True),
            ({"ActiveState": "activating", "SubState": "auto-restart"}, True, False),
            ({"ActiveState": "activating", "SubState": "start"}, True, False),
            ({"ActiveState": "deactivating", "SubState": "stop-sigterm"}, True, False),
            ({"ActiveState": "failed", "SubState": "failed"}, False, False),
            (_DEAD, False, False),
        ],
        ids=["active", "reloading", "auto-restart", "starting", "deactivating", "failed", "dead"],
    )
    def test_is_active_is_reach_and_is_up_is_health(self, state, reach, up):
        """`is_active()` — is there a unit `stop` / `restart` must reach — and
        `is_up()` — is the gateway running right now — are two predicates. They
        agree everywhere but on a unit the manager is between attempts on, or
        mid-transition: reachable, not up. `is_up()` is what `systemctl
        is-active` exits 0 for, so the `service status` exit code keeps the
        meaning the base gave it while reading both scopes."""
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=None, user=state)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            assert svc_linux.is_active() is reach
            assert svc_linux.is_up() is up
        assert all("sudo" not in c for c in run.calls), run.calls

    def test_is_up_reads_either_scope(self):
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=_RUNNING, user=None)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            assert svc_linux.is_up() is True
        run = _fake_systemctl(system=_DEAD, user=_RUNNING)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            assert svc_linux.is_up() is True
        run = _fake_systemctl(system=None, user_bus_error=_NO_BUS)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            assert svc_linux.is_up() is False

    def test_the_selinux_remedy_no_longer_disowns_the_user_unit(self, monkeypatch):
        from kiro_crew.service import linux as svc_linux

        with (
            patch(
                "kiro_crew.service.common.shutil.which",
                return_value="/home/tester/.local/bin/kirocrew",
            ),
            patch.object(svc_linux, "_home_for_user", return_value="/home/tester"),
        ):
            remedy = svc_linux._user_scope_remedy()

        assert "will not see" not in remedy
        assert "only looks at the system unit" not in remedy
        assert "kirocrew service status|uninstall" in remedy

    # -- alias_holder -------------------------------------------------------

    def test_alias_holder_names_the_scope_and_target_of_a_system_scope_alias(self):
        """`show kirocrew.service` on an alias answers for the resolved unit;
        `alias_holder()` reports the scope and that unit's canonical Id so the
        CLI can refuse before it signals the alias target's process."""
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=_RUNNING, user=None, system_id="other.service")
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            holder = svc_linux.alias_holder()

        assert holder == svc_linux.AliasHolder("system", "other.service")
        # The question is answerable without sudo, like status/is_active.
        assert all("sudo" not in c for c in run.calls), run.calls

    def test_alias_holder_names_the_user_scope_when_only_it_is_an_alias(self):
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=None, user=_RUNNING, user_id="other.service")
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            holder = svc_linux.alias_holder()

        assert holder == svc_linux.AliasHolder("user", "other.service")

    def test_alias_holder_prefers_the_system_scope_when_both_are_aliases(self):
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(
            system=_RUNNING, user=_RUNNING, system_id="sys.service", user_id="usr.service"
        )
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            holder = svc_linux.alias_holder()

        assert holder == svc_linux.AliasHolder("system", "sys.service")

    def test_alias_holder_is_none_when_the_alias_target_is_stopped(self):
        """An alias whose target is `inactive`/`failed` is not reported: there is
        no supervised process, so a caller's SIGTERM would not land on another
        unit, and refusing would misdirect — the `systemctl` remedy names a unit
        that is already down while the real foreground gateway keeps running. The
        guard is `is_alias and running`, so a down alias lets the fallback proceed."""
        from kiro_crew.service import linux as svc_linux

        for stopped in (_DEAD, {"ActiveState": "failed", "SubState": "failed"}):
            run = _fake_systemctl(system=stopped, user=None, system_id="other.service")
            with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
                assert svc_linux.alias_holder() is None, stopped

    def test_alias_holder_is_none_for_our_own_unit(self):
        """Our canonical name in both scopes is not an alias — nothing to refuse."""
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=_RUNNING, user=None)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            assert svc_linux.alias_holder() is None

    def test_alias_holder_is_none_when_no_unit_is_installed(self):
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=None, user=None)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            assert svc_linux.alias_holder() is None


class TestLinuxStatusNamesAStaleLoadedUnit:
    """``service status`` asks each scope's manager for the LOADED
    ``RestartPreventExitStatus`` of our unit and, when it does not name
    ``LIVE_HOLDER_EXIT_CODE``, prints one line naming the directive and the
    command that adds it. Nothing is rewritten, reloaded or run under sudo."""

    @pytest.fixture(autouse=True)
    def _not_root(self, _floor_monkeypatch, tmp_path):
        from kiro_crew.service import linux as svc_linux

        _floor_monkeypatch.setattr(svc_linux.os, "geteuid", lambda: 1000, raising=False)
        _floor_monkeypatch.setenv("USER", "tester")
        _floor_monkeypatch.setattr(svc_linux, "UNIT_PATH", tmp_path / "system" / _UNIT)
        user_unit = tmp_path / ".config" / "systemd" / "user" / _UNIT
        _floor_monkeypatch.setattr(svc_linux, "user_unit_file_path", lambda: user_unit)

    @staticmethod
    def _with_loaded(run, *, system=None, user=None):
        """Wrap a ``_fake_systemctl`` so the ``RestartPreventExitStatus`` query
        answers ``system`` / ``user`` per scope (``None``: no property line).

        The values the tests pass are the strings live managers print (systemd
        245 and 252): ``78``, ``1 78``, ``78 TERM`` (a signal prints without its
        ``SIG`` prefix), and an empty value for a unit without the directive."""

        def wrapped(argv, *a, **k):
            tokens = list(argv)
            if "show" in tokens and "RestartPreventExitStatus" in tokens:
                wrapped.calls.append(tokens)
                value = user if "--user" in tokens else system
                body = "" if value is None else f"RestartPreventExitStatus={value}\n"
                return subprocess.CompletedProcess(tokens, 0, body, "")
            result = run(argv, *a, **k)
            wrapped.calls.append(tokens)
            return result

        wrapped.calls = []
        return wrapped

    @staticmethod
    def _status(run):
        from kiro_crew.service import linux as svc_linux

        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            return svc_linux.status()

    def test_a_system_unit_loaded_without_the_exemption_is_named_with_the_reinstall_remedy(self):
        run = self._with_loaded(_fake_systemctl(system=_RUNNING, user=None), system="")
        out = self._status(run)

        lines = out.splitlines()
        idx = next(i for i, line in enumerate(lines) if "lacks" in line)
        assert "`RestartPreventExitStatus=78`" in lines[idx], out
        assert "`sudo kirocrew service install`" in lines[idx], out
        assert lines[idx - 1].startswith("system scope: active (running)"), out
        assert all("sudo" not in c for c in run.calls), run.calls
        assert not any("daemon-reload" in c for c in run.calls), run.calls

    def test_a_user_unit_loaded_without_the_exemption_gets_a_no_sudo_remedy(self, tmp_path):
        fragment = str(tmp_path / "u" / _UNIT)
        run = self._with_loaded(
            _fake_systemctl(system=None, user=_RUNNING, user_fragment=fragment), user=""
        )
        out = self._status(run)

        stale = [line for line in out.splitlines() if "lacks" in line]
        assert len(stale) == 1, out
        assert fragment in stale[0]
        assert "`systemctl --user daemon-reload`" in stale[0]
        assert "sudo" not in stale[0]
        assert not any("daemon-reload" in c for c in run.calls), run.calls

    @pytest.mark.parametrize("loaded", ["78", "78 TERM", "1 78"])
    def test_a_loaded_exemption_in_either_scope_reads_clean(self, loaded):
        run = self._with_loaded(
            _fake_systemctl(system=_RUNNING, user=_RUNNING), system=loaded, user=loaded
        )
        assert "lacks" not in self._status(run)

    def test_a_loaded_value_that_does_not_name_the_live_holder_code_is_reported(self):
        run = self._with_loaded(_fake_systemctl(system=_RUNNING, user=None), system="1 TERM")
        assert "lacks `RestartPreventExitStatus=78`" in self._status(run)

    def test_no_property_line_makes_no_claim(self):
        run = self._with_loaded(_fake_systemctl(system=_RUNNING, user=None), system=None)
        assert "lacks" not in self._status(run)

    @pytest.mark.parametrize(
        "kwargs",
        [{"system_load": "masked"}, {"system_id": "shared.service"}],
        ids=["masked", "alias"],
    )
    def test_a_unit_that_is_not_loaded_or_not_ours_is_not_asked(self, kwargs):
        run = self._with_loaded(_fake_systemctl(system=_RUNNING, user=None, **kwargs), system="")
        out = self._status(run)

        assert "lacks" not in out, out
        assert not any("RestartPreventExitStatus" in c for c in run.calls), run.calls

    def test_the_query_asks_for_empty_properties_too(self):
        """``--all`` keeps an unset property printed as ``Key=`` rather than
        omitted, so "unset" is not read as "systemctl did not answer"."""
        run = self._with_loaded(_fake_systemctl(system=_RUNNING, user=None), system="78")
        self._status(run)

        query = next(c for c in run.calls if "RestartPreventExitStatus" in c)
        assert query[:3] == ["systemctl", "show", "--all"], query

    def test_the_freshly_rendered_unit_carries_the_exemption(self):
        """The renderer and the check name the same code: a unit this build
        writes carries the value the check looks for."""
        from kiro_crew.service import linux as svc_linux

        with (
            patch.object(svc_linux, "_current_group", lambda _u: "tester"),
            patch.object(svc_linux.selinux, "installer_context", lambda _b: None),
        ):
            for user_scope in (False, True):
                rendered = svc_linux.render_unit(user_scope=user_scope).splitlines()
                assert f"RestartPreventExitStatus={svc_linux.LIVE_HOLDER_EXIT_CODE}" in rendered
