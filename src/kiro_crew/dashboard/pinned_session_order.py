"""The person's order for pinned sidebar sessions, kept on the gateway.

One JSON file, ``pinned-order/chat_pinned_order.json`` in the config dir,
holds a list of slot keys. A key's position in the list is its rank. The list is global rather
than per folder: the sidebar renders pinned rows inside each folder (and at the
top level) and sorts them by rank there, so filtering the one list by group
gives each group's order. That is the same model the browser used when this
order lived in ``localStorage`` (``mc-pinned-session-order``), so the sidebar's
existing drag handling keeps working on the data it already understood.

The file is written whole with an atomic replace, so a reorder is one write:
it lands entirely or not at all. Only the person may set the order (the route
refuses app and crew-member callers), so the file sits in its own directory,
``pinned-order``, which the agent file tools and the OS sandbox both fence: an
agent cannot rewrite the order on disk and have a restart load it.

Rules the writers follow:

* No file at all means the person never reordered. Pinned rows then keep the
  sidebar's own sort, which is what they had before this store existed. A
  file holding an empty list is an order that was saved and has since emptied.
* Pinning a session appends it to the end once an order has been saved, even
  one that has since emptied. A person who never reordered keeps the plain
  sort.
* Unpinning a session removes it, so re-pinning later puts it at the end
  instead of back in an old slot.
* A key whose live slot is unpinned is dropped on the next write. A key with
  no live slot has no rank but is kept, because it may name a session the
  gateway has not restored yet.

The same directory holds the person's manual order for every session, a
second file with its own rules (see "The manual order" below).
"""

from __future__ import annotations

import json
import logging
import os
import stat
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write

logger = logging.getLogger(__name__)

PINNED_ORDER_FILE = "chat_pinned_order.json"

#: The fenced directory the order file lives in (``security.paths`` and
#: ``sandbox._CREW_HIDDEN_LEAVES`` both name it). A directory rather than the
#: bare file, because ``atomic_write`` publishes through a sibling temp that a
#: file-only mask would not cover.
PINNED_ORDER_DIR = "pinned-order"

#: The slot-row fields that describe the person's orders kept in this store: a
#: row's place in the pinned order and in the manual order (see "The manual
#: order" below), and each order's revision. Scoped callers (apps, members) get
#: none of them, so every route that strips one strips all four.
PIN_ORDER_FIELDS = frozenset({"pin_rank", "pin_rev", "manual_rank", "manual_rev"})


def next_revision(current: int, now_us: int) -> int:
    """The revision after *current*: strictly larger, and at least *now_us*.

    Starting from the clock rather than from 0 keeps revisions increasing
    across a gateway restart, so a browser that saw the old process's last
    revision still takes the new process's first one.
    """
    return max(current + 1, now_us)


def store_path(home: Path) -> Path:
    """The order file under *home* (the config dir)."""
    return home / PINNED_ORDER_DIR / PINNED_ORDER_FILE


#: The most keys the store keeps and one reorder request may carry. Far above
#: any real sidebar; a request past it is malformed rather than large.
MAX_PINNED_ORDER_KEYS = 2000

#: Slot keys are short generated identifiers; this bounds a hand-edited file
#: and a request body without constraining any real key.
MAX_PINNED_ORDER_KEY_CHARS = 256


#: The most bytes ``json.dumps`` spends on one key character. With its default
#: ``ensure_ascii`` a character outside the Basic Multilingual Plane is one
#: code point (one unit of ``MAX_PINNED_ORDER_KEY_CHARS``) but is written as a
#: surrogate pair, ``\ud83d\ude00``: 12 bytes.
_MAX_JSON_BYTES_PER_KEY_CHAR = 12

#: Byte ceiling for the order file: every key at its maximum length, every
#: character at its worst-case JSON escape, plus quoting and the ``", "``
#: separators. Anything :func:`save` writes fits; a larger file was not
#: written by this module.
MAX_PINNED_ORDER_FILE_BYTES = (
    MAX_PINNED_ORDER_KEYS * (MAX_PINNED_ORDER_KEY_CHARS * _MAX_JSON_BYTES_PER_KEY_CHAR + 4) + 2
)


def _valid_key(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_PINNED_ORDER_KEY_CHARS


def load_stored(path: Path) -> tuple[list[str], bool]:
    """Read the stored order from *path* and whether a file was there.

    A missing file is the normal first-run state and reads as ``([], False)``.
    A file that does not parse, or is not a list, reads as ``([], True)`` with
    a warning: the order is a display preference, so losing it degrades to the
    sidebar's own sort rather than failing startup. Entries that are not
    usable keys and repeated keys are skipped. Only ``FileNotFoundError``
    counts as missing; any other ``OSError`` is raised, so an unreadable home
    is never mistaken for an empty order that a later save would write over
    the real file. The flag comes from this one read, not a second stat, so
    the two answers cannot disagree.
    """
    try:
        data = _read_order_file(path)
    except FileNotFoundError:
        return [], False
    if data is None:
        return [], True
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        logger.warning("%s has malformed content: %s", path.name, exc)
        return [], True
    if not isinstance(raw, list):
        logger.warning("%s is not a list (%s); ignoring", path.name, type(raw).__name__)
        return [], True
    keys = dedupe(key for key in raw if _valid_key(key))
    if len(keys) > MAX_PINNED_ORDER_KEYS:
        logger.warning(
            "%s holds %d keys; keeping the first %d and dropping %d",
            path.name,
            len(keys),
            MAX_PINNED_ORDER_KEYS,
            len(keys) - MAX_PINNED_ORDER_KEYS,
        )
    return keys[:MAX_PINNED_ORDER_KEYS], True


def _read_order_file(path: Path) -> bytes | None:
    """The order file's bytes, or ``None`` for a file this module did not write.

    The read never follows a link and never reads more than
    :data:`MAX_PINNED_ORDER_FILE_BYTES` plus one byte, so a link to a device or
    an oversized file planted at the path cannot exhaust memory on the load
    that runs at every gateway start. A link, a directory, a device or an
    oversized file is logged and read as ``None``, so the sidebar falls back to
    its own sort. The next save's atomic rename replaces a link, a device or an
    oversized file; a directory at the path stays, and that save fails until the
    directory is removed. ``FileNotFoundError`` and other
    ``OSError`` propagate as before.
    """
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        logger.warning("%s is not a regular file; ignoring it", path.name)
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            logger.warning("%s is not a regular file; ignoring it", path.name)
            return None
        data = handle.read(MAX_PINNED_ORDER_FILE_BYTES + 1)
    if len(data) > MAX_PINNED_ORDER_FILE_BYTES:
        logger.warning(
            "%s is larger than %d bytes; ignoring it",
            path.name,
            MAX_PINNED_ORDER_FILE_BYTES,
        )
        return None
    return data


def save(path: Path, keys: list[str]) -> None:
    """Replace the stored order with *keys* in one atomic, owner-only write.

    ``restrict_to_owner`` also refuses a linked parent, so a ``pinned-order``
    symlink planted before the directory was fenced cannot redirect the write.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write(path, json.dumps(keys), fsync=True, restrict_to_owner=True)


def dedupe(keys: Iterable[str]) -> list[str]:
    """*keys* in order with later repeats removed."""
    seen: set[str] = set()
    out: list[str] = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def prune(
    keys: Iterable[str],
    pinned: Mapping[str, bool],
    *,
    label: str = "pinned-order",
    noun: str = "pinned sessions",
) -> list[str]:
    """Drop keys whose slot is live and unpinned; keep the rest, in order.

    *label* and *noun* only name the list in the warnings, so the manual order
    (which passes every live key as ``True``) logs as itself.

    *pinned* maps each live slot key to its pinned flag. A key with no live
    slot is kept: the gateway restores open sessions after it starts serving,
    so a key missing from *pinned* may be a session that has not been restored
    yet, and dropping it would lose its place for good. Such a key ranks
    nothing (only a live pinned row has a rank), and the list is capped at
    ``MAX_PINNED_ORDER_KEYS``. The cap trims those keys first, last first: live
    slots are far fewer than the cap, so every live pinned key keeps its place.
    Only if live pinned keys alone exceeded the cap would the last of them be
    dropped, so the stored list never outgrows the cap.
    """
    unique = dedupe(keys)
    invalid = sum(1 for key in unique if not _valid_key(key))
    if invalid:
        # The loader skips these too, so a stored one would lose its rank on
        # the next restart; dropping it here keeps the two answers the same.
        logger.warning("dropping %d %s key(s) that are not usable keys", invalid, label)
    kept = [key for key in unique if _valid_key(key) and pinned.get(key, True)]
    room = MAX_PINNED_ORDER_KEYS - sum(1 for key in kept if key in pinned)
    out: list[str] = []
    evicted = 0
    for key in kept:
        if key in pinned:
            out.append(key)
        elif room > 0:
            out.append(key)
            room -= 1
        else:
            evicted += 1
    if evicted:
        logger.warning(
            "dropping %d %s key(s) over the %d-key cap that have no live slot",
            evicted,
            label,
            MAX_PINNED_ORDER_KEYS,
        )
    if len(out) > MAX_PINNED_ORDER_KEYS:
        logger.warning(
            "%d %s exceed the %d-key order cap; the last %d lose their rank",
            len(out),
            noun,
            MAX_PINNED_ORDER_KEYS,
            len(out) - MAX_PINNED_ORDER_KEYS,
        )
    return out[:MAX_PINNED_ORDER_KEYS]


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
    """The order after a reorder of the keys in *requested*.

    The requested keys take the places the named keys already held, in the
    requested order; keys the request did not name stay exactly where they
    were. A caller that could not see a session (one being unpinned, or not
    restored yet) therefore leaves its place alone instead of pushing it to the
    end. Requested keys that were not stored yet follow, in request order.
    Live pinned sessions that are in neither list come last, in *pinned*'s
    order: a pin that landed after the caller read the slots, while no order
    was stored yet, would otherwise stay unranked and sort differently in
    every browser.
    """
    named = set(requested)
    order = list(dedupe(current))
    sequence = iter(dedupe(requested))
    for index, key in enumerate(order):
        if key in named:
            order[index] = next(sequence)
    order.extend(sequence)
    seated = set(order)
    order.extend(key for key, is_pinned in pinned.items() if is_pinned and key not in seated)
    return prune(order, pinned)


# ── The manual order ──
#
# ``dashboard.session_sort`` "manual" draws the sidebar's unpinned sessions in
# a second person-set list, kept beside the pinned one in the same fenced
# directory and read by the same loader: ``MANUAL_ORDER_FILE`` holds slot keys,
# a key's position is its rank, and the list is global, so each folder group
# shows its sessions in this list's order. A sibling file rather than a new
# shape for the pinned one, so a build that only knows the pinned file still
# reads it unchanged.
#
# Rules the writer follows:
#
# * No file means nothing is saved, and every session is unranked. Unranked
#   sessions draw above the ranked ones: that is where a new session appears.
# * The first write is a seed: the order on the person's screen, landing only
#   while nothing is saved, so two browsers seeding at once cannot replace each
#   other's order.
# * Every later write moves ONE session next to another, so a move made in one
#   browser never puts back a place another browser changed.
# * Pinning and unpinning leave this list alone: pinned rows draw in the pinned
#   order, and an unpinned row takes back the place it had, if it had one.
# * A key with no live slot is kept, like the pinned order's: it may be a
#   session the gateway has not restored yet, or one archived and later
#   restored. The cap trims those first.

MANUAL_ORDER_FILE = "chat_manual_order.json"


def manual_store_path(home: Path) -> Path:
    """The manual order file under *home* (the config dir)."""
    return home / PINNED_ORDER_DIR / MANUAL_ORDER_FILE


class ManualOrder:
    """The manual order as the gateway holds it in memory.

    ``keys`` is the stored list, ``loaded`` whether the file has been read
    (the read runs after the gateway starts serving), ``stored`` whether a file
    exists at all, and ``rev`` the revision every slot row and every write
    answer carries, raised on each change so a browser can drop a frame
    serialized before a write it already applied. ``rank`` is a dict lookup:
    it runs once per row on every slots push.
    """

    def __init__(self) -> None:
        self.keys: list[str] = []
        self.loaded = False
        self.stored = False
        self.rev = 0
        self._index: dict[str, int] = {}

    def rank(self, key: str) -> int | None:
        """*key*'s place in the order, or ``None`` when it has none."""
        return self._index.get(key)

    def adopt(self, keys: list[str], stored: bool, now_us: int) -> None:
        """Take *keys* as the order and raise the revision."""
        self.keys = list(keys)
        self._index = {key: index for index, key in enumerate(self.keys)}
        self.loaded = True
        self.stored = stored
        self.rev = next_revision(self.rev, now_us)


def _prune_manual(keys: Iterable[str], live: Iterable[str]) -> list[str]:
    """*keys* with repeats and unusable keys dropped, capped; no live key is dropped."""
    return prune(keys, dict.fromkeys(live, True), label="manual-order", noun="live sessions")


def after_manual_seed(keys: Iterable[str], live: Iterable[str]) -> list[str]:
    """The order a seed writes: the person's on-screen order, cleaned."""
    return _prune_manual(keys, live)


def after_manual_move(
    current: list[str],
    key: str,
    anchor: str | None,
    *,
    after: bool,
    seat: Iterable[str],
    live: Iterable[str],
) -> list[str] | None:
    """The order after *key* moves next to *anchor*; ``None`` when *anchor* has no place.

    *seat* names the unranked sessions the caller showed above the ranked ones,
    in that order. The ones still unranked are put at the top first, which
    changes nothing on screen (unranked rows already draw there) but gives
    *anchor* a place when it is one of them. *key* then goes right after
    *anchor*, or right before it when *after* is false. ``anchor=None`` with
    *after* puts *key* at the very top. Every other key keeps its place.
    """
    order = dedupe(current)
    placed = set(order)
    live_keys = set(live)
    seated = [k for k in dedupe(seat) if k not in placed and k != key and k in live_keys]
    order = [*seated, *(k for k in order if k != key)]
    if anchor is None:
        if not after:
            return None
        index = 0
    elif anchor in order:
        index = order.index(anchor) + (1 if after else 0)
    else:
        return None
    order.insert(index, key)
    return _prune_manual(order, live_keys)


def clear_stored(path: Path) -> None:
    """Remove the stored order at *path*, so nothing is saved; a missing file is fine."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
