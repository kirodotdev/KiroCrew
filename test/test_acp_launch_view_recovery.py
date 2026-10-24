"""A short unreadable-main-spec episode ends when its co-tenants leave.

A process launched on a view of the main spec keeps that view for life, so once
an episode opens every new main-agent session on it is refused, before the
episode closes and after. That refusal is bounded by the co-tenants' lifetime:
the first refused open made with none left ends the process
(``AcpRuntime._recycle_superseded_launch``), the background runtime's holder
displaces it (``_is_stale`` answers ``launch_view``), and the next main-agent
open runs on a fresh process whose launch recorded a view built after the
rebuild re-projected the spec.

A pre-episode view stays refused while co-tenants live. Admitting it because its
source bytes match again is not safe: the view's grants were derived before the
episode, and equal source bytes do not prove the view derived from them now
matches.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from acp_runtime_helpers import _make_runtime
from test_acp_runtime_session_start import _launch_on_a_main_spec_view
from test_update_provider import _UNALLOCATABLE_PID

from kiro_crew.acp.session_handle import AcpRuntimeError
from kiro_crew.acp.types import METHOD_SESSION_NEW
from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import SessionManager

_LAUNCH_FIELDS = ("_launch_unbound", "_launch_view_source", "_launch_view_episode")


async def _launched(agent_mod, agents, tmp_path):
    """A runtime on a fake process whose launch ran the real view-recording launch."""
    rt, _, proc = _make_runtime()
    rt._agent = "kirocrew"
    rt._work_dir = str(tmp_path)
    proc.pid = _UNALLOCATABLE_PID
    rt._pid = _UNALLOCATABLE_PID
    launch, launcher = _launch_on_a_main_spec_view(rt, agent_mod, agents, "main")
    await launch()
    for name in _LAUNCH_FIELDS:
        setattr(rt, name, getattr(launcher, name))
    rt._spawn_monotonic = time.monotonic()  # young: no age or RSS reason
    rt._expect_mcp_reports = False  # a fake process reports no MCP servers
    return rt, proc


def _holder() -> SessionManager:
    cfg = KiroCrewConfig()
    cfg.session.timeout_secs = 2
    return SessionManager(cfg, provider_factory=lambda **kwargs: AsyncMock())


@pytest.mark.asyncio
async def test_after_recovery_and_the_co_tenants_leaving_the_next_open_runs_fresh(
    unreadable_main_episode, monkeypatch, tmp_path
):
    """Episode, rebuild re-projects, co-tenant leaves, the holder relaunches fresh.

    While the co-tenant lives, the old process refuses main-agent opens, is only
    marked, and the holder's sweep leaves it in place. Once it leaves, the next
    refused open ends the process and the sweep displaces it, so the slot is
    free for a relaunch. The relaunch runs the real launch, which records a view
    built after the rebuild, and its open succeeds. Mutation pin: without the
    recycle the old process is never marked, so the holder keeps it and every
    later main-agent open on it is refused.
    """
    agent_mod, audited = unreadable_main_episode
    monkeypatch.setattr(agent_mod, "_main_spec_unreadable", False)
    monkeypatch.setattr(agent_mod, "_main_spec_episode_revision", 0)
    monkeypatch.setattr(agent_mod, "_main_spec_retry_timer", None)
    agents = tmp_path / "agents"
    agents.mkdir()
    # Present, so neither launch regenerates the managed configs first.
    (agents / "kirocrew.json").write_text('{"name": "kirocrew"}', encoding="utf-8")
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", agents)

    old, old_proc = await _launched(agent_mod, agents, tmp_path)
    assert old._launch_view_episode == agent_mod.main_spec_episode()  # built while healthy
    old.kill = AsyncMock()  # type: ignore[method-assign]
    old._session_queues["sid-co-tenant"] = asyncio.Queue()
    holder = _holder()
    holder._bg_runtime = old
    try:
        # A transient read failure opens the episode; the next rebuild reads
        # and writes the spec, which closes it.
        with agent_mod._main_spec_episode_lock:
            agent_mod._main_spec_unreadable = True
            agent_mod._main_spec_episode_revision += 1
            revision = agent_mod._main_spec_episode_revision
        agent_mod._note_main_spec_readable(agents / "kirocrew.json", "kirocrew", revision)
        assert agent_mod.main_spec_episode() == (revision, False)

        # The co-tenant keeps the old process: refused, marked, not ended, kept.
        with pytest.raises(AcpRuntimeError, match="may not start"):
            await old.create_session(cwd=str(tmp_path), agent=None)
        old.kill.assert_not_awaited()
        assert await holder._reap_idle_stale_bg_runtime() is False
        assert holder._bg_runtime is old

        # The co-tenant leaves: the next refused open ends the old process, and
        # the holder's sweep displaces it.
        del old._session_queues["sid-co-tenant"]
        with pytest.raises(AcpRuntimeError, match="may not start"):
            await old.create_session(cwd=str(tmp_path), agent=None)
        old.kill.assert_awaited_once()
        assert "launch view" in old.kill.await_args.kwargs["reason"]
        old_proc.stdin.write.assert_not_called()
        assert await holder._reap_idle_stale_bg_runtime() is True
        assert holder._bg_runtime is None

        # The relaunch records a view built after the rebuild, and opens.
        fresh, _ = await _launched(agent_mod, agents, tmp_path)
        assert fresh._launch_view_episode == agent_mod.main_spec_episode() == (revision, False)
        holder._bg_runtime = fresh
        sent: list[str] = []

        async def _send(method, params, **kwargs):
            sent.append(method)
            return {"sessionId": "sid-fresh"}

        fresh._send_and_await = _send  # type: ignore[method-assign]
        handle = await fresh.create_session(cwd=str(tmp_path), agent=None)
        assert handle.session_id == "sid-fresh"
        assert sent[0] == METHOD_SESSION_NEW
        assert audited == ["kirocrew", "kirocrew"]
    finally:
        holder._bg_runtime = None
        holder._draining_bg_runtimes.clear()
        await holder.close_all()
