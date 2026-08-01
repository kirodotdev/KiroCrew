"""Stable command-args and effective-env hashing shared across the MCP gateway.

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
from typing import Mapping


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


#: Env-key prefixes treated as ROTATING SECRETS and excluded from the
#: ``effective_env_hash`` PoolKey dimension, so a credential rotation does not
#: split an otherwise-identical pool.
#:
#: The exclusion has a second, security-critical consequence: it makes the hash
#: NON-INJECTIVE over these keys. Two sessions whose only difference is an
#: ``AWS_SECRET*`` value collide onto the same hash and therefore SHARE one
#: backend — so there is no single correct value for a secret-prefixed key in a
#: pooled backend, and one must never be forwarded into it. Servers that need a
#: per-session secret read it from disk (the platform credential helper / the
#: provider's default credential chain, unchanged by pooling) or stay ``poolable: false``.
ENV_SCRUB_PREFIXES: tuple[str, ...] = ("AWS_SECRET", "AWS_SESSION", "OAUTH")


def is_secret_env_key(key: str) -> bool:
    """Return ``True`` if ``key`` is a rotating-secret key.

    Single source of truth for the scrub decision, shared by the stub (which
    excludes these keys when hashing) and by ``gatewayd`` (which excludes them
    when forwarding declared env to a pooled backend). Sharing it is what keeps
    "every forwarded key is also a hashed key" a checkable invariant rather than
    a comment in two files.

    Forwarding applies a SECOND, independent filter on top of this one —
    ``manager.is_credential_env_key`` — so the forwarded set is a strict subset
    of the hashed set: keys the daemon's own credential scrub removes
    (``AWS_ACCESS``, ``SSH_AUTH_SOCK``, ``GNUPGHOME``, ``GIT_ASKPASS``) are in
    the hash but are still never forwarded.
    """
    return any(key.startswith(prefix) for prefix in ENV_SCRUB_PREFIXES)


def non_secret_env(env_pairs: Mapping[str, str]) -> dict[str, str]:
    """Return ``env_pairs`` minus every :func:`is_secret_env_key` entry.

    This is the set folded into :func:`hash_effective_env`, and the OUTER bound
    on what may be applied to a shared pooled backend. Because these keys are
    part of the PoolKey, every session sharing a backend agrees on their values,
    so applying them at spawn cannot make one co-tenant observe another's
    configuration.

    It is not sufficient on its own: the forwarding path in ``gatewayd`` also
    drops ``manager.is_credential_env_key`` matches, so a declared credential
    key that the daemon scrub removes is never re-introduced.
    """
    return {k: v for k, v in env_pairs.items() if not is_secret_env_key(k)}


def hash_effective_env(env_pairs: Mapping[str, str]) -> str:
    """Sorted ``K=V\\0``-delimited SHA-256 over the NON-SECRET env pairs.

    Feeds the ``effective_env_hash`` dimension of
    :class:`kiro_crew.mcp_gateway.pool.PoolKey`. Implemented on top of
    :func:`non_secret_env` so the hashed set and the forwardable set are the
    same set by construction.
    """
    filtered = non_secret_env(env_pairs)
    h = hashlib.sha256()
    for k in sorted(filtered):
        h.update(k.encode("utf-8"))
        h.update(b"=")
        h.update(filtered[k].encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()
