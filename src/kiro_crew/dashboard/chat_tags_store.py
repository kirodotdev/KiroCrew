"""Keeping the tag vocabulary: the per-state write lock, its snapshot and new definitions.

The lock that serializes every vocabulary write, the folder tag-id filter, the snapshot
written to disk and its reconciliation when a create fails, and the creation of a tag
definition on and off the event loop. A change to how the vocabulary is stored goes here.

Served through :mod:`kiro_crew.dashboard.chat_tags`, which callers import and tests
patch; its mirror (:mod:`kiro_crew._facade_mirror`) carries such a patch to this module,
and this module never imports it.
"""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
import weakref
from typing import Any

from kiro_crew.dashboard.chat_tag_grants import (
    mint_grant,
    refresh_cache,
    revoke_grant,
    store_write_blocked,
)
from kiro_crew.dashboard.chat_tags_policy import _COLOR_RE, _DEFAULT_COLOR, _NAME_MAX
from kiro_crew.dashboard.state import DashboardState, mint_tags_revision
from kiro_crew.loop_lock import LoopBoundLock

logger = logging.getLogger("kiro_crew.dashboard.chat_tags")


# Per-state tag-write lock. Serializes ALL mutations to state._tags + disk
# persistence so concurrent creates/updates/deletes cannot race the atomic
# fsync/os.replace sequence. Keyed weakly so a discarded state's lock is
# collectable (mirrors _RECONCILE_LOCKS in channel_slots.py).
_TAGS_WRITE_LOCKS: weakref.WeakKeyDictionary[Any, LoopBoundLock] = weakref.WeakKeyDictionary()


def validate_folder_tag_ids(raw: Any, state: DashboardState) -> list[str]:
    """Filter a folder's raw ``tags`` value down to usable tag ids.

    The definition of "a folder tag id a new chat may inherit" for the
    folder/inheritance paths, shared by the dashboard slot-create path
    (chat_handlers), the folder create/PATCH validation (chat_folders), the
    channel first-filing path, and the channel restore read (channel_slots)
    so an inheritance-rule change cannot land on one path and miss the
    others. (Three pre-existing slot-restore prunes elsewhere apply the same
    shape guards inline, and ``api_chat_slot_tags``'s strict inline filter is
    a fourth spelling differing only on the fail-open axis; consolidating all
    four onto this helper — likely via a strict-mode flag — is deliberately
    left to a follow-up. Those sites are outside this change.)
    Callers must invoke it AT THE POINT OF APPLICATION (immediately before the
    ids are written to a slot or persisted), never on a value resolved
    earlier — a tag deletion between resolve and apply would otherwise
    resurrect the deleted id.

    Two guard tiers, deliberately different in when they apply:

    * UNCONDITIONAL shape guards — ``raw`` must be a list (``folders.json``
      is hand-editable; anything else yields ``[]``), and each entry must be
      a string BEFORE the membership test (a non-string entry is unhashable
      and the test itself would raise, turning a malformed store row into a
      500 on every chat created in that folder). Order-preserving,
      de-duplicated.
    * AUTHORITY-GATED vocabulary intersection — ids are checked against the
      live vocabulary (``state._tags``) only when it is authoritative,
      mirroring the three sibling restore paths (``_tags_authoritative`` is
      False when ``tags.json`` was unreadable at boot). Failing OPEN there is
      load-bearing: intersecting with an unknown (empty) vocabulary would
      drop every id, the next save would persist the loss, and the sticky
      filing marker would block re-inheritance forever. A dangling id kept
      this way is pruned by the restore paths on the next authoritative boot.
    """
    if not isinstance(raw, list):
        return []
    check_vocab = getattr(state, "_tags_authoritative", True)
    # Only string ids enter the vocabulary set: a malformed persisted entry
    # (e.g. a hand-edited tags.json with a list/dict id) must degrade to
    # "unknown id" — not crash the set build with an unhashable type.
    valid_ids = (
        {i for t in state._tags if isinstance(i := t.get("id"), str)} if check_vocab else None
    )
    out: list[str] = []
    seen: set[str] = set()
    for tid in raw:
        if not isinstance(tid, str) or tid in seen:
            continue
        if valid_ids is not None and tid not in valid_ids:
            continue
        seen.add(tid)
        out.append(tid)
    return out


def _tags_write_lock(state: Any) -> LoopBoundLock:
    """Return (lazily create) the per-state lock for tag writes (loop-bound)."""
    lock = _TAGS_WRITE_LOCKS.get(state)
    if lock is None:
        lock = LoopBoundLock()
        _TAGS_WRITE_LOCKS[state] = lock
    return lock


# Public accessor — used by the tags/boards HTTP handlers of the chat_tags facade and by
# chat_auto_tag.maybe_auto_tag to hold the lock across an entire
# resolve→merge→persist critical section.
tags_write_lock = _tags_write_lock


def _write_tags_snapshot(state: DashboardState, snapshot: list[dict]) -> None:
    """Persist a pre-captured tag snapshot (runs in worker thread).

    Delegates to ``state.save_tags_snapshot`` so the file location resolves
    through ``kiro_crew.dashboard.state.config_dir`` exactly like
    ``save_tags`` (and stays patchable in tests)."""
    state.save_tags_snapshot(snapshot)


async def _reconcile_failed_tag_create(
    state: DashboardState,
    generated_tag: dict[str, Any],
    *,
    identity_was_minted: bool,
) -> dict[str, Any] | None:
    """Reconcile an ambiguous vocabulary write while the caller holds the tag lock."""
    tag_id = str(generated_tag["id"])
    durable = await asyncio.to_thread(state.read_durable_tags_snapshot)
    if durable is None:
        logger.warning(
            "tag create: durable vocabulary unreadable after write failure; "
            "withdrawing unconfirmed tag %s",
            tag_id,
        )
        # A 5xx response must not leave the unconfirmed vocabulary row or its
        # protected identity live in this process. If the write actually
        # committed, a later successful reload can recover the durable row; it
        # remains rowless and therefore cannot authorize an agent meanwhile.
        state._tags = [row for row in state._tags if row.get("id") != tag_id]
    elif durable.present:
        state._tags = durable.tags
        state._unparsed_tag_entries = durable.unparsed
        state._tags_authoritative = True
        committed = next((row for row in durable.tags if row.get("id") == tag_id), None)
        if committed is not None:
            logger.warning(
                "tag create: vocabulary write reported failure after commit for %s", tag_id
            )
            return committed
    else:
        state._tags = [row for row in state._tags if row.get("id") != tag_id]

    if identity_was_minted:
        try:
            await asyncio.to_thread(revoke_grant, tag_id)
        except Exception:
            logger.warning(
                "tag create: orphan identity revoke failed for %s", tag_id, exc_info=True
            )
    return None


async def persist_tags_snapshot_unlocked(state: DashboardState) -> None:
    """Persist the current state._tags to disk without acquiring the lock.

    Callers MUST hold `tags_write_lock(state)` themselves. Exported for use in
    cross-module critical sections (e.g. chat_auto_tag.maybe_auto_tag).
    """
    snapshot = [dict(t) for t in state._tags]
    await asyncio.to_thread(_write_tags_snapshot, state, snapshot)


def _valid_color(value: str) -> str:
    return value if _COLOR_RE.match(value) else _DEFAULT_COLOR


def _tag_by_id(state: DashboardState, tag_id: str) -> dict | None:
    return next((t for t in state._tags if t.get("id") == tag_id), None)


def _bump_slot_tags_revision(slot: Any) -> str:
    """Rotate a slot's tag revision while preserving duck-typed callers."""
    bump_revision = getattr(slot, "bump_tags_revision", None)
    if callable(bump_revision):
        return str(bump_revision())
    revision = mint_tags_revision()
    slot.tags_revision = revision
    return revision


def create_tag_definition(
    state: DashboardState,
    name: str,
    color: str | None = None,
    *,
    status: bool = False,
) -> dict:
    """Create a new tag definition (sync, no lock).

    Shared by the async locked callers. Mutates state._tags in place.
    Returns the created tag dict.
    """
    clean_name = name.strip()[:_NAME_MAX]
    if not clean_name:
        raise ValueError("tag name must not be empty")
    clean_color = _valid_color(str(color or _DEFAULT_COLOR))
    tag = {
        "id": uuid.uuid4().hex[:12],
        "name": clean_name,
        "color": clean_color,
        "order": len(state._tags),
        "status": bool(status),
    }
    state._tags.append(tag)
    return tag


class TagGrantStoreBlocked(RuntimeError):
    """An owner-browser create refused because the grant store's write gate is closed.

    Typed so the route can record the refusal on the SEL as a ``denied``
    permission decision, distinct from a failed write (``error``).
    """

    def __init__(self, blocked: str) -> None:
        super().__init__(f"agent-tag grant store unavailable: {blocked}")
        self.blocked = blocked


TagGrantStoreBlocked.__module__ = "kiro_crew.dashboard.chat_tags"


async def create_tag_definition_off_loop(
    state: DashboardState,
    name: str,
    color: str | None = None,
    *,
    status: bool = False,
    record_protected_identity: bool,
) -> dict:
    """Async wrapper: creates a tag definition under the shared write lock.

    Acquires the per-state tag-write lock (shared with update/delete) so
    concurrent callers creating the same missing tag name produce exactly one
    definition. The sync fsync write runs in a worker thread while the lock is
    held.

    ``record_protected_identity`` is true only after the HTTP route has
    positively verified an owner-dashboard browser request. Internal MCP
    creation still persists the vocabulary row, but it cannot mint the
    protected identity or default policy that the same agent would consume.

    Raises on persist failure so the caller can surface HTTP 5xx.
    """
    async with _tags_write_lock(state):
        # Re-check under lock: another caller may have just created it.
        lower = name.strip().lower()
        existing = next(
            (t for t in state._tags if (t.get("name") or "").lower() == lower),
            None,
        )
        if existing:
            return existing
        if record_protected_identity:
            # Same write gate as PATCH and adoption: mint only into a store
            # that currently verifies. ``mint_grant`` alone refuses a present
            # but unverifiable store, yet it recreates a MISSING one from an
            # empty document, which after a failed quarantine reseed would
            # write an owner row into a store with no trusted defaults.
            await asyncio.to_thread(refresh_cache)
            blocked = store_write_blocked()
            if blocked is not None:
                logger.warning("tag create: permission data unavailable (%s)", blocked)
                raise TagGrantStoreBlocked(blocked)
        tag = create_tag_definition(state, name, color, status=status)
        if record_protected_identity:
            # Only a positively verified owner-dashboard browser create records
            # protected provenance. A plain label's ``none`` row records identity
            # without granting authority; a workflow state keeps the shipped
            # ``add-remove`` default. An internal MCP create deliberately skips
            # this block, so its vocabulary row remains rowless until owner
            # adoption.
            #
            # ORDERING (crash atomicity): the row is minted BEFORE the vocabulary
            # commit. A crash between the writes leaves an inert orphan row for an
            # id no vocabulary entry references, while the reverse order leaves a
            # durable tag that cannot prove its dashboard origin. A mint failure
            # aborts before anything is durable.
            policy = "add-remove" if status else "none"
            try:
                await asyncio.to_thread(mint_grant, str(tag["id"]), policy=policy, status=status)
            except Exception:
                logger.warning("agent-tag identity mint failed for %s", tag["id"], exc_info=True)
                state._tags = [t for t in state._tags if t.get("id") != tag["id"]]
                raise
        snapshot = [dict(t) for t in state._tags]
        try:
            await asyncio.to_thread(_write_tags_snapshot, state, snapshot)
        except Exception:
            committed = await _reconcile_failed_tag_create(
                state, tag, identity_was_minted=record_protected_identity
            )
            if committed is not None:
                return committed
            raise
        return tag


def _order_key(row: dict) -> int:
    """A tag's or column's stored position as a sort key; a non-number is 0.

    ``tags.json`` and ``tag_boards.json`` are loaded verbatim (only ``id`` is
    checked), and the product's own writers only ever store an int — so a
    non-numeric ``order`` is a hand edit. A sort key that raises on it takes
    down every reader of that file (the sidebar's tag list and columns, the MCP
    tag tools) for one bad row.
    """
    value = row.get("order", 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    return int(value)
