"""MCP gateway — sidecar broker for cross-session MCP sharing.

This package wires an asyncio unix-socket broker into KiroCrew as a
sidecar subprocess. When enabled, it:

1. Writes rewritten kiro agent JSON into an overlay directory
   (``~/.kiro/crew/mcp-gateway/agents/``) — never touches the user's
   ``~/.kiro/agents/`` files on disk.
2. Spawns ``python -m kiro_crew.mcp_gateway.gatewayd`` at KiroCrew
   startup and supervises it.
3. Injects the rewritten specs' broker stubs into each kiro-cli session
   over ACP ``session/new``. A session-injected MCP server outranks the
   same-named entry in the agent spec, so no file is delivered anywhere and
   the user's ``~/.kiro/agents/`` is never read from or written to.

Every component ships as Python under this package — no external
binaries, no native extensions. See ``docs/system-specs/modules/acp-client.md``
for the full design.
"""

import importlib
import sys
from typing import TYPE_CHECKING

#: Platforms where the sidecar broker can run. The broker listens on an
#: ``AF_UNIX`` socket (``asyncio.start_unix_server``) and delivers each
#: session's poolable stubs over ACP ``session/new`` injection (no bind-mount,
#: no privileged operation), so it runs on any POSIX platform with
#: unix-domain sockets. Windows is excluded: its asyncio proactor loop has no
#: ``AF_UNIX`` support, so ``gatewayd`` cannot bind its listening socket there
#: until a loopback/named-pipe transport is added.
GATEWAY_SUPPORTED_PLATFORMS = ("linux", "darwin")


def is_gateway_supported() -> bool:
    """Return ``True`` if the shared MCP gateway broker can run on this OS.

    Single source of truth shared by the boot path
    (``GatewayOrchestrator._init_mcp_gateway``) and the dashboard status/enable
    handlers, so the UI's "supported" signal never disagrees with whether the
    broker will actually start.
    """
    return sys.platform in GATEWAY_SUPPORTED_PLATFORMS


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
    "GATEWAY_SUPPORTED_PLATFORMS",
    "GatewayManager",
    "GatewaySpec",
    "PoolKey",
    "UNPOOLABLE_SERVERS",
    "default_socket_path",
    "is_gateway_supported",
    "rewrite_agents",
    "run_gatewayd",
    "send_initialize",
    "spawn_backend",
    "stub_main",
]
