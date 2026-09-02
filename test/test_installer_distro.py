"""Managed-Python (uv) bootstrap coverage for the shell installers.

The published one-liner ``curl … cli.sh | sh`` must bring up a runnable gateway
even when the host has no usable Python 3.12+. cli.sh does this WITHOUT the
system package manager: it fetches a SHA-256-pinned uv tarball (or uses an
already-installed ``uv``) and provisions a python-build-standalone CPython into
a user-owned directory. These tests run the real ``cli.sh`` under a fabricated
``PATH`` that contains NO usable ``python3``, with a recording ``curl`` stub,
so the script is forced through the managed-Python path. POSIX-only scripts,
so the suite skips on native Windows.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from installer_test_helpers import run_bounded

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
    interpreters: dict[str, str] | None = None,
    with_timeout: bool = True,
    with_uv: str | None = None,
    curl_stub: str | None = None,
    extra_args: list[str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run cli.sh with a PATH that has NO usable python3 and a recording
    ``curl`` stub. Returns the process result plus the marker directory the
    stubs wrote to.

    ``interpreters`` replaces the default too-old stub for the named
    interpreters, which is how a scenario expresses a usable or a wedged
    candidate. ``with_uv`` plants an executable ``uv`` stub with the given
    body. ``curl_stub`` replaces the default recording curl (which exits 22,
    modelling a failed download).
    """
    tools = tmp_path / "tools"
    tools.mkdir()
    markers = tmp_path / "markers"
    markers.mkdir()

    # The PATH is ISOLATED to `tools` plus symlinks to the specific real
    # coreutils cli.sh needs. It deliberately does NOT include /usr/bin etc.,
    # so no real curl, uv, or interpreter can leak into the ladder.
    def _link_real(name: str) -> None:
        real = shutil.which(name)
        if real:
            (tools / name).symlink_to(real)

    for _util in ("sh", "env", "id", "awk", "sed", "mktemp", "rm", "rmdir",
                  "mkdir", "cat", "printf", "chmod", "ln", "date", "grep",
                  "tr", "head", "cut", "dirname", "basename", "uname", "sleep",
                  "tar", "gzip", "sha256sum",
                  # cli.sh bounds its interpreter probe with `timeout` when one
                  # exists, so the isolated PATH must expose it for that guard
                  # to be exercised here at all. ``with_timeout=False`` models a
                  # stock macOS (no coreutils `timeout`), forcing the POSIX
                  # watchdog fallback instead.
                  *(("timeout",) if with_timeout else ())):
        _link_real(_util)

    # Recording curl: the marker path is baked in absolutely because cli.sh
    # does not EXPORT its variables, so a child stub would not inherit an env
    # var. The default stub records the request and fails (exit 22, curl's
    # HTTP-error code), which is how a scenario proves WHICH URL cli.sh
    # reached for without any network.
    curl_marker = markers / "curl"
    curl = tools / "curl"
    curl.write_text(
        curl_stub
        if curl_stub is not None
        else (
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> "{curl_marker}"\n'
            "exit 22\n"
        )
    )
    curl.chmod(0o755)

    if with_uv is not None:
        uv = tools / "uv"
        uv.write_text(with_uv)
        uv.chmod(0o755)

    # Every interpreter cli.sh probes for is an executable stub reporting an
    # OLD (<3.12) version, so the isolated PATH guarantees the "no usable
    # python" branch. The stub answers cli.sh's version check (exit 1) and
    # `--version`. Extra names in ``interpreters`` (e.g. the fake interpreter a
    # uv stub claims to have installed) are created too.
    default_names = ("python3.13", "python3.12", "python3", "python")
    for name in {*default_names, *(interpreters or {})}:
        stub = tools / name
        stub.write_text(
            interpreters.get(name)
            if interpreters and name in interpreters
            else (
                "#!/bin/sh\n"
                'case "$*" in\n'
                "  *version_info*) exit 1 ;;\n"  # cli.sh's >=3.12 gate -> too old
                "  *--version*) echo 'Python 3.6.8' ;;\n"
                "  *) exit 1 ;;\n"
                "esac\n"
            )
        )
        stub.chmod(0o755)

    # openssl needs to exist so cli.sh's tool preflight passes and it reaches
    # the Python block; it is never actually reached in these scenarios, but
    # `command -v` must find it.
    openssl = tools / "openssl"
    openssl.write_text("#!/bin/sh\nexit 0\n")
    openssl.chmod(0o755)

    env = {
        # Isolated: ONLY the fake tools dir. No system bin dirs, so no real
        # curl, uv, or interpreter can leak into cli.sh's ladder.
        "PATH": str(tools),
        "HOME": str(tmp_path / "home"),
        "KIROCREW_HOME": str(tmp_path / "data-home"),
    }
    argv = [str(tools / "sh"), str(CLI_SH), *(extra_args or [])]
    result = run_bounded(argv, env)
    return result, markers


def test_cli_fails_over_from_a_wedged_interpreter_candidate(tmp_path: Path) -> None:
    """A version-manager shim that never answers must not wedge the install.

    python3.12 is probed FIRST, so a shim that hangs there used to hold
    _resolve_python forever and leak a spinning orphan per invocation. The probe
    is bounded, so resolution has to reach the usable python3 below it.
    """
    result, _markers = _run_cli_with_fake_env(
        tmp_path,
        interpreters={
            "python3.12": "#!/bin/sh\nexec sleep 300\n",
            "python3": (
                "#!/bin/sh\n"
                'case "$*" in\n'
                "  *version_info*) exit 0 ;;\n"  # a supported interpreter
                "  *--version*) echo 'Python 3.12.0' ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n"
            ),
        },
    )

    combined = result.stdout + result.stderr
    assert "Python >=3.12 is required" not in combined, combined


def test_cli_bounds_wedged_interpreter_without_timeout_binary(tmp_path: Path) -> None:
    """Stock macOS ships no coreutils `timeout`; the POSIX watchdog must bound
    the probe there too.

    Same wedged-shim scenario as above, but with `timeout` absent from PATH.
    Before the watchdog existed, cli.sh simply ran the probe unbounded in this
    configuration, so the hanging python3.12 shim wedged the install forever.
    """
    result, _markers = _run_cli_with_fake_env(
        tmp_path,
        with_timeout=False,
        interpreters={
            "python3.12": "#!/bin/sh\nexec sleep 300\n",
            "python3": (
                "#!/bin/sh\n"
                'case "$*" in\n'
                "  *version_info*) exit 0 ;;\n"  # a supported interpreter
                "  *--version*) echo 'Python 3.12.0' ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n"
            ),
        },
    )

    combined = result.stdout + result.stderr
    assert "Python >=3.12 is required" not in combined, combined


def test_cli_watchdog_path_still_rejects_too_old_interpreters(tmp_path: Path) -> None:
    """The watchdog fallback must propagate the probe's REAL exit status.

    With `timeout` absent, every candidate is the default too-old stub (exits 1
    at the version gate). If the watchdog path swallowed that status, cli.sh
    would accept Python 3.6 as usable and skip the managed-Python branch; the
    curl marker reaching for the uv tarball proves the "no usable python" path
    was taken instead.
    """
    result, markers = _run_cli_with_fake_env(tmp_path, with_timeout=False)

    assert (markers / "curl").exists(), result.stderr
    assert "astral-sh/uv/releases/download" in (markers / "curl").read_text()


def test_cli_falls_back_to_pinned_uv_when_no_python(tmp_path: Path) -> None:
    """With no usable interpreter anywhere, cli.sh must reach for the PINNED uv
    release tarball over HTTPS — not a package manager, not the astral install
    script — and fail with actionable guidance when that download fails."""
    result, markers = _run_cli_with_fake_env(tmp_path)

    assert result.returncode != 0
    recorded = (markers / "curl").read_text()
    assert "https://github.com/astral-sh/uv/releases/download/" in recorded
    assert ".tar.gz" in recorded
    # The failure guidance must tell the user what to do next, without
    # hardcoding one distro's package manager as the answer.
    combined = result.stdout + result.stderr
    assert "Python >=3.12 is required and could not be found or provisioned" in combined


def test_cli_uses_installed_uv_before_downloading_one(tmp_path: Path) -> None:
    """An already-present `uv` on PATH is the user's own trust decision — the
    installer must drive IT (python install + python find) instead of
    downloading another copy."""
    uv_marker = tmp_path / "markers" / "uv"
    fake_python = tmp_path / "tools" / "uv-python"
    uv_stub = (
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{uv_marker}"\n'
        'case "$*" in\n'
        f'  *"python find"*) echo "{fake_python}" ;;\n'
        "esac\n"
        "exit 0\n"
    )
    result, markers = _run_cli_with_fake_env(
        tmp_path,
        with_uv=uv_stub,
        # The provisioned interpreter must satisfy the same >=3.12 usability
        # probe as a system one; this stub models the PBS python uv installed.
        interpreters={
            "uv-python": (
                "#!/bin/sh\n"
                'case "$*" in\n'
                "  *version_info*) exit 0 ;;\n"
                "  *--version*) echo 'Python 3.12.0' ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n"
            ),
        },
    )

    recorded = (markers / "uv").read_text()
    assert "python install cpython-3.12" in recorded
    assert "python find cpython-3.12" in recorded
    # No uv tarball download happened: the only curl traffic (if any) is the
    # manifest fetch that comes AFTER the python gate passed.
    curl_marker = markers / "curl"
    if curl_marker.exists():
        assert "astral-sh/uv" not in curl_marker.read_text()
    # The run got PAST the python gate: whatever it fails on later (this
    # hermetic env cannot satisfy the trust-root/manifest steps), it must not
    # be the interpreter requirement.
    combined = result.stdout + result.stderr
    assert "Python >=3.12 is required" not in combined, combined
    assert "could not provision a managed Python" not in combined, combined


def test_cli_managed_python_flag_skips_system_interpreters(tmp_path: Path) -> None:
    """--managed-python must go straight to uv even when a usable system
    interpreter exists — that is the flag's whole contract."""
    result, markers = _run_cli_with_fake_env(
        tmp_path,
        extra_args=["--managed-python"],
        interpreters={
            "python3.12": (
                "#!/bin/sh\n"
                'case "$*" in\n'
                "  *version_info*) exit 0 ;;\n"  # usable — and must be skipped
                "  *--version*) echo 'Python 3.12.0' ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n"
            ),
        },
    )

    # It reached for the uv tarball despite the usable python3.12 on PATH.
    assert (markers / "curl").exists(), result.stderr
    assert "astral-sh/uv/releases/download" in (markers / "curl").read_text()
    assert result.returncode != 0  # recording curl fails the download
    combined = result.stdout + result.stderr
    assert "could not provision a managed Python via uv" in combined


def test_cli_rejects_a_tampered_uv_download(tmp_path: Path) -> None:
    """A uv tarball whose digest does not match the pinned SHA-256 must stop the
    install as a security failure — never be extracted or executed."""
    # This curl "succeeds" and writes attacker-controlled bytes to -o <file>.
    curl_stub = (
        "#!/bin/sh\n"
        "out=''\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in\n'
        "    -o) out=\"$2\"; shift 2 ;;\n"
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        '[ -n "$out" ] && printf "not-really-uv" > "$out"\n'
        "exit 0\n"
    )
    result, _markers = _run_cli_with_fake_env(tmp_path, curl_stub=curl_stub)

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "uv download SHA-256 mismatch" in combined
    assert "refusing to continue" in combined


def test_cli_does_not_pipe_an_unsigned_installer_into_a_shell() -> None:
    # The signed installer must never fetch-and-execute a third-party script:
    # `curl … mise.run | sh` and `curl … astral.sh | sh` bootstraps are the
    # exact pattern this invariant exists to prevent. uv arrives as a tarball
    # verified against a pinned digest, never as a piped script.
    text = CLI_SH.read_text()
    for needle in ("mise.run", "astral.sh"):
        for lineno, line in enumerate(text.splitlines(), start=1):
            if needle in line and "| sh" in line:
                raise AssertionError(
                    f"cli.sh:{lineno} pipes a third-party script into a shell: {line!r}"
                )


def test_cli_never_drives_the_system_package_manager() -> None:
    # Provisioning Python is a user-directory download, never a system
    # mutation: no branch of the installer may invoke a package manager or
    # sudo. (Package-manager names inside err() guidance strings are fine —
    # text the user chooses to run themselves.)
    for lineno, line in enumerate(CLI_SH.read_text().splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        for needle in ("sudo ", "apt-get ", "dnf ", "yum "):
            if needle in stripped:
                assert stripped.startswith("err ") or "|| err " in stripped, (
                    f"cli.sh:{lineno} drives the system package manager: {line!r}"
                )


def test_cli_relinks_the_interpreter_when_rebuilding_a_venv(tmp_path: Path) -> None:
    """Re-running the installer over an existing managed venv must recreate the
    interpreter links, not inherit them -- WITHOUT destroying the install.

    ``python -m venv`` on an existing venv rewrites pyvenv.cfg but LEAVES an
    existing bin/python* symlink in place: switching an existing install to
    --managed-python would produce a hybrid venv that claims the new
    interpreter while its shebangs still resolve to the old system one, dying
    the day that interpreter is removed. The fix removes ONLY the interpreter
    links before the rebuild -- never ``venv --clear``, which would empty
    site-packages and the entrypoint too, so a failed wheel download in the
    step after would leave NO working install. This locks both halves: the
    link is recreated against the new interpreter, and non-link venv content
    survives.
    """
    a_dir = tmp_path / "old-interp"
    a_dir.mkdir()
    ver = f"{sys.version_info[0]}.{sys.version_info[1]}"
    a_python = a_dir / f"python{ver}"
    a_python.symlink_to(sys.executable)

    venv_dir = tmp_path / "crew-venv"
    subprocess.run(
        [str(a_python), "-m", "venv", "--without-pip", str(venv_dir)],
        check=True,
        cwd=tmp_path,
    )
    link = venv_dir / "bin" / f"python{ver}"
    assert link.is_symlink()
    # Site content that a `venv --clear` would have destroyed.
    survivor = venv_dir / "lib" / f"python{ver}" / "site-packages" / "keepme.txt"
    survivor.parent.mkdir(parents=True, exist_ok=True)
    survivor.write_text("installed package data")

    # Rebuild with the same real python by its own path, through the exact
    # guarded shape cli.sh runs (pyvenv.cfg present, not a symlink root ->
    # drop the interpreter links, then a plain venv rebuild).
    guard = (
        f'if [ -f "{venv_dir}/pyvenv.cfg" ] && [ ! -L "{venv_dir}" ]; then '
        f'rm -f "{venv_dir}/bin/python" "{venv_dir}/bin/python3" '
        f'"{venv_dir}/bin"/python3.* 2>/dev/null || true; fi; '
        f'"{sys.executable}" -m venv --without-pip "{venv_dir}"'
    )
    subprocess.run(["sh", "-c", guard], check=True, cwd=tmp_path)

    # The link was recreated: routing through the old scratch dir is the
    # stale-symlink hybrid this test exists to prevent.
    assert str(a_dir) not in os.readlink(venv_dir / "bin" / f"python{ver}"), (
        "venv rebuild kept the previous interpreter symlink -- the link "
        "removal in cli.sh has been lost"
    )
    # And the install's data survived (a --clear would have deleted it).
    assert survivor.read_text() == "installed package data"
    text = CLI_SH.read_text()
    assert "-m venv --clear" not in text, (
        "cli.sh uses venv --clear again: a wheel-download failure after the "
        "clear would leave the user with no working install"
    )
    assert 'rm -f "$VENV/bin/python"' in text


def test_cli_link_removal_never_reaches_a_symlinked_venv(tmp_path: Path) -> None:
    """The pre-rebuild link removal must not run against a SYMLINKED venv
    root: the venv module refuses a symlink root anyway, so removing links
    inside its target first would break the linked venv and then abort. The
    guard strips a trailing slash so `-L` tests the link itself (a trailing
    slash makes the shell follow it)."""
    real = tmp_path / "real-venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(real)],
        check=True,
        cwd=tmp_path,
    )
    link = tmp_path / "link"
    link.symlink_to(real)
    ver = f"{sys.version_info[0]}.{sys.version_info[1]}"
    interp_link = real / "bin" / f"python{ver}"
    assert interp_link.exists()

    # The exact guarded shape cli.sh runs, against the symlink WITH a trailing
    # slash (the spelling that defeats a naive -L test).
    venv_arg = f"{link}/"
    stripped = str(link)
    guard = (
        f'V="{venv_arg}"; '
        f'if [ -f "$V/pyvenv.cfg" ] && [ ! -L "{stripped}" ]; then '
        f'rm -f "$V/bin/python" "$V/bin/python3" "$V/bin"/python3.* '
        f"2>/dev/null || true; fi"
    )
    subprocess.run(["sh", "-c", guard], check=True, cwd=tmp_path)

    assert interp_link.exists(), (
        "the symlink guard is gone: the link removal ran through a symlinked "
        "venv root and broke its target"
    )
    # And the trailing-slash-stripped guard must still be present in cli.sh.
    assert '[ ! -L "${VENV%/}" ]' in CLI_SH.read_text()


def test_cli_reuses_a_recorded_managed_python_choice(tmp_path: Path) -> None:
    """A completed --managed-python install records its mode; a later run
    WITHOUT the flag must reuse it -- most importantly the re-run that
    `kirocrew update` performs, which passes only --channel. Without the
    marker read, every update would silently flip a managed install back onto
    whatever system interpreter it finds."""
    data_home = tmp_path / "data-home"
    data_home.mkdir()
    (data_home / "python-mode").write_text("managed\n")

    result, markers = _run_cli_with_fake_env(
        tmp_path,
        # A perfectly usable system python that the recorded choice must skip.
        interpreters={
            "python3.12": (
                "#!/bin/sh\n"
                'case "$*" in\n'
                "  *version_info*) exit 0 ;;\n"
                "  *--version*) echo 'Python 3.12.0' ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n"
            ),
        },
    )

    assert (markers / "curl").exists(), result.stderr
    assert "astral-sh/uv/releases/download" in (markers / "curl").read_text()
    combined = result.stdout + result.stderr
    assert "Reusing the recorded managed-python choice" in combined


def test_cli_system_python_flag_overrides_the_recorded_choice(tmp_path: Path) -> None:
    """--system-python is the opt-out: with a 'managed' marker on disk it must
    use the system interpreter and never reach for uv."""
    data_home = tmp_path / "data-home"
    data_home.mkdir()
    (data_home / "python-mode").write_text("managed\n")

    result, markers = _run_cli_with_fake_env(
        tmp_path,
        extra_args=["--system-python"],
        interpreters={
            "python3.12": (
                "#!/bin/sh\n"
                'case "$*" in\n'
                "  *version_info*) exit 0 ;;\n"
                "  *--version*) echo 'Python 3.12.0' ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n"
            ),
        },
    )

    combined = result.stdout + result.stderr
    assert "Reusing the recorded managed-python choice" not in combined
    assert "Python >=3.12 is required" not in combined, combined
    curl_marker = markers / "curl"
    if curl_marker.exists():
        assert "astral-sh/uv" not in curl_marker.read_text()


def _run_marker_write_shape(data_home: Path, tmp_path: Path) -> subprocess.CompletedProcess[bytes]:
    """Run the exact guarded marker-write shape cli.sh uses, targeting
    ``python-mode`` with value ``managed``. Contained under tmp_path."""
    shape = (
        f'd="{data_home}/python-mode"; '
        f'if [ -L "$d" ]; then rm -f "$d"; fi; '
        f'if [ -d "$d" ]; then echo "refusing: directory" >&2; exit 1; fi; '
        f't="$(mktemp "{data_home}/.marker.XXXXXX")"; '
        f'printf \'%s\\n\' managed > "$t"; '
        f'mv -f "$t" "$d"'
    )
    return subprocess.run(
        ["sh", "-c", shape], check=False, capture_output=True, cwd=tmp_path
    )


def test_cli_marker_write_replaces_a_planted_symlink(tmp_path: Path) -> None:
    """The channel / python-mode marker writes must be symlink-proof: the data
    home is agent-writable, so a pre-planted `ln -sf ~/.bashrc .../python-mode`
    would turn a plain `>` redirection into an arbitrary-file overwrite on the
    next install run. The guarded shape removes the symlink and renames a
    fresh regular file into place -- never writing through the link."""
    data_home = tmp_path / "data-home"
    data_home.mkdir()
    victim = tmp_path / "victim-bashrc"
    victim.write_text("precious shell profile\n")
    (data_home / "python-mode").symlink_to(victim)

    result = _run_marker_write_shape(data_home, tmp_path)

    assert result.returncode == 0, result.stderr
    assert victim.read_text() == "precious shell profile\n", (
        "the marker write followed the planted symlink and overwrote the "
        "victim file -- the guarded write shape in cli.sh has been lost"
    )
    marker = data_home / "python-mode"
    assert not marker.is_symlink()
    assert marker.read_text() == "managed\n"


def test_cli_marker_write_survives_a_symlink_to_a_directory(tmp_path: Path) -> None:
    """A planted symlink can also point at a DIRECTORY -- `mv` onto a
    symlink-to-directory moves the temp file INSIDE the target instead of
    replacing the link, silently landing the marker outside its path. The
    symlink is removed before the rename regardless of what it points at."""
    data_home = tmp_path / "data-home"
    data_home.mkdir()
    victim_dir = tmp_path / "victim-dir"
    victim_dir.mkdir()
    (data_home / "python-mode").symlink_to(victim_dir)

    result = _run_marker_write_shape(data_home, tmp_path)

    assert result.returncode == 0, result.stderr
    assert list(victim_dir.iterdir()) == [], (
        "the marker rename landed inside the symlinked directory target"
    )
    marker = data_home / "python-mode"
    assert not marker.is_symlink()
    assert marker.read_text() == "managed\n"


def test_cli_marker_write_refuses_a_directory_target(tmp_path: Path) -> None:
    """A real directory occupying the marker path is corrupt state: `mv` would
    move the temp file inside it and every later read would silently miss the
    marker (an update would quietly fall back to defaults). The write must
    refuse loudly instead."""
    data_home = tmp_path / "data-home"
    data_home.mkdir()
    (data_home / "python-mode").mkdir()

    result = _run_marker_write_shape(data_home, tmp_path)

    assert result.returncode != 0
    assert (data_home / "python-mode").is_dir()
    assert list((data_home / "python-mode").iterdir()) == [], (
        "the marker temp file was moved inside the directory target"
    )
    # And the guards must still be present in cli.sh.
    text = CLI_SH.read_text()
    assert "_write_marker" in text
    assert 'mktemp "$_DATA_HOME/.marker.XXXXXX"' in text
    assert '[ -d "$_marker_dest" ]' in text
    assert '[ -L "$_marker_dest" ]' in text


def test_cli_marker_read_ignores_a_planted_symlink(tmp_path: Path) -> None:
    """The marker READ is guarded like the write: a planted symlink at
    python-mode (e.g. to /dev/zero, which would wedge an unbounded cat, or to
    an attacker file spoofing 'managed') must be ignored -- the run proceeds
    on the system interpreter as if no marker existed."""
    data_home = tmp_path / "data-home"
    data_home.mkdir()
    spoof = tmp_path / "spoof"
    spoof.write_text("managed\n")
    (data_home / "python-mode").symlink_to(spoof)

    result, markers = _run_cli_with_fake_env(
        tmp_path,
        interpreters={
            "python3.12": (
                "#!/bin/sh\n"
                'case "$*" in\n'
                "  *version_info*) exit 0 ;;\n"
                "  *--version*) echo 'Python 3.12.0' ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n"
            ),
        },
    )

    combined = result.stdout + result.stderr
    assert "Reusing the recorded managed-python choice" not in combined
    curl_marker = markers / "curl"
    if curl_marker.exists():
        assert "astral-sh/uv" not in curl_marker.read_text()
    # And the read guards must still be present in cli.sh.
    text = CLI_SH.read_text()
    assert '[ ! -L "$_py_mode_file" ]' in text
    assert 'head -c 16 "$_py_mode_file"' in text


def test_cli_marker_read_never_opens_a_fifo(tmp_path: Path) -> None:
    """A FIFO planted at python-mode would block a reader forever; the -f
    regular-file guard must skip it without ever opening it (this test hangs
    at the harness timeout if the guard is lost)."""
    data_home = tmp_path / "data-home"
    data_home.mkdir()
    os.mkfifo(data_home / "python-mode")

    result, _markers = _run_cli_with_fake_env(
        tmp_path,
        interpreters={
            "python3.12": (
                "#!/bin/sh\n"
                'case "$*" in\n'
                "  *version_info*) exit 0 ;;\n"
                "  *--version*) echo 'Python 3.12.0' ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n"
            ),
        },
    )

    combined = result.stdout + result.stderr
    assert "Reusing the recorded managed-python choice" not in combined
