"""Stable command-args hashing shared across the MCP gateway.

Kept in its own dependency-free leaf module (only ``hashlib``) so every caller
imports it at module top level. The lightweight ``rewriter`` sits on
``config.loader``'s import path, while ``pool`` and ``stub`` are asyncio/socket
-heavy submodules that must stay unloaded until the gateway is actually enabled
(``test_loader_does_not_import_mcp_gateway_at_module_load``). Routing the shared
hash through this leaf lets the rewriter import it directly without dragging
those heavy submodules into CLI/test/MCP startup.
"""

from __future__ import annotations

import hashlib


def hash_command(command: str, args: list[str]) -> str:
    """SHA-256 over ``command\\0`` + each ``arg\\0``.

    Single source of truth for the ``command_args_hash`` dimension of
    :class:`kiro_crew.mcp_gateway.pool.PoolKey`. The stub hashes its
    ``--target-command`` + split ``--target-args`` through this to register a
    pool key; the rewriter hashes the same inputs to build the
    ``MC_MCP_TARGET_<SERVER>__<hash>`` env entry that
    ``gatewayd.env_target_resolver`` looks up by that same key. Both call THIS
    function so the wire-format can never drift between writer and reader.
    """
    h = hashlib.sha256()
    h.update(command.encode("utf-8"))
    h.update(b"\0")
    for a in args:
        h.update(a.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()
