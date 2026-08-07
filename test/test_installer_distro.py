"""Distro-bootstrap coverage for the shell installers.

The published one-liner ``curl … cli.sh | sh`` must bring up a runnable gateway
on Ubuntu (apt, and the split ``python3-venv`` package), RHEL/CentOS 7 (yum,
Python 3.6 baseline), and RHEL/CentOS 8+/Amazon Linux (dnf). Before this
coverage existed cli.sh had ONLY a dnf branch, so Ubuntu and CentOS 7 hard-
failed at the "Python >=3.10 is required" gate, and no test ever entered the
Python-resolution block (the signing harness leaks the host ``python3``).

These tests run the real ``cli.sh`` under a fabricated ``PATH`` that contains NO
usable ``python3`` plus fake package managers, so the script is forced through
its distro-detection ladder. Each fake records that it ran; the assertion is
which manager cli.sh reached for on which distro. POSIX-only scripts, so the
suite skips on native Windows.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_SH = REPO_ROOT / "cli.sh"
INSTALL_SH = REPO_ROOT / "install.sh"
CLOUD_INSTALL_SH = REPO_ROOT / "cloud-install.sh"

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="cli.sh / install.sh are POSIX shell (macOS + Linux)"
)


def test_shell_installers_parse() -> None:
    """A syntax error in an installer reaches every new user's shell, and CI has
    no shellcheck gate — so the cheap `-n` parse check lives here."""
    subprocess.run(["sh", "-n", str(CLI_SH)], check=True)
    # install.sh / cloud-install.sh are bash (`#!/usr/bin/env bash`).
    subprocess.run(["bash", "-n", str(INSTALL_SH)], check=True)
    subprocess.run(["bash", "-n", str(CLOUD_INSTALL_SH)], check=True)


def _run_cli_with_fake_env(
    tmp_path: Path,
    *,
    managers: list[str],
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run cli.sh with a PATH that has NO python3 and only the named package
    managers (each a stub that records its name and args). Returns the process
    result plus the marker directory the stubs wrote to.

    The stubs deliberately do NOT install a working python, so cli.sh proceeds
    through every branch and then errs at the final "Python >=3.10 is required"
    guard — by which point the marker proves which manager it tried.
    """
    tools = tmp_path / "tools"
    tools.mkdir()
    markers = tmp_path / "markers"
    markers.mkdir()

    # The PATH is ISOLATED to `tools` plus symlinks to the specific real
    # coreutils cli.sh needs. It deliberately does NOT include /usr/bin etc.,
    # because `command -v <mgr>` skips a non-executable shadow and finds the
    # host's real binary beneath it (CI's Ubuntu runner HAS a real apt-get) —
    # so a distro is "absent" here simply by NOT creating its stub, not by
    # shadowing. Only managers this scenario declares present exist on PATH.
    def _link_real(name: str) -> None:
        real = shutil.which(name)
        if real:
            (tools / name).symlink_to(real)

    for _util in ("sh", "env", "id", "awk", "sed", "mktemp", "rm", "rmdir",
                  "mkdir", "cat", "printf", "chmod", "ln", "date", "grep",
                  "tr", "head", "cut", "dirname", "basename", "uname", "sleep"):
        _link_real(_util)

    # A manager "under test" is an EXECUTABLE stub that records its args. The
    # marker path is baked in absolutely: cli.sh does not EXPORT its variables,
    # so a child stub would not inherit an env var.
    for name in ("apt-get", "dnf", "yum"):
        if name in managers:
            marker = markers / name
            stub = tools / name
            stub.write_text(
                "#!/bin/sh\n"
                f'printf "%s\\n" "$*" >> "{marker}"\n'
                "exit 0\n"
            )
            stub.chmod(0o755)
    # mise, pipx are simply absent (never created) so the fallback finds none.

    # Fake `sudo`: strip a leading `-n` / option flags and exec the rest through
    # the SAME isolated PATH. Without this the test is not hermetic — a CI
    # runner with passwordless sudo makes cli.sh pick `sudo -n`, and the real
    # `sudo` resets PATH via secure_path so `sudo -n apt-get` would run the
    # host's real apt-get instead of our recording stub. `exec env PATH=... "$@"`
    # re-imposes the isolated PATH the way real sudo would impose secure_path.
    sudo_stub = tools / "sudo"
    sudo_stub.write_text(
        "#!/bin/sh\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in\n'
        "    -n|-E|-H) shift ;;\n"
        "    -*) shift ;;\n"
        "    *) break ;;\n"
        "  esac\n"
        "done\n"
        f'exec env "PATH={tools}" "$@"\n'
    )
    sudo_stub.chmod(0o755)

    # Every interpreter cli.sh probes for is an executable stub reporting an
    # OLD (<3.10) version, so the isolated PATH guarantees the "no usable
    # python" branch. The stub answers cli.sh's version check (exit 1) and
    # `--version`.
    for name in ("python3.13", "python3.12", "python3.11", "python3.10", "python3", "python"):
        stub = tools / name
        stub.write_text(
            "#!/bin/sh\n"
            'case "$*" in\n'
            "  *version_info*) exit 1 ;;\n"  # cli.sh's >=3.10 gate -> too old
            "  *--version*) echo 'Python 3.6.8' ;;\n"
            "  *) exit 1 ;;\n"
            "esac\n"
        )
        stub.chmod(0o755)

    # curl/openssl/sha256sum need to exist so cli.sh's tool preflight passes and
    # it reaches the Python block; they are never actually called before the
    # python gate fails, but `command -v` must find them.
    for name in ("curl", "openssl", "sha256sum"):
        stub = tools / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)

    env = {
        # Isolated: ONLY the fake tools dir. No system bin dirs, so no real
        # package manager or interpreter can leak into cli.sh's ladder.
        "PATH": str(tools),
        "HOME": str(tmp_path / "home"),
        "KIROCREW_HOME": str(tmp_path / "data-home"),
    }
    result = subprocess.run(
        [str(tools / "sh"), str(CLI_SH)],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    return result, markers


def test_cli_uses_apt_on_debian_ubuntu(tmp_path: Path) -> None:
    result, markers = _run_cli_with_fake_env(tmp_path, managers=["apt-get"])
    # No python was produced, so the run fails at the python gate — but it must
    # have reached for apt-get first (the Ubuntu path that did not exist before).
    assert (markers / "apt-get").exists(), result.stderr
    assert "install" in (markers / "apt-get").read_text()
    assert "python3-venv" in (markers / "apt-get").read_text()


def test_cli_uses_yum_on_centos7(tmp_path: Path) -> None:
    # CentOS 7 has yum but not dnf and no >=3.10 in base repos: the pre-fix
    # script had no yum branch and died with dnf-only advice.
    result, markers = _run_cli_with_fake_env(tmp_path, managers=["yum"])
    assert (markers / "yum").exists(), result.stderr
    assert "install" in (markers / "yum").read_text()


def test_cli_uses_dnf_on_modern_rhel(tmp_path: Path) -> None:
    result, markers = _run_cli_with_fake_env(tmp_path, managers=["dnf"])
    assert (markers / "dnf").exists(), result.stderr
    assert "python3.11" in (markers / "dnf").read_text()


def test_cli_prefers_apt_over_a_present_yum(tmp_path: Path) -> None:
    # Debian derivatives can carry a yum-alike; the apt branch must win so a
    # Debian box never drives an rpm manager.
    _result, markers = _run_cli_with_fake_env(tmp_path, managers=["apt-get", "yum"])
    assert (markers / "apt-get").exists()
    assert not (markers / "yum").exists()


def test_cli_error_message_is_distro_neutral_when_no_manager(tmp_path: Path) -> None:
    # With no package manager AND no mise reachable, the final error must not
    # hardcode Amazon-Linux/dnf advice (the pre-fix message did): it names the
    # Debian/Ubuntu and RHEL-family paths so a user on any target distro gets a
    # command that applies to them.
    result, _markers = _run_cli_with_fake_env(tmp_path, managers=[])
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "apt-get" in combined and "dnf" in combined and "CentOS 7" in combined


def test_cli_does_not_pipe_an_unsigned_installer_into_a_shell() -> None:
    # The signed installer must never fetch-and-execute a third-party script: a
    # `curl … mise.run | sh` bootstrap was removed for exactly this reason. It
    # may USE an already-installed mise, but never install one itself. Any
    # remaining `mise.run` reference must live INSIDE the final `err "…"`
    # guidance string — text the user chooses to run, not a line the installer
    # executes.
    for lineno, line in enumerate(CLI_SH.read_text().splitlines(), start=1):
        if "mise.run" not in line:
            continue
        stripped = line.lstrip()
        assert stripped.startswith("[ -n \"$PY\" ] || err ") or stripped.startswith("err "), (
            f"cli.sh:{lineno} references mise.run outside the err() guidance string: {line!r}"
        )
