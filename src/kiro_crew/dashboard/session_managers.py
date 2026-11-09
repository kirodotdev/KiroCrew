"""Who manages a session when it is not the session that created it.

``session_handoff`` gives a session to another session to manage. The ownership fence
then reads the MANAGER for the verbs that run a session (send, read, stop, close and
the tree verbs) instead of ``_created_by``, which is never rewritten.

The record lives in one small JSON file under the data home's ``crew-log`` root, keyed
by slot key:

    {"chat-worker": {"manager": "chat-conductor", "tab": "<worker tab id>"}}

A record MAY also carry ``"bind": true``, set only by a ``session_handoff`` whose
opt-in ``bind`` flag was on. It moves work-ledger bind rights with management: the
manager may then bind the session on its OWN ledger, where ``_created_by`` alone would
refuse it ``worker_not_owned``. Without the flag the key is absent and bind rights stay
with the creator, which is today's behaviour. :func:`bind_rights_holder` is the one
reader of it, and it applies the same tab-match fail-closed test :func:`holder` does.

The ``crew-log`` root is hidden from every sandboxed process and fenced from the
agent's own file tools (``sandbox_leaves``, ``security.paths``), so a session that
handed a worker away cannot delete or rewrite the record to get it back.

A slot with no record is managed by its creator, which is the reach the fence had
before this existed. The file is read once per process and cached, so the synchronous
fence never touches the disk after the first read and never waits on a writer; every
writer goes through :func:`write`, which replaces the file atomically and then swaps
in a new cached snapshot.

``tab`` is the tab identity of the session the record was written for. A record whose
tab does not match the slot it is read for decides nothing: :func:`holder` answers
``""``, which matches no caller, so only the person reaches that session. That is the
fail-closed answer for a malformed record too. It is what stops a giver from winning a
session back by editing the tab id on its transcript line: the edit makes the record
stop matching, and a non-matching record withholds reach instead of handing the
session back to its creator.

An unreadable or missing file means no session has a manager, so every session falls
back to its creator.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home
from kiro_crew.loop_lock import LoopBoundLock

logger = logging.getLogger(__name__)

FILENAME = "session_managers.json"

_lock = threading.Lock()
#: Serializes every decide-then-write on the records across awaits: a handoff's checks
#: and write, a close's hand-back, a resume's rebind. ``_lock`` covers one file write.
HANDOFF_LOCK = LoopBoundLock()
#: ``(path, records)`` for the file last read or written, so a test that moves the data
#: home gets a fresh read rather than another home's records.
_cache: "tuple[Path, dict[str, Any]] | None" = None


def _path() -> Path:
    return data_home() / "crew-log" / FILENAME


def _read(path: Path) -> "dict[str, Any]":
    try:
        raw = path.read_text("utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        logger.warning("session managers: %s is unreadable; creators manage", path)
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("session managers: %s is not JSON; creators manage", path)
        return {}
    if not isinstance(data, dict):
        logger.warning("session managers: %s is not an object; creators manage", path)
        return {}
    return data


def load() -> "dict[str, Any]":
    """Every record, read from disk on the first call and from memory after it.

    Blocking on the first call. An async caller runs it in a thread before the
    synchronous fence needs it.
    """
    global _cache
    path = _path()
    cached = _cache
    if cached is not None and cached[0] == path:
        # A snapshot is never mutated after it is published, so this read needs no lock
        # and never waits on a writer holding one across its disk write.
        return cached[1]
    with _lock:
        if _cache is None or _cache[0] != path:
            _cache = (path, _read(path))
        return _cache[1]


def reset_cache() -> None:
    """Forget the cached records, so the next read comes from disk (a restart, in tests)."""
    global _cache
    with _lock:
        _cache = None


def holder(slot: Any) -> str:
    """The slot key of the session that manages *slot* now, or ``""`` for nobody.

    The creator when no record names *slot*. ``""`` when a record names it but cannot
    be trusted for this session: it is malformed, or its tab is not this slot's tab.
    ``""`` matches no caller key, so the fence refuses every agent and only the person
    reaches the session.
    """
    record = load().get(str(getattr(slot, "key", "") or ""))
    if record is None:
        return str(getattr(slot, "_created_by", "") or "")
    if not isinstance(record, Mapping):
        return ""
    manager = record.get("manager")
    tab = record.get("tab")
    if not isinstance(manager, str) or not manager or not isinstance(tab, str):
        return ""
    if tab != str(getattr(slot, "_tab_id", "") or ""):
        return ""
    return manager


def bind_rights_holder(slot: Any) -> str:
    """The manager that holds work-ledger BIND rights for *slot*, or ``""`` for nobody.

    Only a record written by a ``session_handoff`` with its ``bind`` flag on carries
    those rights, so this reads ``""`` for every record without ``"bind": true`` and
    for a missing, malformed or tab-mismatched one -- the same fail-closed test
    :func:`holder` applies. ``""`` matches no caller, so the bind check falls back to
    ``_created_by`` and today's behaviour stands. A record naming a manager who holds
    management but NOT bind rights (the flag was off) also reads ``""`` here, which is
    how an ordinary handoff leaves bind ownership with the creator.
    """
    key = str(getattr(slot, "key", "") or "")
    record = load().get(key)
    if not isinstance(record, Mapping) or record.get("bind") is not True:
        return ""
    manager = record.get("manager")
    tab = record.get("tab")
    if not isinstance(manager, str) or not manager or not isinstance(tab, str):
        return ""
    if tab != str(getattr(slot, "_tab_id", "") or ""):
        return ""
    return manager


def managed_by(manager_key: str) -> "dict[str, str]":
    """``{slot key: tab}`` for every record naming *manager_key* as the manager."""
    out: dict[str, str] = {}
    for key, record in load().items():
        if isinstance(record, Mapping) and record.get("manager") == manager_key:
            out[key] = str(record.get("tab") or "")
    return out


def recorded_manager(slot_key: str, tab: str) -> str:
    """The manager recorded for *slot_key* when the record's tab is *tab*, else ``""``.

    For a session that is not live (an archived transcript): *tab* is the tab id its
    metadata line carries. The same test :func:`holder` applies to a live slot.
    """
    manager, _bind = recorded_manager_with_bind(slot_key, tab)
    return manager


def recorded_manager_with_bind(slot_key: str, tab: str) -> "tuple[str, bool]":
    """:func:`recorded_manager`, plus whether the record carries work-ledger bind rights.

    A resume re-writes the record for the new tab, so it must carry the ``bind`` flag
    through or a handoff made with ``bind=true`` loses it silently. ``("", False)``
    when the record does not match, so a non-matching record keeps the resume from
    granting rights nobody recorded.
    """
    record = load().get(slot_key)
    if not isinstance(record, Mapping):
        return "", False
    manager, held_tab = record.get("manager"), record.get("tab")
    if not isinstance(manager, str) or not isinstance(held_tab, str) or not held_tab:
        return "", False
    if held_tab != tab:
        return "", False
    return manager, record.get("bind") is True


async def rebind_resumed(slot: Any, archived_tab: str) -> None:
    """Keep a handoff on a session reopened from history. Best-effort; never raises.

    A resume mints a new tab id, and a record naming the old tab decides for nobody,
    so the manager would lose the session for good. Rebound only when the record
    matched the archived transcript's tab, read under :data:`HANDOFF_LOCK` so a
    handoff of the now-live session wins.
    """
    tab = str(getattr(slot, "_tab_id", "") or "")
    key = str(getattr(slot, "key", "") or "")
    if not tab or not key or not archived_tab or tab == archived_tab:
        return
    try:
        async with HANDOFF_LOCK:
            manager, bind = await asyncio.to_thread(recorded_manager_with_bind, key, archived_tab)
            if manager:
                if not getattr(slot, "_created_by", ""):
                    from kiro_crew.dashboard.session_control_parts.session_revive import (
                        UNVERIFIED_CREATOR,
                    )

                    # A History reopen does not restore the creator, and a blank one
                    # reads as a person's tab: the manager would get back a session
                    # that runs unfenced. Fence it before the manager regains reach.
                    slot._created_by = UNVERIFIED_CREATOR
                # Carry the bind flag through, so a handoff made with bind=true keeps
                # the manager's work-ledger bind rights across a History resume.
                await asyncio.to_thread(write, {key: (manager, tab, bind)})
    except Exception as exc:
        logger.warning("session managers: manager of %s not kept on resume: %s", key, exc)


def write(changes: "Mapping[str, tuple[str, str] | tuple[str, str, bool] | None]") -> None:
    """Apply *changes* and persist them: ``key -> (manager, tab[, bind])``, or ``None``
    to drop.

    A third element sets work-ledger bind rights on the record (``"bind": true``); a
    two-element change clears any prior flag, so a later ordinary handoff of a session
    that had bind rights takes them back. Dropping a record hands the session back to
    its creator. Blocking; the file is replaced atomically before the cache, so the
    fence never reads a manager the disk does not hold. Raises what the write raises,
    with nothing changed.
    """
    global _cache
    path = _path()
    with _lock:
        records = dict(_cache[1]) if _cache is not None and _cache[0] == path else _read(path)
        for key, change in changes.items():
            if change is None:
                records.pop(key, None)
            else:
                record: dict[str, Any] = {"manager": change[0], "tab": change[1]}
                if len(change) == 3 and change[2]:
                    record["bind"] = True
                records[key] = record
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(records, indent=1, sort_keys=True) + "\n")
        _cache = (path, records)
