"""Whether a resumed session may rejoin the scratch directory recorded for its key.

The tree index (:mod:`kiro_crew.agent_scratch_trees`) can bind keys of unrelated
session trees to one directory. A resume hands its recorded directory back only
when every key that owns it resolves to one session tree under a settled lineage.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from kiro_crew import agent_scratch_trees
from kiro_crew.dashboard.chat_utils_awaits import run_to_completion
from kiro_crew.work_vocab import WORK_MAX_DEPTH_MAX

logger = logging.getLogger(__name__)

#: How many keys a lineage walk reads before the key counts as unknown: one per
#: subagent level ``work_ledger.max_depth`` allows, plus the root chat.
_LINEAGE_MAX_HOPS = WORK_MAX_DEPTH_MAX + 1


#: What an owner's placement needs from disk, read before the lineage snapshot:
#: ``(chat, path, claimed)``. *chat* is the dashboard key the walk reached, or ""
#: when it stopped before one; *path* is the keys walked after the owner; *claimed*
#: says a pruned run record was stood in for by a recorded spawn parent. A tuple,
#: not a class: this module defines no classes of its own (the bindings inventory).
_PreRead = tuple[str, tuple[str, ...], bool]


async def _pre_read(key: str) -> _PreRead:
    """Walk *key*'s run records (and recorded parents) off the loop. No lineage read.

    A subagent runs in its parent's directory, so the walk follows each run
    record's ``parent_session`` until it reaches a dashboard chat. Once a record is
    pruned, the parent the gateway recorded beside the binding at spawn (or that an
    earlier walk backfilled from the record) stands in and the placement is
    *claimed*: the caller must still corroborate it. Any other
    key, or a subagent with neither, stops the walk (``chat`` empty).
    """
    from kiro_crew import subagent_persistence

    chat = key
    claimed = False
    seen: set[str] = set()
    path: list[str] = []
    for _ in range(_LINEAGE_MAX_HOPS):
        if chat.startswith("dashboard:"):
            return chat, tuple(path), claimed
        run_id = subagent_persistence.subagent_id_from_conversation_key(chat)
        if run_id is None or chat in seen:
            return "", tuple(path), claimed
        seen.add(chat)
        record = await asyncio.to_thread(subagent_persistence.read_state, run_id)
        parent = record.get("parent_session") if isinstance(record, dict) else None
        if record is None:
            parent = await asyncio.to_thread(agent_scratch_trees.recorded_parent, chat)
            claimed = claimed or bool(parent)
        elif isinstance(parent, str) and parent:
            # Backfill the parent row while the record still names the parent, so a
            # later prune still finds a parent to place this key by.
            await asyncio.to_thread(agent_scratch_trees.backfill_parent, chat, parent)
        if not isinstance(parent, str) or not parent:
            return "", (), claimed
        chat = parent
        path.append(chat)
    return "", (), claimed


def _place(pre: _PreRead) -> tuple[str | None, bool, bool, tuple[str, ...]]:
    """``(root, verified, claimed, path)`` from *pre*; synchronous, inside one reading.

    A dashboard chat is placed by the gateway reader, which judges the chain while
    it walks it; an unverified chat is its own identity. A walk that stopped before
    a dashboard chat cannot be placed (root None).
    """
    from kiro_crew.agent_sdk.chat_runtime_sharing import share_tree_placement_of

    chat, path, claimed = pre
    if not chat:
        return None, True, claimed, path
    root, verified = share_tree_placement_of(chat)
    return (root, True, claimed, path) if verified else (chat, False, claimed, path)


async def _placement(key: str) -> tuple[str | None, bool, bool, tuple[str, ...]]:
    """``(root, verified, claimed, path)`` for *key*: :func:`_pre_read`, then :func:`_place`."""
    return _place(await _pre_read(key))


def _lineage_unavailable_reason() -> str:
    """Load the crew log's session tree; ``""`` once its lineage is settled. Blocking.

    The same fold a restore runs (``projection().ensure_seeded()``), so a closed
    chat's creator can be read. Otherwise the reason it cannot be: the caller
    must then neither treat two keys as different trees nor as one.
    """
    from kiro_crew import crew_mode
    from kiro_crew.crew_log import emit as crew_log_emit

    if not crew_log_emit.enabled():
        return "the crew log is off"
    try:
        from kiro_crew.crew_log.session_tree_projection import projection

        projection().ensure_seeded()
    except Exception:  # noqa: BLE001 - an unloadable lineage is unknown
        return "the session-tree lineage could not be loaded"
    return "" if crew_mode.lineage_is_settled() else "the session-tree lineage is not settled"


async def _owner_roots(owners: frozenset[str]) -> tuple[set[str], bool]:
    """The session-tree roots of *owners*, and whether every one of them is verified.

    Every owner is placed against ONE reading of the persisted lineage
    (``crew_mode.one_lineage_reading``). If the lineage moved before the last owner
    was placed, no root is trusted: each owner is its own unverified identity, so
    the caller takes the quarantine path rather than certify a mix of generations.

    An owner that cannot be placed (a subagent whose run record was pruned, a cron
    or channel key) and an owner with an unverified creator each add their own
    identity and make the set unverified: a co-owner is never dropped, so it never
    lets another chat's tree look like the only one.

    A subagent placed only by its recorded spawn parent counts as placed only when
    that parent resolves, verified, to the same root as another current owner
    placed without any claim; otherwise it is its own unverified identity. A subagent
    whose parent chain (run records, or parent rows where a record is pruned) runs
    through current owners only to an unplaceable channel or cron co-owner takes
    that owner's identity.
    """
    from kiro_crew import crew_mode, subagent_persistence

    # Every disk read happens first; the placement inside the reading is synchronous,
    # so ``current()`` can only be False for a lineage write that truly landed
    # between the snapshot and the decision.
    pre = {k: await _pre_read(k) for k in sorted(owners)}
    with crew_mode.one_lineage_reading() as lineage:
        placed = {k: _place(pre[k]) for k in sorted(owners)}
        if not lineage.current():
            return set(owners), False
    proven = {root for root, ok, claimed, _ in placed.values() if root and ok and not claimed}
    roots: set[str] = set()
    verified = True
    for k, (root, ok, claimed, path) in placed.items():
        if (
            root is None
            and path
            and all(hop in placed for hop in path)
            and placed[path[-1]][0] is None
            and subagent_persistence.subagent_id_from_conversation_key(path[-1]) is None
        ):
            # The run records (or, once pruned, the gateway's parent rows) lead from
            # this subagent through co-owners only to a channel or cron co-owner that
            # cannot be placed: this key is that owner's identity, which its own
            # entry adds with its own flag.
            continue
        if root is None or not ok or (claimed and root not in proven):
            roots.add(k if root is None or claimed else root)
            verified = False
        else:
            roots.add(root)
    return roots, verified


def _retire_or_mark(tree: Path, owners: frozenset[str]) -> bool:
    """Retire every binding to *tree* under a mark of *owners*; on a failed write, hold the mark. Blocking."""
    if agent_scratch_trees.abandon_tree_all(tree, owners):
        return True
    agent_scratch_trees.quarantine_tree(tree, owners)
    return False


async def _recorded_tree_of_own_session_tree(key: str, logger: Any) -> Path | None:
    """*key*'s recorded scratch tree, or None when chats of two session trees record it.

    The index can bind keys of unrelated trees to one directory, and rebinding
    it on every resume would keep them mounting each other's staged files. A
    directory whose keys resolve to two different known tree roots is handed
    back to none of them: every binding to it is retired, and a mark of its
    owners written, in one index write, and the resume starts in a fresh
    directory (the old one stays on disk for the sweep, and the mark refuses it
    to any key bound to it later). A co-owner that cannot be placed counts as its own tree, so it is
    never dropped from the comparison.
    Owners that live slots and run records place in one tree rejoin without the
    persisted lineage. When those answers disagree and the lineage is not
    settled, the directory is neither handed back nor retired: it is quarantined
    with its keys (:func:`agent_scratch_trees.quarantine_tree`), and the mark
    holds on every later resume until the owners resolve to one tree.
    """
    tree = await asyncio.to_thread(agent_scratch_trees.recorded_tree, key)
    if tree is None:
        return None
    members = await asyncio.to_thread(agent_scratch_trees.keys_on_tree, tree)
    if key not in members:
        # Retired between the two reads: another resume refused this directory.
        return None
    try:
        marked = await asyncio.to_thread(agent_scratch_trees.quarantined_keys, tree)
        former = await asyncio.to_thread(agent_scratch_trees.former_owners, tree)
        held = await asyncio.to_thread(agent_scratch_trees.held_refusal, tree)
    except OSError:
        logger.warning(
            "session %s: the tree quarantine is unreadable; not rejoining recorded work "
            "directory %r and starting in a fresh directory",
            key,
            tree.name,
            exc_info=True,
        )
        return None
    if await _settle_ownership(tree, members, marked, former, held, f"session {key}", logger):
        return tree
    return None


async def _settle_ownership(
    tree: Path,
    members: frozenset[str],
    marked: frozenset[str] | None,
    former: frozenset[str] | None,
    held: frozenset[str] | None,
    who: str,
    logger: Any,
) -> bool:
    """Weigh every owner *tree* has and act on the answer; True when it may be mounted.

    The one place an ownership question is answered: one tree clears any mark,
    former-owner row or held refusal and the directory may be mounted; two trees
    retire every binding under a mark; an answer that cannot be reached
    quarantines it. Every mount seam reaches it, through the resume here or
    :func:`agent_scratch_trees.resolve_open_ownership`.
    """
    # The keys whose ownership has to agree: those bound now, plus every key the
    # directory was bound to when a resume could not settle it. A key that has
    # moved away since still owns what it staged there.
    owners = members | (marked or frozenset()) | (former or frozenset()) | (held or frozenset())
    # Live slots and run records place most owners without the persisted lineage
    # (a chat and its own dedicated subagent, for one). The lineage is needed only
    # when those answers disagree: a chat with no live slot reads as its own tree,
    # and only the lineage can say whether it belongs to another.
    roots, verified = await _owner_roots(owners)
    if len(roots) > 1:
        unavailable = await asyncio.to_thread(_lineage_unavailable_reason)
        if not unavailable:
            roots, verified = await _owner_roots(owners)  # again, with the loaded lineage
            if len(roots) > 1 and not verified:
                # Owners in two places, one of them only by an unverified creator:
                # neither one tree nor two can be shown, so quarantine, never retire.
                unavailable = "a session's creator could not be verified"
        if unavailable:
            # Without the lineage a shared directory can be neither shown to be one
            # tree (sharing it could expose another tree's files) nor shown to be
            # two (retiring deletes bindings). The directory is quarantined with its
            # keys, so no later resume rejoins it while the lineage is unreadable,
            # however many keys are still bound to it. This resume starts in a fresh
            # directory: the other sessions' bindings stay, this session's own
            # binding moves to its fresh directory, and the old one stays on disk.
            await run_to_completion(
                asyncio.to_thread(agent_scratch_trees.quarantine_tree, tree, frozenset(owners))
            )
            logger.warning(
                "%s: %s; not rejoining recorded work directory %r, which %d "
                "sessions own, and starting in a fresh directory",
                who,
                unavailable,
                tree.name,
                len(owners),
            )
            return False
    if len(roots) <= 1:
        if marked is not None or former is not None or held is not None:
            if not await run_to_completion(
                asyncio.to_thread(agent_scratch_trees.clear_quarantine, tree, frozenset(owners))
            ):
                return False
        # An overflow of this process's held refusals refuses every directory
        # without a durable mark; only the mount check carries it.
        if not await asyncio.to_thread(agent_scratch_trees.refused_across_trees, tree):
            return True
        logger.warning(
            "%s: recorded work directory %r is refused in this process; starting in a "
            "fresh directory. Its staged files are untouched on disk.",
            who,
            tree.name,
        )
        return False
    # One shielded unit: a cancelled resume still finishes the retirement and, if
    # its index write failed, the mark, so a restart never finds the mark gone
    # while a binding to the directory stays.
    await run_to_completion(asyncio.to_thread(_retire_or_mark, tree, frozenset(owners)))
    logger.warning(
        "%s: recorded work directory %r is recorded by %d session trees; "
        "starting in a fresh directory",
        who,
        tree.name,
        len(roots),
    )
    return False


async def _settle_open_ownership(tree: Path) -> bool:
    """The resolver a mount seam calls for a directory with an open ownership question."""
    try:
        members = await asyncio.to_thread(agent_scratch_trees.keys_on_tree, tree)
        marked = await asyncio.to_thread(agent_scratch_trees.quarantined_keys, tree)
        former = await asyncio.to_thread(agent_scratch_trees.former_owners, tree)
        held = await asyncio.to_thread(agent_scratch_trees.held_refusal, tree)
    except OSError:
        return False
    return await _settle_ownership(
        tree, members, marked, former, held, f"mount of {tree.name!r}", logger
    )


agent_scratch_trees.set_ownership_resolver(_settle_open_ownership)
