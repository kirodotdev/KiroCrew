"""A resumed KAS session's launch snapshots go with the session.

``session/load`` hoists a launcher entry out of the agent batch into the
session-level array (``kas_agents.hoist_servers_for_session(...,
include_user_servers=True)``), and the batch the handle registers then lacks the
snapshot that projection wrote. These tests drive the real ``load_session``
and the provider's MCP re-injection, and check that shutdown still removes every
snapshot this session's projections wrote.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from acp_runtime_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_runtime_helpers import _make_runtime

from kiro_crew.acp.harness import SessionExtras
from kiro_crew.acp.kas_agents import to_client_custom_agent
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import ACP_BACKEND_KAS, METHOD_SESSION_LOAD

SESSION = "dashboard_chat-26256-resume"
SERVER = "canary-resume-26256"


@pytest.fixture
def home(tmp_path, monkeypatch):
    data_home = tmp_path / "crew-home"
    data_home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(data_home))
    return data_home


def _snapshots(home: Path) -> set[str]:
    leaf = home / "kas-mcp-launch"
    return {p.name for p in leaf.iterdir()} if leaf.is_dir() else set()


@pytest.fixture
def runtime(tmp_path, monkeypatch, home):
    """A KAS runtime whose every load re-projects the spec, as a real resume does."""
    script = tmp_path / "server.py"
    script.write_text("pass\n", encoding="utf-8")
    spec = {
        "mcpServers": {
            SERVER: {
                "command": sys.executable,
                "args": [str(script)],
                "env": {"CANARY_26256_ENDPOINT": "https://canary-26256.invalid/v1/zq7Xk"},
            }
        }
    }
    rt, _, _ = _make_runtime()
    rt._can_load_session = True
    rt._acp_backend = ACP_BACKEND_KAS

    async def _fake_send(method, params, timeout=None):
        if method == METHOD_SESSION_LOAD:
            return {"modes": {"currentModeId": "kirocrew"}, "models": []}
        return {}

    async def _fake_agents(agent, *, member_dispatch=False, crew_panel=False, session_key=""):
        return SessionExtras(
            custom_agents=[to_client_custom_agent(agent, spec, "p", session_key=SESSION)]
        )

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)
    monkeypatch.setattr(rt, "_kas_custom_agents", _fake_agents)
    rt.test_work_dir = str(tmp_path)
    return rt


async def _load(rt):
    return await rt.load_session(
        "", "sid-resume", cwd=rt.test_work_dir, agent="kirocrew", session_key=SESSION
    )


def _provider(rt, handle) -> AcpSessionProvider:
    provider = AcpSessionProvider(handle, rt, session_key=SESSION)  # type: ignore[arg-type]
    # The gateway claim reads real state; it is not what these tests pin.
    provider.reclaim = MagicMock()  # type: ignore[method-assign]
    return provider


async def _shutdown(provider: AcpSessionProvider, monkeypatch) -> None:
    async def _done():
        return None

    monkeypatch.setattr(provider, "_shutdown_session", _done, raising=False)
    await provider.shutdown()


async def _reinject(provider: AcpSessionProvider) -> None:
    provider._mcp_reinject_pending = True
    await provider.apply_pending_mcp_reinjection()


@pytest.mark.asyncio
async def test_a_resumed_sessions_hoisted_snapshot_is_removed_at_shutdown(
    home, runtime, monkeypatch
):
    handle = await _load(runtime)
    written = _snapshots(home)
    assert len(written) == 1
    registered = [a.get("mcpServers", {}) for a in handle.kas_registered_agents]
    assert all(SERVER not in servers for servers in registered), "the resume hoisted it"
    await _shutdown(_provider(runtime, handle), monkeypatch)
    assert _snapshots(home) == set()


@pytest.mark.asyncio
async def test_every_resume_of_one_session_is_removed_at_shutdown(home, runtime, monkeypatch):
    provider = _provider(runtime, await _load(runtime))
    await _reinject(provider)
    await _reinject(provider)
    assert len(_snapshots(home)) == 3, "each load wrote its own snapshot"
    await _shutdown(provider, monkeypatch)
    assert _snapshots(home) == set()


@pytest.mark.asyncio
async def test_an_mcp_reinjection_keeps_the_old_handles_snapshot_for_shutdown(
    home, runtime, monkeypatch
):
    provider = _provider(runtime, await _load(runtime))
    first = _snapshots(home)
    await _reinject(provider)
    assert first <= set(provider._handle.kas_launch_snapshots), "carried across the swap"
    await _shutdown(provider, monkeypatch)
    assert _snapshots(home) == set()
