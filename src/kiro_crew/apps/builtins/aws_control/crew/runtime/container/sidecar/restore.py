"""Bringing the two authority files back, before the backend can overwrite them.

## Why this runs before the backend and not beside it

``session_map.json`` and ``open_slots.json`` are what turn a slot id back into a
conversation. The backend flushes them periodically from its own in-memory state, so
a backend that starts before they are on disk starts with an empty slot table and
then PERSISTS that emptiness over the restored files. The conversation list comes up
blank, the transcripts are still on disk, and nothing reports a fault.

So the restore is not "early for speed". Finishing before the backend starts is the
correctness rule, and this function is called from the supervisor's startup order
where that is enforced, not from the sidecar, which does not exist yet at that point.

Transcripts are deliberately NOT restored here. The front fetches the one transcript
a turn continues, on that turn, which keeps the property that a task only ever holds
the conversations it has itself served. Downloading them all at boot would undo that
and would need a bucket listing, which the front's reader cannot do.

## Why the bytes are validated before they are written

Both of the backend's own readers ignore an authority file they cannot parse and
carry on with an empty result. That is right for them and wrong for this step: a
malformed object written here would be read as "no conversations" and then replaced by
the flush, so the restore would look like it worked and the customer's list would be
empty. Refusing to boot instead turns a silent loss into a message an operator gets
before the task serves a turn.

The check is exactly as strict as those readers require -- the file must be a JSON
object, and ``open_slots.json``'s ``keys`` must be a list if it is present -- and no
stricter. A schema invented here would refuse a file the backend would have accepted.

## What an existing local file means

It is kept. Nothing has started, so a file already at the path did not come from this
boot's backend: it came from a data home that outlived the task, and that copy leads
the bucket by up to one backup interval. Overwriting it would roll a conversation list
backwards. ``link_new`` makes that a filesystem guarantee rather than a check.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..common import Settings, keys, statefile
from ..common.config import MAX_OBJECT_BYTES
from . import generation
from .store import ObjectAbsent, ObjectStore, StoreUnusable

log = logging.getLogger("smc.sidecar.restore")

__all__ = [
    "RestoreFailed",
    "RestoreResult",
    "validate_authority",
    "restore_authority",
]


class RestoreFailed(RuntimeError):
    """The authority files could not be restored, so the task must not start.

    Fail-closed on purpose. Every alternative -- boot without them, boot with some of
    them, boot with bytes that did not parse -- ends the same way: the backend flushes
    an empty slot table over the real one and the customer's conversation list is gone
    with nothing to say so.
    """


@dataclass
class RestoreResult:
    """What the restore did, per authority file."""

    restored: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)
    kept_local: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{len(self.restored)} restored, {len(self.absent)} not in the bucket, "
            f"{len(self.kept_local)} already on disk"
        )


def validate_authority(name: str, raw: bytes) -> None:
    """Refuse bytes the backend would silently ignore. Returns nothing on success.

    Whole-file shapes and INDIVIDUAL entries both, because the backend discards at both
    levels and the consequence is the same: what it does not load, its next flush deletes
    from the bucket. A file that parses and whose entries it drops one by one is not a
    safer state than a file that does not parse -- it is the same loss arriving quietly.

    A refusal is the recoverable choice and an acceptance is not, which is what settles
    every case here. Refusing leaves the bytes in the bucket for a repair to read;
    accepting spends them. That is also what separates this from refusing an authority
    file the bucket never held: there, a refusal protects nothing, because there are no
    bytes behind the absence.

    The one entry the backend drops that this does NOT refuse is an open-slot key holding
    a path separator. The backend rejects that deliberately and warns, as a screen against
    a key smuggled in to escape the sessions directory, so its removal is the designed
    outcome rather than a loss -- and refusing the boot on it would let one planted byte in
    the bucket deny every replacement task permanently.

    Raises :class:`RestoreFailed` naming the file and what was wrong with it.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RestoreFailed(
            f"{name} in the bucket is not UTF-8 text ({exc}). The backend would read it "
            "as no conversations and then replace it, so the task refuses to start "
            "rather than boot into an empty slot table."
        ) from exc
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise RestoreFailed(
            f"{name} in the bucket is not valid JSON ({exc}). The backend would read it "
            "as no conversations and then replace it, so the task refuses to start "
            "rather than boot into an empty slot table."
        ) from exc
    if not isinstance(parsed, dict):
        raise RestoreFailed(
            f"{name} in the bucket is a JSON {type(parsed).__name__}, not an object. "
            "The backend requires an object at the top level and ignores anything else, "
            "so this would boot the task with an empty slot table."
        )
    if name == "open_slots.json":
        _validate_open_slots(name, parsed)
    else:
        _validate_session_map(name, parsed)


def _validate_session_map(name: str, parsed: dict) -> None:
    """Refuse a session-map entry the backend's loader would skip or its prune would drop.

    Its loader keeps a value that is a string (the legacy form it migrates) and a value
    that is an object carrying ``sid``. Every other value meets a bare ``continue``
    commented "skip corrupt entries" -- no log, no count -- and the map it then writes back
    omits that conversation.

    ``sid`` must itself be a STRING, because the loader's check is that the key is present
    and the startup prune's is that the value is truthy. A ``sid`` of ``[]`` passes the
    loader and reads as empty to the prune, which collects the entry as stale and removes
    it; a ``sid`` of ``5`` reads as truthy and sends the prune looking for ``5.json``,
    which is absent, so it is collected too. Either way the mapping is deleted and the next
    flush publishes the deletion. An EMPTY string is not a fault: the backend writes one
    itself when it stashes a session id that stopped resolving, keeping the entry.
    """
    for key, value in parsed.items():
        if isinstance(value, str):
            continue
        if isinstance(value, dict) and isinstance(value.get("sid"), str):
            continue
        raise RestoreFailed(
            f"{name} in the bucket maps {key!r} to a "
            f"{type(value).__name__ if not isinstance(value, dict) else 'object whose sid is not a string'}"
            ", which the backend skips as corrupt without logging it. Loading this file "
            "would drop that conversation's pointer and the next flush would delete it "
            "from the bucket, so the task refuses to start while the bytes are still "
            "there to repair."
        )


def _validate_open_slots(name: str, parsed: dict) -> None:
    """Refuse an open-slots member the backend's loader would silently drop.

    Its reader yields no slots at all when ``keys`` is not a list, which is the
    empty-list-that-looks-restored case, and an ABSENT ``keys`` is legal and means no open
    tabs. Each member is then folded by a screen that rejects a non-string and an empty
    string by returning nothing at all.

    A member holding a path separator is the one thing that screen rejects and this does
    not: it warns, and the rejection is the security outcome it exists for.
    """
    if "keys" not in parsed:
        return
    listed = parsed["keys"]
    if not isinstance(listed, list):
        raise RestoreFailed(
            f"{name} in the bucket has a 'keys' field that is a "
            f"{type(listed).__name__}, not a list. The backend reads that as no "
            "open slots, so the task would come up with an empty conversation list."
        )
    for member in listed:
        if isinstance(member, str) and member:
            continue
        shown = "an empty string" if isinstance(member, str) else f"a {type(member).__name__}"
        raise RestoreFailed(
            f"{name} in the bucket lists {shown} among its open slots, which the backend's "
            "own screen folds to nothing and drops without a warning. The tab would not be "
            "reopened and the next flush would remove it from the bucket, so the task "
            "refuses to start while the bytes are still there to repair."
        )


def _write(settings: Settings, name: str, raw: bytes) -> bool:
    """Put *raw* at the authority file's local path. ``False`` if one was already there.

    The parent is checked for a symlink first, for the reason the front checks the
    sessions directory: ``mkdir(exist_ok=True)`` succeeds on a link to a directory and
    every write then lands wherever the link points, which for these two files means
    the task's whole conversation index written outside the data home.
    """
    parent: Path = settings.config_dir
    if parent.is_symlink():
        raise RestoreFailed(
            f"the config directory is a symlink: {parent}. Writing {name} through it "
            "would put this task's conversation index outside the data home."
        )
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RestoreFailed(
            f"the config directory could not be created at {parent} ({exc}), so {name} "
            "cannot be restored."
        ) from exc
    try:
        return statefile.link_new(parent / name, raw, prefix=f".smc-restore-{name}-")
    except OSError as exc:
        raise RestoreFailed(f"{name} could not be written to {parent} ({exc}).") from exc


def _authority_source(settings: Settings, store: ObjectStore) -> tuple[str | None, frozenset[str]]:
    """Which generation to read, as ``(slot, names the commitment covers)``.

    ``(None, every authority name)`` is GENERATION 0: no pointer has been published, so
    the legacy ``data/`` keys are what this bucket has. They are never rewritten by the
    generation protocol, which is what lets a bucket be read without being migrated.

    A pointer that exists and cannot be used raises instead, because absent and unusable
    decide opposite things: absent says nothing is committed and the legacy keys are the
    truth, while unusable says a generation may be committed and this task cannot tell
    which, so reading the legacy keys would boot from the objects the pointer was steering
    away from.
    """
    try:
        pointer = generation.read_pointer(settings, store)
    except StoreUnusable as exc:
        raise RestoreFailed(
            f"the bucket itself could not be read ({exc}), so which generation is "
            "committed cannot be established. This is not the same as the pointer being "
            "absent, so the task refuses to start rather than boot into an empty slot "
            "table and flush it over the real one."
        ) from exc
    except generation.PointerUnusable as exc:
        raise RestoreFailed(
            f"{exc} The task refuses to start rather than boot from a generation it cannot "
            "confirm is the committed one."
        ) from exc
    if pointer is None:
        log.info("restore: no generation pointer, so the legacy keys are generation 0")
        return None, frozenset(keys.AUTHORITY_NAMES)
    log.info("restore: generation slot %s is committed", pointer.slot)
    return pointer.slot, pointer.authority


def restore_authority(settings: Settings, store: ObjectStore) -> RestoreResult:
    """Fetch, validate and write both authority files. Raise if any of it fails.

    The pair is read from the COMMITTED generation and from nowhere else. A pointer object
    names the slot whose pair was published whole, a writer publishes into the other slot,
    and commitment is that one object -- so a publication interrupted halfway damages only
    a slot this function never looks at, and there is no state in which half a commitment
    is visible.

    Six cases, each decided here. No pointer: generation 0, the legacy keys, and an
    authority file absent there is absent rather than torn -- a crew that never published
    one has nothing a boot could overwrite. A pointer whose generation holds every name it
    was committed with: the ordinary restore. A pointer whose generation is missing one of
    those names: refused, because that pair lost a member, and the next complete cycle
    republishes into the other slot and commits it, which repairs the bucket. A pointer
    present but unusable: refused, since unusable is not absent. A bucket with only legacy
    keys: generation 0 again. A bucket with legacy keys AND a committed generation: the
    pointer wins and the legacy keys are ignored, because they are the older publication.

    A read that fails for any OTHER reason is failure too, including a denial -- reading a
    denial as absence is the same route to booting with an empty slot table.
    """
    result = RestoreResult()
    fetched: dict[str, bytes] = {}
    slot, committed_names = _authority_source(settings, store)
    for name in keys.AUTHORITY_NAMES:
        key = (
            keys.authority_key(settings, name)
            if slot is None
            else keys.authority_slot_key(settings, slot, name)
        )
        try:
            raw = store.get(key, limit=MAX_OBJECT_BYTES)
        except ObjectAbsent:
            log.info("restore: %s is not in the bucket", name)
            result.absent.append(name)
            continue
        except Exception as exc:  # noqa: BLE001 - translated, never swallowed
            raise RestoreFailed(
                f"{name} could not be read from the bucket ({exc}). This is not the same "
                "as it being absent, so the task refuses to start rather than boot into "
                "an empty slot table and flush it over the real one."
            ) from exc
        validate_authority(name, raw)
        fetched[name] = raw
    if slot is not None:
        missing = sorted(name for name in committed_names if name in result.absent)
        if missing:
            raise RestoreFailed(
                f"generation {slot} is committed with "
                f"{', '.join(sorted(committed_names))} but does not hold "
                f"{', '.join(missing)}. A committed generation that lost a member is not a "
                "first boot: starting from the rest would let the backend flush its own "
                "empty view of the missing one over a real conversation list. The task "
                "refuses to start, and the next complete cycle commits the other slot."
            )
    for name, raw in fetched.items():
        if _write(settings, name, raw):
            log.info("restore: %s restored, %d B", name, len(raw))
            result.restored.append(name)
        else:
            log.info(
                "restore: %s is already on disk; keeping the local copy, which leads the "
                "bucket by up to one backup interval",
                name,
            )
            result.kept_local.append(name)
    log.info("restore: complete -- %s", result.summary())
    return result
