"""cli.sh names the package to install when a verifier prerequisite is missing.

The installer refuses before any download when curl, openssl or a SHA-256 tool
is absent, and a minimal image can lack any of them (Amazon Linux 2023 ships no
openssl binary). Each refusal names the package that provides the tool and says
to re-run, so a new user is not left to work out the fix alone.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from installer_test_helpers import run_bounded

CLI_SH = Path(__file__).resolve().parents[1] / "cli.sh"

pytestmark = pytest.mark.skipif(os.name == "nt", reason="cli.sh is POSIX shell (macOS + Linux)")


@pytest.mark.parametrize(
    ("present", "package"),
    [((), "curl"), (("curl",), "openssl"), (("curl", "openssl"), "coreutils")],
)
def test_missing_prerequisite_names_its_package(
    tmp_path: Path, present: tuple[str, ...], package: str
) -> None:
    # A closed PATH: only sh and stubs for the tools this case has. The checks
    # are `command -v` lookups, so a stub that exists is all a present tool needs.
    tools = tmp_path / "tools"
    tools.mkdir()
    real_sh = shutil.which("sh")
    assert real_sh is not None
    (tools / "sh").symlink_to(real_sh)
    for name in present:
        stub = tools / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)

    result = run_bounded([str(tools / "sh"), str(CLI_SH)], {"PATH": str(tools)}, cwd=str(tmp_path))

    assert result.returncode == 1, result.stdout + result.stderr
    assert f"'sudo apt-get install -y {package}'" in result.stderr, result.stderr
    assert f"'sudo dnf install -y {package}'" in result.stderr, result.stderr
    assert "then re-run this installer." in result.stderr, result.stderr
