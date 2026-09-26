"""The person's order for pinned sidebar sessions, kept on the gateway.

One JSON file, ``chat_pinned_order.json`` in the config dir, holds a list of
slot keys. A key's position in the list is its rank. The list is global rather
than per folder: the sidebar renders pinned rows inside each folder (and at the
top level) and sorts them by rank there, so filtering the one list by group
gives each group's order. That is the same model the browser used when this
order lived in ``localStorage`` (``mc-pinned-session-order``), so the sidebar's
existing drag handling keeps working on the data it already understood.

The file is written whole with an atomic replace, so a reorder is one write:
it lands entirely or not at all.

Rules the writers follow:

* No file at all means the person never reordered. Pinned rows then keep the
  sidebar's own sort, which is what they had before this store existed. A
  file holding an empty list is an order that was saved and has since emptied.
* Pinning a session appends it to the end once an order has been saved, even
  one that has since emptied. A person who never reordered keeps the plain
  sort.
* Unpinning a session removes it, so re-pinning later puts it at the end
  instead of back in an old slot.
* A key whose slot is gone or is not pinned has no rank. Such keys are
  dropped on the next write.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write

logger = logging.getLogger(__name__)

PINNED_ORDER_FILE = "chat_pinned_order.json"

#: The most keys the store keeps and one reorder request may carry. Far above
#: any real sidebar; a request past it is malformed rather than large.
MAX_PINNED_ORDER_KEYS = 2000

#: Slot keys are short generated identifiers; this bounds a hand-edited file
#: and a request body without constraining any real key.
MAX_PINNED_ORDER_KEY_CHARS = 256


def _valid_key(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_PINNED_ORDER_KEY_CHARS


def load(path: Path) -> list[str]:
    """Read the stored order from *path*.

    A missing file is the normal first-run state and reads as ``[]``. A file
    that does not parse, or is not a list, also reads as ``[]`` with a warning:
    the order is a display preference, so losing it degrades to the sidebar's
    own sort rather than failing startup. Entries that are not usable keys and
    repeated keys are skipped. A transient ``OSError`` is raised, so a caller
    never replaces good in-memory state with an empty read.
    """
    try:
        if not path.exists():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        logger.warning("%s has malformed content: %s", PINNED_ORDER_FILE, exc)
        return []
    if not isinstance(raw, list):
        logger.warning("%s is not a list (%s); ignoring", PINNED_ORDER_FILE, type(raw).__name__)
        return []
    return dedupe(key for key in raw if _valid_key(key))[:MAX_PINNED_ORDER_KEYS]


def save(path: Path, keys: list[str]) -> None:
    """Replace the stored order with *keys* in one atomic, owner-only write."""
    atomic_write(path, json.dumps(keys), fsync=True, mode=0o600)


def dedupe(keys: Iterable[str]) -> list[str]:
    """*keys* in order with later repeats removed."""
    seen: set[str] = set()
    out: list[str] = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def prune(keys: Iterable[str], pinned: Mapping[str, bool]) -> list[str]:
    """Keep only keys whose slot is live and pinned, in order.

    *pinned* maps each live slot key to its pinned flag.
    """
    return [key for key in dedupe(keys) if pinned.get(key)]


def after_pin_change(
    current: list[str],
    key: str,
    now_pinned: bool,
    pinned: Mapping[str, bool],
    stored: bool,
) -> list[str]:
    """The order after *key* is pinned or unpinned.

    Unpinning removes the key. Pinning appends it once the person has ordered
    their pins at all (*stored*: an order was saved, even one that has since
    emptied). A person who never reordered keeps the plain sort.
    """
    rest = [k for k in current if k != key]
    if now_pinned and (stored or rest):
        rest.append(key)
    return prune(rest, pinned)


def after_reorder(
    current: list[str], requested: list[str], pinned: Mapping[str, bool]
) -> list[str]:
    """The order after a reorder that puts *requested* first, in that order.

    Stored keys the request did not name keep their relative order after it,
    so a caller that ranks only part of the pinned set does not erase the rest.
    """
    named = set(requested)
    return prune([*requested, *(k for k in current if k not in named)], pinned)
