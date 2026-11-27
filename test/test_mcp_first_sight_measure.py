"""A finished probe round measures, unasked, the servers it saw with no verdict yet.

The round at gateway start, and the one a newly added server triggers, start one
budgeted pre-flight pass in its own task. The pass takes only stdio servers that
just answered their probe: never a disabled one, one that needs sign-in, a
remote one or one whose env holds a ``secret://`` reference. A round that
overlapped a shutdown starts none.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard.handlers import mcp_probe
from kiro_crew.mcp_discovery import McpServerInfo
from kiro_crew.mcp_gateway import evaluate

#: Opts this module out of the conftest guard that turns this pass off.
FIRST_SIGHT_MEASURE = True


def _servers() -> list[McpServerInfo]:
    return [
        McpServerInfo(name="ok-stdio", command="/bin/true", status="ok"),
        McpServerInfo(name="off", command="/bin/true", status="ok", disabled=True),
        McpServerInfo(name="sign-in", command="/bin/true", status="needs_auth"),
        McpServerInfo(name="remote", url="https://mcp.example.com/sse", status="ok"),
        McpServerInfo(
            name="secret", command="/bin/true", status="ok", env={"TOKEN": "secret://vault/token"}
        ),
        McpServerInfo(name="broken", command="/bin/true", status="error"),
    ]


def test_first_sight_keeps_only_stdio_servers_that_just_answered():
    assert [s.name for s in evaluate.first_sight(_servers())] == ["ok-stdio"]


class _State:
    def __init__(self, *, draining: bool = False) -> None:
        self._background_tasks: set = set()
        self.sessions = SimpleNamespace(final_drain_started=draining)
        self.pushed: list[tuple[str, ...]] = []

    def push_refresh(self, *kinds: str) -> None:
        self.pushed.append(kinds)


@pytest.fixture
def passes(monkeypatch, tmp_path):
    """Every pre-flight pass a round starts, with the probe round itself faked."""
    import kiro_crew.mcp_discovery as disc

    calls: list[tuple[list[str], object]] = []

    async def _evaluate(
        servers, runtime_dir, *, budget=evaluate.MAX_EVALUATIONS_PER_PASS, on_progress=None
    ):
        calls.append(([s.name for s in servers], budget))
        if on_progress is not None:
            on_progress(1, 1, 1)
        return {}

    async def _probe_all():
        return _servers()

    monkeypatch.setattr(evaluate, "evaluate_new_servers", _evaluate)
    monkeypatch.setattr(disc, "probe_all", _probe_all)
    # Shared names go through the facade, which carries them to every holder.
    from kiro_crew.dashboard.handlers import mcp as mcp_facade

    monkeypatch.setattr(mcp_facade, "records_dir", lambda _socket: tmp_path)
    monkeypatch.setattr(mcp_facade, "_GLOBAL_MCP_JSON", tmp_path / "absent.json", raising=False)
    monkeypatch.setattr(mcp_facade, "_kirocrew_mcp_json", lambda: tmp_path / "absent-kc.json")
    monkeypatch.setattr(mcp_probe, "_record_probe_verdicts", lambda *_a: None)
    monkeypatch.setattr(mcp_probe, "_mcp_probe_cache", [])
    monkeypatch.setattr(mcp_probe, "_mcp_probe_task", None)
    return calls


async def _drain(state: _State) -> None:
    # Bounded: every task here is a fake that finishes at once.
    await asyncio.wait_for(asyncio.gather(*list(state._background_tasks)), timeout=30)


@pytest.mark.asyncio
async def test_a_probe_round_starts_one_budgeted_pass_over_the_first_sight_servers(passes):
    state = _State()

    await mcp_probe._run_mcp_probe(state)
    await _drain(state)

    assert passes == [(["ok-stdio"], evaluate.MAX_EVALUATIONS_PER_PASS)]
    assert state.pushed == [("mcp_servers",), ("mcp_servers",)]


@pytest.mark.asyncio
async def test_a_round_that_overlapped_a_shutdown_starts_no_pass(passes):
    state = _State(draining=True)

    await mcp_probe._run_mcp_probe(state)
    await _drain(state)

    assert passes == []
