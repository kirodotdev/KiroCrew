"""cli.sh refuses Git Bash, MSYS2 and Cygwin up front.

A Windows user may paste the README one-liner into Git Bash, where advice to
install Python and re-run could never give them a working install. The
installer names the unsupported shell and points at the Windows installer,
the Windows guide and WSL, before it touches the network.

These tests run the real ``cli.sh`` with a ``uname`` stub that reports each
shell's kernel name and a ``curl`` stub that records every call.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from installer_test_helpers import run_bounded

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_SH = REPO_ROOT / "cli.sh"

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="cli.sh is POSIX shell, run here on macOS and Linux"
)


def _run_cli_with_uname(
    tmp_path: Path, kernel: str
) -> tuple[subprocess.CompletedProcess[str], Path]:
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in (
        "sh",
        "env",
        "cat",
        "printf",
        "mktemp",
        "rm",
        "mkdir",
        "grep",
        "sed",
        "tr",
        "openssl",
    ):
        real = shutil.which(name)
        if real:
            (tools / name).symlink_to(real)
    uname = tools / "uname"
    uname.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        f"  -s) echo '{kernel}' ;;\n"
        "  -m) echo x86_64 ;;\n"
        f"  *) echo '{kernel}' ;;\n"
        "esac\n"
    )
    uname.chmod(0o755)
    curl_marker = tmp_path / "curl-calls"
    curl = tools / "curl"
    curl.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{curl_marker}"\nexit 22\n')
    curl.chmod(0o755)
    env = {
        "PATH": str(tools),
        "HOME": str(tmp_path / "home"),
        "KIROCREW_HOME": str(tmp_path / "data-home"),
    }
    proc = run_bounded(["sh", str(CLI_SH)], env)
    return proc, curl_marker


@pytest.mark.parametrize(
    "kernel", ["MINGW64_NT-10.0-26100", "MSYS_NT-10.0-26100", "CYGWIN_NT-10.0-26100"]
)
def test_windows_posix_shells_are_refused_before_any_download(tmp_path: Path, kernel: str) -> None:
    proc, curl_marker = _run_cli_with_uname(tmp_path, kernel)

    assert proc.returncode == 1, proc.stderr
    assert "does not support Git Bash, MSYS2 or Cygwin" in proc.stderr
    assert kernel in proc.stderr
    assert "KiroCrew-Setup.exe" in proc.stderr
    assert "docs/guides/windows-install.md" in proc.stderr
    assert "WSL" in proc.stderr
    # The old dead end must not be what the user reads.
    assert "Install Python" not in proc.stderr
    assert "no pinned uv build" not in proc.stderr
    assert not curl_marker.exists(), "the refusal must come before any network request"
    assert not (tmp_path / "data-home").exists()


def test_linux_is_not_refused(tmp_path: Path) -> None:
    proc, _ = _run_cli_with_uname(tmp_path, "Linux")

    assert "does not support Git Bash" not in proc.stderr
