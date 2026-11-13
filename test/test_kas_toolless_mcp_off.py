"""A KAS process spawned for a tool-less agent switches its MCP servers off.

KAS starts every ``mcp.json`` server for each process even when the agent sets
``includeMcpJson: false``, so each ``kirocrew-lite`` helper started a new copy of
the user's servers. The runtime now sends ``_kiro/mcp/toggle {enabled: false}``
before any session exists, and only for a spec that mounts nothing.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.acp import runtime_spawn_launch as launch
from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO

LITE = {"name": "kirocrew-lite", "tools": [], "mcpServers": {}, "includeMcpJson": False}


def _runtime(spec: Any, backend: str = ACP_BACKEND_KAS, *, send_fails: bool = False):
    sent: list[tuple[str, dict]] = []

    def projected() -> Any:
        if isinstance(spec, Exception):
            raise spec
        return spec

    async def send_notification(method: str, params: dict) -> None:
        if send_fails:
            raise BrokenPipeError("gone")
        sent.append((method, params))

    rt = SimpleNamespace(
        _acp_backend=backend,
        _agent="kirocrew-lite",
        _projected_spawn_spec=projected,
        send_notification=send_notification,
    )
    return rt, sent


def test_toolless_kas_spawn_switches_mcp_off() -> None:
    rt, sent = _runtime(dict(LITE))
    asyncio.run(launch._switch_off_mcp_for_toolless_spawn(rt))
    assert sent == [("_kiro/mcp/toggle", {"enabled": False})]


@pytest.mark.parametrize(
    "spec",
    [
        {**LITE, "tools": ["read"]},
        {**LITE, "mcpServers": {"oauth-server": {"command": "srv"}}},
        {**LITE, "includeMcpJson": True},
        {k: v for k, v in LITE.items() if k != "includeMcpJson"},
        None,
    ],
    ids=["tools", "mcp-server", "include-mcp-json", "include-unset", "no-spec"],
)
def test_an_agent_that_can_use_mcp_keeps_it(spec: Any) -> None:
    rt, sent = _runtime(spec)
    asyncio.run(launch._switch_off_mcp_for_toolless_spawn(rt))
    assert sent == []


def test_other_backends_are_untouched() -> None:
    rt, sent = _runtime(dict(LITE), backend=ACP_BACKEND_KIRO)
    asyncio.run(launch._switch_off_mcp_for_toolless_spawn(rt))
    assert sent == []


def test_unreadable_spec_or_failed_send_does_not_raise() -> None:
    rt, sent = _runtime(OSError("unreadable"))
    asyncio.run(launch._switch_off_mcp_for_toolless_spawn(rt))
    assert sent == []
    rt, _ = _runtime(dict(LITE), send_fails=True)
    asyncio.run(launch._switch_off_mcp_for_toolless_spawn(rt))
