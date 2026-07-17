"""MCP gateway — sidecar broker for cross-session MCP sharing.

This package wires an asyncio unix-socket broker into KiroCrew as a
sidecar subprocess. When enabled, it:

1. Writes rewritten kiro agent JSON into an overlay directory
   (``~/.kirocrew/mcp-gateway/agents/``) — never touches the user's
   ``~/.kiro/agents/`` files on disk.
2. Spawns ``python -m kiro_crew.mcp_gateway.gatewayd`` at KiroCrew
   startup and supervises it.
3. Cooperates with ``sandbox.py`` so sandboxed kiro-cli sessions see the
   rewritten specs via a bind-mount while the host filesystem is unchanged.

Every component ships as Python under this package — no external
binaries, no native extensions. See ``docs/system-specs/modules/acp-client.md``
for the full design.
"""

import importlib
from typing import TYPE_CHECKING

# Lazy attribute access (PEP 562) so importing this
# package — e.g. config.loader importing the rewriter path helpers at module
# top level — does NOT eagerly pull asyncio/socket/signal via
# backend/gatewayd/manager/stub. Submodules load on first attribute access.
_LAZY = {
    "Backend": ("backend", "Backend"),
    "send_initialize": ("backend", "send_initialize"),
    "spawn_backend": ("backend", "spawn_backend"),
    "run_gatewayd": ("gatewayd", "run_gatewayd"),
    "GatewayManager": ("manager", "GatewayManager"),
    "GatewaySpec": ("manager", "GatewaySpec"),
    "BackendPool": ("pool", "BackendPool"),
    "PoolKey": ("pool", "PoolKey"),
    "UNPOOLABLE_SERVERS": ("rewriter", "UNPOOLABLE_SERVERS"),
    "default_socket_path": ("rewriter", "default_socket_path"),
    "rewrite_agents": ("rewriter", "rewrite_agents"),
    "stub_main": ("stub", "main"),
}


def __getattr__(name: str):
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f"{__name__}.{target[0]}")
    return getattr(module, target[1])


def __dir__():
    return sorted(__all__)


if TYPE_CHECKING:  # names for static type checkers only
    from kiro_crew.mcp_gateway.backend import Backend, send_initialize, spawn_backend
    from kiro_crew.mcp_gateway.gatewayd import run_gatewayd
    from kiro_crew.mcp_gateway.manager import GatewayManager, GatewaySpec
    from kiro_crew.mcp_gateway.pool import BackendPool, PoolKey
    from kiro_crew.mcp_gateway.rewriter import (
        UNPOOLABLE_SERVERS,
        default_socket_path,
        rewrite_agents,
    )
    from kiro_crew.mcp_gateway.stub import main as stub_main

__all__ = [
    "Backend",
    "BackendPool",
    "GatewayManager",
    "GatewaySpec",
    "PoolKey",
    "UNPOOLABLE_SERVERS",
    "default_socket_path",
    "rewrite_agents",
    "run_gatewayd",
    "send_initialize",
    "spawn_backend",
    "stub_main",
]
