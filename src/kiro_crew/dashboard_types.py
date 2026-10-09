"""The data-type catalog: what a dynamic dashboard can bind a block to.

Part 2 of the dynamic dashboard has no templates. An agent composes a page by
declaring a MODEL -- a set of fields, each naming a fold and a dotted path into that
fold's rendered value -- and this module is what tells it which folds exist and which
paths inside them are real. Without it the agent guesses a path, the write is refused,
and it guesses again; the catalog turns that loop into one read.

**Derived from the registry, never from a list.** The folds come from
:data:`~kiro_crew.crew_log.projection.FOLD_NAMES`, and every fold's SHAPE is obtained by
running that fold's own ``start`` and ``render``
(:data:`~kiro_crew.crew_log.projection._FOLDS`). A fold added to that registry therefore
appears here with a shape on the next import, with nothing in this file edited --
which is the only arrangement under which "the catalog lists every type" stays true. A
hand-written table would say what was true when somebody last looked.

**The shape is the dotted-key tree a fold path can ADDRESS, and no more.** A manifest
field's ``path`` is dotted keys only
(:data:`~kiro_crew.dashboard_templates.manifest._PATH`), so nothing inside a list is
reachable: an array is a LEAF of type ``array`` here, and its element shape is
deliberately absent rather than guessed. The vocabulary is the manifest's own
(:data:`~kiro_crew.dashboard_templates.manifest.FIELD_TYPES` plus ``properties``), so a
Model field's declared ``type`` can be compared against a catalog leaf without a
translation step in between.

**Three states per node, because a probe of an EMPTY fold cannot see everything.**

``properties``
    The keys this object has. Known, enumerated, validatable.
``opaque``
    This object's keys are not the catalog's to list: a map keyed by an id the
    template cannot know (a model name, a subagent id, an agentic field name), or a
    document the agent itself supplied. A path below it is neither right nor wrong
    here, and a validator must say so rather than refuse it.
``type: "unknown"``
    The probe saw ``null`` and nothing declares what a non-null value would be. A
    validator can neither accept nor refuse a path ending here. EMPTY TODAY -- every
    nullable leaf every registered fold renders is declared in
    :data:`_NULLABLE`, and ``test_dashboard_types`` fails if a new one appears
    undeclared. It exists so that a fold added later degrades to "the catalog cannot
    see this" instead of to a type somebody guessed.

That third state is the one this module exists to keep honest. A leaf the probe could
not type is NOT the same as a path that does not exist, and collapsing the two would
make a legitimate Model field look like a typo.

**Read only.** Nothing here writes, appends or caches. :func:`catalog` is pure and the
live half -- how far each fold has been folded -- is passed IN by the caller that read
it, so this module holds no data home and no slot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Mapping

from kiro_crew.crew_log.entry_types import DASHBOARD_FOLD_NAME
from kiro_crew.crew_log.projection import (
    _FOLDS,
    _SLOT_FOLD_ROW_BYTES,
    FOLD_NAMES,
    OWNER_SERVED_SLOT_PROJECTION,
    SESSION_FOLD_NAMES,
    SLOT_PROJECTION_NAMES,
)
from kiro_crew.dashboard_templates.manifest import FIELD_TYPES

__all__ = [
    "KEYED_BY_SESSION",
    "KEYED_BY_SLOT",
    "UNKNOWN_TYPE",
    "FoldType",
    "catalog",
    "catalog_by_name",
    "describe",
    "shape_at",
]

KEYED_BY_SESSION: Final[str] = "session"
KEYED_BY_SLOT: Final[str] = "slot"

#: The ``type`` of a leaf the probe saw as ``null`` and nothing declares. Deliberately
#: NOT a member of :data:`~kiro_crew.dashboard_templates.manifest.FIELD_TYPES`: a Model
#: field can never declare it, so a validator comparing types cannot accidentally match
#: it, and the mismatch it produces is the honest "this path is not checkable" rather
#: than a wrong verdict either way.
UNKNOWN_TYPE: Final[str] = "unknown"

#: The slot a probe binds a slot-keyed fold to. A ``bind_slot`` fold renders its own key,
#: so the probe has to pass one; it is a placeholder and never reaches a reader, because
#: the probe's rendered VALUES are discarded and only the key tree and the JSON types
#: survive into a shape.
_PROBE_SLOT: Final[str] = "catalog-probe"

#: The nullable leaves of the registered folds, each ``"<fold>.<dotted path>"`` -> the
#: shape a NON-null value has there. Needed because a fold probed on empty state renders
#: ``null`` at these paths, and ``null`` carries no type.
#:
#: Read off each fold's own ``render``, which is why each entry names the line it came
#: from. A stale entry cannot sit here unnoticed: ``test_every_declared_nullable_leaf_is
#: _a_real_path`` fails on a path the probe does not reach, and the companion test fails
#: on a null leaf with no entry -- so the two directions are both pinned and neither a
#: removed field nor an added one passes quietly.
_NULLABLE: Final[dict[str, dict[str, Any]]] = {
    # ``_status_render``: three stamps, three reasons and the previous unit's id, all
    # null until the entry that carries them is folded.
    "status.opened_at": {"type": "string"},
    "status.closed_at": {"type": "string"},
    "status.close_reason": {"type": "string"},
    "status.previous": {"type": "string"},
    "status.last_stop_reason": {"type": "string"},
    "status.last_error": {"type": "string"},
    "status.last_time": {"type": "string"},
    # The open turn, whose keys ``_status_step`` writes in one place, so they are
    # enumerable even though the empty probe renders null.
    "status.turn": {
        "type": "object",
        "properties": {
            "turn": {"type": "number"},
            "attempt": {"type": "number"},
            "actor": {"type": "string"},
            "started_at": {"type": "string"},
            "seq": {"type": "number"},
        },
    },
    # ``_timeline_render``: the first and last seq of the window, null while it is empty.
    "timeline.first_seq": {"type": "number"},
    "timeline.last_seq": {"type": "number"},
    # ``_approvals_render``: the newest decision, keys from ``_approvals_step``.
    "approvals.last": {
        "type": "object",
        "properties": {
            "approval_id": {"type": "string"},
            "decision": {"type": "string"},
            "by": {"type": "string"},
            "cause": {"type": "string"},
            "tool": {"type": "string"},
            "turn": {"type": "number"},
            "time": {"type": "string"},
            "seq": {"type": "number"},
        },
    },
    # ``_outline_render``: the window's first and last turn ORDINAL, both null while it
    # holds no turn. A number and not a string: ``_outline_step`` places a row only for
    # a ``turn`` that is an ``int``, is not a ``bool`` and is ``>= 0``, and the row
    # carries that same value under ``turn`` -- so a non-null leaf here is always one of
    # those integers, never a formatted label.
    "outline.first_turn": {"type": "number"},
    "outline.last_turn": {"type": "number"},
    # ``_work_render``: the item this board hangs under, null for a root conductor.
    "work.conductor.parent_item": {"type": "string"},
}

#: Objects whose KEYS are not this catalog's to enumerate, each ``"<fold>.<path>"`` ->
#: what the key is. Marked rather than left as an empty ``properties``, because the two
#: mean opposite things to a validator: an empty ``properties`` says "no key below here
#: is valid", while ``opaque`` says "the catalog cannot judge a key below here".
#:
#: Every one is a map keyed by a runtime id, or a document an agent supplied. A template
#: CAN bind a path into one -- ``usage.by_model.claude-opus-4`` is a real reading -- so
#: refusing those would refuse a working dashboard, and accepting them silently would
#: let a typo through. Saying which it is leaves the choice with the validator.
#:
#: ``test_every_opaque_node_is_a_real_object`` pins each path, and
#: ``test_no_empty_properties_passes_as_a_known_shape`` fails on an object the probe
#: rendered empty that is declared neither here nor in :data:`_NULLABLE`, so a map added
#: to a fold cannot land as "has no keys".
_OPAQUE: Final[dict[str, str]] = {
    "usage.by_model": "a model id",
    "usage.context.by_source": "a context source name",
    "tools.by_name": "a tool name",
    "approvals.by_decision": "a decision value",
    "subagents.by_id": "a subagent id",
    "ledger.artifacts": "an artifact name",
    "radar.skips": "an item id",
    "radar.phase_lines": "an item id",
    "panel.data": "a key the publishing crew chose",
    "panel.owners": "an owning crew key",
    # Built from the fold's own registry constant rather than spelled out, because a
    # key naming a fold under a name the registry does not carry is not an error here:
    # ``_node`` looks this table up with a plain ``in``, misses, and falls through to
    # the plain-object branch -- so the map renders as ``properties: {}``, which tells a
    # validator that no key below it is valid. That is the opposite of what this entry
    # says, and it is the one failure in this module that is silent.
    f"{DASHBOARD_FOLD_NAME}.fields": "an agentic field name",
}


@dataclass(frozen=True)
class FoldType:
    """One data type a dashboard block can bind to.

    ``name`` is what a Model field's ``source.fold`` names. ``source_fold`` is the fold
    the value is FOLDED from, which for a registered fold is itself -- the two are kept
    apart so a derived type (a generalized sub-path, a projection of one fold under
    another name) can state its origin without a reader having to infer it from the
    name.

    ``folded_through`` is the crew log seq this type's current value reflects
    (:attr:`~kiro_crew.crew_log.projection.Projection.seq`), and ``None`` means it was
    not read: either no caller key was resolved, or the fold is
    :data:`~kiro_crew.crew_log.projection.OWNER_SERVED_SLOT_PROJECTION` and a generic
    route refuses it. ``None`` is therefore "not answered", never "nothing folded yet",
    which is ``0``.
    """

    name: str
    shape: Mapping[str, Any]
    source_fold: str
    keyed_by: str
    #: True for the fold only its OWNER serves. Carried so the catalog can list the
    #: type -- a dashboard that is the owner's own may bind it -- while telling a
    #: generic reader that its value will not come back from the generic route.
    owner_served: bool
    #: What the fold's stored state is versioned at. A reader comparing two gateways'
    #: catalogs uses it to tell "the same fold" from "the same name".
    state_version: int
    #: Bytes the warm cache charges one row of this fold, for a composer sizing a block
    #: against a fold that retains rows. ``None`` for a session-keyed fold, which holds
    #: no warm slot cell and is charged by serialized size instead.
    row_bytes: int | None
    folded_through: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "shape": dict(self.shape),
            "source_fold": self.source_fold,
            "keyed_by": self.keyed_by,
            "owner_served": self.owner_served,
            "state_version": self.state_version,
            "row_bytes": self.row_bytes,
            "folded_through": self.folded_through,
        }


def _json_type(value: Any) -> str:
    """*value*'s type in the manifest's vocabulary, or :data:`UNKNOWN_TYPE`.

    ``bool`` is tested BEFORE ``int``, because ``True`` is an ``int`` in Python and a
    boolean reported as a number would let a Model declare ``number`` for a flag and
    draw a 1 where the page wanted a yes.
    """
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    return UNKNOWN_TYPE


def _node(fold: str, path: str, value: Any) -> dict[str, Any]:
    """The shape of one rendered *value*, at ``<fold>.<path>``.

    Recurses only through objects, because only an object's keys are addressable by a
    dotted path. A declaration in :data:`_NULLABLE` wins over a probed ``null`` and is
    returned whole, so a declared object brings its own ``properties`` with it.
    """
    key = f"{fold}.{path}" if path else fold
    if value is None:
        declared = _NULLABLE.get(key)
        if declared is not None:
            return {**declared, "nullable": True}
        # The third state, named rather than guessed: the probe saw null, nothing
        # declares the type, so neither accepting nor refusing a path here is honest.
        return {"type": UNKNOWN_TYPE, "nullable": True, "why": "the fold renders null when empty"}
    jtype = _json_type(value)
    if jtype != "object":
        # An array is a LEAF: a fold path cannot index one, so an element shape here
        # would describe something no Model field can name.
        return {"type": jtype}
    if key in _OPAQUE:
        opaque: dict[str, Any] = {"type": "object", "opaque": True}
        keyed_by = _OPAQUE[key]
        if keyed_by:
            opaque["keyed_by"] = keyed_by
        return opaque
    properties = {
        str(sub): _node(fold, f"{path}.{sub}" if path else str(sub), inner)
        for sub, inner in value.items()
    }
    return {"type": "object", "properties": properties}


def _probe(name: str) -> dict[str, Any]:
    """*name*'s shape, from that fold's own ``start`` and ``render``.

    The fold is bound to :data:`_PROBE_SLOT` first when it takes a slot, because a
    ``bind_slot`` fold renders its key and would otherwise render a state it was never
    told the board of.
    """
    fold = _FOLDS[name]
    state = fold.start()
    if fold.bind_slot is not None:
        fold.bind_slot(state, _PROBE_SLOT)
    return _node(name, "", fold.render(state))


def catalog(folded_through: Mapping[str, int | None] | None = None) -> tuple[FoldType, ...]:
    """Every data type a dashboard can bind to, in registry order.

    *folded_through* maps a fold name to the seq its current value reflects, read by the
    caller that holds a key to read it with. A name it omits comes back as ``None``,
    which is "not answered" -- a catalog read with no key at all is still a complete
    catalog of SHAPES, and that is the half a composing agent needs first.
    """
    reached = folded_through or {}
    return tuple(
        FoldType(
            name=name,
            shape=_probe(name),
            # Itself, for a registered fold. The field is not redundant: it is what a
            # derived type sets to the fold it is derived FROM, and a reader walks it
            # to find the subscription a block actually needs.
            source_fold=name,
            keyed_by=KEYED_BY_SLOT if name in SLOT_PROJECTION_NAMES else KEYED_BY_SESSION,
            owner_served=name == OWNER_SERVED_SLOT_PROJECTION,
            state_version=_FOLDS[name].state_version,
            row_bytes=_SLOT_FOLD_ROW_BYTES.get(name),
            folded_through=reached.get(name),
        )
        for name in FOLD_NAMES
    )


def catalog_by_name(
    folded_through: Mapping[str, int | None] | None = None,
) -> dict[str, FoldType]:
    """:func:`catalog` keyed by type name, for a caller resolving one."""
    return {entry.name: entry for entry in catalog(folded_through)}


def shape_at(shape: Mapping[str, Any], path: str) -> dict[str, Any] | None:
    """The shape at dotted *path* inside *shape*, or ``None`` when it is not there.

    Returns the OPAQUE node itself when the walk reaches one, rather than descending
    into keys the catalog cannot enumerate or reporting the path absent. A caller
    deciding whether a Model field is valid therefore gets three answers from one call,
    and has to handle all three: a shape with a ``type`` it can compare, an opaque node
    it cannot judge below, and ``None`` for a key the fold genuinely does not render.

    An empty *path* is the whole shape, which is what a field binding a fold's entire
    value asks for.
    """
    node: Mapping[str, Any] = shape
    if not path:
        return dict(node)
    for key in path.split("."):
        if node.get("opaque") is True:
            return dict(node)
        properties = node.get("properties")
        if not isinstance(properties, dict) or key not in properties:
            return None
        node = properties[key]
    return dict(node)


def describe(folded_through: Mapping[str, int | None] | None = None) -> dict[str, Any]:
    """The catalog as a JSON payload, for the read route.

    ``unknown_type`` and ``field_types`` ride along so a reader does not have to import
    this module to know which ``type`` values it may see: ``field_types`` are the ones a
    Model field may declare, and ``unknown_type`` is the one it may not.
    """
    return {
        "types": [entry.to_dict() for entry in catalog(folded_through)],
        "field_types": sorted(FIELD_TYPES),
        "unknown_type": UNKNOWN_TYPE,
        "owner_served": OWNER_SERVED_SLOT_PROJECTION,
        "session_keyed": list(SESSION_FOLD_NAMES),
        "slot_keyed": list(SLOT_PROJECTION_NAMES),
    }
