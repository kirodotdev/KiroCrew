"""Resolve ``secret://NAME`` URIs in MCP server environment variables.

At spawn time, env values matching the ``secret://`` scheme are resolved
against the local :class:`~kiro_crew.secrets.SecretVault`. Resolution is
in-memory only — the sidecar file on disk retains the raw URI template so
the secret is never persisted in plaintext outside the vault.
"""

from __future__ import annotations

import re
from pathlib import Path

from kiro_crew.secrets import SecretVault

_SECRET_URI_RE = re.compile(r"^secret://(.+)$")


def resolve_secret_uris(env: dict[str, str], config_dir: Path) -> tuple[dict[str, str], set[str]]:
    """Return a copy of *env* with ``secret://NAME`` values resolved.

    Returns ``(resolved_env, secret_keys)`` where *secret_keys* is the set
    of env-var names that held a ``secret://`` URI and now contain plaintext.
    The caller MUST clear these keys from the returned dict after the child
    process has been spawned (``exec`` copies them into the child's address
    space) so that plaintext secrets do not linger in parent-process memory.

    Non-matching values pass through unchanged. Raises :exc:`ValueError`
    when a referenced secret does not exist in the vault — failing closed
    prevents an MCP server from starting with a missing credential.

    This function is intentionally synchronous: vault reads are local
    filesystem I/O and the caller (gatewayd spawn path) is already in an
    async context that would need ``await asyncio.to_thread(...)`` for a
    blocking call — keeping this sync lets the caller wrap it once.
    """
    vault = SecretVault(config_dir)
    resolved: dict[str, str] = {}
    secret_keys: set[str] = set()

    for key, value in env.items():
        m = _SECRET_URI_RE.match(value)
        if m is None:
            resolved[key] = value
            continue

        secret_name = m.group(1)
        secret_value = vault.get(secret_name)
        if secret_value is None:
            raise ValueError(
                f"MCP server env var {key!r} references secret://{secret_name} "
                f"but no secret named {secret_name!r} exists in the vault. "
                f"Run `kirocrew secrets set {secret_name}` to store it."
            )
        resolved[key] = secret_value.reveal()
        secret_keys.add(key)

    return resolved, secret_keys
