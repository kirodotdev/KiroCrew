"""The host tools the gateway runs, resolved where it trusts them.

The listening-pid lookup, the owner-uid ``ps`` and the Windows ``taskkill`` use
the pinned tool and ignore one planted on ``PATH``; a tool installed outside the
trusted directories is reported once; ``trusted_git_bin``; the real
``find_python_interpreter`` probe; and the desktop bundle's interpreter
sentinel, pinned against the packaging files. A topic of the
``test_platform_compat`` family; see ``test_platform_compat.py``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew import platform_compat as pc

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_platform_compat family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


class TestFindPythonInterpreterReal:
    def test_real_resolve_returns_none_or_valid_python(self):
        # No mocks: drive the REAL resolution loop. On the Linux build host a
        # versioned python3.1x resolves and runs the version probe, returning
        # its path; in a stripped sandbox nothing resolves and we get None.
        # Tolerant either-way so it can never flake.
        got = pc.find_python_interpreter()
        assert got is None or isinstance(got, str)
        if got is not None:
            assert os.path.exists(got)
            assert "python" in got.lower()

    def test_returns_none_when_version_probe_raises(self, monkeypatch):
        # Force the version-probe subprocess to fail for a resolvable, non-stub
        # path: the except (OSError, ValueError, SubprocessError) -> continue
        # branch fires for every candidate, so the loop exhausts -> None.
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/python3.99")

        def boom(*args, **kwargs):
            raise subprocess.SubprocessError("probe failed")

        monkeypatch.setattr(pc.subprocess, "check_output", boom)
        assert pc.find_python_interpreter() is None

    def test_version_gate_ignores_a_sitecustomize_decoy_on_pythonpath(self, tmp_path, monkeypatch):
        # The selection-side twin of test_origin_probe_ignores_pythonpath: at
        # child startup the ``site`` module imports any ``sitecustomize.py``
        # found on the caller's PYTHONPATH, and that module can monkeypatch
        # ``sys.version_info`` — here forcing this real >= 3.12 interpreter to
        # report 3.4, which would make the version gate reject it and steer
        # selection. The gate runs the probe isolated (-I), so the decoy is
        # never imported and the candidate is judged by its REAL version.
        # This spawns a real child; the probe is a read-only version query
        # that creates nothing, so no cwd pin is needed.
        decoy = tmp_path / "decoy-pythonpath"
        decoy.mkdir()
        (decoy / "sitecustomize.py").write_text(
            "import sys\nsys.version_info = (3, 4, 0, 'final', 0)\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("PYTHONPATH", str(decoy))
        # Every candidate name resolves to this suite's own interpreter — a
        # real, runnable >= 3.12 CPython on every platform CI runs.
        monkeypatch.setattr("shutil.which", lambda name: sys.executable)

        assert pc.find_python_interpreter() == sys.executable


def _listening_port(sock):
    """Bind and listen on an ephemeral loopback port, returning it."""

    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock.getsockname()[1]


def test_listening_pid_lookup_ignores_a_planted_tool_on_path(tmp_path, monkeypatch):
    """A PATH-planted lsof must never answer the port->PID lookup.

    This lookup feeds ``cli_server._gateway_owns_port``, so a shim that names an
    attacker-chosen PID as the port holder subverts an ownership gate rather
    than merely returning bad diagnostics.

    POSIX-only by necessity: a faithful Windows shim would have to be a real
    ``.exe``, because ``CreateProcess`` appends only that extension when it
    resolves a bare argv name and so never reaches a planted ``.bat``. The
    Windows guarantee is covered at the resolution level instead, by
    ``test_trusted_system_bin_resolves_system32_and_rejects_path_on_windows``.
    """

    import socket

    if pc.IS_WINDOWS:  # pragma: no cover - POSIX binary resolution
        pytest.skip("POSIX binary resolution")

    bogus = 999_999
    tool = pc.listening_pid_tool()
    shim = tmp_path / tool
    shim.write_text(f"#!/bin/sh\necho {bogus}\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        pids = pc.find_listening_pids(_listening_port(sock))

    assert bogus not in pids, "planted PATH shim was executed"
    if pc.trusted_system_bin(tool) is not None:
        # A trusted tool exists on this host, so an empty list would not be a
        # real answer — the lookup must still see this process holding the port.
        # Without this the test could pass simply by returning nothing.
        assert os.getpid() in pids


def test_listening_pid_lookup_still_resolves_the_pinned_tool(tmp_path, monkeypatch):
    """Pinning must not cost the lookup its real answer, PATH notwithstanding.

    Guards the other direction from the shim test: a pin that resolved nothing
    would make every port read as unheld, which is silent and fails open into
    "no gateway is running".
    """

    import socket

    tool = pc.listening_pid_tool()
    if pc.trusted_system_bin(tool) is None:  # pragma: no cover - host lacks the tool
        pytest.skip(f"no trusted {tool} on this host")

    # An empty PATH proves the resolution owes nothing to it.
    monkeypatch.setenv("PATH", str(tmp_path))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        pids = pc.find_listening_pids(_listening_port(sock))

    assert os.getpid() in pids


def test_listening_pid_tool_available_ignores_a_planted_tool_on_path(tmp_path, monkeypatch):
    """The availability probe must agree with the lookup it describes.

    Probing PATH here while the lookup resolves from the trusted directories
    would let the two disagree: a shim would answer "available" for a tool the
    lookup refuses to run, and a live gateway would read as stopped.
    """

    tool = pc.listening_pid_tool()
    planted = tmp_path / (f"{tool}.exe" if pc.IS_WINDOWS else tool)
    planted.write_text("")
    if not pc.IS_WINDOWS:
        planted.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    # Empty the trusted directories so the only resolvable copy of the tool is
    # the planted one. A host that genuinely ships the tool would otherwise
    # answer True for both the pinned and the PATH lookup, and the test could
    # not tell them apart.
    monkeypatch.setattr(pc, "_TRUSTED_SYSTEM_BIN_DIRS", ())
    monkeypatch.setattr(pc, "_windows_system_dirs", lambda: ())

    assert pc.trusted_system_bin(tool) is None
    assert pc.listening_pid_tool_available() is False


def test_listening_pid_lookup_degrades_when_no_trusted_tool_exists(monkeypatch):
    """No trusted tool must read as "absent", never as a silent empty answer."""

    monkeypatch.setattr(pc, "trusted_system_bin", lambda name: None)
    assert pc.find_listening_pids(8000) == []
    assert pc.listening_pid_tool_available() is False


def test_process_owner_uid_ignores_a_planted_ps_on_path(tmp_path, monkeypatch):
    """The uid backing the port-trust gate must not come from a PATH shim.

    ``process_owner_uid`` reads ``/proc`` on Linux and shells out to ``ps`` only
    on macOS, so the darwin branch is selected explicitly to exercise the spawn
    on any POSIX host rather than leaving it covered on macOS CI alone.
    """

    if pc.IS_WINDOWS:  # pragma: no cover - POSIX binary resolution
        pytest.skip("POSIX binary resolution")

    shim = tmp_path / "ps"
    shim.write_text("#!/bin/sh\necho 999999\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(sys, "platform", "darwin")

    assert pc.process_owner_uid(os.getpid()) == os.getuid()


def test_process_owner_uid_denies_when_no_trusted_ps_exists(monkeypatch):
    """An unresolvable ``ps`` must report "unknown owner", which the gate denies on."""

    if pc.IS_WINDOWS:  # pragma: no cover - POSIX binary resolution
        pytest.skip("POSIX binary resolution")

    monkeypatch.setattr(pc, "trusted_system_bin", lambda name: None)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert pc.process_owner_uid(os.getpid()) is None


def test_trusted_system_bin_resolves_system32_and_rejects_path_on_windows(tmp_path, monkeypatch):
    """Windows argv names must resolve from the real system directory only."""

    if not pc.IS_WINDOWS:  # pragma: no cover - Windows binary resolution
        pytest.skip("Windows binary resolution")

    planted = tmp_path / "definitely-not-a-system-tool.exe"
    planted.write_text("")
    monkeypatch.setenv("PATH", str(tmp_path))

    assert pc.trusted_system_bin("definitely-not-a-system-tool") is None
    # A bare argv name still resolves, extension supplied by the lookup.
    resolved = pc.trusted_system_bin("taskkill")
    assert resolved is not None and resolved.lower().endswith("taskkill.exe")
    assert os.path.isfile(resolved)


def test_kill_helpers_fail_loud_when_taskkill_is_unresolvable(monkeypatch):
    """Windows kills must raise, not silently report success, with no taskkill.

    Callers branch on the exception to escalate; a quiet ``True`` would strand a
    live process while reporting it terminated.
    """

    if not pc.IS_WINDOWS:  # pragma: no cover - Windows kill path
        pytest.skip("Windows kill path")

    # A PID that does not exist, so a regression that reaches the real taskkill
    # cannot terminate the test runner; ``match`` pins the failure to the
    # resolution step rather than to taskkill rejecting an unknown PID.
    monkeypatch.setattr(pc, "trusted_system_bin", lambda name: None)
    with pytest.raises(OSError, match="trusted system directories"):
        pc.kill_pid(999_999)
    with pytest.raises(OSError, match="trusted system directories"):
        pc.kill_process_tree(999_999)


def _plant_on_path(tmp_path, monkeypatch, name):
    """Make *name* the only thing PATH can resolve, and return its path."""

    planted = tmp_path / (f"{name}.exe" if pc.IS_WINDOWS else name)
    planted.write_text("")
    if not pc.IS_WINDOWS:
        planted.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    return planted


def _pin_warnings(caplog):
    """Only this module's records, so an unrelated warning cannot skew the count."""

    return [r for r in caplog.records if r.name == "kiro_crew.platform_compat"]


def test_a_tool_installed_outside_the_trusted_dirs_is_diagnosable(tmp_path, monkeypatch, caplog):
    """A non-FHS host must learn the pin is why its tool reads as unavailable.

    NixOS and Homebrew/conda prefixes keep a perfectly good ``lsof`` outside the
    system directories. The pin still refuses it, but without this line the
    operator sees only ``kirocrew stop`` no-opping and a prompt to install a
    tool they already have.
    """

    monkeypatch.setattr(pc, "_UNPINNED_TOOL_PROBED", set())
    planted = _plant_on_path(tmp_path, monkeypatch, "definitely-not-a-system-tool")

    with caplog.at_level(logging.WARNING, logger="kiro_crew.platform_compat"):
        assert pc.trusted_system_bin("definitely-not-a-system-tool") is None

    records = _pin_warnings(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    # Case-insensitive: Windows resolution reports the PATHEXT entry's own
    # casing (".EXE"), not the casing the file was created with.
    assert str(planted).casefold() in message.casefold(), "must name where the tool actually is"
    assert "unavailable" in message


def test_the_unpinned_tool_diagnostic_does_not_repeat(tmp_path, monkeypatch, caplog):
    """One line per name: these lookups run on every teardown and gate check."""

    monkeypatch.setattr(pc, "_UNPINNED_TOOL_PROBED", set())
    _plant_on_path(tmp_path, monkeypatch, "definitely-not-a-system-tool")

    with caplog.at_level(logging.WARNING, logger="kiro_crew.platform_compat"):
        for _ in range(3):
            assert pc.trusted_system_bin("definitely-not-a-system-tool") is None

    assert len(_pin_warnings(caplog)) == 1


def test_a_genuinely_absent_tool_is_not_reported_as_misplaced(tmp_path, monkeypatch, caplog):
    """Nothing on PATH means nothing to explain, so the line must stay quiet.

    Claiming a tool sits outside the trusted directories when it is simply not
    installed would send the operator hunting for a path that does not exist.
    """

    monkeypatch.setattr(pc, "_UNPINNED_TOOL_PROBED", set())
    monkeypatch.setenv("PATH", str(tmp_path))

    with caplog.at_level(logging.WARNING, logger="kiro_crew.platform_compat"):
        assert pc.trusted_system_bin("definitely-not-a-system-tool") is None

    assert _pin_warnings(caplog) == []


def test_a_resolvable_tool_is_never_reported_as_sitting_outside_the_pin(monkeypatch):
    """Nothing to explain when the pinned lookup succeeded."""

    monkeypatch.setattr(pc, "trusted_system_bin", lambda name: os.path.join("/usr/bin", name))
    assert pc.tool_outside_trusted_dirs("lsof") is None


def test_the_unpinned_path_is_reported_so_stop_can_name_it(tmp_path, monkeypatch):
    """``stop`` needs the real location to tell a NixOS operator what happened."""

    monkeypatch.setattr(pc, "trusted_system_bin", lambda name: None)
    planted = _plant_on_path(tmp_path, monkeypatch, "definitely-not-a-system-tool")

    found = pc.tool_outside_trusted_dirs("definitely-not-a-system-tool")

    assert found is not None
    assert found.casefold() == str(planted).casefold()


def test_an_absent_tool_reports_no_unpinned_path(tmp_path, monkeypatch):
    """Absent everywhere must stay ``None``, or ``stop`` would claim a path that
    does not exist instead of saying the tool is missing."""

    monkeypatch.setattr(pc, "trusted_system_bin", lambda name: None)
    monkeypatch.setenv("PATH", str(tmp_path))

    assert pc.tool_outside_trusted_dirs("definitely-not-a-system-tool") is None


# ── Desktop bundled-interpreter detection ──

_REPO_ROOT = Path(__file__).parent.parent


class TestIsBundledInterpreter:
    """``is_bundled_interpreter`` is the single runtime owner of the desktop
    packaging-layout sentinel; these tests pin both its behavior and its
    agreement with the packaging layer, so a bundler directory rename breaks a
    test here instead of silently un-matching the runtime guard (which would
    let pip write into the signed macOS bundle)."""

    def test_bundled_interpreter_path_is_detected(self, tmp_path, monkeypatch):
        """The real desktop layout — a python-build-standalone runtime under
        ``Resources/backend-dist/`` — must be recognized. The literal directory
        name is deliberate here: the test pins the real-world layout, not the
        constant (asserting via the constant would be tautological)."""
        bundled = (
            tmp_path
            / "App.app"
            / "Contents"
            / "Resources"
            / "backend-dist"
            / "kirocrew-backend-arm64"
            / "bin"
            / "python3.12"
        )
        monkeypatch.setattr(pc.sys, "executable", str(bundled))
        assert pc.is_bundled_interpreter() is True

    def test_regular_interpreter_path_is_not_detected(self, tmp_path, monkeypatch):
        """An ordinary venv interpreter must not trip the guard — a false
        positive would refuse every Python app build on normal installs."""
        regular = tmp_path / "gateway-venv" / "bin" / "python3.12"
        monkeypatch.setattr(pc.sys, "executable", str(regular))
        assert pc.is_bundled_interpreter() is False

    def test_sentinel_matches_electron_builder_packaging_layout(self):
        """Pin the constant to electron-builder's ``extraResources`` target so
        a packaging rename fails HERE, not at runtime inside a signed bundle."""
        pkg_json = _REPO_ROOT / "website" / "electron" / "package.json"
        pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
        targets = {
            res["to"]
            for res in pkg["build"]["extraResources"]
            if isinstance(res, dict) and "to" in res
        }
        assert pc.BUNDLED_BACKEND_DIST_DIRNAME in targets, (
            "platform_compat.BUNDLED_BACKEND_DIST_DIRNAME no longer matches the "
            "electron-builder extraResources target in website/electron/package.json. "
            "If the desktop packaging directory was renamed, update the constant "
            "(and this test) in the same change — otherwise the bundled-interpreter "
            "guard silently stops matching and pip can write into the signed bundle."
        )

    def test_sentinel_matches_desktop_build_script_staging_dir(self):
        """Same pin against the build script that stages the runtime trees.

        Asserts the directory NAME as a path component — not any exact
        shell-quoted expression — so a script refactor that introduces a
        variable for the staging path does not false-positive this pin."""
        script = (_REPO_ROOT / "packaging" / "build-desktop.sh").read_text(encoding="utf-8")
        needle = f"/{pc.BUNDLED_BACKEND_DIST_DIRNAME}"
        assert needle in script, (
            "packaging/build-desktop.sh no longer stages anything under a "
            f"'{pc.BUNDLED_BACKEND_DIST_DIRNAME}' directory — keep "
            "platform_compat.BUNDLED_BACKEND_DIST_DIRNAME in sync with the "
            "packaging layer (see is_bundled_interpreter)."
        )


class TestTrustedGitBin:
    """`git` resolution for privileged/unattended callers.

    Moved here from `test_cli_doctor` with the logic: the doctor and the update
    seam are two callers of one resolver, so the resolution rules belong beside
    the resolver rather than in either caller's tests.
    """

    def test_uses_the_trusted_system_resolver(self, monkeypatch) -> None:
        monkeypatch.setattr(pc, "trusted_system_bin", lambda _n: "/usr/bin/git")
        assert pc.trusted_git_bin() == "/usr/bin/git"

    def test_windows_falls_back_to_the_git_for_windows_roots(self, monkeypatch, tmp_path) -> None:
        """Git for Windows installs under Program Files, never System32.

        Without the fallback every supported Windows source install resolves to
        None, which would silently disable the callers that depend on it.
        """
        monkeypatch.setattr(pc, "trusted_system_bin", lambda _n: None)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)

        gfw = tmp_path / "Git" / "cmd"
        gfw.mkdir(parents=True)
        exe = gfw / "git.exe"
        exe.write_text("")
        exe.chmod(0o755)
        monkeypatch.setattr(pc, "_WINDOWS_GIT_DIRS", (str(gfw),))
        assert pc.trusted_git_bin() == str(exe)

    def test_windows_returns_none_when_the_roots_are_empty(self, monkeypatch) -> None:
        """Fixed roots only -- a miss returns None without consulting PATH.

        Reading `%ProgramFiles%` instead would let a poisoned variable redirect
        the lookup to an agent-writable directory, which is the hole the pin
        exists to close.
        """
        monkeypatch.setattr(pc, "trusted_system_bin", lambda _n: None)
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_WINDOWS_GIT_DIRS", (r"Z:\nonexistent\Git\cmd",))
        assert pc.trusted_git_bin() is None

    def test_posix_never_probes_the_windows_roots(self, monkeypatch) -> None:
        """On POSIX the trusted-dirs decision is final."""
        monkeypatch.setattr(pc, "trusted_system_bin", lambda _n: None)
        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        monkeypatch.setattr(
            pc,
            "_WINDOWS_GIT_DIRS",
            property(lambda _s: (_ for _ in ()).throw(AssertionError("probed on POSIX"))),
        )
        assert pc.trusted_git_bin() is None
