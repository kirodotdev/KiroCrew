"""Tests for venv detection in dashboard update handler."""

from __future__ import annotations

import asyncio
import os
import subprocess
from unittest.mock import DEFAULT, AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard.state import DashboardState


def _as_owner(req: MagicMock) -> None:
    """Give a mock request the dashboard owner's claims, leaving every other key as-is.

    ``POST /api/update`` is owner-gated, so the dashboard-user request these tests
    model carries ``app == ""`` and the owner's subject: ``state.owner_id`` when
    one is configured, else the signed local bootstrap subject.
    """
    owner = str(getattr(req.app["state"], "owner_id", "") or "") or "local-app"
    claims = {"app": "", "user": owner}
    req.__contains__.side_effect = lambda key: key in claims
    req.__getitem__.side_effect = lambda key: claims[key] if key in claims else DEFAULT
    req.get.side_effect = lambda key, *default: claims[key] if key in claims else DEFAULT


def _init_repo(path) -> None:
    """Make *path* the top level of a real git working tree.

    Detection asks git and anchors the answer to this exact directory, so a
    fabricated ``.git`` entry does not stand in for a repository.
    """
    subprocess.run(
        ["git", "init", "-q"], cwd=str(path), check=True, capture_output=True, timeout=30
    )


def _pin_probe_git(monkeypatch, tmp_path):
    """Resolve the worktree probe's git to a fake under ``tmp_path``.

    ``update_capability._git_toplevel`` finds git through ``trusted_system_bin``
    (fixed system directories, never PATH) and asks ``rev-parse --show-toplevel``
    about the install root. Left alone, that is the HOST's git running from the
    test process -- and on a host that keeps git outside those directories the
    probe silently degrades to the on-disk fallback, so which branch a test
    exercised depended on the machine. The fake answers the one question the
    probe asks the way git does: the ``-C`` root itself when it carries ``.git``,
    exit 128 otherwise. Every argv it sees is appended to ``git-calls.log``
    beside it. The probe's own reading of real repositories is covered in
    ``test_update_capability.py``; here the install shape is a precondition.
    """
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    log = bin_dir / "git-calls.log"
    if os.name == "nt":
        fake = bin_dir / "git.cmd"
        fake.write_text(
            "@echo off\r\n"
            f'echo %* >> "{log}"\r\n'
            ":loop\r\n"
            'if "%~1"=="" goto miss\r\n'
            'if "%~1"=="-C" (\r\n'
            '  if exist "%~2\\.git" (echo %~2& exit /b 0)\r\n'
            "  goto miss\r\n"
            ")\r\n"
            "shift\r\n"
            "goto loop\r\n"
            ":miss\r\n"
            "echo fatal: not a git repository 1>&2\r\n"
            "exit /b 128\r\n",
            encoding="utf-8",
        )
    else:
        fake = bin_dir / "git"
        fake.write_text(
            "#!/bin/sh\n"
            f'printf \'%s\\n\' "$*" >> "{log}"\n'
            "root=\n"
            'while [ "$#" -gt 0 ]; do\n'
            '  if [ "$1" = "-C" ]; then root=$2; shift; fi\n'
            "  shift\n"
            "done\n"
            'if [ -n "$root" ] && [ -e "$root/.git" ]; then printf \'%s\\n\' "$root"; exit 0; fi\n'
            "echo 'fatal: not a git repository' >&2\n"
            "exit 128\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
    monkeypatch.setattr(
        "kiro_crew.platform.update_capability.trusted_system_bin", lambda _name: str(fake)
    )
    return fake


def _make_state(monkeypatch, tmp_path) -> DashboardState:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


def _make_pip_proj(tmp_path):
    """Create a git-backed project layout for the pip update path."""
    proj = tmp_path / "project"
    proj.mkdir()
    _init_repo(proj)
    return proj


def _track_errors(state):
    errors: list[str] = []
    original = state.push_update_progress

    def track(step, detail=""):
        if step == "error":
            errors.append(detail)
        original(step, detail)

    state.push_update_progress = track
    return errors


class TestVenvPipInstall:
    """Tests for the _venv_pip_install helper.

    The helper does not spawn pip itself — it hands the install to
    ``dep_sync.sync_or_reinstall``, which picks an editable reinstall or a
    dependency-only sync depending on whether the console script can be
    rewritten. These stub that one seam, so they assert what this endpoint owns:
    the exit code becomes a bool, and every message reaches the progress feed
    redacted and capped.
    """


class TestRestartGateway:
    """Tests for the _restart_gateway helper."""

    @pytest.mark.asyncio
    async def test_invalid_executable_pushes_error_and_returns(self, monkeypatch, tmp_path) -> None:
        """If sys.executable is missing or not executable, error is reported."""
        from kiro_crew.dashboard.handlers.updates import _restart_gateway

        state = _make_state(monkeypatch, tmp_path)
        errors = _track_errors(state)

        execv_called: list[bool] = []

        monkeypatch.setattr("sys.executable", "/nonexistent/python")
        monkeypatch.setattr("os.execv", lambda *a, **k: execv_called.append(True))

        await _restart_gateway(state)

        assert execv_called == []
        assert any("Cannot restart" in e for e in errors), errors

    @pytest.mark.asyncio
    async def test_success_path_invokes_execv(self, monkeypatch, tmp_path) -> None:
        """Happy path: history saved, sessions closed, os.execv invoked."""
        from kiro_crew.dashboard.handlers.updates import _restart_gateway

        state = _make_state(monkeypatch, tmp_path)
        state.sessions = MagicMock()
        state.sessions.close_all = AsyncMock(return_value=None)

        save_called: list[bool] = []
        execv_called: list[tuple] = []

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat.save_all_slots_to_history",
            lambda s: save_called.append(True),
        )
        monkeypatch.setattr("os.execv", lambda *a, **k: execv_called.append(a))
        # Skip the 0.5s drain delay.
        monkeypatch.setattr("asyncio.sleep", AsyncMock(return_value=None))

        await _restart_gateway(state)

        assert save_called == [True]
        assert execv_called, "os.execv should have been called"

    @pytest.mark.asyncio
    async def test_save_history_failure_is_swallowed(self, monkeypatch, tmp_path) -> None:
        """If save_all_slots_to_history raises, restart still proceeds."""
        from kiro_crew.dashboard.handlers.updates import _restart_gateway

        state = _make_state(monkeypatch, tmp_path)
        state.sessions = MagicMock()
        state.sessions.close_all = AsyncMock(return_value=None)

        execv_called: list[tuple] = []

        def raising_save(s):
            raise RuntimeError("boom")

        monkeypatch.setattr("kiro_crew.dashboard.chat.save_all_slots_to_history", raising_save)
        monkeypatch.setattr("os.execv", lambda *a, **k: execv_called.append(a))
        monkeypatch.setattr("asyncio.sleep", AsyncMock(return_value=None))

        await _restart_gateway(state)
        assert execv_called, "os.execv should have been called even if save raises"

    @pytest.mark.asyncio
    async def test_session_close_failure_is_swallowed(self, monkeypatch, tmp_path) -> None:
        """If sessions.close_all raises, restart still proceeds."""
        from kiro_crew.dashboard.handlers.updates import _restart_gateway

        state = _make_state(monkeypatch, tmp_path)
        state.sessions = MagicMock()
        state.sessions.close_all = AsyncMock(side_effect=RuntimeError("net down"))

        execv_called: list[tuple] = []

        monkeypatch.setattr("kiro_crew.dashboard.chat.save_all_slots_to_history", lambda s: None)
        monkeypatch.setattr("os.execv", lambda *a, **k: execv_called.append(a))
        monkeypatch.setattr("asyncio.sleep", AsyncMock(return_value=None))

        await _restart_gateway(state)
        assert execv_called, "os.execv should have been called even if close raises"

    @pytest.mark.asyncio
    async def test_concurrent_restart_is_coalesced_before_session_drain(
        self, monkeypatch, tmp_path
    ) -> None:
        """Only one caller may drain and exec a gateway process at a time."""
        from kiro_crew.dashboard.handlers.updates import _restart_gateway

        state = _make_state(monkeypatch, tmp_path)
        entered = asyncio.Event()
        release = asyncio.Event()
        execv_called: list[tuple] = []

        async def blocking_close() -> None:
            entered.set()
            await release.wait()

        state.sessions = MagicMock()
        state.sessions.close_all = blocking_close
        monkeypatch.setattr("kiro_crew.dashboard.chat.save_all_slots_to_history", lambda s: None)
        monkeypatch.setattr("os.execv", lambda *a, **k: execv_called.append(a))
        monkeypatch.setattr("asyncio.sleep", AsyncMock(return_value=None))

        first = asyncio.create_task(_restart_gateway(state))
        await entered.wait()
        assert await _restart_gateway(state) is False
        release.set()
        assert await first is True
        assert len(execv_called) == 1


class TestApiUpdateApplyVenvDispatch:
    """Tests for the install-path dispatch logic in api_update_apply."""
