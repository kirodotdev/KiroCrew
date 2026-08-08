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
The Notes builtin made the same call for the same reason — its PAT sits at
``<crew-home>/workspace/md-notebook/pat`` rather than inside a vault tree the
user can relocate.

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
import contextlib
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.config.paths import config_dir
from kiro_crew.platform_compat import restrict_to_owner

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
    """Directory holding the calendar credential file."""
    if _HOME is not None:
        return _HOME
    return _crew_data_home() / "workspace" / k.APP_NAME


def credentials_file() -> Path:
    """Absolute path of the calendar credential store."""
    return credentials_home() / k.CALENDAR_CREDENTIALS_FILE


def _read_sync() -> dict[str, dict[str, str]]:
    """Load the whole store. A missing or unreadable file reads as empty.

    Corruption degrades to empty rather than raising: the realistic cause is a
    half-written file from a crash, and the recoverable answer is "you are not
    connected, connect again" instead of a settings page that cannot load. The
    file is rewritten wholesale on the next write, so an unparseable one is not
    sticky.
    """
    path = credentials_file()
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("meetings: calendar credential store is unreadable", exc_info=True)
        return {}
    if not isinstance(raw, dict):
        return {}
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
            continue
        out[provider_id] = {
            str(key): str(value)
            for key, value in values.items()
            if isinstance(key, str) and value is not None and not isinstance(value, (dict, list))
        }
    return out


def _write_sync(store: dict[str, dict[str, str]]) -> None:
    """Persist the whole store, owner-only and atomically.

    Write to an owner-only sibling temp, fsync, then replace. A direct
    ``O_TRUNC`` open would empty the existing credentials before the new bytes
    land, so a failure partway — a full disk being the realistic one — would
    destroy a working connection. The temp is created ``0o600`` so the secret is
    never briefly world-readable, and ``restrict_to_owner`` runs on the TEMP
    before the replace so the final file is never briefly permissive (``chmod``
    is close to a no-op on Windows, which is why the helper exists).
    """
    target = credentials_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(store, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            restrict_to_owner(tmp)
        except OSError:
            logger.warning(
                "meetings: could not restrict the calendar credential file to owner-only",
                exc_info=True,
            )
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


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


async def write_for(provider_id: str, values: dict[str, str]) -> None:
    """Merge *values* into one provider's credentials.

    Merge rather than replace so a caller can update one field — the OAuth token
    refresh rewrites the access token and leaves the refresh token alone. An
    empty string CLEARS its key, which is how the settings route lets a user
    remove one field without clearing the whole connection.
    """
    key = (provider_id or "").strip().lower()
    if not key:
        raise ValueError("provider_id must be a non-empty string")

    def _apply() -> None:
        store = _read_sync()
        current = dict(store.get(key, {}))
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

    await asyncio.to_thread(_apply)


async def clear_for(provider_id: str) -> None:
    """Forget one provider's credentials entirely (the disconnect action)."""
    key = (provider_id or "").strip().lower()

    def _apply() -> None:
        store = _read_sync()
        if store.pop(key, None) is not None:
            _write_sync(store)

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
