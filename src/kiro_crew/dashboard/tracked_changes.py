"""A session's stored list of the pull and merge requests it works on.

The sidebar chips and the side panel's Changes tab today scan the loaded transcript
for links, so a restart that loads only a window loses older ones and a pasted link
shows up as if the session worked on it. This list is the stored answer instead: one
entry per change a writer (a created PR, a PR acted on, a work report, a monitor, the
code channel, the agent, the person) said this session works on. It lives on the
slot as ``_tracked_changes`` and is persisted in the slot's metadata line beside
``dismissed_source_links`` (``metadata_codec``), so it restores, rebinds and forks with
the transcript.

This module is storage only. Nothing writes the list yet except through
:func:`track_change`, and nothing renders it: it is readable through
``GET /api/chat/slots/{slot}/source-links?tracked=1`` (:func:`tracked_changes_view`).

The list holds changes (pull and merge requests) only; an issue URL is refused on
write and dropped on restore.

Instance fields of ``_ChatSlot`` this module reads and writes (initialised by
``state_parts/source_links.py``):

Writes: ``_tracked_changes``, ``_tracked_hydrated``, ``_dirty``.
Reads only: ``_dismissed_source_links``, ``_dismissed_hydrated``, ``_dismissed_txn_pending``.

Nothing here changes what a person sees: the chips, unlink and the side panel's tabs
read what they read without this module. Later slices add what this one leaves out:
the writers (S2, S3), the import of existing sessions and its marker (S4), pins and a
fork inheriting the parent's unlinked set (S5), and live sources a ``/clear`` keeps (S3).

The metadata line is a file the agent's own tools could write, so every entry read
from it is checked again here: its shape, its bounds, and that its URL still parses
to the identity it is stored under. An entry whose URL does not parse right now (a
self-managed host not yet in the allowlist) is kept but not shown.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import fields
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.source_providers.contract import (
    _MAX_URL_LENGTH,
    SourceRef,
    canonical_source_identity_key,
    is_valid_source_identity_key,
    source_ref_identity_key,
)

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import _ChatSlot

logger = logging.getLogger("kiro_crew.dashboard.tracked_changes")

# The handler-layer names (the URL parser, the allowlist, the lock) are imported in the
# functions that use them: that layer imports the dashboard state this module serves.

#: The metadata-line key this module owns (in ``history_schema.SLOT_OWNED_META_KEYS``).
LINE_KEY = "tracked_changes"

#: Entries kept per session. Past it, the oldest entry by ``added_at`` goes.
MAX_TRACKED_CHANGES = 64
#: Distinct source kinds recorded per entry; past it the oldest goes.
MAX_SOURCES_PER_ENTRY = 4
#: The longest URL an entry stores: the URL parser's own bound.
MAX_TRACKED_URL_CHARS = _MAX_URL_LENGTH

#: Who said the session works on a change.
SOURCE_KINDS = frozenset(
    {"created", "acted", "work_report", "monitor", "code_channel", "agent", "person", "seeded"}
)
#: A source's pointer (a tool-call digest, an item id, a loop id, a channel id).
_SOURCE_REF_RE = re.compile(r"[A-Za-z0-9._:@#/-]{0,128}")
_ADDED_AT_MAX_CHARS = 40


def _identity_kind_index() -> int:
    """Where ``kind`` sits in a serialized ``SourceRef.identity`` key."""
    members = [f.name for f in fields(SourceRef) if f.name != "url"]
    return members.index("kind")


def identity_is_change(key: object) -> bool:
    """Whether a serialized identity key names a pull or merge request, not an issue."""
    if not is_valid_source_identity_key(key):
        return False
    return bool(json.loads(str(key))[_identity_kind_index()] == "change")


def _parsed_ref(url: str) -> SourceRef | None:
    """*url* parsed as any source link on an allowed host, else ``None``."""
    from kiro_crew.dashboard.handlers.source_providers import parse_source_url

    try:
        return parse_source_url(url)
    except ValueError:
        return None


def _parsed_change(url: str) -> SourceRef | None:
    """*url* parsed as a pull or merge request on an allowed host, else ``None``."""
    ref = _parsed_ref(url)
    return ref if ref is not None and ref.kind == "change" else None


def _identity_key(ref: SourceRef) -> str:
    return source_ref_identity_key(ref.identity)


def _valid_added_at(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > _ADDED_AT_MAX_CHARS:
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _clean_sources(raw: object) -> list[dict] | None:
    """The entry's sources, distinct by kind and bounded, or ``None`` when unusable."""
    if not isinstance(raw, list):
        return None
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind, ref = item.get("kind"), item.get("ref", "")
        if not isinstance(kind, str) or kind not in SOURCE_KINDS or kind in seen:
            continue
        if not isinstance(ref, str) or not _SOURCE_REF_RE.fullmatch(ref):
            continue
        seen.add(kind)
        out.append({"kind": kind, "ref": ref})
    # The newest sources win: a writer appends, so the tail is the most recent.
    return out[-MAX_SOURCES_PER_ENTRY:] or None


def _clean_entry(raw: object) -> dict | None:
    """One entry's fields checked for shape and bounds (no URL parse), else ``None``."""
    if not isinstance(raw, dict):
        return None
    identity, url, added_at = raw.get("identity"), raw.get("url"), raw.get("added_at")
    if not identity_is_change(identity):
        return None
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    if len(url) > MAX_TRACKED_URL_CHARS or not _valid_added_at(added_at):
        return None
    sources = _clean_sources(raw.get("sources"))
    if sources is None:
        return None
    return {
        "identity": canonical_source_identity_key(str(identity)),
        "url": url,
        "sources": sources,
        "added_at": added_at,
    }


def _evict(entries: dict[str, dict]) -> int:
    """Drop the oldest entries until the cap holds; returns how many went."""
    excess = len(entries) - MAX_TRACKED_CHANGES
    if excess <= 0:
        return 0
    # Ties on ``added_at`` go by insertion order, oldest first.
    oldest = sorted(
        (entry["added_at"], index, key) for index, (key, entry) in enumerate(entries.items())
    )
    for _, _, key in oldest[:excess]:
        del entries[key]
    return excess


def sanitize_tracked_changes(raw: object) -> tuple[dict[str, dict], int]:
    """Entries a metadata line's value may restore, keyed by identity, and how many it dropped.

    Drops an entry that is malformed, names an issue, or whose URL parses to a
    different identity than the one it is stored under, and anything past the
    cap. An entry whose URL does not parse right now is kept: its host may join
    the allowlist later, and :func:`tracked_changes_view` re-parses it each read.
    """
    if not isinstance(raw, list):
        return {}, 0
    # Bounded while collecting: never more than the cap plus one entry held,
    # whatever the line's length.
    entries: dict[str, dict] = {}
    dropped = 0
    for item in raw:
        entry = _clean_entry(item)
        if entry is None or entry["identity"] in entries:
            dropped += 1
            continue
        # The URL check runs before the entry is admitted, so an entry whose URL
        # parses to an issue or to another identity can neither take a valid one's
        # identity nor push one past the cap. Only a URL that does not parse now
        # (a host not allowed yet) is kept unchecked.
        ref = _parsed_ref(entry["url"])
        # An issue URL parses to an issue identity, so the mismatch covers it too.
        if ref is not None and _identity_key(ref) != entry["identity"]:
            dropped += 1
            continue
        entries[entry["identity"]] = entry
        dropped += _evict(entries)
    ordered = dict(sorted(entries.items(), key=lambda item: item[1]["added_at"]))
    return ordered, dropped


def serialize_tracked_changes(entries: Mapping[str, dict]) -> list[dict]:
    """The line value for *entries*, oldest first (a copy, so the slot stays its own)."""
    # ``list()`` snapshots the values in one step: a save may fold this off the loop.
    return [
        {**entry, "sources": [dict(source) for source in entry["sources"]]}
        for entry in list(entries.values())
    ]


def restore_tracked_changes(slot: _ChatSlot, meta: Mapping[str, Any]) -> None:
    """Take the list from a READABLE metadata line.

    Authoritative for the slot: a line without the key leaves the slot with no
    entries, so a reused slot object cannot carry a previous binding's list.
    """
    entries, dropped = sanitize_tracked_changes(meta.get(LINE_KEY))
    if dropped:
        logger.warning(
            "tracked_changes restore dropped %d entr%s (malformed, mismatched or past the cap)",
            dropped,
            "y" if dropped == 1 else "ies",
        )
    slot._tracked_changes = entries
    slot._tracked_hydrated = True


def mark_tracked_changes_unread(slot: _ChatSlot) -> None:
    """Mark a re-bound slot's list as held only on disk.

    The in-memory list may be a reused slot object's from another transcript, so it
    is cleared. Until :func:`restore_tracked_changes` runs,
    the slot's saves write the stored value back and :func:`track_change` refuses.
    """
    slot._tracked_changes = {}
    slot._tracked_hydrated = False


def apply_tracked_changes_if_unread(slot: _ChatSlot, meta: Mapping[str, Any]) -> bool:
    """Restore a re-bound slot's list from a READABLE line, only while it is still unread.

    A list hydrated while the read ran is newer than the line in hand, which is then
    dropped. Returns whether it applied.
    """
    if getattr(slot, "_tracked_hydrated", True):
        return False
    restore_tracked_changes(slot, meta)
    return True


def tracked_changes_fold(
    slot: _ChatSlot, on_disk: Mapping[str, Any], *, merge: bool
) -> list[dict] | None:
    """What a save writes for the list; ``None`` leaves the key off.

    A slot whose list was not read back (``_tracked_hydrated`` False) holds an empty
    list that does not reflect disk, so the on-disk value is carried forward,
    checked again, never overwritten with an empty list. Otherwise the slot's own
    list is written. The full save rebuilds the line, so it clears by absence; the
    empty-window merge cannot delete a key, so it writes ``[]`` over a key already
    on disk and leaves an absent one absent.
    """
    if not getattr(slot, "_tracked_hydrated", True):
        carried, _ = sanitize_tracked_changes(on_disk.get(LINE_KEY))
        return serialize_tracked_changes(carried) or None
    entries = serialize_tracked_changes(slot._tracked_changes)
    if merge and not entries and LINE_KEY in on_disk:
        return []
    return entries or None


def _tombstoned(slot: _ChatSlot, identity: str) -> bool:
    """Whether the person unlinked *identity* here, or may have (an unread or tentative set)."""
    if not getattr(slot, "_dismissed_hydrated", True):
        return True
    keys = set(slot._dismissed_source_links) | set(slot._dismissed_txn_pending)
    return identity in {canonical_source_identity_key(key) for key in keys}


def record_tracked_change(slot: _ChatSlot, url: str, kind: str, ref: str = "") -> bool:
    """Add or merge one change into *slot*'s list; returns whether it marked the slot dirty.

    The caller holds the slot's source-link transaction lock (:func:`track_change`).
    Refused (``False``, nothing written): a URL that is not a change on an allowed
    host, an unknown source kind or malformed ref, an identity the person
    unlinked, and a slot whose list or tombstones are not read back yet. A repeat
    write of a listed identity with the same URL and an already-recorded source
    kind changes nothing, so it does not rewrite the transcript file.
    """
    if not isinstance(kind, str) or kind not in SOURCE_KINDS:
        return False
    if not isinstance(ref, str) or not _SOURCE_REF_RE.fullmatch(ref):
        return False
    if not isinstance(url, str) or len(url) > MAX_TRACKED_URL_CHARS:
        return False
    parsed = _parsed_change(url)
    if parsed is None or not getattr(slot, "_tracked_hydrated", True):
        return False
    identity = _identity_key(parsed)
    # An identity the restore would refuse (past the identity key's length bound)
    # is refused here, so a write never reports a change the next restart drops.
    if not identity_is_change(identity) or _tombstoned(slot, identity):
        return False
    entries = slot._tracked_changes
    entry = entries.get(identity)
    if entry is None:
        entries[identity] = {
            "identity": identity,
            "url": parsed.url,
            "sources": [{"kind": kind, "ref": ref}],
            "added_at": datetime.now(timezone.utc).isoformat(),
        }
        if _evict(entries):
            logger.info(
                "tracked_changes for %s reached the %d cap; the oldest entry went",
                slot.key,
                MAX_TRACKED_CHANGES,
            )
    else:
        changed = False
        if entry["url"] != parsed.url:
            entry["url"] = parsed.url
            changed = True
        if all(source["kind"] != kind for source in entry["sources"]):
            entry["sources"] = [*entry["sources"], {"kind": kind, "ref": ref}][
                -MAX_SOURCES_PER_ENTRY:
            ]
            changed = True
        if not changed:
            return False
    slot._dirty = True
    return True


async def track_change(slot: _ChatSlot, url: str, kind: str, ref: str = "") -> bool:
    """:func:`record_tracked_change` under the slot's source-link transaction lock.

    The lock is the one the unlink route holds, so a write and an unlink of the same
    transcript cannot interleave. The self-managed host allowlist is loaded first, as
    every async entry point that parses a URL does.
    """
    from kiro_crew.dashboard.chat_handlers import _source_link_txn_lock
    from kiro_crew.dashboard.chat_utils import slot_history_key
    from kiro_crew.dashboard.handlers.source_providers import ensure_gitlab_hosts_loaded

    try:
        await ensure_gitlab_hosts_loaded()
    except Exception:
        logger.debug("GitLab allowlist warm-up failed before a tracked write", exc_info=True)
    async with _source_link_txn_lock(slot_history_key(slot)):
        return record_tracked_change(slot, url, kind, ref)


def drop_on_clear(slot: _ChatSlot) -> bool:
    """A ``/clear`` that ran: drop every entry. Tombstones stay.

    shortcut: every entry goes, because no writer records a live source yet; the
    slice that adds the monitor and code-channel writers (S3) keeps the entries an
    armed monitor or the channel's current pull request still holds.

    Returns whether it marked the slot dirty; an unread list is left to disk.
    """
    if not getattr(slot, "_tracked_hydrated", True) or not slot._tracked_changes:
        return False
    slot._tracked_changes = {}
    slot._dirty = True
    return True


def copy_tracked_for_fork(child: _ChatSlot, parent: _ChatSlot) -> None:
    """Give a fork its parent's list, less the changes the person unlinked there.

    The fork's own unlinked set is left as it is today: copying the parent's
    tombstones would change which chips a fork shows, and it comes with the unlink
    slice (S5). So an entry the person unlinked on the parent, committed or still in
    flight, stays behind, and a parent whose list or tombstones are not read back
    gives the fork no list.
    """
    if not getattr(parent, "_tracked_hydrated", True):
        return
    if not getattr(parent, "_dismissed_hydrated", True):
        return
    unlinked = set(parent._dismissed_source_links) | set(parent._dismissed_txn_pending)
    hidden = {canonical_source_identity_key(key) for key in unlinked}
    child._tracked_changes = {
        key: copy.deepcopy(entry)
        for key, entry in parent._tracked_changes.items()
        if key not in hidden
    }


def tracked_changes_view(slot: _ChatSlot) -> list[dict]:
    """The list as the read route returns it, newest ``added_at`` first.

    Each URL is parsed again, so an entry on a host the allowlist does not hold,
    or whose identity does not match, shows nothing. A committed unlink hides its entry;
    one still in flight does not, matching the chips. Each link has the chip
    fields (``provider``, ``number``, ``url``, ``kind``, ``label``, ``identity``)
    plus ``sources`` (the source kinds, oldest first), ``origin`` (the newest) and
    ``added_at``.
    """
    from kiro_crew.dashboard.handlers.source_providers import source_ref_label

    if not getattr(slot, "_dismissed_hydrated", True):
        # The tombstones are not read back, so any entry may be one the person
        # unlinked: show none until they are.
        return []
    hidden = {
        canonical_source_identity_key(key)
        for key in set(slot._dismissed_source_links) - set(slot._dismissed_txn_pending)
    }
    # Ties on ``added_at`` go by insertion order, so the later write reads as newer.
    ranked = sorted(
        enumerate(slot._tracked_changes.values()),
        key=lambda item: (item[1]["added_at"], item[0]),
        reverse=True,
    )
    out: list[dict] = []
    for _, entry in ranked:
        ref = _parsed_change(entry["url"])
        if ref is None or _identity_key(ref) != entry["identity"] or entry["identity"] in hidden:
            continue
        out.append(
            {
                "provider": ref.provider,
                "number": ref.number,
                "url": ref.url,
                "kind": ref.kind,
                "label": source_ref_label(ref),
                "identity": entry["identity"],
                # The kinds only: a source's ``ref`` is an internal pointer read back
                # from an agent-writable line, so it never leaves the server.
                "sources": [source["kind"] for source in entry["sources"]],
                "origin": entry["sources"][-1]["kind"],
                "added_at": entry["added_at"],
            }
        )
    return out
