"""Tests for the host variables a service-started gateway does not inherit.

launchd and systemd start the gateway with ``service_environment()`` and nothing
else, so the AWS container credential endpoint, the AWS region and a fixed
ssh-agent socket exported in the installing shell are missing under the service.
``service install`` and ``doctor`` name them, never their values, and none of them
is baked into the world-readable unit or plist.
"""

from __future__ import annotations

import inspect
import json
import os
import shlex
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest
from service_helpers import _clear_sudo_user  # noqa: F401 -- an autouse fixture, found by name

from kiro_crew.service import common, controller
from kiro_crew.service.common import Platform, service_environment
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

BEARER = "tok-not-for-disclosure"

#: What a container host's login shell exports. Every value is distinct from
#: anything the warning prints, so an absence assertion on a value is meaningful.
HOST_ENV = {
    "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://127.0.0.1:1338/x",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN": BEARER,
    "AWS_REGION": "us-west-2",
    "SSH_AUTH_SOCK": "/tmp/ssh-agent-stable.sock",
}

#: Every name the check reads, cleared before a test sets its own, so a variable
#: the developer's shell happens to export cannot change a verdict.
_CHECKED_NAMES = (
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_EC2_METADATA_DISABLED",
    "SSH_AUTH_SOCK",
)


def _inherited_env():
    """The module under test, imported per test so a missing one fails each test."""
    from kiro_crew.service import inherited_env

    return inherited_env


@pytest.fixture(autouse=True)
def overrides(_floor_monkeypatch, tmp_path):
    """Point the unit's overrides file at an absent temp path, so the host's is never read."""
    from kiro_crew.service import linux

    target = tmp_path / "etc-kirocrew" / "kirocrew.env"
    _floor_monkeypatch.setattr(linux, "ENV_FILE_PATH", target)
    return target


@pytest.fixture
def dotenv(monkeypatch, tmp_path):
    """Point ``env_path()`` at an empty temp ``.env`` so the real one is never read."""
    target = tmp_path / ".env"
    target.write_text("", encoding="utf-8")
    monkeypatch.setattr(common.loader, "env_path", lambda: target)
    return target


@pytest.fixture
def host_shell(monkeypatch):
    """Give this process exactly :data:`HOST_ENV` among the checked names."""
    for name in _CHECKED_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name, value in HOST_ENV.items():
        monkeypatch.setenv(name, value)


def _install(plat: Platform) -> int:
    """Run ``install_service()`` on *plat* with the platform installer stubbed."""
    with (
        patch.object(controller, "current_platform", return_value=plat),
        patch.object(controller.linux, "install", MagicMock(return_value=MagicMock(message=""))),
        patch.object(controller.linux, "linger_warning_for_service_account", return_value=None),
        patch.object(controller.macos, "install", MagicMock(return_value=None)),
        patch.object(controller, "headless_auth_warning", return_value=""),
    ):
        return controller.install_service()


class TestServiceInstallNamesDroppedHostEnv:
    """`service install` names the host variables the service will not have."""

    @pytest.mark.parametrize("plat", [Platform.SYSTEMD, Platform.LAUNCHD])
    def test_install_names_each_dropped_variable_and_the_env_file(
        self, plat, dotenv, host_shell, capsys
    ):
        assert _install(plat) == 0
        out = capsys.readouterr().out
        for name in (
            "AWS_CONTAINER_CREDENTIALS_FULL_URI",
            "AWS_CONTAINER_AUTHORIZATION_TOKEN",
            "AWS_REGION",
        ):
            assert name in out, name
        assert str(dotenv) in out, "the warning must name the file to edit"
        for value in HOST_ENV.values():
            assert value not in out, "only names are printed, never a value"

    def test_systemd_also_names_the_ssh_agent_socket(self, dotenv, host_shell, capsys):
        """The Linux session spawn re-discovers only per-login agent sockets."""
        assert _install(Platform.SYSTEMD) == 0
        assert "SSH_AUTH_SOCK" in capsys.readouterr().out

    def test_launchd_does_not_name_the_ssh_agent_socket(self, dotenv, host_shell, capsys):
        """launchd's agent socket moves at each boot, so a copy in .env goes stale."""
        assert _install(Platform.LAUNCHD) == 0
        out = capsys.readouterr().out
        assert "AWS_REGION" in out, "fixture must produce a warning"
        assert "SSH_AUTH_SOCK" not in out

    def test_a_failing_check_cannot_break_a_successful_install(self, dotenv, capsys):
        """The unit is already started when this runs, so it must never raise."""
        boom = MagicMock(side_effect=OSError("home resolution exploded"))
        with patch.object(controller, "dropped_host_env_warning", boom):
            assert _install(Platform.SYSTEMD) == 0
        assert boom.called
        captured = capsys.readouterr()
        assert "exploded" not in captured.out + captured.err
        assert "installed and started" in captured.out


class TestDroppedHostEnvNames:
    """Which names count as dropped."""

    def test_silent_when_the_shell_sets_none_of_them(self, dotenv):
        mod = _inherited_env()
        assert mod.dropped_host_env_names({}, Platform.SYSTEMD) == []
        assert mod.dropped_host_env_warning({}, Platform.SYSTEMD) == ""

    def test_silent_when_dotenv_already_defines_every_name(self, dotenv):
        dotenv.write_text("".join(f"{name}=configured\n" for name in HOST_ENV), encoding="utf-8")
        mod = _inherited_env()
        assert mod.dropped_host_env_warning(HOST_ENV, Platform.SYSTEMD) == ""

    def test_a_blank_value_is_not_set(self, dotenv):
        mod = _inherited_env()
        assert mod.dropped_host_env_names({"AWS_REGION": "  "}, Platform.SYSTEMD) == []

    def test_a_blank_dotenv_line_does_not_supply_the_name(self, dotenv):
        """`load_credentials()` skips a bare `NAME=`, so it never reaches children."""
        dotenv.write_text("AWS_REGION=\n", encoding="utf-8")
        mod = _inherited_env()
        assert mod.dropped_host_env_names({"AWS_REGION": "us-west-2"}, Platform.SYSTEMD) == [
            "AWS_REGION"
        ]

    def test_a_name_the_service_environment_carries_is_not_dropped(self, dotenv, monkeypatch):
        mod = _inherited_env()
        real = common.service_environment
        monkeypatch.setattr(
            common, "service_environment", lambda home: {**real(home), "AWS_REGION": "baked"}
        )
        assert mod.dropped_host_env_names({"AWS_REGION": "us-west-2"}, Platform.SYSTEMD) == []

    def test_a_name_the_systemd_overrides_file_sets_is_not_dropped(self, dotenv, overrides):
        """Both unit scopes read ``/etc/kirocrew/kirocrew.env``; launchd does not."""
        overrides.parent.mkdir()
        overrides.write_text("AWS_REGION=us-east-1\n", encoding="utf-8")
        mod = _inherited_env()
        shell = {"AWS_REGION": "us-west-2"}
        assert mod.dropped_host_env_names(shell, Platform.SYSTEMD) == []
        assert mod.dropped_host_env_names(shell, Platform.LAUNCHD) == ["AWS_REGION"]

    def test_an_agent_socket_sessions_find_again_is_not_named(self, dotenv):
        """A per-login socket is re-found by each session and gone at logout."""
        mod = _inherited_env()
        for sock in ("/tmp/ssh-XXXXabcd/agent.4242", "/tmp/ssh-XXXXabcd//agent.4242"):
            assert mod.dropped_host_env_names({"SSH_AUTH_SOCK": sock}, Platform.SYSTEMD) == []
        # Deeper than the session spawn's glob reaches, so it is lost: still named.
        deep = {"SSH_AUTH_SOCK": "/tmp/ssh-XXXXabcd/nested/agent.4242"}
        assert mod.dropped_host_env_names(deep, Platform.SYSTEMD) == ["SSH_AUTH_SOCK"]

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the resolver's Linux branch")
    def test_the_unnamed_socket_paths_are_the_ones_sessions_search(self, monkeypatch):
        """Pinned to ``_resolve_ssh_auth_sock`` so the two lists cannot drift apart."""
        from kiro_crew.acp import client

        searched: list[str] = []
        monkeypatch.setattr(client.glob, "glob", lambda p: searched.append(p) or [])
        client._resolve_ssh_auth_sock({})
        assert tuple(searched) == _inherited_env()._agent_socket_globs()

    def test_a_sudo_install_checks_and_names_the_service_accounts_dotenv(
        self, dotenv, monkeypatch, tmp_path
    ):
        """Under `sudo -H` the unit runs as `SUDO_USER`, with that account's home.

        ``dotenv`` stands for root's file, which the service never reads, so a name
        it supplies is still dropped, and the remedy names the account's file.
        """
        from kiro_crew.service import linux as svc_linux

        dotenv.write_text("AWS_REGION=us-east-1\n", encoding="utf-8")
        alice_home = tmp_path / "alice"
        monkeypatch.setattr(svc_linux, "_home_for_user", lambda user: str(alice_home))
        shell = {"AWS_REGION": "us-west-2", "SUDO_USER": "alice"}
        warning = _inherited_env().dropped_host_env_warning(shell, Platform.SYSTEMD)
        assert f"touch {shlex.quote(str(alice_home / '.kiro' / 'crew' / '.env'))}" in warning
        assert str(dotenv) not in warning
        assert "as alice, not under sudo" in warning

    def test_the_inline_token_is_left_out_when_a_token_file_is_set(self, dotenv):
        """The SDKs read the file first, so the bearer value need not be copied."""
        mod = _inherited_env()
        names = mod.dropped_host_env_names(
            {**HOST_ENV, "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE": "/run/token"},
            Platform.SYSTEMD,
        )
        assert "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE" in names
        assert "AWS_CONTAINER_AUTHORIZATION_TOKEN" not in names


class TestDroppedHostEnvRemedy:
    """The printed remedy is safe to paste."""

    def test_remedy_tightens_permissions_before_appending(self, dotenv):
        """A .env born from the append alone is 0644 until the gateway next reads it."""
        warning = _inherited_env().dropped_host_env_warning(HOST_ENV, Platform.SYSTEMD)
        assert warning.index("chmod 600") < warning.index("printf")
        assert common.restart_command_hint() in warning

    def test_remedy_references_each_variable_rather_than_its_value(self, dotenv):
        warning = _inherited_env().dropped_host_env_warning(HOST_ENV, Platform.SYSTEMD)
        printf = next(line for line in warning.splitlines() if "printf" in line)
        for name in HOST_ENV:
            assert f'"{name}=${name}"' in printf, name
        for value in HOST_ENV.values():
            assert value not in warning

    @pytest.mark.skipif(sys.platform == "win32", reason="the remedy is a POSIX shell snippet")
    def test_remedy_keeps_a_last_line_without_a_newline_whole(self, dotenv, tmp_path):
        """An appended assignment must not join a hand-edited file's unterminated last line."""
        dotenv.write_text("KEEP=1", encoding="utf-8")
        names = _inherited_env().dropped_host_env_names(HOST_ENV, Platform.SYSTEMD)
        warning = _inherited_env().dropped_host_env_warning(HOST_ENV, Platform.SYSTEMD)
        snippet = "\n".join(
            line.strip()
            for line in warning.splitlines()
            if line.strip().startswith(("touch ", "echo ", "printf "))
        )
        subprocess.run(
            ["sh", "-c", snippet],
            env={**HOST_ENV, "PATH": os.environ.get("PATH", "")},
            cwd=tmp_path,
            check=True,
            timeout=FALLBACK_TIMEOUT_SECS,
        )
        parsed = common.loader.parse_env_lines(dotenv.read_text(encoding="utf-8"))
        assert parsed["KEEP"] == "1"
        assert names
        for name in names:
            assert parsed[name] == HOST_ENV[name], name

    @pytest.mark.skipif(sys.platform == "win32", reason="the remedy is a POSIX shell snippet")
    def test_remedy_appends_nothing_when_the_chmod_fails(self, dotenv, tmp_path):
        """A file left readable to others must not receive a copied credential."""
        dotenv.write_text("KEEP=1\n", encoding="utf-8")
        warning = _inherited_env().dropped_host_env_warning(HOST_ENV, Platform.SYSTEMD)
        snippet = "\n".join(
            line.strip()
            for line in warning.splitlines()
            if line.strip().startswith(("touch ", "echo ", "printf "))
        )
        failing = tmp_path / "failing-bin"
        failing.mkdir()
        chmod = failing / "chmod"
        chmod.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        chmod.chmod(0o755)
        result = subprocess.run(
            ["sh", "-c", snippet],
            env={**HOST_ENV, "PATH": f"{failing}{os.pathsep}{os.environ.get('PATH', '')}"},
            cwd=tmp_path,
            timeout=FALLBACK_TIMEOUT_SECS,
        )
        assert result.returncode != 0
        assert dotenv.read_text(encoding="utf-8") == "KEEP=1\n"

    def test_remedy_survives_a_crew_home_containing_spaces(self, monkeypatch, tmp_path):
        spaced = tmp_path / "crew home"
        spaced.mkdir()
        target = spaced / ".env"
        monkeypatch.setattr(common.loader, "env_path", lambda: target)
        warning = _inherited_env().dropped_host_env_warning(HOST_ENV, Platform.SYSTEMD)
        for line in warning.splitlines():
            if "touch" in line or "printf" in line:
                assert str(target) in shlex.split(line.strip()), line

    def test_a_name_a_later_blank_assignment_clears_is_still_named(self, dotenv):
        """The gateway keeps a key's last assignment, so ``AWS_REGION=`` after a value is unset."""
        dotenv.write_text("AWS_REGION=us-west-2\nAWS_REGION=\n", encoding="utf-8")
        names = _inherited_env().dropped_host_env_names(HOST_ENV, Platform.SYSTEMD)
        assert "AWS_REGION" in names

    def test_an_undecodable_dotenv_is_named_with_its_re_save(self, dotenv):
        """Appending UTF-8 after a UTF-16 mark leaves a file the gateway cannot read."""
        dotenv.write_bytes("AWS_REGION=us-west-2\n".encode("utf-16"))
        warning = _inherited_env().dropped_host_env_warning(HOST_ENV, Platform.SYSTEMD)
        assert "AWS_REGION" in warning
        assert "Save it as UTF-8" in warning


def test_no_dropped_variable_is_baked_into_the_service_environment(host_shell):
    """The unit, the plist and kirocrew.env are world-readable; these stay out."""
    env = service_environment("/home/tester")
    for name in (
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "SSH_AUTH_SOCK",
    ):
        assert name not in env
    assert all(BEARER not in value for value in env.values())


class TestDoctorReportsDroppedHostEnv:
    """`doctor` reports the same names, only when a service definition exists."""

    def _run(self, monkeypatch, unit, warning="Note: dropped host env"):
        from kiro_crew import cli_doctor
        from kiro_crew.doctor_checks import services

        mod = _inherited_env()
        monkeypatch.setattr(cli_doctor.service_controller, "installed_unit_path", lambda: unit)
        monkeypatch.setattr(mod, "dropped_host_env_warning", lambda: warning)
        issues: list[str] = []
        services._doctor_dropped_host_env(issues)
        return issues

    def test_reports_when_a_service_is_installed(self, monkeypatch, capsys, tmp_path):
        issues = self._run(monkeypatch, tmp_path / "kirocrew.service")
        out = capsys.readouterr().out
        assert "Note: dropped host env" in out
        assert issues == [], "the report is advisory and never fails doctor"

    def test_silent_when_no_service_is_installed(self, monkeypatch, capsys):
        """A foreground gateway inherits the shell that runs doctor."""
        assert self._run(monkeypatch, None) == []
        assert capsys.readouterr().out == ""

    def test_a_failing_probe_cannot_break_doctor(self, monkeypatch, capsys, tmp_path):
        from kiro_crew import cli_doctor
        from kiro_crew.doctor_checks import services

        def boom():
            raise OSError("environment resolution exploded")

        monkeypatch.setattr(
            cli_doctor.service_controller,
            "installed_unit_path",
            lambda: tmp_path / "kirocrew.service",
        )
        monkeypatch.setattr(_inherited_env(), "dropped_host_env_warning", boom)
        issues: list[str] = []
        services._doctor_dropped_host_env(issues)
        assert capsys.readouterr().out == ""
        assert issues == []

    def test_nothing_to_report_skips_the_unit_probe(self, monkeypatch, capsys):
        """The gate may spawn `systemctl --user show`; an empty note needs no answer."""
        from kiro_crew import cli_doctor
        from kiro_crew.doctor_checks import services

        probe = MagicMock(return_value=None)
        monkeypatch.setattr(cli_doctor.service_controller, "installed_unit_path", probe)
        monkeypatch.setattr(_inherited_env(), "dropped_host_env_warning", lambda: "")
        services._doctor_dropped_host_env([])
        monkeypatch.setattr(cli_doctor.common_service, "headless_auth_warning", lambda: "")
        services._doctor_headless_auth([])
        assert not probe.called
        assert capsys.readouterr().out == ""

    def test_doctor_actually_calls_the_check(self):
        """Asserted on `_doctor`'s source, since running it probes the live host."""
        from kiro_crew import cli_doctor

        assert "_doctor_dropped_host_env(issues)" in inspect.getsource(cli_doctor._doctor)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX service PATH")
class TestServicePathCarriesNoVersionedTree:
    """The baked service PATH names no dir a later update replaces."""

    def test_toolbox_version_dirs_become_toolbox_bin_and_venv_dirs_go_through_the_link(
        self, monkeypatch, tmp_path
    ):
        from kiro_crew.platform import tree_liveness, wheel_engine

        home = str(tmp_path)
        tree = tmp_path / "crew-venv-1.0"
        (tree / "bin").mkdir(parents=True)
        (tree / tree_liveness.TREE_MARKER).write_text("layout\n", encoding="utf-8")
        stable = str(tmp_path / "crew-venv-current" / "bin")
        monkeypatch.setattr(wheel_engine, "stable_launch_path", lambda _p: stable)
        toolbox = f"{home}/.toolbox/tools/kirocrew/1.2.3/bin"
        monkeypatch.setenv("PATH", f"{toolbox}:{tree / 'bin'}:/opt/x")

        entries = common.service_path(home).split(":")

        assert toolbox not in entries
        assert str(tree / "bin") not in entries
        assert stable in entries
        assert f"{home}/.toolbox/bin" in entries and "/opt/x" in entries


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX service PATH")
class TestMissingLauncherDirsNote:
    """`service install` names configured MCP launcher dirs the service PATH lacks."""

    @pytest.fixture
    def launcher_dir(self, monkeypatch, tmp_path):
        from kiro_crew import env as crew_env
        from kiro_crew.config import KiroCrewConfig

        target = tmp_path / "launchers"
        target.mkdir()
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: None))
        monkeypatch.setattr(crew_env, "_extra_mcp_path_dirs", lambda: [str(target)])
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        return str(target)

    @pytest.mark.parametrize("plat", [Platform.SYSTEMD, Platform.LAUNCHD])
    def test_a_non_login_install_names_the_dir_and_the_fix(
        self, plat, launcher_dir, dotenv, capsys
    ):
        assert _install(plat) == 0
        out = capsys.readouterr().out
        assert "MCP launcher directories" in out
        assert launcher_dir in out and str(dotenv) in out

    def test_silent_once_the_dotenv_path_line_adds_it(self, launcher_dir, dotenv):
        dotenv.write_text(f"PATH={launcher_dir}\n", encoding="utf-8")
        assert _inherited_env().missing_launcher_dirs_warning({}, Platform.SYSTEMD) == ""

    def test_silent_when_the_overrides_file_path_carries_it(self, launcher_dir, dotenv, overrides):
        overrides.parent.mkdir(parents=True)
        overrides.write_text(f"PATH=/usr/bin:{launcher_dir}\n", encoding="utf-8")
        assert _inherited_env().missing_launcher_dirs_warning({}, Platform.SYSTEMD) == ""

    def test_silent_when_the_installer_path_has_it(self, launcher_dir, dotenv, monkeypatch):
        monkeypatch.setenv("PATH", f"/usr/bin:{launcher_dir}")
        assert _inherited_env().missing_launcher_dirs_warning({}, Platform.LAUNCHD) == ""

    def test_the_fix_keeps_what_an_existing_dotenv_path_line_adds(
        self, launcher_dir, dotenv, tmp_path
    ):
        kept = tmp_path / "kept"
        kept.mkdir()
        dotenv.write_text(f"PATH={kept}\n", encoding="utf-8")
        note = _inherited_env().missing_launcher_dirs_warning({}, Platform.LAUNCHD)
        assert f"PATH={kept}:{launcher_dir}" in note

    def test_a_crew_home_override_carries_its_caveat(self, launcher_dir, dotenv):
        note = _inherited_env().missing_launcher_dirs_warning(
            {"KIROCREW_HOME": "/x"}, Platform.LAUNCHD
        )
        assert "KIROCREW_HOME is set here" in note

    def test_quoted_overrides_file_path_is_read_as_systemd_does(
        self, launcher_dir, dotenv, overrides
    ):
        overrides.parent.mkdir(parents=True)
        overrides.write_text(f'PATH="/usr/bin:{launcher_dir}"\n', encoding="utf-8")
        assert _inherited_env().missing_launcher_dirs_warning({}, Platform.SYSTEMD) == ""

    def test_a_sudo_install_reads_the_service_accounts_config(self, monkeypatch, tmp_path):
        from kiro_crew import env as crew_env
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.service import linux

        mod = _inherited_env()
        target = tmp_path / "svc" / "launchers"
        target.mkdir(parents=True)
        (tmp_path / "svc" / "bin").mkdir()
        crew = tmp_path / "svc" / ".kiro" / "crew"
        crew.mkdir(parents=True)
        (crew / "config.json").write_text(
            json.dumps({"mcp": {"extra_path_dirs": ["~/stale"]}}), encoding="utf-8"
        )
        (crew / "config.local.json").write_text(
            json.dumps({"mcp": {"extra_path_dirs": ["~/launchers"]}}), encoding="utf-8"
        )
        (crew / ".env").write_text("PATH=~/bin\n", encoding="utf-8")
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: None))
        monkeypatch.setattr(crew_env, "_extra_mcp_path_dirs", lambda: [])
        monkeypatch.setattr(common, "service_dotenv", lambda *_a: (crew / ".env", "svc"))
        monkeypatch.setattr(linux, "_home_for_user", lambda _u: str(tmp_path / "svc"))
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        note = mod.missing_launcher_dirs_warning({}, Platform.SYSTEMD)
        assert f"PATH=~/bin:{target}" in note, "~ kept as written, local overlay wins"
        assert "write the file as svc" in note, "names the account to write it as"
