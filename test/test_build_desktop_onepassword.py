"""The desktop build's 1Password SDK step (``packaging/desktop-onepassword.sh``).

``install_onepassword_extra`` is sourced by ``packaging/build-desktop.sh`` and
called once per backend. It is run here against a stand-in interpreter that
records its argv, so the pins are on what the step actually does: one pip call,
wheels only for ``onepassword-sdk``, and a miss that logs one line and never
fails the build.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the step is a POSIX bash function")

REPO = Path(__file__).resolve().parents[1]
BUILD = (REPO / "packaging" / "build-desktop.sh").read_text(encoding="utf-8")


def _run(tmp_path: Path, pip_exit: int) -> tuple[subprocess.CompletedProcess[str], str]:
    argv_file = tmp_path / "argv"
    python = tmp_path / "python"
    python.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > "{argv_file}"\nexit {pip_exit}\n', encoding="utf-8"
    )
    python.chmod(0o755)
    script = (
        "set -euo pipefail\n"
        'ROOT="$1"\n'
        'log() { printf "LOG %s\\n" "$*"; }\n'
        '. "$ROOT/packaging/desktop-onepassword.sh"\n'
        'install_onepassword_extra "$2" "$ROOT[1password]" "linux/x86_64"\n'
        "echo AFTER\n"
    )
    result = subprocess.run(
        ["bash", "-c", script, "bash", str(REPO), str(python)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        cwd=tmp_path,
        timeout=FALLBACK_TIMEOUT_SECS,
    )
    return result, argv_file.read_text(encoding="utf-8") if argv_file.exists() else ""


def test_the_build_sources_the_step_and_calls_it_once_per_backend() -> None:
    assert re.search(r'^\. "\$ROOT/packaging/desktop-onepassword\.sh"$', BUILD, re.M)
    posix = BUILD[BUILD.index("build_backend() {") : BUILD.index("build_backend_windows() {")]
    windows = BUILD[BUILD.index("build_backend_windows() {") :]
    assert posix.count("install_onepassword_extra ") == 1
    assert windows.count("install_onepassword_extra ") == 1
    assert BUILD.count("onepassword-sdk") == 0  # the flags live in the helper only


def test_the_step_installs_wheels_only_for_the_sdk(tmp_path: Path) -> None:
    result, argv = _run(tmp_path, 0)
    assert result.returncode == 0 and "AFTER" in result.stdout
    assert "LOG" not in result.stdout
    args = argv.split("\n")
    assert args[:3] == ["-m", "pip", "install"]
    assert args[args.index("--only-binary") + 1] == "onepassword-sdk"
    assert ":all:" not in args
    assert args[-2].endswith("[1password]")


def test_a_missing_wheel_logs_one_line_and_never_fails_the_build(tmp_path: Path) -> None:
    result, _ = _run(tmp_path, 1)
    assert result.returncode == 0 and "AFTER" in result.stdout
    logged = [line for line in result.stdout.splitlines() if line.startswith("LOG ")]
    assert logged == [
        "LOG No prebuilt 1Password SDK for linux/x86_64 — shipping without the 1Password backend."
    ]
