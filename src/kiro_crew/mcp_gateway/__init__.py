"""MCP gateway — sidecar broker for cross-session MCP sharing.

This package wires an asyncio local-IPC broker into KiroCrew as a
sidecar subprocess. The endpoint is a unix-domain socket on POSIX and a named
pipe on Windows; ``mcp_gateway.transport`` owns that split so no other module
has to know which is in play. When enabled, it:

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

#: Platforms where the sidecar broker can run. The broker listens on a local
#: endpoint owned by ``mcp_gateway.transport`` -- an ``AF_UNIX`` socket on POSIX,
#: a named pipe on Windows -- and delivers each session's poolable stubs over ACP
#: ``session/new`` injection (no bind-mount, no privileged operation), so no
#: platform needs a mount namespace or elevation.
#:
#: Windows is in this set now that the named-pipe transport exists. Two caveats
#: apply there and neither blocks enabling it, because ``mcp_gateway.enabled``
#: defaults to ``False`` and the whole feature is opt-in:
#:
#: * The same-name ``session/new`` override precedence that pooling relies on is
#:   pinned by ``test_mcp_gateway_session_inject`` but is not documented by
#:   kiro-cli. That pinning test now runs on any platform where kiro-cli is on
#:   PATH, so a Windows machine with the CLI installed verifies it; the CI runner
#:   has no kiro-cli, so CI alone cannot.
#: * A macOS peer-*principal* check is still unimplemented (see
#:   ``socketsec``), so macOS falls back to the filesystem gate. Windows does
#:   compare SIDs.
GATEWAY_SUPPORTED_PLATFORMS = ("linux", "darwin", "win32")

#: Import path of the stub entrypoint, and therefore the substring that
#: identifies a stub process by its cmdline (the rewriter emits
#: ``<python> -m kiro_crew.mcp_gateway.stub …``).
#:
#: It lives here, above every submodule, because the producer (``rewriter``) and
#: the cmdline-matching consumers (the Sessions surface's per-session and
#: per-task stub counts) must agree on ONE string: a copy that drifts from the
#: launch line does not fail loudly, it silently reports zero stubs.
STUB_MODULE = "kiro_crew.mcp_gateway.stub"


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
