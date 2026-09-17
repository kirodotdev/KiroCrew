"""The installers resolve dependencies from prebuilt wheels only.

Left to itself, pip treats a dependency with no wheel for the host as something
to BUILD from its sdist. On an old-glibc distro the newest numpy / Pillow wheels
carry a manylinux floor the host does not meet, so pip compiled them -- and that
needs GCC >= 10 and libjpeg headers the host was never required to have. The
failure surfaces deep inside a compiler run, after ``cli.sh`` has already moved
the working venv aside, and the transactional rebuild rolls back on every retry.

``--only-binary=:all:`` changes both halves: pip resolves the newest release of
each dependency that publishes a wheel the host can run, and when no release
does, it fails BEFORE any build starts with ``No matching distribution found``,
which the installers turn into a supported-platform message. These tests drive
the real ``cli.sh`` through the signed-manifest harness with the install step
faked, so they pin the pip/pipx invocation and the failure report rather than a
paraphrase of either.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from installer_test_helpers import run_bounded
from test_cli_manifest_signature import (  # noqa: F401  (fixtures are looked up by name)
    CDN_BASE,
    WHEEL_NAME,
    SigningKey,
    _build_manifest,
    _openssl_bin,
    _openssl_on_path,
    _patched_installer,
    _stage_cdn,
    test_key,
)

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "cli.sh"
INSTALL_SH = ROOT / "install.sh"


@pytest.fixture(scope="module")
def signing_key(test_key: SigningKey) -> SigningKey:  # noqa: F811 -- pytest fixture by name
    """The manifest harness's signing key, under a name no test parameter shadows."""
    return test_key


ONLY_BINARY = "--only-binary=:all:"
OPT_IN_ENV = "KIROCREW_ALLOW_SOURCE_BUILDS"

# pip's exact wording when binary-only resolution finds no usable release, as
# printed for a dependency the wheel pulls in (captured from a real run).
NO_WHEEL_LOG = (
    "ERROR: Could not find a version that satisfies the requirement numpy>=1.21,<3 "
    "(from kirocrew) (from versions: 1.26.0, 1.26.4, 2.0.2, 2.2.6)\n"
    "ERROR: No matching distribution found for numpy>=1.21,<3\n"
)
NETWORK_LOG = (
    "WARNING: Retrying (Retry(total=0, connect=None, read=None, redirect=None, status=None)) "
    "after connection broken by 'NewConnectionError': /simple/numpy/\n"
    "ERROR: Could not install packages due to an OSError: [Errno 101] Network is unreachable\n"
)
# The same two lines when every candidate was dropped before pip built its
# list: a package whose wheels all target a newer libc or another arch reads
# exactly like an index that does not carry it (pip counts only candidates
# that survived its link filter). Under binary-only resolution this IS the
# ticket's shape for a dependency with no old-glibc wheel at all.
VERSIONS_NONE_LOG = (
    "ERROR: Could not find a version that satisfies the requirement numpy>=1.21,<3 "
    "(from kirocrew) (from versions: none)\n"
    "ERROR: No matching distribution found for numpy>=1.21,<3\n"
)

_FAKE_CURL = """#!/bin/sh
set -eu
out=""
url=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
case "$url" in
  https://fixtures.invalid/*) rel=${url#https://fixtures.invalid/} ;;
  *) echo "unexpected URL: $url" >&2; exit 9 ;;
esac
[ -n "$out" ] || exit 10
cp "$FAKE_CDN_ROOT/$rel" "$out"
"""

# Records every argument (one per line) and, when FAKE_PIP_FAIL_WITH names a
# file, replays that file as pip's output and fails -- the shape of a real pip
# error reaching the installer's failure branch. Like real pipx: a failed
# install DELETES the package venv (its install path calls venv.remove_venv()
# on any exception); a successful --force install writes INTO the existing
# venv (`--force-reinstall`, no --clear), so whatever else the venv held stays.
# FAKE_PIPX_STARTED names a file to create when the install begins and
# FAKE_PIPX_HOLD how long to then sleep -- a slow install a second run can be
# started against. FAKE_PIPX_WAIT_FOR names a file to wait for before the
# install proceeds and FAKE_PIPX_DONE one to create once it has finished, so
# one run's install can be ordered after another's.
_FAKE_PIPX = """#!/bin/sh
set -eu
case "$1" in
  install)
    printf '%s\\n' "$@" > "$FAKE_ARGV_FILE"
    if [ -n "${FAKE_PIPX_STARTED:-}" ]; then
      : > "$FAKE_PIPX_STARTED"
      sleep "${FAKE_PIPX_HOLD:-0}"
    fi
    if [ -n "${FAKE_PIPX_WAIT_FOR:-}" ]; then
      _i=0
      while [ ! -e "$FAKE_PIPX_WAIT_FOR" ]; do
        _i=$((_i + 1)); [ "$_i" -le 300 ] || exit 12
        sleep 0.1
      done
    fi
    if [ -n "${FAKE_PIP_FAIL_WITH:-}" ]; then
      rm -rf "$FAKE_PIPX_VENVS/kirocrew"
      if [ -n "${FAKE_PIPX_RESIDUE:-}" ]; then
        # Leave something `rm -rf` cannot remove as a non-root user: a file
        # inside a directory with no write bit.
        mkdir -p "$FAKE_PIPX_VENVS/kirocrew/stuck"
        : > "$FAKE_PIPX_VENVS/kirocrew/stuck/pin"
        chmod 555 "$FAKE_PIPX_VENVS/kirocrew/stuck"
      fi
      cat "$FAKE_PIP_FAIL_WITH" >&2; exit 1
    fi
    mkdir -p "$FAKE_PIPX_VENVS/kirocrew/bin"
    printf 'home = fresh\\n' > "$FAKE_PIPX_VENVS/kirocrew/pyvenv.cfg"
    printf 'fresh\\n' > "$FAKE_PIPX_VENVS/kirocrew/bin/kirocrew"
    if [ -n "${FAKE_PIPX_DONE:-}" ]; then : > "$FAKE_PIPX_DONE"; fi ;;
  environment)
    case "${3:-}" in
      PIPX_LOCAL_VENVS) printf '%s\\n' "$FAKE_PIPX_VENVS" ;;
      *) printf '%s\\n' "$HOME/.local/bin" ;;
    esac ;;
  *) exit 11 ;;
esac
"""

_FAKE_PIP = """#!/bin/sh
set -eu
# The best-effort `pip install --upgrade pip` refresh is not the install step.
case " $* " in
  *" --upgrade pip "*) exit 0 ;;
esac
printf '%s\\n' "$@" > "$FAKE_ARGV_FILE"
if [ -n "${FAKE_PIP_FAIL_WITH:-}" ]; then cat "$FAKE_PIP_FAIL_WITH" >&2; exit 1; fi
"""

# A `python3` that answers `-m venv DIR` by laying out a venv whose pip is the
# recorder above, and hands every other invocation (the signature and digest
# checks, the symlink helper) to the real interpreter. cli.sh's venv branch is
# otherwise unreachable without a network-facing pip.
_FAKE_PYTHON = """#!/bin/sh
set -eu
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "venv" ]; then
  mkdir -p "$3/bin"
  printf 'home = %s\\n' "$FAKE_REAL_PYTHON" > "$3/pyvenv.cfg"
  cp "$FAKE_PIP_SCRIPT" "$3/bin/pip"
  chmod 755 "$3/bin/pip"
  ln -sf "$FAKE_REAL_PYTHON" "$3/bin/python"
  ln -sf "$FAKE_REAL_PYTHON" "$3/bin/python3"
  exit 0
fi
exec "$FAKE_REAL_PYTHON" "$@"
"""


def _write_tools(root: Path, *, with_pipx: bool, cp_fails: bool = False) -> tuple[Path, Path]:
    """PATH prefix for one installer run: fake curl, optional fake pipx, and an
    interpreter ladder that either points at the real interpreter (pipx branch)
    or at the venv-faking wrapper (venv branch). ``cp_fails`` shadows ``cp`` with
    a wrapper that fails the rollback copy only (a full disk) and defers every
    other copy to the real ``cp``."""
    tools = root / "tools"
    tools.mkdir(parents=True)
    argv_file = root / "install-argv"
    (tools / "curl").write_text(_FAKE_CURL, encoding="utf-8")
    (tools / "curl").chmod(0o755)
    if with_pipx:
        (tools / "pipx").write_text(_FAKE_PIPX, encoding="utf-8")
        (tools / "pipx").chmod(0o755)
    if cp_fails:
        real_cp = shutil.which("cp", path="/bin:/usr/bin")
        assert real_cp is not None
        (tools / "cp").write_text(
            "#!/bin/sh\n"
            'for a in "$@"; do case "$a" in *.pre-rebuild.*) '
            "echo 'cp: No space left on device' >&2; exit 1 ;; esac; done\n"
            f'exec "{real_cp}" "$@"\n',
            encoding="utf-8",
        )
        (tools / "cp").chmod(0o755)
    pip_script = root / "fake-pip"
    pip_script.write_text(_FAKE_PIP, encoding="utf-8")
    pip_script.chmod(0o755)
    # Shadow EVERY candidate in cli.sh's ladder (same reasoning as the manifest
    # harness: a host shim under an empty HOME wedges instead of answering).
    for name in ("python3.13", "python3.12", "python3"):
        if with_pipx:
            (tools / name).symlink_to(sys.executable)
        else:
            (tools / name).write_text(_FAKE_PYTHON, encoding="utf-8")
            (tools / name).chmod(0o755)
    return tools, argv_file


def _prepare_installer(
    case: Path,
    key: SigningKey,
    *,
    with_pipx: bool,
    fail_with: str | None = None,
    extra_env: dict[str, str] | None = None,
    existing_pipx_venv: bool = False,
    injected: bool = False,
    runpip_added: bool = False,
    cp_fails: bool = False,
) -> tuple[list[str], dict[str, str], Path, Path]:
    """Stage one installer run without starting it: returns the argv, its
    environment, the run root (cwd) and the file the fakes record pip's argv
    in. ``_run_installer`` runs it; the concurrency test starts two."""
    if os.name == "nt":
        pytest.skip("cli.sh is supported on macOS and Linux only")
    case.mkdir(exist_ok=True)
    wheel = case / WHEEL_NAME
    wheel.write_bytes(b"verified wheel")
    manifest = _build_manifest(case, key, wheel)
    cdn = _stage_cdn(case, manifest, wheel)
    script = _patched_installer(case, key)
    run_root = case / "run"
    tools, argv_file = _write_tools(run_root, with_pipx=with_pipx, cp_fails=cp_fails)
    pipx_venvs = run_root / "pipx-venvs"
    pipx_venvs.mkdir()
    if existing_pipx_venv:
        _write_existing_pipx_venv(
            pipx_venvs / "kirocrew", injected=injected, runpip_added=runpip_added
        )
    env = os.environ.copy()
    env.pop(OPT_IN_ENV, None)
    # A closed PATH: the fakes, the openssl the harness resolved (linked in on
    # its own -- its directory may be a package-manager prefix that also holds
    # pipx), and the system directories that hold sh/awk/sed/tar. A host pipx
    # would otherwise win `command -v pipx` and turn the venv-branch cases into
    # pipx-branch runs.
    openssl = shutil.which("openssl")
    assert openssl is not None, "the manifest harness needs openssl on PATH"
    (tools / "openssl").symlink_to(openssl)
    path = os.pathsep.join([str(tools), "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    if not with_pipx and shutil.which("pipx", path=path) is not None:
        pytest.skip("a system-directory pipx shadows cli.sh's venv branch on this host")
    env.update(
        {
            "PATH": path,
            "HOME": str(run_root / "home"),
            "KIROCREW_HOME": str(run_root / "data-home"),
            "FAKE_CDN_ROOT": str(cdn),
            "FAKE_ARGV_FILE": str(argv_file),
            "FAKE_PIPX_VENVS": str(pipx_venvs),
            "FAKE_PIP_SCRIPT": str(run_root / "fake-pip"),
            "FAKE_REAL_PYTHON": sys.executable,
        }
    )
    if fail_with is not None:
        log = run_root / "pip-failure.txt"
        log.write_text(fail_with, encoding="utf-8")
        env["FAKE_PIP_FAIL_WITH"] = str(log)
    if extra_env:
        env.update(extra_env)
    return ["sh", str(script), "--cdn", CDN_BASE], env, run_root, argv_file


def _write_existing_pipx_venv(old: Path, *, injected: bool, runpip_added: bool) -> None:
    """A working install from an earlier run: pyvenv.cfg is what cli.sh keys
    on, bin/kirocrew is the launcher pipx's symlink points at, and
    pipx_metadata.json records any `pipx inject`ed packages."""
    (old / "bin").mkdir(parents=True)
    (old / "pyvenv.cfg").write_text("home = old\n", encoding="utf-8")
    (old / "bin" / "kirocrew").write_text("old\n", encoding="utf-8")
    injections = {"extra": {"package": "extra"}} if injected else {}
    (old / "pipx_metadata.json").write_text(
        json.dumps({"main_package": {"package": "kirocrew"}, "injected_packages": injections}),
        encoding="utf-8",
    )
    if injected:
        (old / "bin" / "extra").write_text("injected\n", encoding="utf-8")
    if runpip_added:
        # `pipx runpip kirocrew install <pkg>` puts files in the venv that
        # pipx_metadata.json never records.
        site = old / "lib" / "site-packages"
        site.mkdir(parents=True)
        (site / "added_by_runpip.py").write_text("ADDED = True\n", encoding="utf-8")
    # Real venvs hold symlinks (bin/python -> the interpreter); the rollback
    # copy must keep them as symlinks, not follow them.
    (old / "bin" / "python").symlink_to("kirocrew")


def _run_installer(
    case: Path,
    key: SigningKey,
    *,
    with_pipx: bool,
    fail_with: str | None = None,
    extra_env: dict[str, str] | None = None,
    existing_pipx_venv: bool = False,
    injected: bool = False,
    runpip_added: bool = False,
    cp_fails: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    argv, env, run_root, argv_file = _prepare_installer(
        case,
        key,
        with_pipx=with_pipx,
        fail_with=fail_with,
        extra_env=extra_env,
        existing_pipx_venv=existing_pipx_venv,
        injected=injected,
        runpip_added=runpip_added,
        cp_fails=cp_fails,
    )
    result = run_bounded(argv, env, cwd=str(run_root))
    recorded = argv_file.read_text(encoding="utf-8").splitlines() if argv_file.exists() else []
    return result, recorded


# ---------------------------------------------------------------------------
# The invocation: binary-only reaches pip on both install branches
# ---------------------------------------------------------------------------


def test_pipx_branch_forwards_binary_only_to_pip(tmp_path: Path, signing_key: SigningKey) -> None:
    result, argv = _run_installer(tmp_path / "case", signing_key, with_pipx=True)

    assert result.returncode == 0, result.stderr
    assert argv[0] == "install"
    # pipx does not resolve dependencies itself; --pip-args is the only way the
    # policy reaches the pip it drives, and it must be ONE argument.
    assert f"--pip-args={ONLY_BINARY}" in argv, argv
    assert argv[-1].endswith(WHEEL_NAME), argv


def test_venv_branch_passes_binary_only_before_the_wheel(
    tmp_path: Path, signing_key: SigningKey
) -> None:
    result, argv = _run_installer(tmp_path / "case", signing_key, with_pipx=False)

    assert result.returncode == 0, result.stderr
    assert argv[:2] == ["install", "--quiet"], argv
    assert argv[-2] == ONLY_BINARY, argv
    assert argv[-1].endswith(WHEEL_NAME), argv
    # `$PIP_BINARY_ONLY` is expanded unquoted so that an empty value vanishes;
    # the non-empty value must still arrive as exactly one word.
    assert argv.count(ONLY_BINARY) == 1


@pytest.mark.parametrize("with_pipx", [True, False], ids=["pipx", "venv"])
def test_the_opt_in_restores_the_compile_fallback(
    tmp_path: Path, signing_key: SigningKey, with_pipx: bool
) -> None:
    """KIROCREW_ALLOW_SOURCE_BUILDS=1 removes the flag and adds nothing in its
    place: no empty `--pip-args=` for pipx, no empty word for pip."""
    result, argv = _run_installer(
        tmp_path / "case", signing_key, with_pipx=with_pipx, extra_env={OPT_IN_ENV: "1"}
    )

    assert result.returncode == 0, result.stderr
    assert not any(ONLY_BINARY in word for word in argv), argv
    assert not any(word.startswith("--pip-args") for word in argv), argv
    assert "" not in argv, argv
    assert argv[-1].endswith(WHEEL_NAME), argv


@pytest.mark.parametrize("value", ["0", "", "true", "yes"])
def test_only_the_literal_one_opts_in(tmp_path: Path, signing_key: SigningKey, value: str) -> None:
    result, argv = _run_installer(
        tmp_path / "case", signing_key, with_pipx=True, extra_env={OPT_IN_ENV: value}
    )

    assert result.returncode == 0, result.stderr
    assert f"--pip-args={ONLY_BINARY}" in argv, argv


# ---------------------------------------------------------------------------
# The failure report: a missing wheel names the platform and the way out
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("with_pipx", [True, False], ids=["pipx", "venv"])
@pytest.mark.parametrize(
    "log", [NO_WHEEL_LOG, VERSIONS_NONE_LOG], ids=["versions-listed", "versions-none"]
)
def test_no_wheel_failure_names_platform_packages_and_remedy(
    tmp_path: Path, signing_key: SigningKey, with_pipx: bool, log: str
) -> None:
    """Both shapes of pip's refusal get the report: a numeric list (only versions
    outside the required range have a wheel here) and ``none`` (every wheel was
    dropped before the list was built -- the pure no-wheel host, or a missing
    index, which pip's text cannot tell apart)."""
    result, _argv = _run_installer(
        tmp_path / "case", signing_key, with_pipx=with_pipx, fail_with=log
    )

    assert result.returncode == 1
    err = result.stderr
    # pip's own words are replayed first, so nothing the user could act on is hidden.
    assert "No matching distribution found for numpy>=1.21,<3" in err
    # The report names the host, the packages and both remedies.
    assert "pip found no prebuilt wheel it may install on this platform" in err
    # The same `uname` the installer's child shell runs, so an arch-translated
    # shell (Rosetta) cannot make the expectation disagree with the report.
    uname = subprocess.run(
        ["uname", "-s", "-m"], capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout.strip()
    assert uname in err, (uname, err)
    assert "for: numpy>=1.21,<3" in err
    assert "never compiles a dependency" in err
    # The usual cause is named as usual, not as a verdict, and the other cause
    # (index unreachable or missing the release) is named next to it.
    assert "Usually this means the host is older than the wheels' floor" in err
    assert "package index could not be reached or does not carry these releases" in err
    assert "not a supported platform" not in err
    assert f"{OPT_IN_ENV}=1" in err
    # And still ends on the branch's own failure line, so the exit path is unchanged.
    assert "kirocrew-install: installing the wheel" in err
    assert "Installed kirocrew" not in result.stdout


@pytest.mark.parametrize("with_pipx", [True, False], ids=["pipx", "venv"])
def test_other_pip_failures_keep_the_generic_message(
    tmp_path: Path, signing_key: SigningKey, with_pipx: bool
) -> None:
    """A failure without pip's "No matching distribution" verdict (the connection
    died before resolution finished) is not the no-wheel shape: the tail is
    replayed, the platform paragraph stays out, and the run still fails."""
    result, _argv = _run_installer(
        tmp_path / "case", signing_key, with_pipx=with_pipx, fail_with=NETWORK_LOG
    )

    assert result.returncode == 1
    assert "Network is unreachable" in result.stderr
    assert "no prebuilt wheel" not in result.stderr
    assert "wheels' floor" not in result.stderr
    assert "kirocrew-install: installing the wheel" in result.stderr


def test_the_no_wheel_report_is_silent_when_compiling_was_opted_in(
    tmp_path: Path, signing_key: SigningKey
) -> None:
    """Under the opt-in the same pip text does not mean "no wheel": pip builds
    an sdist there, so a `No matching distribution` is a real resolution
    failure and the platform paragraph would mislead."""
    result, _argv = _run_installer(
        tmp_path / "case",
        signing_key,
        with_pipx=True,
        fail_with=NO_WHEEL_LOG,
        extra_env={OPT_IN_ENV: "1"},
    )

    assert result.returncode == 1
    assert "No matching distribution found for numpy>=1.21,<3" in result.stderr
    assert "no prebuilt wheel" not in result.stderr
    assert "wheels' floor" not in result.stderr


def test_venv_failure_still_restores_the_previous_install(
    tmp_path: Path, signing_key: SigningKey
) -> None:
    """The new capture-and-report path sits inside the transactional rebuild:
    a binary-only refusal must leave the pre-rebuild venv back in place, exactly
    as any other pip failure does."""
    case = tmp_path / "case"
    data_home = case / "run" / "data-home"
    venv = case / "run" / "data-home-venv"
    venv.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /previous\n", encoding="utf-8")
    (venv / "bin").mkdir()
    (venv / "bin" / "kirocrew").write_text("#!/bin/sh\necho previous\n", encoding="utf-8")
    data_home.mkdir(parents=True)

    result, _argv = _run_installer(case, signing_key, with_pipx=False, fail_with=NO_WHEEL_LOG)

    assert result.returncode == 1
    assert "The previous install was restored" in result.stderr
    assert (venv / "pyvenv.cfg").read_text(encoding="utf-8") == "home = /previous\n"
    assert not list(case.glob("run/data-home-venv.pre-rebuild.*"))


def test_pipx_failure_restores_the_previous_install(
    tmp_path: Path, signing_key: SigningKey
) -> None:
    """pipx's own install path deletes the package venv on any failure, so a
    binary-only refusal over an existing install would otherwise take the
    working `kirocrew` down with it. The installer copies the venv aside first
    and puts the copy back when pipx fails -- everything in it, including what
    pipx's metadata records (`pipx inject`) and what it does not (`pipx runpip`),
    symlinks kept as symlinks."""
    case = tmp_path / "case"
    result, _argv = _run_installer(
        case,
        signing_key,
        with_pipx=True,
        fail_with=NO_WHEEL_LOG,
        existing_pipx_venv=True,
        injected=True,
        runpip_added=True,
    )

    venv = case / "run" / "pipx-venvs" / "kirocrew"
    assert result.returncode == 1
    assert "The previous install was restored" in result.stderr
    assert (venv / "pyvenv.cfg").read_text(encoding="utf-8") == "home = old\n"
    assert (venv / "bin" / "kirocrew").read_text(encoding="utf-8") == "old\n"
    assert (venv / "bin" / "extra").read_text(encoding="utf-8") == "injected\n"
    assert (venv / "lib" / "site-packages" / "added_by_runpip.py").exists()
    assert (venv / "bin" / "python").is_symlink()
    assert os.readlink(venv / "bin" / "python") == "kirocrew"
    assert not list(case.glob("run/pipx-venvs/kirocrew.pre-rebuild.*"))


def test_pipx_success_drops_the_backup(tmp_path: Path, signing_key: SigningKey) -> None:
    """The copy is a rollback, not a second install: once pipx has reinstalled
    into the venv the copy is removed and only the live tree remains."""
    case = tmp_path / "case"
    result, _argv = _run_installer(case, signing_key, with_pipx=True, existing_pipx_venv=True)

    venv = case / "run" / "pipx-venvs" / "kirocrew"
    assert result.returncode == 0, result.stderr
    assert (venv / "pyvenv.cfg").read_text(encoding="utf-8") == "home = fresh\n"
    assert not list(case.glob("run/pipx-venvs/kirocrew.pre-rebuild.*"))


def test_pipx_first_install_has_nothing_to_back_up(tmp_path: Path, signing_key: SigningKey) -> None:
    """No existing venv: no copy, no restore wording, the plain failure."""
    case = tmp_path / "case"
    result, _argv = _run_installer(case, signing_key, with_pipx=True, fail_with=NO_WHEEL_LOG)

    assert result.returncode == 1
    assert "The previous install was restored" not in result.stderr
    assert "installing the wheel with pipx failed." in result.stderr


@pytest.mark.parametrize("injected", [False, True], ids=["runpip-only", "inject+runpip"])
def test_pipx_reinstall_runs_in_place_and_keeps_what_metadata_does_not_record(
    tmp_path: Path, signing_key: SigningKey, injected: bool
) -> None:
    """The install is pipx's own in-place --force, so a successful reinstall
    keeps `pipx inject`ed packages AND packages added with `pipx runpip`, which
    pipx_metadata.json never lists -- a fresh venv would silently drop the
    latter, and a metadata-gated rebuild drops them exactly when the metadata
    is injection-free. The rollback copy is gone afterwards."""
    case = tmp_path / "case"
    result, _argv = _run_installer(
        case,
        signing_key,
        with_pipx=True,
        existing_pipx_venv=True,
        injected=injected,
        runpip_added=True,
    )

    venv = case / "run" / "pipx-venvs" / "kirocrew"
    assert result.returncode == 0, result.stderr
    assert (venv / "pyvenv.cfg").read_text(encoding="utf-8") == "home = fresh\n"
    if injected:
        assert (venv / "bin" / "extra").read_text(encoding="utf-8") == "injected\n"
    assert (venv / "lib" / "site-packages" / "added_by_runpip.py").exists()
    assert not list(case.glob("run/pipx-venvs/kirocrew.pre-rebuild.*"))


def test_pipx_failed_rollback_copy_aborts_before_pipx_runs(
    tmp_path: Path, signing_key: SigningKey
) -> None:
    """A rollback copy that cannot be made (no space) must not fall through to an
    unprotected `pipx install --force`: pipx would delete the venv on a pip
    failure with nothing to restore. The installer stops before pipx is invoked,
    says nothing was changed, and the existing install is untouched."""
    case = tmp_path / "case"
    result, argv = _run_installer(
        case,
        signing_key,
        with_pipx=True,
        existing_pipx_venv=True,
        injected=True,
        cp_fails=True,
    )

    venv = case / "run" / "pipx-venvs" / "kirocrew"
    assert result.returncode == 1
    assert "could not copy the existing install aside for rollback" in result.stderr
    assert "Nothing was changed" in result.stderr
    assert argv == [], argv  # pipx install was never invoked
    assert (venv / "pyvenv.cfg").read_text(encoding="utf-8") == "home = old\n"
    assert (venv / "bin" / "extra").read_text(encoding="utf-8") == "injected\n"
    assert not list(case.glob("run/pipx-venvs/kirocrew.pre-rebuild.*"))


def test_pipx_never_inspects_metadata_to_decide_the_rollback() -> None:
    """The rollback does not depend on what pipx_metadata.json says: any venv
    with a pyvenv.cfg is copied. A metadata-driven exception is what let
    `pipx runpip` additions fall through the cracks."""
    body = INSTALLER.read_text(encoding="utf-8")
    assert "pipx_metadata.json" not in body
    assert "injected_packages" not in body
    assert "cp -R -p" in body


def _wait_for(path: Path, *, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists():
        assert time.monotonic() < deadline, f"{path} never appeared"
        time.sleep(0.05)


def test_pipx_second_run_waits_and_never_restores_a_stale_snapshot(
    tmp_path: Path, signing_key: SigningKey
) -> None:
    """Two installer runs against one pipx venv are serialized around the whole
    copy/mutate/restore. Run A holds pipx's install open; run B starts while A
    is inside it, and B's pipx fails -- only after A's install has finished.
    Without the lock B copies the OLD venv while A is mid-update, then restores
    that stale copy over A's finished update: the successful update is silently
    reverted. With the lock B waits, copies A's result, and its rollback puts
    A's result back."""
    if os.name == "nt":
        pytest.skip("cli.sh is supported on macOS and Linux only")
    argv_a, env_a, root_a, _ = _prepare_installer(
        tmp_path / "a", signing_key, with_pipx=True, existing_pipx_venv=True, runpip_added=True
    )
    started = tmp_path / "a-started"
    a_done = tmp_path / "a-done"
    env_a["FAKE_PIPX_STARTED"] = str(started)
    env_a["FAKE_PIPX_HOLD"] = "3"
    env_a["FAKE_PIPX_DONE"] = str(a_done)
    # B has its own tools and CDN but is pointed at A's venv directory -- the
    # shape of two shells running the installer for the same user.
    argv_b, env_b, root_b, argv_file_b = _prepare_installer(
        tmp_path / "b",
        signing_key,
        with_pipx=True,
        fail_with="ERROR: No matching distribution found for numpy\n",
        extra_env={"FAKE_PIPX_VENVS": env_a["FAKE_PIPX_VENVS"], "FAKE_PIPX_WAIT_FOR": str(a_done)},
    )
    venv = Path(env_a["FAKE_PIPX_VENVS"]) / "kirocrew"
    # A runs through the same bounded runner as every other installer run
    # (own session, whole process tree killed and reaped on timeout), on a
    # worker thread so B can be started while A is inside pipx. A hang
    # surfaces as run_bounded's TimeoutExpired when the future is read.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future_a = pool.submit(run_bounded, argv_a, env_a, 30.0, str(root_a))
        try:
            _wait_for(started, seconds=20)  # A is inside pipx: the venv is being updated
            result_b = run_bounded(argv_b, env_b, cwd=str(root_b))
        finally:
            result_a = future_a.result(timeout=45)
    assert result_a.returncode == 0, result_a.stderr
    assert result_b.returncode != 0
    assert argv_file_b.exists(), "B's pipx never ran"
    assert "The previous install was restored" in result_b.stderr, result_b.stderr
    # The venv holds A's finished update, not the pre-A tree B would have
    # snapshotted without the lock.
    assert (venv / "bin" / "kirocrew").read_text(encoding="utf-8") == "fresh\n"
    assert (venv / "pyvenv.cfg").read_text(encoding="utf-8") == "home = fresh\n"
    # ... with everything A's in-place reinstall preserved.
    assert (venv / "lib" / "site-packages" / "added_by_runpip.py").exists()
    assert not list(venv.parent.glob("kirocrew.pre-rebuild.*"))
    # B did wait for A rather than racing it (A's pipx wrote "fresh"; B's fake
    # deletes the venv, so what B restored was its snapshot of "fresh").
    assert "waiting for it to finish" in result_b.stdout, result_b.stdout
    # The lock is a plain file beside the venv, left in place (pipx lists
    # directories only, so it is invisible to `pipx list`).
    assert (venv.parent / "kirocrew.install.lock").is_file()


def test_pipx_restore_never_claims_success_over_residue(
    tmp_path: Path, signing_key: SigningKey
) -> None:
    """When the failed venv cannot be removed (a read-only file left behind),
    `mv` of the backup onto the surviving directory would NEST the backup
    inside it and exit 0 -- a "restored and keeps working" message over a
    broken install. The restore must check the path is gone first, report
    that it could not restore, and leave the backup copy intact where it is."""
    if os.name == "nt":
        pytest.skip("cli.sh is supported on macOS and Linux only")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root removes the residue, so the failure cannot be staged")
    case = tmp_path / "residue"
    stuck = case / "run" / "pipx-venvs" / "kirocrew" / "stuck"
    try:
        result, argv = _run_installer(
            case,
            signing_key,
            with_pipx=True,
            fail_with="ERROR: No matching distribution found for numpy\n",
            existing_pipx_venv=True,
            injected=True,
            extra_env={"FAKE_PIPX_RESIDUE": "1"},
        )
        assert result.returncode != 0
        assert argv, "pipx install was never invoked"
        assert "could not be restored" in result.stderr, result.stderr
        assert "The previous install was restored" not in result.stderr, result.stderr
        assert "left intact at" in result.stderr, result.stderr
        venv = case / "run" / "pipx-venvs" / "kirocrew"
        # The residue is still the only thing at the venv path -- nothing nested.
        assert (venv / "stuck" / "pin").exists()
        assert not list(
            venv.glob("kirocrew.pre-rebuild.*")
        ), "the backup was nested inside the residue"
        # The working copy is where the message says it is, whole.
        backups = list(venv.parent.glob("kirocrew.pre-rebuild.*"))
        assert len(backups) == 1, backups
        assert (backups[0] / "bin" / "kirocrew").read_text(encoding="utf-8") == "old\n"
        assert (backups[0] / "bin" / "extra").read_text(encoding="utf-8") == "injected\n"
        assert str(backups[0]) in result.stderr
    finally:
        if stuck.exists():
            stuck.chmod(0o755)  # let tmp_path be cleaned up


def test_restore_checks_the_path_is_gone_before_moving() -> None:
    """Every restore site uses the helper; no bare `rm -rf ... || true` followed
    by `mv backup target` remains, since that pair is what nests on residue."""
    body = INSTALLER.read_text(encoding="utf-8")
    assert body.count("_restore_tree ") == 3, body.count("_restore_tree ")
    assert 'if [ -e "$2" ] || [ -L "$2" ]; then\n    return 1' in body
    for target in ('"$_PIPX_VENV"', '"$VENV"'):
        assert f"rm -rf {target} 2>/dev/null || true\n      mv " not in body, target


def test_pipx_lock_wait_is_the_kernel_lock_not_a_directory() -> None:
    """The guarantee comes from flock(2) on an fd the shell holds open for the
    whole transaction: a crashed run leaves nothing stale, and only 'already
    locked' is waited on -- any other error fails the take outright."""
    body = INSTALLER.read_text(encoding="utf-8")
    assert "fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)" in body
    assert "except BlockingIOError:" in body
    assert 'exec 9>>"$_PIPX_LOCK"' in body
    assert "exec 9>&-" in body
    # The lock is taken BEFORE the existence check that decides whether to
    # copy, and released only after the backup is dropped.
    take = body.index("_wait_install_lock 900")
    check = body.index('[ -f "$_PIPX_VENV/pyvenv.cfg" ] && [ ! -L "$_PIPX_VENV" ]')
    release = body.index("exec 9>&-")
    drop = body.index(
        'rm -rf "$_PIPX_VENV_BACKUP" 2>/dev/null || true\n  fi\n  # The transaction is complete'
    )
    assert take < check < drop < release
    # No mkdir-style lock directory: that shape needs stale-lock reclaim.
    assert "install.lock.d" not in body and 'mkdir "$_PIPX_LOCK"' not in body


# ---------------------------------------------------------------------------
# install.sh: the same policy on the editable install's dependency resolution
# ---------------------------------------------------------------------------


def _install_sh_pip_line() -> str:
    body = INSTALL_SH.read_text(encoding="utf-8")
    lines = [ln for ln in body.splitlines() if '"$_venv/bin/pip" install' in ln and " -e " in ln]
    assert len(lines) == 1, lines
    return lines[0]


def test_install_sh_editable_install_is_binary_only() -> None:
    line = _install_sh_pip_line()
    assert "$_pip_binary_only" in line, line
    body = INSTALL_SH.read_text(encoding="utf-8")
    assert f'_pip_binary_only="{ONLY_BINARY}"' in body
    assert f'"${{{OPT_IN_ENV}:-0}}" = "1"' in body
    # The flag precedes `-e`: it is a resolution option, not part of the target.
    assert line.index("$_pip_binary_only") < line.index(" -e ")


def test_install_sh_flag_expands_to_one_word_or_nothing() -> None:
    """The variable is expanded unquoted on purpose (a quoted empty value would
    hand pip a bare "" argument); the non-empty value has no whitespace to split."""
    line = _install_sh_pip_line()
    assert re.search(r"\s\$_pip_binary_only\s", line), line
    assert shlex.split(ONLY_BINARY) == [ONLY_BINARY]


def test_install_sh_reports_a_missing_wheel_as_a_platform_verdict() -> None:
    body = INSTALL_SH.read_text(encoding="utf-8")
    report = body.split("No matching distribution found for", 1)
    assert len(report) == 2, "install.sh does not classify pip's no-wheel failure"
    tail = report[1]
    assert "pip found no prebuilt wheel it may install on this platform" in tail
    assert "uname -s" in tail and "uname -m" in tail
    assert f"{OPT_IN_ENV}=1" in tail
    # Same two causes as cli.sh, in the same order: the floor as the usual one,
    # the index as the other, and no unqualified "unsupported" verdict.
    assert "Usually this" in tail and "wheels' floor" in tail
    assert "package index" in tail and "could not be reached" in tail
    assert "not a supported platform" not in tail
    # The report is gated on the policy being ON, since under the opt-in the same
    # pip text is an ordinary resolution failure ...
    gate = body[: body.index("pip found no prebuilt wheel")].rsplit("if ", 1)[1]
    assert '-n "$_pip_binary_only"' in gate
    # ... and on nothing else: pip's "(from versions: ...)" list is built from
    # candidates that survived its link filter, so wheels for a newer libc read
    # "none" exactly like a missing index -- it cannot gate the report.
    assert "from versions" not in gate


def test_cli_sh_help_documents_the_opt_in() -> None:
    help_text = INSTALLER.read_text(encoding="utf-8").split("cat <<'EOF'", 1)[1].split("EOF", 1)[0]
    assert f"{OPT_IN_ENV}=1" in help_text
