"""Fixtures and builders shared by the ``test_pid_lifecycle*.py`` suites.

A PLAIN module, not a ``conftest.py``: each suite imports what it uses by name.
The two tracking-file fixtures are declared with an explicit ``name=`` so that
importing them does not collide with the test parameter of the same name
(flake8 F811); a suite imports the ``*_fixture`` symbols and its tests keep
requesting ``pid_file`` / ``session_pid_file``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# These tests exercise POSIX-only process-management semantics: process-group
# APIs (os.killpg / os.getpgrp / os.getpgid), POSIX identity/age probes
# (os.getuid / os.sysconf, /proc, ps), the raw signal.SIGKILL constant, and the
# POSIX kill path of the orphan sweep (which no-ops on Windows). None of these
# have a Windows equivalent, so they are skipped on Windows.
_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX process-management semantics only; see issue #2041"
)


@pytest.fixture(name="pid_file")
def pid_file_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect _pid_file_path to a temp file."""
    p = tmp_path / "kiro_pids.txt"
    monkeypatch.setattr("kiro_crew.session_pid._pid_file_path", lambda: p)
    return p


@pytest.fixture(name="session_pid_file")
def session_pid_file_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect _session_pid_file_path to a temp file."""
    p = tmp_path / "kiro_session_pids.txt"
    monkeypatch.setattr("kiro_crew.session_pid._session_pid_file_path", lambda: p)
    return p


def _launcher_shape() -> "tuple[tuple[str, ...], str, str, str]":
    """``(flags, run dir, prefix, suffix)`` as ``sandbox.namespace_argv`` uses them.

    Read off the sandbox module itself, never from literals, so these fixtures cannot
    become the place the builder and the recogniser drift apart.
    """
    import kiro_crew.sandbox as sandbox_mod

    return (
        sandbox_mod._LAUNCHER_INTERPRETER_FLAGS,
        sandbox_mod.namespace_launcher_script_dir(),
        sandbox_mod._SANDBOX_ARTIFACT_PREFIX,
        sandbox_mod._LAUNCHER_SCRIPT_SUFFIX,
    )


def _launcher_script_path() -> str:
    """A path in the exact shape ``sandbox.namespace_argv``'s ``mkstemp`` produces."""
    _flags, run_dir, prefix, suffix = _launcher_shape()
    return os.path.join(run_dir, f"{prefix}4242_ab12cd{suffix}")


def _launcher_cmdline(*wrapped: str, interpreter: str = "/usr/bin/python3") -> bytes:
    """A command line in the exact shape ``sandbox.namespace_argv`` builds."""
    flags, _run_dir, _prefix, _suffix = _launcher_shape()
    script = _launcher_script_path()
    return b"\x00".join(token.encode() for token in (interpreter, *flags, script, *wrapped))
