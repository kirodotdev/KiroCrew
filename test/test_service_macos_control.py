"""Tests for the macOS service's control paths under launchd.

Install, uninstall, stop, restart, status and is_active, the launcher self-heal,
and the rule that only the real instance repairs the shared launcher.
"""

from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from service_helpers import (  # noqa: F401 -- fixtures are found by name
    _clear_sudo_user,
    _no_host_env_file,
    _no_host_unit_files,
    _restart_settle_without_waiting,
)

from kiro_crew.service.common import LAUNCHD_LABEL


class TestMacOSControlPaths:
    """Cover uninstall, stop, status, is_active for macOS / launchd."""

    def test_install_unloads_existing_plist_before_writing(self, tmp_path, monkeypatch):
        """Re-running install on a host that already has the plist loaded
        should boot it out first, then write+bootstrap. Otherwise the new
        plist wouldn't take effect."""
        from kiro_crew.service import macos as svc_macos

        plist_dir = tmp_path / "LaunchAgents"
        plist_path = plist_dir / f"{LAUNCHD_LABEL}.plist"
        log_dir = tmp_path / "Logs"
        plist_dir.mkdir(parents=True)
        # Pre-create the plist so install hits the bootout-first branch.
        plist_path.write_text("<plist/>")
        monkeypatch.setattr(svc_macos, "PLIST_DIR", plist_dir)
        monkeypatch.setattr(svc_macos, "PLIST_PATH", plist_path)
        monkeypatch.setattr(svc_macos, "LOG_DIR", log_dir)
        monkeypatch.setattr(svc_macos, "STDOUT_LOG", log_dir / "gateway.log")
        monkeypatch.setattr(svc_macos, "STDERR_LOG", log_dir / "gateway.err")
        # Every stubbed `print` answers "still loaded"; skip the settle wait.
        monkeypatch.setattr(svc_macos, "_BOOTOUT_SETTLE_SECS", 0.0)

        ok = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch(
                "kiro_crew.service.common.shutil.which",
                return_value="/opt/homebrew/bin/kirocrew",
            ),
            patch("kiro_crew.service.macos.subprocess.run", return_value=ok) as run,
        ):
            svc_macos.install()
        called = [c.args[0] for c in run.call_args_list]
        # The bootout must come BEFORE the bootstrap for the new plist to take effect.
        unload_idx = next(i for i, c in enumerate(called) if c[:2] == ["launchctl", "bootout"])
        load_idx = next(i for i, c in enumerate(called) if c[:2] == ["launchctl", "bootstrap"])
        assert unload_idx < load_idx

    def test_uninstall_unloads_and_removes_plist(self, tmp_path, monkeypatch):
        from kiro_crew.service import macos as svc_macos

        plist_dir = tmp_path / "LaunchAgents"
        plist_path = plist_dir / f"{LAUNCHD_LABEL}.plist"
        plist_dir.mkdir(parents=True)
        plist_path.write_text("<plist/>")
        data_home = tmp_path / "crew-home"
        data_home.mkdir()
        sentinel = data_home / "memory.db"
        sentinel.write_text("user data")
        monkeypatch.setenv("KIROCREW_HOME", str(data_home))
        monkeypatch.setattr(svc_macos, "PLIST_PATH", plist_path)

        ok = MagicMock(returncode=0, stdout="", stderr="")
        with patch("kiro_crew.service.macos.subprocess.run", return_value=ok) as run:
            svc_macos.uninstall()
        assert not plist_path.exists()
        called = [c.args[0] for c in run.call_args_list]
        uid = getattr(os, "getuid", lambda: -1)()
        assert ["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"] in called
        assert sentinel.read_text() == "user data"

    def test_uninstall_idempotent_when_plist_missing(self, tmp_path, monkeypatch):
        from kiro_crew.service import macos as svc_macos

        monkeypatch.setattr(svc_macos, "PLIST_PATH", tmp_path / "missing.plist")
        with patch("kiro_crew.service.macos.subprocess.run") as run:
            svc_macos.uninstall()
        run.assert_not_called()

    def test_is_active_returns_false_when_launchctl_errors(self):
        from kiro_crew.service import macos as svc_macos

        not_loaded = MagicMock(returncode=1, stdout="", stderr="not loaded")
        with patch("kiro_crew.service.macos.subprocess.run", return_value=not_loaded):
            assert svc_macos.is_active() is False

    def test_is_active_returns_true_with_pid_in_output(self):
        from kiro_crew.service import macos as svc_macos

        loaded = MagicMock(
            returncode=0,
            stdout='{\n\t"PID" = 1234;\n\t"Label" = "dev.kirocrew.gateway";\n}\n',
            stderr="",
        )
        with patch("kiro_crew.service.macos.subprocess.run", return_value=loaded):
            assert svc_macos.is_active() is True

    def test_is_active_returns_true_when_loaded_without_pid_line(self):
        """`launchctl list <label>` succeeds even if the agent is loaded
        but not running. We treat that as active so callers don't trip
        over a transient state."""
        from kiro_crew.service import macos as svc_macos

        loaded_no_pid = MagicMock(
            returncode=0,
            stdout='{\n\t"Label" = "dev.kirocrew.gateway";\n}\n',
            stderr="",
        )
        with patch("kiro_crew.service.macos.subprocess.run", return_value=loaded_no_pid):
            assert svc_macos.is_active() is True

    def test_stop_unloads_plist_when_present(self, tmp_path, monkeypatch):
        # ``launchctl stop`` would just send SIGTERM and KeepAlive would
        # restart the agent immediately. ``bootout`` removes the job from its
        # domain, so there is nothing to respawn, while leaving the plist
        # enabled for the next login.
        from kiro_crew.service import macos as svc_macos

        plist_path = tmp_path / "agent.plist"
        plist_path.write_text("<plist/>")
        monkeypatch.setattr(svc_macos, "PLIST_PATH", plist_path)
        ok = MagicMock(returncode=0, stdout="", stderr="")
        with patch("kiro_crew.service.macos.subprocess.run", return_value=ok) as run:
            assert svc_macos.stop() is True
        called = [c.args[0] for c in run.call_args_list]
        uid = getattr(os, "getuid", lambda: -1)()
        assert ["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"] in called
        # Crucially, we should NOT have called `launchctl stop`.
        assert not any(c[:2] == ["launchctl", "stop"] for c in called)

    def test_stop_no_op_when_plist_absent(self, tmp_path, monkeypatch):
        from kiro_crew.service import macos as svc_macos

        monkeypatch.setattr(svc_macos, "PLIST_PATH", tmp_path / "missing.plist")
        with patch("kiro_crew.service.macos.subprocess.run") as run:
            svc_macos.stop()
        run.assert_not_called()

    def test_restart_kickstarts_the_service_target(self, tmp_path, monkeypatch):
        # ``launchctl restart`` is deprecated and behaves like ``stop`` under
        # KeepAlive (SIGTERM, immediate respawn — no plist re-read). The former
        # implementation used a transient unload+load, which cannot be issued
        # from INSIDE the gateway: the unload SIGTERMs the caller, so the load
        # never runs and the agent stays down. ``kickstart -k`` is performed by
        # launchd itself, so it survives the caller's death — the property Dev
        # Fleet's Restart control depends on.
        from kiro_crew.service import macos as svc_macos

        plist_path = tmp_path / "agent.plist"
        plist_path.write_text("<plist/>")
        monkeypatch.setattr(svc_macos, "PLIST_PATH", plist_path)
        ok = MagicMock(returncode=0, stdout="", stderr="")
        with patch("kiro_crew.service.macos.subprocess.run", return_value=ok) as run:
            assert svc_macos.restart() is True
        called = [c.args[0] for c in run.call_args_list]
        assert len(called) == 1, "restart must be a single launchd operation"
        argv = called[0]
        assert argv[:3] == ["launchctl", "kickstart", "-k"]
        # Addressed as gui/<uid>/<label>, what the modern verbs require.
        assert argv[3].startswith("gui/")
        assert argv[3].endswith(f"/{LAUNCHD_LABEL}")
        # No unload anywhere: an unload would kill the caller mid-restart.
        assert not any(c[:2] == ["launchctl", "unload"] for c in called)

    def test_restart_is_false_when_launchd_rejects_it(self, tmp_path, monkeypatch):
        """A rejected kickstart must not be reported as a restart."""
        from kiro_crew.service import macos as svc_macos

        plist_path = tmp_path / "agent.plist"
        plist_path.write_text("<plist/>")
        monkeypatch.setattr(svc_macos, "PLIST_PATH", plist_path)
        bad = MagicMock(returncode=1, stdout="", stderr="no such service")
        with patch("kiro_crew.service.macos.subprocess.run", return_value=bad):
            assert svc_macos.restart() is False

    def test_restart_no_op_when_plist_absent(self, tmp_path, monkeypatch):
        # Restart on an uninstalled service is a no-op rather than an
        # error. The CLI controller decides whether to fall back to the
        # foreground-gateway path; this layer just refuses to invent a
        # plist that doesn't exist.
        from kiro_crew.service import macos as svc_macos

        monkeypatch.setattr(svc_macos, "PLIST_PATH", tmp_path / "missing.plist")
        with patch("kiro_crew.service.macos.subprocess.run") as run:
            svc_macos.restart()
        run.assert_not_called()

    def test_status_returns_launchctl_output_when_loaded(self):
        from kiro_crew.service import macos as svc_macos

        loaded = MagicMock(
            returncode=0,
            stdout='{\n\t"PID" = 1234;\n}\n',
            stderr="",
        )
        with patch("kiro_crew.service.macos.subprocess.run", return_value=loaded):
            out = svc_macos.status()
        assert "PID" in out

    def test_status_returns_friendly_message_when_not_loaded(self):
        from kiro_crew.service import macos as svc_macos

        not_loaded = MagicMock(returncode=1, stdout="", stderr="no entry")
        with patch("kiro_crew.service.macos.subprocess.run", return_value=not_loaded):
            out = svc_macos.status()
        assert "not loaded" in out

    def test_kirocrew_bin_falls_back_to_argv0(self, monkeypatch):
        """If `kirocrew` is not on PATH, kirocrew_bin should resolve
        sys.argv[0] rather than crash."""
        from kiro_crew.service import common as svc_common

        monkeypatch.setattr(sys, "argv", ["/some/path/kirocrew"])
        with patch("kiro_crew.service.common.shutil.which", return_value=None):
            assert "kirocrew" in svc_common.kirocrew_bin()


class TestEnsureLiveProgram:
    """Self-heal for a deleted launchd launcher.

    The repair has to be surgical: re-running `service install` also restores the
    launcher, but it rewrites the plist and so throws away operator-added
    EnvironmentVariables. These lock in that only the launcher half moves, and
    that the helper stays quiet where there is no agent to repair.
    """

    def _agent(self, monkeypatch, tmp_path, *, indirected=True, fmt=None):
        from kiro_crew.service import macos as svc_macos

        launcher = tmp_path / "live-gateway"
        plist = tmp_path / "dev.kirocrew.gateway.plist"
        target = str(launcher) if indirected else "/usr/local/bin/kirocrew"
        # A REAL plist, in either wire format: launchd accepts XML and binary
        # alike, and the reconcile must not care which one it is handed.
        plist.write_bytes(
            plistlib.dumps(
                {
                    "Label": "dev.kirocrew.gateway",
                    "EnvironmentVariables": {"KIROCREW_PORT": "5477"},
                    "ProgramArguments": [target, "gateway", "--no-open"],
                },
                fmt=fmt or plistlib.FMT_XML,
            )
        )
        monkeypatch.setattr(svc_macos, "LIVE_PROGRAM", launcher)
        monkeypatch.setattr(svc_macos, "PLIST_PATH", plist)
        return svc_macos, launcher, plist

    def test_reads_a_binary_plist_rather_than_assuming_utf8_text(self, monkeypatch, tmp_path):
        """launchd plists are legitimately binary or UTF-16.

        This runs during gateway startup, so decoding one as UTF-8 text would
        raise UnicodeDecodeError and take the whole gateway down over a check
        that only decides whether to rewrite a launcher.
        """
        svc, launcher, _plist = self._agent(monkeypatch, tmp_path, fmt=plistlib.FMT_BINARY)
        monkeypatch.setenv("KIROCREW_SERVICE_BIN", str(self._exe(tmp_path / "bin" / "kirocrew")))

        assert svc.ensure_live_program() is True
        assert launcher.exists()

    def test_writes_nothing_when_the_plist_is_not_a_plist(self, monkeypatch, tmp_path):
        """Garbage at the plist path must be declined, not raised through."""
        svc, launcher, plist = self._agent(monkeypatch, tmp_path)
        plist.write_bytes(b"\x00\x01 not a plist at all")

        assert svc.ensure_live_program() is False
        assert not launcher.exists()

    def test_a_malformed_xml_plist_cannot_take_the_gateway_down(self, monkeypatch, tmp_path):
        """An unescaped `&` in a hand-added value is the ordinary way to get one.

        plistlib surfaces that as xml.parsers.expat.ExpatError, whose base is
        Exception — and this runs at gateway startup, so anything escaping here is
        a crash loop under launchd KeepAlive rather than a skipped check.
        """
        svc, launcher, plist = self._agent(monkeypatch, tmp_path)
        plist.write_bytes(
            b'<?xml version="1.0"?><plist version="1.0"><dict>'
            b"<key>Label</key><string>a & b</string></dict></plist>"
        )

        assert svc.ensure_live_program() is False
        assert not launcher.exists()

    def test_a_plist_whose_root_is_an_array_is_declined(self, monkeypatch, tmp_path):
        """A plist root may legally be an array, which has no .get()."""
        svc, launcher, plist = self._agent(monkeypatch, tmp_path)
        plist.write_bytes(plistlib.dumps(["not", "a", "dict"]))

        assert svc.ensure_live_program() is False
        assert not launcher.exists()

    def test_a_non_list_program_arguments_is_declined(self, monkeypatch, tmp_path):
        """ProgramArguments carries no type guarantee either."""
        svc, launcher, plist = self._agent(monkeypatch, tmp_path)
        plist.write_bytes(plistlib.dumps({"ProgramArguments": "just a string"}))

        assert svc.ensure_live_program() is False
        assert not launcher.exists()

    @staticmethod
    def _exe(path: Path) -> Path:
        """An executable stub — the repair now refuses a non-executable target."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)  # fmt: skip
        return path

    def test_restores_a_deleted_launcher_and_leaves_the_plist_untouched(
        self, monkeypatch, tmp_path
    ):
        svc, launcher, plist = self._agent(monkeypatch, tmp_path)
        pinned = self._exe(tmp_path / "opt" / "kirocrew")
        monkeypatch.setenv("KIROCREW_SERVICE_BIN", str(pinned))
        # Read the target back off the resolver rather than restating a literal:
        # kirocrew_bin() absolutizes, so the path SHAPE differs per platform
        # (Windows resolves a rooted POSIX path to a drive-qualified one).
        resolved = svc.kirocrew_bin()
        before = plist.read_bytes()

        assert svc.ensure_live_program() is True

        assert f"exec '{resolved}' \"$@\"" in launcher.read_text()
        assert os.access(launcher, os.X_OK), "launchd must be able to exec it"
        # The whole reason this exists rather than deferring to `service install`.
        assert plist.read_bytes() == before
        assert b"5477" in plist.read_bytes()

    @pytest.mark.skipif(
        os.name != "posix",
        reason="os.access(X_OK) has no permission meaning for a .py file on "
        "Windows, and this reconcile only ever runs on darwin",
    )
    def test_refuses_to_write_a_launcher_that_execs_a_non_executable(self, monkeypatch, tmp_path):
        """`python -m kiro_crew` with no console script resolves to `__main__.py`.

        Writing that would leave launchd unable to spawn the agent AND suppress
        every later repair, since the launcher would then exist — the self-heal
        would cement the broken state. Refusing keeps the next run able to fix it.
        """
        svc, launcher, _plist = self._agent(monkeypatch, tmp_path)
        not_exec = tmp_path / "pkg" / "__main__.py"
        not_exec.parent.mkdir()
        not_exec.write_text("# a module, not a program\n")
        monkeypatch.setenv("KIROCREW_SERVICE_BIN", str(not_exec))

        with pytest.raises(OSError, match="not an executable file"):
            svc.ensure_live_program()

        assert not launcher.exists(), "a later repair must still be possible"

    def test_refuses_rather_than_falling_back_to_path(self, monkeypatch, tmp_path):
        """No sibling script and no override: refuse, never resolve through PATH.

        PATH cannot answer "which install is running", so an unrelated or older
        `kirocrew` ahead of this one would be persisted into the agent — the
        mismatch this repair exists to end, recreated by the repair.
        """
        svc, launcher, _plist = self._agent(monkeypatch, tmp_path)
        monkeypatch.delenv("KIROCREW_SERVICE_BIN", raising=False)
        stray = self._exe(tmp_path / "stray" / "kirocrew")
        from kiro_crew.service import common as svc_common

        monkeypatch.setattr(svc_common.shutil, "which", lambda _n: str(stray))
        # An interpreter directory with NO kirocrew beside it.
        bare = tmp_path / "bare"
        bare.mkdir()
        monkeypatch.setattr(svc.sys, "executable", str(bare / "python"))

        with pytest.raises(OSError, match="no kirocrew console script"):
            svc.ensure_live_program()

        assert not launcher.exists()

    def test_targets_the_repairing_install_not_whatever_path_finds(self, monkeypatch, tmp_path):
        """A stray `kirocrew` earlier on PATH must not be baked into the launcher.

        Restoring the agent onto some OTHER install is a quieter version of the
        mismatch this repair exists to end, so the target comes from the running
        interpreter rather than from PATH resolution.
        """
        svc, launcher, _plist = self._agent(monkeypatch, tmp_path)
        monkeypatch.delenv("KIROCREW_SERVICE_BIN", raising=False)
        stray = self._exe(tmp_path / "stray" / "kirocrew")
        # kirocrew_bin() lives in service.common and resolves through ITS shutil.
        from kiro_crew.service import common as svc_common

        monkeypatch.setattr(svc_common.shutil, "which", lambda _n: str(stray))
        # The console script that ships beside the running interpreter.
        mine = self._exe(tmp_path / "mine" / ("kirocrew.exe" if os.name == "nt" else "kirocrew"))
        monkeypatch.setattr(svc.sys, "executable", str(mine.parent / "python"))

        assert svc.ensure_live_program() is True

        script = launcher.read_text()
        assert str(mine) in script
        assert str(stray) not in script

    def test_an_explicit_service_bin_override_still_wins(self, monkeypatch, tmp_path):
        """Pinning the service Program is operator intent, not PATH shadowing."""
        svc, launcher, _plist = self._agent(monkeypatch, tmp_path)
        pinned = self._exe(tmp_path / "pinned" / "kirocrew")
        monkeypatch.setenv("KIROCREW_SERVICE_BIN", str(pinned))
        resolved = svc.kirocrew_bin()

        assert svc.ensure_live_program() is True

        assert f"exec '{resolved}' \"$@\"" in launcher.read_text()

    def test_is_a_noop_when_the_launcher_is_already_there(self, monkeypatch, tmp_path):
        """An existing launcher may carry a Dev Fleet cutover — never clobber it."""
        svc, launcher, _plist = self._agent(monkeypatch, tmp_path)
        launcher.write_text(
            "#!/bin/sh\ncd '/wt/live' || exit 1\nexec '/wt/live/.venv/bin/kirocrew' \"$@\"\n"
        )

        assert svc.ensure_live_program() is False
        assert "/wt/live" in launcher.read_text()

    def test_writes_nothing_when_no_agent_is_installed(self, monkeypatch, tmp_path):
        """No plist means no job whose launcher this would be — writing it is litter."""
        svc, launcher, plist = self._agent(monkeypatch, tmp_path)
        plist.unlink()

        assert svc.ensure_live_program() is False
        assert not launcher.exists()

    def test_writes_nothing_when_the_agent_bypasses_the_launcher(self, monkeypatch, tmp_path):
        """An older agent execs the binary directly; a launcher it never runs is litter."""
        svc, launcher, _plist = self._agent(monkeypatch, tmp_path, indirected=False)

        assert svc.ensure_live_program() is False
        assert not launcher.exists()


class TestLauncherReconcileIsProductionOnly:
    """Only the real instance may repair the shared launchd launcher.

    LIVE_PROGRAM is a per-user path that KIROCREW_HOME does not scope, so a dev,
    pod, or worktree gateway "repairing" it would repoint the user's REAL agent
    at its own venv — the serving-vs-managed mismatch the reconcile exists to
    prevent, authored by the reconcile itself.
    """

    def test_the_default_home_on_darwin_reconciles(self, monkeypatch):
        from kiro_crew import cli_server

        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(cli_server.sys, "platform", "darwin")

        assert cli_server._should_reconcile_launchd_launcher() is True

    def test_an_isolated_home_does_not_reconcile(self, monkeypatch, tmp_path):
        from kiro_crew import cli_server

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / ".kirocrew-dev"))
        monkeypatch.setattr(cli_server.sys, "platform", "darwin")

        assert cli_server._should_reconcile_launchd_launcher() is False

    def test_non_darwin_never_reconciles(self, monkeypatch):
        from kiro_crew import cli_server

        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(cli_server.sys, "platform", "linux")

        assert cli_server._should_reconcile_launchd_launcher() is False

    def test_the_desktop_bundle_never_reconciles(self, monkeypatch, tmp_path):
        """The packaged app must not own this artifact.

        launchd would run the bundled interpreter WITHOUT the environment the app
        supplies it — notably PYTHONPYCACHEPREFIX — so bytecode would land inside
        the signed bundle and invalidate its signature. The launchd agent belongs
        to a `service install`, not to an app that manages its own backend.
        """
        from kiro_crew import cli_server, platform_compat

        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(cli_server.sys, "platform", "darwin")
        # Drive the real predicate with a bundle-shaped interpreter path rather
        # than stubbing it, so this test and the packaging layout cannot agree on
        # a shape the build never ships.
        bundled_python = (
            tmp_path
            / platform_compat.BUNDLED_BACKEND_DIST_DIRNAME
            / "kirocrew-backend"
            / "bin"
            / "python3.12"
        )
        monkeypatch.setattr(cli_server.sys, "executable", str(bundled_python))

        assert cli_server._should_reconcile_launchd_launcher() is False
