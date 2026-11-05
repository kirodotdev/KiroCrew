"""`platform_compat.reveal_in_file_manager` — argv, environment, degradation.

The dashboard handler that calls this is tested for policy (which target, what the
response and the audit say); the launch mechanics are pinned here, where they live.
Three properties matter and each has a reason to exist:

* the launcher is an ABSOLUTE path, never a bare argv name — a gateway's ``PATH``
  can lead with an agent-writable directory, so a bare name would let a planted
  shim run on a click the user initiated;
* ``PATH`` is replaced for the spawn, because ``xdg-open`` is a shell script that
  dispatches to ``gio`` / ``gvfs-open`` / ``exo-open`` through ``PATH`` — pinning
  the binary alone leaves the hole open one level down;
* an absent or unrunnable launcher answers ``False`` rather than raising, so the
  caller can degrade instead of failing a user's click.
"""
from __future__ import annotations

import contextlib
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import platform_compat, platform_compat_trust
from kiro_crew.service import common as service_common


def _no_user_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing to ask about a desktop session but the test's own environment.

    The lookup is a real ``systemctl --user`` on the machine running the suite, so
    a developer's desktop login would otherwise decide a headless test. The kept
    answer is cleared too, so no earlier test's lookup is reused.
    ``raising=False`` keeps the stub harmless where the lookup does not exist.
    """
    monkeypatch.setattr(platform_compat_trust, "_user_manager_environment", lambda: None, raising=False)
    monkeypatch.setattr(platform_compat_trust, "_desktop_session_cache", None, raising=False)


@contextlib.contextmanager
def _linux_host_with_xdg_open() -> Iterator[None]:
    with patch("sys.platform", "linux"), \
         patch.object(platform_compat, "IS_WINDOWS", False), \
         patch("os.path.isfile", return_value=True):
        yield


class TestRevealInFileManager:
    @pytest.fixture(autouse=True)
    def _a_desktop_session(self, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
        # The Linux arms spawn only into a desktop session (see
        # `desktop_launcher_available`) and a CI runner has none, so every test
        # starts with an X11 display named; a test about its absence removes it
        # through its own `monkeypatch`, which unwinds before this one does.
        _floor_monkeypatch.setenv("DISPLAY", ":0")
        _floor_monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        _no_user_manager(_floor_monkeypatch)

    def test_macos_selects_the_file_with_an_absolute_open(self) -> None:
        with patch("sys.platform", "darwin"), \
             patch("os.path.isfile", return_value=True), \
             patch("subprocess.Popen") as popen:
            assert platform_compat.reveal_in_file_manager("/w/x.md") is True
        assert popen.call_args.args[0] == ["/usr/bin/open", "-R", "/w/x.md"]

    def test_open_with_default_app_launches_the_file_on_macos(self) -> None:
        with patch("sys.platform", "darwin"), \
             patch.object(platform_compat, "IS_WINDOWS", False), \
             patch("os.path.isfile", return_value=True), \
             patch("subprocess.Popen") as popen:
            assert platform_compat.open_with_default_app("/w/x.md") is True
        assert popen.call_args.args[0] == ["/usr/bin/open", "/w/x.md"]

    def test_open_with_default_app_does_not_wait_by_default(self) -> None:
        # Every existing caller relies on "started" being the whole answer.
        with patch("sys.platform", "linux"), \
             patch.object(platform_compat, "IS_WINDOWS", False), \
             patch("os.path.isfile", return_value=True), \
             patch("subprocess.Popen") as popen:
            assert platform_compat.open_with_default_app("/w/x.md") is True
        assert popen.call_args.args[0] == ["/usr/bin/xdg-open", "/w/x.md"]
        popen.return_value.wait.assert_not_called()

    def test_open_with_default_app_wait_reads_the_launcher_exit(self) -> None:
        # `open` exits non-zero when no application handles the target, and
        # xdg-open exits 3/4 when no handler is registered.
        for platform in ("darwin", "linux"):
            for returncode, expected in ((0, True), (1, False), (3, False)):
                with patch("sys.platform", platform), \
                     patch.object(platform_compat, "IS_WINDOWS", False), \
                     patch("os.path.isfile", return_value=True), \
                     patch("subprocess.Popen") as popen:
                    popen.return_value.wait.return_value = returncode
                    assert platform_compat.open_with_default_app(
                        "kirocrew://", wait_for_exit=2.0) is expected
                popen.return_value.wait.assert_called_once_with(timeout=2.0)

    def test_open_with_default_app_still_running_at_the_deadline_is_a_launch(self) -> None:
        # xdg-open's generic path runs the application in the foreground.
        with patch("sys.platform", "linux"), \
             patch.object(platform_compat, "IS_WINDOWS", False), \
             patch("os.path.isfile", return_value=True), \
             patch("subprocess.Popen") as popen:
            popen.return_value.wait.side_effect = subprocess.TimeoutExpired(
                ["/usr/bin/xdg-open", "kirocrew://"], 2.0)
            assert platform_compat.open_with_default_app(
                "kirocrew://", wait_for_exit=2.0) is True

    def test_open_with_default_app_wait_with_no_launcher_answers_false(self) -> None:
        with patch("sys.platform", "linux"), \
             patch.object(platform_compat, "IS_WINDOWS", False), \
             patch("os.path.isfile", return_value=False), \
             patch("subprocess.Popen") as popen:
            assert platform_compat.open_with_default_app(
                "kirocrew://", wait_for_exit=2.0) is False
        popen.assert_not_called()

    def test_open_with_default_app_is_refused_on_windows(self) -> None:
        # Launching by association goes through the shell and the path usually
        # comes from a request; the caller degrades instead. `isfile` is forced
        # true so the ONLY thing that can produce False here is the Windows
        # refusal — otherwise a host without xdg-open passes this vacuously.
        with patch.object(platform_compat, "IS_WINDOWS", True), \
             patch("os.path.isfile", return_value=True), \
             patch("subprocess.Popen") as popen:
            assert platform_compat.open_with_default_app("C:\\w\\x.md") is False
        popen.assert_not_called()

    def test_a_file_is_revealed_in_its_folder_not_launched(self) -> None:
        # explorer.exe <file> would launch the file's associated application, so
        # the folder derivation is enforced here rather than at the call site.
        for platform, windows, expected_bin in (
                ("linux", False, "/usr/bin/xdg-open"),
                ("win32", True, r"C:\Windows\explorer.exe")):
            with patch("sys.platform", platform), \
                 patch.object(platform_compat, "IS_WINDOWS", windows), \
                 patch("os.path.isfile", return_value=True), \
                 patch("subprocess.Popen") as popen:
                assert platform_compat.reveal_in_file_manager("/w/dir/x.md") is True
            assert popen.call_args.args[0] == [expected_bin, "/w/dir"]

    def test_a_target_with_no_parent_is_declined(self) -> None:
        # A bare name has no containing folder to open, and the folder derivation
        # is unconditional, so there is nothing safe to spawn.
        with patch("sys.platform", "linux"), \
             patch.object(platform_compat, "IS_WINDOWS", False), \
             patch("os.path.isfile", return_value=True), \
             patch("subprocess.Popen") as popen:
            assert platform_compat.reveal_in_file_manager("bare-name") is False
        popen.assert_not_called()

    def test_linux_uses_an_absolute_xdg_open(self) -> None:
        with patch("sys.platform", "linux"), \
             patch.object(platform_compat, "IS_WINDOWS", False), \
             patch("os.path.isfile", return_value=True), \
             patch("subprocess.Popen") as popen:
            assert platform_compat.reveal_in_file_manager("/w/x.md") is True
        assert popen.call_args.args[0] == ["/usr/bin/xdg-open", "/w"]

    def test_windows_uses_an_absolute_explorer(self) -> None:
        # A POSIX-shaped target on purpose: this test runs on Linux, where
        # `os.path` is posixpath and a backslash is an ordinary character, so
        # `dirname(r"C:\w\x.md")` would be empty and decline before reaching the
        # spawn. On a real Windows gateway `os.path` is ntpath and the separator
        # works; what is pinned here is the branch and its argv, not separator
        # handling.
        with patch("sys.platform", "win32"), \
             patch.object(platform_compat, "IS_WINDOWS", True), \
             patch("os.path.isfile", return_value=True), \
             patch("subprocess.Popen") as popen:
            assert platform_compat.reveal_in_file_manager("/w/x.md") is True
        assert popen.call_args.args[0] == [r"C:\Windows\explorer.exe", "/w"]

    def test_posix_spawn_gets_a_system_only_path(self) -> None:
        with patch("sys.platform", "linux"), \
             patch.object(platform_compat, "IS_WINDOWS", False), \
             patch("os.path.isfile", return_value=True), \
             patch.dict(os.environ, {"PATH": "agent-writable-first", "DISPLAY": ":0"}), \
             patch("subprocess.Popen") as popen:
            assert platform_compat.reveal_in_file_manager("/w/x.md") is True
            # Read the expectation INSIDE the patch: on a Windows host
            # `trusted_system_path()` answers None once the real IS_WINDOWS is
            # restored, so comparing after the block would make this assertion
            # pass or fail depending on which OS ran it.
            expected_path = platform_compat.trusted_system_path()
        env = popen.call_args.kwargs["env"]
        assert env["PATH"] == expected_path
        assert env["PATH"] != "agent-writable-first"
        # The rest of the environment survives: DISPLAY / DBUS_SESSION_BUS_ADDRESS /
        # XDG_* are what let a launcher reach the running desktop session.
        assert env["DISPLAY"] == ":0"

    def test_a_missing_launcher_answers_false_without_spawning(self) -> None:
        for platform, windows in (("darwin", False), ("linux", False), ("win32", True)):
            with patch("sys.platform", platform), \
                 patch.object(platform_compat, "IS_WINDOWS", windows), \
                 patch("os.path.isfile", return_value=False), \
                 patch("subprocess.Popen") as popen:
                assert platform_compat.reveal_in_file_manager("/w/x.md") is False
            popen.assert_not_called()

    @pytest.mark.parametrize("present", [True, False])
    @pytest.mark.parametrize(
        ("platform", "windows", "expected_bin"),
        [
            ("darwin", False, "/usr/bin/open"),
            ("linux", False, "/usr/bin/xdg-open"),
            ("win32", True, r"C:\Windows\explorer.exe"),
        ],
    )
    def test_desktop_launcher_available_checks_the_launcher_each_platform_spawns(
        self, platform: str, windows: bool, expected_bin: str, present: bool
    ) -> None:
        # The dashboard reads this to decide whether to offer the two hand-offs at
        # all, so it must name the same absolute launcher the spawn uses (with a
        # desktop session named, from the fixture, for the Linux arm).
        with patch("sys.platform", platform), \
             patch.object(platform_compat, "IS_WINDOWS", windows), \
             patch("os.path.isfile", return_value=present) as isfile:
            assert platform_compat.desktop_launcher_available() is present
        isfile.assert_called_once_with(expected_bin)

    @pytest.mark.parametrize(
        ("display", "wayland", "ready"),
        [
            (None, None, False),
            ("", "", False),
            (":0", None, True),
            (None, "wayland-0", True),
        ],
    )
    def test_linux_needs_a_desktop_session_as_well_as_xdg_open(
        self, monkeypatch: pytest.MonkeyPatch, display: str | None, wayland: str | None, ready: bool
    ) -> None:
        # A headless box with xdg-utils installed has the launcher and nothing
        # for it to open into, so it cannot open a file manager right now.
        for name, value in (("DISPLAY", display), ("WAYLAND_DISPLAY", wayland)):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        with patch("sys.platform", "linux"), \
             patch.object(platform_compat, "IS_WINDOWS", False), \
             patch("os.path.isfile", return_value=True):
            assert platform_compat.desktop_launcher_available() is ready

    def test_macos_needs_no_display_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `open` reaches the logged-in session through LaunchServices, not
        # through DISPLAY, so its absence there says nothing.
        monkeypatch.delenv("DISPLAY", raising=False)
        with patch("sys.platform", "darwin"), \
             patch("os.path.isfile", return_value=True):
            assert platform_compat.desktop_launcher_available() is True

    def test_no_desktop_session_means_neither_hand_off_spawns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The launch asks the same question the dashboard does, so a click that
        # reaches /api/reveal on a headless host degrades instead of starting an
        # xdg-open that has nowhere to show a window.
        monkeypatch.delenv("DISPLAY", raising=False)
        with patch("sys.platform", "linux"), \
             patch.object(platform_compat, "IS_WINDOWS", False), \
             patch("os.path.isfile", return_value=True), \
             patch("subprocess.Popen") as popen:
            assert platform_compat.reveal_in_file_manager("/w/x.md") is False
            assert platform_compat.open_with_default_app("/w/x.md") is False
        popen.assert_not_called()

    def test_no_desktop_launcher_means_neither_hand_off_spawns(self) -> None:
        # One readiness answer for the dashboard and the launch: when it says no,
        # a click cannot start a launcher.
        with patch("sys.platform", "linux"), \
             patch.object(platform_compat, "IS_WINDOWS", False), \
             patch.object(platform_compat, "desktop_launcher_available", return_value=False), \
             patch("os.path.isfile", return_value=True), \
             patch("subprocess.Popen") as popen:
            assert platform_compat.reveal_in_file_manager("/w/x.md") is False
            assert platform_compat.open_with_default_app("/w/x.md") is False
        popen.assert_not_called()

    def test_a_launcher_that_refuses_to_run_answers_false(self) -> None:
        # Installed but unrunnable: AppLocker, a revoked exec bit, an exhausted
        # process table. The caller degrades; it must not see the OSError.
        with patch("sys.platform", "linux"), \
             patch.object(platform_compat, "IS_WINDOWS", False), \
             patch("os.path.isfile", return_value=True), \
             patch("subprocess.Popen", side_effect=OSError("blocked by policy")):
            assert platform_compat.reveal_in_file_manager("/w/x.md") is False

    def test_trusted_system_path_is_the_pinned_directories(self) -> None:
        with patch.object(platform_compat, "IS_WINDOWS", False):
            pinned = platform_compat.trusted_system_path()
        assert pinned is not None
        # Every entry is one of the trusted directories, in order.
        assert pinned.split(os.pathsep) == list(platform_compat._TRUSTED_SYSTEM_BIN_DIRS)

    def test_trusted_system_path_is_none_on_windows(self) -> None:
        # Windows helpers live beside their install rather than on a search path.
        with patch.object(platform_compat, "IS_WINDOWS", True):
            assert platform_compat.trusted_system_path() is None


class TestDesktopSessionFromTheUserManager:
    """A gateway started outside the desktop login asks the user's systemd manager.

    The installed Linux service is a system unit, so its environment names no
    display server even while the user sits at a GNOME or KDE login. That login
    exports its display variables to the user's systemd manager, and the launchers
    read them from there: only four allowlisted variables, each checked, and a
    display only while its socket exists. A manager that names no live display is
    a headless host, whose rows stay hidden and whose clicks stay refused.
    """

    @pytest.fixture(autouse=True)
    def _a_service_gateway(self, _floor_monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # The service's own environment: no display variables, and the runtime
        # directory the unit sets. The X11 socket directory is the test's own,
        # so a display server on the machine running the suite is never seen.
        for name in (
            "DISPLAY",
            "WAYLAND_DISPLAY",
            "XAUTHORITY",
            "XDG_CURRENT_DESKTOP",
            "KDE_SESSION_VERSION",
        ):
            _floor_monkeypatch.delenv(name, raising=False)
        for name in ("run-user", "x11"):
            (tmp_path / name).mkdir()
        _floor_monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run-user"))
        _floor_monkeypatch.setattr(
            platform_compat_trust, "_X11_SOCKET_DIR", str(tmp_path / "x11"), raising=False
        )
        _no_user_manager(_floor_monkeypatch)

    @staticmethod
    def _manager_says(monkeypatch: pytest.MonkeyPatch, *lines: str) -> list[list[str]]:
        """Make the user manager's environment *lines*; returns one entry per lookup."""
        answers: list[list[str]] = []

        def show_environment() -> list[str]:
            answers.append(list(lines))
            return list(lines)

        monkeypatch.setattr(
            platform_compat_trust, "_user_manager_environment", show_environment, raising=False
        )
        return answers

    def test_a_wayland_login_reaches_the_launcher(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "run-user" / "wayland-0").touch()
        for name in ("LD_PRELOAD", "GTK_MODULES"):
            monkeypatch.delenv(name, raising=False)
        self._manager_says(
            monkeypatch,
            "HOME=/home/u",
            "WAYLAND_DISPLAY=wayland-0",
            "XDG_CURRENT_DESKTOP=ubuntu:GNOME",
            "LD_PRELOAD=/tmp/x.so",
            "GTK_MODULES=gail",
            "PATH=/home/u/.local/bin:/usr/bin",
        )
        with _linux_host_with_xdg_open(), patch("subprocess.Popen") as popen:
            assert platform_compat.desktop_launcher_available() is True
            assert platform_compat.reveal_in_file_manager("/w/x.md") is True
            expected_path = platform_compat.trusted_system_path()
        assert popen.call_args.args[0] == ["/usr/bin/xdg-open", "/w"]
        env = popen.call_args.kwargs["env"]
        assert env["WAYLAND_DISPLAY"] == "wayland-0"
        assert env["XDG_CURRENT_DESKTOP"] == "ubuntu:GNOME"
        # Nothing outside the allowlist crosses over, and PATH stays pinned.
        assert "LD_PRELOAD" not in env and "GTK_MODULES" not in env
        assert env["PATH"] == expected_path

    def test_an_x11_login_brings_its_cookie_to_the_default_app(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "x11" / "X1").touch()
        self._manager_says(monkeypatch, "DISPLAY=:1", "XAUTHORITY=/run/user/1000/gdm/Xauthority")
        with _linux_host_with_xdg_open(), patch("subprocess.Popen") as popen:
            assert platform_compat.open_with_default_app("/w/x.md") is True
        assert popen.call_args.args[0] == ["/usr/bin/xdg-open", "/w/x.md"]
        env = popen.call_args.kwargs["env"]
        assert env["DISPLAY"] == ":1"
        assert env["XAUTHORITY"] == "/run/user/1000/gdm/Xauthority"

    def test_a_plasma_login_brings_its_session_version(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # xdg-open's KDE opener picks kde-open5 or kde-open by KDE_SESSION_VERSION;
        # without it, it calls Konqueror's kfmclient, which Plasma does not ship.
        (tmp_path / "run-user" / "wayland-0").touch()
        self._manager_says(
            monkeypatch, "WAYLAND_DISPLAY=wayland-0", "XDG_CURRENT_DESKTOP=KDE", "KDE_SESSION_VERSION=5"
        )
        with _linux_host_with_xdg_open(), patch("subprocess.Popen") as popen:
            assert platform_compat.reveal_in_file_manager("/w/x.md") is True
        env = popen.call_args.kwargs["env"]
        assert env["XDG_CURRENT_DESKTOP"] == "KDE"
        assert env["KDE_SESSION_VERSION"] == "5"

    def test_a_unit_without_session_pointers_still_reaches_a_wayland_login(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A unit rendered without the runtime directory and the bus address (the
        # service user's uid was unknown at install). Both come from the resolver
        # every `systemctl --user` spawn uses, so the login's Wayland socket is
        # still found, and the launcher gets what it needs to reach that login.
        runtime_dir = tmp_path / "run-user"
        (runtime_dir / "wayland-0").touch()
        (runtime_dir / "bus").touch()
        monkeypatch.delenv("XDG_RUNTIME_DIR")
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        monkeypatch.setattr(service_common, "session_runtime_dir", lambda: str(runtime_dir))
        self._manager_says(monkeypatch, "WAYLAND_DISPLAY=wayland-0", "XDG_CURRENT_DESKTOP=GNOME")
        with _linux_host_with_xdg_open(), patch("subprocess.Popen") as popen:
            assert platform_compat.desktop_launcher_available() is True
            assert platform_compat.reveal_in_file_manager("/w/x.md") is True
        env = popen.call_args.kwargs["env"]
        assert env["WAYLAND_DISPLAY"] == "wayland-0"
        assert env["XDG_RUNTIME_DIR"] == str(runtime_dir)
        assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={runtime_dir / 'bus'}"

    def test_the_longest_accepted_values_pass(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The upper edge of each check: a display with a screen, eight desktop
        # names, and a two-digit Plasma version.
        (tmp_path / "x11" / "X12").touch()
        desktops = "a:b:c:d:e:f:g:h"
        self._manager_says(
            monkeypatch, "DISPLAY=:12.3", f"XDG_CURRENT_DESKTOP={desktops}", "KDE_SESSION_VERSION=10"
        )
        with _linux_host_with_xdg_open(), patch("subprocess.Popen") as popen:
            assert platform_compat.reveal_in_file_manager("/w/x.md") is True
        env = popen.call_args.kwargs["env"]
        assert env["DISPLAY"] == ":12.3"
        assert env["XDG_CURRENT_DESKTOP"] == desktops
        assert env["KDE_SESSION_VERSION"] == "10"

    def test_a_logout_is_seen_on_a_kept_answer(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, manual_clock
    ) -> None:
        # The manager's answer is kept for a while, but whether its display is
        # live is asked again each time, so a click right after a logout copies
        # the path instead of starting an xdg-open with nowhere to show a window.
        manual_clock.install(monkeypatch, platform_compat_trust)
        socket = tmp_path / "run-user" / "wayland-0"
        socket.touch()
        answers = self._manager_says(monkeypatch, "WAYLAND_DISPLAY=wayland-0")
        with _linux_host_with_xdg_open(), patch("subprocess.Popen") as popen:
            assert platform_compat.desktop_launcher_available() is True
            socket.unlink()
            assert platform_compat.desktop_launcher_available() is False
            assert platform_compat.reveal_in_file_manager("/w/x.md") is False
        assert len(answers) == 1, "the kept answer was reused, not asked again"
        popen.assert_not_called()

    def test_a_headless_host_stays_hidden_and_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The service on a headless box: the user manager runs (lingering) and
        # its session bus answers, but it names no display. A reachable bus is
        # not a desktop.
        self._manager_says(
            monkeypatch,
            "HOME=/home/u",
            "XDG_RUNTIME_DIR=/run/user/1000",
            "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
        )
        with _linux_host_with_xdg_open(), patch("subprocess.Popen") as popen:
            assert platform_compat.desktop_launcher_available() is False
            assert platform_compat.reveal_in_file_manager("/w/x.md") is False
            assert platform_compat.open_with_default_app("/w/x.md") is False
        popen.assert_not_called()

    def test_no_user_manager_stays_hidden_and_refused(self) -> None:
        with _linux_host_with_xdg_open(), patch("subprocess.Popen") as popen:
            assert platform_compat.desktop_launcher_available() is False
            assert platform_compat.reveal_in_file_manager("/w/x.md") is False
        popen.assert_not_called()

    def test_a_display_whose_server_is_gone_is_not_a_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A lingering manager can keep the variables of a login that has ended.
        # With no socket listening there is nowhere to open a window.
        self._manager_says(
            monkeypatch, "DISPLAY=:0", "WAYLAND_DISPLAY=wayland-0", "XDG_CURRENT_DESKTOP=GNOME"
        )
        with _linux_host_with_xdg_open(), patch("subprocess.Popen") as popen:
            assert platform_compat.desktop_launcher_available() is False
            assert platform_compat.reveal_in_file_manager("/w/x.md") is False
        popen.assert_not_called()

    @pytest.mark.parametrize(
        "line",
        [
            "DISPLAY=remote-host:0",
            "DISPLAY=:0 ; touch /tmp/x",
            "DISPLAY=$':0\\n'",
            "DISPLAY=",
            "DISPLAY=:\u0660",
            "DISPLAY=:0\n",
            "WAYLAND_DISPLAY=/tmp/a-socket",
            "WAYLAND_DISPLAY=../wayland-0",
            "WAYLAND_DISPLAY=$'wayland-0\\n'",
            "WAYLAND_DISPLAY=wayland-0\n",
        ],
        ids=[
            "another-machine",
            "shell-text",
            "systemctl-quoted",
            "empty-display",
            "non-ascii-digit",
            "trailing-newline",
            "a-path",
            "parent-dir",
            "quoted-name",
            "name-with-newline",
        ],
    )
    def test_a_malformed_display_is_not_a_session(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, line: str
    ) -> None:
        # Every socket a well-formed value could name exists, so the shape alone
        # decides.
        (tmp_path / "run-user" / "wayland-0").touch()
        (tmp_path / "x11" / "X0").touch()
        self._manager_says(monkeypatch, line)
        with _linux_host_with_xdg_open(), patch("subprocess.Popen") as popen:
            assert platform_compat.desktop_launcher_available() is False
            assert platform_compat.reveal_in_file_manager("/w/x.md") is False
        popen.assert_not_called()

    @pytest.mark.parametrize(
        "line",
        [
            "XAUTHORITY=relative/Xauthority",
            "XAUTHORITY=/run/user/1000/../../etc/shadow",
            "XAUTHORITY=/home/u/my cookie",
            "XDG_CURRENT_DESKTOP=GNOME;rm -rf ~",
            "XDG_CURRENT_DESKTOP=$'GNOME\\n'",
            "XDG_CURRENT_DESKTOP=a:b:c:d:e:f:g:h:i",
            "XAUTHORITY=/run/user/1000/Xauthority\n",
            "KDE_SESSION_VERSION=5.27",
            "KDE_SESSION_VERSION=\u0665",
        ],
        ids=[
            "relative",
            "parent-dir",
            "space",
            "shell-text",
            "systemctl-quoted",
            "ninth-desktop-name",
            "cookie-with-newline",
            "kde-version-not-a-number",
            "kde-version-non-ascii-digit",
        ],
    )
    def test_a_malformed_companion_variable_is_left_out(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, line: str
    ) -> None:
        (tmp_path / "run-user" / "wayland-0").touch()
        self._manager_says(monkeypatch, "WAYLAND_DISPLAY=wayland-0", line)
        with _linux_host_with_xdg_open(), patch("subprocess.Popen") as popen:
            assert platform_compat.reveal_in_file_manager("/w/x.md") is True
        env = popen.call_args.kwargs["env"]
        assert env["WAYLAND_DISPLAY"] == "wayland-0"
        assert line.partition("=")[0] not in env

    def test_the_gateway_s_own_display_is_used_without_asking(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A gateway started inside the login launches with its own environment:
        # nothing is asked and nothing is filled in, not even a bus it could find.
        (tmp_path / "x11" / "X7").touch()
        (tmp_path / "run-user" / "bus").touch()
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        answers = self._manager_says(monkeypatch, "DISPLAY=:7")
        monkeypatch.setenv("DISPLAY", ":0")
        with _linux_host_with_xdg_open(), patch("subprocess.Popen") as popen:
            assert platform_compat.reveal_in_file_manager("/w/x.md") is True
        env = popen.call_args.kwargs["env"]
        assert env["DISPLAY"] == ":0"
        assert "DBUS_SESSION_BUS_ADDRESS" not in env
        assert answers == []

    def test_the_answer_is_kept_briefly_then_asked_again(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, manual_clock
    ) -> None:
        manual_clock.install(monkeypatch, platform_compat_trust)
        lines: list[str] = []
        lookups: list[int] = []

        def show_environment() -> list[str]:
            lookups.append(1)
            return list(lines)

        monkeypatch.setattr(
            platform_compat_trust, "_user_manager_environment", show_environment, raising=False
        )
        with _linux_host_with_xdg_open():
            assert platform_compat.desktop_launcher_available() is False
            assert platform_compat.desktop_launcher_available() is False
            assert len(lookups) == 1, "a second ask within the window must reuse the answer"
            # The user logs in to the desktop after the gateway started.
            (tmp_path / "run-user" / "wayland-0").touch()
            lines.append("WAYLAND_DISPLAY=wayland-0")
            assert platform_compat.desktop_launcher_available() is False
            assert len(lookups) == 1
            manual_clock.advance(30.0)
            assert platform_compat.desktop_launcher_available() is True
            assert len(lookups) == 2

    def test_macos_never_asks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        answers = self._manager_says(monkeypatch, "DISPLAY=:0")
        with patch("sys.platform", "darwin"), \
             patch.object(platform_compat, "IS_WINDOWS", False), \
             patch("os.path.isfile", return_value=True), \
             patch("subprocess.Popen") as popen:
            assert platform_compat.reveal_in_file_manager("/w/x.md") is True
        assert "DISPLAY" not in popen.call_args.kwargs["env"]
        assert answers == []


class TestUserManagerLookup:
    """The one spawn behind the session lookup: fixed argv, bounded, no shell."""

    def test_it_runs_one_fixed_argv_with_a_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, object] = {}

        def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            seen["argv"] = argv
            seen.update(kwargs)
            return subprocess.CompletedProcess(argv, 0, stdout="DISPLAY=:0\nHOME=/h\n", stderr="")

        monkeypatch.setattr(platform_compat_trust, "trusted_system_bin_quiet", lambda name: "/usr/bin/" + name)
        monkeypatch.setattr(subprocess, "run", run)
        with patch.object(platform_compat, "IS_WINDOWS", False):
            assert platform_compat_trust._user_manager_environment() == ["DISPLAY=:0", "HOME=/h"]
            expected_path = platform_compat.trusted_system_path()
        assert seen["argv"] == ["/usr/bin/systemctl", "--user", "show-environment"]
        assert seen["timeout"] == platform_compat_trust._DESKTOP_SESSION_TIMEOUT_SECS
        assert seen["stdin"] is subprocess.DEVNULL
        assert not seen.get("shell")
        env = seen["env"]
        assert isinstance(env, dict) and env["PATH"] == expected_path

    def test_it_gets_the_session_bus_a_unit_can_lack(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Its environment is `service.common.systemctl_user_env`, the one resolver
        # for a `systemctl --user` spawn, so a gateway whose unit names no runtime
        # directory or bus address still reaches the user manager.
        (tmp_path / "bus").touch()
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        monkeypatch.setattr(service_common, "session_runtime_dir", lambda: str(tmp_path))
        seen: dict[str, object] = {}

        def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            seen.update(kwargs)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(platform_compat_trust, "trusted_system_bin_quiet", lambda name: "/usr/bin/" + name)
        monkeypatch.setattr(subprocess, "run", run)
        with patch.object(platform_compat, "IS_WINDOWS", False):
            assert platform_compat_trust._user_manager_environment() == []
            expected_path = platform_compat.trusted_system_path()
        env = seen["env"]
        assert isinstance(env, dict)
        assert env["XDG_RUNTIME_DIR"] == str(tmp_path)
        assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={tmp_path / 'bus'}"
        assert env["PATH"] == expected_path

    @pytest.mark.parametrize("failure", ["no-systemctl", "timeout", "refused", "non-zero-exit"])
    def test_no_answer_is_no_session(self, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
        def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(argv, 2.0)
            if failure == "refused":
                raise OSError("blocked by policy")
            return subprocess.CompletedProcess(argv, 1, stdout="DISPLAY=:0\n", stderr="no bus")

        found = None if failure == "no-systemctl" else "/usr/bin/systemctl"
        monkeypatch.setattr(platform_compat_trust, "trusted_system_bin_quiet", lambda name: found)
        monkeypatch.setattr(subprocess, "run", run)
        assert platform_compat_trust._user_manager_environment() is None
