"""Tests for venv detection in dashboard update handler."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from kiro_crew.dashboard.state import DashboardState


def _init_repo(path) -> None:
    """Make *path* the top level of a real git working tree.

    Detection asks git and anchors the answer to this exact directory, so a
    fabricated ``.git`` entry does not stand in for a repository.
    """
    subprocess.run(
        ["git", "init", "-q"], cwd=str(path), check=True, capture_output=True, timeout=30
    )


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

    The helper no longer spawns pip itself — it hands the install to
    ``dep_sync.sync_or_reinstall``, which picks an editable reinstall or a
    dependency-only sync depending on whether the console script can be
    rewritten. These stub that one seam, so they assert what this endpoint owns:
    the exit code becomes a bool, and every message reaches the progress feed
    redacted and capped.
    """

    @pytest.mark.asyncio
    async def test_returns_true_on_success(self, monkeypatch, tmp_path) -> None:
        proj = _make_pip_proj(tmp_path)
        from kiro_crew import dep_sync
        from kiro_crew.dashboard.handlers.updates import _venv_pip_install

        state = _make_state(monkeypatch, tmp_path)
        seen: dict = {}

        def fake_sync(repo, target_py, emit=None, timeout=None):
            seen["repo"] = repo
            seen["target"] = target_py
            return 0

        monkeypatch.setattr(dep_sync, "sync_or_reinstall", fake_sync)
        assert await _venv_pip_install(str(proj), state) is True
        # The venv written to is THIS gateway's own, and the checkout is the one
        # the endpoint was asked to update — neither is re-derived downstream.
        assert str(seen["repo"]) == str(proj)
        assert str(seen["target"]) == sys.executable

    @pytest.mark.asyncio
    async def test_returns_false_on_nonzero_exit(self, monkeypatch, tmp_path) -> None:
        proj = _make_pip_proj(tmp_path)
        from kiro_crew import dep_sync
        from kiro_crew.dashboard.handlers.updates import _venv_pip_install

        state = _make_state(monkeypatch, tmp_path)
        errors = _track_errors(state)

        def fake_sync(repo, target_py, emit=None, timeout=None):
            emit("dep-sync: pip install -e exited 1: ERROR: could not find setup.py", True)
            return 1

        monkeypatch.setattr(dep_sync, "sync_or_reinstall", fake_sync)
        assert await _venv_pip_install(str(proj), state) is False
        assert any("could not find setup.py" in e for e in errors), errors

    @pytest.mark.asyncio
    async def test_substitution_notice_is_progress_not_an_error(
        self, monkeypatch, tmp_path
    ) -> None:
        """A locked script is a route change, not a failure.

        The substitute reports which wrapper was locked; pushing that as an error
        would surface a red step on a sync that went on to succeed.
        """
        proj = _make_pip_proj(tmp_path)
        from kiro_crew import dep_sync
        from kiro_crew.dashboard.handlers.updates import _venv_pip_install

        state = _make_state(monkeypatch, tmp_path)
        errors = _track_errors(state)

        def fake_sync(repo, target_py, emit=None, timeout=None):
            emit(r"dep-sync: C:\v\kirocrew.exe locked; substituting", False)
            return 0

        monkeypatch.setattr(dep_sync, "sync_or_reinstall", fake_sync)
        assert await _venv_pip_install(str(proj), state) is True
        assert not errors, errors

    @pytest.mark.asyncio
    async def test_progress_is_published_on_the_loop_not_the_worker_thread(
        self, monkeypatch, tmp_path
    ) -> None:
        """``push_update_progress`` touches loop-bound state, so it must not be
        called from the executor thread that runs the install.

        The callback is handed to dep_sync and invoked from a worker thread. The
        SSE subscriber queues it writes to belong to the serving loop, so calling
        it inline is a cross-thread mutation that works right up until a client is
        actually connected and then raises out of a thread nobody is awaiting.
        """
        import threading

        proj = _make_pip_proj(tmp_path)
        from kiro_crew import dep_sync
        from kiro_crew.dashboard.handlers.updates import _venv_pip_install

        state = _make_state(monkeypatch, tmp_path)
        loop_thread = threading.get_ident()
        seen_threads: list[int] = []
        original = state.push_update_progress

        def _record(step, detail):
            seen_threads.append(threading.get_ident())
            original(step, detail)

        state.push_update_progress = _record

        def fake_sync(repo, target_py, emit=None, timeout=None):
            # Called on the executor thread — assert that, so the test cannot pass
            # by accident if the install stops being offloaded.
            assert threading.get_ident() != loop_thread
            emit("dep-sync: installing 3 declared requirements", False)
            return 0

        monkeypatch.setattr(dep_sync, "sync_or_reinstall", fake_sync)
        assert await _venv_pip_install(str(proj), state) is True

        assert seen_threads, "expected at least one progress push"
        assert set(seen_threads) == {loop_thread}, seen_threads

    @pytest.mark.asyncio
    async def test_stderr_is_redacted(self, monkeypatch, tmp_path) -> None:
        """Credentials and exfil URLs in pip stderr are redacted before display.

        dep_sync hands pip's captured text over RAW — it has no idea where its
        caller publishes it — so redaction belongs here, at the publishing edge.
        """
        proj = _make_pip_proj(tmp_path)
        from kiro_crew import dep_sync
        from kiro_crew.dashboard.handlers.updates import _venv_pip_install

        state = _make_state(monkeypatch, tmp_path)
        errors = _track_errors(state)

        def fake_sync(repo, target_py, emit=None, timeout=None):
            emit("failed: AKIAIOSFODNN7EXAMPLE token leaked", True)
            return 1

        monkeypatch.setattr(dep_sync, "sync_or_reinstall", fake_sync)
        assert await _venv_pip_install(str(proj), state) is False
        # The raw AWS access key id should NOT appear verbatim in the error string.
        assert errors, "expected an error to be reported"
        assert "AKIAIOSFODNN7EXAMPLE" not in errors[0], errors


class TestRestartGateway:
    """Tests for the _restart_gateway helper."""

    @pytest.mark.asyncio
    async def test_invalid_executable_pushes_error_and_returns(
        self, monkeypatch, tmp_path
    ) -> None:
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
        monkeypatch.setattr(
            "asyncio.sleep", AsyncMock(return_value=None)
        )

        await _restart_gateway(state)

        assert save_called == [True]
        assert execv_called, "os.execv should have been called"

    @pytest.mark.asyncio
    async def test_save_history_failure_is_swallowed(
        self, monkeypatch, tmp_path
    ) -> None:
        """If save_all_slots_to_history raises, restart still proceeds."""
        from kiro_crew.dashboard.handlers.updates import _restart_gateway

        state = _make_state(monkeypatch, tmp_path)
        state.sessions = MagicMock()
        state.sessions.close_all = AsyncMock(return_value=None)

        execv_called: list[tuple] = []

        def raising_save(s):
            raise RuntimeError("boom")

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat.save_all_slots_to_history", raising_save
        )
        monkeypatch.setattr("os.execv", lambda *a, **k: execv_called.append(a))
        monkeypatch.setattr(
            "asyncio.sleep", AsyncMock(return_value=None)
        )

        await _restart_gateway(state)
        assert execv_called, "os.execv should have been called even if save raises"

    @pytest.mark.asyncio
    async def test_session_close_failure_is_swallowed(
        self, monkeypatch, tmp_path
    ) -> None:
        """If sessions.close_all raises, restart still proceeds."""
        from kiro_crew.dashboard.handlers.updates import _restart_gateway

        state = _make_state(monkeypatch, tmp_path)
        state.sessions = MagicMock()
        state.sessions.close_all = AsyncMock(side_effect=RuntimeError("net down"))

        execv_called: list[tuple] = []

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat.save_all_slots_to_history", lambda s: None
        )
        monkeypatch.setattr("os.execv", lambda *a, **k: execv_called.append(a))
        monkeypatch.setattr(
            "asyncio.sleep", AsyncMock(return_value=None)
        )

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

    @pytest.mark.asyncio
    async def test_pip_path_invokes_pip_install_then_restarts(
        self, monkeypatch, tmp_path
    ) -> None:
        proj = _make_pip_proj(tmp_path)
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))
        monkeypatch.setattr(
            "kiro_crew.platform.update_capability.running_from_checkout",
            lambda root, **kw: True,
        )
        monkeypatch.setattr("kiro_crew.env.is_toolbox_install", lambda: False)

        pip_called: list[bool] = []
        restart_called: list[bool] = []

        async def fake_pip(p, s):
            pip_called.append(True)
            return True

        async def fake_restart(s):
            restart_called.append(True)

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.updates._venv_pip_install", fake_pip
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.updates._restart_gateway", fake_restart
        )

        # Stub git pull so it succeeds.
        async def fake_exec(*args, **kwargs):
            proc = MagicMock()
            # The apply guard fails CLOSED on an unparseable rev-list count, so
            # the universal success stub must answer that one call with a real
            # fast-forwardable distance for the dispatch under test to be
            # reachable at all.
            out = b"0\t1\n" if "rev-list" in args else b""
            proc.communicate = AsyncMock(return_value=(out, b""))
            proc.returncode = 0
            return proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

        from kiro_crew.dashboard.handlers.updates import api_update_apply

        state = _make_state(monkeypatch, tmp_path)
        app = web.Application()
        app["state"] = state
        request = MagicMock()
        request.app = app

        resp = await api_update_apply(request)
        assert resp.status == 200
        await asyncio.sleep(0.05)
        for task in list(state._background_tasks):
            await task

        assert pip_called == [True]
        assert restart_called == [True]

    @pytest.mark.asyncio
    async def test_pip_failure_returns_without_restart(
        self, monkeypatch, tmp_path
    ) -> None:
        proj = _make_pip_proj(tmp_path)
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))
        monkeypatch.setattr(
            "kiro_crew.platform.update_capability.running_from_checkout",
            lambda root, **kw: True,
        )
        monkeypatch.setattr("kiro_crew.env.is_toolbox_install", lambda: False)

        restart_called: list[bool] = []

        async def fake_pip(p, s):
            return False  # pip install failed

        async def fake_restart(s):
            restart_called.append(True)

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.updates._venv_pip_install", fake_pip
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.updates._restart_gateway", fake_restart
        )

        async def fake_exec(*args, **kwargs):
            proc = MagicMock()
            # The apply guard fails CLOSED on an unparseable rev-list count, so
            # the universal success stub must answer that one call with a real
            # fast-forwardable distance for the dispatch under test to be
            # reachable at all.
            out = b"0\t1\n" if "rev-list" in args else b""
            proc.communicate = AsyncMock(return_value=(out, b""))
            proc.returncode = 0
            return proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

        from kiro_crew.dashboard.handlers.updates import api_update_apply

        state = _make_state(monkeypatch, tmp_path)
        app = web.Application()
        app["state"] = state
        request = MagicMock()
        request.app = app

        resp = await api_update_apply(request)
        assert resp.status == 200
        await asyncio.sleep(0.05)
        for task in list(state._background_tasks):
            await task

        assert restart_called == [], "restart should not run when pip install failed"
