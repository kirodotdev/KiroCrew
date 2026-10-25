"""``kirocrew restart`` (``_restart``): the service-aware dispatch, the stop and the spawn.

What restart waits for after the spawn, and what it reports, is in
``test_cli_restart_readiness.py``.
"""

import contextlib
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cli_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _doctor_reads_the_warm_probe_cache,
)

from kiro_crew.service.common import RestartReport


class TestRestart:
    """Tests for the service-aware ``_restart`` CLI function.

    Mirrors :class:`TestStop` — restart re-uses the same service-detection
    plumbing, so we drive the same ``service_controller`` boundary with
    fakes and assert the two branches:

    1. service active → controller handles it, no SIGTERM/spawn
    2. no service → SIGTERM via ``_stop`` if a foreground gateway is
       listening, then detach a fresh gateway via Popen
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class autouse fixture moved verbatim; no test of this class calls monkeypatch.undo()
    def _fast_restart_ready(self, monkeypatch):
        """Collapse the post-spawn readiness work so these tests don't spin.

        These tests mock the gateway lifecycle (``_spawn_detached_gateway`` /
        ``restart_service``); with no real gateway, ``_print_token_url()``'s
        readiness loop polls ``localhost`` once per second for the full
        ``_RESTART_TOKEN_WAIT`` (15s) before giving up -- ~15s x5 tests. These
        tests assert restart/spawn/stop dispatch, not token-URL readiness, so
        pin the timeout to 0 (loop is skipped, function returns immediately).
        Production default is unchanged.

        For the same reason the post-spawn readiness VERDICT is pinned to
        ``_READY_OK``: nothing here can actually become ready, so the real
        verdict would (correctly) fail every dispatch test and mask what they
        assert. The verdict itself is covered by
        :class:`TestRestartReadinessVerdict` and :class:`TestWaitGatewayReady`,
        which deliberately do NOT inherit this fixture.
        """
        from kiro_crew import cli_server

        monkeypatch.setattr("kiro_crew.cli_server._RESTART_TOKEN_WAIT", 0)
        monkeypatch.setattr(
            "kiro_crew.cli_server._wait_gateway_ready",
            lambda *a, **kw: (cli_server._READY_OK, None),
        )

    @staticmethod
    def _fake_proc(pid: int) -> MagicMock:
        """A ``Popen``-shaped stand-in: ``_restart`` reads ``.pid`` and polls it."""
        return MagicMock(pid=pid, poll=MagicMock(return_value=None))

    @pytest.fixture(autouse=True)
    def _no_active_service(self):
        # The denied-service branch consults ``is_service_active()`` after a
        # refused ``restart_service()``. The real implementation shells out to
        # systemctl/launchctl, so an unmocked call would make these tests
        # depend on whether the BUILD HOST runs a kirocrew service. Pin it
        # False; the denied-branch tests override it per-test.
        with patch(
            "kiro_crew.cli_server.service_controller.is_service_active",
            return_value=False,
        ):
            yield

    @pytest.fixture(autouse=True)
    def _no_alias_service(self):
        # ``_restart`` consults ``service_alias_holder()`` before the foreground
        # path (an alias of another unit must not be SIGTERM'd/respawned). The
        # real call shells out to systemctl, so pin it None here; the alias
        # refusal test overrides it.
        with patch(
            "kiro_crew.cli_server.service_controller.service_alias_holder",
            return_value=None,
        ):
            yield

    @pytest.fixture(autouse=True)
    def _tool_available(self):
        # ``_restart`` enters ``_stop`` when the port-lookup tool is ABSENT
        # (find_listening_pids() returns [] both for "nothing listening" and
        # "lsof missing", so a missing tool must not be mistaken for a dead
        # gateway and skipped). These tests drive the tool-present branches, so
        # pin availability True instead of depending on whether ``lsof`` is
        # installed on the build host.
        with patch(
            "kiro_crew.cli_server.platform_compat.listening_pid_tool_available",
            return_value=True,
        ):
            yield

    def _mock_sel(self):
        return patch("kiro_crew.cli_server.sel", return_value=MagicMock())

    def _lock_file(self, tmp_path, *, mode=0o600):
        """A real ``gateway.lock`` in a patched config dir (see ``TestStop``)."""
        path = tmp_path / "gateway.lock"
        path.write_text("50519\n")
        path.chmod(mode)
        return patch("kiro_crew.cli_server.config_dir", return_value=tmp_path)

    def _lock_private(self, answer):
        """Pin the lock-permission seam (see ``TestStop._lock_private``)."""
        return patch("kiro_crew.cli_server._lock_file_is_account_private", return_value=answer)

    def test_service_active_restarts_via_controller(self, capsys):
        from kiro_crew.cli_server import _restart

        with (
            self._mock_sel(),
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=True,
            ) as mock_restart,
            patch("kiro_crew.cli_server._spawn_detached_gateway") as mock_spawn,
            patch("kiro_crew.cli_server.platform_compat.find_listening_pids") as mock_ports,
        ):
            _restart(None)
        mock_restart.assert_called_once()
        # Service path must NOT also spawn — that would race the supervisor.
        mock_spawn.assert_not_called()
        # And must not poke at the port lookup (the supervisor owns the lifecycle).
        mock_ports.assert_not_called()
        assert "Restarted" in capsys.readouterr().out

    def test_active_service_restart_denied_fails_loud_with_remedy(self, capsys):
        # A system-scope unit refuses an unprivileged `systemctl restart`
        # ("Interactive authentication required"), while the unit stays
        # active. Falling through to the listener path is a silent no-op on a
        # unix-socket deployment: nothing listens on TCP, so nothing is
        # stopped, and the ORIGINAL gateway keeps running while the command
        # reads like a restart. The command must instead fail loudly and name
        # the privileged command the operator has to run themselves — the one
        # for the scope that refused, carried in the restart report.
        from kiro_crew import cli_server
        from kiro_crew.service.common import RESTART_REFUSED, RestartReport, ScopeRestart

        mock_sel = MagicMock()
        refused = RestartReport(
            (
                ScopeRestart(
                    "system",
                    False,
                    reason=(
                        "the system manager refused the restart: "
                        "Interactive authentication required"
                    ),
                    kind=RESTART_REFUSED,
                    hint="sudo systemctl restart kirocrew",
                ),
            )
        )

        with (
            patch("kiro_crew.cli_server.sel", return_value=mock_sel),
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=refused,
            ),
            patch(
                "kiro_crew.cli_server.service_controller.is_service_active",
                return_value=True,
            ),
            patch("kiro_crew.cli_server.platform_compat.find_listening_pids") as mock_ports,
            patch("kiro_crew.cli_server._stop") as mock_stop,
            patch("kiro_crew.cli_server._spawn_detached_gateway") as mock_spawn,
        ):
            with pytest.raises(SystemExit) as exc:
                cli_server._restart(None)

        assert exc.value.code == 1
        # The listener fallback must never be entered: it cannot see a
        # unix-socket service gateway and would spawn a doomed competitor.
        mock_ports.assert_not_called()
        mock_stop.assert_not_called()
        mock_spawn.assert_not_called()
        out = capsys.readouterr().out
        assert "NOT restarted" in out
        assert "system scope: the system manager refused the restart" in out
        assert "Interactive authentication required" in out
        assert "Run the restart yourself:  sudo systemctl restart kirocrew" in out
        assert "journalctl" not in out
        audit = mock_sel.log_api_access.call_args.kwargs
        assert audit["outcome"] == "denied"
        assert "reason=service_restart_denied" in audit["resources"]

    def test_user_unit_that_does_not_stay_up_names_its_state_not_a_sudo_hint(self, capsys):
        # The SELinux remedy's per-user unit: `systemctl --user restart` exits 0
        # (Type=simple forks and is done) and the gateway exits on start, so the
        # unit sits in `activating (auto-restart)` — reachable to `is_active()`,
        # so this branch is taken. The report says the unit did not stay up, and
        # the command must print THAT and point at the user journal. It must not
        # claim a privilege problem nor hint `sudo systemctl restart kirocrew`,
        # which on this host answers "Unit kirocrew.service not found".
        from kiro_crew import cli_server
        from kiro_crew.service.common import RESTART_NOT_UP, RestartReport, ScopeRestart

        mock_sel = MagicMock()
        not_up = RestartReport(
            (
                ScopeRestart(
                    "user",
                    False,
                    reason=(
                        "kirocrew.service is activating (auto-restart) (last result: "
                        "exit-code) after the restart — the gateway exits as soon as it starts"
                    ),
                    kind=RESTART_NOT_UP,
                    hint="journalctl --user -u kirocrew.service -n 50 --no-pager",
                ),
            )
        )

        with (
            patch("kiro_crew.cli_server.sel", return_value=mock_sel),
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=not_up,
            ),
            patch(
                "kiro_crew.cli_server.service_controller.is_service_active",
                return_value=True,
            ),
            patch("kiro_crew.cli_server.platform_compat.find_listening_pids") as mock_ports,
            patch("kiro_crew.cli_server._spawn_detached_gateway") as mock_spawn,
        ):
            with pytest.raises(SystemExit) as exc:
                cli_server._restart(None)

        assert exc.value.code == 1
        mock_ports.assert_not_called()
        mock_spawn.assert_not_called()
        out = capsys.readouterr().out
        assert "NOT restarted" in out
        assert "user scope: kirocrew.service is activating (auto-restart)" in out
        assert "last result: exit-code" in out
        assert "Read why it exits:  journalctl --user -u kirocrew.service" in out
        assert "sudo" not in out
        assert "privileges" not in out
        audit = mock_sel.log_api_access.call_args.kwargs
        assert audit["outcome"] == "denied"
        assert "reason=service_restart_not_up" in audit["resources"]

    def test_attempted_restart_that_left_the_unit_failed_never_spawns_a_foreground_gateway(
        self, capsys
    ):
        # The restart WAS attempted and the unit landed `failed` (start limit
        # hit): `is_service_active()` now reads False, but the unit is still an
        # installed, enabled definition. Falling through would spawn an
        # unmanaged gateway beside it; the attempted report must win over the
        # live reach check.
        from kiro_crew import cli_server
        from kiro_crew.service.common import RESTART_NOT_UP, RestartReport, ScopeRestart

        mock_sel = MagicMock()
        failed = RestartReport(
            (
                ScopeRestart(
                    "system",
                    False,
                    reason=(
                        "kirocrew.service is failed (failed) (last result: "
                        "start-limit-hit) after the restart — the gateway exits as soon as it starts"
                    ),
                    kind=RESTART_NOT_UP,
                    hint="sudo journalctl -u kirocrew.service -n 50 --no-pager",
                ),
            )
        )

        with (
            patch("kiro_crew.cli_server.sel", return_value=mock_sel),
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=failed,
            ),
            patch(
                "kiro_crew.cli_server.service_controller.is_service_active",
                return_value=False,
            ) as mock_active,
            patch("kiro_crew.cli_server.platform_compat.find_listening_pids") as mock_ports,
            patch("kiro_crew.cli_server._stop") as mock_stop,
            patch("kiro_crew.cli_server._spawn_detached_gateway") as mock_spawn,
        ):
            with pytest.raises(SystemExit) as exc:
                cli_server._restart(None)

        assert exc.value.code == 1
        mock_active.assert_not_called()
        mock_ports.assert_not_called()
        mock_stop.assert_not_called()
        mock_spawn.assert_not_called()
        out = capsys.readouterr().out
        assert "system scope: kirocrew.service is failed (failed)" in out
        assert "start-limit-hit" in out
        assert "Read why it exits:  sudo journalctl -u kirocrew.service" in out

    def test_a_unit_in_both_scopes_is_reported_per_scope_never_as_not_restarted(self, capsys):
        # A stale crash-looping system unit beside the working per-user one:
        # `restart()` restarts the user unit (it stays up) and reports the
        # system unit NOT UP. The command must say which scope restarted and
        # which did not, with the failed scope's own remedy — never "the gateway
        # was NOT restarted", which is false for the gateway the operator uses.
        # Exit 1 all the same: a scope still needs a hand, the shape `service
        # uninstall` gives a teardown that finished in one scope only.
        from kiro_crew import cli_server
        from kiro_crew.service.common import RESTART_NOT_UP, RestartReport, ScopeRestart

        mock_sel = MagicMock()
        mixed = RestartReport(
            (
                ScopeRestart(
                    "system",
                    False,
                    reason=(
                        "kirocrew.service is activating (auto-restart) (last result: "
                        "exit-code) after the restart — the gateway exits as soon as it starts"
                    ),
                    kind=RESTART_NOT_UP,
                    hint="sudo journalctl -u kirocrew.service -n 50 --no-pager",
                ),
                ScopeRestart("user", True),
            )
        )

        with (
            patch("kiro_crew.cli_server.sel", return_value=mock_sel),
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=mixed,
            ),
            patch(
                "kiro_crew.cli_server.service_controller.is_service_active",
                return_value=True,
            ),
            patch("kiro_crew.cli_server.platform_compat.find_listening_pids") as mock_ports,
            patch("kiro_crew.cli_server._spawn_detached_gateway") as mock_spawn,
        ):
            with pytest.raises(SystemExit) as exc:
                cli_server._restart(None)

        assert exc.value.code == 1
        mock_ports.assert_not_called()
        mock_spawn.assert_not_called()
        out = capsys.readouterr().out
        assert "NOT restarted" not in out
        assert out.startswith(
            "⚠️ Restarted kirocrew service in the user scope; the restart did not "
            "take in the system scope:\n"
        )
        assert "\n   user scope: restarted.\n" in out
        assert "\n   ⚠️ system scope: kirocrew.service is activating (auto-restart)" in out
        assert "Read why it exits:  sudo journalctl -u kirocrew.service" in out
        audit = mock_sel.log_api_access.call_args.kwargs
        assert audit["outcome"] == "partial"
        assert "restarted=user" in audit["resources"]
        assert "reason=service_restart_not_up" in audit["resources"]

    def test_unconfirmed_restart_points_at_service_status(self, capsys):
        # The manager stopped answering while the unit was re-read: its health is
        # unknown, so neither "exits on start" nor a restart command is honest —
        # the remedy is to look.
        from kiro_crew import cli_server
        from kiro_crew.service.common import RESTART_UNCONFIRMED, RestartReport, ScopeRestart

        mock_sel = MagicMock()
        unconfirmed = RestartReport(
            (
                ScopeRestart(
                    "user",
                    False,
                    reason=(
                        "the user manager stopped answering after the restart (Failed to "
                        "connect to bus: No medium found); whether kirocrew.service is up "
                        "could not be read"
                    ),
                    kind=RESTART_UNCONFIRMED,
                    hint="kirocrew service status",
                ),
            )
        )

        with (
            patch("kiro_crew.cli_server.sel", return_value=mock_sel),
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=unconfirmed,
            ),
            patch(
                "kiro_crew.cli_server.service_controller.is_service_active",
                return_value=False,
            ),
            patch("kiro_crew.cli_server._spawn_detached_gateway") as mock_spawn,
        ):
            with pytest.raises(SystemExit) as exc:
                cli_server._restart(None)

        assert exc.value.code == 1
        mock_spawn.assert_not_called()
        out = capsys.readouterr().out
        assert "stopped answering after the restart" in out
        assert "Check its state:  kirocrew service status" in out
        assert "exits" not in out.split("stopped answering")[1].split("\n")[0]
        audit = mock_sel.log_api_access.call_args.kwargs
        assert "reason=service_restart_unconfirmed" in audit["resources"]

    def test_inactive_service_still_falls_through_after_refused_restart(self, capsys):
        # ``restart_service()`` returning False because NO service is active
        # must keep taking the foreground path — the denied diagnostic is only
        # for a unit that is active right now yet refused the restart.
        from kiro_crew import cli_server

        with (
            self._mock_sel(),
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=RestartReport(),
            ),
            patch(
                "kiro_crew.cli_server.service_controller.is_service_active",
                return_value=False,
            ),
            patch(
                "kiro_crew.cli_server.platform_compat.find_listening_pids",
                return_value=[],
            ),
            patch(
                "kiro_crew.cli_server._spawn_detached_gateway",
                return_value=self._fake_proc(4321),
            ) as mock_spawn,
        ):
            cli_server._restart(None)
        mock_spawn.assert_called_once()
        out = capsys.readouterr().out
        assert "4321" in out
        assert "NOT restarted" not in out

    @contextlib.contextmanager
    def _probe_missed_gateway(self, holder_pid: int, *, kirocrew: bool):
        """Patches for the tool-present path where the port probe finds nothing
        and no gateway answers the authenticated shutdown, yet gateway.lock is
        held by the live ``holder_pid``. Every way the command could signal a
        pid is recorded so the tests can assert that none was used."""
        from kiro_crew.gateway_lock import LockHolder

        with (
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=RestartReport(),
            ),
            patch(
                "kiro_crew.cli_server.platform_compat.find_listening_pids",
                return_value=[],
            ),
            patch("kiro_crew.cli_server.platform_compat.IS_WINDOWS", False),
            patch("kiro_crew.cli_server._report_authenticated_shutdown", return_value=False),
            patch(
                "kiro_crew.cli_server.lock_holder",
                return_value=LockHolder(pid=holder_pid, alive=True, source="flock_owner"),
            ),
            patch("kiro_crew.cli_server._is_kirocrew_process", return_value=kirocrew),
            patch("kiro_crew.cli_server.os.kill") as mock_kill,
            patch("kiro_crew.cli_server.platform_compat.kill_process_tree") as mock_tree,
            patch("kiro_crew.cli_server._stop_mcp_gateway_daemon") as mock_daemon,
            patch("kiro_crew.cli_server._wait_for_pids_exit") as mock_wait,
            patch("kiro_crew.cli_server._spawn_detached_gateway") as mock_spawn,
        ):
            yield {
                "kill": mock_kill,
                "tree": mock_tree,
                "daemon": mock_daemon,
                "wait": mock_wait,
                "spawn": mock_spawn,
            }

    def test_lock_holder_alive_kirocrew_refuses_to_restart_and_names_the_pid(self, capsys):
        """The split-brain this fixes: the port probe finds nothing (a
        unix-socket-only gateway, or a blind spot) but gateway.lock is held by
        a live Kiro Crew pid. Restart must not report "nothing running" and
        must not spawn a replacement the lock would refuse -- and it must not
        signal through the lock either: a gateway neither the port nor the
        API could reach is refused and NAMED, with the manual command, so the
        operator stops it by hand and retries."""
        from kiro_crew.cli_server import _restart

        mock_sel = MagicMock()
        with (
            patch("kiro_crew.cli_server.sel", return_value=mock_sel),
            self._probe_missed_gateway(4242, kirocrew=True) as mocks,
        ):
            with pytest.raises(SystemExit) as exc:
                _restart(None)
        assert exc.value.code == 1
        mocks["kill"].assert_not_called()
        mocks["tree"].assert_not_called()
        mocks["daemon"].assert_not_called()
        mocks["wait"].assert_not_called()
        mocks["spawn"].assert_not_called()
        out = capsys.readouterr().out
        assert "gateway.lock" in out
        assert "pid 4242" in out
        assert "kill -TERM 4242" in out
        assert "Not starting a replacement" in out
        mock_sel.log_api_access.assert_called_once()
        audit = mock_sel.log_api_access.call_args.kwargs
        assert audit["operation"] == "gateway_restart"
        assert audit["outcome"] == "denied"
        assert "pids=[4242]" in audit["resources"]
        assert "reason=lock_holder_kirocrew" in audit["resources"]

    def test_lock_holder_alive_but_foreign_refuses_without_stopping_it(self, capsys):
        """A live holder that is not a Kiro Crew process must never be signaled
        -- refuse with the same wording ``kirocrew gateway`` would give for this
        holder, audited as denied, rather than silently spawning a competitor
        the lock refuses."""
        from kiro_crew.cli_server import _restart

        mock_sel = MagicMock()
        with (
            patch("kiro_crew.cli_server.sel", return_value=mock_sel),
            self._probe_missed_gateway(99, kirocrew=False) as mocks,
        ):
            with pytest.raises(SystemExit) as exc:
                _restart(None)
        assert exc.value.code == 1
        mocks["kill"].assert_not_called()
        mocks["tree"].assert_not_called()
        mocks["spawn"].assert_not_called()
        out = capsys.readouterr().out
        assert "does not look like a Kiro Crew gateway" in out
        assert "kill -TERM 99" in out
        mock_sel.log_api_access.assert_called_once()
        audit = mock_sel.log_api_access.call_args.kwargs
        assert audit["operation"] == "gateway_restart"
        assert audit["outcome"] == "denied"
        assert "reason=lock_holder_foreign" in audit["resources"]

    @contextlib.contextmanager
    def _tool_absent_acked_shutdown(
        self,
        holder_pid: int,
        lock_probe_error=None,
        *,
        acknowledged: bool = True,
        kirocrew: bool = True,
    ):
        """Patches for the port-tool-absent path with an acknowledged shutdown.

        ``find_listening_pids`` is blind (tool missing), so ``_restart`` enters
        ``_stop`` with an EMPTY incumbent list; ``_stop`` gets the gateway to
        acknowledge a graceful shutdown over the API and returns without naming
        a pid. The lock still names the exiting gateway as ``holder_pid`` --
        unless ``lock_probe_error`` is given, in which case the probe raises it.
        ``acknowledged=False`` drives the same entry with NO gateway answering
        the API, so ``_stop`` reaches the lock holder instead.
        ``kirocrew=False`` makes the argv check decline every pid, as it does
        for a gateway spawned in a shape the patterns do not know.
        """
        from kiro_crew.gateway_lock import LockHolder

        holder_kwargs = (
            {"side_effect": lock_probe_error}
            if lock_probe_error is not None
            else {"return_value": LockHolder(pid=holder_pid, alive=True, source="flock_owner")}
        )
        with (
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=RestartReport(),
            ),
            patch(
                "kiro_crew.cli_server.platform_compat.listening_pid_tool_available",
                return_value=False,
            ),
            patch(
                "kiro_crew.cli_server.platform_compat.find_listening_pids",
                return_value=[],
            ),
            patch("kiro_crew.cli_server.platform_compat.IS_WINDOWS", False),
            patch("kiro_crew.cli_server.service_controller.stop_service", return_value=False),
            patch(
                "kiro_crew.cli_server._report_authenticated_shutdown",
                return_value=acknowledged,
            ),
            patch("kiro_crew.cli_server.lock_holder", **holder_kwargs),
            patch("kiro_crew.cli_server._is_kirocrew_process", return_value=kirocrew),
        ):
            yield

    def test_acked_shutdown_waits_for_lock_holder_whose_argv_is_unrecognised(self):
        """The gateway answered the secret-bearing shutdown and the
        lock names its pid, but its argv matches no pattern (e.g. the desktop
        app's bare interpreter). Restart must still wait on that pid and then
        spawn, not strand the user with no gateway. The pid is only waited
        on: nothing is signalled."""
        from kiro_crew.cli_server import _restart
        from kiro_crew.gateway_lock import LockHolder

        order: list[str] = []

        def _probe(_home):
            order.append("probe")
            if "wait:[4242]" in order:
                return LockHolder(pid=None, alive=False, source="none")
            return LockHolder(pid=4242, alive=True, source="flock_owner")

        def _wait(pids, timeout):
            order.append(f"wait:{pids}")
            return []

        with (
            self._mock_sel(),
            self._tool_absent_acked_shutdown(4242, kirocrew=False),
            patch("kiro_crew.cli_server.lock_holder", side_effect=_probe),
            patch("kiro_crew.cli_server.os.kill") as mock_kill,
            patch("kiro_crew.cli_server.platform_compat.kill_process_tree") as mock_tree,
            patch("kiro_crew.cli_server._wait_for_pids_exit", side_effect=_wait),
            patch(
                "kiro_crew.cli_server._spawn_detached_gateway",
                side_effect=lambda port: (order.append("spawn"), self._fake_proc(4321))[1],
            ),
        ):
            _restart(None)
        assert order == ["probe", "wait:[4242]", "probe", "spawn"]
        mock_kill.assert_not_called()
        mock_tree.assert_not_called()

    def test_unacked_shutdown_unrecognised_lock_holder_is_still_refused(self, capsys):
        """With NO acknowledged shutdown there is no
        second proof, so an unrecognised lock holder keeps today's refusal --
        no signal, no wait, no spawn."""
        from kiro_crew.cli_server import _restart

        with (
            self._mock_sel(),
            self._tool_absent_acked_shutdown(4242, acknowledged=False, kirocrew=False),
            patch("kiro_crew.cli_server.os.kill") as mock_kill,
            patch("kiro_crew.cli_server.platform_compat.kill_process_tree") as mock_tree,
            patch("kiro_crew.cli_server._wait_for_pids_exit") as mock_wait,
            patch("kiro_crew.cli_server._spawn_detached_gateway") as mock_spawn,
        ):
            with pytest.raises(SystemExit) as exc:
                _restart(None)
        assert exc.value.code == 1
        mock_kill.assert_not_called()
        mock_tree.assert_not_called()
        mock_wait.assert_not_called()
        mock_spawn.assert_not_called()
        assert "does not look like a Kiro Crew gateway" in capsys.readouterr().out

    def test_tool_absent_unacked_shutdown_live_holder_refuses_to_restart(self, capsys):
        """Port tool absent, no gateway answers the API, yet the lock is held by
        a live Kiro Crew pid: ``_stop`` refuses (its exit is swallowed, as for
        "nothing running") and restart must NOT read that as a free lock --
        it re-reads the holder, refuses too, waits on nothing and spawns
        nothing. Both refusals are audited under their own operation."""
        from kiro_crew.cli_server import _restart

        mock_sel = MagicMock()
        with (
            patch("kiro_crew.cli_server.sel", return_value=mock_sel),
            self._tool_absent_acked_shutdown(4242, acknowledged=False),
            patch("kiro_crew.cli_server.os.kill") as mock_kill,
            patch("kiro_crew.cli_server._wait_for_pids_exit") as mock_wait,
            patch("kiro_crew.cli_server._spawn_detached_gateway") as mock_spawn,
        ):
            with pytest.raises(SystemExit) as exc:
                _restart(None)
        assert exc.value.code == 1
        mock_kill.assert_not_called()
        mock_wait.assert_not_called()
        mock_spawn.assert_not_called()
        out = capsys.readouterr().out
        assert "kill -TERM 4242" in out
        assert "Not starting a replacement" in out
        audits = [c.kwargs for c in mock_sel.log_api_access.call_args_list]
        assert [(a["operation"], a["outcome"]) for a in audits] == [
            ("gateway_stop", "denied"),
            ("gateway_restart", "denied"),
        ]
        assert all("reason=lock_holder_kirocrew" in a["resources"] for a in audits)

    def test_tool_absent_acked_shutdown_waits_for_lock_holder_before_spawning(self):
        """Port tool absent + graceful shutdown acknowledged: the port lookup
        names no incumbent, so without the lock-holder resolution the wait is
        on an empty list and the replacement spawns while the exiting gateway
        still owns gateway.lock -- it loses the race and NO gateway remains.
        The spawn must happen only after the lock holder's pid is gone."""
        from kiro_crew.cli_server import _restart

        order: list[str] = []

        def _wait(pids, timeout):
            order.append(f"wait:{pids}")
            return []

        from kiro_crew.gateway_lock import LockHolder

        def _probe(_home):
            # The holder is named before the wait; once it has exited, the
            # pre-spawn re-probe finds the lock free.
            order.append("probe")
            if "wait:[4242]" in order:
                return LockHolder(pid=None, alive=False, source="none")
            return LockHolder(pid=4242, alive=True, source="flock_owner")

        with (
            self._mock_sel(),
            self._tool_absent_acked_shutdown(4242),
            patch("kiro_crew.cli_server.lock_holder", side_effect=_probe),
            patch("kiro_crew.cli_server._wait_for_pids_exit", side_effect=_wait),
            patch(
                "kiro_crew.cli_server._spawn_detached_gateway",
                side_effect=lambda port: (order.append("spawn"), self._fake_proc(4321))[1],
            ),
        ):
            _restart(None)
        assert order == ["probe", "wait:[4242]", "probe", "spawn"]

    def test_tool_absent_acked_shutdown_holder_still_alive_refuses_to_spawn(self, capsys):
        """Same entry, holder never exits within the wait: refuse with the same
        'still running' message the port-lookup path prints, non-zero exit, no
        spawn -- a replacement the lock would refuse is worse than none."""
        from kiro_crew.cli_server import _restart

        mock_sel = MagicMock()
        with (
            patch("kiro_crew.cli_server.sel", return_value=mock_sel),
            self._tool_absent_acked_shutdown(4242),
            patch("kiro_crew.cli_server._wait_for_pids_exit", return_value=[4242]) as mock_wait,
            patch("kiro_crew.cli_server._spawn_detached_gateway") as mock_spawn,
        ):
            with pytest.raises(SystemExit) as exc:
                _restart(None)
        assert exc.value.code == 1
        mock_wait.assert_called_once()
        assert mock_wait.call_args.args[0] == [4242]
        mock_spawn.assert_not_called()
        out = capsys.readouterr().out
        assert "Gateway (pid 4242) did not exit within" in out
        assert "Not starting a replacement" in out
        audit = mock_sel.log_api_access.call_args.kwargs
        assert audit["outcome"] == "denied"
        assert "reason=incumbent_still_running" in audit["resources"]

    def test_tool_absent_indeterminate_lock_refuses_to_spawn(self, capsys):
        """Port tool absent and the lock probe cannot say who holds the lock:
        neither signal a guessed pid nor spawn a replacement the lock may
        refuse -- print the existing indeterminate message and exit 1."""
        from kiro_crew.cli_server import _restart
        from kiro_crew.gateway_lock import LockProbeError

        with (
            self._mock_sel(),
            self._tool_absent_acked_shutdown(
                4242,
                lock_probe_error=LockProbeError(
                    Path("/nowhere/gateway.lock"), OSError("probe failed")
                ),
            ),
            patch("kiro_crew.cli_server._wait_for_pids_exit") as mock_wait,
            patch("kiro_crew.cli_server._spawn_detached_gateway") as mock_spawn,
        ):
            with pytest.raises(SystemExit) as exc:
                _restart(None)
        assert exc.value.code == 1
        mock_wait.assert_not_called()
        mock_spawn.assert_not_called()
        out = capsys.readouterr().out
        assert "could not determine whether a gateway holds the lock" in out
        assert "not starting a replacement" in out

    def test_no_service_no_running_gateway_spawns_fresh(self, capsys):
        # Restart should be tolerant of a crashed gateway: if the user runs
        # ``kirocrew restart`` after the gateway died, they should still
        # end up with a running gateway, not an error.
        from kiro_crew.cli_server import _restart

        with (
            self._mock_sel(),
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=RestartReport(),
            ),
            patch(
                "kiro_crew.cli_server.platform_compat.find_listening_pids",
                return_value=[],
            ),
            patch(
                "kiro_crew.cli_server._spawn_detached_gateway",
                return_value=self._fake_proc(4321),
            ) as mock_spawn,
            patch("kiro_crew.cli_server._stop") as mock_stop,
        ):
            _restart(None)
        mock_stop.assert_not_called()
        mock_spawn.assert_called_once()
        out = capsys.readouterr().out
        assert "4321" in out
        assert "detached" in out.lower()

    def test_no_service_with_running_gateway_stops_then_spawns(self, capsys):
        from kiro_crew.cli_server import _restart

        with (
            self._mock_sel(),
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=RestartReport(),
            ),
            patch(
                "kiro_crew.cli_server.platform_compat.find_listening_pids",
                return_value=[1234],
            ),
            # A RUNNING gateway, per this test's name: argv classifies it, so it
            # is the incumbent by that route and the endpoint path is not
            # involved. The wait is stubbed so the assertion never depends on
            # whether pid 1234 happens to exist on the host.
            patch("kiro_crew.cli_server._is_kirocrew_process", return_value=True),
            patch("kiro_crew.cli_server._wait_for_pids_exit", return_value=[]),
            patch("kiro_crew.cli_server._stop") as mock_stop,
            patch(
                "kiro_crew.cli_server._spawn_detached_gateway",
                return_value=self._fake_proc(5678),
            ) as mock_spawn,
        ):
            _restart(None)
        # Order matters: stop first, then spawn — otherwise the new
        # gateway would race the old one for the port and lose.
        mock_stop.assert_called_once_with(None)
        mock_spawn.assert_called_once()
        assert "5678" in capsys.readouterr().out

    def test_toctou_stop_systemexit_is_swallowed_so_spawn_proceeds(self, capsys):
        # review-bot finding on rev 1: lsof can show a listener, then the
        # gateway exits before _stop() runs. _stop() then finds nothing
        # and calls sys.exit(1). For restart, that's the wrong behavior:
        # the user asked for a restart, not a stop, and an exit here would
        # leave them with no running gateway at all. Verify we swallow
        # SystemExit and still spawn the replacement.
        from kiro_crew.cli_server import _restart

        with (
            self._mock_sel(),
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=RestartReport(),
            ),
            patch(
                "kiro_crew.cli_server.platform_compat.find_listening_pids",
                return_value=[1234],
            ),
            patch("kiro_crew.cli_server._stop", side_effect=SystemExit(1)) as mock_stop,
            patch(
                "kiro_crew.cli_server._spawn_detached_gateway",
                return_value=self._fake_proc(9999),
            ) as mock_spawn,
        ):
            _restart(None)
        mock_stop.assert_called_once_with(None)
        mock_spawn.assert_called_once()
        assert "9999" in capsys.readouterr().out

    def test_waits_for_incumbent_to_exit_before_spawning(self):
        """The replacement must not be spawned while the old gateway is alive.

        The incumbent holds the ``KIROCREW_HOME`` flock for its whole graceful
        shutdown, and ``_stop`` waits only ~1s for exit without reporting back.
        A replacement spawned inside that window is refused by the lock and exits
        1, leaving NO gateway running. Assert the spawn happens strictly after
        the incumbent pid is observed gone.
        """
        from kiro_crew import cli_server

        # Alive for the first two probes, gone from the third.
        exited = iter([False, False, True, True, True])
        probe_log: list[bool] = []

        def fake_pid_exited(pid: int) -> bool:
            val = next(exited, True)
            probe_log.append(val)
            return val

        with (
            self._mock_sel(),
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=RestartReport(),
            ),
            patch(
                "kiro_crew.cli_server.platform_compat.find_listening_pids",
                return_value=[1234],
            ),
            patch("kiro_crew.cli_server._is_kirocrew_process", return_value=True),
            patch("kiro_crew.cli_server._stop"),
            patch("kiro_crew.cli_server._pid_exited", side_effect=fake_pid_exited),
            patch("kiro_crew.cli_server._print_token_url"),
            patch(
                "kiro_crew.cli_server._spawn_detached_gateway",
                return_value=self._fake_proc(5678),
            ) as mock_spawn,
        ):
            cli_server._restart(None)

        mock_spawn.assert_called_once()
        # The wait actually polled past the "still alive" answers rather than
        # spawning on the first one.
        assert probe_log[:3] == [False, False, True]

    def test_refuses_to_spawn_when_incumbent_never_exits(self, capsys):
        """A wedged incumbent must abort the restart, not produce zero gateways.

        Spawning anyway would print a success line for a child the lock kills,
        so the user ends up with nothing. Aborting leaves the (slow) gateway up
        and names the pid to force.
        """
        from kiro_crew import cli_server

        with (
            self._mock_sel(),
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=RestartReport(),
            ),
            patch(
                "kiro_crew.cli_server.platform_compat.find_listening_pids",
                return_value=[1234],
            ),
            patch("kiro_crew.cli_server._is_kirocrew_process", return_value=True),
            patch("kiro_crew.cli_server._stop"),
            patch("kiro_crew.cli_server._pid_exited", return_value=False),
            patch("kiro_crew.cli_server._RESTART_STOP_TIMEOUT", 0),
            patch("kiro_crew.cli_server._spawn_detached_gateway") as mock_spawn,
        ):
            with pytest.raises(SystemExit) as exc:
                cli_server._restart(None)

        assert exc.value.code == 1
        mock_spawn.assert_not_called()
        out = capsys.readouterr().out
        assert "1234" in out
        assert "did not exit" in out

    def test_unrelated_port_listener_is_not_waited_on(self, capsys):
        """A non-KiroCrew listener must never gate the restart.

        ``find_listening_pids`` reports whatever holds the port. Blocking on a
        foreign process would make restart hang for the full timeout and then
        refuse, so nothing is waited on and the spawn proceeds to fail on its own
        bind.

        The stub raises ``SystemExit`` because that is what ``_stop`` does with a
        listener nothing identifies: the argv check declines it, the
        authenticated shutdown gets no answer, and it exits 1 on the
        ``unrecognized_listener`` refusal. A stub that returned normally would
        model a stop that neither stopped nor refused.
        """
        from kiro_crew import cli_server

        with (
            self._mock_sel(),
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=RestartReport(),
            ),
            patch(
                "kiro_crew.cli_server.platform_compat.find_listening_pids",
                return_value=[1234],
            ),
            patch("kiro_crew.cli_server._is_kirocrew_process", return_value=False),
            patch("kiro_crew.cli_server._stop", side_effect=SystemExit(1)) as mock_stop,
            patch("kiro_crew.cli_server._pid_exited", return_value=False) as mock_exited,
            patch("kiro_crew.cli_server._print_token_url"),
            patch(
                "kiro_crew.cli_server._spawn_detached_gateway",
                return_value=self._fake_proc(5678),
            ) as mock_spawn,
        ):
            cli_server._restart(None)

        # _stop still runs (it owns the "not a Kiro Crew gateway" diagnostic)...
        mock_stop.assert_called_once_with(None)
        # ...but nothing is waited on, and the spawn proceeds.
        mock_exited.assert_not_called()
        mock_spawn.assert_called_once()
        out = capsys.readouterr().out
        assert "5678" in out
        assert "did not exit" not in out

    def test_spawn_detached_gateway_binds_requested_port(self, tmp_path, monkeypatch):
        """The child must bind the port the parent resolved.

        The parent stops a gateway on the resolved port and then polls that same
        port for readiness, but the child re-resolves independently. With
        run-marker discovery in the chain (and the marker cleared by the stop we
        just did), an unparameterised spawn lets the replacement bind 5476 while
        the parent waits on 6776 and prints a 6776 URL.
        """
        from kiro_crew.cli_server import _spawn_detached_gateway

        monkeypatch.setattr("kiro_crew.cli_server.config_dir", lambda: tmp_path)
        # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process, moved verbatim; the process double is never signalled
        proc = MagicMock(pid=4321)
        with (
            patch("shutil.which", return_value="/usr/local/bin/kirocrew"),
            patch("kiro_crew.cli_server.subprocess.Popen", return_value=proc) as mock_popen,
        ):
            _spawn_detached_gateway(6776)
        assert mock_popen.call_args.args[0] == [
            "/usr/local/bin/kirocrew",
            "gateway",
            "--port",
            "6776",
        ]

    def test_restart_passes_resolved_port_to_spawn(self, tmp_path, monkeypatch):
        """`restart` with no --port must hand its resolved port to the child."""
        from kiro_crew import cli_server

        monkeypatch.setattr("kiro_crew.cli_server.config_dir", lambda: tmp_path)
        with (
            patch("kiro_crew.cli_server.resolve_client_port", return_value=6776),
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=RestartReport(),
            ),
            patch("kiro_crew.cli_server.platform_compat.find_listening_pids", return_value=[]),
            patch(
                "kiro_crew.cli_server.platform_compat.listening_pid_tool_available",
                return_value=True,
            ),
            patch(
                "kiro_crew.cli_server._spawn_detached_gateway",
                return_value=self._fake_proc(1234),
            ) as mock_spawn,
            patch("kiro_crew.cli_server._print_token_url"),
        ):
            cli_server._restart(None)
        assert mock_spawn.call_args.args[0] == 6776

    def test_spawn_detached_gateway_uses_kirocrew_bin(self, tmp_path, monkeypatch):
        # When ``kirocrew`` is on PATH, the detached child must invoke it
        # directly (not via ``python -m``). This exercises the production
        # path on installed hosts.
        from kiro_crew.cli_server import _spawn_detached_gateway

        monkeypatch.setattr("kiro_crew.cli_server.config_dir", lambda: tmp_path)
        # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process, moved verbatim; the process double is never signalled
        proc = MagicMock(pid=9999)
        with (
            patch("shutil.which", return_value="/usr/local/bin/kirocrew"),
            patch("kiro_crew.cli_server.subprocess.Popen", return_value=proc) as mock_popen,
        ):
            spawned = _spawn_detached_gateway()
        # The Popen HANDLE is returned, not a bare pid: restart must be able to
        # poll the child for early death (and its exit status) before it reports
        # success.
        assert spawned is proc
        assert spawned.pid == 9999
        argv = mock_popen.call_args.args[0]
        assert argv == ["/usr/local/bin/kirocrew", "gateway"]
        # Must detach from the controlling terminal — otherwise the detached
        # process would die when the calling shell exits. POSIX: start_new_session;
        # Windows: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP creationflags.
        kw = mock_popen.call_args.kwargs
        if sys.platform == "win32":
            flags = kw["creationflags"]
            assert flags & subprocess.DETACHED_PROCESS
            assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
            # Passed explicitly as False (mypy-safe explicit flags, not **dict
            # unpack); Windows ignores it, the creationflags do the detaching.
            assert kw.get("start_new_session") is False
        else:
            assert kw["start_new_session"] is True
        # Must not inherit stdin from the parent — otherwise reading from
        # a detached terminal would block the new gateway.
        assert kw["stdin"] == subprocess.DEVNULL

    def test_spawn_detached_gateway_prefers_own_console_script(self, tmp_path, monkeypatch):
        # Two `kirocrew` scripts can exist at once — an edition composes this
        # core behind an entry point of the same name, and a stock editable
        # install in another interpreter puts a second one on PATH. A restart
        # must respawn the one that was invoked, not whichever which() finds
        # first, or the replacement gateway composes different providers than
        # the one just stopped.
        from kiro_crew.cli_server import _spawn_detached_gateway

        monkeypatch.setattr("kiro_crew.cli_server.config_dir", lambda: tmp_path)
        own = tmp_path / "kirocrew"
        own.write_text("#!/bin/sh\n")
        own.chmod(0o755)
        monkeypatch.setattr(sys, "argv", [str(own), "restart"])
        # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process, moved verbatim; the process double is never signalled
        proc = MagicMock(pid=7777)
        with (
            patch("shutil.which", return_value="/usr/local/bin/kirocrew"),
            patch("kiro_crew.cli_server.subprocess.Popen", return_value=proc) as mock_popen,
        ):
            _spawn_detached_gateway()
        assert mock_popen.call_args.args[0] == [str(own), "gateway"]

    def test_spawn_detached_gateway_ignores_non_kirocrew_argv0(self, tmp_path, monkeypatch):
        # argv[0] is only trusted when it names the kirocrew console script.
        # Anything else (a test runner, a wrapper, `python -m kiro_crew restart`)
        # must fall through to the documented which()/module resolution.
        from kiro_crew.cli_server import _spawn_detached_gateway

        monkeypatch.setattr("kiro_crew.cli_server.config_dir", lambda: tmp_path)
        other = tmp_path / "pytest"
        other.write_text("#!/bin/sh\n")
        other.chmod(0o755)
        monkeypatch.setattr(sys, "argv", [str(other), "restart"])
        # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process, moved verbatim; the process double is never signalled
        proc = MagicMock(pid=6666)
        with (
            patch("shutil.which", return_value="/usr/local/bin/kirocrew"),
            patch("kiro_crew.cli_server.subprocess.Popen", return_value=proc) as mock_popen,
        ):
            _spawn_detached_gateway()
        assert mock_popen.call_args.args[0] == ["/usr/local/bin/kirocrew", "gateway"]

    def test_spawn_detached_gateway_ignores_missing_argv0_path(self, tmp_path, monkeypatch):
        # Fails closed on a correctly-named but non-existent argv[0] (frozen
        # bundles and some launchers rewrite it), rather than handing Popen a
        # path that cannot be executed.
        from kiro_crew.cli_server import _spawn_detached_gateway

        monkeypatch.setattr("kiro_crew.cli_server.config_dir", lambda: tmp_path)
        monkeypatch.setattr(sys, "argv", [str(tmp_path / "ghost" / "kirocrew"), "restart"])
        # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process, moved verbatim; the process double is never signalled
        proc = MagicMock(pid=5555)
        with (
            patch("shutil.which", return_value="/usr/local/bin/kirocrew"),
            patch("kiro_crew.cli_server.subprocess.Popen", return_value=proc) as mock_popen,
        ):
            _spawn_detached_gateway()
        assert mock_popen.call_args.args[0] == ["/usr/local/bin/kirocrew", "gateway"]

    def test_spawn_detached_gateway_absolutizes_relative_argv0(self, tmp_path, monkeypatch):
        # Regression: `cd ~/checkout && .venv/bin/kirocrew restart`. shutil.which()
        # returns an argument that already has a directory component *unchanged*,
        # so it stays relative — and Popen gets cwd=$HOME, which chdirs the child
        # before exec, so a relative program path resolves under $HOME and raises
        # FileNotFoundError with no gateway running (_stop() already killed it).
        # Deliberately exercises the real which() rather than patching it.
        from kiro_crew.cli_server import _spawn_detached_gateway

        monkeypatch.setattr("kiro_crew.cli_server.config_dir", lambda: tmp_path)
        # Use the real per-platform venv console-script layout: `bin/kirocrew` on
        # POSIX, `Scripts\kirocrew.exe` on Windows. shutil.which() only accepts a
        # directory-qualified argument on Windows with a PATHEXT extension
        # attached, so an extensionless name there would miss and fall through to
        # the which()/module chain instead of exercising this path.
        if sys.platform == "win32":
            venv_bin = tmp_path / ".venv" / "Scripts"
            script_name = "kirocrew.exe"
        else:
            venv_bin = tmp_path / ".venv" / "bin"
            script_name = "kirocrew"
        venv_bin.mkdir(parents=True)
        own = venv_bin / script_name
        own.write_text("#!/bin/sh\n")
        own.chmod(0o755)
        monkeypatch.chdir(tmp_path)
        rel = os.path.join(".venv", venv_bin.name, script_name)
        monkeypatch.setattr(sys, "argv", [rel, "restart"])
        # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process, moved verbatim; the process double is never signalled
        proc = MagicMock(pid=4444)
        with patch("kiro_crew.cli_server.subprocess.Popen", return_value=proc) as mock_popen:
            _spawn_detached_gateway()
        spawned = Path(mock_popen.call_args.args[0][0])
        assert spawned.is_absolute()
        assert spawned.resolve() == own.resolve()

    def test_spawn_detached_gateway_falls_back_to_python_m(
        self, tmp_path, monkeypatch, nonbundled_python_without_user_site
    ):
        # Dev/Brazil-workspace installs may not have ``kirocrew`` on
        # PATH globally. Fall back to ``python -s -P -m kiro_crew`` so the
        # command works regardless of install layout without loading user site
        # and without the spawn cwd (the home directory) ahead of the stdlib.
        from kiro_crew.cli_server import _spawn_detached_gateway

        monkeypatch.setattr("kiro_crew.cli_server.config_dir", lambda: tmp_path)
        # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on a mocked process, moved verbatim; the process double is never signalled
        proc = MagicMock(pid=8888)
        with (
            patch("shutil.which", return_value=None),
            patch("kiro_crew.cli_server.subprocess.Popen", return_value=proc) as mock_popen,
        ):
            _spawn_detached_gateway()
        argv = mock_popen.call_args.args[0]
        # First arg is sys.executable (path to current Python). Just check
        # the invocation form, not the absolute path.
        assert argv[1:] == ["-s", "-P", "-m", "kiro_crew", "gateway"]

    def test_explicit_port_bypasses_service_short_circuit(self, capsys):
        # When cli_port is not None, bypass systemd: the service unit is not
        # bound to a specific port, so short-circuiting through it would
        # target the wrong gateway.
        from kiro_crew.cli_server import _restart

        with (
            self._mock_sel(),
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=True,
            ) as mock_restart_service,
            patch(
                "kiro_crew.cli_server._spawn_detached_gateway",
                return_value=self._fake_proc(4321),
            ) as mock_spawn,
            patch(
                "kiro_crew.cli_server.platform_compat.find_listening_pids",
                return_value=[],
            ),
        ):
            _restart(8089)
        mock_restart_service.assert_not_called()
        mock_spawn.assert_called_once()
        assert "Started detached gateway" in capsys.readouterr().out

    def test_restart_waits_for_the_listener_that_acknowledged(self, capsys):
        """Restart must outwait the gateway that accepted the shutdown.

        The app-spawned listener fails the argv check, so it is no incumbent by
        that route -- yet it is the process that must release the port before a
        replacement can bind, and it is still exiting. Who to wait for is
        answered by who is listening, so the enumerated pid is used and the
        replacement spawns only after it is gone.
        """
        from kiro_crew.cli_server import _restart

        app_argv = (
            "/Applications/KiroCrew.app/Contents/Resources/backend-dist/"
            "kirocrew-backend-arm64/bin/python3.12 /Applications/KiroCrew.app/"
            "Contents/Resources/backend-dist/kirocrew-backend-arm64/serve.py"
        )
        with (
            self._mock_sel(),
            patch(
                "kiro_crew.cli_server.platform_compat.find_listening_pids",
                return_value=[50519],
            ),
            patch(
                "kiro_crew.cli_server.platform_compat.process_command_line",
                return_value=app_argv,
            ),
            patch(
                "kiro_crew.cli_server._verified_loopback_gateway_pids",
                return_value=[50519],
            ),
            patch("kiro_crew.cli_server._request_gateway_shutdown", return_value=True),
            patch("kiro_crew.cli_server.platform_compat.IS_WINDOWS", False),
            patch("kiro_crew.cli_server.os.kill") as mock_kill,
            patch("kiro_crew.cli_server._refuse_if_lock_held"),
            patch("kiro_crew.cli_server._wait_for_pids_exit", return_value=[]) as mock_wait,
            patch("kiro_crew.cli_server.run_marker.read_pid", return_value=None),
            patch(
                "kiro_crew.cli_server._spawn_detached_gateway",
                return_value=self._fake_proc(60815),
            ) as mock_spawn,
        ):
            _restart(5476)
        # Nothing is signalled: the gateway shut itself down.
        mock_kill.assert_not_called()
        assert mock_wait.call_args.args[0] == [50519]
        mock_spawn.assert_called_once()
        assert "does not look like a Kiro Crew gateway" not in capsys.readouterr().out

    def test_an_alias_holding_the_name_is_refused_before_the_foreground_path(self, capsys):
        """When ``kirocrew.service`` is an alias of another unit, ``restart_service()``
        returns an empty report and ``is_service_active()`` is False, but the
        foreground fallback must NOT then SIGTERM the alias target's managed
        process and spawn a replacement beside it: refuse, exit 1, signal and
        spawn nothing."""
        from kiro_crew.cli_server import _restart
        from kiro_crew.service.common import RestartReport
        from kiro_crew.service.linux import AliasHolder

        mock_sel = MagicMock()
        holder = AliasHolder("system", "other.service")
        with (
            patch("kiro_crew.cli_server.sel", return_value=mock_sel),
            patch(
                "kiro_crew.cli_server.service_controller.restart_service",
                return_value=RestartReport(),
            ),
            patch(
                "kiro_crew.cli_server.service_controller.service_alias_holder",
                return_value=holder,
            ),
            patch("kiro_crew.cli_server._spawn_detached_gateway") as mock_spawn,
            patch("kiro_crew.cli_server.platform_compat.find_listening_pids") as mock_ports,
        ):
            with pytest.raises(SystemExit) as exc:
                _restart(None)
            assert exc.value.code == 1
        mock_spawn.assert_not_called()
        mock_ports.assert_not_called()
        out = capsys.readouterr().out
        assert "alias of other.service" in out
        assert "system scope" in out
        # restart verb + system-scope spelling.
        assert "`sudo systemctl restart other.service`" in out
        resources = mock_sel.log_api_access.call_args.kwargs["resources"]
        assert "reason=service_name_is_alias" in resources
        assert "scope=system" in resources
