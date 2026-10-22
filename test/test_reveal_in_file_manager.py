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

import os
from unittest.mock import patch

import pytest

from kiro_crew import platform_compat


class TestRevealInFileManager:
    @pytest.fixture(autouse=True)
    def _a_desktop_session(self, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
        # The Linux arms spawn only into a desktop session (see
        # `desktop_launcher_available`) and a CI runner has none, so every test
        # starts with an X11 display named; a test about its absence removes it
        # through its own `monkeypatch`, which unwinds before this one does.
        _floor_monkeypatch.setenv("DISPLAY", ":0")
        _floor_monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

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
