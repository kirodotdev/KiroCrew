"""cli.sh's umask tightening covers the managed-Python step.

On a fresh account the managed-Python step is what creates ``~/.kiro``, the
parent of ``crew-python`` and ``crew-venv``. Under the stock Ubuntu umask
``002`` that directory would be born ``0775`` without the tightening, and
``kirocrew service install`` would then refuse the AppArmor profile because an
ancestor of the launcher is group-writable. The curl stub here stands in for
the uv download and creates the interpreter store the way uv would, under
whatever umask cli.sh runs it with, then fails so the run stops before any
real install step.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from test_installer_distro import _USABLE_PY312_STUB, _run_cli_with_fake_env

pytestmark = pytest.mark.skipif(os.name == "nt", reason="cli.sh is POSIX shell (macOS + Linux)")


def _run_under_umask_002(
    tmp_path: Path, *, existing_kiro_mode: int | None = None
) -> tuple[str, Path]:
    home = tmp_path / "home"
    home.mkdir()
    home.chmod(0o755)
    kiro = home / ".kiro"
    if existing_kiro_mode is not None:
        kiro.mkdir()
        kiro.chmod(existing_kiro_mode)
    curl_stub = (
        "#!/bin/sh\n"
        'case "$*" in\n'
        f'  *astral-sh/uv/releases/download*) mkdir -p "{kiro}/crew-python" ;;\n'
        "esac\n"
        "exit 22\n"
    )
    saved = os.umask(0o002)
    try:
        result, _markers = _run_cli_with_fake_env(
            tmp_path,
            interpreters={"python3.12": _USABLE_PY312_STUB},
            curl_stub=curl_stub,
            extra_env={"KIROCREW_HOME": str(kiro / "crew")},
        )
    finally:
        os.umask(saved)
    output = result.stdout + result.stderr
    assert (kiro / "crew-python").is_dir(), output
    return output, kiro


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_managed_python_step_creates_kiro_dir_non_group_writable(tmp_path: Path) -> None:
    output, kiro = _run_under_umask_002(tmp_path)

    assert _mode(kiro) == 0o755, output
    assert _mode(kiro / "crew-python") == 0o755, output


def test_an_existing_kiro_dir_keeps_its_mode(tmp_path: Path) -> None:
    output, kiro = _run_under_umask_002(tmp_path, existing_kiro_mode=0o775)

    assert _mode(kiro) == 0o775, output
