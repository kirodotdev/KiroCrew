"""Opt-in real MXC tests; subprocesses import Crew only after home isolation.

Set KIROCREW_TEST_WINDOWS_RUNNER to the freshly built runner. The Codex case
additionally needs KIROCREW_TEST_CODEX_EXE; its provider/API are synthetic local
fixtures, and it neither signs in nor contacts a real model provider.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

RUNNER = os.environ.get("KIROCREW_TEST_WINDOWS_RUNNER")
CODEX = os.environ.get("KIROCREW_TEST_CODEX_EXE")
ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    os.name != "nt" or not RUNNER, reason="Opt-in real Windows MXC runner"
)


def run_probe(script: str, *args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-B", str(ROOT / "scripts" / script), "--runner", str(RUNNER), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=115,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "mode",
    ["mcp", "identity", "expired", "foreign_scope", "tamper", "foreign", "outage", "approval"],
)
def test_native_broker_with_real_core_cron_and_refusals(mode):
    run_probe("check_windows_native_mcp.py", "--mode", mode)


@pytest.mark.skipif(not CODEX, reason="Actual Codex executable supplied separately")
def test_actual_codex_calls_both_real_crew_servers():
    run_probe("check_windows_native_mcp.py", "--codex", str(CODEX))


@pytest.mark.parametrize("args", [[], ["--rpc"]], ids=["silent-late-handle", "rpc-refusal"])
def test_failed_stub_does_not_retain_native_handles(args):
    run_probe("check_windows_native_refusal.py", *args)
