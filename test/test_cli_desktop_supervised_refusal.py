"""``kirocrew stop`` and ``kirocrew restart`` leave the desktop app's gateway alone.

The desktop app starts its gateway again when the gateway goes away, so a stop
from the CLI is undone and a restart races the app. Like a systemd/launchd unit,
the app owns that gateway's lifecycle: both verbs refuse, signal nothing and
print the remedy. A gateway not recognised as the app's keeps the existing path.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.service.common import RestartReport

# Pids no test signals: every process here is a patched probe.
_GATEWAY = 2**22 + 31
_SHIM = 2**22 + 32
_APP = 2**22 + 33

_MAC_APP = "/Applications/Kiro Crew.app/Contents"
_MAC_GATEWAY = f"{_MAC_APP}/Resources/backend-dist/bin/python3.12"
_MAC_ELECTRON = f"{_MAC_APP}/MacOS/Kiro Crew"


def _probes(paths: dict[int, str], parents: dict[int, int], *, cmdlines=None):
    """Patch the process probes to answer from *paths*, *parents* and *cmdlines*."""
    cmdlines = cmdlines or {}
    return [
        patch(
            "kiro_crew.cli_server.platform_compat.process_executable_path",
            side_effect=lambda pid: paths.get(pid),
        ),
        patch(
            "kiro_crew.cli_server.platform_compat.process_command_line",
            side_effect=lambda pid: cmdlines.get(pid, ""),
        ),
        patch(
            "kiro_crew.cli_server.platform_compat.get_ppid",
            side_effect=lambda pid: parents.get(pid, -1),
        ),
    ]


def _supervisor(paths, parents, *, cmdlines=None):
    from kiro_crew import cli_server

    with contextlib.ExitStack() as es:
        for p in _probes(paths, parents, cmdlines=cmdlines):
            es.enter_context(p)
        return cli_server._desktop_supervisor_pid(_GATEWAY)


class TestDesktopSupervisorPid:
    def test_the_macos_app_that_spawned_the_bundle_is_found(self):
        paths = {_GATEWAY: _MAC_GATEWAY, _APP: _MAC_ELECTRON}
        assert _supervisor(paths, {_GATEWAY: _APP}) == _APP

    def test_the_linux_app_that_spawned_the_bundle_is_found(self):
        paths = {
            _GATEWAY: "/opt/Kiro Crew/resources/backend-dist/bin/python3",
            _APP: "/opt/Kiro Crew/kiro-crew",
        }
        assert _supervisor(paths, {_GATEWAY: _APP}) == _APP

    def test_windows_reads_both_command_lines(self):
        root = r"C:\Users\u\AppData\Local\Programs\Kiro Crew"
        cmdlines = {
            _GATEWAY: rf'"{root}\resources\backend-dist\python\python.exe" -P -m kiro_crew gateway',
            _APP: rf'"{root.upper()}\Kiro Crew.exe" --some-flag',
        }
        assert _supervisor({}, {_GATEWAY: _APP}, cmdlines=cmdlines) == _APP

    def test_a_launcher_shim_inside_the_bundle_is_skipped(self):
        paths = {
            _GATEWAY: _MAC_GATEWAY,
            _SHIM: f"{_MAC_APP}/Resources/backend-dist/bin/kirocrew",
            _APP: _MAC_ELECTRON,
        }
        assert _supervisor(paths, {_GATEWAY: _SHIM, _SHIM: _APP}) == _APP

    @pytest.mark.parametrize("ppid", [1, 0, -1])
    def test_a_gateway_the_app_left_behind_is_not_supervised(self, ppid):
        """Reparented to init, or a parent that cannot be read."""
        assert _supervisor({_GATEWAY: _MAC_GATEWAY}, {_GATEWAY: ppid}) is None

    @pytest.mark.parametrize(
        "parent",
        [
            # A user-session subreaper that adopted the orphan.
            "/usr/lib/systemd/systemd",
            # A different install of the app.
            "/Applications/Other Kiro Crew.app/Contents/MacOS/Kiro Crew",
            # A pid reused by something else, or an unreadable parent.
            "",
        ],
    )
    def test_a_parent_outside_the_app_is_not_the_supervisor(self, parent):
        paths = {_GATEWAY: _MAC_GATEWAY, _APP: parent}
        assert _supervisor(paths, {_GATEWAY: _APP}) is None

    def test_a_gateway_outside_the_bundle_has_no_supervisor(self):
        paths = {_GATEWAY: "/home/u/.venv/bin/python3.12", _APP: _MAC_ELECTRON}
        assert _supervisor(paths, {_GATEWAY: _APP}) is None

    def test_the_search_is_bounded(self):
        """A chain of bundle processes longer than the depth names nobody."""
        shim = f"{_MAC_APP}/Resources/backend-dist/bin/kirocrew"
        chain = [_GATEWAY + 100 + i for i in range(10)]
        paths = {_GATEWAY: _MAC_GATEWAY, **{pid: shim for pid in chain}}
        parents = {_GATEWAY: chain[0], **dict(zip(chain, chain[1:]))}
        assert _supervisor(paths, parents) is None


def _run(verb: str, *, owner, paths, parents, cli_port=None, listeners=()):
    """Run ``_stop`` or ``_restart`` with the gateway probes patched.

    Returns ``(exit_code, mock_sel, listener_lookup)``. Without ``--port`` the
    listener lookup is the first step of the kill path, so its call count says
    whether the command went on to find a gateway to signal; with ``--port`` the
    refusal makes one lookup of its own first. ``signal`` records any kill.
    """
    from kiro_crew import cli_server

    mock_sel = MagicMock()
    lookup = MagicMock(return_value=list(listeners))
    stack = [
        patch("kiro_crew.cli_server.sel", return_value=mock_sel),
        patch("kiro_crew.cli_server._config_dir_owner", return_value=owner),
        patch("kiro_crew.cli_server.service_controller.stop_service", return_value=False),
        patch(
            "kiro_crew.cli_server.service_controller.restart_service",
            return_value=RestartReport(),
        ),
        patch("kiro_crew.cli_server.service_controller.is_service_active", return_value=False),
        patch("kiro_crew.cli_server.service_controller.installed_unit_path", return_value=None),
        patch("kiro_crew.cli_server.service_controller.service_alias_holder", return_value=None),
        patch("kiro_crew.cli_server.platform_compat.find_listening_pids", lookup),
        patch(
            "kiro_crew.cli_server.platform_compat.listening_pid_tool_available",
            return_value=True,
        ),
        # The existing path, when reached, stops here: nothing answers.
        patch("kiro_crew.cli_server._report_authenticated_shutdown", return_value=False),
        patch("kiro_crew.cli_server._refuse_if_lock_held", side_effect=SystemExit(1)),
        patch("kiro_crew.cli_server._spawn_detached_gateway"),
        patch("kiro_crew.cli_server.os.kill", side_effect=AssertionError("signalled")),
        patch(
            "kiro_crew.cli_server.platform_compat.kill_process_tree",
            side_effect=AssertionError("signalled"),
        ),
        patch(
            "kiro_crew.cli_server._request_gateway_shutdown",
            side_effect=AssertionError("asked to shut down"),
        ),
        *_probes(paths, parents),
    ]
    with contextlib.ExitStack() as es:
        for p in stack:
            es.enter_context(p)
        code = None
        try:
            if verb == "stop":
                cli_server._stop(cli_port)
            else:
                cli_server._restart(cli_port)
        except SystemExit as exc:
            code = exc.code
    return code, mock_sel, lookup


_SUPERVISED = {"paths": {_GATEWAY: _MAC_GATEWAY, _APP: _MAC_ELECTRON}, "parents": {_GATEWAY: _APP}}


class TestRefusal:
    @pytest.mark.parametrize("verb", ["stop", "restart"])
    @pytest.mark.parametrize("cli_port", [None, 5476])
    def test_a_supervised_gateway_is_refused_and_nothing_is_signalled(self, capsys, verb, cli_port):
        code, mock_sel, lookup = _run(
            verb, owner=(_GATEWAY, True), cli_port=cli_port, listeners=[_GATEWAY], **_SUPERVISED
        )

        assert code == 1
        # Only the refusal's own check of the targeted port, never the kill path.
        assert lookup.call_count == (0 if cli_port is None else 1)
        out = capsys.readouterr().out
        assert f"The Kiro Crew desktop app (pid {_APP}) runs this gateway (pid {_GATEWAY})" in out
        assert f"`kirocrew {verb}` will not kill it" in out
        calls = mock_sel.log_api_access.call_args_list
        assert len(calls) == 1
        assert calls[0].kwargs["operation"] == f"gateway_{verb}"
        assert calls[0].kwargs["outcome"] == "denied"
        assert "reason=desktop_app_supervised" in calls[0].kwargs["resources"]
        assert f"app_pid={_APP}" in calls[0].kwargs["resources"]

    def test_stop_names_quitting_the_app(self, capsys):
        _run("stop", owner=(_GATEWAY, True), **_SUPERVISED)
        out = capsys.readouterr().out
        assert "the app would start it again" in out
        assert "Quit the app to stop the gateway." in out

    def test_restart_names_the_in_app_restart(self, capsys):
        _run("restart", owner=(_GATEWAY, True), **_SUPERVISED)
        out = capsys.readouterr().out
        assert "Settings → About → Restart gateway" in out
        assert "quit the app and run `kirocrew restart` again" in out

    @pytest.mark.parametrize("verb", ["stop", "restart"])
    @pytest.mark.parametrize(
        "owner, parents",
        [
            pytest.param(None, {_GATEWAY: _APP}, id="probe-failed-or-lock-free"),
            pytest.param((_GATEWAY, False), {_GATEWAY: _APP}, id="not-the-bundle"),
            pytest.param((_GATEWAY, True), {_GATEWAY: 1}, id="app-gone"),
        ],
    )
    def test_an_unrecognised_owner_keeps_the_existing_path(self, capsys, verb, owner, parents):
        paths = {_GATEWAY: _MAC_GATEWAY, _APP: _MAC_ELECTRON}
        code, mock_sel, lookup = _run(verb, owner=owner, paths=paths, parents=parents)

        assert lookup.called
        assert "desktop app" not in capsys.readouterr().out
        for call in mock_sel.log_api_access.call_args_list:
            assert "desktop_app_supervised" not in call.kwargs["resources"]

    @pytest.mark.parametrize("verb", ["stop", "restart"])
    @pytest.mark.parametrize("listeners", [[], [_GATEWAY + 7]], ids=["unknown", "other-pid"])
    def test_an_explicit_port_this_homes_gateway_does_not_serve_is_not_refused(
        self, capsys, verb, listeners
    ):
        """``--port`` can name another home's gateway; the lock only names this home's."""
        code, mock_sel, lookup = _run(
            verb, owner=(_GATEWAY, True), cli_port=6776, listeners=listeners, **_SUPERVISED
        )

        assert lookup.call_count >= 2
        assert "desktop app" not in capsys.readouterr().out
        for call in mock_sel.log_api_access.call_args_list:
            assert "desktop_app_supervised" not in call.kwargs["resources"]

    def test_the_apps_own_pinned_stop_is_not_refused(self):
        """``stop --port N --expect-pid P`` is how the app itself stops a gateway."""
        from kiro_crew import cli_server

        with (
            patch("kiro_crew.cli_server._config_dir_owner") as owner,
            patch("kiro_crew.cli_server._stop_expected_pid") as pinned,
        ):
            cli_server._stop(5476, expect_pid=_GATEWAY)

        pinned.assert_called_once()
        owner.assert_not_called()
