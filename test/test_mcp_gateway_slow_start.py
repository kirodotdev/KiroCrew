"""A slow first start is given the chat's own wait, without holding the spawn gate for it.

A server that took 45 s to answer its first ``initialize`` (a first ``npx`` /
``uvx`` run) was reaped at the old 30 s floor on every attempt, so it never
started, shared or not.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest
from test_mcp_gateway_spawn_gate import (
    _admission,
    _fake_backend,
    _register_frame,
    _resolver,
)

from kiro_crew.agent_sdk.mcp_problems import MCP_SERVER_WAIT_SECS
from kiro_crew.mcp_gateway import admission as adm
from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway.backend_handshake import _effective_initialize_timeout_secs
from kiro_crew.mcp_gateway.pool import BackendPool, PoolKey

pytestmark = pytest.mark.xdist_group("mcp_gateway")


def test_the_init_deadline_covers_what_a_chat_waits_for_a_starting_server() -> None:
    assert _effective_initialize_timeout_secs(10.0) >= MCP_SERVER_WAIT_SECS
    assert _effective_initialize_timeout_secs(10.0) > 45.0
    assert adm.INIT_DEADLINE_STRETCH_CAP_SECS > _effective_initialize_timeout_secs(10.0)


@pytest.mark.asyncio
async def test_the_spawn_permit_waits_a_shorter_window_than_the_deadline(monkeypatch) -> None:
    backend = _fake_backend()
    backend.initialize_timeout_secs = MCP_SERVER_WAIT_SECS

    async def fake_spawn(**_kwargs: Any) -> Any:
        return backend

    watched: list[tuple[float, Optional[float]]] = []
    admission = _admission(capacity=4, max_procs=8)

    def watch(permit: Any, **kwargs: Any) -> None:
        watched.append((kwargs["timeout"], kwargs["deadline"]))
        permit.release()

    monkeypatch.setattr(admission.gate, "watch_initialize", watch)
    monkeypatch.setattr(gw, "spawn_backend", fake_spawn)
    monkeypatch.setattr(gw, "_declared_env_to_forward", lambda k: {})
    monkeypatch.setattr(gw, "resolve_secret_uris", lambda env, home: (env, []))
    key = PoolKey.from_register(_register_frame())
    await gw._acquire_backend(BackendPool(max_backends=4), key, _resolver, admission=admission)
    assert watched == [(adm.SPAWN_PERMIT_INIT_WINDOW_SECS, MCP_SERVER_WAIT_SECS)]
    assert adm.SPAWN_PERMIT_INIT_WINDOW_SECS < MCP_SERVER_WAIT_SECS
    await admission.close()


async def _watch_in_flight(monkeypatch, deadline: Optional[float]) -> tuple[int, Any]:
    """Run the watcher on a handshake that stays in flight; count its waits."""
    gate = adm.SpawnGate(1)
    waits: list[float] = []

    async def no_event(_event: Any, timeout: float, _exited: Any = None) -> bool:
        waits.append(timeout)
        return False

    monkeypatch.setattr(adm.SpawnGate, "_wait_event", staticmethod(no_event))
    never_exits: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    permit = await gate.acquire(label="hung")
    await gate.watch_initialize(
        permit,
        init_done=asyncio.Event(),
        init_state=lambda: "in_flight",
        process_exited=lambda: never_exits,
        timeout=adm.SPAWN_PERMIT_INIT_WINDOW_SECS,  # flake-ok: _wait_event is faked; nothing waits
        deadline=deadline,
    )
    never_exits.cancel()
    return len(waits), permit


@pytest.mark.asyncio
async def test_a_clipped_window_is_the_whole_hold(monkeypatch) -> None:
    waits, permit = await _watch_in_flight(monkeypatch, MCP_SERVER_WAIT_SECS)
    assert waits == 1, "a hung start held the permit past the window"
    assert permit.released and permit.outcome == adm.OUTCOME_NEUTRAL


@pytest.mark.asyncio
async def test_a_window_that_is_the_deadline_still_outlasts_it(monkeypatch) -> None:
    waits, permit = await _watch_in_flight(monkeypatch, None)
    assert waits == 2
    assert permit.released
