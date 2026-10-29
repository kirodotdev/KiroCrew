"""Per-type ``data`` shapes for the session crew log entry types, checked on append.

:data:`TYPE_OWNERSHIP` answers whether a KIND of unit has such events at all, by
domain prefix. This module answers the next question -- what does one entry of
this type carry -- and it is the single machine-readable source for it. The shape
of a ``data`` payload is otherwise stated twice, in the emitter that builds it and
in a spec table describing it, and two statements of one fact drift.

**What a declaration is derived from.** The WRITER, not the table: every field
declared here is read off the site that produces it (:mod:`kiro_crew.crew_log.emit`
for the ordinary entries, ``store._closer_entries`` for the crash-repair closers).
A type earns a declaration by having a writer, so the types declared here are
exactly the types something writes today, whether or not the writer marks the
entry ignorable. An ignorable write is not exempt: a folding reader SKIPS an
undeclared ignorable entry, a skip is a gap in the sequence the fold receives, and
the class fold reads a gap as damage. So ``plan/updated`` is declared like the
rest, and its write keeps ``ignorable=True`` untouched. A type nothing writes at
all is left undeclared, which is the posture ``message/steered`` already gets.
``test_crew_log_types`` pins the two sets equal in both directions -- every
declared type has a producing site, and every type a writer appends is declared.
A field
is ``required`` only when EVERY writer of that type produces it, which is why a few
fields the spec table marks required are optional here -- the repair closer knows
the turn and the reason and nothing else, and a required field it cannot supply
would refuse the one write that closes an interrupted turn.

**A missing declaration is not a passive gap.** A reader that FOLDS state passes
``known=`` to :meth:`~kiro_crew.crew_log.store.CrewLog.iter_from`, which refuses an
entry whose type it does not know and which is not marked ignorable -- so a type
written without a declaration does not merely go uninterpreted, it stops every
later fold of that log permanently. Declaring a type is therefore what makes a log
containing it readable at all, and it is independent of whether any fold branches
on it: a declared type a fold ignores is a fact it chose not to use, while an
undeclared one is a fact it does not know exists.

**Undeclared keys are refused**, the same posture and for the same reason as
:func:`~kiro_crew.crew_log.schema.build_header`: a caller that misspells a field
would otherwise be told the entry landed as asked while the value it meant to
record silently vanished. So a new field arrives with its declaration, in one
commit, or not at all.

**Values come in two strengths, and only one of them refuses.** ``enum_closed``
marks a vocabulary the WRITER itself clamps -- ``turn/started.actor`` and
``turn/refused.actor`` are coerced to
:data:`~kiro_crew.crew_log.emit.ACTORS` at the emitter -- so no caller can
produce a value outside it and enforcing costs nothing. Every other vocabulary is
PASSED THROUGH from
somewhere this module does not own: a provider's ``stop_reason``, the gateway's
own ``end_reason``, a provider's tool ``status``, a subagent runtime's outcome.
Those are recorded as ``enum`` for the reference tables and are NOT enforced,
because enforcing them converts "the upstream vocabulary grew" into "the entry is
refused and counted as a write loss" -- the registry would then destroy records
instead of catching mistakes.

Types with no declaration pass through untouched. Two kinds are declared here --
the session families and the crew kind's two dispatch contracts -- and everything
else is left open on purpose: the crew kind's other six domains, the member
kind's whole vocabulary (owned by the member event log), and every guest
namespace (``app:<name>/…``) are writable without a registry entry.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from typing import Any

from kiro_crew.crew_log import entry_types_delegated as _delegated
from kiro_crew.crew_log import entry_types_exchange as _exchange
from kiro_crew.crew_log import entry_types_ledgers as _ledgers
from kiro_crew.crew_log import entry_types_lifecycle as _lifecycle
from kiro_crew.crew_log import entry_types_surfaces as _surfaces
from kiro_crew.crew_log.entry_types_fields import (  # noqa: F401
    JSON_ARRAY,
    JSON_BOOL,
    JSON_FLOAT,
    JSON_INT,
    JSON_OBJECT,
    JSON_STRING,
    JSON_TYPES,
    EntryType,
    Field,
)
from kiro_crew.crew_log.entry_types_ledgers import (  # noqa: F401
    OBJECT_PRODUCER_PROBE,
    OBJECT_PRODUCERS,
    RADAR_CI_BOUNDS,
    RADAR_CI_KEYS,
    RADAR_CLEARABLE_FIELDS,
    RADAR_CREW_LEVEL_EVENT_KIND,
    RADAR_DEFAULT_SKIP_SCOPE,
    RADAR_EDITING_PHASES,
    RADAR_ENTRY_TYPE,
    RADAR_EVENT_KINDS,
    RADAR_LABELS_LIMIT,
    RADAR_NUMBER_BOUNDS,
    RADAR_PHASES,
    RADAR_SKIP_SCOPES,
    RADAR_TERMINAL_PHASES,
    RADAR_TTL_ACTIVE_PHASES,
    WORK_CHARGE_ENTRY_TYPE,
    WORK_ENTRY_TYPE,
)
from kiro_crew.crew_log.entry_types_lifecycle import ACTOR_VALUES  # noqa: F401
from kiro_crew.crew_log.entry_types_surfaces import (  # noqa: F401
    CARD_FINISHED_STATUSES,
    CREWMATE_OPTION_LIMIT,
    CREWMATE_ROW_LIMIT,
    CREWMATE_TEXT_LIMIT,
    CREWMATE_WAITING_LIMIT,
    CUSTOM_ENTRY_TYPE,
    CUSTOM_FOLD_NAME,
    CUSTOM_RECORD_BYTES,
    CUSTOM_RECORD_DEPTH,
    CUSTOM_TYPE_LIMIT,
    CUSTOM_TYPE_NAME_LIMIT,
    CUSTOM_WRITER_LIMIT,
    DASHBOARD_AGENTIC_ENTRY_TYPE,
    DASHBOARD_CODE_LIMIT,
    DASHBOARD_FIELD_LIMIT,
    DASHBOARD_FOLD_NAME,
    DASHBOARD_INSTANCE_ACTIONS,
    DASHBOARD_INSTANCE_ENTRY_TYPE,
    DASHBOARD_REASON_LIMIT,
    DASHBOARD_REFUSED_ENTRY_TYPE,
    DASHBOARD_TYPE_LIMIT,
    DASHBOARD_VALUE_BYTES,
    DASHBOARD_VALUE_LIMIT,
    GOAL_ENTRY_TYPE,
    GOAL_FOLD_NAME,
    GUIDE_FINISHED_REASONS,
    GUIDE_FINISHED_STATUSES,
    MISTAKE_CORRECTED_CODE,
    MISTAKE_CORRECTION_LIMIT,
    MISTAKE_GROUP_LIMIT,
    MISTAKES_FOLD_NAME,
    PANEL_CREW_KEY_LIMIT,
    PANEL_ENTRY_TYPE,
    PANEL_FOLD_NAME,
    PANEL_HISTORY_LIMIT,
    PANEL_OWNER_LIMIT,
    PANEL_TEMPLATE_LIMIT,
    PANEL_TITLE_LIMIT,
    WAITING_ENTRY_TYPE,
    WAITING_FOLD_NAME,
)
from kiro_crew.crew_log.errors import CODE_BAD_DATA_FIELD, CrewLogError
from kiro_crew.crew_log.schema import KIND_CREW, KIND_SESSION
from kiro_crew.work_vocab import (  # noqa: F401
    WORK_ACTIONS,
    WORK_ACTORS,
    WORK_BLOCKED_REASONS,
    WORK_EVENT_KINDS,
    WORK_ITEM_STATES,
    WORK_VERDICTS,
    WORK_WORKER_STATUSES,
)

# The session families are declared in five owner modules, one per group of type
# prefixes, and composed here in the order the reference tables render them. An owner
# imports ``entry_types_fields`` for ``Field``, ``EntryType`` and the JSON types and
# never imports this module; this module re-exports every public name an owner defines
# other than its ``TYPES``, so callers keep importing from ``entry_types``.
#
#   entry_types_lifecycle -- session/, turn/, write/
#   entry_types_exchange  -- message/, request/, context/, step/, tool/, approval/,
#                            model/, compaction/, plan/
#   entry_types_delegated -- subagent/, background/
#   entry_types_ledgers   -- ledger/, object/, radar/, work/
#   entry_types_surfaces  -- panel/, dashboard/, card/, guide/
#
# A new session type goes into the owner of its prefix. A new prefix goes into the owner
# whose docstring describes it, or into a new owner listed in ``_SESSION_OWNERS`` at the
# position its types render. A crew type goes into ``_CREW_TYPES`` below.
_SESSION_OWNERS = (_lifecycle, _exchange, _delegated, _ledgers, _surfaces)
_SESSION_TYPES: tuple[EntryType, ...] = tuple(
    spec for owner in _SESSION_OWNERS for spec in owner.TYPES
)

#: The session types that have a writer. Keyed by ``type`` for the append path.
SESSION_ENTRY_TYPES: dict[str, EntryType] = {item.type: item for item in _SESSION_TYPES}

#: Which sort of party a dispatch went to. Closed: the writer builds the object,
#: so no caller can produce a third kind.
CREW_TARGET_KINDS: tuple[str, ...] = ("session", "crew")

#: The statuses the crew kind's own spec names for a report.
_SPEC_REPORT_STATUSES: tuple[str, ...] = ("done", "blocked", "failed", "progress")

#: What a ``crew/report`` may say about an item: the spec's four, plus every
#: status the work ledger's worker half can commit. Derived from that writer's
#: own vocabulary rather than restated, because it is the one producer of this
#: type: a closed enum narrower than its writer turns "the ledger gained a
#: status" into a refused entry counted as a write loss, which is the posture
#: this module's docstring rejects. ``question`` reaches the log under its own
#: name rather than folded into ``blocked``: the two differ by WHICH party must
#: act, and a conductor reading the fold acts on that difference.
CREW_REPORT_STATUSES: tuple[str, ...] = _SPEC_REPORT_STATUSES + tuple(
    value for value in WORK_WORKER_STATUSES if value not in _SPEC_REPORT_STATUSES
)

#: The crew kind's declared types: the dispatch contract and the report contract.
#: Only these two, because only these two have a writer -- the same rule the
#: session table follows. The other six crew domains
#: (``member``, ``activity``, ``slot``, ``patrol``, ``message``, ``memory``) and
#: the remaining ``crew``/``item`` actions stay undeclared and pass through, so a
#: guest app and a future family are writable without a registry change.
_CREW_TYPES: tuple[EntryType, ...] = (
    EntryType(
        type="crew/dispatch",
        summary="A crew handed one work item to a target.",
        fields=(
            Field("item", JSON_STRING, required=True, note="The work item's id."),
            Field(
                "target",
                JSON_OBJECT,
                required=True,
                fields=(
                    Field(
                        "kind",
                        JSON_STRING,
                        required=True,
                        enum=CREW_TARGET_KINDS,
                        enum_closed=True,
                        note="Which sort of target this item went to.",
                    ),
                    Field(
                        "slot",
                        JSON_STRING,
                        note="The slot key, carried when the target is a session.",
                    ),
                    Field(
                        "name",
                        JSON_STRING,
                        note="The crew name, carried when the target is a crew.",
                    ),
                ),
                note="Who the item went to. A dispatch with no target names nobody.",
            ),
            Field("brief", JSON_STRING, note="The brief handed over."),
        ),
        note=(
            "The opener of the dispatch family: one or more reports thread onto its "
            "seq. Two invariants the declaration cannot state are the writer's and "
            "are enforced where the entry is built -- ``target.kind`` decides which "
            "of ``slot`` or ``name`` is carried, and the two forms are exclusive, so "
            "a target names a session slot or a crew and never both. A conditional "
            "requirement has no spelling here, and a field marked required that one "
            "legitimate form omits would refuse a valid dispatch."
        ),
    ),
    EntryType(
        type="crew/report",
        summary="A dispatched party reported back on one work item.",
        fields=(
            Field(
                "item",
                JSON_STRING,
                required=True,
                note="The work item's id, matching the dispatch.",
            ),
            Field(
                "status",
                JSON_STRING,
                required=True,
                enum=CREW_REPORT_STATUSES,
                enum_closed=True,
                note="Where the item stands.",
            ),
            Field("credits", JSON_FLOAT, note="What the work cost. Absent is not zero."),
            Field("summary", JSON_STRING, note="What was done."),
        ),
        note=(
            "The one type that constrains the ENVELOPE as well as ``data``: ``ref`` "
            "is required, citing the span of the reporting session's log that holds "
            "the work, and ``thread`` is the answered dispatch's seq. Neither is a "
            "``data`` key, so neither is declarable here. ``ref`` is always carried: "
            "a report built without one is refused where it is built. ``thread`` is "
            "carried when the dispatch anchor resolves and absent on an UNTHREADED "
            "report. See docs/reference/crew-log/crew-types.md. A "
            "``progress`` status may appear several times for one dispatch; a "
            "terminal status appears once."
        ),
    ),
)

#: The crew types that have a writer. Keyed by ``type`` for the append path.
CREW_ENTRY_TYPES: dict[str, EntryType] = {item.type: item for item in _CREW_TYPES}

#: Per kind, because the question "what does this type carry" is asked of a unit.
#: The member kind declares nothing here: its vocabulary, writers and projections
#: are owned by the member event log, and an undeclared type passes through.
ENTRY_TYPES: dict[str, dict[str, EntryType]] = {
    KIND_SESSION: SESSION_ENTRY_TYPES,
    KIND_CREW: CREW_ENTRY_TYPES,
}


def declaration_for(kind: str, entry_type: str) -> EntryType | None:
    """The declaration for (*kind*, *entry_type*), or ``None`` when undeclared."""
    return ENTRY_TYPES.get(kind, {}).get(entry_type)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def _refuse(path: str, message: str) -> CrewLogError:
    return CrewLogError(f"{path}: {message}", code=CODE_BAD_DATA_FIELD, field=path)


def _type_ok(value: Any, json_type: str) -> bool:
    if json_type == JSON_BOOL:
        return isinstance(value, bool)
    # A JSON ``true`` is a Python bool, which is an int. Every numeric field here
    # counts or measures something, so admitting a boolean would let a flag land
    # where a count belongs and read back as 1.
    if json_type == JSON_INT:
        return isinstance(value, int) and not isinstance(value, bool)
    if json_type == JSON_FLOAT:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if json_type == JSON_STRING:
        return isinstance(value, str)
    if json_type == JSON_OBJECT:
        return isinstance(value, Mapping)
    # A str is a Sequence, and so is bytes. An array field means a JSON array.
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _check_value(value: Any, spec: Field, path: str) -> None:
    if not _type_ok(value, spec.json_type):
        raise _refuse(path, f"expected {spec.json_type}, got {type(value).__name__}")
    if spec.enum_closed and value not in spec.enum:
        raise _refuse(path, f"{value!r} is not one of {list(spec.enum)}")
    if spec.json_type == JSON_OBJECT and spec.fields:
        _check_members(value, spec.fields, path)
        return
    if spec.json_type != JSON_ARRAY:
        return
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if not _type_ok(item, spec.item_type):
            raise _refuse(item_path, f"expected {spec.item_type}, got {type(item).__name__}")
        if spec.item_type == JSON_OBJECT and spec.fields:
            _check_members(item, spec.fields, item_path)


def _check_members(data: Any, fields: "tuple[Field, ...]", path: str) -> None:
    declared = {item.name: item for item in fields}
    for name in data:
        if name not in declared:
            raise _refuse(
                f"{path}.{name}",
                f"is not a declared field; declared: {sorted(declared)}",
            )
    for spec in fields:
        member_path = f"{path}.{spec.name}"
        if spec.name not in data:
            if spec.required:
                raise _refuse(member_path, "is required and absent")
            continue
        _check_value(data[spec.name], spec, member_path)


def validate_data(kind: str, entry_type: str, data: Any) -> None:
    """Check *data* against the declaration for (*kind*, *entry_type*).

    Raises ``bad_data_field`` naming the offending path when a required field is
    absent, a value is of the wrong JSON type, a key is not declared, or a value
    falls outside a CLOSED enum. Returns silently for a type with no declaration
    -- the member kind, a crew domain outside the two dispatch contracts, and
    every guest namespace.

    The declaration is selected by KIND as well as by type, so it cannot answer
    for the wrong unit: a ``crew/report`` reaching a session's log finds no
    session declaration and is refused one layer up, by ``check_ownership``, with
    ``event_type_not_owned``.

    A refusal is a :class:`~kiro_crew.crew_log.errors.CrewLogError`, so the
    write-behind emitter already treats it the way it treats an oversize entry: a
    permanent refusal, reported and counted in ``dropped_writes()``, never raised
    into the gateway and never retried against a verdict that cannot change.
    """
    spec = declaration_for(kind, entry_type)
    if spec is None or not isinstance(data, Mapping):
        # A non-mapping ``data`` is ``require_data``'s refusal to make, with its
        # own code. Two codes for one fact would make a caller branch twice.
        return
    _check_members(data, spec.fields, "data")


# --------------------------------------------------------------------------- #
# Reference tables
# --------------------------------------------------------------------------- #


def _values_cell(spec: Field) -> str:
    if not spec.enum:
        return "--"
    listed = " \\| ".join(f"`{value}`" for value in spec.enum)
    return listed if spec.enum_closed else f"{listed} (open)"


def _rows(fields: "tuple[Field, ...]", prefix: str = "") -> list[str]:
    rows: list[str] = []
    for spec in fields:
        shape = spec.json_type
        if spec.json_type == JSON_ARRAY:
            shape = f"array[{spec.item_type}]"
        rows.append(
            f"| `{prefix}{spec.name}` | {shape} | "
            f"{'required' if spec.required else 'optional'} | "
            f"{_values_cell(spec)} | {spec.note or '--'} |"
        )
        if spec.fields:
            member_prefix = (
                f"{prefix}{spec.name}[]."
                if spec.json_type == JSON_ARRAY
                else f"{prefix}{spec.name}."
            )
            rows.extend(_rows(spec.fields, member_prefix))
    return rows


def render_markdown(kind: str = KIND_SESSION) -> str:
    """The declarations for *kind* as Markdown tables, one section per type.

    So the reference tables in the spec can be GENERATED from the registry the
    append path enforces, instead of being a second description of it that drifts.
    """
    out: list[str] = [f"# Declared `{kind}` crew log entry types", ""]
    for spec in ENTRY_TYPES.get(kind, {}).values():
        out.append(f"## `{spec.type}`")
        out.append("")
        out.append(spec.summary)
        out.append("")
        if spec.ignorable:
            out.append("Always written with `ignorable: true`.")
            out.append("")
        if spec.note:
            out.append(spec.note)
            out.append("")
        out.append("| Field | Type | Req/Opt | Values | Meaning |")
        out.append("|---|---|---|---|---|")
        out.extend(_rows(spec.fields))
        out.append("")
    return "\n".join(out)


def main(argv: "list[str] | None" = None) -> int:
    """``--markdown`` writes the reference tables to stdout."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--markdown"]:
        print(render_markdown())
        return 0
    print("usage: python -m kiro_crew.crew_log.entry_types --markdown", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
