"""Resolve ``secret://NAME`` URIs in MCP server environment variables.

At spawn time, env values matching the ``secret://`` scheme are resolved
against the local :class:`~kiro_crew.secrets.SecretVault`. Resolution is
in-memory only — the sidecar file on disk retains the raw URI template so
the secret is never persisted in plaintext outside the vault.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.secrets import SecretVault

#: Scheme prefix for a vault secret reference. Single source of truth: the
#: importer (``kiro_crew.secrets.migrate``) writes references with this prefix
#: and the source-provider check reads it, so both import it from here rather
#: than re-spelling the literal.
SECRET_URI_PREFIX = "secret://"
_SECRET_URI_PREFIX = SECRET_URI_PREFIX  # internal alias for existing call sites

#: Validation accepts EVERY name the vault write path can store, rejecting
#: only what can never round-trip: the empty name and names with
#: leading/trailing whitespace (``api_secrets_set`` ``.strip()``s the name
#: before storing, so such a reference can never match a stored entry — see
#: ``kiro_crew.dashboard.handlers.secrets``). No character-class policy is
#: applied here: an interior space, control, format, or private-use character
#: is storable, so refusing it would strand a real vault entry and abort MCP
#: spawn for a name the product's own write path accepted. The log-injection
#: concern that motivates charset filtering is closed at the SINK instead:
#: no error or log line raised by this module ever echoes the secret name —
#: every message names only the operator-declared env-var KEY. With nothing
#: echoed, a hostile name (bidi override, newline, zero-width) has no text to
#: forge.


def _is_valid_secret_name(name: str) -> bool:
    """True if *name* could round-trip through the vault's storage boundary.

    Rejects only the empty name and leading/trailing whitespace (the store
    ``.strip()``s names, so such a reference can never match a stored entry).
    Every storable name — including interior spaces and unusual Unicode — is
    accepted; hostile characters are neutralized by never echoing names in
    errors, not by refusing to resolve them.
    """
    return bool(name) and name == name.strip()


def resolve_secret_uris(env: dict[str, str], config_dir: Path) -> tuple[dict[str, str], set[str]]:
    """Return a copy of *env* with ``secret://NAME`` values resolved.

    Returns ``(resolved_env, secret_keys)`` where *secret_keys* is the set
    of env-var names that held a ``secret://`` URI and now contain plaintext.
    The caller MUST clear these keys from the returned dict after the child
    process has been spawned (``exec`` copies them into the child's address
    space) so that plaintext secrets do not linger in parent-process memory.

    Non-matching values pass through unchanged. Any value beginning with the
    ``secret://`` scheme is treated as a secret reference — including
    ``secret://`` with an empty or malformed name, which fails closed with a
    :exc:`ValueError` rather than passing the literal template through into the
    child's environment. Raises :exc:`ValueError` when a referenced secret does
    not exist in the vault — failing closed prevents an MCP server from
    starting with a missing credential.

    All referenced secrets are read from the vault in a single batch
    (:meth:`SecretVault.get_many`), so K references cost one store load and one
    key read rather than K of each.

    This function is intentionally synchronous: vault reads are local
    filesystem I/O and the caller (gatewayd spawn path) is already in an
    async context that would need ``await asyncio.to_thread(...)`` for a
    blocking call — keeping this sync lets the caller wrap it once.
    """
    resolved: dict[str, str] = {}
    secret_keys: set[str] = set()

    # First pass: classify each env value. Validate every secret reference's
    # name BEFORE touching the vault so a malformed URI fails closed without a
    # store read. ``pending`` maps env-var key -> secret name to resolve.
    pending: dict[str, str] = {}
    for key, value in env.items():
        if not value.startswith(_SECRET_URI_PREFIX):
            resolved[key] = value
            continue

        secret_name = value[len(_SECRET_URI_PREFIX) :]
        if not _is_valid_secret_name(secret_name):
            # Do NOT echo the raw reference: a malformed name can carry
            # control characters (CWE-117 log injection) and this ValueError
            # propagates to the spawn path's logs unsanitised. Name only the
            # env-var key, which is operator-declared config.
            raise ValueError(
                f"MCP server env var {key!r} has a malformed secret:// "
                f"reference: the name after 'secret://' must be non-empty with "
                f"no leading or trailing whitespace (stored names are "
                f"stripped, so such a reference can never match). "
                f"Fix the reference, then store the secret under "
                f"Settings > Secrets in the dashboard (or migrate it with "
                f"`kirocrew secrets import`)."
            )
        pending[key] = secret_name

    if not pending:
        return resolved, secret_keys

    vault = SecretVault(config_dir)
    fetched = vault.get_many(list(pending.values()))

    for key, secret_name in pending.items():
        secret_value = fetched.get(secret_name)
        if secret_value is None:
            raise ValueError(
                f"MCP server env var {key!r} references a secret that does not "
                f"exist in the vault (read the referenced name from the "
                f"server's env config under {key!r}). "
                f"Store it under Settings > Secrets in the dashboard (or "
                f"migrate it with `kirocrew secrets import`)."
            )
        resolved[key] = secret_value.reveal()
        secret_keys.add(key)

    return resolved, secret_keys
