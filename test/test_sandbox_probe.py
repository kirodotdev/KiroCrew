"""Tests for sandbox availability probes — distinct paths."""

from __future__ import annotations

import subprocess
from unittest.mock import mock_open, patch

import pytest

from kiro_crew.sandbox import _probe_sandbox_exec


@patch("kiro_crew.sandbox.sys")
def test_non_darwin_returns_false(mock_sys):
    mock_sys.platform = "linux"
    assert _probe_sandbox_exec() is False


@patch("kiro_crew.sandbox.sys")
@patch("kiro_crew.sandbox.os.path.exists", return_value=False)
def test_sandbox_exec_not_found_returns_false(mock_exists, mock_sys):
    mock_sys.platform = "darwin"
    assert _probe_sandbox_exec() is False


@patch("kiro_crew.sandbox.sys")
@patch("kiro_crew.sandbox.os.path.exists", return_value=True)
@patch("kiro_crew.sandbox.subprocess.run")
def test_sandbox_exec_works(mock_run, mock_exists, mock_sys):
    mock_sys.platform = "darwin"
    mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
    assert _probe_sandbox_exec() is True


@patch("kiro_crew.sandbox.sys")
@patch("kiro_crew.sandbox.os.path.exists", return_value=True)
@patch("kiro_crew.sandbox.subprocess.run")
def test_sandbox_exec_works_on_macos_26(mock_run, mock_exists, mock_sys):
    """macOS 26 (Tahoe) is NOT hard-blocked: sandbox-exec + the Seatbelt kernel
    subsystem still work there, so the probe decides empirically. A passing probe
    returns True regardless of OS version — the old ``major >= 26 -> return False``
    gate was removed after verifying the real profile compiles, runs kiro-cli, and
    enforces credential-path denies on macOS 26.5."""
    mock_sys.platform = "darwin"
    mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
    assert _probe_sandbox_exec() is True


@patch("kiro_crew.sandbox.sys")
@patch("kiro_crew.sandbox.os.path.exists", return_value=True)
@patch("kiro_crew.sandbox.subprocess.run")
def test_sandbox_exec_fails_returns_false(mock_run, mock_exists, mock_sys):
    mock_sys.platform = "darwin"
    mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1)
    assert _probe_sandbox_exec() is False


@patch("kiro_crew.sandbox.sys")
@patch("kiro_crew.sandbox.os.path.exists", side_effect=[True, False])
@patch("kiro_crew.sandbox.subprocess.run")
def test_missing_trusted_probe_binary_fails_closed(mock_run, mock_exists, mock_sys):
    mock_sys.platform = "darwin"

    assert _probe_sandbox_exec() is False
    mock_run.assert_not_called()
    assert mock_exists.call_count == 2


@patch("kiro_crew.sandbox.sys")
@patch("kiro_crew.sandbox.os.path.exists", return_value=True)
@patch("kiro_crew.sandbox.subprocess.run", side_effect=OSError("timeout"))
def test_subprocess_exception_returns_false(mock_run, mock_exists, mock_sys):
    mock_sys.platform = "darwin"
    assert _probe_sandbox_exec() is False


def test_userns_available_delegates_to_probe(monkeypatch):
    """Public userns_available() is a stable alias for the private probe."""
    import kiro_crew.sandbox as sb

    monkeypatch.setattr(sb, "_probe_unshare", lambda: True)
    assert sb.userns_available() is True
    monkeypatch.setattr(sb, "_probe_unshare", lambda: False)
    assert sb.userns_available() is False


@pytest.fixture(autouse=True)
def _reset_wsl_cache():
    """Clear the ``is_wsl`` lru_cache before AND after every test.

    ``is_wsl`` is ``@lru_cache``-decorated and process-wide, so a
    monkeypatch-derived result cached inside a test would otherwise leak into
    later tests in the same pytest-xdist worker (e.g. a future JailProvider
    test that consults ``is_wsl()`` would see a stale ``True`` on a native
    Linux host). Tearing down the cache keeps each test hermetic.
    """
    import kiro_crew.sandbox as sb

    sb.is_wsl.cache_clear()
    yield
    sb.is_wsl.cache_clear()


def test_is_wsl_false_off_linux(monkeypatch):
    import kiro_crew.sandbox as sb

    monkeypatch.setattr(sb.sys, "platform", "darwin")
    assert sb.is_wsl() is False


def test_is_wsl_true_via_env_distro(monkeypatch):
    import kiro_crew.sandbox as sb

    monkeypatch.setattr(sb.sys, "platform", "linux")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    assert sb.is_wsl() is True


def test_is_wsl_true_via_env_interop(monkeypatch):
    import kiro_crew.sandbox as sb

    monkeypatch.setattr(sb.sys, "platform", "linux")
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.setenv("WSL_INTEROP", "/run/WSL/8_interop")
    assert sb.is_wsl() is True


def test_is_wsl_true_via_proc_version(monkeypatch):
    import kiro_crew.sandbox as sb

    monkeypatch.setattr(sb.sys, "platform", "linux")
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    version = "Linux version 5.15.0-microsoft-standard-WSL2 (gcc ...)"
    with patch("builtins.open", mock_open(read_data=version)):
        assert sb.is_wsl() is True


def test_is_wsl_false_on_native_linux(monkeypatch):
    import kiro_crew.sandbox as sb

    monkeypatch.setattr(sb.sys, "platform", "linux")
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    version = "Linux version 6.12.90-120.amzn2023.aarch64 (mockbuild@...)"
    with patch("builtins.open", mock_open(read_data=version)):
        assert sb.is_wsl() is False


def test_is_wsl_false_when_proc_version_unreadable(monkeypatch):
    import kiro_crew.sandbox as sb

    monkeypatch.setattr(sb.sys, "platform", "linux")
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    with patch("builtins.open", side_effect=OSError("no /proc")):
        assert sb.is_wsl() is False
