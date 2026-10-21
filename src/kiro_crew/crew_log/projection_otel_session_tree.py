"""A session's own tree facts: the ``depth`` its ``session/opened`` recorded, and how
many child sessions it has created -- the SESSION-keyed fold the OTel export turns into
the depth and child counters.

WHY A SEPARATE FOLD, NOT A READ OF ``otel_usage`` OR ``status``. ``otel_usage``
(:mod:`~kiro_crew.crew_log.projection_otel_usage`) renders ``opened_agent_kind`` and
per-actor turn counts and nothing about the tree; ``status`` renders ``owner``,
``slot`` and ``cwd``, which no attribute may carry. Reading either and "picking out"
what this export needs is a boundary a later edit to that fold could silently break.
This fold reads ``session/opened`` and ``session/spawned`` directly and keeps nothing
but a few numbers and one closed enum value, so its own render is already the shape
the export may emit -- the same posture ``otel_usage`` argues for itself.

WHAT IT RENDERS. ``depth`` for a session whose ``session/opened`` carried a valid
depth, and ``None`` for one that did not (an entry from before the field existed, or
one whose writer could not establish a depth). ABSENT, never a zero: the export's diff
turns a present depth into exactly one counter add and a missing one into none, which
is how a distribution over the sessions that DO carry a depth stays honest (see
:func:`_otel_session_tree_diff` in :mod:`kiro_crew.metrics.crew_log_export` and the
``depth`` field note in ``entry_types_lifecycle.py``).

``children`` is how many ``session/spawned`` entries this session's own log holds:
one per child session it created, written on the CREATOR's log at creation time
(``entry_types_lifecycle.py``). Counting them here is what gives each child its
ordinal among its creator's children without walking any parent link and without
holding any child's identity: the n-th ``session/spawned`` this fold steps over IS the
n-th child, so the export reads the move from one ``children`` total to the next and
adds one per new ordinal. ``agent_kind`` is this session's own closed kind -- the same
mapping ``otel_usage`` applies, done here in ``step`` from the same two raw fields and
stored only as the enum -- because a child counter is attributed by its CREATOR's
kind, and the creator is the session whose log this is.

ONE DEPTH ADD PER SESSION, NOT PER DELIVERY. ``session/opened`` fires once per unit;
a re-attach to an existing log (``resumed=True``) carries it again with the same depth
and is skipped here the way ``otel_usage`` skips a second open, so this fold's
``depth`` moves from ``None`` to ``N`` exactly once. The export's diff emits the depth
add only on the first sight of a session it can prove is new, so a cold-boot baseline
re-delivering the same render records nothing a second time.

THE STATE IS NUMBERS AND ONE ENUM VALUE. ``step`` sets ``depth``, ``opened_ts`` and
``agent_kind`` once and adds one to ``children`` per spawn; there is no list, id, name
or path here, which is the counting-fold rule every OTel counting fold in this package
keeps.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from kiro_crew.crew_log.otel_session_tree_fold import depth_observation
from kiro_crew.crew_log.projection_otel_usage import SESSION_OPENED_TYPE, _agent_kind
from kiro_crew.crew_log.schema import Entry

#: This fold's registry name. Spelled out here the way ``OTEL_USAGE_FOLD_NAME`` is,
#: so the registry and the export module import one constant rather than a literal
#: each could drift from.
OTEL_SESSION_TREE_FOLD_NAME: Final[str] = "otel_session_tree"

#: ``session/spawned`` is written on the CREATOR's log once per child it creates, so
#: this fold steps over it on the creator's own unit.
SESSION_SPAWNED_TYPE: Final[str] = "session/spawned"

#: Entry types this fold's ``step`` can move, named the way the registry's own
#: docstring requires of an eager fold that narrows its set.
OTEL_SESSION_TREE_TYPES: Final[frozenset[str]] = frozenset(
    {SESSION_OPENED_TYPE, SESSION_SPAWNED_TYPE}
)

#: The ``agent_kind`` a session carries before its opening entry is folded.
_TREE_AGENT_KIND_UNSET: Final[str] = ""


def _otel_session_tree_start() -> dict[str, Any]:
    """One session's tree facts.

    ``seen`` guards against a re-attach's second ``session/opened`` overwriting the
    first unit's depth and kind, mirroring ``otel_usage``'s ``opened`` flag. ``depth``
    is ``None`` until an opened entry carries a valid one, which is exactly the
    "absent, not zero" the render and the export both depend on. ``opened_ts`` is the
    opening entry's own timestamp (off the log, so a replay folds the same value, and a
    number not an id); the export compares it to install time so a session counts only
    when this process can prove it is new -- the same restart guard ``otel_usage``
    uses. ``children`` counts this session's ``session/spawned`` entries, and
    ``agent_kind`` is the closed enum the export attributes those children by.
    """
    return {
        "seen": False,
        "depth": None,
        "opened_ts": 0,
        "agent_kind": _TREE_AGENT_KIND_UNSET,
        "children": 0,
    }


def _otel_session_tree_step(state: dict[str, Any], entry: Entry) -> None:
    if entry.type == SESSION_SPAWNED_TYPE:
        # One child per entry: the creator writes exactly one ``session/spawned`` per
        # child it mints. The child's slot is not read, because the ordinal is the
        # count itself and this fold keeps no identity. A line whose ``child`` is not
        # an object is a damaged or planted entry and is skipped, not counted.
        child = entry.data.get("child")
        if not isinstance(child, Mapping):
            return
        state["children"] = _tree_count(state.get("children")) + 1
        return
    if entry.type != SESSION_OPENED_TYPE:
        return
    if state["seen"]:
        # A re-attach (``resumed=True``) carries ``session/opened`` again with the
        # SAME depth the unit was opened with. The unit's depth is recorded once, not
        # once per claim that reattaches to it.
        return
    state["seen"] = True
    # ``depth_observation`` is presence- and shape-aware: it answers ``None`` for a
    # missing key, a bool, a negative, or a non-int -- the same guard the writer
    # applied before storing the field -- so a damaged or pre-field entry leaves
    # ``depth`` at ``None`` and contributes no observation.
    state["depth"] = depth_observation(entry.data)
    time = getattr(entry, "time", 0)
    state["opened_ts"] = time if isinstance(time, int) and not isinstance(time, bool) else 0
    # Fold the two raw strings to the closed ``agent_kind`` set right here, with the
    # same classifier ``otel_usage`` uses, and store only the enum: both are locals
    # that go out of scope with this call, so neither reaches state, render or
    # savepoint.
    agent = entry.data.get("agent")
    slot = entry.data.get("slot")
    state["agent_kind"] = _agent_kind(
        agent if isinstance(agent, str) else "",
        slot if isinstance(slot, str) else "",
    )


def _tree_count(value: Any) -> int:
    """*value* as a non-negative count, ``0`` for anything that is not one."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return 0


def _otel_session_tree_render(state: dict[str, Any]) -> dict[str, Any]:
    """This session's tree facts as the fixed shape the export reads.

    Every key is ALWAYS present: a fold's render is a fixed shape the fold catalogue
    probes for its field types (``dashboard_types._probe``), and a key that appears
    only sometimes is a field the catalogue cannot see. ``depth`` is ``None`` for "no
    depth established", the catalogue's own "nullable" shape (declared in
    ``dashboard_types._NULLABLE`` for ``otel_session_tree.depth``), and the export's
    diff reads ``None`` as "no depth add" -- the one sentinel, declared in both places
    that must agree on it.
    """
    depth = state.get("depth")
    ts = state.get("opened_ts")
    opened_ts = ts if isinstance(ts, int) and not isinstance(ts, bool) else 0
    kind = state.get("agent_kind")
    rendered: dict[str, Any] = {
        "depth": None,
        "opened_ts": opened_ts,
        "agent_kind": kind if isinstance(kind, str) else _TREE_AGENT_KIND_UNSET,
        "children": _tree_count(state.get("children")),
    }
    if isinstance(depth, int) and not isinstance(depth, bool) and depth >= 0:
        rendered["depth"] = depth
    return rendered


def _otel_session_tree_copy(state: dict[str, Any]) -> dict[str, Any]:
    """*state* copied -- scalars only, so a shallow copy is the whole of it.

    Spelled out rather than falling back to the deep copy, the way ``otel_usage``'s
    is: a copier named here is one a reviewer can check against the step instead of
    trusting the fallback to cover a state that later grew.
    """
    return {
        "seen": state.get("seen", False),
        "depth": state.get("depth"),
        "opened_ts": state.get("opened_ts", 0),
        "agent_kind": state.get("agent_kind", _TREE_AGENT_KIND_UNSET),
        "children": state.get("children", 0),
    }
