"""``kirocrew stop`` is not a lasting stop, and ``service disable`` / ``enable`` are.

``kirocrew stop`` on an installed service leaves the unit or plist enabled, so the
manager starts the gateway again at the next boot or login. These tests pin that the
stop's success line says so and names the lasting stop, that ``service disable`` and
``service enable`` parse and dispatch, what each runs on systemd in each scope, and
the line ``service status`` adds. The launchd half lives in
``test_service_macos_launchd_domain.py``, beside its stateful ``launchctl`` fake.
Nothing here spawns ``systemctl``: ``_fake_systemctl`` answers every call.
"""

from __future__ import annotations

import argparse
import subprocess
from unittest.mock import patch

import pytest
from cli_server_more_coverage_helpers import (  # noqa: F401 -- the fixture is requested by name
    sel_rec_fixture,
)
from service_helpers import (  # noqa: F401 -- fixtures are found by name
    _DEAD,
    _OURS_UNIT_TEXT,
    _RUNNING,
    _UNIT,
    _booted_with_systemd,
    _clear_sudo_user,
    _fake_systemctl,
    _no_host_env_file,
    _no_host_unit_files,
    _restart_settle_without_waiting,
)

from kiro_crew import cli_parser_gateway, cli_server, platform_compat
from kiro_crew.service import controller
from kiro_crew.service import linux as svc_linux
from kiro_crew.service.common import Platform


def _linux(run):
    return patch("kiro_crew.service.linux.subprocess.run", side_effect=run)


@pytest.fixture(autouse=True)
def _non_root_runner(_floor_monkeypatch, tmp_path):
    """Run as a non-root account whatever account runs the suite: as root,
    ``_privilege_prefix`` drops ``sudo`` and the user manager is out of reach.
    The root-shell tests set ``geteuid`` to 0 themselves. The installer's unit
    paths point under ``tmp_path``, so the link check ``disable`` makes reads no
    host file."""
    _floor_monkeypatch.setattr(svc_linux.os, "geteuid", lambda: 1000, raising=False)
    _floor_monkeypatch.setattr(svc_linux, "UNIT_PATH", tmp_path / "system" / _UNIT)
    _floor_monkeypatch.setattr(
        svc_linux, "user_unit_file_path", lambda: tmp_path / ".config" / "systemd" / "user" / _UNIT
    )


def test_a_service_stop_says_it_comes_back_and_names_the_lasting_stop(
    monkeypatch, sel_rec, capsys
) -> None:
    monkeypatch.setattr(cli_server, "resolve_client_port", lambda p: 5476)
    monkeypatch.setattr(cli_server.service_controller, "stop_service", lambda: True)
    monkeypatch.setattr(platform_compat, "find_listening_pids", lambda port: [])
    cli_server._stop(None)
    out = capsys.readouterr().out
    assert "It starts again at the next boot or login." in out
    assert "To keep it stopped: kirocrew service disable" in out
    assert "kirocrew service uninstall" in out


@pytest.mark.parametrize("action", ["disable", "enable"])
def test_service_disable_and_enable_parse(action) -> None:
    parser = argparse.ArgumentParser()
    cli_parser_gateway.register_service_parser(parser.add_subparsers(dest="command"))
    assert parser.parse_args(["service", action]).service_action == action


@pytest.mark.parametrize(
    "action,fn", [("disable", "disable_service"), ("enable", "enable_service")]
)
def test_service_disable_and_enable_dispatch_and_audit(monkeypatch, sel_rec, action, fn) -> None:
    monkeypatch.setattr(cli_server.service_controller, fn, lambda: 1)
    assert cli_server._service_cmd(argparse.Namespace(service_action=action)) == 1
    assert sel_rec.operations == [f"service_{action}"]
    assert sel_rec.calls[0]["outcome"] == "error"


class TestLinuxDisableEnable:
    def test_disable_runs_disable_then_stop_with_sudo_in_the_system_scope(self) -> None:
        run = _fake_systemctl(system=_RUNNING, user=None)
        with _linux(run):
            assert svc_linux.disable() == ([], [])
        verbs = [c for c in run.calls if c[:2] == ["sudo", "systemctl"]]
        assert verbs == [
            ["sudo", "systemctl", "disable", _UNIT],
            ["sudo", "systemctl", "stop", _UNIT],
        ], run.calls

    def test_disable_acts_on_a_stopped_user_unit_without_sudo(self) -> None:
        run = _fake_systemctl(system=None, user=_DEAD)
        with _linux(run):
            assert svc_linux.disable() == ([], [])
        assert ["systemctl", "--user", "disable", _UNIT] in run.calls
        assert ["systemctl", "--user", "stop", _UNIT] in run.calls
        assert not any(c[0] == "sudo" for c in run.calls), run.calls

    def test_a_refused_disable_names_the_command_and_skips_the_stop(self) -> None:
        refused = subprocess.CompletedProcess([], 1, "", "Interactive authentication required")
        run = _fake_systemctl(system=_RUNNING, user=None, overrides={"disable": refused})
        with _linux(run):
            assert svc_linux.disable() == (
                ["sudo systemctl disable kirocrew", "sudo systemctl stop kirocrew"],
                [],
            )
        assert ["sudo", "systemctl", "stop", _UNIT] not in run.calls

    def test_a_root_shell_beside_a_user_unit_is_not_told_nothing_is_installed(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(svc_linux.os, "geteuid", lambda: 0, raising=False)
        monkeypatch.setenv("SUDO_USER", "tester")
        run = _fake_systemctl(system=None, user=_RUNNING)
        with _linux(run):
            (line,), unreached = svc_linux.disable()
        assert unreached == []
        assert line.startswith("user scope: not reachable from this shell"), line
        assert not any("--user" in c for c in run.calls), run.calls

    def test_a_system_unit_does_not_hide_an_unreachable_user_scope(
        self, monkeypatch, capsys
    ) -> None:
        monkeypatch.setattr(svc_linux.os, "geteuid", lambda: 0, raising=False)
        monkeypatch.setenv("SUDO_USER", "tester")
        run = _fake_systemctl(system=_DEAD, user=_RUNNING)
        with (
            _linux(run),
            patch("kiro_crew.service.controller.current_platform", return_value=Platform.SYSTEMD),
        ):
            refused, (line,) = svc_linux.disable()
            rc = controller.disable_service()
        assert refused == []
        assert line.startswith("user scope: not reachable from this shell"), line
        out = capsys.readouterr().out
        assert rc == 0
        assert "Warning: this changed only the scopes this shell can reach" in out
        assert "user scope: not reachable from this shell" in out

    def test_a_running_masked_unit_is_reported_with_its_state(self) -> None:
        run = _fake_systemctl(system=_RUNNING, user=None, system_load="masked")
        with _linux(run):
            (line,), _unreached = svc_linux.disable()
        assert "the unit is masked and still active" in line
        assert "sudo systemctl stop kirocrew" in line
        assert not any("disable" in c for c in run.calls), run.calls

    def test_no_unit_in_either_scope_is_none_and_runs_nothing(self) -> None:
        run = _fake_systemctl(system=None, user=None)
        with _linux(run):
            assert svc_linux.disable() is None
            assert svc_linux.enable() is None
        assert all("show" in c for c in run.calls), run.calls

    def test_an_alias_is_never_disabled(self) -> None:
        run = _fake_systemctl(system=_RUNNING, user=None, system_id="shared.service")
        with _linux(run):
            assert svc_linux.disable() is None
        assert not any("disable" in c for c in run.calls), run.calls

    def test_enable_with_our_unit_in_both_scopes_starts_neither(self) -> None:
        run = _fake_systemctl(system=_RUNNING, user=_DEAD)
        with _linux(run):
            refused, unreached = svc_linux.enable()
        assert unreached == []
        assert "both the system and the user scope" in refused[0]
        assert "sudo systemctl enable kirocrew && sudo systemctl start kirocrew" in refused
        assert not any(v in c for c in run.calls for v in ("enable", "start")), run.calls

    @pytest.mark.parametrize("marked", [True, False], ids=["marked", "operator-file"])
    def test_disable_leaves_a_linked_unit_installed(self, tmp_path, marked) -> None:
        """`systemctl disable` removes the `systemctl link` entry too, which would
        uninstall a linked unit: disable runs nothing on it and says what to do,
        whether or not the linked file carries the managed marker."""
        source = tmp_path / "operator" / _UNIT
        source.parent.mkdir(parents=True)
        source.write_text(_OURS_UNIT_TEXT if marked else "[Unit]\n", encoding="utf-8")
        link = tmp_path / ".config" / "systemd" / "user" / _UNIT
        link.parent.mkdir(parents=True)
        link.symlink_to(source)
        run = _fake_systemctl(system=None, user=_RUNNING, user_fragment=str(link))
        with _linux(run):
            (line,), unreached = svc_linux.disable()
        assert unreached == []
        assert line.startswith("user scope: the unit is linked"), line
        assert "systemctl --user stop kirocrew" in line
        assert not any("disable" in c or "stop" in c for c in run.calls), run.calls
        assert link.is_symlink()

    def test_disable_leaves_a_unit_linked_at_the_installer_path_installed(self, tmp_path) -> None:
        """systemd reports the link's resolved source as the fragment, so the
        installer's own path is the symlink: disable still runs nothing on it."""
        source = tmp_path / "operator" / _UNIT
        source.parent.mkdir(parents=True)
        source.write_text(_OURS_UNIT_TEXT, encoding="utf-8")
        link = tmp_path / ".config" / "systemd" / "user" / _UNIT
        link.parent.mkdir(parents=True)
        link.symlink_to(source)
        run = _fake_systemctl(system=None, user=_RUNNING, user_fragment=str(source.resolve()))
        with _linux(run):
            (line,), unreached = svc_linux.disable()
        assert unreached == []
        assert line.startswith("user scope: the unit is linked"), line
        assert not any("disable" in c or "stop" in c for c in run.calls), run.calls
        assert link.is_symlink()

    @pytest.mark.parametrize("verb", ["disable", "enable"])
    def test_a_stopped_masked_unit_is_not_read_as_not_installed(self, verb) -> None:
        run = _fake_systemctl(system=_DEAD, user=None, system_load="masked")
        with _linux(run):
            report = getattr(svc_linux, verb)()
        assert report is not None
        if verb == "disable":
            assert report == ([], [])
        else:
            (line,), _ = report
            assert line.startswith("system scope: the unit is masked"), line
            assert "sudo systemctl unmask kirocrew" in line
        assert not any(v in c for c in run.calls for v in ("disable", "enable", "stop", "start"))

    def test_enable_of_a_unit_that_dies_after_its_start_is_not_a_start(self) -> None:
        """A Type=simple start job succeeds once the process forks: enable reads
        the unit afterwards and names its journal when it did not stay up."""
        run = _fake_systemctl(system=_DEAD, user=None, start_lands_in=_DEAD)
        with _linux(run):
            (line,), unreached = svc_linux.enable()
        assert unreached == []
        assert line.startswith("system scope: enabled, but "), line
        assert "sudo journalctl -u kirocrew.service" in line

    def test_enable_runs_enable_then_start(self) -> None:
        run = _fake_systemctl(system=_DEAD, user=None)
        with _linux(run):
            assert svc_linux.enable() == ([], [])
        assert [c for c in run.calls if c[:2] == ["sudo", "systemctl"]] == [
            ["sudo", "systemctl", "enable", _UNIT],
            ["sudo", "systemctl", "start", _UNIT],
        ]


class TestLinuxStatusBootLine:
    def test_an_enabled_unit_says_it_starts_at_the_next_boot(self) -> None:
        run = _fake_systemctl(system=_RUNNING, user=None)
        with _linux(run):
            out = svc_linux.status()
        assert "Starts at the next boot: yes. To keep it stopped: kirocrew service disable" in out
        assert ["systemctl", "is-enabled", _UNIT] in run.calls
        assert "user scope: not installed" in out
        assert out.count("Starts at") == 1, out

    def test_after_disable_status_says_it_does_not_start(self) -> None:
        run = _fake_systemctl(system=_RUNNING, user=None)
        with _linux(run):
            svc_linux.disable()
            out = svc_linux.status()
        assert "system scope: inactive (dead)" in out
        assert "Starts at the next boot: no (disabled)" in out
        assert "kirocrew service enable" in out

    def test_a_user_unit_names_the_next_login(self) -> None:
        run = _fake_systemctl(system=None, user=dict(_RUNNING, UnitFileState="static"))
        with _linux(run):
            out = svc_linux.status()
        assert "Starts at the next login: unknown (systemctl is-enabled says static)" in out


class TestControllerDisableEnable:
    @staticmethod
    def _systemd():
        return patch("kiro_crew.service.controller.current_platform", return_value=Platform.SYSTEMD)

    def test_disable_reports_success(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(svc_linux, "disable", lambda: ([], []))
        with self._systemd():
            assert controller.disable_service() == 0
        out = capsys.readouterr().out
        assert "stopped and disabled" in out
        assert "kirocrew service enable" in out

    def test_a_refusal_exits_1_with_the_hand_command(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(svc_linux, "enable", lambda: (["sudo systemctl enable kirocrew"], []))
        with self._systemd():
            assert controller.enable_service() == 1
        assert "sudo systemctl enable kirocrew" in capsys.readouterr().err

    def test_nothing_installed_exits_3(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(svc_linux, "disable", lambda: None)
        with self._systemd():
            assert controller.disable_service() == 3
        assert "kirocrew service install" in capsys.readouterr().out

    def test_unsupported_exits_2(self) -> None:
        with patch(
            "kiro_crew.service.controller.current_platform", return_value=Platform.UNSUPPORTED
        ):
            assert controller.disable_service() == 2
