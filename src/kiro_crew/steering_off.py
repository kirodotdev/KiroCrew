"""The steering files one chat has switched off: resolved paths, never bodies.

One file under the data home maps a chat's session key to the sorted resolved
paths it excludes. Context assembly reads :func:`steering_off_for` once per turn
and hands the set to the one writer that honours it, the Claude Code ``[Steering
resources]`` load, which skips those documents; the folder steering section does
not read it. A chat with nothing switched off reads an empty set, so its turn text
is unchanged.
The dashboard's per-chat steering route writes it.

The record is one JSON object, rewritten in place on its own inode by
:func:`kiro_crew.atomic_write.rewrite_in_place`, the writer the sibling sealed
switches use: the leaf is sealed read-only in the sandbox, and a rename over it
would detach a running Linux sandbox's read-only mount.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from kiro_crew.atomic_write import rewrite_in_place

logger = logging.getLogger(__name__)

_FILE_NAME = "chat_steering_off.json"
# One gateway process writes this file; the lock keeps two toggles of one
# process from losing each other's read-modify-write, and keeps a reader from
# seeing a rewrite half done.
_WRITE_LOCK = threading.Lock()


def _store_path() -> Path:
    from kiro_crew.config.paths import data_home

    return data_home() / _FILE_NAME


class SteeringOffStoreUnreadable(OSError):
    """The store exists but cannot be read, so a write would erase it."""


# The last record this process read or wrote whole, per store path, held under
# _WRITE_LOCK. A store that later cannot be read reads as this, never as
# "nothing switched off": an off switch must fail closed.
_LAST_GOOD: dict[str, dict[str, list[str]]] = {}
# Store paths whose unreadable record was already logged, so a turn loop logs once.
_WARNED_UNREADABLE: set[str] = set()


def _read_all() -> dict[str, list[str]]:
    """The store, ``{}`` when the file is missing.

    A file that is empty, not one JSON object, or cannot be read raises
    :class:`SteeringOffStoreUnreadable`, so a switch never overwrites every other
    chat's record with an empty one. Empty counts as unreadable because no writer
    leaves it so: the sandbox pre-creates the leaf as ``{}`` and every save writes a
    whole object, so zero bytes is a rewrite that failed after its truncate.
    """
    try:
        text = _store_path().read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise SteeringOffStoreUnreadable(str(exc)) from exc
    if not text.strip():
        raise SteeringOffStoreUnreadable("the store is empty")
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise SteeringOffStoreUnreadable("the store is not a whole record") from exc
    if not isinstance(raw, dict):
        raise SteeringOffStoreUnreadable("the store is not a JSON object")
    return {
        key: [p for p in paths if isinstance(p, str) and p]
        for key, paths in raw.items()
        if isinstance(key, str) and isinstance(paths, list)
    }


def steering_off_for(session_key: str | None) -> frozenset[str]:
    """The resolved paths *session_key* switched off; empty for none.

    A store that cannot be read reads as the last record this process held whole,
    logged once.
    """
    if not session_key:
        return frozenset()
    where = str(_store_path())
    # Under the write lock: a reader never sees a rewrite half done.
    with _WRITE_LOCK:
        try:
            store = _read_all()
        except SteeringOffStoreUnreadable as exc:
            if where not in _WARNED_UNREADABLE:
                _WARNED_UNREADABLE.add(where)
                logger.warning(
                    "steering-off store unreadable (%s); keeping switched-off files off", exc
                )
            # shortcut: a process that never held a whole record (a restart onto a
            # file a crash tore, or one it may not read) has nothing to keep off, so
            # it reads as nothing switched off, the turn main sends; the next switch
            # answers 409 store_unreadable, which makes it visible.
            return frozenset(_LAST_GOOD.get(where, {}).get(session_key, ()))
        _LAST_GOOD[where] = store
        _WARNED_UNREADABLE.discard(where)
        return frozenset(store.get(session_key, ()))


def set_steering_off(session_key: str, path: str, off: bool) -> frozenset[str]:
    """Switch one resolved *path* off (or back on) for *session_key*; the new set."""
    with _WRITE_LOCK:
        target = _store_path()
        store = _read_all()
        _LAST_GOOD[str(target)] = {key: list(paths) for key, paths in store.items()}
        current = set(store.get(session_key, ()))
        if off:
            current.add(path)
        else:
            current.discard(path)
        if current:
            store[session_key] = sorted(current)
        else:
            store.pop(session_key, None)
        data = json.dumps(store, sort_keys=True, indent=2, ensure_ascii=True) + "\n"
        rewrite_in_place(target, data)
        _LAST_GOOD[str(target)] = store
        return frozenset(current)
