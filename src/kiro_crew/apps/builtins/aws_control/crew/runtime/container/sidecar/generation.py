"""The committed-generation pointer, read the same way by both processes.

The two authority files are published as a PAIR into one of two generation slots, and a
single object -- the pointer -- names the slot whose pair is committed. A reader is
entitled to exactly that slot; a writer publishes into the other one and commits it last.
Two consequences follow, and they are the whole reason the protocol exists:

* A cycle interrupted between the pair's two PUTs damages only a slot no reader looks at.
  The pointer still names the previous generation, whose objects were never rewritten, so
  a replacement boots from a coherent older pair rather than a torn newer one.
* Commitment is a single object, and a single object is either there or it is not. There
  is no state in which half a commitment is visible.

The pointer's ABSENCE is meaningful rather than an error. A bucket written before this
protocol holds the authority objects at their ``data/`` keys with no pointer, and that is
read as GENERATION 0. Those objects are never deleted, moved or rewritten, so a bucket
does not have to be migrated to be read, and a writer that predates the protocol keeps
producing buckets this one understands.

The interpretation lives here and neither process keeps its own copy, for the reason
``keys.py`` gives for key derivation: the writer and the reader agreeing with each other
while both disagree with the contract is the failure this shape makes unrepresentable.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from ..common import Settings, keys
from ..common.config import MAX_OBJECT_BYTES
from .store import ObjectAbsent, ObjectStore, StoreUnusable

log = logging.getLogger("smc.sidecar.generation")

__all__ = [
    "Pointer",
    "PointerUnusable",
    "read_pointer",
    "pointer_body",
]


class PointerUnusable(RuntimeError):
    """The pointer is present and cannot be used.

    Distinct from absent, and the distinction decides a boot. Absent means no generation
    has been committed, so the legacy keys are generation 0 and a task starts from them.
    Present-but-unusable means a generation may well be committed and this task cannot
    tell which -- reading that as absence would boot from objects the pointer was steering
    away from.
    """


@dataclass(frozen=True)
class Pointer:
    """The committed generation: which slot, and which files it was committed with."""

    slot: str
    authority: frozenset[str]


def pointer_body(slot: str) -> bytes:
    """The pointer's bytes for a commitment of *slot*.

    One function so the writer's bytes and the reader's expectations cannot drift; the
    reader's own parsing is the other half and lives in :func:`read_pointer`.
    """
    return json.dumps(
        {"slot": slot, "authority": sorted(keys.AUTHORITY_NAMES)}, sort_keys=True
    ).encode("utf-8")


def read_pointer(settings: Settings, store: ObjectStore) -> Pointer | None:
    """The committed generation, or ``None`` when no pointer has been published.

    ``None`` is the generation-0 answer: the bucket either holds the legacy authority keys
    or holds nothing at all, and both are states a task may boot from.

    Raises :class:`PointerUnusable` for every other way this can go -- a read that fails,
    bytes that do not parse, a slot this writer does not publish into, a missing file list.
    A pointer that exists and cannot be trusted is not permission to look elsewhere.
    """
    key = keys.authority_pointer_key(settings)
    try:
        raw = store.get(key, limit=MAX_OBJECT_BYTES)
    except ObjectAbsent:
        return None
    except StoreUnusable:
        # Not translated: the bucket itself cannot be read, so every later read meets the
        # same answer and the process must end on it rather than report a pointer problem.
        raise
    except Exception as exc:  # noqa: BLE001 - translated, never swallowed
        raise PointerUnusable(
            f"the generation pointer could not be read from the bucket ({exc}). This is "
            "not the same as it being absent: absent means no generation is committed and "
            "the legacy keys are this bucket's truth, while unreadable means one may be "
            "committed and this task cannot tell which."
        ) from exc
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PointerUnusable(
            f"the generation pointer in the bucket does not parse ({exc}), so it cannot "
            "say which generation is committed."
        ) from exc
    if not isinstance(parsed, dict):
        raise PointerUnusable(
            f"the generation pointer in the bucket is a JSON {type(parsed).__name__}, not "
            "an object, so it names no generation."
        )
    slot = parsed.get("slot")
    if slot not in keys.AUTHORITY_SLOTS:
        raise PointerUnusable(
            f"the generation pointer names slot {slot!r}, which is not one of "
            f"{', '.join(keys.AUTHORITY_SLOTS)}. No cycle of this writer published it, so "
            "the objects it points at are not a generation this task can read."
        )
    listed = parsed.get("authority")
    if not isinstance(listed, list) or not all(isinstance(name, str) for name in listed):
        raise PointerUnusable(
            "the generation pointer has no 'authority' list of names, so it cannot say "
            "which files the committed generation contains."
        )
    named = frozenset(listed)
    missing = [name for name in keys.AUTHORITY_NAMES if name not in named]
    if missing:
        raise PointerUnusable(
            f"the generation pointer commits a generation without {', '.join(missing)}, "
            "and this writer commits the authority pair whole or not at all. Read as a "
            "partial generation it would present a name this task knows as legitimately "
            "absent, and the backend would flush its own empty view over it -- so the "
            "pointer is unusable rather than a generation missing a member."
        )
    # Names this version does not know are dropped, not refused: a bucket written by a
    # newer writer that commits a third authority file still names a generation whose
    # pair this one can read, and refusing it would make a rollback unbootable. The
    # check above is what keeps that tolerance from also admitting a pointer that
    # under-lists a name this version DOES know.
    return Pointer(slot=slot, authority=frozenset(n for n in named if n in keys.AUTHORITY_NAMES))
