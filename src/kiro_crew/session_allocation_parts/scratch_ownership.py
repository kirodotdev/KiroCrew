"""Whether a resumed session may rejoin the scratch directory recorded for its key.

The tree index (:mod:`kiro_crew.agent_scratch_trees`) can bind keys of unrelated
session trees to one directory. A resume hands its recorded directory back only
when every key that owns it resolves to one session tree under a settled lineage.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from kiro_crew import agent_scratch_trees
from kiro_crew.dashboard.chat_utils_awaits import run_to_completion

#: How many parent links a subagent key's lineage is followed before it is unknown.
_LINEAGE_MAX_HOPS = 8


async def _placement(key: str) -> tuple[str | None, bool, bool, tuple[str, ...]]:
    """``(root, verified, claimed, path)`` for *key*.

    A dashboard chat is placed by the gateway reader, which judges the chain while
    it walks it; an unverified chat is its own identity. A subagent is placed
    through its run record's ``parent_session``. Once that record is pruned, the
    parent the gateway recorded beside its binding at spawn time stands in, and
    *claimed* is True: the caller must still corroborate it. Any other key, or a
    subagent with neither, cannot be placed (root None). *path* is the keys walked
    after *key*, so an unplaced walk names the key it stopped at.
    """
    from kiro_crew import subagent_persistence
    from kiro_crew.agent_sdk.chat_runtime_sharing import share_tree_placement_of

    chat = key
    claimed = False
    seen: set[str] = set()
    path: list[str] = []
    for _ in range(_LINEAGE_MAX_HOPS):
        if chat.startswith("dashboard:"):
            root, verified = share_tree_placement_of(chat)
            done = tuple(path)
            return (root, True, claimed, done) if verified else (chat, False, claimed, done)
        run_id = subagent_persistence.subagent_id_from_conversation_key(chat)
        if run_id is None or chat in seen:
            return None, True, claimed, tuple(path)
        seen.add(chat)
        record = await asyncio.to_thread(subagent_persistence.read_state, run_id)
        parent = record.get("parent_session") if isinstance(record, dict) else None
        if record is None:
            parent = await asyncio.to_thread(agent_scratch_trees.recorded_parent, chat)
            claimed = claimed or bool(parent)
        if not isinstance(parent, str) or not parent:
            return None, True, claimed, ()
        chat = parent
        path.append(chat)
    return None, True, claimed, ()


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
    from kiro_crew import subagent_persistence

    placed = {k: await _placement(k) for k in sorted(owners)}
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
    except OSError:
        logger.warning(
            "session %s: the tree quarantine is unreadable; not rejoining recorded work "
            "directory %r and starting in a fresh directory",
            key,
            tree.name,
            exc_info=True,
        )
        return None
    # The keys whose ownership has to agree: those bound now, plus every key the
    # directory was bound to when a resume could not settle it. A key that has
    # moved away since still owns what it staged there.
    owners = members | (marked or frozenset())
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
                "session %s: %s; not rejoining recorded work directory %r, which %d "
                "sessions own, and starting in a fresh directory",
                key,
                unavailable,
                tree.name,
                len(owners),
            )
            return None
    if len(roots) <= 1:
        if marked is not None:
            if not await run_to_completion(
                asyncio.to_thread(agent_scratch_trees.clear_quarantine, tree, frozenset(owners))
            ):
                return None
        return tree
    # One shielded unit: a cancelled resume still finishes the retirement and, if
    # its index write failed, the mark, so a restart never finds the mark gone
    # while a binding to the directory stays.
    await run_to_completion(asyncio.to_thread(_retire_or_mark, tree, frozenset(owners)))
    logger.warning(
        "session %s: recorded work directory %r is recorded by %d session trees; "
        "starting in a fresh directory",
        key,
        tree.name,
        len(roots),
    )
    return None
