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
import re
import shutil
import signal
import subprocess
import sys
import time
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
    extra_env: dict[str, str] | None = None,
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

    # openssl must be REAL: the trust-root self-check runs before the Python
    # block (fail-before-network), so these scenarios execute it for the
    # embedded key before reaching the interpreter ladder under test.
    _link_real("openssl")

    env = {
        # Isolated: ONLY the fake tools dir. No system bin dirs, so no real
        # curl, uv, or interpreter can leak into cli.sh's ladder.
        "PATH": str(tools),
        "HOME": str(tmp_path / "home"),
        "KIROCREW_HOME": str(tmp_path / "data-home"),
        **(extra_env or {}),
    }
    argv = [str(tools / "sh"), str(CLI_SH), *(extra_args or [])]
    result = run_bounded(argv, env)
    return result, markers


def test_cli_fails_over_from_a_wedged_interpreter_candidate(tmp_path: Path) -> None:
    """A version-manager shim that never answers must not wedge the install.

    python3.12 is probed FIRST, so a shim that hangs there would hold
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


def test_cli_never_executes_a_path_uv(tmp_path: Path) -> None:
    """A `uv` on PATH must NEVER be executed. With managed as the default,
    every install and update run reaches the provisioning path, and PATH
    commonly leads with user-writable directories (~/.local/bin) an agent
    session can write -- a planted shim there would run inside the user's own
    unattended update. Only the pinned, SHA-256-verified tarball is trusted."""
    uv_marker = tmp_path / "markers" / "uv"
    uv_stub = (
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{uv_marker}"\n'
        "exit 0\n"
    )
    result, markers = _run_cli_with_fake_env(tmp_path, with_uv=uv_stub)

    # The planted PATH uv was never invoked ...
    assert not (markers / "uv").exists(), (
        "cli.sh executed a uv found on PATH -- the pinned-tarball-only "
        "trust decision has been lost"
    )
    # ... and the pinned tarball download was attempted instead.
    assert (markers / "curl").exists(), result.stderr
    assert "astral-sh/uv/releases/download" in (markers / "curl").read_text()


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


def test_cli_restores_the_previous_venv_when_the_wheel_install_fails(
    tmp_path: Path,
) -> None:
    """A venv rebuild is transactional: the working venv is MOVED ASIDE before
    the fresh build, and a wheel-install failure puts it back. Without this,
    a default migration that relinks the venv to a different interpreter
    series and then loses the network at the pip step leaves a venv that
    cannot import its old site-packages -- the previous install is
    destroyed by a run that delivered nothing."""
    ver = f"{sys.version_info[0]}.{sys.version_info[1]}"
    venv_dir = tmp_path / "crew-venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv_dir)],
        check=True,
        cwd=tmp_path,
    )
    survivor = venv_dir / "lib" / f"python{ver}" / "site-packages" / "keepme.txt"
    survivor.parent.mkdir(parents=True, exist_ok=True)
    survivor.write_text("previous working install")

    # The exact transactional shape cli.sh runs: back up, rebuild, simulate a
    # failed pip step, restore.
    script = (
        f'V="{venv_dir}"; BK=""; '
        f'if [ -f "$V/pyvenv.cfg" ] && [ ! -L "$V" ]; then '
        f'BK="$V.pre-rebuild.$$"; mv "$V" "$BK" || BK=""; fi; '
        f'"{sys.executable}" -m venv --without-pip "$V"; '
        f"if ! false; then "  # the pip step, failing
        f'if [ -n "$BK" ] && [ -d "$BK" ]; then '
        f'rm -rf "$V"; mv "$BK" "$V" && exit 3; fi; exit 4; fi'
    )
    result = subprocess.run(["sh", "-c", script], cwd=tmp_path)

    assert result.returncode == 3, "the restore path did not run"
    assert survivor.read_text() == "previous working install", (
        "the pre-rebuild venv was not restored after the failed wheel "
        "install -- the migration is no longer rollback-safe"
    )
    assert not list(tmp_path.glob("crew-venv.pre-rebuild.*")), (
        "the restore left a stray backup tree behind"
    )
    # Pin the transactional shape in cli.sh itself.
    text = CLI_SH.read_text()
    assert 'mv "$VENV" "$_VENV_BACKUP"' in text, (
        "cli.sh no longer moves the working venv aside before the rebuild"
    )
    assert '_restore_tree "$_VENV_BACKUP" "$VENV"' in text, (
        "cli.sh no longer restores the pre-rebuild venv on a failed wheel "
        "install"
    )
    # The restore goes through _restore_tree, which refuses to `mv` onto a
    # path that survived `rm -rf` (the move would nest the backup and read
    # as a restore that never happened).
    assert 'mv "$1" "$2"' in text and 'if [ -e "$2" ] || [ -L "$2" ]; then' in text, (
        "cli.sh's restore helper no longer checks the target is gone before moving"
    )
    # The venv-creation step after the move-aside must be guarded too:
    # under `set -eu` an unguarded `"$PY" -m venv` failure (disk full at
    # ensurepip time) would exit past the restore and orphan the backup.
    # The creation runs through the `_run_step` progress wrapper, which
    # returns the command's own exit status, so the `if !` guard still sees
    # the failure.
    venv_guard = re.search(
        r'if ! _run_step "[^"]*" "[^"]*" "\$PY" -m venv "\$VENV"; then', text
    )
    assert venv_guard is not None, (
        "cli.sh runs venv creation unguarded after the move-aside -- a "
        "creation failure under set -eu skips the restore and destroys the "
        "working install"
    )
    # A tree already at the backup path (recycled PID) may be the only
    # WORKING install left by an interrupted earlier run, so it is never
    # deleted -- the move-aside picks the next free sibling instead.
    assert 'rm -rf "$_VENV_BACKUP"' not in text[: venv_guard.start()], (
        "cli.sh deletes whatever sits at the backup path before the "
        "move-aside -- an interrupted earlier run parks the working venv "
        "exactly there, so a recycled PID would destroy it"
    )
    assert 'while [ -e "$_VENV_BACKUP" ]; do' in text, (
        "cli.sh no longer steps past an occupied backup path before the "
        "move-aside -- mv would nest the venv inside the stale tree"
    )


def test_cli_venv_rebuild_preserves_a_stale_backup(tmp_path: Path) -> None:
    """A tree already sitting at the backup path is a working venv parked by
    an interrupted earlier run (rename done, restore never reached). With a
    recycled PID the new run must NOT delete it: it steps to the next free
    sibling, so both the parked venv and the one being rebuilt survive a
    second failure."""
    venv_dir = tmp_path / "crew-venv"
    venv_dir.mkdir()
    (venv_dir / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (venv_dir / "marker").write_text("current")

    script = (
        f'V="{venv_dir}"; STALE="$V.pre-rebuild.$$"; '
        f'mkdir "$STALE"; echo parked > "$STALE/marker"; '
        # The exact path-selection shape cli.sh runs.
        f'BK="$V.pre-rebuild.$$"; n=0; '
        f'while [ -e "$BK" ]; do n=$((n + 1)); BK="$V.pre-rebuild.$$.$n"; done; '
        f'mv "$V" "$BK" || exit 4; '
        f'[ "$(cat "$STALE/marker")" = parked ] || exit 5; '
        f'[ "$(cat "$BK/marker")" = current ] || exit 6; '
        f'[ "$BK" = "$STALE.1" ] || exit 7'
    )
    result = subprocess.run(["sh", "-c", script], cwd=tmp_path)
    assert result.returncode == 0, (
        f"exit {result.returncode}: the move-aside destroyed or nested into "
        "the stale backup instead of stepping past it"
    )
    backups = sorted(p.name for p in tmp_path.glob("crew-venv.pre-rebuild.*"))
    assert len(backups) == 2, backups


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


_USABLE_PY312_STUB = (
    "#!/bin/sh\n"
    'case "$*" in\n'
    "  *version_info*) exit 0 ;;\n"
    "  *--version*) echo 'Python 3.12.0' ;;\n"
    "  *) exit 0 ;;\n"
    "esac\n"
)


def test_cli_defaults_to_managed_python(tmp_path: Path) -> None:
    """With no flag, no env value, and no recorded pin, the installer must go
    for the managed interpreter EVEN THOUGH a usable system python3.12 is on
    PATH -- managed is the default, not the fallback."""
    result, markers = _run_cli_with_fake_env(
        tmp_path,
        interpreters={"python3.12": _USABLE_PY312_STUB},
    )

    combined = result.stdout + result.stderr
    assert "Using a managed Python (the default" in combined
    assert (markers / "curl").exists(), result.stderr
    assert "astral-sh/uv/releases/download" in (markers / "curl").read_text()


def test_cli_legacy_system_marker_migrates_to_managed(tmp_path: Path) -> None:
    """A bare `system` marker is what earlier installers recorded for EVERY
    default install -- it is not an opt-out. Such installs must migrate onto
    the managed default; only `system-pinned` (written by --system-python)
    holds an install on the system interpreter."""
    data_home = tmp_path / "data-home"
    data_home.mkdir()
    (data_home / "python-mode").write_text("system\n")

    result, markers = _run_cli_with_fake_env(
        tmp_path,
        interpreters={"python3.12": _USABLE_PY312_STUB},
    )

    combined = result.stdout + result.stderr
    assert "Reusing the recorded system-python choice" not in combined
    assert (markers / "curl").exists(), result.stderr
    assert "astral-sh/uv/releases/download" in (markers / "curl").read_text()


def test_cli_system_pinned_marker_stays_on_system(tmp_path: Path) -> None:
    """A recorded `system-pinned` marker (the --system-python opt-out) must
    survive a flag-less re-run -- the exact shape `kirocrew update` performs --
    and never reach for uv."""
    data_home = tmp_path / "data-home"
    data_home.mkdir()
    (data_home / "python-mode").write_text("system-pinned\n")

    result, markers = _run_cli_with_fake_env(
        tmp_path,
        interpreters={"python3.12": _USABLE_PY312_STUB},
    )

    combined = result.stdout + result.stderr
    assert "Reusing the recorded system-python choice" in combined
    assert "Python >=3.12 is required" not in combined, combined
    curl_marker = markers / "curl"
    if curl_marker.exists():
        assert "astral-sh/uv" not in curl_marker.read_text()


def test_cli_default_managed_falls_back_to_system_on_provision_failure(
    tmp_path: Path,
) -> None:
    """The managed DEFAULT must not turn a working install path into a hard
    network dependency: when the uv download fails and a usable system
    interpreter exists, the run degrades to it with a warning. Only an
    EXPLICIT managed request (flag / env / recorded pin) fails hard -- that
    contract is pinned by test_cli_managed_python_flag_skips_system_interpreters."""
    result, markers = _run_cli_with_fake_env(
        tmp_path,
        interpreters={"python3.12": _USABLE_PY312_STUB},
    )

    combined = result.stdout + result.stderr
    # The uv download was attempted (default = managed) ...
    assert "astral-sh/uv/releases/download" in (markers / "curl").read_text()
    # ... failed (recording curl exits 22), and the run degraded to the
    # system interpreter instead of dying at the python gate.
    assert "could not provision a managed Python (network?)" in combined
    assert "Python >=3.12 is required" not in combined, combined
    # A transient fallback must never freeze into a pin: the fallback branch
    # writes no python-mode marker, so the next run retries managed.
    text = CLI_SH.read_text()
    assert '_write_marker python-mode system-pinned' in text
    assert '_write_marker python-mode system\n' not in text
    assert 'PY_MODE_FALLBACK' in text


def test_cli_never_executes_a_store_interpreter_directly(tmp_path: Path) -> None:
    """An interpreter already sitting in the store must NOT be executed or
    reused by a direct scan: the store lives on an agent-writable disk, so a
    planted executable there would run inside the user's own unattended
    update. Interpreters are only ever resolved through the pinned,
    SHA-256-verified uv binary."""
    data_home = tmp_path / "data-home"
    data_home.mkdir()
    (data_home / "python-mode").write_text("managed\n")
    store = tmp_path / "data-home-python" / "cpython-3.12.10-linux" / "bin"
    store.mkdir(parents=True)
    executed_marker = tmp_path / "markers" / "store-python-executed"
    planted = store / "python3.12"
    planted.write_text(
        "#!/bin/sh\n"
        f'printf ran > "{executed_marker}"\n'
        'case "$*" in\n'
        "  *version_info*) exit 0 ;;\n"
        "  *--version*) echo 'Python 3.12.0' ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    planted.chmod(0o755)

    result, markers = _run_cli_with_fake_env(tmp_path)

    assert not executed_marker.exists(), (
        "cli.sh executed an interpreter found by scanning the store -- the "
        "resolve-only-through-pinned-uv decision has been lost"
    )
    # The run went for the pinned uv tarball instead.
    assert (markers / "curl").exists(), result.stderr
    assert "astral-sh/uv/releases/download" in (markers / "curl").read_text()


def test_cli_marker_driven_managed_run_falls_back_on_provision_failure(
    tmp_path: Path,
) -> None:
    """A `managed` marker records a DEFAULTED choice, not an explicit request:
    when the uv download fails on a marker-driven re-run (the shape every
    `kirocrew update` takes after the first successful default install) and a
    usable system interpreter exists, the run must degrade to it instead of
    hard-failing -- otherwise the first successful install would turn every
    later update into a hard network dependency on the uv download. Only a
    flag/env request made THIS run fails hard (pinned by
    test_cli_managed_python_flag_skips_system_interpreters)."""
    data_home = tmp_path / "data-home"
    data_home.mkdir()
    (data_home / "python-mode").write_text("managed\n")

    result, markers = _run_cli_with_fake_env(
        tmp_path,
        interpreters={"python3.12": _USABLE_PY312_STUB},
    )

    combined = result.stdout + result.stderr
    # The uv download was attempted (marker kept the managed choice) ...
    assert "astral-sh/uv/releases/download" in (markers / "curl").read_text()
    # ... failed, and the run degraded instead of dying at the python gate.
    assert "could not provision a managed Python (network?)" in combined
    assert "Python >=3.12 is required" not in combined, combined


def test_cli_uv_download_honors_a_mirror_base(tmp_path: Path) -> None:
    """KIROCREW_UV_URL points air-gapped / proxied hosts at a mirror of the uv
    release tree. The URL must be built from it -- and the SHA-256 pin still
    applies, so a mirror can serve the bytes but never substitute them (pinned
    by test_cli_rejects_a_tampered_uv_download)."""
    result, markers = _run_cli_with_fake_env(
        tmp_path,
        extra_env={"KIROCREW_UV_URL": "https://mirror.example.com/uv/"},
    )

    recorded = (markers / "curl").read_text()
    assert "https://mirror.example.com/uv/" in recorded, result.stderr
    assert "github.com/astral-sh" not in recorded
    assert ".tar.gz" in recorded


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
    an attacker file spoofing 'system-pinned' to freeze the install off the
    managed default) must be ignored -- the run proceeds on the managed
    default as if no marker existed."""
    data_home = tmp_path / "data-home"
    data_home.mkdir()
    spoof = tmp_path / "spoof"
    spoof.write_text("system-pinned\n")
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
    assert "Reusing the recorded system-python choice" not in combined
    # The spoofed pin was ignored: the run went for the managed default.
    assert (markers / "curl").exists(), result.stderr
    assert "astral-sh/uv" in (markers / "curl").read_text()
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


# ── Progress output ──────────────────────────────────────────────────────────
# The slow steps (wheel download, pip, venv creation) run through `_run_step`,
# which must keep a non-interactive log readable (one line per step plus a
# heartbeat, never a spinner), capture the command's output to the log the
# failure reporter reads, and return the command's own exit status so the
# `if !` guards around it still see a failure.


def _progress_helpers() -> str:
    """The helper block of cli.sh, extracted so it can be sourced alone."""
    text = CLI_SH.read_text()
    start = text.index("# ── Progress output")
    end = text.index("# Dependencies come from prebuilt wheels only.")
    return text[start:end]


def test_run_step_off_a_terminal_prints_done_line_and_passes_exit_status(
    tmp_path: Path,
) -> None:
    helpers = tmp_path / "helpers.sh"
    helpers.write_text(_progress_helpers())
    log = tmp_path / "step.log"
    script = (
        f'set -eu; TMP="{tmp_path}"; . "{helpers}"; '
        f'echo "tty=$_tty curl=$CURL_PROGRESS"; '
        f"_run_step \"{log}\" \"Installing things\" sh -c 'echo Collecting aiohttp; echo oops >&2; exit 3' "
        f'|| echo "rc=$?"'
    )
    # Bytes, not text=True: universal newlines would fold a leaked \r into \n
    # and the "no spinner off a terminal" assertion below could never fail.
    result = subprocess.run(
        ["sh", "-c", script],
        capture_output=True,
        cwd=tmp_path,
        env={**os.environ, "TERM": "xterm"},
    )
    assert result.returncode == 0, result.stderr
    assert b"\r" not in result.stdout, "spinner redraws leaked into a non-terminal run"
    stdout = result.stdout.decode()
    stderr = result.stderr.decode()
    # stdout is a pipe here, so the terminal path must be off regardless of TERM.
    assert "tty=0 curl=-s" in stdout
    assert "rc=3" in stdout, "the wrapper did not return the command's exit status"
    assert "Installing things ... FAILED after" in stderr
    # Both streams of the wrapped command land in the log the failure report reads.
    assert log.read_text() == "Collecting aiohttp\noops\n"


def test_run_step_reports_success_with_elapsed_time(tmp_path: Path) -> None:
    helpers = tmp_path / "helpers.sh"
    helpers.write_text(_progress_helpers())
    script = (
        f'set -eu; TMP="{tmp_path}"; . "{helpers}"; '
        f'_run_step "{tmp_path}/ok.log" "Creating virtual environment" true'
    )
    result = subprocess.run(
        ["sh", "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert re.search(r"Creating virtual environment \.\.\. done \(\d+s\)", result.stdout)


def test_run_step_interrupt_terminates_the_child_and_fails_the_step(
    tmp_path: Path,
) -> None:
    """Ctrl-C during a step must stop the command, not just the spinner.

    The command runs asynchronously, and POSIX starts an async child of a
    non-interactive shell with SIGINT ignored, so the shell's INT trap is the
    only thing that can reach it. The step must end with the child's signal
    status (so the caller's `if !` restore path runs, the same status a
    foreground pip returns for the keypress), the child must be gone, and
    the caller's own traps must be back in place afterwards.
    """
    helpers = tmp_path / "helpers.sh"
    helpers.write_text(_progress_helpers())
    grandchild_pid = tmp_path / "grandchild.pid"
    script = (
        f'set -eu; TMP="{tmp_path}"; trap \'echo "outer trap"\' INT; . "{helpers}"; '
        # The child ignores INT itself (as pip would, inherited), so only the
        # helper's TERM can end it, and it forks a grandchild the way
        # `python -m venv` forks ensurepip. 30 s is the test's failure mode.
        f'_run_step "{tmp_path}/slow.log" "Installing" '
        f"sh -c 'trap \"\" INT; sleep 30 & echo $! > \"{grandchild_pid}\"; wait' "
        f'|| echo "rc=$?"; '
        f'if kill -0 "$_rs_pid" 2>/dev/null; then echo child-alive; else echo child-dead; fi; '
        f"trap"
    )
    # Its own session, so a timed-out run can be reaped as a group in
    # `finally` instead of leaving the sleeps behind.
    proc = subprocess.Popen(
        ["sh", "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
        start_new_session=True,
    )
    try:
        time.sleep(1.0)  # let the step start; the child is 30 s of sleep
        proc.send_signal(signal.SIGINT)
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            raise AssertionError("SIGINT did not end the step; the child kept running")
        # Measured before the cleanup below, which would otherwise take the
        # grandchild down itself and hide a survivor.
        gc_pid = int(grandchild_pid.read_text().strip())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _pid_alive(gc_pid):
            time.sleep(0.1)
        grandchild_survived = _pid_alive(gc_pid)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=10)
    assert proc.returncode == 0, out
    assert "Installing ... interrupted (SIGINT)" in out
    assert "rc=143" in out, out
    assert "child-dead" in out, out
    # The whole process group went, not just the direct child: the sleep the
    # child forked must be gone too, or a surviving ensurepip would keep
    # writing into the tree the caller then restores over.
    assert not grandchild_survived, "the step's grandchild survived the interrupt"
    assert "outer trap" in out and "INT" in out.split("child-dead", 1)[1], (
        "the caller's INT trap was not restored after the step"
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # Zombies answer kill -0; read the state so a dead-but-unreaped sleep
    # does not count as a survivor.
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return True
    return state != "Z"


def test_tolerate_swallows_a_failure_but_not_an_interrupt(tmp_path: Path) -> None:
    helpers = tmp_path / "helpers.sh"
    helpers.write_text(_progress_helpers())
    script = (
        f'set -eu; TMP="{tmp_path}"; . "{helpers}"; '
        f'_rs_optional=1; _run_step "{tmp_path}/opt.log" "Updating pip" false || _tolerate $?; '
        f'echo "after-failure"; _tolerate 143; echo "after-interrupt"'
    )
    result = subprocess.run(
        ["sh", "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
    )
    assert result.returncode == 143, result.stderr
    assert "after-failure" in result.stdout
    assert "after-interrupt" not in result.stdout
    # A tolerated step never says FAILED on an install that goes on to succeed.
    assert "FAILED" not in result.stderr
    assert "Updating pip ... skipped (exit 1" in result.stderr


def test_tolerate_restores_the_moved_aside_venv_on_an_interrupt(tmp_path: Path) -> None:
    """Ctrl-C during the tolerated pip upgrade must not strand the old install.

    By then the working venv has been moved to the backup path and the new
    one has no kirocrew yet. ``_tolerate`` exits, and the rollback the rebuild
    arms on EXIT (``_venv_rollback_on_exit``, armed here as cli.sh arms it)
    puts the old venv back.
    """
    helpers = tmp_path / "helpers.sh"
    venv = tmp_path / "venv"
    backup = tmp_path / "venv.pre-rebuild.1"
    venv.mkdir()
    (venv / "new").write_text("half-built\n")
    backup.mkdir()
    (backup / "bin").mkdir()
    (backup / "bin" / "kirocrew").write_text("old launcher\n")
    restore = CLI_SH.read_text()
    start = restore.index("_restore_tree() {")
    end = restore.index("\n}\n", start) + 3
    helpers.write_text(_progress_helpers() + restore[start:end])
    script = (
        f'set -eu; TMP="{tmp_path / "tmp"}"; . "{helpers}"; '
        f'VENV="{venv}"; _VENV_BACKUP="{backup}"; _VENV_MOVED=1; '
        f"trap '_venv_rollback_on_exit' EXIT; "
        f'_tolerate 130; echo "after-interrupt"'
    )
    result = subprocess.run(
        ["sh", "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
    )
    assert result.returncode == 130, result.stderr
    assert "after-interrupt" not in result.stdout
    assert "previous install was restored" in result.stderr
    assert (venv / "bin" / "kirocrew").read_text() == "old launcher\n"
    assert not (venv / "new").exists()
    assert not backup.exists()


def test_cli_downloads_use_the_progress_flag_and_pip_is_wrapped() -> None:
    text = CLI_SH.read_text()
    # Wheel and uv downloads: silent in a log (the flag resolves to -s), a
    # progress bar on a terminal. Never both -s and --progress-bar at once.
    assert re.search(r'curl -f \$CURL_PROGRESS -S --proto \'=https\' "\$WHEEL_URL"', text)
    assert re.search(r"curl -f \$CURL_PROGRESS -S -L --proto '=https'", text)
    assert 'CURL_PROGRESS="-s"' in text and 'CURL_PROGRESS="--progress-bar"' in text
    # Every pip/pipx install goes through the wrapper and keeps writing the
    # log _report_pip_failure reads.
    assert re.search(
        r'_run_step "\$TMP/pip-install\.log" "[^"]*" \\\n\s+pipx install --force', text
    )
    assert re.search(
        r'_run_step "\$TMP/pip-install\.log" "[^"]*" \\\n\s+"\$VENV/bin/pip" install --progress-bar off',
        text,
    )
    assert "pip\" install --quiet $PIP_BINARY_ONLY" not in text, (
        "the wheel install went back to --quiet; the progress line and the "
        "failure report both need pip's Collecting/Downloading lines"
    )


def test_run_step_is_portable_to_dash() -> None:
    """The one-line install runs under /bin/sh, which is dash on Debian.

    dash prints nothing for `trap` inside a command substitution, so the
    caller's traps have to be saved through a file, and its `kill` accepts a
    process group only as `kill -s SIG -- -PGID`. Job control cannot be
    enabled off a tty there, so the group comes from setsid(1) when present.
    """
    helpers = _progress_helpers()
    code = "\n".join(
        line for line in helpers.splitlines() if not line.lstrip().startswith("#")
    )
    assert "$(trap)" not in code
    assert re.search(r'trap > "\$_rs_traps"', code)
    assert re.search(r'\. "\$_rs_traps"', code)
    assert 'kill -s TERM -- "-$_rs_pid"' in code
    assert not re.search(r"kill -TERM -- ", code)
    assert re.search(r"command -v setsid .*\n\s+setsid \"\$@\"", code)


def test_run_step_redrawn_line_fits_the_terminal_width(tmp_path: Path) -> None:
    """Every `\\r`-redrawn frame must be shorter than the terminal.

    A frame that wraps is not overwritten by the next `\\r`: it leaves a new
    row per redraw and the "one live line" turns into a wall of spinner
    lines. `tput cols` is what the helper measures, so drive it with a fake
    tput and a pty-free stand-in for the terminal test (`_tty=1` forced).
    """
    helpers = tmp_path / "helpers.sh"
    helpers.write_text(_progress_helpers())
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "tput").write_text("#!/bin/sh\necho 72\n")
    (fake_bin / "tput").chmod(0o755)
    long_line = "Downloading " + "x" * 200 + ".whl (310 kB)"
    script = (
        f'set -eu; TMP="{tmp_path}"; . "{helpers}"; _tty=1; '
        f'_run_step "{tmp_path}/wide.log" "Installing kirocrew 1.2.3 and its dependencies" '
        f"sh -c 'echo \"{long_line}\"; sleep 1'"
    )
    # Bytes, not text=True: universal newlines would fold the \r redraws into \n.
    result = subprocess.run(
        ["sh", "-c", script],
        capture_output=True,
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "TERM": "xterm"},
    )
    assert result.returncode == 0, result.stderr
    frames = re.findall(r"\r\x1b\[K([^\r\n]*)(?=\r)", result.stdout.decode())
    assert frames, "no redrawn frames on the forced-terminal path"
    tails = [f for f in frames if "Downloading" in f]
    assert tails, "the command's last line never made it onto the progress line"
    assert max(len(f) for f in frames) < 72, max(frames, key=len)


def test_run_step_prefix_is_cut_on_a_terminal_narrower_than_the_message(
    tmp_path: Path,
) -> None:
    """On a 40-column terminal even the spinner + message + elapsed prefix is
    wider than the row; it must be truncated, not left to wrap."""
    helpers = tmp_path / "helpers.sh"
    helpers.write_text(_progress_helpers())
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "tput").write_text("#!/bin/sh\necho 40\n")
    (fake_bin / "tput").chmod(0o755)
    script = (
        f'set -eu; TMP="{tmp_path}"; . "{helpers}"; _tty=1; '
        f'_run_step "{tmp_path}/narrow.log" "Installing kirocrew 1.2.3 and its dependencies with pipx" '
        f"sh -c 'echo Collecting-something-long-enough-to-matter; sleep 1'"
    )
    result = subprocess.run(
        ["sh", "-c", script],
        capture_output=True,
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "TERM": "xterm"},
    )
    assert result.returncode == 0, result.stderr
    frames = re.findall(r"\r\x1b\[K([^\r\n]*)(?=\r)", result.stdout.decode())
    assert frames, "no redrawn frames on the forced-terminal path"
    assert max(len(f) for f in frames) < 40, max(frames, key=len)
    assert all(f.startswith(("|", "/", "-", "\\")) for f in frames), frames


def _cli_sh_function(name: str) -> str:
    """The text of one top-level function in cli.sh, as cli.sh defines it."""
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", CLI_SH.read_text(), re.M | re.S)
    assert match is not None, f"cli.sh no longer defines {name}()"
    return match.group(0)


def _cli_sh_move_aside_block() -> str:
    """cli.sh's venv move-aside, from arming the rollback to the rename."""
    text = CLI_SH.read_text()
    start = text.index('  _VENV_BACKUP=""\n  _VENV_MOVED=0\n')
    end = text.index("  # EVERY failure after the move-aside", start)
    return text[start:end]


def test_cli_arms_the_venv_rollback_around_the_whole_rebuild() -> None:
    """Armed before the move-aside, gated on the rename, disarmed at the commit."""
    text = CLI_SH.read_text()
    block = _cli_sh_move_aside_block()
    for arm in (
        "trap '_venv_rollback_on_exit' EXIT",
        "trap 'exit 130' INT",
        "trap 'exit 143' TERM",
        "trap 'exit 129' HUP",
    ):
        assert block.index(arm) < block.index('mv "$VENV" "$_VENV_BACKUP"'), arm
    # The flag is raised after a FREE backup path is picked and before the
    # rename, and dropped again if the rename fails: a TERM whose trap runs
    # the instant `mv` returns must still restore, while a stop before the
    # rename finds no backup directory and restores nothing.
    assert block.index('while [ -e "$_VENV_BACKUP" ]') < block.index("    _VENV_MOVED=1\n")
    assert re.search(
        r'_VENV_MOVED=1\n\s*if ! mv "\$VENV" "\$_VENV_BACKUP" 2>/dev/null; then\n\s*_VENV_MOVED=0',
        block,
    )
    # Disarmed after the venv branch's wheel install, before the backup is
    # deleted (a restore during that delete would replace the finished venv).
    pip_step = text.index('"$VENV/bin/pip" install --progress-bar off', text.index(block))
    disarm = text.index("  _VENV_MOVED=0\n  trap 'rm -rf \"$TMP\"' EXIT INT TERM", pip_step)
    commit = text.index('rm -rf "$_VENV_BACKUP" 2>/dev/null || true', pip_step)
    assert pip_step < disarm < commit


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell signals")
def test_a_term_right_after_the_move_aside_still_restores_the_venv(tmp_path: Path) -> None:
    """A TERM whose trap runs the instant the move-aside `mv` returns restores.

    A PATH stub for ``mv`` does the real rename, then TERMs the installer
    shell before returning, so the trap runs before the next statement.
    """
    venv_dir = tmp_path / "crew-venv"
    venv_dir.mkdir()
    (venv_dir / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (venv_dir / "marker").write_text("previous working install")
    stubs = tmp_path / "bin"
    stubs.mkdir()
    real_mv = shutil.which("mv")
    assert real_mv is not None
    mv_stub = stubs / "mv"
    mv_stub.write_text(
        "#!/bin/sh\n"
        f'"{real_mv}" "$@" || exit $?\n'
        'case "$2" in *.pre-rebuild.*) kill -TERM "$PPID";; esac\n'
        "exit 0\n"
    )
    mv_stub.chmod(0o755)
    script = "\n".join(
        [
            "set -eu",
            f'TMP="{tmp_path / "tmp"}"; mkdir -p "$TMP"',
            f'VENV="{venv_dir}"',
            _cli_sh_function("_restore_tree"),
            _cli_sh_function("_venv_rollback_on_exit"),
            _cli_sh_move_aside_block(),
            "echo after-move-aside",
            "exit 9",
        ]
    )
    env = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}"}
    result = subprocess.run(
        ["sh", "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 143, result.stderr
    assert "after-move-aside" not in result.stdout
    assert (venv_dir / "marker").read_text() == "previous working install"
    assert not list(tmp_path.glob("crew-venv.pre-rebuild.*"))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell signals")
@pytest.mark.parametrize(
    ("where", "sig_name", "status"),
    [
        # Names, not signal numbers: SIGHUP does not exist on Windows, and the
        # parameters are built at collection time there too.
        ("between steps", "SIGTERM", 143),
        ("inside a step", "SIGTERM", 143),
        ("between steps", "SIGHUP", 129),
    ],
)
def test_cli_restores_the_venv_when_stopped_mid_rebuild(
    tmp_path: Path, where: str, sig_name: str, status: int
) -> None:
    """A stop anywhere in the rebuild puts the moved-aside venv back.

    Runs cli.sh's own rollback, step runner and move-aside block, and signals
    the installer's process GROUP, the way the gateway stops it. Inside a step
    the step runner's trap stops the setsid'd child; between steps only the
    rebuild's own traps stand between the signal and an exit past every
    restore.
    """
    venv_dir = tmp_path / "crew-venv"
    venv_dir.mkdir()
    (venv_dir / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (venv_dir / "marker").write_text("previous working install")
    phase = tmp_path / "phase"
    if where == "inside a step":
        step = f"echo $$ > {tmp_path / 'steppid'}; exec sleep 30"
        gap = (
            f'echo ready > "{phase}"; '
            f"_run_step \"$TMP/step.log\" \"Step\" sh -c '{step}' || exit $?"
        )
    else:
        gap = f'echo ready > "{phase}"; sleep 30 & wait $!'
    script = "\n".join(
        [
            "set -eu",
            f'TMP="{tmp_path / "tmp"}"; mkdir -p "$TMP"',
            "trap 'rm -rf \"$TMP\"' EXIT INT TERM",
            f'VENV="{venv_dir}"',
            "_tty=0; _rs_optional=0",
            _cli_sh_function("_restore_tree"),
            _cli_sh_function("_venv_rollback_on_exit"),
            _cli_sh_function("_rs_interrupt"),
            _cli_sh_function("_run_step"),
            "if true; then",
            _cli_sh_move_aside_block(),
            "fi",
            'mkdir "$VENV"',
            gap,
            "exit 9",
        ]
    )
    proc = subprocess.Popen(["sh", "-c", script], cwd=tmp_path, start_new_session=True)
    try:
        deadline = time.monotonic() + 10
        while not phase.exists():
            assert time.monotonic() < deadline, "the stand-in never reached its gap"
            time.sleep(0.02)
        time.sleep(0.2)
        os.killpg(proc.pid, getattr(signal, sig_name))
        assert proc.wait(timeout=15) == status
    finally:
        # Only while the shell is unreaped: once it has exited, its stopped
        # step went with it and both group numbers are free for another
        # worker's session to take. The step runs under setsid, in a group of
        # its own, so reap that too.
        if proc.returncode is None:
            groups = [proc.pid]
            steppid = tmp_path / "steppid"
            if steppid.is_file():
                groups.append(int(steppid.read_text().strip()))
            for pgid in groups:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            proc.wait(timeout=10)

    assert (venv_dir / "marker").read_text() == "previous working install"
    assert not list(tmp_path.glob("crew-venv.pre-rebuild.*"))


def _cli_sh_rebuild_steps() -> str:
    """cli.sh's venv rebuild steps and their failure branches, up to the commit."""
    text = CLI_SH.read_text()
    start = text.index('  if ! _run_step "$TMP/venv-create.log"')
    end = text.index("  # Committed: the wheel landed", start)
    return text[start:end]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell signals")
@pytest.mark.parametrize("failing", ["venv create", "wheel install"])
def test_a_stop_during_a_failure_branch_restore_keeps_the_venv(tmp_path: Path, failing: str) -> None:
    """A TERM that lands inside a failed step's restore must not strand the venv.

    The failure branch deletes the half-built venv and renames the backup
    back. A TERM between the two (the gateway's group TERM after a step it
    stopped) must not cut that short: the restore runs with INT, TERM and HUP
    ignored. The delete is slowed by a PATH stub for ``rm`` so the signal lands inside
    it deterministically.
    """
    venv_dir = tmp_path / "crew-venv"
    venv_dir.mkdir()
    (venv_dir / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (venv_dir / "marker").write_text("previous working install")
    phase = tmp_path / "phase"
    stubs = tmp_path / "bin"
    stubs.mkdir()
    real_rm = shutil.which("rm")
    assert real_rm is not None
    rm_stub = stubs / "rm"
    rm_stub.write_text(
        "#!/bin/sh\n"
        'for a; do last="$a"; done\n'
        f'case "$last" in "{venv_dir}"|"{venv_dir}".failed.*) '
        f'echo in-restore > "{phase}"; sleep 1;; esac\n'
        f'exec "{real_rm}" "$@"\n'
    )
    rm_stub.chmod(0o755)
    # Stands in for `python -m venv "$VENV"`: leaves a half-built tree whose
    # pip always fails, and fails itself on the venv-create parametrization.
    py_stub = tmp_path / "python"
    py_stub.write_text(
        "#!/bin/sh\n"
        'mkdir -p "$3/bin"; echo half > "$3/half"\n'
        "printf '#!/bin/sh\\nexit 4\\n' > \"$3/bin/pip\"; chmod +x \"$3/bin/pip\"\n"
        f"exit {3 if failing == 'venv create' else 0}\n"
    )
    py_stub.chmod(0o755)
    err_line = re.search(r"^err\(\) \{.*\}$", CLI_SH.read_text(), re.M)
    assert err_line is not None
    script = "\n".join(
        [
            "set -eu",
            f'TMP="{tmp_path / "tmp"}"; mkdir -p "$TMP"',
            "trap 'rm -rf \"$TMP\"' EXIT INT TERM",
            f'VENV="{venv_dir}"; PY="{py_stub}"; VER=0; WHL=wheel; PIP_BINARY_ONLY=""',
            "_tty=0; _rs_optional=0",
            err_line.group(0),
            _cli_sh_function("_restore_tree"),
            _cli_sh_function("_venv_rollback_on_exit"),
            _cli_sh_function("_venv_restore_after_failure"),
            _cli_sh_function("_tolerate"),
            _cli_sh_function("_report_pip_failure"),
            _cli_sh_function("_rs_interrupt"),
            _cli_sh_function("_run_step"),
            "if true; then",
            _cli_sh_move_aside_block(),
            _cli_sh_rebuild_steps(),
            "fi",
            "exit 9",
        ]
    )
    env = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}"}
    proc = subprocess.Popen(
        ["sh", "-c", script],
        cwd=tmp_path,
        env=env,
        start_new_session=True,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        deadline = time.monotonic() + 15
        while not phase.exists():
            assert proc.poll() is None, proc.stderr.read() if proc.stderr else ""
            assert time.monotonic() < deadline, "the restore never started"
            time.sleep(0.02)
        os.killpg(proc.pid, signal.SIGTERM)
        _, stderr = proc.communicate(timeout=15)
    finally:
        # Only while the shell is unreaped: once it is, its group number is
        # free for another worker's session to take.
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait(timeout=10)

    assert proc.returncode == 1, stderr
    assert "The previous install was restored and keeps working" in stderr
    assert (venv_dir / "marker").read_text() == "previous working install"
    assert not (venv_dir / "half").exists()
    assert not list(tmp_path.glob("crew-venv.pre-rebuild.*"))
    assert not list(tmp_path.glob("crew-venv.failed.*"))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell signals")
def test_a_sigkill_during_the_restore_delete_still_leaves_the_previous_venv(
    tmp_path: Path,
) -> None:
    """A SIGKILL inside ``_restore_tree``'s slow delete must not leave no venv.

    The gateway SIGKILLs the installer once its grace runs out. The restore
    renames the half-built tree aside and the backup into place BEFORE it
    deletes anything, so the kill can only cut short the delete of the
    discarded tree. The delete is slowed by a PATH stub for ``rm``.
    """
    venv_dir = tmp_path / "crew-venv"
    venv_dir.mkdir()
    (venv_dir / "half").write_text("half-built\n")
    backup = tmp_path / "crew-venv.pre-rebuild.1"
    backup.mkdir()
    (backup / "marker").write_text("previous working install")
    phase = tmp_path / "phase"
    stubs = tmp_path / "bin"
    stubs.mkdir()
    real_rm = shutil.which("rm")
    assert real_rm is not None
    rm_stub = stubs / "rm"
    rm_stub.write_text(
        "#!/bin/sh\n"
        'for a; do last="$a"; done\n'
        f'case "$last" in "{venv_dir}"|"{venv_dir}".failed.*) '
        f'echo in-delete > "{phase}"; sleep 30;; esac\n'
        f'exec "{real_rm}" "$@"\n'
    )
    rm_stub.chmod(0o755)
    script = "\n".join(
        [
            "set -eu",
            _cli_sh_function("_restore_tree"),
            f'_restore_tree "{backup}" "{venv_dir}"',
            "exit 9",
        ]
    )
    env = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}"}
    proc = subprocess.Popen(["sh", "-c", script], cwd=tmp_path, env=env, start_new_session=True)
    try:
        deadline = time.monotonic() + 15
        while not phase.exists():
            assert proc.poll() is None, "the restore ended before its delete"
            assert time.monotonic() < deadline, "the restore never reached its delete"
            time.sleep(0.02)
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=15)
    finally:
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait(timeout=10)

    assert (venv_dir / "marker").read_text() == "previous working install"
    assert not (venv_dir / "half").exists()
    assert not backup.exists()
