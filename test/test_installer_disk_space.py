"""cli.sh on a nearly full disk: a plain stop before any download, and no venv left behind.

Without a free-space check, a near-full disk lets the one-line install download
the managed Python, then fail inside ``python -m venv`` with a raw ensurepip
error and leave the half-built venv on a disk fuller than before. The cases
drive the real script through the signed-manifest harness of
``test_installer_binary_only`` with a ``df`` stub placed ahead of the system
directories on the closed ``PATH``, so the free space each directory reports
is the test's choice.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from installer_test_helpers import run_bounded
from test_cli_manifest_signature import (  # noqa: F401  (fixtures are looked up by name)
    SigningKey,
    _openssl_bin,
    test_key,
)
from test_installer_binary_only import _prepare_installer

# Answers for the directory it is asked about (its last argument):
# FAKE_DF_SHORT_DIR has FAKE_DF_SHORT_KB KiB free, every other directory 10 GiB.
# FAKE_DF_BROKEN=1 is a df that cannot read the mount table.
_DF = """#!/bin/sh
[ -z "${FAKE_DF_BROKEN:-}" ] || { echo "df: cannot read table of mounted file systems" >&2; exit 1; }
for a; do dir="$a"; done
avail=10485760
[ "$dir" = "${FAKE_DF_SHORT_DIR:-}" ] && avail="$FAKE_DF_SHORT_KB"
echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
echo "/dev/fake 20971520 0 $avail 1% /"
"""

# `python -m venv` as it fails on a full disk: a partial tree, and ensurepip's
# own words swallowed, so only the CalledProcessError line is printed.
_VENV_FAILS = """#!/bin/sh
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "venv" ]; then
  mkdir -p "$3/bin"
  echo "Error: Command '['$3/bin/python3.12', '-m', 'ensurepip', '--upgrade', '--default-pip']' returned non-zero exit status 1." >&2
  exit 1
fi
exec "$FAKE_REAL_PYTHON" "$@"
"""

# mktemp as macOS ships it: a bare `mktemp -d` creates under the per-user temp
# directory and reads TMPDIR only when the system has none; a template is used
# as given. Every case runs against it, so a Linux run fails too when cli.sh's
# scratch dir stops following TMPDIR.
_MKTEMP_AS_ON_MACOS = """#!/bin/sh
[ "$*" != "-d" ] || set -- -d "$FAKE_USER_TEMP_DIR/tmp.XXXXXXXX"
exec "$FAKE_REAL_MKTEMP" "$@"
"""

ENOSPC_LOG = (
    "Collecting numpy>=1.21,<3\n"
    "ERROR: Could not install packages due to an OSError: [Errno 28] No space left on device\n"
)


@pytest.fixture(scope="module")
def signing_key(test_key: SigningKey) -> SigningKey:  # noqa: F811 -- pytest fixture by name
    if os.name == "nt":
        pytest.skip("cli.sh is supported on macOS and Linux only")
    return test_key


def _run(
    case: Path,
    key: SigningKey,
    *,
    fail_with: str | None = None,
    venv_fails: bool = False,
    env_extra: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """One venv-branch run with the df stub; returns the result and the run root."""
    argv, env, run_root, _argv_file = _prepare_installer(
        case, key, with_pipx=False, fail_with=fail_with
    )
    stubs = run_root / "disk-stubs"
    stubs.mkdir()
    (stubs / "df").write_text(_DF, encoding="utf-8")
    (stubs / "df").chmod(0o755)
    real_mktemp = shutil.which("mktemp", path="/usr/bin:/bin")
    assert real_mktemp is not None
    (stubs / "mktemp").write_text(_MKTEMP_AS_ON_MACOS, encoding="utf-8")
    (stubs / "mktemp").chmod(0o755)
    user_temp = run_root / "user-temp"
    user_temp.mkdir()
    env.update({"FAKE_REAL_MKTEMP": real_mktemp, "FAKE_USER_TEMP_DIR": str(user_temp)})
    if venv_fails:
        for name in ("python3.13", "python3.12", "python3"):
            (stubs / name).write_text(_VENV_FAILS, encoding="utf-8")
            (stubs / name).chmod(0o755)
    tmpdir = run_root / "tmp"
    tmpdir.mkdir()
    env["PATH"] = os.pathsep.join([str(stubs), env["PATH"]])
    env["TMPDIR"] = str(tmpdir)
    if env_extra:
        env.update(env_extra)
    return run_bounded([*argv, "--no-kas"], env, cwd=str(run_root)), run_root


@pytest.mark.parametrize(
    ("short", "free", "need", "hint"),
    [("install", 300, 500, ""), ("temp", 50, 100, " (or point TMPDIR at a disk with room)")],
    ids=["install-dir", "temp-dir"],
)
def test_too_little_free_space_stops_before_the_first_download(
    tmp_path: Path, signing_key: SigningKey, short: str, free: int, need: int, hint: str
) -> None:
    """The venv's location does not exist on a first install, so its nearest
    existing ancestor (the run root here) is measured; the temp directory is the
    one mktemp created the install's scratch dir in."""
    case = tmp_path / "case"
    run_root = case / "run"
    short_dir = run_root if short == "install" else run_root / "tmp"
    result, _root = _run(
        case,
        signing_key,
        env_extra={"FAKE_DF_SHORT_DIR": str(short_dir), "FAKE_DF_SHORT_KB": str(free * 1024)},
    )

    assert result.returncode == 1, result.stderr
    assert (
        f"Kiro Crew needs about {need} MB free in {short_dir}, and that disk has {free} MB. "
        f"Free some space there{hint}, then re-run this installer. Nothing was changed."
    ) in result.stderr
    # Stopped before anything was fetched or built.
    assert "Downloading" not in result.stdout
    assert not (run_root / "data-home-venv").exists()
    assert not (run_root / "install-argv").exists()


def test_a_tmpdir_that_does_not_exist_stops_and_names_it(
    tmp_path: Path, signing_key: SigningKey
) -> None:
    """The scratch dir is made under TMPDIR, so a TMPDIR with no directory
    behind it stops the install; the message names it and says what to do."""
    case = tmp_path / "case"
    missing = case / "run" / "no-such-dir"
    result, run_root = _run(case, signing_key, env_extra={"TMPDIR": str(missing)})

    assert result.returncode == 1, result.stderr
    assert (
        f"kirocrew-install: could not create a scratch directory in {missing}, the directory "
        "TMPDIR names. Create it, point TMPDIR at a directory you can write to, or unset TMPDIR, "
        "then re-run this installer. Nothing was changed."
    ) in result.stderr
    assert "Downloading" not in result.stdout
    assert not (run_root / "data-home-venv").exists()


def test_a_df_that_cannot_answer_skips_the_check(tmp_path: Path, signing_key: SigningKey) -> None:
    result, run_root = _run(tmp_path / "case", signing_key, env_extra={"FAKE_DF_BROKEN": "1"})

    assert result.returncode == 0, result.stderr
    assert "MB free in" not in result.stderr
    assert (run_root / "data-home-venv" / "bin" / "kirocrew").exists()


@pytest.mark.parametrize(
    ("step", "message"),
    [
        ("venv create", "creating the venv at {venv} failed (disk full?)."),
        (
            "wheel install",
            "kirocrew-install: the disk filled up during the install (No space left on device). "
            "Free some space where Kiro Crew installs ({root}) and in the temp directory "
            "({root}/tmp), then re-run this installer.",
        ),
    ],
)
def test_a_first_install_that_fails_says_disk_full_and_removes_its_venv(
    tmp_path: Path, signing_key: SigningKey, step: str, message: str
) -> None:
    """Both failing steps of a first install name the full disk, and the venv
    the run created from nothing is gone afterwards, so a re-run starts clean."""
    if step == "venv create":
        result, run_root = _run(tmp_path / "case", signing_key, venv_fails=True)
    else:
        result, run_root = _run(tmp_path / "case", signing_key, fail_with=ENOSPC_LOG)

    venv = run_root / "data-home-venv"
    assert result.returncode == 1, result.stderr
    assert message.format(venv=venv, root=run_root) in result.stderr
    assert "The previous install was restored" not in result.stderr
    assert not venv.exists()


def test_a_failed_install_keeps_a_venv_directory_it_did_not_create(
    tmp_path: Path, signing_key: SigningKey
) -> None:
    """KIROCREW_VENV aimed at a directory that already exists (and is not a
    venv) is never deleted: only a path that held nothing before the run is."""
    case = tmp_path / "case"
    target = case / "run" / "existing-dir"
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("user data\n", encoding="utf-8")

    result, _root = _run(
        case, signing_key, fail_with=ENOSPC_LOG, env_extra={"KIROCREW_VENV": str(target)}
    )

    assert result.returncode == 1, result.stderr
    assert (target / "keep.txt").read_text(encoding="utf-8") == "user data\n"
