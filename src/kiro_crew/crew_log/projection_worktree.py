"""The ``worktree`` fold: the tree of work boards, keyed by its root -- the third key kind."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from kiro_crew.crew_log.entry_types import WORK_ENTRY_TYPE
from kiro_crew.crew_log.projection_values import _as_id, _as_int, _as_str
from kiro_crew.crew_log.schema import Entry
from kiro_crew.crew_log.session_tree import fold_citations

# worktree -- the tree of work BOARDS, keyed by its root. The THIRD key kind.
# --------------------------------------------------------------------------- #
#
# Nothing to do with a git worktree. A "work tree" here is what
# :attr:`~kiro_crew.work_vocab.WorkBoardConductor.parent_item` builds: a whole board
# hangs under ONE ITEM of a parent board, so a fleet of conductors is a tree whose nodes
# are boards and whose edges are items. The ``work`` fold cannot render it -- it binds to
# one slot and drops every entry naming another, by design -- and before this fold a
# dashboard block could not reach the tree at all: :func:`~kiro_crew.crew_log.
# session_tree.fold_citations` holds the placement rules but is a pure FUNCTION, and a
# block binds a fold NAME plus a dotted path. This fold is that name.
#
# The placement rules are NOT restated here. They are ``fold_citations``' -- first
# citation carrying a parent wins, an unfollowable citation degrades the child to a root
# that still carries it, a cycle marks every node on it -- and this fold is a second
# caller of exactly the machinery the session tree uses, which is what the data line
# generalized it for. What lives here is the part that is about WORK: that an edge is
# recorded as an ITEM id and has to be resolved to the board that created that item.

#: Boards (nodes) this fold retains, in the order it first saw them, with ``dropped``
#: beside them. Two hundred because a board is a conductor and a fleet that deep is
#: already past what one page renders; a tree larger than this reports short rather than
#: growing without bound.
WORKTREE_BOARD_LIMIT: Final[int] = 200

#: Entries the item-to-board map retains. The map answers ONE question -- which board
#: created the item a child board's ``parent_item`` cites -- and a board has at most one
#: ``parent_item``, so at most :data:`WORKTREE_BOARD_LIMIT` of its rows can ever be read.
#: The limit is twenty times that, as headroom for the ids that arrive before the
#: citation they will answer: an item's creation and the board hanging under it are
#: recorded in different logs, so the fold cannot know which ids matter while it reads.
WORKTREE_OWNER_LIMIT: Final[int] = 4_000

#: Characters of a board's goal kept as its label. CHARACTERS, not bytes -- the unit
#: every fold's clamp counts in -- and the row cost in
#: :data:`_TREE_FOLD_ROW_BYTES` is read OFF this clamp at four bytes a character.
#: Shorter than the ``work`` fold's own 2,000-character goal because a node in a tree is
#: labelled, not read: the board's own fold serves the whole goal to a reader who wants it.
WORKTREE_GOAL_CHARS: Final[int] = 200


def _worktree_start() -> dict[str, Any]:
    return {
        # The root the tree was read from, so a rendered value says which tree it is.
        # Set by ``bind_slot``, like the ``work`` fold's board.
        "root": "",
        # board slot -> that board's header row.
        "boards": {},
        # Board slots in the order first seen, which IS the citation order handed to
        # ``fold_citations`` -- and that function takes the order as given rather than
        # inventing one, so this list is load-bearing and not a convenience. The walk
        # yields the root's units first and then each worker's, so a parent board is
        # seen before the children it bound.
        "order": [],
        # item id -> the board that CREATED it. The edge source: a child board cites a
        # parent ITEM, and only this map turns that into a parent BOARD.
        "owner": {},
        # Entries and rows this fold refused, for the reason every fold here renders one:
        # a tree that silently shows fewer boards than the record holds reads as whole.
        "omitted": 0,
    }


def _worktree_copy(state: dict[str, Any]) -> dict[str, Any]:
    """Copy every container ``step`` can reach. Board rows are flat, so one level
    inside ``boards`` is the whole of it."""
    return {
        "root": state["root"],
        "boards": {slot: dict(row) for slot, row in state["boards"].items()},
        "order": list(state["order"]),
        "owner": dict(state["owner"]),
        "omitted": state["omitted"],
    }


def _worktree_bind_slot(state: dict[str, Any], slot: str) -> None:
    """The ROOT this tree is read from, as the reader names it, before the first entry."""
    state["root"] = slot


def _worktree_new_board(slot: str) -> dict[str, Any]:
    return {
        "board": slot,
        "parent_item": None,
        "depth": 0,
        "goal": "",
        "items": 0,
        "generation": "",
    }


def _worktree_step(state: dict[str, Any], entry: Entry) -> None:
    if entry.type != WORK_ENTRY_TYPE:
        return
    data = entry.data
    if data.get("mirror") is True:
        # A worker's own copy of a report whose board copy is in the board's log. A
        # sub-board's record relayed up (``relay`` set) is how that board reaches this
        # tree, so it is read.
        return
    slot = _as_str(data.get("slot"))
    if not slot:
        # An entry naming no board places nothing in a tree whose nodes ARE boards.
        state["omitted"] += 1
        return
    row = state["boards"].get(slot)
    if row is None:
        if len(state["boards"]) >= WORKTREE_BOARD_LIMIT:
            state["omitted"] += 1
            return
        row = state["boards"][slot] = _worktree_new_board(slot)
        state["order"].append(slot)

    # THE GENERATION RULE, one branch. A generation is an opaque id minted with a board's
    # record, so generations transition in LOG order and are never compared: a CONDUCTOR
    # entry carrying an id this row does not hold is the next board under that slot, and
    # its header replaces the earlier board's. Any other entry whose id disagrees is a
    # straggler from a board that was purged or superseded -- the log is append-only, so
    # it outlives that board forever -- and applying its header would resurrect it on
    # every fold.
    generation = _as_str(data.get("generation"))
    conductor = data.get("actor") == "conductor"
    if generation and generation != row["generation"]:
        if not conductor and row["generation"]:
            state["omitted"] += 1
            return
        if conductor and row["generation"]:
            # Header only. ``owner`` is deliberately NOT cleared: see the module note
            # above this fold and "Deliberate limitations" in the evidence -- an item id
            # from a superseded generation still identifies the board that created it,
            # which is the board a ``parent_item`` citing that id meant.
            fresh = _worktree_new_board(slot)
            for key in ("parent_item", "depth", "goal"):
                row[key] = fresh[key]
        row["generation"] = generation
    elif not generation and row["generation"]:
        # A stamped board is live, so an entry with no generation cannot be its.
        state["omitted"] += 1
        return

    # The citation, and the board label beside it. ``parent_item`` and ``depth`` are
    # first-write-wins for :func:`_work_apply_header`'s reason: they are the board's
    # lineage, which is minted once and never rewritten, so a later entry repeating them
    # is a repeat and not a correction.
    parent_item = data.get("parent_item")
    if isinstance(parent_item, str) and row["parent_item"] is None:
        row["parent_item"] = _as_id(parent_item)
    if "depth" in data and not row["depth"]:
        row["depth"] = _as_int(data.get("depth"))
    # The goal comes from a conductor's ``goal`` write, which is the authoritative one;
    # a worker baseline supplies it only while the record holds none for this board.
    if isinstance(data.get("goal"), str):
        if (conductor and _as_str(data.get("action")) == "goal") or not row["goal"]:
            row["goal"] = _as_str(data["goal"])[:WORKTREE_GOAL_CHARS]

    # The edge source. Only a CREATE or a BASELINE establishes which board owns an item:
    # a worker's report also carries ``slot`` and ``item_id``, and that slot is the board
    # the report was filed AGAINST, not a second claim on the item.
    item_id = _as_id(data.get("item_id"))
    if not item_id:
        return
    action = _as_str(data.get("action"))
    if action != "create" and data.get("baseline") is not True:
        return
    if item_id in state["owner"]:
        # First claim wins, the same rule the placement itself runs on. A second create
        # of one id is what the ``work`` fold omits too.
        state["omitted"] += 1
        return
    if len(state["owner"]) >= WORKTREE_OWNER_LIMIT:
        state["omitted"] += 1
        return
    state["owner"][item_id] = slot
    row["items"] += 1


def _worktree_rows(state: Mapping[str, Any]) -> int:
    """Every container this state retains, as a count of rows.

    BOTH kinds. A counter that weighed only ``boards`` would charge a tree of three
    boards and four thousand owner rows as three rows, and the owner map is the half
    that grows with the record rather than with the page.
    """
    boards = state.get("boards")
    owner = state.get("owner")
    return (len(boards) if isinstance(boards, Mapping) else 0) + (
        len(owner) if isinstance(owner, Mapping) else 0
    )


def _worktree_render(state: dict[str, Any]) -> dict[str, Any]:
    """The tree: one row per board, each with the parent the record resolves to.

    The placement is :func:`~kiro_crew.crew_log.session_tree.fold_citations`' answer and
    not this function's. What happens here is the translation either side of it: a cited
    ITEM becomes a cited BOARD on the way in, and a
    :class:`~kiro_crew.crew_log.session_tree.TreePlacement` becomes a board row on the
    way out. So the orphan and cycle handling a block binding this tree gets is the same
    code the Sessions table has been nesting sessions with.

    ``unresolved`` counts the boards whose ``parent_item`` names an item no board in THIS
    tree created, which is the ordinary reading for a root read from mid-tree: the item
    lives in a board above the root and the root's own citation cannot be followed. Those
    boards render as roots that still carry ``parent_item``, so a reader can tell "no
    parent" from "a parent outside this tree" -- and a count beside them says how many.
    """
    boards = state["boards"]
    owner = state["owner"]
    citations: list[tuple[str, str | None]] = []
    unresolved = 0
    for slot in state["order"]:
        row = boards.get(slot)
        if row is None:  # pragma: no cover - order and boards are written together
            continue
        cited_item = row["parent_item"]
        parent_board = owner.get(cited_item) if cited_item else None
        if cited_item and parent_board is None:
            unresolved += 1
        citations.append((slot, parent_board))
    placements = fold_citations(citations)
    rendered: list[dict[str, Any]] = []
    roots: list[str] = []
    cycles = 0
    for slot in state["order"]:
        row = boards.get(slot)
        placement = placements.get(slot)
        if row is None or placement is None:  # pragma: no cover - same population
            continue
        if placement.cycle:
            cycles += 1
        # A board with no FOLLOWABLE parent is a root of this tree, whatever its own
        # record cites. A cycle member is not a root: it has a parent the tree can
        # follow, and the consumer is told not to nest it rather than told it is a top.
        if placement.parent is None:
            roots.append(slot)
        rendered.append(
            {
                "board": slot,
                "parent": placement.parent,
                # The record's own citation, kept beside the resolved parent because the
                # two differ exactly where it matters: a board whose cited item is
                # outside the tree carries one and not the other.
                "parent_item": row["parent_item"],
                "cycle": placement.cycle,
                "depth": row["depth"],
                "goal": row["goal"],
                "items": row["items"],
                "generation": row["generation"],
            }
        )
    return {
        "root": state["root"],
        "boards": rendered,
        # The boards nothing in this tree hangs under, which is what a renderer starts
        # its walk from. A list and not a count: a tree read from a mid-tree root has
        # several, and a renderer needs to know WHICH.
        "roots": roots,
        "cycles": cycles,
        "unresolved": unresolved,
        "dropped": state["omitted"],
        "limit": WORKTREE_BOARD_LIMIT,
        "owner_limit": WORKTREE_OWNER_LIMIT,
        "goal_chars": WORKTREE_GOAL_CHARS,
    }
