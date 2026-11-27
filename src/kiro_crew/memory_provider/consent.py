"""The owner's consent to send memory to a provider that keeps it elsewhere.

Recorded in the keystone ``memory_provider_consent.json``
(:func:`kiro_crew.config.loader_locations.memory_provider_consent_path`): READONLY in
every sandbox mode, written only by the owner's ``PUT /api/memory/provider``. A grant
names one provider id, the app version the owner agreed to (so an update asks again)
and the install's ``installedAt`` (so another app installed later under the same name
and version asks again). Every read fails soft to no consent.
"""

from __future__ import annotations

import calendar
import contextlib
import json
import logging
import threading
import time
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger("kiro_crew.memory_provider")

_MAX_GRANTS = 32
_write_lock = threading.Lock()


@contextlib.contextmanager
def _rewrite_lock() -> Iterator[None]:
    """Held across each read-modify-write: a lock every process takes (an install or
    uninstall run from the CLI rewrites the record too), then the thread lock.

    The process lock comes first, so a thread waiting for it holds nothing: a caller
    on the event loop, whose acquire is single-shot, never waits on the thread lock
    longer than one rewrite.
    """
    from kiro_crew.apps.protected_app_state import memory_consent_lock

    with memory_consent_lock(), _write_lock:
        yield


def _path() -> Any:
    from kiro_crew.config.loader_locations import memory_provider_consent_path

    return memory_provider_consent_path()


def read_grants() -> list[dict[str, Any]]:
    """The grants, for a reader: any failure reads as no consent (the narrow answer)."""
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return _valid(raw)


def _grants_for_rewrite() -> list[dict[str, Any]]:
    """The grants a rewrite starts from. Only a missing file reads as empty.

    Any other failure (EACCES, EIO, a record that is not JSON) raises, so a transient
    error never publishes an empty record over the owner's other consents.
    """
    try:
        text = _path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return _valid(json.loads(text)) if text.strip() else []


def _valid(raw: Any) -> list[dict[str, Any]]:
    grants = raw.get("grants") if isinstance(raw, dict) else None
    if not isinstance(grants, list):
        return []
    return [
        g
        for g in grants
        if isinstance(g, dict)
        and isinstance(g.get("provider_id"), str)
        and isinstance(g.get("app_version"), str)
    ]


def has_egress_consent(provider: Any) -> bool:
    """Whether the owner agreed to send memory to this install of *provider* at its version."""
    provider_id = getattr(provider, "provider_id", "")
    version = getattr(provider, "app_version", "")
    installed_at = getattr(provider, "installed_at", "")
    return any(
        g["provider_id"] == provider_id
        and g["app_version"] == version
        and g.get("egress") is True
        and _same_install(g, installed_at)
        for g in read_grants()
    )


def _same_install(grant: dict[str, Any], installed_at: str) -> bool:
    """Whether *grant* was given to the install stamped *installed_at*.

    A grant written before grants named their install has no ``installed_at``. It holds
    when it was given on or after the current install began: consent is only asked
    while the app is installed, so an older grant belongs to an earlier install.
    """
    bound = grant.get("installed_at")
    if isinstance(bound, str) and bound:
        return bound == installed_at
    started = parse_installed_at(installed_at)
    if started is None:
        return True  # no readable install stamp: keep the answer grants gave before
    granted_at = grant.get("granted_at")
    return isinstance(granted_at, (int, float)) and granted_at >= started


def parse_installed_at(installed_at: str) -> float | None:
    """``installedAt`` (``%Y-%m-%dT%H:%M:%SZ``, UTC) as epoch seconds, or ``None``."""
    try:
        return float(calendar.timegm(time.strptime(installed_at, "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return None


def grant_egress(provider_id: str, app_version: str, installed_at: str = "") -> None:
    """Record the owner's consent. Called only from the owner's dashboard route."""
    grant: dict[str, Any] = {
        "provider_id": provider_id,
        "app_version": app_version,
        "egress": True,
        "granted_at": time.time(),
    }
    from kiro_crew.apps.execution import INSTALLED_AT_MAX_CHARS

    if installed_at and len(installed_at) <= INSTALLED_AT_MAX_CHARS:
        grant["installed_at"] = installed_at
    with _rewrite_lock():
        grants = [g for g in _grants_for_rewrite() if g["provider_id"] != provider_id]
        grants.append(grant)
        _write({"grants": grants[-_MAX_GRANTS:]})


def revoke_egress(provider_id: str) -> None:
    with _rewrite_lock():
        _write({"grants": [g for g in _grants_for_rewrite() if g["provider_id"] != provider_id]})


def revoke_app(app_name: str) -> None:
    """Drop every grant for a provider *app_name* offers. Raises ``OSError``/``ValueError``.

    Takes no lock and writes nothing when no grant names the app, so an install with
    nothing to clear never needs the record to be writable.
    """
    prefix = f"app:{app_name}/"
    if _names_prefix_unlocked(prefix) is False:
        return
    with _rewrite_lock():
        grants = _grants_for_rewrite()
        kept = [g for g in grants if not g["provider_id"].startswith(prefix)]
        if len(kept) != len(grants):
            _write({"grants": kept})


def _names_prefix_unlocked(prefix: str) -> bool | None:
    """Whether the record names a provider under *prefix*, read without a lock.

    ``None`` when it cannot tell: an unreadable record, or one caught mid-rewrite (a
    rewrite in place empties the file first, and no prefix of the document parses).
    A complete JSON document is the record as some writer left it.
    """
    try:
        text = _path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except OSError:
        return None
    try:
        raw = json.loads(text)
    except ValueError:
        return None
    return any(g["provider_id"].startswith(prefix) for g in _valid(raw))


def revoke_app_soft(app_name: str) -> None:
    """:func:`revoke_app` for an uninstall, logging a failure. The next install under
    the name refuses until the grant is removed (``revoke_app`` there raises)."""
    try:
        revoke_app(app_name)
    except (OSError, ValueError):
        logger.warning("could not remove memory consent of app %r", app_name, exc_info=True)


def _write(document: dict[str, Any]) -> None:
    """Rewrite the record in place: a running sandbox bind-mounts this inode read-only,
    and a rename over it would detach that seal (``atomic_write.rewrite_in_place``)."""
    from kiro_crew.atomic_write import rewrite_in_place

    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    rewrite_in_place(path, json.dumps(document) + "\n", restrict_to_owner=True)
