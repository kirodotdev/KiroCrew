"""Per-type ``data`` shapes for the session-ledger entry types, checked on append.

:data:`TYPE_OWNERSHIP` answers whether a KIND of unit has such events at all, by
domain prefix. This module answers the next question -- what does one entry of
this type carry -- and it is the single machine-readable source for it. The shape
of a ``data`` payload is otherwise stated twice, in the emitter that builds it and
in a spec table describing it, and two statements of one fact drift.

**What a declaration is derived from.** The WRITER, not the table: every field
below is read off the site that produces it (:mod:`kiro_crew.session_ledger_emit`
for the ordinary entries, ``store._closer_entries`` for the crash-repair closers).
A type earns a declaration by having a writer, so the 20 declared here are exactly
the session types something writes today; a type nothing writes is left undeclared
and passes through, which is the posture ``message/steered`` already gets. A field
is ``required`` only when EVERY writer of that type produces it, which is why a few
fields the spec table marks required are optional here -- the repair closer knows
the turn and the reason and nothing else, and a required field it cannot supply
would refuse the one write that closes an interrupted turn.

**Undeclared keys are refused**, the same posture and for the same reason as
:func:`~kiro_crew.ledger.schema.build_header`: a caller that misspells a field
would otherwise be told the entry landed as asked while the value it meant to
record silently vanished. So a new field arrives with its declaration, in one
commit, or not at all.

**Values come in two strengths, and only one of them refuses.** ``enum_closed``
marks a vocabulary the WRITER itself clamps -- ``turn/started.actor`` and
``turn/refused.actor`` are coerced to
:data:`~kiro_crew.session_ledger_emit.ACTORS` at the emitter -- so no caller can
produce a value outside it and enforcing costs nothing. Every other vocabulary is
PASSED THROUGH from
somewhere this module does not own: a provider's ``stop_reason``, the gateway's
own ``end_reason``, a provider's tool ``status``, a subagent runtime's outcome.
Those are recorded as ``enum`` for the reference tables and are NOT enforced,
because enforcing them converts "the upstream vocabulary grew" into "the entry is
refused and counted as a write loss" -- the registry would then destroy records
instead of catching mistakes.

Types with no declaration pass through untouched. That is what keeps the crew
ledger, whose own type families have no emitter, and every guest namespace
(``crew:<name>/…``, ``app:<name>/…``) writable while this covers the session
families that are written today.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from kiro_crew.ledger.errors import CODE_BAD_DATA_FIELD, LedgerError
from kiro_crew.ledger.schema import KIND_SESSION

#: JSON types a declared field may hold. ``int`` and ``float`` are separate
#: because the wire format's numbers are separate to a reader: a count is not a
#: measurement. ``float`` accepts an int, since JSON has one number type and 0 is
#: a legal reading of a percentage; ``int`` does not accept a float, because a
#: fractional token count or millisecond is a bug at the site that built it.
JSON_STRING = "string"
JSON_INT = "int"
JSON_FLOAT = "float"
JSON_BOOL = "bool"
JSON_OBJECT = "object"
JSON_ARRAY = "array"

JSON_TYPES: frozenset[str] = frozenset(
    {JSON_STRING, JSON_INT, JSON_FLOAT, JSON_BOOL, JSON_OBJECT, JSON_ARRAY}
)


@dataclass(frozen=True)
class Field:
    """One declared key of an entry's ``data``.

    ``fields`` describes the members of an object -- either this field's own, when
    ``json_type`` is :data:`JSON_OBJECT`, or its ELEMENTS', when ``json_type`` is
    :data:`JSON_ARRAY` and ``item_type`` is :data:`JSON_OBJECT`. One attribute
    serves both because the rules applied to a member and to an element's member
    are the same rules.
    """

    name: str
    json_type: str
    required: bool = False
    enum: tuple[str, ...] = ()
    enum_closed: bool = False
    item_type: str = ""
    fields: tuple["Field", ...] = ()
    note: str = ""

    def __post_init__(self) -> None:
        # A declaration is repo data, so a wrong one is a programming error rather
        # than a refused write: it is caught here, at import, instead of becoming a
        # check that silently passes everything.
        if self.json_type not in JSON_TYPES:
            raise ValueError(f"field {self.name!r} declares unknown json type {self.json_type!r}")
        if self.json_type == JSON_ARRAY and self.item_type not in JSON_TYPES:
            raise ValueError(f"array field {self.name!r} must declare an item_type")
        if self.fields and not (
            self.json_type == JSON_OBJECT
            or (self.json_type == JSON_ARRAY and self.item_type == JSON_OBJECT)
        ):
            raise ValueError(f"field {self.name!r} declares members but holds no object")
        if self.enum_closed and not self.enum:
            raise ValueError(f"field {self.name!r} is a closed enum with no values")


@dataclass(frozen=True)
class EntryType:
    """One declared entry type: what it says, and what its ``data`` carries."""

    type: str
    summary: str
    fields: tuple[Field, ...] = ()
    ignorable: bool = False
    note: str = ""

    @property
    def required_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.fields if item.required)


def _turn(note: str = "Turn ordinal.") -> Field:
    return Field("turn", JSON_INT, required=True, note=note)


#: The four billed token dimensions, each required INSIDE the mapping.
#: ``on_turn_completed`` builds the whole mapping in one literal, defaulting each
#: dimension to zero, so a present ``tokens`` always carries all four. The parent
#: field stays optional: the crash-repair closer omits ``tokens`` altogether, and a
#: nested requirement is checked only once its object is there.
_TOKEN_FIELDS: tuple[Field, ...] = (
    Field("input", JSON_INT, required=True),
    Field("output", JSON_INT, required=True),
    Field("cache_read", JSON_INT, required=True),
    Field("cache_write", JSON_INT, required=True),
)

#: Who caused a turn. Enforced: the emitter coerces anything outside this set to
#: ``other`` before it builds the entry, so no call site can widen it.
ACTOR_VALUES: tuple[str, ...] = (
    "user",
    "app",
    "crew",
    "cron",
    "autonudge",
    "subagent",
    "gateway",
    "other",
)

_SESSION_TYPES: tuple[EntryType, ...] = (
    # -- session, turn ------------------------------------------------------ #
    EntryType(
        "session/opened",
        "The ledger was created, or this claim re-attached to an existing conversation.",
        (
            Field("agent", JSON_STRING, required=True, note="Agent name."),
            Field("slot", JSON_STRING, required=True, note="Slot key; may be empty."),
            Field(
                "model",
                JSON_STRING,
                required=True,
                note="Configured model; empty when the backend serves its own default.",
            ),
            Field("cwd", JSON_STRING, required=True, note="Working directory; may be empty."),
            Field("owner", JSON_STRING, required=True, note="Owner."),
            Field(
                "resumed",
                JSON_BOOL,
                required=True,
                note="True when this claim re-attached to an existing ledger.",
            ),
        ),
    ),
    EntryType(
        "session/closed",
        "The gateway stopped serving this session, for a stated reason.",
        (
            Field(
                "reason",
                JSON_STRING,
                required=True,
                enum=("reset",),
                note=(
                    "The gateway's own end_reason, verbatim. Open: the teardown "
                    "vocabulary belongs to metrics.sessions, which holds more "
                    "reasons than any site passes here today."
                ),
            ),
        ),
    ),
    EntryType(
        "turn/started",
        "A turn was authorized and is about to run.",
        (
            _turn("Message-boundary ordinal identifying the turn."),
            Field(
                "actor",
                JSON_STRING,
                required=True,
                enum=ACTOR_VALUES,
                enum_closed=True,
                note="Who caused the turn; the emitter coerces an unknown value to other.",
            ),
            Field("depth", JSON_INT, required=True, note="Prompt depth."),
            Field(
                "message_seq",
                JSON_INT,
                note="Seq of the causing message entry; absent when unknown.",
            ),
            Field(
                "attempt",
                JSON_INT,
                note="Which try at this ordinal; absent at 1, present on a rerun.",
            ),
        ),
    ),
    EntryType(
        "turn/refused",
        "A turn was dispatched but a gate refused to run it.",
        (
            _turn(),
            Field(
                "actor",
                JSON_STRING,
                required=True,
                enum=ACTOR_VALUES,
                enum_closed=True,
                note="Same coercion as turn/started.",
            ),
            Field(
                "reason",
                JSON_STRING,
                required=True,
                enum=("not_authorized", "gateway_closing", "stopped_before_dispatch"),
                note=(
                    "Which gate refused. Open: a gate added to the dispatch path "
                    "names its own reason, and refusing it would lose the record "
                    "of the refusal itself."
                ),
            ),
            Field("depth", JSON_INT, required=True, note="Prompt depth."),
        ),
    ),
    EntryType(
        "turn/completed",
        "A turn ended; records its outcome and cost.",
        (
            _turn(),
            Field(
                "stop_reason",
                JSON_STRING,
                required=True,
                enum=("failed", "interrupted"),
                note=(
                    "How it ended. Open: the measured closer passes the provider's "
                    "own terminal reason through. failed is the in-process closer, "
                    "interrupted is written only by crash-repair."
                ),
            ),
            Field(
                "depth",
                JSON_INT,
                note="Prompt depth. Absent on the crash-repair closer, which cannot know it.",
            ),
            Field(
                "duration_ms",
                JSON_INT,
                note="Measured turn duration. Absent on the crash-repair closer.",
            ),
            Field(
                "model",
                JSON_STRING,
                note="Model the turn served on. Absent on the crash-repair closer.",
            ),
            Field("provider", JSON_STRING, note="Provider. Absent on the crash-repair closer."),
            Field(
                "credits",
                JSON_FLOAT,
                note="Present on a provider-reported completion; absent on a synthesized close.",
            ),
            Field(
                "tokens",
                JSON_OBJECT,
                fields=_TOKEN_FIELDS,
                note="Present with credits; absent on a synthesized close.",
            ),
            Field(
                "error",
                JSON_STRING,
                note="Exception class name, never its message, on an in-process failed close.",
            ),
        ),
        note=(
            "Three writers close a turn: the measured path, the in-process failed "
            "closer, and crash-repair. Only turn and stop_reason are common to all "
            "three, so the other fields are optional here even though the spec "
            "table marks four of them required."
        ),
    ),
    EntryType(
        "write/dropped",
        "One durable account of writer losses before later entries resume.",
        (
            Field("dropped_count", JSON_INT, required=True, note="How many appends were lost."),
            Field("dropped_bytes", JSON_INT, required=True, note="Size hint for the lost jobs."),
        ),
    ),
    # -- message, request, step --------------------------------------------- #
    EntryType(
        "message/received",
        "The body of a message the gateway accepted into this session.",
        (
            _turn(),
            Field("role", JSON_STRING, required=True, note="Message role."),
            Field(
                "source", JSON_STRING, required=True, note="Surface it arrived on; may be empty."
            ),
            Field(
                "text",
                JSON_STRING,
                note="Redacted body. Replaced by chunks when the body overflows one line.",
            ),
            Field(
                "attachments",
                JSON_ARRAY,
                item_type=JSON_STRING,
                note="Attachment ids, not refs. Absent when there are none.",
            ),
            Field(
                "attachments_omitted",
                JSON_INT,
                note="How many ids were dropped to fit the entry.",
            ),
            Field(
                "chunks",
                JSON_ARRAY,
                item_type=JSON_INT,
                note="Chunk seqs, present instead of text on an overflow body.",
            ),
            Field("chars", JSON_INT, note="Character count of the full body, with chunks."),
        ),
        note="Carries either text or chunks; the pair is a cross-field rule, not a field shape.",
    ),
    EntryType(
        "message/sent",
        "A finished assistant message -- one model call's worth of text.",
        (
            _turn(),
            Field("step", JSON_INT, note="Model call ordinal; absent when unknown."),
            Field("text", JSON_STRING, note="Redacted body, or replaced by chunks on overflow."),
            Field("interrupted", JSON_BOOL, note="True when a steer cut this reply."),
            Field(
                "chunks", JSON_ARRAY, item_type=JSON_INT, note="Chunk seqs on the overflow form."
            ),
            Field("chars", JSON_INT, note="Full-body character count, with chunks."),
        ),
        note="No usage: usage is measured per turn and rides on turn/completed.",
    ),
    EntryType(
        "message/chunk",
        "One slice of an oversize body.",
        (
            _turn(),
            Field("step", JSON_INT, note="Model call ordinal, on assistant bodies."),
            Field("delta", JSON_STRING, required=True, note="One redacted slice of the body."),
        ),
        ignorable=True,
    ),
    EntryType(
        "message/queued",
        "A message arrived while a turn was already running.",
        (
            Field("source", JSON_STRING, required=True, note="Surface it arrived on."),
            Field("bytes", JSON_INT, required=True, note="Size of the queued message."),
            Field("queued_seq", JSON_STRING, required=True, note="The queue entry's own id."),
        ),
        note="No turn: a queued message belongs to no turn yet.",
    ),
    EntryType(
        "request/configured",
        "The request configuration, recorded only when it changed.",
        (
            _turn(),
            Field("model", JSON_STRING, required=True, note="Model."),
            Field("provider", JSON_STRING, required=True, note="Provider."),
            Field("context_window", JSON_INT, required=True, note="Context window size."),
            Field("system", JSON_STRING, note="sha256 of the system prompt, when one is supplied."),
            Field("system_bytes", JSON_INT, note="Byte length of the system prompt, with system."),
        ),
        note="No tools list: the gateway never receives the resolved tool set with tool search on.",
    ),
    EntryType(
        "context/composed",
        "What the gateway put in front of the model, block by block.",
        (
            _turn(),
            Field("step", JSON_INT, note="Model call ordinal; absent when unknown."),
            Field(
                "sources",
                JSON_ARRAY,
                required=True,
                item_type=JSON_OBJECT,
                fields=(
                    Field("kind", JSON_STRING, required=True, note="Block label."),
                    Field("chars", JSON_INT, required=True, note="Characters in the block."),
                    Field("tokens", JSON_INT, required=True, note="Estimated tokens."),
                ),
                note="Per-block tallies, sorted by descending chars.",
            ),
            Field("chars", JSON_INT, required=True, note="Total characters."),
            Field("tokens", JSON_INT, required=True, note="Estimated tokens."),
            Field(
                "tokens_estimated",
                JSON_BOOL,
                required=True,
                note="Always true -- tokens are derived from characters.",
            ),
        ),
    ),
    EntryType(
        "step/started",
        "Opens one model call inside a turn.",
        (_turn(), Field("step", JSON_INT, required=True, note="Model call ordinal, from 1.")),
    ),
    EntryType(
        "step/completed",
        "Closes one model call and records how long it took.",
        (
            _turn(),
            Field("step", JSON_INT, required=True, note="Model call ordinal."),
            Field("ms", JSON_INT, required=True, note="Duration."),
        ),
    ),
    # -- tool, approval ----------------------------------------------------- #
    EntryType(
        "tool/called",
        "A tool call, identified by id; arguments are digested, never recorded.",
        (
            _turn(),
            Field("call_id", JSON_STRING, required=True, note="Tool call id; may be empty."),
            Field("name", JSON_STRING, required=True, note="Trusted tool name; may be empty."),
            Field("server", JSON_STRING, required=True, note="MCP server name; may be empty."),
            Field("kind", JSON_STRING, required=True, note="Tool kind; may be empty."),
            Field("call_index", JSON_INT, note="Position among the turn's calls; absent at 0."),
            Field("step", JSON_INT, note="Model call that issued it; absent at 0."),
            Field("args_hash", JSON_STRING, note="sha256 of the serialized args, when there are."),
            Field(
                "args_bytes", JSON_INT, note="Byte length of the serialized args, with the hash."
            ),
        ),
    ),
    EntryType(
        "tool/completed",
        "A tool call's terminal frame; results are digested, never recorded.",
        (
            _turn(),
            Field("call_id", JSON_STRING, required=True, note="Same id as the call."),
            Field("name", JSON_STRING, required=True, note="Filled from the remembered call."),
            Field("server", JSON_STRING, required=True, note="Filled from the remembered call."),
            Field(
                "status",
                JSON_STRING,
                required=True,
                enum=("completed", "refused", "unknown"),
                note=(
                    "Outcome. Open: the frame's own status is passed through. "
                    "unknown is written by the turn-end sweep and by crash-repair."
                ),
            ),
            Field("call_index", JSON_INT, note="Present when known."),
            Field("step", JSON_INT, note="Present when known."),
            Field("elapsed_ms", JSON_INT, note="Present when the call frame was in memory."),
            Field("is_error", JSON_BOOL, note="Tri-state: absent when the caller did not assert."),
            Field("result_hash", JSON_STRING, note="sha256 of the redacted result, when there is."),
            Field("result_bytes", JSON_INT, note="Byte length; 0 on an output-less close."),
        ),
    ),
    EntryType(
        "approval/requested",
        "A tool call is waiting on a human.",
        (
            _turn(),
            Field("approval_id", JSON_STRING, required=True, note="Approval request id."),
            Field("tool", JSON_STRING, note="Tool name; absent when the frame named none."),
            Field("reason", JSON_STRING, note="Redacted, clipped title shown to the human."),
        ),
    ),
    EntryType(
        "approval/decided",
        "How an approval resolved.",
        (
            _turn(),
            Field("approval_id", JSON_STRING, required=True, note="Same id as the request."),
            Field(
                "decision",
                JSON_STRING,
                required=True,
                enum=("approved", "rejected", "rejected_once", "unknown"),
                note=(
                    "The decision, as the resolving surface worded it. Open: the "
                    "approval vocabulary is the dashboard's. unknown is written "
                    "only by crash-repair."
                ),
            ),
            Field(
                "by",
                JSON_STRING,
                enum=("host",),
                note="Written only for a host-made decision; absent for a person's answer.",
            ),
            Field("cause", JSON_STRING, note="Host's reason code for an auto-decline."),
        ),
    ),
    # -- model, compaction, plan -------------------------------------------- #
    EntryType(
        "model/selected",
        "A model swap, and why it was chosen.",
        (
            Field("model", JSON_STRING, required=True, note="Model id."),
            Field("source", JSON_STRING, required=True, note="Why it was chosen."),
            Field("turn", JSON_INT, note="The turn the pick was made for; absent outside a turn."),
        ),
        note="A session's starting model rides on session/opened; this records a fallback swap.",
    ),
    EntryType(
        "compaction/applied",
        "A compaction, recorded as context-usage percentages.",
        (
            Field("pct_before", JSON_FLOAT, required=True, note="Context usage % before."),
            Field("pct_after", JSON_FLOAT, required=True, note="Context usage % after."),
            Field(
                "freed_pct",
                JSON_FLOAT,
                required=True,
                note="pct_before minus pct_after; negative when a deferred reading grew.",
            ),
        ),
        note="No turn: the deferred verdict can settle turns later than the compaction.",
    ),
)

#: The session types that have a writer. Keyed by ``type`` for the append path.
SESSION_ENTRY_TYPES: dict[str, EntryType] = {item.type: item for item in _SESSION_TYPES}

#: Per kind, because the question "what does this type carry" is asked of a unit.
#: A crew registry drops in beside this one when a crew emitter lands; until then
#: a crew ledger's types are simply undeclared and pass through.
ENTRY_TYPES: dict[str, dict[str, EntryType]] = {KIND_SESSION: SESSION_ENTRY_TYPES}


def declaration_for(kind: str, entry_type: str) -> EntryType | None:
    """The declaration for (*kind*, *entry_type*), or ``None`` when undeclared."""
    return ENTRY_TYPES.get(kind, {}).get(entry_type)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def _refuse(path: str, message: str) -> LedgerError:
    return LedgerError(f"{path}: {message}", code=CODE_BAD_DATA_FIELD, field=path)


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
    falls outside a CLOSED enum. Returns silently for a type with no declaration,
    which is every crew type and every guest namespace.

    A refusal is a :class:`~kiro_crew.ledger.errors.LedgerError`, so the
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
    out: list[str] = [f"# Declared `{kind}` ledger entry types", ""]
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
    print("usage: python -m kiro_crew.ledger.entry_types --markdown", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
