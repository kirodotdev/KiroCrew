"""Tests for sandbox._probe_sandbox_exec — distinct paths."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from kiro_crew.sandbox import _probe_sandbox_exec


@patch("kiro_crew.sandbox.sys")
def test_non_darwin_returns_false(mock_sys):
    mock_sys.platform = "linux"
    assert _probe_sandbox_exec() is False


@patch("kiro_crew.sandbox.sys")
@patch("kiro_crew.sandbox.shutil.which", return_value=None)
def test_which_not_found_returns_false(mock_which, mock_sys):
    mock_sys.platform = "darwin"
    assert _probe_sandbox_exec() is False


@patch("kiro_crew.sandbox.sys")
@patch("kiro_crew.sandbox.shutil.which", return_value="/usr/bin/sandbox-exec")
@patch("kiro_crew.sandbox.subprocess.run")
def test_sandbox_exec_works(mock_run, mock_which, mock_sys):
    mock_sys.platform = "darwin"
    mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
    assert _probe_sandbox_exec() is True


@patch("kiro_crew.sandbox.sys")
@patch("kiro_crew.sandbox.shutil.which", return_value="/usr/bin/sandbox-exec")
@patch("kiro_crew.sandbox.subprocess.run")
def test_sandbox_exec_works_on_macos_26(mock_run, mock_which, mock_sys):
    """macOS 26 (Tahoe) is NOT hard-blocked: sandbox-exec + the Seatbelt kernel
    subsystem still work there, so the probe decides empirically. A passing probe
    returns True regardless of OS version — the old ``major >= 26 -> return False``
    gate was removed after verifying the real profile compiles, runs kiro-cli, and
    enforces credential-path denies on macOS 26.5."""
    mock_sys.platform = "darwin"
    mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
    assert _probe_sandbox_exec() is True


@patch("kiro_crew.sandbox.sys")
@patch("kiro_crew.sandbox.shutil.which", return_value="/usr/bin/sandbox-exec")
@patch("kiro_crew.sandbox.subprocess.run")
def test_sandbox_exec_fails_returns_false(mock_run, mock_which, mock_sys):
    mock_sys.platform = "darwin"
    mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1)
    assert _probe_sandbox_exec() is False


@patch("kiro_crew.sandbox.sys")
@patch("kiro_crew.sandbox.shutil.which", return_value="/usr/bin/sandbox-exec")
@patch("kiro_crew.sandbox.subprocess.run", side_effect=OSError("timeout"))
def test_subprocess_exception_returns_false(mock_run, mock_which, mock_sys):
    mock_sys.platform = "darwin"
    assert _probe_sandbox_exec() is False
