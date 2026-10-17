"""A session's own tree-depth fact: the ``depth`` its ``session/opened`` recorded --
the SESSION-keyed fold the OTel export turns into the depth histogram.

WHY A SEPARATE FOLD, NOT A READ OF ``otel_usage`` OR ``status``. ``otel_usage``
(:mod:`~kiro_crew.crew_log.projection_otel_usage`) renders ``opened_agent`` and
per-actor turn counts and nothing about depth; ``status`` renders ``owner``,
``slot`` and ``cwd``, which no attribute may carry. Reading either and "picking out"
depth is a boundary a later edit to that fold could silently break. This fold reads
``session/opened`` directly and keeps nothing but one number, so its own render is
already the shape the export may emit -- the same posture ``otel_usage`` argues for
itself.

WHAT IT RENDERS. ``{"depth": <int>}`` for a session whose ``session/opened`` carried
a valid depth, and ``{}`` for one that did not (an entry from before the field
existed, or one whose writer could not establish a depth). ABSENT, never a zero:
the export's diff turns a present depth into exactly one histogram observation and a
missing one into no observation, which is how a distribution over the sessions that
DO carry a depth stays honest (see :func:`_otel_session_tree_diff` in
:mod:`kiro_crew.metrics.crew_log_export` and the ``depth`` field note in
``entry_types_lifecycle.py``).

ONE OBSERVATION PER SESSION, NOT PER DELIVERY. ``session/opened`` fires once per
unit; a re-attach to an existing log (``resumed=True``) carries it again with the
same depth and is skipped here the way ``otel_usage`` skips a second open, so this
fold's render moves from ``{}`` to ``{"depth": N}`` exactly once. The export's diff
emits the histogram observation only on that transition, so a cold-boot baseline
re-delivering the same render records nothing a second time.

THE STATE IS ONE NUMBER AND ONE FLAG. ``step`` sets them once and never grows them;
there is no list, id, name or path here, which is the counting-fold rule every OTel
counting fold in this package keeps.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from kiro_crew.crew_log.otel_session_tree_fold import depth_observation
from kiro_crew.crew_log.schema import Entry

#: This fold's registry name. Spelled out here the way ``OTEL_USAGE_FOLD_NAME`` is,
#: so the registry and the export module import one constant rather than a literal
#: each could drift from.
OTEL_SESSION_TREE_FOLD_NAME: Final[str] = "otel_session_tree"

#: The one entry type this fold answers to. ``session/opened`` fires once per unit and
#: carries the ``depth`` field; nothing else moves this fold.
SESSION_OPENED_TYPE: Final[str] = "session/opened"

#: Entry types this fold's ``step`` can move. One, named the way the registry's own
#: docstring requires of an eager fold that narrows its set.
OTEL_SESSION_TREE_TYPES: Final[frozenset[str]] = frozenset({SESSION_OPENED_TYPE})


def _otel_session_tree_start() -> dict[str, Any]:
    """One session's tree-depth fact.

    ``seen`` guards against a re-attach's second ``session/opened`` overwriting the
    first unit's depth, mirroring ``otel_usage``'s ``opened`` flag. ``depth`` is
    ``None`` until an opened entry carries a valid one, which is exactly the "absent,
    not zero" the render and the export both depend on. ``opened_ts`` is the opening
    entry's own timestamp (off the log, so a replay folds the same value, and a number
    not an id); the export compares it to install time to observe a session's depth
    only on the first sight of a session new to this process -- the same restart guard
    ``otel_usage`` uses, so a resumed session's depth is not re-observed on every boot.
    """
    return {"seen": False, "depth": None, "opened_ts": 0}


def _otel_session_tree_step(state: dict[str, Any], entry: Entry) -> None:
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


def _otel_session_tree_render(state: dict[str, Any]) -> dict[str, Any]:
    """This session's depth as the fixed shape the export reads: ``{"depth": N}`` when
    one was established, ``{"depth": None}`` when it was not.

    The key is ALWAYS present and is ``None`` for "no depth established", never
    omitted: a fold's render is a fixed shape the fold catalogue probes for its field
    types (``dashboard_types._probe``), and a key that appears only sometimes is a
    field the catalogue cannot see. ``None`` is the catalogue's own "nullable" shape
    (declared in ``dashboard_types._NULLABLE`` for ``otel_session_tree.depth``), and
    the export's diff reads ``current["depth"] is None`` as "no observation" -- the
    one sentinel, declared in both places that must agree on it.
    """
    depth = state.get("depth")
    ts = state.get("opened_ts")
    opened_ts = ts if isinstance(ts, int) and not isinstance(ts, bool) else 0
    if isinstance(depth, int) and not isinstance(depth, bool) and depth >= 0:
        return {"depth": depth, "opened_ts": opened_ts}
    return {"depth": None, "opened_ts": opened_ts}


def _otel_session_tree_copy(state: dict[str, Any]) -> dict[str, Any]:
    """*state* copied -- scalars only, so a shallow copy is the whole of it.

    Spelled out rather than falling back to the deep copy, the way ``otel_usage``'s
    is: this fold is woken only by ``session/opened``, which is rare next to a log's
    message bodies, but a copier named here is one a reviewer can check against the
    step instead of trusting the fallback to cover a state that later grew.
    """
    return {
        "seen": state.get("seen", False),
        "depth": state.get("depth"),
        "opened_ts": state.get("opened_ts", 0),
    }


def _otel_session_tree_rows(state: "Mapping[str, Any]") -> int:
    """A SESSION-keyed fold holds no slot cell, so nothing here charges a slot budget;
    kept for symmetry with the slot folds' row counters and to make a future re-key of
    this fold fail loudly rather than silently charge a constant. The state is two
    scalars, so it retains no rows.
    """
    return 0
