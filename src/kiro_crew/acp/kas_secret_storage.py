"""Crew as the KAS engine's store for remote MCP sign-ins (opt-in).

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
  DEDICATED vault at ``<data_home>/kas/mcp-grants/.vault/``. It has its own key
  file and is neither the user Secrets vault (so it is not listed in Settings ->
  Secrets and cannot be granted to a cron) nor the Kiro sign-in vault (so a Kiro
  logout leaves MCP grants alone and the reverse). The whole ``kas`` directory is
  a keystone leaf the agent can neither read nor write, and is hidden from the
  sandbox.
- ``verifier`` and ``discovery`` only matter while a sign-in is running, so they
  stay in this runtime's memory and die with the engine process, exactly as every
  key does with the feature off.

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
    """The dedicated vault holding persisted MCP ``client`` / ``tokens`` entries."""

    def __init__(self, data_home: str | Path) -> None:
        from kiro_crew.secrets import SecretVault

        self._kas_dir = Path(data_home) / "kas"
        self._root = self._kas_dir / "mcp-grants"
        self._vault = SecretVault(self._root)

    def _assert_unlinked(self) -> None:
        """Refuse to work through a linked ``kas``, ``mcp-grants`` or ``.vault`` dir.

        Before a directory first exists, a link planted at its name could point the
        key file and ciphertext somewhere agent-readable. Same check as the Kiro
        sign-in store makes for ``kas`` and ``kas/.vault``.
        """
        from kiro_crew.platform_compat import is_link_or_junction

        for path in (self._kas_dir, self._root, self._root / ".vault"):
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


def default_grant_store() -> McpGrantStore:
    """The gateway's one grant store, under the data home."""
    from kiro_crew.config.paths import data_home

    return McpGrantStore(data_home())


class SecretStorageServer:
    """Answers one engine process's ``_kiro/secret/*`` requests.

    One per spawned engine: its in-memory half (``verifier`` / ``discovery``)
    must die with that process, as the engine's own memory store would. Like
    that store it has no entry cap: the engine is the host's own child process,
    and each value is still capped at :data:`MAX_VALUE_CHARS`.

    Several engine processes share one persisted grant. A process whose refresh
    fails clears what it read, and that must not erase a fresh grant another
    process stored since. So the server remembers, per persisted key, the
    revision this process last read or wrote (``None``: it read nothing), and a
    delete removes only that entry. A key this process never touched is deleted
    outright: it holds no copy that could be stale.
    """

    def __init__(self, store: McpGrantStore | None = None) -> None:
        self._store = store if store is not None else default_grant_store()
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
        persisted = match.group(1) in _PERSISTED_KINDS

        if persisted:
            async with self._locks.setdefault(key, asyncio.Lock()):
                return await self._answer_key(method, key, params, persisted)
        return await self._answer_key(method, key, params, persisted)

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
