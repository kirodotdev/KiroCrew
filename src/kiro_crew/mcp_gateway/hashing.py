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


#: Env-key prefixes for rotating secrets. Keys starting with any of these are
#: EXCLUDED from :func:`hash_effective_env` so an otherwise-identical pair of
#: stubs still pools together across a credential rotation (the hash would
#: otherwise change on every rotation and split the pool). This is also the
#: single source of truth for env FORWARDING: the rewriter forwards exactly the
#: keys that are NOT scrub-prefixed (the same keys that were hashed), so the
#: forwarded set is provably identical to the hashed set and cannot drift.
#: Secret-prefixed keys are never hashed and never forwarded.
ENV_SCRUB_PREFIXES: tuple[str, ...] = ("AWS_SECRET", "AWS_SESSION", "OAUTH")


def is_scrubbed_env_key(key: str) -> bool:
    """True if ``key`` is a rotating-secret key excluded from both the effective
    env hash and env forwarding (see :data:`ENV_SCRUB_PREFIXES`)."""
    return any(key.startswith(p) for p in ENV_SCRUB_PREFIXES)


def hash_effective_env(env_pairs: dict[str, str]) -> str:
    """Sorted ``K=V\\0``-delimited SHA-256, skipping scrub-prefixed keys.

    Single source of truth for the ``effective_env_hash`` dimension of
    :class:`kiro_crew.mcp_gateway.pool.PoolKey`. The stub hashes its declared
    env through this to register a pool key; the rewriter hashes the same env to
    build the ``MC_MCP_ENV_<SERVER>__<hash>`` forwarding entry that
    ``gatewayd.env_target_resolver`` looks up by that same key. Both call THIS
    function so the mapping can never drift between writer and reader.
    """
    h = hashlib.sha256()
    for k in sorted(env_pairs):
        if is_scrubbed_env_key(k):
            continue
        h.update(k.encode("utf-8"))
        h.update(b"=")
        h.update(str(env_pairs[k]).encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()
