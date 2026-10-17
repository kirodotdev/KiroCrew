"""Tests for the environment the installed service runs with.

The resolved kirocrew binary and its ``KIROCREW_SERVICE_BIN`` override, and the
locale, binary pin and quoting the unit and plist carry into the service.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from service_helpers import (  # noqa: F401 -- fixtures are found by name
    _clear_sudo_user,
    _no_host_env_file,
    _no_host_unit_files,
    _restart_settle_without_waiting,
)

from kiro_crew.service import common
from kiro_crew.service.common import kirocrew_bin, service_environment


class TestKirocrewBinOverride:
    @pytest.mark.parametrize("source", ["which", "argv"])
    def test_managed_tree_launch_path_is_persisted_through_the_stable_link(
        self, source, monkeypatch, tmp_path
    ):
        from kiro_crew.platform import tree_liveness, wheel_engine

        tree = tmp_path / "crew-venv-1.0"
        binary = tree / "bin" / "kirocrew"
        binary.parent.mkdir(parents=True)
        binary.touch()
        (tree / tree_liveness.TREE_MARKER).write_text("layout\n", encoding="utf-8")
        stable = str(tmp_path / "crew-venv-current" / "bin" / "kirocrew")
        rewrite = MagicMock(return_value=stable)
        monkeypatch.setattr(wheel_engine, "stable_launch_path", rewrite)
        monkeypatch.delenv("KIROCREW_SERVICE_BIN", raising=False)
        monkeypatch.setattr(
            common.shutil, "which", lambda _name: str(binary) if source == "which" else None
        )
        monkeypatch.setattr(common.sys, "argv", [str(binary)])

        assert kirocrew_bin() == stable
        rewrite.assert_called_once_with(str(binary.resolve()))

    def test_service_bin_override_wins_over_which(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_SERVICE_BIN", "/opt/wrapper/kirocrew")
        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/usr/local/bin/kirocrew",
        ):
            assert kirocrew_bin() == "/opt/wrapper/kirocrew"

    def test_falls_back_to_which_when_override_unset(self, monkeypatch):
        monkeypatch.delenv("KIROCREW_SERVICE_BIN", raising=False)
        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/usr/local/bin/kirocrew",
        ):
            assert kirocrew_bin() == "/usr/local/bin/kirocrew"

    def test_blank_override_is_ignored(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_SERVICE_BIN", "   ")
        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/usr/local/bin/kirocrew",
        ):
            assert kirocrew_bin() == "/usr/local/bin/kirocrew"

    def test_relative_override_is_made_absolute(self, monkeypatch):
        # A relative override would produce an invalid ExecStart/ProgramArguments
        # under launchd/systemd (no meaningful cwd), so it must be absolutised.
        import os

        monkeypatch.setenv("KIROCREW_SERVICE_BIN", "./.venv/bin/kirocrew")
        result = kirocrew_bin()
        assert os.path.isabs(result)
        assert result == os.path.abspath("./.venv/bin/kirocrew")


class TestServiceEnvironment:
    # The pinned UTF-8 locale is platform-specific: en_US.UTF-8 on macOS (BSD
    # libc has no C.UTF-8), C.UTF-8 on Linux (always present on glibc/musl).
    EXPECTED_UTF8 = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"

    def test_always_sets_home_path_and_locale(self, monkeypatch):
        monkeypatch.delenv("LANG", raising=False)
        monkeypatch.delenv("LC_ALL", raising=False)
        monkeypatch.delenv("KIROCREW_KIRO_BIN", raising=False)
        env = service_environment("/home/tester")
        assert env["HOME"] == "/home/tester"
        assert "PATH" in env
        # A valid UTF-8 locale is pinned so subprocesses that read non-ASCII
        # files do not crash under the US-ASCII default codec.
        assert env["LANG"] == self.EXPECTED_UTF8
        assert env["LC_ALL"] == self.EXPECTED_UTF8

    def test_locale_is_pinned_ignoring_installer(self, monkeypatch):
        # The installer's locale is NOT trusted. A UTF-8-named installer locale
        # can still be one the target host never generated (SSH-forwarded
        # LC_ALL=zz_ZZ.UTF-8), where setlocale falls back to C; the fixed
        # platform UTF-8 locale is used regardless.
        monkeypatch.setenv("LANG", "en_GB.UTF-8")
        monkeypatch.setenv("LC_ALL", "zz_ZZ.UTF-8")
        env = service_environment("/home/tester")
        assert env["LANG"] == self.EXPECTED_UTF8
        assert env["LC_ALL"] == self.EXPECTED_UTF8

    def test_locale_is_platform_appropriate(self, monkeypatch):
        # C.UTF-8 is invalid on macOS BSD libc; en_US.UTF-8 is invalid-by-
        # absence on minimal Linux. Assert each platform gets its always-valid
        # UTF-8 locale.
        env = service_environment("/home/tester")
        if sys.platform == "darwin":
            assert env["LANG"] == "en_US.UTF-8"
            assert env["LC_ALL"] == "en_US.UTF-8"
        else:
            assert env["LANG"] == "C.UTF-8"
            assert env["LC_ALL"] == "C.UTF-8"

    def test_propagates_port_only_when_set(self, monkeypatch):
        """KIROCREW_PORT reaches the installed service.

        It is the ONLY input DASHBOARD_PORT reads, so a service definition that
        cannot carry it can only ever bind the default 5476 — broken by
        construction on any host where that port is taken, which includes every
        host running Kiro Crew's own instance tunnel (it pins
        local_port == remote_port).
        """
        monkeypatch.delenv("KIROCREW_PORT", raising=False)
        assert "KIROCREW_PORT" not in service_environment("/home/tester")
        monkeypatch.setenv("KIROCREW_PORT", "5477")
        assert service_environment("/home/tester")["KIROCREW_PORT"] == "5477"

    def test_port_reaches_both_rendered_service_definitions(self, monkeypatch, tmp_path):
        """End-to-end, not just present in the dict.

        `service_environment()` feeds the launchd plist's EnvironmentVariables and
        the systemd unit's Environment= lines. Asserting only the dict would pass
        even if a renderer dropped the key on the way out.
        """
        from kiro_crew.service import linux as svc_linux
        from kiro_crew.service import macos as svc_macos

        monkeypatch.setenv("KIROCREW_PORT", "5477")
        monkeypatch.setattr(svc_macos, "LIVE_PROGRAM", tmp_path / "live-gateway")
        with patch(
            "kiro_crew.service.common.shutil.which", return_value="/opt/homebrew/bin/kirocrew"
        ):
            plist = svc_macos.render_plist()
        envs = plist.split("<key>EnvironmentVariables</key>", 1)[1].split("</dict>", 1)[0]
        assert "<key>KIROCREW_PORT</key>" in envs and "<string>5477</string>" in envs
        assert "<key>KIROCREW_SERVICE_MANAGED</key>" in envs

        monkeypatch.setenv("USER", "tester")
        gid = MagicMock(returncode=0, stdout="staff\n", stderr="")
        with (
            patch("kiro_crew.service.common.shutil.which", return_value="/usr/local/bin/kirocrew"),
            patch("kiro_crew.service.linux.subprocess.run", return_value=gid),
        ):
            unit = svc_linux.render_unit()
        assert "KIROCREW_PORT=5477" in unit
        assert "KIROCREW_SERVICE_MANAGED=1" in unit

    def test_propagates_kiro_bin_pin_only_when_set(self, monkeypatch):
        monkeypatch.delenv("KIROCREW_KIRO_BIN", raising=False)
        assert "KIROCREW_KIRO_BIN" not in service_environment("/home/tester")
        monkeypatch.setenv("KIROCREW_KIRO_BIN", "/opt/shim/kiro-cli")
        env = service_environment("/home/tester")
        assert env["KIROCREW_KIRO_BIN"] == "/opt/shim/kiro-cli"

    def test_kiro_bin_pin_is_absolutized(self, monkeypatch):
        # A relative pin is meaningless once the service runs from a different
        # cwd; it must be absolutised like the service-bin override.
        import os

        monkeypatch.setenv("KIROCREW_KIRO_BIN", "./kiro-cli")
        env = service_environment("/home/tester")
        assert os.path.isabs(env["KIROCREW_KIRO_BIN"])
        assert env["KIROCREW_KIRO_BIN"] == os.path.abspath("./kiro-cli")

    def test_non_utf8_installer_locale_not_preserved(self, monkeypatch):
        # LANG=C / POSIX must NOT be preserved: with LC_ALL then explicitly set
        # to it, PEP 538 coercion is suppressed and subprocesses crash on the
        # ASCII codec. The fixed platform UTF-8 locale is used instead.
        for bad in ("C", "POSIX", "en_US"):
            monkeypatch.setenv("LANG", bad)
            monkeypatch.delenv("LC_ALL", raising=False)
            env = service_environment("/home/tester")
            assert env["LANG"] == self.EXPECTED_UTF8, bad
            assert env["LC_ALL"] == self.EXPECTED_UTF8, bad

    def test_plist_includes_locale_and_kiro_bin(self, monkeypatch):
        from kiro_crew.service import macos as svc_macos

        monkeypatch.setenv("KIROCREW_KIRO_BIN", "/opt/shim/kiro-cli")
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/opt/homebrew/bin/kirocrew",
        ):
            plist = svc_macos.render_plist()
        assert "<key>LANG</key>" in plist
        assert "<key>LC_ALL</key>" in plist
        assert "<key>KIROCREW_KIRO_BIN</key>" in plist
        assert "<string>/opt/shim/kiro-cli</string>" in plist
        assert "<key>HOME</key>" in plist
        assert "<key>PATH</key>" in plist

    def test_unit_includes_locale_and_kiro_bin(self, monkeypatch):
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "tester")
        monkeypatch.setenv("KIROCREW_KIRO_BIN", "/opt/shim/kiro-cli")
        with patch(
            "kiro_crew.service.common.shutil.which",
            return_value="/usr/local/bin/kirocrew",
        ):
            unit = svc_linux.render_unit()
        # Environment values are double-quoted (systemd tokenizes on whitespace).
        assert 'Environment="USER=tester"\n' in unit
        assert 'Environment="LANG=' in unit
        assert 'Environment="KIROCREW_KIRO_BIN=/opt/shim/kiro-cli"\n' in unit

    def test_unit_quotes_spaced_program_and_env(self, monkeypatch):
        # A spaced KIROCREW_SERVICE_BIN / KIROCREW_KIRO_BIN must not split the
        # ExecStart exec (203/EXEC) or truncate the env value at the space.
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "tester")
        monkeypatch.setenv("KIROCREW_SERVICE_BIN", "/opt/Kiro Crew/kirocrew")
        monkeypatch.setenv("KIROCREW_KIRO_BIN", "/opt/Kiro Crew/kiro-cli")
        unit = svc_linux.render_unit()
        assert 'ExecStart="/opt/Kiro Crew/kirocrew" gateway' in unit
        assert 'Environment="KIROCREW_KIRO_BIN=/opt/Kiro Crew/kiro-cli"\n' in unit
        # The bare unquoted forms must NOT appear (would break systemd parsing).
        assert "ExecStart=/opt/Kiro Crew/kirocrew gateway" not in unit

    def test_unit_escapes_percent_specifiers(self, monkeypatch):
        # systemd expands %-specifiers (%h=home, %i=instance) in ExecStart /
        # Environment= regardless of quoting; a literal % in a path (e.g. a dir
        # named "100%") must be escaped to %% or the exec targets the wrong path.
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "tester")
        monkeypatch.setenv("KIROCREW_SERVICE_BIN", "/opt/100%/kirocrew")
        unit = svc_linux.render_unit()
        assert 'ExecStart="/opt/100%%/kirocrew" gateway' in unit
        # The single-% form must NOT survive (systemd would treat %/ as a
        # specifier). Guard against a bare "/opt/100%/kirocrew" in ExecStart.
        assert "/opt/100%/kirocrew" not in unit

    def test_sd_quote_escape_order(self):
        from kiro_crew.service.linux import _sd_quote

        # %% before \\ before \" — a value with all three renders correctly.
        assert _sd_quote("a%b") == '"a%%b"'
        assert _sd_quote('x"y') == '"x\\"y"'
        assert _sd_quote("p\\q") == '"p\\\\q"'

    def test_sd_quote_rejects_control_chars(self):
        # A newline (or other C0/DEL) in a value would break out of the quoted
        # systemd token and let the remainder be parsed as fresh unit
        # directives (e.g. User=root injection into the root-owned unit) — must
        # raise, not escape.
        from kiro_crew.service.linux import _sd_quote

        for bad in ("/opt/x\nUser=root", "a\tb", "a\x00b", "a\x7fb", "a\rb"):
            with pytest.raises(ValueError):
                _sd_quote(bad)

    def test_render_unit_rejects_newline_injection(self, monkeypatch):
        # End-to-end: a newline-bearing override must abort render_unit(), not
        # emit an injectable unit file.
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setenv("USER", "tester")
        monkeypatch.setenv("KIROCREW_SERVICE_BIN", "/opt/x/kirocrew\nUser=root\nExecStart=/evil")
        with pytest.raises(ValueError):
            svc_linux.render_unit()

    @pytest.mark.skipif(
        os.name != "posix",
        reason=(
            "creates a real symlink to exercise the launchd live-gateway link; "
            "the code under test is macOS-only and Windows has no unprivileged "
            "symlink creation. Still runs on Linux CI and on the macOS job "
            "(which now includes test_service.py), so coverage is not lost."
        ),
    )
    def test_live_program_quotes_a_spaced_override(self, monkeypatch, tmp_path):
        # The resolved binary now goes into a generated shell script, so a spaced
        # path must be QUOTED there or the launcher would exec the wrong argv.
        # Compared against what kirocrew_bin() actually returned rather than a
        # literal, so the test does not re-encode one platform's path shape.
        from kiro_crew.service import macos as svc_macos

        link = tmp_path / "live-gateway"
        monkeypatch.setattr(svc_macos, "LIVE_PROGRAM", link)
        monkeypatch.setenv("KIROCREW_SERVICE_BIN", "/opt/Kiro Crew/kirocrew")
        resolved = svc_macos.kirocrew_bin()
        assert " " in resolved, "the spaced override must survive resolution"
        svc_macos.write_live_program(svc_macos.render_live_program(resolved))
        script = link.read_text()
        assert f"exec '{resolved}' \"$@\"" in script
        assert os.access(link, os.X_OK), "launchd must be able to exec it"

    def test_live_program_escapes_a_single_quote_in_the_path(self):
        # A path containing ' would otherwise terminate the shell quoting and
        # turn the rest of the path into separate argv words.
        from kiro_crew.service import macos as svc_macos

        script = svc_macos.render_live_program("/opt/it's/kirocrew")
        assert """exec '/opt/it'\\''s/kirocrew' "$@\"""" in script

    @pytest.mark.skipif(
        os.name != "posix",
        reason="real-symlink test for macOS-only code; see the test above",
    )
    def test_write_live_program_is_atomic_and_leaves_no_temp_files(self, monkeypatch, tmp_path):
        """Rewriting must never expose a partial or non-executable launcher.

        The agent can be kickstarted at any moment, so the write goes through a
        temp sibling that is chmod'd before the rename.
        """
        from kiro_crew.service import macos as svc_macos

        link = tmp_path / "live-gateway"
        monkeypatch.setattr(svc_macos, "LIVE_PROGRAM", link)
        svc_macos.write_live_program(svc_macos.render_live_program("/first/kirocrew"))
        svc_macos.write_live_program(svc_macos.render_live_program("/second/kirocrew"))
        assert "'/second/kirocrew'" in link.read_text()
        assert "'/first/kirocrew'" not in link.read_text()
        assert os.access(link, os.X_OK)
        # No temp siblings left behind.
        assert [p.name for p in tmp_path.iterdir()] == ["live-gateway"]
