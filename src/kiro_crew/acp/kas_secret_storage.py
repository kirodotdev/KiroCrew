"""Crew as the KAS engine's store for remote MCP sign-ins (on by default).

When ``agent.kas_persist_mcp_sign_in`` is on (the default), the KAS handshake
declares ``_meta.kiro.secretStorage`` and the engine keeps an MCP server's OAuth
state through three connection-level requests that name no session:
``_kiro/secret/get {key}`` -> ``{value}``, ``_kiro/secret/store {key, value}``
and ``_kiro/secret/delete {key}``. Off (a stored ``false``, any non-``true``
value, or a config read that failed or degraded -- :func:`persistence_enabled`),
nothing is declared, the engine holds every grant in its own memory, and a stray
request is answered -32601 by the reader loop like any other unclaimed method.

The vault is the gateway's, not one person's: every session the gateway runs
reads the same grants. That is the access model the rest of Crew has (a Shared
Crew collaborator can use everything the Crew can) and the one the ``kiro``
backend has for the same sign-ins, which kiro-cli keeps host-wide in
``~/.aws/sso/cache``.

What is stored, and where:

- Only keys of the engine's MCP namespace are accepted,
  ``kiro.mcp.<sha256 hex>.<kind>``. The hash is the engine's own digest of the
  server URL, headers and OAuth config, so a changed server config reads as a
  different key and never inherits a grant. The engine's organization (identity
  provider) sign-in, set by an ``identityProvider`` block in ``mcp.json``, is one
  more key, ``kiro.mcp.idp.<sha256 hex>.session``, hashed from the issuer and
  client id. A stored client secret is ``kiro.mcp.client-secret.<sha256 hex>``
  and the issuer it was accepted by ``kiro.mcp.client-secret-issuer.<sha256 hex>``.
  Any other key, and a value over :data:`MAX_VALUE_CHARS`, is refused,
  so the engine cannot use Crew as a general key-value store.
- ``client``, ``kiro-path-client`` (the client registration, under either kind),
  ``tokens``, ``kiro-path-tokens`` (the sign-in, under either kind), the two
  client-secret keys, and the organization ``session``, persist, in a DEDICATED vault at
  ``<data_home>/kas/mcp-grants/.vault/``. It has its own key
  file and is neither the user Secrets vault (so it is not listed in Settings ->
  Secrets and cannot be granted to a cron) nor the Kiro sign-in vault (so a Kiro
  logout leaves MCP grants alone and the reverse). The whole ``kas`` directory is
  a keystone leaf the agent can neither read nor write, and is hidden from the
  sandbox.
- ``verifier``, ``discovery`` and ``first-party-document-refused`` only matter
  while a sign-in is running, so they stay in this runtime's memory and die with
  the engine process, exactly as every key does with the feature off. So does a
  ``<kind>`` Crew does not know yet (a plain label only), with one WARNING per
  kind per gateway process naming the kind: a newer engine keeps connecting, and
  the line shows a kind that should be saved.

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

from kiro_crew.mcp_grant_kas import PERSISTED_KINDS, TOKENS_KINDS, persistence_enabled

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


#: Kinds that only matter while a sign-in runs, kept in runtime memory.
#: ``first-party-document-refused`` (KAS 0.66.123) records that an authorization
#: server refused Kiro's client-metadata document; losing it costs one retry, and
#: keeping it would hold a stale refusal until a Disconnect.
_MEMORY_KINDS = ("verifier", "discovery", "first-party-document-refused")
#: The engine's MCP OAuth key shape: ``kiro.mcp.<sha256 hex>.<kind>``, any kind.
#: Which kinds are known, persisted or kept in memory is decided after the match.
_KEY_RE = re.compile(r"\Akiro\.mcp\.[0-9a-f]{64}\.([^.]+)\Z")
#: The engine's organization sign-in key: ``kiro.mcp.idp.<sha256 hex>.session``.
#: It holds that sign-in's tokens, so it persists like ``tokens``.
_IDP_SESSION_RE = re.compile(r"\Akiro\.mcp\.idp\.[0-9a-f]{64}\.session\Z")
#: A stored client secret, ``kiro.mcp.client-secret.<sha256 hex>``, and the issuer
#: it was accepted by, ``kiro.mcp.client-secret-issuer.<sha256 hex>`` (KAS 0.66.135,
#: ``client-secret-store.ts``). The hash covers the url and client id. A server
#: with a ``clientId`` reads the secret key on every connect and refuses to
#: connect when the read fails, so both persist.
_CLIENT_SECRET_RE = re.compile(r"\Akiro\.mcp\.client-secret\.[0-9a-f]{64}\Z")
_CLIENT_SECRET_ISSUER_RE = re.compile(r"\Akiro\.mcp\.client-secret-issuer\.[0-9a-f]{64}\Z")
#: Kinds that outlive the engine process. The rest stay in runtime memory.
_PERSISTED_KINDS = frozenset(PERSISTED_KINDS)
_KNOWN_KINDS = _PERSISTED_KINDS | frozenset(_MEMORY_KINDS)
#: Unknown kinds already warned about in this gateway process, so each says so once.
_WARNED_UNKNOWN_KINDS: set[str] = set()
#: Largest value accepted, in characters. A client registration or token set is a
#: few KB; the cap stops a runaway value from bloating a vault that every get
#: re-reads whole.
MAX_VALUE_CHARS = 64 * 1024


#: The entry kinds that ARE the sign-in; the only ones gateway.log reports on.
_TOKENS_KINDS = frozenset(TOKENS_KINDS)
_KEY_PREFIX = "kiro.mcp."
#: How many hex characters of the engine's connection hash name a server in
#: gateway.log. The hash is the engine's digest of the server's connection
#: config, never a token, and a prefix keeps one server's lines easy to follow.
_SERVER_ID_CHARS = 12


class SecretStorageRefused(Exception):
    """A secret request Crew will not serve. The message never holds a value."""


def _is_secret_key(key: str) -> bool:
    return _CLIENT_SECRET_RE.match(key) is not None


def _log_subject(key: str) -> str | None:
    """What a log line calls an already-validated *key*, or ``None`` if it is not logged.

    Only the entries a connection stands or falls on are logged: the tokens and
    a stored client secret. Each is named by a hash prefix, never by its value.
    A client-secret key has no server hash, so it gets its own label.
    """
    if _is_secret_key(key):
        return f"MCP client secret {key.rsplit('.', 1)[-1][:_SERVER_ID_CHARS]}"
    match = _KEY_RE.match(key)
    if match is not None and match.group(1) in _TOKENS_KINDS:
        return f"MCP sign-in for server {key[len(_KEY_PREFIX) :][:_SERVER_ID_CHARS]}"
    return None


def _warn_unknown_kind(kind: str) -> None:
    """Say once per gateway process that *kind* (a plain label) is kept in memory only."""
    if kind in _WARNED_UNKNOWN_KINDS:
        return
    _WARNED_UNKNOWN_KINDS.add(kind)
    logger.warning(
        "MCP grant store: engine key kind %r is not known to Crew; keeping it in this"
        " engine's memory only, so it is not saved across sessions",
        kind,
    )


#: A refused key's last segment is named in the refusal only when it has this
#: shape, so an engine/gateway kind mismatch shows in gateway.log while an
#: arbitrary key (which could carry anything) is never echoed.
_KIND_LABEL_RE = re.compile(r"\A[a-z][a-z0-9-]{0,31}\Z")


def _namespace_refusal(key: str) -> SecretStorageRefused:
    """The refusal for a *key* outside the namespace, naming its kind when safe."""
    if key.startswith(_KEY_PREFIX):
        kind = key.rsplit(".", 1)[-1]
        if _KIND_LABEL_RE.match(kind) is not None:
            return SecretStorageRefused(f"key kind {kind!r} is outside the MCP sign-in namespace")
    return SecretStorageRefused("key is outside the MCP sign-in namespace")


def with_secret_storage(capabilities: dict[str, Any]) -> dict[str, Any]:
    """A copy of *capabilities* that also declares ``_meta.kiro.secretStorage``.

    A copy, never the argument: the harness hands out a module constant, and
    mutating it would turn the declaration on for every later spawn.
    """
    caps = copy.deepcopy(capabilities)
    caps.setdefault("_meta", {}).setdefault("kiro", {})["secretStorage"] = True
    return caps


class McpGrantStore:
    """The dedicated vault holding persisted MCP client registration and ``tokens`` entries."""

    def __init__(self, data_home: str | Path) -> None:
        from kiro_crew.mcp_grant_kas import grant_store_root
        from kiro_crew.secrets import SecretVault

        # One layout, shared with the Connections probe's name-only presence check.
        self._root = grant_store_root(data_home)
        self._kas_dir = self._root.parent
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

    async def delete(self, key: str, revision: str | None = None) -> bool:
        """Delete *key*; with *revision*, only while the entry still has it.

        ``False`` when *revision* does not name the stored entry; ``True``
        otherwise, which without a revision does not mean an entry existed.
        """
        await asyncio.to_thread(self._assert_unlinked)
        if revision is None:
            await self._vault.delete(key)
            return True
        return await self._vault.delete_if_revision(key, revision)


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
        # The last read outcome logged per ``tokens`` key, so an engine that asks
        # again with the same answer adds no line. Bounded like ``_locks``.
        self._logged_read: dict[str, str] = {}

    def _log_read(self, key: str, outcome: str, client_key_outcome: str) -> None:
        subject = _log_subject(key)
        if _is_secret_key(key):
            outcome = client_key_outcome
        if subject is None or self._logged_read.get(key) == outcome:
            return
        self._logged_read[key] = outcome
        logger.info("%s: %s", subject, outcome)

    async def answer(self, method: str, params: Any) -> dict[str, Any]:
        """Build the result for one secret request, or raise :class:`SecretStorageRefused`."""
        if not isinstance(params, dict):
            raise SecretStorageRefused("params must be an object")
        key = params.get("key")
        if not isinstance(key, str):
            raise SecretStorageRefused("key is outside the MCP sign-in namespace")
        if (match := _KEY_RE.match(key)) is not None:
            kind = match.group(1)
            if kind not in _KNOWN_KINDS:
                # A kind a newer engine added: serve it from memory rather than
                # fail the connection, and say so once. Only a plain label is
                # accepted (and named), so a key never echoes arbitrary text.
                if _KIND_LABEL_RE.match(kind) is None:
                    raise _namespace_refusal(key)
                _warn_unknown_kind(kind)
            persisted = kind in _PERSISTED_KINDS
        elif (
            _IDP_SESSION_RE.match(key) is not None
            or _CLIENT_SECRET_RE.match(key) is not None
            or _CLIENT_SECRET_ISSUER_RE.match(key) is not None
        ):
            persisted = True
        else:
            raise _namespace_refusal(key)

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
        try:
            found = await self._guarded(asyncio.to_thread(self._store.get, key))
        except SecretStorageRefused:
            self._log_read(key, "saved sign-in not read (store error)", "not read (store error)")
            raise
        if found is None:
            self._seen[key] = None
            self._log_read(key, "no saved sign-in, the server will ask to sign in", "none saved")
            return None
        value, self._seen[key] = found
        self._log_read(key, "saved sign-in found", "found")
        return str(value)

    async def _store_value(self, key: str, value: str, persisted: bool) -> None:
        if persisted:
            self._seen[key] = await self._guarded(self._store.store(key, value))
            if (subject := _log_subject(key)) is not None:
                logger.info("%s: %s", subject, "saved" if _is_secret_key(key) else "sign-in saved")
            return
        self._memory[key] = value

    async def _delete_persisted(self, key: str) -> None:
        if key not in self._seen:
            await self._guarded(self._store.delete(key))
            outcome = "cleared without a check: this engine never read or saved it"
        elif (revision := self._seen[key]) is None:
            outcome = (
                "clear skipped: this engine holds no saved sign-in (read none, or already cleared)"
            )
        elif await self._guarded(self._store.delete(key, revision)):
            outcome = "cleared: it was the entry this engine last read or saved"
        else:
            outcome = (
                "clear skipped: the saved sign-in is no longer the one this engine"
                " last read or saved (replaced or already cleared)"
            )
        # After a delete this process holds no copy: a repeat delete, before a
        # new read or write, is a no-op rather than an unconditional delete.
        self._seen[key] = None
        # The only record of a clear: the branch tells this engine's own clear
        # from one by an engine that never read the sign-in.
        if (subject := _log_subject(key)) is not None:
            if _is_secret_key(key):
                outcome = outcome.replace("saved sign-in", "saved secret")
            logger.info("%s: %s", subject, outcome)

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
