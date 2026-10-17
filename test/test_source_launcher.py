"""Regression tests for the POSIX source-checkout launcher."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_LAUNCHER = _REPO_ROOT / "bin" / "kirocrew"
_POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="POSIX shell launcher")


def _copy_launcher(tmp_path: Path) -> tuple[Path, Path]:
    """Copy the real wrapper into an install root whose path contains spaces."""
    install_root = tmp_path / "Kiro Crew checkout"
    launcher = install_root / "bin" / "kirocrew"
    launcher.parent.mkdir(parents=True)
    shutil.copy2(_SOURCE_LAUNCHER, launcher)
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    return install_root, launcher


@_POSIX_ONLY
def test_launcher_delegates_to_venv_without_rewriting_pythonpath(tmp_path: Path) -> None:
    """The editable venv owns imports while the wrapper preserves caller state."""
    install_root, launcher = _copy_launcher(tmp_path)
    capture_path = tmp_path / "launcher-environment.txt"
    venv_entry = install_root / ".venv" / "bin" / "kirocrew"
    venv_entry.parent.mkdir(parents=True)
    venv_entry.write_text(
        "#!/bin/sh\n"
        "{\n"
        "  printf '%s\\n' \"$KIROCREW_PROJECT_DIR\"\n"
        "  printf '%s\\n' \"${PYTHONPATH-}\"\n"
        "  printf '%s\\n' \"$@\"\n"
        '} > "$KIROCREW_LAUNCH_CAPTURE"\n',
        encoding="utf-8",
    )
    venv_entry.chmod(venv_entry.stat().st_mode | stat.S_IXUSR)

    existing_pythonpath = os.pathsep.join(("/existing/one", "/existing/two"))
    env = os.environ.copy()
    env.pop("KIROCREW_PROJECT_DIR", None)
    env.update(
        {
            "KIROCREW_LAUNCH_CAPTURE": str(capture_path),
            "PYTHONPATH": existing_pythonpath,
        }
    )

    result = subprocess.run(
        [str(launcher), "mcp-core", "--probe"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert capture_path.read_text(encoding="utf-8").splitlines() == [
        str(install_root),
        existing_pythonpath,
        "mcp-core",
        "--probe",
    ]


@_POSIX_ONLY
def test_launcher_without_venv_explains_source_install(tmp_path: Path) -> None:
    """An incomplete checkout fails with one actionable installation path."""
    install_root, launcher = _copy_launcher(tmp_path)

    result = subprocess.run(
        [str(launcher), "doctor"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=10,
    )

    assert result.returncode == 1
    assert f"Kiro Crew virtual environment not found at {install_root}/.venv" in result.stderr
    assert f'cd "{install_root}" && bash minimal_install.sh' in result.stderr
    assert "Source checkouts run from their own Python virtual environment." in result.stderr
