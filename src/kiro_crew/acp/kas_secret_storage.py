"""Crew as the KAS engine's per-user store for remote MCP sign-ins (opt-in).

When ``agent.kas_persist_mcp_sign_in`` is on, the KAS handshake declares
``_meta.kiro.secretStorage`` and the engine keeps an MCP server's OAuth state
through three connection-level requests that name no session:
``_kiro/secret/get {key}`` -> ``{value}``, ``_kiro/secret/store {key, value}``
and ``_kiro/secret/delete {key}``. Off (the default), nothing is declared, the
engine holds every grant in its own memory, and a stray request is answered
-32601 by the reader loop like any other unclaimed method.

What is stored, and where:

- Only keys of the engine's MCP namespace are accepted,
  ``kiro.mcp.<sha256 hex>.(client|tokens|verifier|discovery)``. The hash is the
  engine's own digest of the server URL, headers and OAuth config, so a changed
  server config reads as a different key and never inherits a grant. Any other
  key, and a value over :data:`MAX_VALUE_CHARS`, is refused, so the engine cannot
  use Crew as a general key-value store.
- ``client`` (the dynamic client registration) and ``tokens`` persist, in a
  DEDICATED vault per authenticated user at
  ``<data_home>/kas/mcp-grants/principals/<sha256 of the principal>/.vault/``.
  The principal is the one :mod:`kiro_crew.mcp_grant_principals` bound to
  the session key this engine process was spawned for, read once at the
  handshake. A process with no principal declares no storage at all, so a
  grant stored for one user is never served to a session another user, a
  channel, a cron or an unattributed start opened. Each vault has its own key
  file and is neither the user Secrets vault (so it is not listed in Settings ->
  Secrets and cannot be granted to a cron) nor the Kiro sign-in vault (so a Kiro
  logout leaves MCP grants alone and the reverse). The whole ``kas`` directory is
  a keystone leaf the agent can neither read nor write, and is hidden from the
  sandbox.
- ``verifier`` and ``discovery`` only matter while a sign-in is running, so they
  stay in this runtime's memory and die with the engine process, exactly as every
  key does with the feature off.
- When the session key stops being bound to the process's principal (a second
  user drove it), persisted kinds fall back to this process's memory for the
  rest of its life: nothing more is read from or written to the vault.
- The gateway-wide vault an earlier build wrote at
  ``<data_home>/kas/mcp-grants/.vault/`` names no user, so it is never read:
  its grants are not migrated to anyone, and each user signs in again.

Values never leave this module except as the ``get`` answer on the engine's own
pipe: they are not logged (failures log an exception TYPE only), not put in an
error message, and the reader loop does not hand these frames to the opt-in raw
frame recorder.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import re
from pathlib import Path
from typing import Any

from kiro_crew import mcp_grant_principals

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_VALUE_CHARS",
    "METHOD_SECRET_DELETE",
    "METHOD_SECRET_GET",
    "METHOD_SECRET_STORE",
    "SECRET_METHODS",
    "McpGrantStore",
    "SecretStorageRefused",
    "SecretStorageServer",
    "is_secret_method",
    "persistence_enabled",
    "with_secret_storage",
]

METHOD_SECRET_GET = "_kiro/secret/get"
METHOD_SECRET_STORE = "_kiro/secret/store"
METHOD_SECRET_DELETE = "_kiro/secret/delete"
SECRET_METHODS: frozenset[str] = frozenset(
    {METHOD_SECRET_GET, METHOD_SECRET_STORE, METHOD_SECRET_DELETE}
)


def is_secret_method(method: object) -> bool:
    """Is *method* one of the secret calls? Safe on any frame value.

    A frame's ``method`` is engine-controlled and may be a list or an object,
    which a set lookup would raise on, inside the reader loop that every
    multiplexed session shares.
    """
    return isinstance(method, str) and method in SECRET_METHODS


#: The engine's MCP OAuth key shape: ``kiro.mcp.<sha256 hex>.<kind>``.
_KEY_RE = re.compile(r"\Akiro\.mcp\.[0-9a-f]{64}\.(client|tokens|verifier|discovery)\Z")
#: Kinds that outlive the engine process. The rest stay in runtime memory.
_PERSISTED_KINDS = frozenset({"client", "tokens"})
#: Largest value accepted, in characters. A client registration or token set is a
#: few KB; the cap stops a runaway value from bloating a vault that every get
#: re-reads whole.
MAX_VALUE_CHARS = 64 * 1024


class SecretStorageRefused(Exception):
    """A secret request Crew will not serve. The message never holds a value."""


def persistence_enabled() -> bool:
    """``agent.kas_persist_mcp_sign_in`` as configured. Blocking; call off the loop.

    Any failure to read config is OFF: the feature is opt-in, so a broken config
    keeps today's in-memory behaviour rather than starting to store tokens.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        return bool(KiroCrewConfig.load().agent.kas_persist_mcp_sign_in)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "MCP sign-in persistence stays off: config unreadable (%s)", type(exc).__name__
        )
        return False


def with_secret_storage(capabilities: dict[str, Any]) -> dict[str, Any]:
    """A copy of *capabilities* that also declares ``_meta.kiro.secretStorage``.

    A copy, never the argument: the harness hands out a module constant, and
    mutating it would turn the declaration on for every later spawn.
    """
    caps = copy.deepcopy(capabilities)
    caps.setdefault("_meta", {}).setdefault("kiro", {})["secretStorage"] = True
    return caps


class McpGrantStore:
    """One user's dedicated vault holding persisted MCP ``client`` / ``tokens`` entries."""

    def __init__(self, data_home: str | Path, principal: str) -> None:
        from kiro_crew.secrets import SecretVault

        if not isinstance(principal, str) or not principal:
            # A store with no user would be the gateway-wide namespace that the
            # per-user layout replaces.
            raise SecretStorageRefused("MCP grant store needs a principal")
        self._kas_dir = Path(data_home) / "kas"
        self._grants_dir = self._kas_dir / "mcp-grants"
        self._principals_dir = self._grants_dir / "principals"
        self._root = self._principals_dir / mcp_grant_principals.principal_dir_name(principal)
        self._vault = SecretVault(self._root)

    def _assert_unlinked(self) -> None:
        """Refuse to work through a linked directory anywhere on the vault's path.

        Before a directory first exists, a link planted at its name could point the
        key file and ciphertext somewhere agent-readable. Same check as the Kiro
        sign-in store makes for ``kas`` and ``kas/.vault``.
        """
        from kiro_crew.platform_compat import is_link_or_junction

        for path in (
            self._kas_dir,
            self._grants_dir,
            self._principals_dir,
            self._root,
            self._root / ".vault",
        ):
            if is_link_or_junction(path):
                raise SecretStorageRefused("MCP grant store directory is a link")

    def get(self, key: str) -> tuple[str, str] | None:
        """``(value, revision)`` stored for *key*, or ``None``. Blocking.

        The revision is a digest of the stored ciphertext, never of the value.
        """
        self._assert_unlinked()
        found = self._vault.get_with_revision(key)
        return None if found is None else (found[0].reveal(), found[1])

    async def store(self, key: str, value: str) -> str:
        """Store *value*; return the revision of the entry this call wrote."""
        await asyncio.to_thread(self._assert_unlinked)
        return await self._vault.set_with_revision(key, value)

    async def delete(self, key: str, revision: str | None = None) -> None:
        """Delete *key*; with *revision*, only while the entry still has it."""
        await asyncio.to_thread(self._assert_unlinked)
        if revision is None:
            await self._vault.delete(key)
        else:
            await self._vault.delete_if_revision(key, revision)


def default_grant_store(principal: str) -> McpGrantStore:
    """*principal*'s grant store, under the data home."""
    from kiro_crew.config.paths import data_home

    return McpGrantStore(data_home(), principal)


class SecretStorageServer:
    """Answers one engine process's ``_kiro/secret/*`` requests.

    One per spawned engine: its in-memory half (``verifier`` / ``discovery``)
    must die with that process, as the engine's own memory store would. Like
    that store it has no entry cap: the engine is the host's own child process,
    and each value is still capped at :data:`MAX_VALUE_CHARS`.

    It is bound to one principal and to the session key the process was
    spawned for. Before every persisted read, write or delete it re-checks that
    the key is still bound to that principal; once it is not, persisted kinds
    are served from this process's memory instead, so the vault is never read
    or written on behalf of a session another user drove.

    Several engine processes of one principal share one persisted grant. A process whose refresh
    fails clears what it read, and that must not erase a fresh grant another
    process stored since. So the server remembers, per persisted key, the
    revision this process last read or wrote (``None``: it read nothing), and a
    delete removes only that entry. A key this process never touched is deleted
    outright: it holds no copy that could be stale.
    """

    def __init__(
        self, principal: str, session_key: str, store: McpGrantStore | None = None
    ) -> None:
        self._principal = principal
        self._session_key = session_key
        # The exact binding this process was spawned under, generation included:
        # a binding evicted and made again is a different one, even for this
        # principal, so it can never re-authorize this process.
        found = mcp_grant_principals.binding_for(session_key)
        self._binding = found if found is not None and found[0] == principal else None
        self._revoked = self._binding is None
        self._store = store if store is not None else default_grant_store(principal)
        self._memory: dict[str, str] = {}
        self._seen: dict[str, str | None] = {}
        # The reader loop answers each request in its own task, so two requests
        # for one key can overlap; each persisted key's read-or-write and the
        # revision it records happen under that key's lock. Bounded by the
        # namespace: one lock per key this engine process has touched.
        self._locks: dict[str, asyncio.Lock] = {}

    async def answer(self, method: str, params: Any) -> dict[str, Any]:
        """Build the result for one secret request, or raise :class:`SecretStorageRefused`."""
        if not isinstance(params, dict):
            raise SecretStorageRefused("params must be an object")
        key = params.get("key")
        if not isinstance(key, str) or (match := _KEY_RE.match(key)) is None:
            raise SecretStorageRefused("key is outside the MCP sign-in namespace")
        persisted = match.group(1) in _PERSISTED_KINDS and self.still_bound()

        if persisted:
            async with self._locks.setdefault(key, asyncio.Lock()):
                # Re-checked once the lock is held: a request queued behind
                # another for this key may have waited out a conflict.
                return await self._answer_key(method, key, params, self.still_bound())
        return await self._answer_key(method, key, params, persisted)

    def still_bound(self) -> bool:
        """Whether this process may still use its principal's vault.

        Latched: once the session key is seen not bound to the principal (a
        conflict, or an eviction from the bounded registry), the answer stays
        False for the life of this process, so a later re-bind cannot reopen the
        vault to an engine another user has driven in the meantime.
        """
        if not self._revoked and (
            mcp_grant_principals.binding_for(self._session_key) == self._binding
        ):
            return True
        if not self._revoked:
            self._revoked = True
            logger.warning("MCP grant vault not used: the session is no longer bound to its user")
        return False

    def release(self, params: Any, result: dict[str, Any]) -> dict[str, Any]:
        """*result* as it may be sent now, re-checked at the moment of sending.

        A vault value read while the session was bound is withheld when the
        binding is gone by the time the answer is written: the engine gets this
        process's own memory copy instead (``{}`` when it has none). Values from
        memory, and every non-``get`` answer, pass through unchanged.
        """
        if "value" not in result or self.still_bound():
            return result
        key = params.get("key") if isinstance(params, dict) else None
        if not isinstance(key, str):
            return result
        match = _KEY_RE.match(key)
        if match is None or match.group(1) not in _PERSISTED_KINDS:
            return result
        own = self._memory.get(key)
        if result["value"] == own:
            return result
        return {} if own is None else {"value": own}

    async def _answer_key(
        self, method: str, key: str, params: dict[str, Any], persisted: bool
    ) -> dict[str, Any]:
        if method == METHOD_SECRET_GET:
            value = await self._get(key, persisted)
            return {} if value is None else {"value": value}
        if method == METHOD_SECRET_STORE:
            value = params.get("value")
            if not isinstance(value, str):
                raise SecretStorageRefused("value must be a string")
            if len(value) > MAX_VALUE_CHARS:
                raise SecretStorageRefused("value is too large")
            await self._store_value(key, value, persisted)
            return {}
        if method == METHOD_SECRET_DELETE:
            self._memory.pop(key, None)
            if persisted:
                await self._delete_persisted(key)
            return {}
        raise SecretStorageRefused("unknown secret method")

    async def _get(self, key: str, persisted: bool) -> str | None:
        if not persisted:
            return self._memory.get(key)
        found = await self._guarded(asyncio.to_thread(self._store.get, key))
        if not self.still_bound():
            # The session was conflicted while the vault was read: the value
            # must not reach an engine another user now drives.
            return self._memory.get(key)
        if found is None:
            self._seen[key] = None
            return None
        value, self._seen[key] = found
        return str(value)

    async def _store_value(self, key: str, value: str, persisted: bool) -> None:
        if persisted:
            self._seen[key] = await self._guarded(self._store.store(key, value))
            return
        self._memory[key] = value

    async def _delete_persisted(self, key: str) -> None:
        if key not in self._seen:
            await self._guarded(self._store.delete(key))
        else:
            revision = self._seen[key]
            if revision is not None:
                await self._guarded(self._store.delete(key, revision))
        # After a delete this process holds no copy: a repeat delete, before a
        # new read or write, is a no-op rather than an unconditional delete.
        self._seen[key] = None

    @staticmethod
    async def _guarded(awaitable: Any) -> Any:
        """Await a vault call; turn any failure into a value-free refusal.

        A vault error's text can name a path; the engine gets only the category,
        and the local log gets only the exception type.
        """
        try:
            return await awaitable
        except SecretStorageRefused:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP grant store failed (%s)", type(exc).__name__)
            raise SecretStorageRefused("MCP grant store error") from None
