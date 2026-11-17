"""Where the owner's ticked Bots for each app live: a gateway-only record.

``PUT /api/apps/{name}/bot-grants`` writes one ``<app>.json`` under
``member-records/app-bot-grants/``. ``member-records/`` is masked in every sandbox mode
and refused by the agent file tools (:mod:`kiro_crew.member_records`), so code running
in the sandbox can neither read nor write the list.

The record names the install it belongs to (the install record's ``installedAt``), so a
reinstall under the same name reaches no Bot until the owner ticks again, and uninstall
removes the record (:func:`remove_granted_bots`).

The Bot scopes the owner approved (``permissions.bots``) are not kept here: they are one
family of the app's approved set, in the same protected store as its other permissions
(``apps.protected_app_state``).

This module imports nothing from the ``apps.manager`` facade, so the uninstall owner can
call it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from kiro_crew import member_records, platform_compat
from kiro_crew.apps.execution import APP_NAME_RE
from kiro_crew.atomic_write import atomic_write, fsync_dir, read_bytes_with_retry

#: The record kind under ``member-records/``; one of ``member_records.RECORD_KINDS``.
APP_BOT_GRANTS_KIND = "app-bot-grants"

#: Most Bots one app's ticked list may name. A proposal, to be confirmed.
MAX_BOT_GRANTS = 200

#: Largest record read; a ticked list of the most slugs fits well under it.
_RECORD_MAX_BYTES = 64 * 1024

#: Longest ``installedAt`` a record keeps; an ISO timestamp is far shorter.
_INSTALLED_AT_MAX_CHARS = 64


def _read_bot_grants(value: Any) -> list[str]:
    """A ticked Bot list: valid member slugs, each once, capped. Anything else is none."""
    # Deferred: the member-space module is not needed to import this one.
    from kiro_crew.members import MemberSlugError, validate_slug

    if not isinstance(value, list):
        return []
    out: list[str] = []
    for entry in value:
        try:
            slug = validate_slug(entry)
        except MemberSlugError:
            continue
        if slug not in out:
            out.append(slug)
    return out[:MAX_BOT_GRANTS]


def grants_path(app_name: str) -> Path:
    """``member-records/app-bot-grants/<app>.json``, containment-checked. Raises ``ValueError``."""
    if not isinstance(app_name, str) or not APP_NAME_RE.fullmatch(app_name):
        raise ValueError(f"unsafe app name: {app_name!r}")
    root = member_records.records_dir(APP_BOT_GRANTS_KIND).resolve()
    target = (root / f"{app_name}.json").resolve()
    if target.parent != root:
        raise ValueError(f"app name {app_name!r} escapes {root}")
    return target


def read_granted_bots(app_name: str, installed_at: str) -> list[str]:
    """The slugs the owner ticked for this install of *app_name*; ``[]`` when none.

    A record for another app name or another install reads as none. An unreadable or
    malformed record raises, so the caller grants nothing rather than guessing.
    """
    path = grants_path(app_name)
    try:
        raw = read_bytes_with_retry(path, max_bytes=_RECORD_MAX_BYTES)
    except FileNotFoundError:
        return []
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"bot grants record for {app_name!r} is not an object")
    if data.get("app") != app_name or data.get("installedAt") != installed_at:
        return []
    return _read_bot_grants(data.get("bots"))


def write_granted_bots(app_name: str, installed_at: str, slugs: Iterable[str]) -> list[str]:
    """Store the owner's ticked list for this install; the list as stored.

    Invalid slugs are dropped by the same reader every read uses. An empty list removes
    the record. Raises ``OSError`` when the write does not land, and ``ValueError`` when
    the record would not read back, so the owner is told.
    """
    ticked = _read_bot_grants(list(slugs))
    if not ticked:
        _unlink(grants_path(app_name))
        return []
    if not isinstance(installed_at, str) or len(installed_at) > _INSTALLED_AT_MAX_CHARS:
        raise ValueError(f"install record of {app_name!r} has an unusable installedAt")
    path = grants_path(app_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    for directory in (path.parent, path.parent.parent):
        # Defence in depth only: the sandbox mask and the file-tool floor are the fences.
        try:
            platform_compat.restrict_dir_to_owner(directory)
        except OSError:
            pass
    text = json.dumps({"app": app_name, "installedAt": installed_at, "bots": ticked}) + "\n"
    if len(text.encode("utf-8")) > _RECORD_MAX_BYTES:
        raise ValueError(f"bot grants record for {app_name!r} is too large to read back")
    atomic_write(path, text, fsync=True)
    fsync_dir(path.parent)
    fsync_dir(path.parent.parent)
    return ticked


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    fsync_dir(path.parent, best_effort=True)


def remove_granted_bots(app_name: str) -> None:
    """Remove *app_name*'s ticked list, if any. Raises ``OSError`` when it stays."""
    _unlink(grants_path(app_name))


__all__ = [
    "APP_BOT_GRANTS_KIND",
    "MAX_BOT_GRANTS",
    "grants_path",
    "read_granted_bots",
    "remove_granted_bots",
    "write_granted_bots",
]
