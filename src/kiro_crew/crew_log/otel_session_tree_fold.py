"""The counting logic behind the two session-tree OTel metrics.

This module is the PURE part of the tree counting fold: the two measurements the
exporter reports, written as functions with no OpenTelemetry import, no bus
subscription, and no instrument of their own. The step/render/registration that
turns these into a fold on :mod:`kiro_crew.crew_log.bus` lives in the OTel export
skeleton; this module is split out so the counting is testable and reviewable on
its own, and so the skeleton wires two named functions rather than carrying the
logic inline.

THE TWO METRICS, and why each is shaped the way it is.

``kirocrew.crew.session.tree.depth`` is a HISTOGRAM, one observation per
``session/opened`` entry that CARRIES a ``depth``. :func:`depth_observation`
reads one entry's data and answers the number to record, or ``None`` to skip.
A histogram is the right instrument because the question is a distribution --
"how deep do delegated trees get" -- not a running total, and recording one
observation per opened session lets the exporter's own bucketing answer it.
Skipping is not miscounting: an entry written before the field existed, or one
whose writer could not establish a depth, simply contributes no observation, and
a distribution over the sessions that DO carry one is the honest answer rather
than a zero that would pull the low end down (see the ``depth`` field note in
``entry_types_lifecycle.py`` and :func:`~kiro_crew.crew_log.emit.on_session_opened`).

``kirocrew.crew.session.tree.roots`` is a COUNT of the depth-0 sessions that have
at least one child -- the trees that actually branched. :func:`count_roots_with_children`
reads the folded tree and answers that number. It is a SNAPSHOT property of the
tree, not an append-only tally: whether a root has a child is only known once the
child's own ``session/opened`` is folded, and a root gains and (on
``session/forgotten``) loses children over its life. Deriving it from the tree
the projection already holds is what keeps THIS fold's own state to fixed
counters with no slot set of its own: identity lives in
:mod:`kiro_crew.crew_log.session_tree_projection`, which holds the whole tree in
memory for its other readers, and this function is a read over that -- never a
second store of slot names in a metric fold, which the dev plan forbids and which
would put identity in a counter that has no business holding any.

Neither function raises on malformed input: a metric read must never be the thing
that fails an open or a fold worker.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Annotation-only: this module must stay importable on the gateway boot path
    # without pulling the session-tree surface, the same rule the bus follows.
    from kiro_crew.crew_log.session_tree import TreeNode


def depth_observation(data: "Mapping[str, Any] | None") -> "int | None":
    """The ``session.tree.depth`` value to record for one ``session/opened``, or ``None``.

    *data* is one ``session/opened`` entry's ``data`` mapping. The answer is its
    ``depth`` when the entry carries one as a valid depth -- a non-negative int that
    is not a bool -- and ``None`` in every other case, which the caller records as
    NO observation rather than as a zero.

    PRESENCE-AWARE on purpose, which is why this does not reuse
    :func:`~kiro_crew.crew_log.projection_values._as_int`: that helper folds a
    missing key and a present ``0`` to the same ``0``, and here those are the two
    cases that must stay apart. A session nobody created states ``depth == 0`` and
    is a real observation at the low end; an entry written before the field existed
    carries no ``depth`` and must contribute nothing. The same guard the writer
    applies before storing the field (:func:`~kiro_crew.crew_log.emit.on_session_opened`)
    is applied again on read, so a value that could never have been written by the
    current code -- a bool, a negative, a string -- is skipped rather than charted.
    """
    if not isinstance(data, Mapping):
        return None
    if "depth" not in data:
        return None
    value = data["depth"]
    if isinstance(value, bool):
        return None
    if not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def count_roots_with_children(nodes: "Mapping[str, TreeNode] | None") -> int:
    """How many roots of the folded tree have at least one child. Pure; never raises.

    *nodes* is the tree as :meth:`~kiro_crew.crew_log.session_tree.SessionTree`'s
    reader holds it: slot -> :class:`~kiro_crew.crew_log.session_tree.TreeNode`,
    where a node's ``parent_slot`` is the creator its records cite (``None`` for a
    root) and ``cycle`` marks a slot the fold could not place.

    A ROOT here is a node the tree places under nobody: ``parent_slot is None`` and
    not on a cycle. It HAS A CHILD when some other node in the same reading cites it
    as ``parent_slot``. The answer is the number of roots for which that holds -- the
    trees that branched -- which is exactly the ``depth == 0`` sessions with children
    the metric names, read off the one in-memory tree rather than tallied from the
    entry stream.

    A node whose ``parent_slot`` names a slot that is NOT itself a root still proves
    that parent has a child, so the parent is counted when it is a root -- a child
    hanging off a non-root interior node does not make a NEW root, it just confirms
    one further up branched, and that further-up root is counted through ITS own
    child. A ``parent_slot`` pointing at a slot with no node of its own (a citation
    the fold could not place) contributes no root, because the thing it points at is
    not in ``nodes`` and so is not a root this reading can see.

    Cycles are excluded from BOTH sides: a slot on a cycle is not a root, and a
    citation INTO a cycle is not a child of a root (the chain's top is unreadable,
    so no root owns it). This matches
    :func:`~kiro_crew.crew_log.session_tree_projection.slot_depth`, which refuses the
    same shapes rather than inventing a tree for them.
    """
    if not isinstance(nodes, Mapping) or not nodes:
        return 0
    # Slots that are cited as a parent BY a placeable, non-cycle node. A node on a
    # cycle citing its parent is not a child that makes a root, so it does not seed
    # this set. Built once, read once per root below: a pass over the population, no
    # per-root re-scan.
    cited_parents: set[str] = set()
    for node in nodes.values():
        if getattr(node, "cycle", False):
            continue
        parent_slot = getattr(node, "parent_slot", None)
        if parent_slot:
            cited_parents.add(parent_slot)
    count = 0
    for slot, node in nodes.items():
        if getattr(node, "cycle", False):
            continue
        if getattr(node, "parent_slot", None) is not None:
            continue
        # A root. Counted only if something placed cites it as a parent.
        if slot in cited_parents:
            count += 1
    return count
