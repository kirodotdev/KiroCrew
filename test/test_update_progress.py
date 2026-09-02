"""Tests for the update progress feature."""

from __future__ import annotations

import asyncio
import json
import subprocess
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
    """Create a minimal DashboardState for testing."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


class TestUpdateProgressState:
    """Tests for DashboardState update progress tracking."""

    def test_initial_state_is_none(self, monkeypatch, tmp_path) -> None:
        state = _make_state(monkeypatch, tmp_path)
        assert state._update_progress is None

    def test_push_update_progress_sets_state(self, monkeypatch, tmp_path) -> None:
        state = _make_state(monkeypatch, tmp_path)
        state.push_update_progress("pulling", "Pulling latest changes…")
        assert state._update_progress == {"step": "pulling", "detail": "Pulling latest changes…"}

    def test_push_update_progress_updates_step(self, monkeypatch, tmp_path) -> None:
        state = _make_state(monkeypatch, tmp_path)
        state.push_update_progress("pulling", "Pulling…")
        state.push_update_progress("building", "Building…")
        assert state._update_progress == {"step": "building", "detail": "Building…"}

    def test_push_failed_keeps_progress_visible(self, monkeypatch, tmp_path) -> None:
        state = _make_state(monkeypatch, tmp_path)
        state.push_update_progress("pulling", "Pulling…")
        state.push_update_progress("failed", "Something broke")
        assert state._update_progress is not None
        assert state._update_progress["step"] == "failed"

    def test_clear_update_progress(self, monkeypatch, tmp_path) -> None:
        state = _make_state(monkeypatch, tmp_path)
        state.push_update_progress("building", "Building…")
        state.clear_update_progress()
        assert state._update_progress is None

    def test_broadcast_called_on_push(self, monkeypatch, tmp_path) -> None:
        state = _make_state(monkeypatch, tmp_path)
        calls: list[dict] = []
        monkeypatch.setattr(state, "_broadcast", lambda note: calls.append(note))
        state.push_update_progress("syncing", "Syncing workspace…")
        assert len(calls) == 1
        assert calls[0]["_type"] == "update_progress"
        assert calls[0]["step"] == "syncing"
        assert calls[0]["detail"] == "Syncing workspace…"

    def test_ws_broadcast_format(self, monkeypatch, tmp_path) -> None:
        """Verify the WS message format for update_progress events."""
        state = _make_state(monkeypatch, tmp_path)
        ws_messages: list[str] = []
        mock_ws = MagicMock()
        mock_ws.closed = False

        def fake_send(msg: str) -> None:
            ws_messages.append(msg)

        mock_ws.send_str = fake_send
        state._ws_clients = [mock_ws]

        state.push_update_progress("building", "Rebuilding package…")

        assert len(ws_messages) == 1
        parsed = json.loads(ws_messages[0])
        assert parsed["type"] == "update_progress"
        assert parsed["data"]["step"] == "building"
        assert parsed["data"]["detail"] == "Rebuilding package…"


class TestUpdateEndpoints:
    """Tests for the update HTTP endpoints."""

    @pytest.mark.asyncio
    async def test_simulate_walks_through_steps(self, monkeypatch, tmp_path) -> None:
        """Simulate endpoint broadcasts progress for each step."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.handlers.config_path", lambda: tmp_path / "c.json")

        from kiro_crew.dashboard.handlers import api_update_simulate

        state = _make_state(monkeypatch, tmp_path)
        steps_seen: list[str] = []
        original_push = state.push_update_progress

        def track_push(step: str, detail: str = "") -> None:
            steps_seen.append(step)
            original_push(step, detail)

        monkeypatch.setattr(state, "push_update_progress", track_push)

        app = web.Application()
        app["state"] = state
        request = MagicMock()
        request.app = app
        request.json = AsyncMock(return_value={"delay": 0.01})

        resp = await api_update_simulate(request)
        data = json.loads(resp.body)
        assert data["status"] == "simulating"

        # Let the background task run
        await asyncio.sleep(0.2)

        # Public update flow: git pull + pip install + npm build + restart.
        # (No Brazil "syncing" / toolbox "installing" steps in OSS.)
        assert "pulling" in steps_seen
        assert "building" in steps_seen
        assert "restarting" in steps_seen
        assert "done" in steps_seen

    @pytest.mark.asyncio
    async def test_simulate_fail_at(self, monkeypatch, tmp_path) -> None:
        """Simulate endpoint stops at fail_at step."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.handlers.config_path", lambda: tmp_path / "c.json")

        from kiro_crew.dashboard.handlers import api_update_simulate

        state = _make_state(monkeypatch, tmp_path)
        steps_seen: list[str] = []
        original_push = state.push_update_progress

        def track_push(step: str, detail: str = "") -> None:
            steps_seen.append(step)
            original_push(step, detail)

        monkeypatch.setattr(state, "push_update_progress", track_push)

        app = web.Application()
        app["state"] = state
        request = MagicMock()
        request.app = app
        request.json = AsyncMock(return_value={"delay": 0.01, "fail_at": "building"})

        await api_update_simulate(request)
        await asyncio.sleep(0.15)

        # Public flow order: pulling -> building -> restarting -> done.
        # Failing at "building" stops after "pulling" runs.
        assert "pulling" in steps_seen
        assert "failed" in steps_seen
        assert "building" not in steps_seen  # fails before building runs
        assert "restarting" not in steps_seen

    @pytest.mark.asyncio
    async def test_simulate_reject(self, monkeypatch, tmp_path) -> None:
        """Simulate endpoint returns 409 when reject=true."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.handlers.config_path", lambda: tmp_path / "c.json")

        from kiro_crew.dashboard.handlers import api_update_simulate

        state = _make_state(monkeypatch, tmp_path)
        app = web.Application()
        app["state"] = state
        request = MagicMock()
        request.app = app
        request.json = AsyncMock(return_value={"reject": True})

        resp = await api_update_simulate(request)
        assert resp.status == 409
        data = json.loads(resp.body)
        assert "uncommitted" in data["error"]

    @pytest.mark.asyncio
    async def test_cancel_clears_progress(self, monkeypatch, tmp_path) -> None:
        """Cancel endpoint clears update progress."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)

        from kiro_crew.dashboard.handlers import api_update_cancel

        state = _make_state(monkeypatch, tmp_path)
        state.push_update_progress("building", "Building…")
        assert state._update_progress is not None

        app = web.Application()
        app["state"] = state
        request = MagicMock()
        request.app = app

        resp = await api_update_cancel(request)
        assert resp.status == 200
        # Progress should be cleared after cancel
        assert state._update_progress is None

    @pytest.mark.asyncio
    async def test_update_apply_rejects_dirty_tree(self, monkeypatch, tmp_path) -> None:
        """Update apply returns 409 when working tree is dirty."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        _init_repo(tmp_path)  # must be a git checkout to reach the dirty check
        monkeypatch.setattr(
            "kiro_crew.platform.update_capability.running_from_checkout",
            lambda root, **kw: True,
        )

        from kiro_crew.dashboard.handlers import api_update_apply

        state = _make_state(monkeypatch, tmp_path)
        app = web.Application()
        app["state"] = state
        request = MagicMock()
        request.app = app

        # Mock git status to return dirty output
        async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(b" M some_file.py\n", b""))
            proc.returncode = 0
            return proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

        resp = await api_update_apply(request)
        assert resp.status == 409
        data = json.loads(resp.body)
        assert "uncommitted" in data["error"]
