"""Credential store for the calendar providers that need one.

Three of the shipped calendar providers authenticate: CalDAV with a username and
password, Google and Microsoft 365 with OAuth tokens. None of those values can
live in ``config.json`` — that file is the app's ordinary, agent-readable
configuration, and a live password or refresh token in it would be readable by
any agent with file tools. So they live here instead, behind
:func:`kiro_crew.security.is_sensitive_path`'s floor.

Three properties are load-bearing, and each is a decision rather than a default:

**The file is NOT under ``app_data_dir("meetings")``.** That tree is the one
:func:`store.contain` deliberately opens up: it is where meeting directories go,
where agents write their own output files, and what every containment check is
bounded against. Keeping a live credential entirely outside it removes a whole
class of "could an agent be handed this path" argument rather than answering it.

**The directory is a TOP-LEVEL leaf of the crew data home**
(``<crew-home>/meetings-credentials/``), not one nested under the agent-writable
``workspace/``. The tool gate above only fences agent FILE TOOLS; a spawned
shell's ``open()`` is stopped by the OS sandbox mask
(``sandbox._CREW_HIDDEN_LEAVES``), and that mask has two properties a nested
leaf cannot satisfy. It binds only over a directory that already exists when the
namespace is spawned -- and this directory is created on the FIRST credential
save, so a session started before it would have watched the file appear inside
its own view -- and only a direct child of the data home can be materialised
ahead of every spawn (``sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES``), because a
mask covers the leaf and not its ancestors, so an agent-writable parent could be
renamed out from under it. The Notes builtin's staging directory moved to the top
level for the same reason.

**The path is NOT threaded through the ``root`` parameter** that every other
function in :mod:`.store` takes for test isolation. That parameter is reachable
from request handling, and a secret store whose location can be steered by a
request is not a secret store. Tests override the path through
:func:`set_credentials_home`, which is module state a request cannot reach.

**Reads and writes bypass the shared file gate.** ``hooks.safe_read_file_bytes``
would refuse this path, because registering the leaf in
``security._CREW_SECRET_LEAVES`` is precisely what makes it refuse. This module
therefore opens the file directly — the same exemption every other credential
store in the product relies on, and the reason each one is registered as a leaf
individually rather than the whole directory being waved through.

Values are never returned to the browser. The settings route publishes only the
booleans from :func:`credential_status`, so a stored password or refresh token
has no read path back out through the API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.platform_compat import is_link_or_junction, restrict_to_owner

logger = logging.getLogger("kirocrew.app.meetings")

#: Test hook. ``None`` in production, so the path always resolves under the live
#: crew data home. Set by :func:`set_credentials_home`.
_HOME: Path | None = None


def set_credentials_home(home: Path | None) -> None:
    """Point the credential store at *home* (tests), or restore the default.

    Exists so a test never touches the developer's real credential file. It is
    module state on purpose — see this module's docstring for why the path is not
    a parameter.
    """
    global _HOME
    _HOME = home


def _crew_data_home() -> Path:
    """The crew data home, resolved lazily.

    ``config_dir`` is imported at module scope but CALLED here: resolving the data
    home at import time is forbidden (it would freeze ``KIROCREW_HOME`` at the
    wrong moment), and calling it per use honors the override every time. The
    fallback mirrors the Notes builtin's, so a broken resolver degrades to the
    documented default rather than raising out of a settings read.
    """
    try:
        return config_dir()
    except Exception:
        override = os.environ.get("KIROCREW_HOME")
        return Path(override) if override else Path.home() / ".kiro" / "crew"


def credentials_home() -> Path:
    """Directory holding the calendar credential file.

    A direct child of the crew data home -- see this module's docstring for why
    it is not nested under ``workspace/``.
    """
    if _HOME is not None:
        return _HOME
    return _crew_data_home() / k.CALENDAR_CREDENTIALS_DIR_LEAF


def credentials_file() -> Path:
    """Absolute path of the calendar credential store."""
    return credentials_home() / k.CALENDAR_CREDENTIALS_FILE


def _ensure_home() -> Path:
    """Create the store's directory if absent and prove it is a real directory.

    On a sandboxed host the directory already exists by the time any agent runs:
    the sandbox materialises it (empty, ``0o700``) before every namespace spawn
    (``sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES``), so the ``mkdir`` here matters
    only where nothing masks anything (sandbox off, Windows) and is a no-op
    otherwise. No ``parents=True``: the leaf sits directly under the data home,
    which exists for as long as the gateway does, and a missing parent is a
    real error rather than a tree to mint on the spot.

    A link planted at the name would put every write outside the mask, and no
    per-file check can see that from inside; refused before AND after the
    ``mkdir``, because ``exist_ok=True`` happily accepts a pre-existing link.
    """
    home = credentials_home()
    if is_link_or_junction(home):
        raise OSError(f"calendar credential directory is not a real directory: {home}")
    home.mkdir(mode=0o700, exist_ok=True)
    if is_link_or_junction(home) or not home.is_dir():
        raise OSError(f"calendar credential directory is not a real directory: {home}")
    return home


#: Serialises every read-modify-write of the store. Module-level because the
#: file is the shared resource, and `asyncio.to_thread` hands each call a
#: different worker thread — an asyncio lock would not bind them.
_STORE_LOCK = threading.Lock()

#: Process-local registration revision per provider. Pending OAuth flows are
#: process-local too, so a restart invalidates both together. Mutated only while
#: ``_STORE_LOCK`` is held.
_STORE_GENERATIONS: dict[str, int] = {}


class _StoreUnreadable(RuntimeError):
    """The store exists but could not be read or parsed.

    Deliberately distinct from "no file yet". A reader can treat both as empty; a
    read-modify-write cannot, because rebuilding the file from a partial view
    deletes whatever it could not see.
    """


def _load_sync() -> dict[str, dict[str, str]]:
    """Load the whole store. Missing reads as empty; unreadable RAISES.

    Callers choose how to degrade: :func:`_read_sync` swallows for display,
    :func:`write_for` / :func:`clear_for` let it propagate so a merge never runs
    against a view it knows is incomplete.
    """
    path = credentials_file()
    try:
        if is_link_or_junction(path.parent):
            # A link at the directory would resolve every read through an
            # attacker-chosen tree while the lexical name stays the same. The
            # write path refuses it in :func:`_ensure_home`; refuse the read too,
            # as an unreadable store rather than a crash.
            raise OSError(f"calendar credential directory is not a real directory: {path.parent}")
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("meetings: calendar credential store is unreadable", exc_info=True)
        # Distinguishable from "no file yet". Reading for DISPLAY may treat both as
        # empty — that is the trade the docstring describes — but a
        # read-modify-write must not: an unreadable store read as ``{}`` makes the
        # merge below rebuild the file from one provider and the atomic replace
        # then deletes every other provider's credentials for good. The write path
        # re-raises this instead (see :func:`_read_sync_for_update`).
        raise _StoreUnreadable(str(path)) from exc
    if not isinstance(raw, dict):
        # Parsed, but not a store. The same contract as a parse failure: a
        # read-modify-write that treated this as empty would rebuild the file
        # from nothing and destroy whatever a human could still recover from
        # the malformed content. Display reads degrade via :func:`_read_sync`.
        logger.warning("meetings: calendar credential store has an invalid root")
        raise _StoreUnreadable(str(path))
    # Re-assert owner-only access on load. A file left with a permissive ACL —
    # created before this lockdown, or by another principal on Windows — would
    # otherwise stay readable by other OS accounts. Warn rather than fail: losing
    # the user's calendar connection is worse than a warning.
    try:
        restrict_to_owner(path)
    except OSError:
        logger.warning(
            "meetings: could not restrict the calendar credential file to owner-only",
            exc_info=True,
        )
    out: dict[str, dict[str, str]] = {}
    for provider_id, values in raw.items():
        if not isinstance(provider_id, str) or not isinstance(values, dict):
            # A skipped entry here is not "tolerated", it is INVISIBLE — and the
            # next write would persist the view without it, deleting the entry
            # for good. Schema-invalid entries poison the whole read, exactly
            # like an unparseable file (:class:`_StoreUnreadable`): this module
            # only ever writes ``dict[str, dict[str, str]]``, so anything else
            # is corruption or an external edit, and neither may be silently
            # rewritten away.
            logger.warning("meetings: calendar credential store has an invalid entry")
            raise _StoreUnreadable(str(path))
        entry: dict[str, str] = {}
        for key, value in values.items():
            if not isinstance(key, str) or not isinstance(value, str):
                logger.warning("meetings: calendar credential store has an invalid value")
                raise _StoreUnreadable(str(path))
            entry[key] = value
        out[provider_id] = entry
    return out


def _read_sync() -> dict[str, dict[str, str]]:
    """Load the store for READING. A missing or unreadable file reads as empty.

    Corruption degrades to empty rather than raising: the realistic cause is a
    half-written file from a crash, and the recoverable answer is "you are not
    connected" instead of a settings page that cannot load. It is NOT
    self-healing, deliberately: the write path refuses while the file is
    unreadable (see :func:`_load_sync`), so the malformed file stays on disk
    until a human repairs it or deletes it and connects again. That is the
    trade -- a sticky "not connected" that keeps whatever could still be
    salvaged, over a clean slate that silently destroys every other provider's
    tokens.

    This is the DISPLAY path only. The write path calls :func:`_load_sync`
    directly, because the same degradation there turns one unreadable file into
    the permanent loss of every other provider's credentials.
    """
    try:
        return _load_sync()
    except _StoreUnreadable:
        return {}


def _write_sync(store: dict[str, dict[str, str]]) -> None:
    """Persist the whole store, owner-only and atomically.

    Routed through :func:`kiro_crew.atomic_write.atomic_write` — the repo's one
    audited implementation of temp + fsync + replace — with
    ``restrict_to_owner=True`` and the default fail-closed policy. Two properties
    of the shared helper matter here and are why a hand-rolled copy was wrong:

    * The temp is restricted to the owner BEFORE any content reaches it, so the
      tokens never exist in a file readable under an inherited Windows ACL.
    * A lockdown failure REFUSES the write (``restrict_on_error="raise"``): a
      credential this module cannot protect is not published at all. That is the
      opposite trade from the READ path's warn-and-continue, deliberately — a
      refused write leaves the previous file intact and the connection working,
      while a published world-readable token is unrecoverable.
    """
    target = credentials_file()
    _ensure_home()
    atomic_write(
        target,
        json.dumps(store, indent=2, sort_keys=True),
        fsync=True,
        restrict_to_owner=True,
    )


async def read_all() -> dict[str, dict[str, str]]:
    """Every provider's stored credentials. Off-loop — this touches the disk."""
    return await asyncio.to_thread(_read_sync)


async def read_for(provider_id: str) -> dict[str, str]:
    """Stored credentials for one provider, or an empty dict.

    Providers call this from ``fetch()`` rather than from their constructor:
    :func:`providers.calendar.available_calendar_providers` builds every
    registered factory with an empty source just to read its label, so a
    constructor that touched this file would put a disk read behind rendering the
    settings picker.
    """
    store = await read_all()
    return dict(store.get((provider_id or "").strip().lower(), {}))


async def read_for_with_generation(provider_id: str) -> tuple[dict[str, str], int]:
    """Read one provider and its registration generation in one transaction."""
    key = (provider_id or "").strip().lower()

    def _read() -> tuple[dict[str, str], int]:
        with _STORE_LOCK:
            store = _load_sync()
            return dict(store.get(key, {})), _STORE_GENERATIONS.get(key, 0)

    return await asyncio.to_thread(_read)


async def write_for(
    provider_id: str,
    values: dict[str, str],
    *,
    only_if_refresh_token: str | None = None,
    only_if_client_id: str | None = None,
    only_if_generation: int | None = None,
) -> bool:
    """Merge *values* into one provider's credentials.

    Merge rather than replace so a caller can update one field — the OAuth token
    refresh rewrites the access token and leaves the refresh token alone. An
    empty string CLEARS its key, which is how the settings route lets a user
    remove one field without clearing the whole connection.

    The optional preconditions cover the two OAuth races this store actually
    has: a refresh writes only while the refresh token it used is current, and
    an authorization exchange writes only while its client registration and
    generation are current. Each comparison and write shares ``_STORE_LOCK``, so
    neither flow can restore credentials after a disconnect or overwrite newer
    credentials, including a reconnect that reuses the same client id.
    """
    key = (provider_id or "").strip().lower()
    if not key:
        raise ValueError("provider_id must be a non-empty string")

    def _apply() -> bool:
        # One lock for the whole read-modify-write. Both halves run in a worker
        # thread, so a settings PUT and an OAuth token refresh really do
        # interleave: each read the same snapshot, each merged its own field, and
        # the later atomic replace silently discarded the earlier one — losing a
        # freshly minted refresh token, which cannot be re-derived. The unreadable
        # store is allowed to propagate for the reason on :class:`_StoreUnreadable`.
        with _STORE_LOCK:
            store = _load_sync()
            current = dict(store.get(key, {}))
            if (
                only_if_generation is not None
                and _STORE_GENERATIONS.get(key, 0) != only_if_generation
            ):
                return False
            if (
                only_if_refresh_token is not None
                and current.get("refresh_token", "") != only_if_refresh_token
            ):
                return False
            if (
                only_if_client_id is not None
                and current.get("client_id", "") != only_if_client_id
            ):
                return False
            for field, value in values.items():
                text = "" if value is None else str(value)
                if text == "":
                    current.pop(field, None)
                else:
                    current[field] = text
            if current:
                store[key] = current
            else:
                store.pop(key, None)
            _write_sync(store)
            if (
                only_if_generation is None
                and only_if_refresh_token is None
                and only_if_client_id is None
            ):
                _STORE_GENERATIONS[key] = _STORE_GENERATIONS.get(key, 0) + 1
            return True

    return await asyncio.to_thread(_apply)


async def clear_for(provider_id: str) -> None:
    """Forget one provider's credentials entirely (the disconnect action)."""
    key = (provider_id or "").strip().lower()

    def _apply() -> None:
        # Same transaction boundary as `write_for`: a disconnect that raced a
        # token refresh would otherwise resurrect the provider it just removed.
        with _STORE_LOCK:
            store = _load_sync()
            if store.pop(key, None) is not None:
                _write_sync(store)
            _STORE_GENERATIONS[key] = _STORE_GENERATIONS.get(key, 0) + 1

    await asyncio.to_thread(_apply)


async def credential_status() -> dict[str, dict[str, Any]]:
    """Which providers have credentials, and which fields are present.

    This is the ONLY shape that reaches the browser: field NAMES and booleans,
    never a value. A settings page needs to render "connected" and "which of
    these did you fill in", and neither question needs the secret.
    """
    store = await read_all()
    return {
        provider_id: {
            "configured": True,
            "fields": sorted(values.keys()),
        }
        for provider_id, values in store.items()
        if values
    }
