"""How an install run is reported, bounded and offered.

Stage transitions through ``on_stage``, the installer's whole process tree ending on a
timeout or a scope stop, npm and Node resolved once and logged, and the standalone
installer command ``detect()`` offers when the CLI cannot install itself. Detection and
install sequencing live in ``test_browser_cli_install.py``; the shared autouse pins and
fakes in ``browser_cli_install_helpers.py``.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest
from browser_cli_install_helpers import _clear_node_bin_dir_caches  # noqa: F401
from browser_cli_install_helpers import _default_no_os_deps  # noqa: F401
from browser_cli_install_helpers import isolated_browser_cache_fixture  # noqa: F401
from browser_cli_install_helpers import _wire

from kiro_crew.browser_cli import install as mod
from kiro_crew.testing.wait import wait_until


def test_the_standalone_command_writes_no_fixed_name_into_the_working_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator pastes this into whatever shell is open, so the download
    destination is a directory the command does not own. A fixed
    `playwright-cli.sh` in the working directory would be truncated -- their own
    copy, or an unrelated file that merely shares the name."""
    monkeypatch.setattr(mod.os, "name", "posix")
    posix = mod._standalone_install_command()
    assert "mktemp -d" in posix
    assert "-fsSLO" not in posix, "-O derives the name from the URL, into the cwd"
    assert 'sh "$_pwcli_dir/playwright-cli.sh"' in posix
    assert "d=$(mktemp" not in posix, "must not clobber a common scratch variable"

    monkeypatch.setattr(mod.os, "name", "nt")
    windows = mod._standalone_install_command()
    assert "$env:TEMP" in windows
    assert "NewGuid" in windows
    assert "-OutFile $p" in windows
    assert ".\\playwright-cli.ps1" not in windows


def test_detect_offers_the_os_appropriate_standalone_installer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The panel's Node-blocked state must not end at "Download Node.js", which is
    the one thing the operator it describes often cannot do -- no admin rights, or a
    registry that needs a login. `detect()` therefore carries the standalone
    installer command, and composes it HERE because only the gateway knows which OS
    it runs on: the dashboard may be open on a different machine, and offering two
    commands to choose between puts that guess on the user.

    It is also the only place it can live. A shell command must not enter the i18n
    catalogs -- the pseudolocale accents every Latin character, which would corrupt
    the URL -- and the dashboard's untranslated-literal gate forbids holding it in
    the component.
    """
    # Both `os.name` patches live in their OWN throwaway instance, because the
    # `detect()` call below needs the REAL platform back and `monkeypatch.undo()`
    # on the shared instance is not a way to get there: it reverts every record on
    # that instance, including the two module-level autouse fixtures that patched
    # through it. That would send `_browsers_cache_dir` and `_required_revisions`
    # back to production for the rest of the test, and `detect()` would iterdir the
    # developer's real ~/.cache/ms-playwright -- exactly what
    # `isolated_browser_cache` exists to prevent. Exiting the context restores
    # `os.name` alone.
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(mod.os, "name", "posix")
        posix = mod._standalone_install_command()
        assert "playwright-cli.sh" in posix
        assert "powershell" not in posix
        # Download-then-run rather than a pipe into a shell: a machine locked down
        # enough to need this usually forbids piping the network into `sh`.
        assert "| sh" not in posix
        assert "curl -fsSL" in posix

        patched.setattr(mod.os, "name", "nt")
        windows = mod._standalone_install_command()
        assert "playwright-cli.ps1" in windows
        assert "playwright-cli.sh" not in windows

    # `detect()` is exercised under the REAL platform: with `os.name` patched to
    # "nt", pathlib refuses to build a WindowsPath on Linux and the call dies
    # before the payload exists. The Windows branch above is the helper's job.
    # `_wire` for the same reason every other `detect()` call site here uses it --
    # unwired, `detect()` resolves the real `cli_path()` and spawns the host's own
    # `playwright-cli --version` and `node --version` with the repo root as cwd.
    _wire(monkeypatch, {})
    assert mod.detect()["standalone_install"] == mod._standalone_install_command()


class TestInstallStages:
    """``install`` reports real stage transitions through ``on_stage``."""

    def test_install_reports_each_stage_before_the_step_it_names(self, monkeypatch):
        events: list[str] = []
        calls = _wire(monkeypatch, {"npm": "/n/npm", "playwright-cli": "/n/pw"})
        original = mod._run

        def recording_run(argv, timeout):
            events.append("spawn:" + ("npm" if argv[0] == "/n/npm" else argv[1]))
            return original(argv, timeout)

        monkeypatch.setattr(mod, "_run", recording_run)

        result = mod.install(on_stage=events.append)

        assert result["ok"] is True
        assert calls
        assert events == [
            "installing_cli",
            "spawn:npm",
            "downloading_browser",
            "spawn:install-browser",
            "installing_skills",
            "spawn:install",
            "finishing",
        ]

    def test_a_failing_stage_callback_does_not_stop_the_install(self, monkeypatch):
        _wire(monkeypatch, {"npm": "/n/npm", "playwright-cli": "/n/pw"})

        def boom(stage: str) -> None:
            raise RuntimeError("loop closed")

        assert mod.install(on_stage=boom)["ok"] is True

    def test_a_failed_download_reports_no_later_stage(self, monkeypatch):
        events: list[str] = []
        _wire(
            monkeypatch,
            {"npm": "/n/npm", "playwright-cli": "/n/pw"},
            {"/n/pw": (1, "", "network unreachable")},
        )

        assert mod.install(on_stage=events.append)["ok"] is False
        assert events == ["installing_cli", "downloading_browser"]


_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")


def _exited(pid: int) -> bool:
    """Whether *pid* has exited right now (a zombie awaiting its reaper counts)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    with contextlib.suppress(OSError):
        stat_line = Path(f"/proc/{pid}/stat").read_text()
        if stat_line.rsplit(")", 1)[1].split()[0] == "Z":
            return True
    return False


def _pid_gone(pid: int, deadline_s: float = 5.0) -> bool:
    """Whether *pid* exits within *deadline_s*; False, not a raise, so a teardown can call it."""
    try:
        wait_until(lambda: _exited(pid), timeout=deadline_s)
    except AssertionError:
        return False
    return True


def _tree_script(pid_file: Path) -> list[str]:
    """A child that starts a grandchild and records its pid and start identity.

    The record is written to a temporary file and renamed into place, so a
    reader that sees ``pid_file`` exist always sees the whole record.
    """
    import sys

    tmp_file = pid_file.with_name(pid_file.name + ".tmp")
    code = (
        "import os, subprocess, sys, time\n"
        "from kiro_crew import platform_compat\n"
        "g = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(60)'],\n"
        f"    cwd={str(pid_file.parent)!r},\n"
        ")\n"
        "start_id = platform_compat.get_process_start_id(g.pid)\n"
        f"with open({str(tmp_file)!r}, 'w') as f:\n"
        "    f.write(f'{g.pid}:{start_id or \"\"}')\n"
        f"os.replace({str(tmp_file)!r}, {str(pid_file)!r})\n"
        "time.sleep(60)\n"
    )
    return [sys.executable, "-c", code]


def _read_pid_identity(pid_file: Path) -> tuple[int, str | None]:
    """Read the child-captured grandchild pid and stable start identity."""
    pid_text, start_id = pid_file.read_text().split(":", 1)
    return int(pid_text), start_id or None


class TestInstallerChildrenAreTerminable:
    """A timeout or a gateway shutdown ends the installer's whole process tree."""

    @_POSIX_ONLY
    def test_a_timeout_kills_the_grandchild_too(self, tmp_path):
        import signal

        pid_file = tmp_path / "grandchild.pid"
        pid: int | None = None
        start_id: str | None = None
        try:
            # flake-ok: pre-existing real 3 s installer timeout under test, moved verbatim; tracked for a de-flake pass
            rc, _out, err = mod._run(_tree_script(pid_file), 3.0, cwd=str(tmp_path))
            assert rc == 124
            assert "timed out" in err
            assert pid_file.exists()
            pid, start_id = _read_pid_identity(pid_file)
            assert _pid_gone(pid)
        finally:
            if (
                pid is not None
                and start_id is not None
                and mod.platform_compat.get_process_start_id(pid) == start_id
            ):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
                _pid_gone(pid)

    @_POSIX_ONLY
    def test_terminating_the_scope_kills_a_running_child_tree(self, tmp_path):
        import signal
        import threading
        import time

        pid_file = tmp_path / "grandchild.pid"
        pid: int | None = None
        start_id: str | None = None
        scope = mod.InstallScope()
        outcome: list[tuple[int, str, str]] = []
        worker = threading.Thread(
            target=lambda: outcome.append(
                mod.run_in_scope(
                    scope,
                    mod._run,
                    _tree_script(pid_file),
                    60.0,
                    cwd=str(tmp_path),
                )
            )
        )
        started = time.monotonic()
        try:
            worker.start()
            wait_until(pid_file.exists, timeout=10, describe=lambda: "child never started")
            pid, start_id = _read_pid_identity(pid_file)

            assert scope.terminate() == 1
            worker.join(10)
            assert not worker.is_alive()
            assert outcome and outcome[0][0] == mod.INTERRUPTED_RC
            # flake-ok: pre-existing 30 s lost-run ceiling on the scope stop, moved verbatim; tracked for a de-flake pass
            assert time.monotonic() - started < 30
            assert _pid_gone(pid)
        finally:
            scope.terminate()
            if worker.ident is not None:
                worker.join(10)
            if (
                pid is not None
                and start_id is not None
                and mod.platform_compat.get_process_start_id(pid) == start_id
            ):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
                _pid_gone(pid)

    def test_a_terminated_scope_spawns_nothing(self, monkeypatch):
        spawned: list[object] = []
        monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: spawned.append(a))
        scope = mod.InstallScope()
        scope.terminate()

        rc, _out, err = mod.run_in_scope(scope, mod._run, ["npm", "install"], 10.0)

        assert rc == mod.INTERRUPTED_RC
        assert "interrupted" in err
        assert spawned == []

    def test_the_scope_is_thread_local_and_restored(self):
        scope = mod.InstallScope()
        assert mod._current_scope() is None
        assert mod.run_in_scope(scope, mod._current_scope) is scope
        assert mod._current_scope() is None

    def test_a_child_outside_any_scope_is_not_adopted(self, tmp_path):
        import sys

        rc, out, _err = mod._run([sys.executable, "-c", "print('ok')"], 20.0)
        assert (rc, out.strip()) == (0, "ok")


def test_install_resolves_npm_and_node_once_and_logs_what_it_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The bootstrap's PATH-derived npm/Node are resolved once and recorded.

    Reporting only: the logged writability never changes whether the install runs.
    """
    crew = tmp_path / "crew"
    monkeypatch.setattr(mod, "config_dir", lambda: crew)
    calls = _wire(monkeypatch, {"npm": "/n/npm", "playwright-cli": "/n/playwright-cli"})
    lookups: list[str] = []
    resolve = mod.find_node_tool
    monkeypatch.setattr(
        mod, "find_node_tool", lambda name, base_path=None: (lookups.append(name), resolve(name))[1]
    )
    monkeypatch.setattr(
        mod,
        "bootstrap_tool_provenance",
        lambda path: {
            "path": path,
            "real_path": path,
            "writable_at": "/n",
            "own_toolchain": False,
            "shared_write": True,
        },
    )

    with caplog.at_level("INFO", logger=mod.__name__):
        result = mod.install()

    assert result["ok"] is True
    assert sorted(lookups) == ["node", "npm"]
    assert calls[0][0] == "/n/npm"
    logged = caplog.text
    assert "browser install bootstrap: npm=/n/npm" in logged
    assert "browser install bootstrap: node=/n/node" in logged
    assert "user-writable at=/n, own toolchain=False, group/other-writable=True" in logged
