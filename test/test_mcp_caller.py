"""Unit tests for :mod:`kiro_crew.mcp_caller` — KIROCREW_HOST_PID resolution.

The fork carries only the host-pid env-shortcut tests here (the wider
caller-identity wire-contract suite lives upstream); each test resets the
fork-only process-lifetime ``_FROM_ENV_CACHE`` so a previously-resolved
identity cannot leak between tests.
"""

from __future__ import annotations

import os
from unittest import mock

import kiro_crew.mcp_caller
from kiro_crew.mcp_caller import CallerContext


def test_from_env_uses_host_pid_env_before_walk(tmp_path, monkeypatch) -> None:
    """The sandbox launcher exports KIROCREW_HOST_PID (its own host pid — the
    exact pid the gateway keys session_pid files by). from_env must resolve
    via that env var directly, without depending on the /proc ancestor walk,
    which cannot match when the process's pid view diverges from the host's
    (PID-namespace sandboxing)."""
    monkeypatch.setattr(kiro_crew.mcp_caller, "_FROM_ENV_CACHE", None)
    # File keyed by a pid that is NOT in this test process's real ancestry —
    # only the env var can find it.
    pid_file = tmp_path / "session_pid_987654.txt"
    pid_file.write_text("hostpid-session-789", encoding="utf-8")

    with mock.patch.dict(
        os.environ,
        {"KIROCREW_SESSION_KEY": "", "KIROCREW_HOST_PID": "987654"},
        clear=False,
    ):
        with mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
            ctx = CallerContext.from_env()
    assert ctx.session_key == "hostpid-session-789"
    assert ctx.session_type == "pidfile"


def test_from_env_host_pid_missing_file_falls_back_to_walk(tmp_path, monkeypatch) -> None:
    """A stale/dangling KIROCREW_HOST_PID (no matching file) must not break
    the existing ancestor-walk fallback."""
    monkeypatch.setattr(kiro_crew.mcp_caller, "_FROM_ENV_CACHE", None)
    parent_pid = os.getppid()
    (tmp_path / f"session_pid_{parent_pid}.txt").write_text(
        "walk-session-111", encoding="utf-8"
    )

    with mock.patch.dict(
        os.environ,
        {"KIROCREW_SESSION_KEY": "", "KIROCREW_HOST_PID": "999999"},
        clear=False,
    ):
        with mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
            ctx = CallerContext.from_env()
    assert ctx.session_key == "walk-session-111"
