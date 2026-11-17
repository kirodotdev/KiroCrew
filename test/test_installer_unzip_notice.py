"""``cli.sh`` says before its next steps when the Kiro CLI installer will need unzip.

The Linux Kiro CLI installer refuses a host without ``unzip``. Kiro Crew itself does
not need it, so the one-line install only warns, and only on Linux where neither
``unzip`` nor ``kiro-cli`` is on ``PATH``.

These lift ``_unzip_notice`` verbatim out of the real script and run it with a
``PATH`` of stub executables, so the host's own tools never decide the answer.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

CLI_SH = Path(__file__).resolve().parents[1] / "cli.sh"

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="POSIX shell installer; not exercised on native Windows"
)


def _function(text: str, name: str) -> str:
    start = text.index(f"{name}() {{")
    end = text.index("\n}", start) + len("\n}")
    return text[start:end]


def _notice(tmp_path: Path, os_name: str, *, stubs: tuple[str, ...] = ()) -> str:
    """Run the lifted function under ``set -eu`` with only ``stubs`` on PATH."""
    sh = shutil.which("sh")
    assert sh is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in stubs:
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)
    script = _function(CLI_SH.read_text(encoding="utf-8"), "_unzip_notice")
    result = subprocess.run(
        [sh, "-eu", "-c", script + '\n_unzip_notice "$1"\n', "sh", os_name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={"PATH": str(bin_dir)},
        timeout=FALLBACK_TIMEOUT_SECS,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    return result.stderr


def test_a_linux_host_without_unzip_or_kiro_cli_is_told_how_to_install_unzip(
    tmp_path: Path,
) -> None:
    notice = _notice(tmp_path, "Linux")

    assert "the Kiro CLI installer (https://kiro.dev/cli/) needs it" in notice
    assert "'sudo apt-get install -y unzip'" in notice
    assert "'sudo dnf install -y unzip'" in notice


@pytest.mark.parametrize(
    ("os_name", "stubs"),
    [("Linux", ("unzip",)), ("Linux", ("kiro-cli",)), ("Darwin", ())],
    ids=["unzip-installed", "kiro-cli-installed", "not-linux"],
)
def test_no_notice_where_the_kiro_cli_installer_would_not_stop_on_unzip(
    tmp_path: Path, os_name: str, stubs: tuple[str, ...]
) -> None:
    assert _notice(tmp_path, os_name, stubs=stubs) == ""


def test_the_installer_passes_the_real_os_name_before_its_next_steps() -> None:
    text = CLI_SH.read_text(encoding="utf-8")
    call = '\n_unzip_notice "$(uname -s 2>/dev/null || true)"\n'

    assert text.count(call) == 1
    assert text.index(call) < text.index('echo "Next steps:"')
