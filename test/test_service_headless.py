"""``kirocrew service install --headless``: the service starts ``gateway --headless``.

The mode lives in the service's own command line and nowhere else. On Linux it
is read from the command the service manager has loaded, on macOS from the
plist's ``ProgramArguments``. Nothing here runs ``systemctl`` or ``launchctl``:
the manager's answer and the install steps are faked.
"""

from __future__ import annotations

import argparse
import plistlib
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from service_helpers import (  # noqa: F401 -- fixtures are found by name
    _booted_with_systemd,
    _clear_sudo_user,
    _no_host_env_file,
    _no_host_unit_files,
    _restart_settle_without_waiting,
)

from kiro_crew import cli_parser_gateway, cli_server, gateway_restart
from kiro_crew.service import common as svc_common
from kiro_crew.service import controller
from kiro_crew.service import linux as svc_linux
from kiro_crew.service import macos as svc_macos
from kiro_crew.service.common import (
    LAUNCHD_LABEL,
    SERVICE_NAME,
    Platform,
    RestartReport,
    ScopeRestart,
)

_BIN = "/usr/local/bin/kirocrew"
_HEADLESS_LINE = "mode: headless (no dashboard; the gateway listens on loopback only)\n"
_UNKNOWN_LINE = f"mode: {controller.MODE_UNKNOWN}\n"


def _render_unit(monkeypatch, **kwargs) -> str:
    monkeypatch.setenv("USER", "tester")
    monkeypatch.setattr(svc_common, "_running_install_bin", lambda: None)
    gid = MagicMock(returncode=0, stdout="staff\n", stderr="")
    with (
        patch("kiro_crew.service.common.shutil.which", return_value=_BIN),
        patch("kiro_crew.service.linux.subprocess.run", return_value=gid),
    ):
        return svc_linux.render_unit(**kwargs)


def _render_plist(monkeypatch, tmp_path, **kwargs) -> str:
    monkeypatch.setattr(svc_macos, "LIVE_PROGRAM", tmp_path / "live-gateway")
    monkeypatch.setattr(svc_common, "_running_install_bin", lambda: None)
    with patch("kiro_crew.service.common.shutil.which", return_value=_BIN):
        return svc_macos.render_plist(**kwargs)


def _exec_start(unit: str) -> list[str]:
    return [line for line in unit.splitlines() if line.startswith("ExecStart=")]


def _shown(words: str) -> str:
    """One ``ExecStart`` value as ``systemctl show`` prints it."""
    return (
        f"{{ path={_BIN} ; argv[]={words} ; ignore_errors=no ; start_time=[n/a] ; "
        "stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
    )


def _loaded(headless: bool = False, *, words: "str | None" = None) -> dict[str, list[str]]:
    """What the manager answers for our loaded unit."""
    if words is None:
        words = f"{_BIN} gateway --no-open" + (" --headless" if headless else "")
    return {
        "Id": [f"{SERVICE_NAME}.service"],
        "LoadState": ["loaded"],
        "ExecStart": [_shown(words)],
    }


@pytest.fixture
def manager(monkeypatch):
    """The service manager's answers, by scope (``True`` is the user scope).

    A value is the properties it answers with, or an exception it raises. A
    scope with no value cannot be asked. The real ``systemctl`` is never run.
    """
    answers: dict[object, object] = {}

    def show(user):
        answer = answers.get(user, gateway_restart._Inconclusive("no manager in this test"))
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(gateway_restart, "_systemd_show", show)
    monkeypatch.setattr(svc_linux, "_user_scope_unreachable_reason", lambda: None)
    return answers


@pytest.fixture
def systemd(monkeypatch, tmp_path, manager):
    """A systemd host whose system unit file is ``tmp_path``'s, with no user unit."""
    unit_path = tmp_path / f"{SERVICE_NAME}.service"
    monkeypatch.setattr(controller, "current_platform", lambda: Platform.SYSTEMD)
    monkeypatch.setattr(svc_linux, "UNIT_PATH", unit_path)
    monkeypatch.setattr(svc_linux, "user_unit_path", lambda: None)
    monkeypatch.setattr(svc_linux, "enable_service_account_linger", lambda: None)
    return unit_path


@pytest.fixture
def user_unit(systemd, monkeypatch, tmp_path):
    """A per-user unit file beside the ``systemd`` fixture's system scope."""
    path = tmp_path / "user.service"
    path.write_bytes(b"")
    monkeypatch.setattr(svc_linux, "user_unit_path", lambda: path)
    return path


@pytest.fixture
def launchd(monkeypatch, tmp_path):
    """A launchd host whose plist is ``tmp_path``'s."""
    plist_path = tmp_path / f"{LAUNCHD_LABEL}.plist"
    monkeypatch.setattr(controller, "current_platform", lambda: Platform.LAUNCHD)
    monkeypatch.setattr(svc_macos, "PLIST_PATH", plist_path)
    return plist_path


def _plist(headless: bool) -> bytes:
    words = [_BIN, "gateway", "--no-open"] + (["--headless"] if headless else [])
    return plistlib.dumps({"ProgramArguments": words})


@pytest.fixture
def installs(monkeypatch, manager):
    """Record the mode each platform install is asked to write.

    Each fake leaves what a real install leaves: the definition file, and on
    Linux the manager loaded with the new command. ``manager["after"]`` is the
    answer the manager gives after the install instead, when a test sets it.
    """
    from kiro_crew.service import apparmor

    seen: list[bool] = []

    def linux_install(*, headless=False):
        seen.append(headless)
        svc_linux.UNIT_PATH.write_bytes(b"")
        manager[False] = manager.get("after", _loaded(headless))
        return apparmor.ProfileOutcome(False, "")

    def macos_install(*, headless=False):
        seen.append(headless)
        svc_macos.PLIST_PATH.write_bytes(manager.get("after", _plist(headless)))

    monkeypatch.setattr(svc_linux, "install", linux_install)
    monkeypatch.setattr(svc_macos, "install", macos_install)
    return seen


class TestRendering:
    @pytest.mark.parametrize("user_scope", [False, True])
    def test_unit_starts_the_gateway_headless(self, monkeypatch, user_scope):
        unit = _render_unit(monkeypatch, user_scope=user_scope, headless=True)
        assert _exec_start(unit) == [f'ExecStart="{_BIN}" gateway --no-open --headless']

    @pytest.mark.parametrize("user_scope", [False, True])
    def test_unit_is_ordinary_without_the_flag(self, monkeypatch, user_scope):
        unit = _render_unit(monkeypatch, user_scope=user_scope)
        assert _exec_start(unit) == [f'ExecStart="{_BIN}" gateway --no-open']
        assert "headless" not in unit

    def test_plist_starts_the_gateway_headless(self, monkeypatch, tmp_path):
        payload = plistlib.loads(_render_plist(monkeypatch, tmp_path, headless=True).encode())
        assert payload["ProgramArguments"] == [
            str(tmp_path / "live-gateway"),
            "gateway",
            "--no-open",
            "--headless",
        ]

    def test_plist_is_ordinary_without_the_flag(self, monkeypatch, tmp_path):
        plist = _render_plist(monkeypatch, tmp_path)
        assert plistlib.loads(plist.encode())["ProgramArguments"][1:] == ["gateway", "--no-open"]
        assert "headless" not in plist

    def test_a_headless_definition_carries_no_bind_address(self, monkeypatch, tmp_path):
        """The installer's own bind and URL settings never reach the definition.

        A headless gateway binds loopback whatever it is told
        (``headless.bind_address``); the definition also gives it nothing to be told.
        """
        from kiro_crew.dashboard.headless import bind_address

        monkeypatch.setenv("KIROCREW_BIND", "0.0.0.0")
        monkeypatch.setenv("KIROCREW_DASHBOARD_URL", "http://0.0.0.0:5476")
        for text in (
            _render_unit(monkeypatch, headless=True),
            _render_unit(monkeypatch, user_scope=True, headless=True),
            _render_plist(monkeypatch, tmp_path, headless=True),
        ):
            assert "KIROCREW_BIND" not in text
            assert "0.0.0.0" not in text
            assert "--host" not in text
        assert bind_address(True, "0.0.0.0") == "127.0.0.1"

    def test_the_selinux_remedy_prints_the_headless_unit(self, monkeypatch):
        monkeypatch.setenv("USER", "tester")
        monkeypatch.setattr(svc_common, "_running_install_bin", lambda: None)
        with patch("kiro_crew.service.common.shutil.which", return_value=_BIN):
            headless = svc_linux.selinux_refusal("denied", headless=True)
            ordinary = svc_linux.selinux_refusal("denied")
        assert f'ExecStart="{_BIN}" gateway --no-open --headless\n' in headless
        assert "--headless" not in ordinary


#: Answers of the manager from which no mode can be read.
_NO_MODE = {
    "no-answer": {},
    "no-start-command": {"Id": [f"{SERVICE_NAME}.service"], "LoadState": ["loaded"]},
    "two-start-commands": {
        **_loaded(True),
        "ExecStart": [_shown(f"{_BIN} gateway --headless"), _shown(f"{_BIN} gateway")],
    },
    "a-value-in-another-shape": {**_loaded(), "ExecStart": [f"{_BIN} gateway --headless"]},
    "no-words": _loaded(words=""),
    "a-variable": _loaded(words=f"{_BIN} gateway $FLAGS"),
    "not-loaded": {**_loaded(True), "LoadState": ["not-found"]},
    "an-alias-of-another-unit": {**_loaded(True), "Id": ["other.service"]},
    "an-ambiguous-abbreviation": _loaded(words=f"{_BIN} gateway --he"),
    "a-help-flag": _loaded(words=f"{_BIN} gateway --help"),
    "the-manager-cannot-be-asked": gateway_restart._Inconclusive("no trusted systemctl"),
    "the-call-fails": OSError("exec failed"),
    "the-call-times-out": subprocess.TimeoutExpired("systemctl", 3.0),
}


class TestInstalledMode:
    def test_no_service_installed_is_ordinary_and_the_manager_is_not_asked(self, systemd, manager):
        manager[False] = AssertionError("asked the manager about a service that is not installed")
        assert controller.installed_headless() is False

    def test_a_named_scope_is_asked_even_with_no_definition_file(self, systemd, manager):
        """A scope the manager restarted has a service, whatever the file lookup says."""
        manager[False] = _loaded(True)
        assert controller.installed_headless("system") is True

    @pytest.mark.parametrize("headless", [True, False])
    def test_the_loaded_command_is_read(self, systemd, manager, headless):
        systemd.write_bytes(b"")
        manager[False] = _loaded(headless)
        assert controller.installed_headless() is headless

    def test_the_unit_file_is_not_parsed(self, systemd, manager):
        """What the manager has loaded decides, whatever the file says."""
        systemd.write_bytes(f"[Service]\nExecStart={_BIN} gateway --headless\n".encode())
        manager[False] = _loaded(False)
        assert controller.installed_headless() is False

    def test_an_abbreviated_flag_counts(self, systemd, manager):
        systemd.write_bytes(b"")
        manager[False] = _loaded(words=f"{_BIN} gateway --headl")
        assert controller.installed_headless() is True

    @pytest.mark.parametrize("answer", _NO_MODE.values(), ids=_NO_MODE.keys())
    def test_an_answer_with_no_readable_command_is_unknown(self, systemd, manager, answer):
        systemd.write_bytes(b"")
        manager[False] = answer
        assert controller.installed_headless() is None

    def test_the_user_scope_asks_the_user_manager(self, user_unit, manager):
        manager[True] = _loaded(True)
        assert controller.installed_headless() is True
        assert controller.installed_headless("user") is True

    def test_a_user_scope_that_cannot_be_reached_is_unknown(self, user_unit, manager, monkeypatch):
        manager[True] = AssertionError("asked a user manager that is not this account's")
        monkeypatch.setattr(svc_linux, "_user_scope_unreachable_reason", lambda: "a root shell")
        assert controller.installed_headless("user") is None

    @pytest.mark.parametrize(
        ("stdout", "expected"),
        [
            (
                f"Id={SERVICE_NAME}.service\nLoadState=loaded\nMainPID=0\n"
                f"ExecStart={_shown(f'{_BIN} gateway --no-open --headless')}\n",
                True,
            ),
            (
                f"Id={SERVICE_NAME}.service\nLoadState=loaded\nMainPID=0\n"
                f"ExecStart={_shown(f'{_BIN} gateway --no-open')}\n",
                False,
            ),
            ("", None),
        ],
        ids=["headless", "ordinary", "no-output"],
    )
    def test_the_output_of_systemctl_show_is_read(self, monkeypatch, tmp_path, stdout, expected):
        """Through the real ``_systemd_show``, with only the spawn faked."""
        unit = tmp_path / f"{SERVICE_NAME}.service"
        unit.write_bytes(b"")
        monkeypatch.setattr(controller, "current_platform", lambda: Platform.SYSTEMD)
        monkeypatch.setattr(svc_linux, "UNIT_PATH", unit)
        monkeypatch.setattr(
            gateway_restart.platform_compat, "trusted_system_bin", lambda name: f"/usr/bin/{name}"
        )
        spawned = MagicMock(return_value=MagicMock(returncode=0, stdout=stdout, stderr=""))
        monkeypatch.setattr(gateway_restart.subprocess, "run", spawned)
        assert controller.installed_headless() is expected
        assert spawned.call_args.args[0][:2] == ["/usr/bin/systemctl", "show"]

    # Lines a real service manager printed for `systemctl show -p ExecStart`,
    # copied as published, so the reader meets text this repository did not write.
    _PUBLISHED_AT = (
        "https://michael.stapelberg.ch/posts/2013-07-27-grep-with-line-continuations/",
        "https://forum.manjaro.org/t/which-iscsi-service-to-connect-to-iscsi-at-boot/158861/20",
    )
    _PUBLISHED_2013 = (
        "ExecStart={ path=/usr/bin/source-backend ; argv[]=/usr/bin/source-backend "
        "-unpacked_path=/dcs/unpacked-new/ -listen_address=localhost:30000 ; ignore_errors=no ; "
        "start_time=[Sat 2013-07-27 19:58:23 CEST] ; stop_time=[n/a] ; pid=12428 ; "
        "code=(null) ; status=0/0 }"
    )
    _PUBLISHED_2024 = (
        "ExecStart={ path=/usr/bin/iscsiadm ; argv[]=/usr/bin//iscsiadm -m node "
        "--loginall=automatic -W ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; "
        "pid=0 ; code=(null) ; status=0/0 }"
    )

    @pytest.mark.parametrize(
        ("lines", "expected"),
        [
            (
                [_PUBLISHED_2013],
                [
                    "/usr/bin/source-backend",
                    "-unpacked_path=/dcs/unpacked-new/",
                    "-listen_address=localhost:30000",
                ],
            ),
            (
                [_PUBLISHED_2024],
                ["/usr/bin//iscsiadm", "-m", "node", "--loginall=automatic", "-W"],
            ),
            ([_PUBLISHED_2024, _PUBLISHED_2013], None),
        ],
        ids=["a-running-unit-2013", "a-stopped-unit-2024", "two-commands"],
    )
    def test_lines_a_real_manager_printed_are_read(self, monkeypatch, lines, expected):
        """The words between ``argv[]=`` and ``; ignore_errors=`` are the command."""
        stdout = "".join(
            f"{line}\n" for line in [f"Id={SERVICE_NAME}.service", "LoadState=loaded", *lines]
        )
        monkeypatch.setattr(
            gateway_restart.platform_compat, "trusted_system_bin", lambda name: f"/usr/bin/{name}"
        )
        spawned = MagicMock(return_value=MagicMock(returncode=0, stdout=stdout, stderr=""))
        monkeypatch.setattr(gateway_restart.subprocess, "run", spawned)
        assert controller._loaded_argv("system") == expected

    @pytest.mark.parametrize("headless", [True, False])
    def test_a_rendered_plist_reads_back(self, launchd, monkeypatch, tmp_path, headless):
        launchd.write_text(_render_plist(monkeypatch, tmp_path, headless=headless))
        assert controller.installed_headless() is headless

    def test_a_malformed_plist_is_unknown(self, launchd):
        launchd.write_text("<plist><dict><key>ProgramArguments</key>")
        assert controller.installed_headless() is None


class TestInstallKeepsTheMode:
    @pytest.mark.parametrize("platform", ["systemd", "launchd"])
    def test_a_first_install_is_ordinary(self, request, installs, capsys, platform):
        request.getfixturevalue(platform)
        assert controller.install_service() == 0
        assert installs == [False]
        out = capsys.readouterr().out
        assert "mode:" not in out
        assert "kirocrew token   (prints a sign-in link" in out

    @pytest.mark.parametrize("platform", ["systemd", "launchd"])
    def test_the_flag_installs_headless(self, request, installs, capsys, platform):
        request.getfixturevalue(platform)
        assert controller.install_service(headless=True) == 0
        assert installs == [True]
        out = capsys.readouterr().out
        assert "   mode: headless (no dashboard; listens on loopback only)\n" in out
        assert "kirocrew token --bearer" in out
        assert "sign-in link" not in out

    def test_a_reinstall_without_the_flag_keeps_a_headless_unit(
        self, systemd, manager, installs, capsys
    ):
        systemd.write_bytes(b"")
        manager[False] = _loaded(True)
        assert controller.install_service() == 0
        assert installs == [True]
        out = capsys.readouterr().out
        assert "mode: headless" in out and "kept from the installed service" in out
        assert "kirocrew service install --no-headless" in out

    def test_a_reinstall_without_the_flag_keeps_a_headless_plist(self, launchd, installs):
        launchd.write_bytes(_plist(True))
        assert controller.install_service() == 0
        assert installs == [True]

    def test_a_reinstall_without_the_flag_keeps_an_ordinary_unit(self, systemd, manager, installs):
        systemd.write_bytes(b"")
        manager[False] = _loaded(False)
        assert controller.install_service() == 0
        assert installs == [False]

    def test_no_headless_drops_it(self, systemd, manager, installs, capsys):
        systemd.write_bytes(b"")
        manager[False] = _loaded(True)
        assert controller.install_service(headless=False) == 0
        assert installs == [False]
        assert "mode:" not in capsys.readouterr().out

    @staticmethod
    def _assert_ordinary_install_with_one_warning(installs, capsys, path) -> None:
        assert controller.install_service() == 0
        assert installs == [False]
        captured = capsys.readouterr()
        assert "mode:" not in captured.out
        assert "kirocrew token   (prints a sign-in link" in captured.out
        warning = captured.err.splitlines()
        assert len(warning) == 1
        assert warning[0].startswith("Warning: ")
        assert str(path) in warning[0]
        assert "installed with the dashboard" in warning[0]
        assert "`kirocrew service install --headless`" in warning[0]

    @pytest.mark.parametrize("answer", _NO_MODE.values(), ids=_NO_MODE.keys())
    def test_an_unknown_mode_installs_the_ordinary_service_and_warns(
        self, systemd, manager, installs, capsys, answer
    ):
        """A reinstall without a flag succeeds as it did before the flag existed."""
        systemd.write_bytes(b"")
        manager[False] = answer
        self._assert_ordinary_install_with_one_warning(installs, capsys, systemd)

    def test_an_unreadable_plist_installs_the_ordinary_service_and_warns(
        self, launchd, installs, capsys
    ):
        launchd.write_text("<plist><dict><key>ProgramArguments</key>")
        assert controller.installed_headless() is None
        self._assert_ordinary_install_with_one_warning(installs, capsys, launchd)

    @pytest.mark.parametrize("choice", [True, False])
    def test_a_flag_decides_over_an_unknown_mode_without_a_warning(
        self, systemd, manager, installs, capsys, choice
    ):
        systemd.write_bytes(b"")
        manager[False] = OSError("exec failed")
        assert controller.install_service(headless=choice) == 0
        assert installs == [choice]
        assert capsys.readouterr().err == ""

    @pytest.mark.parametrize(
        "after",
        [_loaded(False), OSError("exec failed"), subprocess.TimeoutExpired("systemctl", 3.0)],
        ids=["another-command-is-loaded", "the-call-fails", "the-call-times-out"],
    )
    def test_an_install_that_does_not_read_back_as_headless_says_unknown(
        self, systemd, manager, installs, capsys, after
    ):
        """A drop-in that replaces the command, or a manager that does not answer."""
        manager["after"] = after
        assert controller.install_service(headless=True) == 0
        out = capsys.readouterr().out
        assert "   mode: unknown (headless was installed, but the service's start command " in out
        assert "mode: headless" not in out
        assert "no dashboard is served" not in out
        assert "kirocrew token   (prints a sign-in link" in out

    def test_a_macos_install_that_cannot_read_its_plist_back_says_unknown(
        self, launchd, manager, installs, capsys
    ):
        manager["after"] = b"<plist><dict><key>ProgramArguments</key>"
        assert controller.install_service(headless=True) == 0
        out = capsys.readouterr().out
        assert "   mode: unknown (" in out
        assert "mode: headless" not in out
        assert "no dashboard is served" not in out

    def test_a_reinstall_whose_unit_lookup_raises_twice_still_installs(
        self, systemd, installs, monkeypatch, capsys
    ):
        """The warning's own lookup of the unit path fails the way the read did."""

        def denied():
            raise PermissionError("unit directory")

        monkeypatch.setattr(controller, "installed_unit_path", denied)
        assert controller.install_service() == 0
        assert installs == [False]
        err = capsys.readouterr().err
        assert err == (
            "Warning: could not read whether the installed service starts the gateway "
            "with --headless, so it is installed with the dashboard; "
            "`kirocrew service install --headless` installs it without the dashboard.\n"
        )

    def test_the_linux_install_writes_the_unit_it_was_asked_for(self, monkeypatch):
        """``linux.install(headless=True)`` stages the headless unit, not the ordinary one."""
        monkeypatch.setenv("USER", "tester")
        monkeypatch.setattr(svc_common, "_running_install_bin", lambda: None)
        monkeypatch.setattr(svc_linux.os, "geteuid", lambda: 1000, raising=False)
        written: list[str] = []
        ok = MagicMock(returncode=0, stdout="", stderr="")

        def write(contents):
            written.append(contents)
            return ok

        monkeypatch.setattr(svc_linux, "_write_unit_via_sudo", write)
        with (
            patch("kiro_crew.service.common.shutil.which", return_value=_BIN),
            patch("kiro_crew.service.linux.subprocess.run", return_value=ok),
        ):
            svc_linux.install(headless=True)
        assert [_exec_start(unit) for unit in written] == [
            [f'ExecStart="{_BIN}" gateway --no-open --headless']
        ]

    def test_the_macos_install_writes_the_plist_it_was_asked_for(
        self, launchd, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(svc_macos, "PLIST_DIR", launchd.parent)
        monkeypatch.setattr(svc_macos, "LOG_DIR", tmp_path / "Logs")
        monkeypatch.setattr(svc_macos, "LIVE_PROGRAM", tmp_path / "live-gateway")
        monkeypatch.setattr(svc_macos, "_BOOTOUT_SETTLE_SECS", 0.0)
        monkeypatch.setattr(svc_common, "_running_install_bin", lambda: None)
        ok = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("kiro_crew.service.common.shutil.which", return_value=_BIN),
            patch("kiro_crew.service.macos.subprocess.run", return_value=ok),
        ):
            svc_macos.install(headless=True)
            assert controller.installed_headless() is True
            svc_macos.install()
            assert controller.installed_headless() is False


class TestStatus:
    def _status(self, monkeypatch, capsys) -> str:
        monkeypatch.setattr(svc_linux, "status", lambda: "system scope: active (running)")
        monkeypatch.setattr(svc_linux, "is_up", lambda: True)
        assert controller.service_status() == 0
        return capsys.readouterr().out.removesuffix("system scope: active (running)\n")

    def test_a_headless_service_says_so_first(self, systemd, manager, monkeypatch, capsys):
        systemd.write_bytes(b"")
        manager[False] = _loaded(True)
        assert self._status(monkeypatch, capsys) == _HEADLESS_LINE

    def test_an_ordinary_service_prints_what_it_printed(
        self, systemd, manager, monkeypatch, capsys
    ):
        systemd.write_bytes(b"")
        manager[False] = _loaded(False)
        assert self._status(monkeypatch, capsys) == ""

    @pytest.mark.parametrize("answer", _NO_MODE.values(), ids=_NO_MODE.keys())
    def test_an_unknown_mode_is_named_unknown(self, systemd, manager, monkeypatch, capsys, answer):
        """Unknown is reported as unknown: never headless, never left out."""
        systemd.write_bytes(b"")
        manager[False] = answer
        assert self._status(monkeypatch, capsys) == _UNKNOWN_LINE

    def test_a_read_that_raises_is_named_unknown(self, systemd, monkeypatch, capsys):
        def boom(scope=None):
            raise RuntimeError("parser failure")

        systemd.write_bytes(b"")
        monkeypatch.setattr(controller, "installed_headless", boom)
        assert self._status(monkeypatch, capsys) == _UNKNOWN_LINE

    def test_two_scopes_each_name_their_own_mode(
        self, systemd, user_unit, manager, monkeypatch, capsys
    ):
        """A stopped ordinary system unit does not speak for the per-user one."""
        systemd.write_bytes(b"")
        manager[False] = _loaded(False)
        manager[True] = _loaded(True)
        assert self._status(monkeypatch, capsys) == (
            "mode (user unit): headless (no dashboard; the gateway listens on loopback only)\n"
        )


class TestRestartedMode:
    """The line after a service restart follows the scope that was restarted."""

    @staticmethod
    def _report(*scopes: str) -> RestartReport:
        return RestartReport(tuple(ScopeRestart(scope, True) for scope in scopes))

    @pytest.fixture
    def two_scopes(self, systemd, user_unit, manager):
        """An ordinary system unit beside a headless per-user unit."""
        systemd.write_bytes(b"")
        manager[False] = _loaded(False)
        manager[True] = _loaded(True)

    def test_the_restarted_scope_is_the_one_read(self, two_scopes):
        assert controller.restarted_headless(self._report("user")) is True
        assert controller.restarted_headless(self._report("system")) is False

    def test_two_restarted_scopes_that_disagree_are_unknown(self, two_scopes):
        assert controller.restarted_headless(self._report("system", "user")) is None

    def test_a_scope_that_did_not_come_back_is_not_read(self, two_scopes):
        report = RestartReport((ScopeRestart("user", False), ScopeRestart("system", True)))
        assert controller.restarted_headless(report) is False

    def test_a_restarted_plist_is_read(self, launchd, monkeypatch, tmp_path):
        launchd.write_text(_render_plist(monkeypatch, tmp_path, headless=True))
        assert controller.restarted_headless(self._report("launchd")) is True

    @pytest.mark.parametrize("answer", _NO_MODE.values(), ids=_NO_MODE.keys())
    def test_an_answer_with_no_readable_command_is_unknown(self, two_scopes, manager, answer):
        manager[True] = answer
        assert controller.restarted_headless(self._report("user")) is None

    def test_a_read_that_raises_is_unknown(self, two_scopes, monkeypatch):
        def boom(scope):
            raise RuntimeError("parser failure")

        monkeypatch.setattr(controller, "_loaded_argv", boom)
        assert controller.restarted_headless(self._report("user")) is None

    @pytest.mark.parametrize(
        ("answer", "printed"),
        [
            (_loaded(False), "\n🔑 Sign in: run kirocrew token for a sign-in link\n"),
            (
                OSError("exec failed"),
                f"   {_UNKNOWN_LINE}\n🔑 Sign in: run kirocrew token for a sign-in link\n",
            ),
        ],
        ids=["ordinary", "unknown"],
    )
    def test_the_restart_never_calls_an_unknown_mode_headless(
        self, systemd, manager, monkeypatch, capsys, answer, printed
    ):
        systemd.write_bytes(b"")
        manager[False] = answer
        monkeypatch.setattr(cli_server, "resolve_client_port", lambda port: 7000)
        monkeypatch.setattr(cli_server, "_print_running_cron_runs", lambda: None)
        monkeypatch.setattr(cli_server, "sel", lambda: MagicMock())
        monkeypatch.setattr(cli_server.sys.stdout, "isatty", lambda: False)
        monkeypatch.setattr(
            cli_server.service_controller, "restart_service", lambda: self._report("system")
        )
        cli_server._restart(None)
        assert capsys.readouterr().out == "✅ Restarted kirocrew service.\n" + printed


class TestCommandLine:
    @pytest.mark.parametrize(
        "argv,expected",
        [
            (["service", "install"], None),
            (["service", "install", "--headless"], True),
            (["service", "install", "--no-headless"], False),
        ],
    )
    def test_the_flag_reaches_the_controller_and_the_audit(self, monkeypatch, argv, expected):
        parser = argparse.ArgumentParser()
        cli_parser_gateway.register_service_parser(parser.add_subparsers(dest="command"))
        seen: list[object] = []
        audit = MagicMock()
        monkeypatch.setattr(cli_server, "sel", lambda: audit)
        monkeypatch.setattr(
            cli_server.service_controller,
            "install_service",
            lambda *, headless=None: seen.append(headless) or 0,
        )
        assert cli_server._service_cmd(parser.parse_args(argv)) == 0
        assert seen == [expected]
        label = {None: "keep", True: "1", False: "0"}[expected]
        assert audit.log_api_access.call_args.kwargs["resources"] == f"rc=0 headless={label}"
