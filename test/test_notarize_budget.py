"""Hanging-call drills for ``packaging/signing/notarize.sh``.

The script advertises a hard wall-clock budget (``NOTARIZE_BUDGET_SECS``).
That promise has to hold not only across its own sleeps but across the
``xcrun notarytool`` calls themselves: a request that hangs instead of
returning a timeout error must be stopped when the budget runs out, or the
job runs on to the workflow-level timeout and reintroduces the
mid-publication cancellation the script exists to prevent.  These drills
stand in a fake ``xcrun`` that hangs at each call site and assert the script
still exits within the budget with the budget-exhausted verdict.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "packaging" / "signing" / "notarize.sh"

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="notarize.sh is a Bash script for the macOS signing runner",
)


def _fake_xcrun(tmp_path: Path, body: str) -> dict[str, str]:
    """Install a fake ``xcrun`` first on PATH; return the env for the script."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    xcrun = bin_dir / "xcrun"
    # $1 is always "notarytool"; $2 is the verb (submit / info / log).
    xcrun.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    xcrun.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env.update(
        APPLE_ID="drill@example.com",
        APPLE_PW="drill",
        TEAM_ID="DRILL",
        NOTARIZE_BUDGET_SECS="2",
        NOTARIZE_POLL_SECS="1",
        NOTARIZE_RETRY_MIN_SECS="1",
    )
    return env


def _run(tmp_path: Path, env: dict[str, str]) -> tuple[subprocess.CompletedProcess[str], float]:
    target = tmp_path / "app.zip"
    target.write_bytes(b"not really a zip")
    started = time.monotonic()
    proc = subprocess.run(
        ["bash", str(SCRIPT), str(target)],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    return proc, time.monotonic() - started


def test_hanging_submit_is_stopped_at_the_budget(tmp_path: Path) -> None:
    # The very first call hangs: no submission id ever comes back.
    env = _fake_xcrun(tmp_path, "exec sleep 300\n")
    proc, took = _run(tmp_path, env)
    assert proc.returncode == 1, proc.stderr
    assert "budget of 2s exhausted" in proc.stderr
    assert "submission: <none>" in proc.stderr
    assert "notarytool submit hung" in proc.stdout
    # Well inside the 300s the fake would have slept and far below any job timeout.
    assert took < 30


def test_hanging_info_is_stopped_at_the_budget(tmp_path: Path) -> None:
    # Submit succeeds, then the status poll hangs.
    env = _fake_xcrun(
        tmp_path,
        'case "$2" in\n'
        '  submit) echo \'{"id":"sub-1"}\' ;;\n'
        "  *) exec sleep 300 ;;\n"
        "esac\n",
    )
    proc, took = _run(tmp_path, env)
    assert proc.returncode == 1, proc.stderr
    assert "budget of 2s exhausted" in proc.stderr
    assert "submission: sub-1" in proc.stderr
    assert "notarytool info hung" in proc.stdout
    assert took < 30


def test_healthy_path_still_accepts(tmp_path: Path) -> None:
    # The watchdog must not disturb a call that returns normally.
    env = _fake_xcrun(
        tmp_path,
        'case "$2" in\n'
        '  submit) echo \'{"id":"sub-1"}\' ;;\n'
        '  info) echo \'{"status":"Accepted"}\' ;;\n'
        "esac\n",
    )
    proc, _ = _run(tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    assert "notarization Accepted: sub-1" in proc.stdout


def test_transient_error_before_budget_is_retried_then_bounded(tmp_path: Path) -> None:
    # A call that FAILS fast stays a retryable transient; the retry sleeps are
    # what run into the budget, so the verdict is still budget-exhausted.
    env = _fake_xcrun(tmp_path, "echo 'The request timed out.' >&2\nexit 1\n")
    proc, took = _run(tmp_path, env)
    assert proc.returncode == 1, proc.stderr
    assert "treating as transient" in proc.stdout
    assert "budget of 2s exhausted" in proc.stderr
    assert took < 30
