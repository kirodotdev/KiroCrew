"""Per-type ``data`` shapes for the session crew log entry types, checked on append.

:data:`TYPE_OWNERSHIP` answers whether a KIND of unit has such events at all, by
domain prefix. This module answers the next question -- what does one entry of
this type carry -- and it is the single machine-readable source for it. The shape
of a ``data`` payload is otherwise stated twice, in the emitter that builds it and
in a spec table describing it, and two statements of one fact drift.

**What a declaration is derived from.** The WRITER, not the table: every field
below is read off the site that produces it (:mod:`kiro_crew.crew_log.emit`
for the ordinary entries, ``store._closer_entries`` for the crash-repair closers).
A type earns a declaration by having a writer, so the types declared here are
exactly the session types something writes today, whether or not the writer marks
the entry ignorable. An ignorable write is not exempt: a folding reader SKIPS an
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

Types with no declaration pass through untouched. That is what keeps the crew
crew log, whose own type families have no emitter, and every guest namespace
(``crew:<name>/…``, ``app:<name>/…``) writable while this covers the session
families that are written today.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from kiro_crew.crew_log.errors import CODE_BAD_DATA_FIELD, CrewLogError
from kiro_crew.crew_log.schema import KIND_SESSION

# The ledger subsystem owns the event vocabulary its own writer clamps to, so the
# declaration below reads it from there instead of restating it. Importing the
# producer is what this module already does for every other type -- the difference
# is only that this producer's vocabulary is a named constant. The import is safe
# in this direction: ``session_ledger`` reaches the crew log lazily, inside the
# functions that need it, so nothing here pulls the storage package onto the
# gateway's boot path.
from kiro_crew.session_ledger import EVENT_KINDS as _LEDGER_EVENT_KINDS

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

#: The ledger's event kinds, in a stable order for the reference tables. Derived
#: from the writer's own set so the two cannot drift.
_EVENT_KIND_VALUES: tuple[str, ...] = tuple(sorted(_LEDGER_EVENT_KINDS))

#: The members of a session's recorded class, shared by the opening entry's
#: ``class`` object and by ``session/class``. One tuple rather than two identical
#: ones, because a reader folds the second over the first to decide an
#: authorization question: a member declared on one and not the other would be
#: read from a transition and silently missing from the opener it supersedes.
_SESSION_CLASS_FIELDS: tuple[Field, ...] = (
    Field(
        "memory",
        JSON_STRING,
        required=True,
        note=(
            "The slot's memory mode, verbatim: persistent for an ordinary "
            "session, anything else for one created to leave and learn nothing. "
            "Required INSIDE the object, so the object is never empty and its "
            "presence is what says the class was recorded at all."
        ),
    ),
    Field(
        "app",
        JSON_STRING,
        note=(
            "The app that owns the session, when one does -- a short registered "
            "app name, never a title or user content."
        ),
    ),
    Field(
        "channel",
        JSON_BOOL,
        note=(
            "True when this session's conversation is published to a messaging "
            "channel, by a link or a mirror. A cron tab's link is not one: it "
            "names the job's own run and republishes to nobody. The channel is "
            "not named -- a reader of this field needs the fact, not the address."
        ),
    ),
    Field(
        "workspace",
        JSON_STRING,
        note=(
            "The workspace this session belongs to. Recorded because a dispatch "
            "grant is derived from a lineage and a lineage OUTLIVES a workspace "
            "switch -- the creating edge is written on the child and nothing "
            "rewrites it -- so without this a conductor that moved workspace would "
            "still read a log belonging to the one it left. Not a restriction like "
            "the members above but an identity, so a reader keeps the FIRST one "
            "stated and treats a later different one as the log spanning two "
            "workspaces, which no single workspace's session may read. Absent on a "
            "log opened before this field existed, and a reader that needs it must "
            "refuse rather than assume."
        ),
    ),
)

#: Who may write an ``object/observed`` entry. CLOSED, and closed on purpose: the
#: value is what lets a reader tell a measured record from anything an agent typed,
#: so the emitter REFUSES a value outside this tuple rather than coercing it -- a
#: coerced producer would be a record attributed to a mechanism that did not make
#: it. ``probe`` is the structured monitor's provider probe. A second producer (a
#: recogniser on the tool-result path, say) is added here, in one commit with the
#: site that writes it, or not at all.
OBJECT_PRODUCER_PROBE = "probe"
OBJECT_PRODUCERS: tuple[str, ...] = (OBJECT_PRODUCER_PROBE,)

_SESSION_TYPES: tuple[EntryType, ...] = (
    # -- session, turn ------------------------------------------------------ #
    EntryType(
        "session/opened",
        "The crew log was created, or this claim re-attached to an existing conversation.",
        (
            Field("agent", JSON_STRING, required=True, note="Agent name."),
            Field("slot", JSON_STRING, required=True, note="Slot key; may be empty."),
            Field(
                "model",
                JSON_STRING,
                required=True,
                note=(
                    "Model the backend confirmed is serving this session; empty when "
                    "that id is not known, which covers both the backend's own default "
                    "and a configured model that was never applied."
                ),
            ),
            Field(
                "model_requested",
                JSON_STRING,
                note=(
                    "Model the gateway SELECTED for the allocation that produced this "
                    "session, before the provider decides whether to send it -- a model "
                    "this account cannot run is withheld rather than requested. Absent "
                    "when no tier resolved one, and also when this gateway process did "
                    "not observe the allocation, as on a re-attach. A difference from "
                    "model is not by itself a refusal: the backend serves the spelling "
                    "it resolved."
                ),
            ),
            Field("cwd", JSON_STRING, required=True, note="Working directory; may be empty."),
            Field("owner", JSON_STRING, required=True, note="Owner."),
            Field(
                "resumed",
                JSON_BOOL,
                required=True,
                note="True when this claim re-attached to an existing crew log.",
            ),
            Field(
                "previous",
                JSON_OBJECT,
                fields=(
                    Field(
                        "sid",
                        JSON_STRING,
                        required=True,
                        note=(
                            "The ACP session id of the store this slot was writing "
                            "before. A citation of that unit, not a tree key."
                        ),
                    ),
                ),
                note=(
                    "The store the SAME slot was writing before this one, present only "
                    "on a store that was just created while the slot already had one. "
                    "``resumed`` covers the other continuity -- this claim re-attaching "
                    "to the same store -- and cannot express this one, because a "
                    "superseded ACP session has a different id and therefore a "
                    "different unit. No ``slot`` is repeated inside: it is the slot in "
                    "``data.slot``. Absent on the slot's first store, and on any store "
                    "whose predecessor the gateway could not name."
                ),
            ),
            Field(
                "parent",
                JSON_OBJECT,
                fields=(
                    Field(
                        "slot",
                        JSON_STRING,
                        required=True,
                        note="The creating session's key, as session_create attributed it.",
                    ),
                    Field(
                        "sid",
                        JSON_STRING,
                        note=(
                            "The creator's ACP session id, frozen by session_create when "
                            "it minted this session -- the creator crew log that holds the "
                            "call. Absent when the creator had no live handle at mint, or "
                            "when its id exceeded MAX_ACP_SESSION_ID_LEN and was dropped "
                            "at retention rather than stored."
                        ),
                    ),
                ),
                note=(
                    "The session that made this one through session_create. Recorded "
                    "on the CHILD, because the child knows its creator at its first turn "
                    "while the creator never learns the child's session id. Absent on a "
                    "person's own tab, on a fork, and on a spawn_run subagent."
                ),
            ),
            Field(
                "class",
                JSON_OBJECT,
                fields=_SESSION_CLASS_FIELDS,
                note=(
                    "What this session IS, as facts rather than as a verdict, recorded "
                    "when the log is opened. It is here because the crew log is the "
                    "authoritative record of a session and a reader deciding whether "
                    "one session may read another's log must be able to answer that "
                    "for a session that has since CLOSED, which no live lookup can. "
                    "Absent on a log opened before this field existed, and a reader "
                    "that needs it must refuse rather than assume: a missing record "
                    "is not evidence that nothing applies."
                ),
            ),
        ),
    ),
    EntryType(
        "session/class",
        "The session's class changed after its log was opened.",
        _SESSION_CLASS_FIELDS,
        note=(
            "The class as re-observed after the log was opened, written only when it "
            "differs from the last one recorded. The opening entry states the class as of "
            "the moment the log was created, and a session can acquire a channel surface, "
            "an app owner or a different memory mode afterwards -- so a reader deciding "
            "whether another session may read this log has to see the whole life of it, "
            "not its first instant. The fold takes the most restrictive value each "
            "member ever held, because a log that was published to a channel for one "
            "turn holds that turn's content for good.\n\n"
            "Observed at TWO points, which together are what make the record exact "
            "rather than approximate. A channel binding announces itself as it COMMITS: "
            "the record is made while the session map's lock is still held, and routing "
            "an inbound message reads that map, so the turn that carries a third party's "
            "words into the log cannot precede the record of the surface that carried "
            "them. Every other way a class moves -- an app owner, a different memory "
            "mode -- is caught by re-observing at the start of a turn, so a change that "
            "commits with no announcement is recorded before the next turn appends "
            "anything.\n\n"
            "Absent from a log whose class never changed, which is the ordinary case. "
            "That absence is only readable as 'nothing changed' on a log whose opening "
            "entry HAS a class: the two landed in one change, so a class on the opener "
            "is what dates the log to a build that also records transitions. An opener "
            "with no class says nothing about either, and refuses."
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
    EntryType(
        "plan/updated",
        "The agent's task list, as the agent just restated it.",
        (
            _turn("The turn the plan was restated in."),
            Field(
                "items",
                JSON_ARRAY,
                item_type=JSON_OBJECT,
                required=True,
                fields=(
                    Field("id", JSON_STRING, required=True, note="The task's id, clipped."),
                    Field("text", JSON_STRING, required=True, note="The task's text, clipped."),
                    Field(
                        "state",
                        JSON_STRING,
                        required=True,
                        enum=("done", "open"),
                        enum_closed=True,
                        note=(
                            "Closed: the writer computes it as done-or-open from the "
                            "stream's single completed boolean, so no caller can produce "
                            "a third value. The backend's todo model carries no "
                            "in-progress state, so a three-state vocabulary would be "
                            "invented here."
                        ),
                    ),
                ),
                note=(
                    "The plan as of this update, and the FRONT of it when clipped. "
                    "Required and present even when empty: an empty list is the agent "
                    "clearing its plan, which is a change and is recorded as one, while "
                    "an event carrying no list at all writes no entry."
                ),
            ),
            Field(
                "total",
                JSON_INT,
                note=(
                    "The real task count, written only when items is shorter than it. "
                    "The list is bounded twice, by count and by serialized bytes, and "
                    "this is how a clipped record says how much it is not showing."
                ),
            ),
        ),
        ignorable=True,
        note=(
            "A WHOLE list, not a delta: the agent re-sends every task on every change, "
            "so a reader diffs consecutive entries itself. Written ignorable because it "
            "samples a stream -- nothing later in the file depends on any single update "
            "having been read -- but it is declared all the same. An undeclared type is "
            "SKIPPED by a folding reader rather than refused, and a skip is a seq "
            "discontinuity: the class fold treats any gap in what it receives as damage "
            "and recorded_class then refuses, so leaving this undeclared made the class "
            "record unreadable for every session whose agent touched its task list."
        ),
    ),
    # -- subagent, background ----------------------------------------------- #
    EntryType(
        "subagent/spawned",
        "A child this session dispatched.",
        (
            Field("agent_id", JSON_STRING, required=True, note="The child's run id."),
            Field(
                "turn",
                JSON_INT,
                note=(
                    "The turn that ASKED, captured where the spawn was accepted. Absent "
                    "when no turn asked -- a slash command, a cron and a hook all "
                    "dispatch children of a session with nothing running, and turns are "
                    "numbered from one, so a literal 0 would name a turn that never "
                    "existed."
                ),
            ),
            Field("agent", JSON_STRING, note="The child's agent name, when one was resolved."),
            Field("model", JSON_STRING, note="The child's model, when one was resolved."),
            Field(
                "scope",
                JSON_OBJECT,
                fields=(
                    Field("memory", JSON_BOOL, required=True),
                    Field("lessons", JSON_BOOL, required=True),
                    Field("project", JSON_BOOL, required=True),
                ),
                note=(
                    "What context the child inherited. The writer builds all three "
                    "members in one literal, so a present scope always carries them all; "
                    "the parent field stays optional because a dispatch that passed no "
                    "scope mapping omits it."
                ),
            ),
        ),
        note=(
            "No ref into the child's log: no subagent code path opens one, and a ref "
            "written now would cite a file that does not exist. Closed by "
            "subagent/completed or subagent/failed carrying the same agent_id -- which "
            "crash-repair matches across the WHOLE file, since a child outlives the turn "
            "that asked for it by design."
        ),
    ),
    EntryType(
        "subagent/steered",
        "A correction sent into a running child.",
        (
            Field("agent_id", JSON_STRING, required=True, note="The child's run id."),
            Field(
                "mode",
                JSON_STRING,
                enum=("interrupt", "follow_up"),
                note=(
                    "How the correction was delivered: injected into the running turn, or "
                    "queued for after it. Open -- the emitter passes the caller's word "
                    "through rather than clamping it, so a third delivery mode must be "
                    "recorded rather than refused."
                ),
            ),
        ),
        note=(
            "Written into the PARENT's log: the parent is what sent it, and the child has "
            "no crew log to receive it. Opens and closes nothing -- a steer is an event "
            "about a child, not a state of one."
        ),
    ),
    EntryType(
        "subagent/completed",
        "A child closed having finished its work.",
        (
            Field("agent_id", JSON_STRING, required=True, note="The child's run id."),
            Field("ms", JSON_INT, note="Measured run duration; absent when it was not measured."),
        ),
        note=(
            "Only the completed outcome. A stopped or failed child closes through "
            "subagent/failed, because the runtime's three-way outcome exists precisely to "
            "stop consumers reading 'no error' as success. No tokens and no credits, and "
            "their absence is the record: nothing in the subagent runtime measures either, "
            "so writing zeros would present the absence of a measurement as a measurement "
            "of zero."
        ),
    ),
    EntryType(
        "subagent/failed",
        "A child closed WITHOUT finishing its work.",
        (
            Field("agent_id", JSON_STRING, required=True, note="The child's run id."),
            Field(
                "reason",
                JSON_STRING,
                note=(
                    "The run's error text, clipped. Absent when the run carried none, and "
                    "on the crash-repair closer, which knows only that the writer is gone."
                ),
            ),
            Field(
                "outcome",
                JSON_STRING,
                enum=("failed", "stopped", "unknown"),
                note=(
                    "WHICH non-success this was: a run the user stopped is not a failure "
                    "and must not read as one, but it is also not a completion, and the "
                    "vocabulary offers no third closer. unknown is written only by "
                    "crash-repair. Open -- the value is the subagent runtime's own, so "
                    "enforcing the set would turn 'the upstream vocabulary grew' into a "
                    "lost record."
                ),
            ),
            Field("ms", JSON_INT, note="Measured run duration; absent on the repair closer."),
        ),
        note=(
            "Two writers close a child this way: the runtime's own terminal report, and "
            "crash-repair. Only agent_id is common to both, so every other field is "
            "optional -- the repair closer knows the child's id and that nothing will "
            "report for it."
        ),
    ),
    EntryType(
        "background/completed",
        "A model call the gateway made ON this session's behalf, and what it cost.",
        (
            Field(
                "kind",
                JSON_STRING,
                required=True,
                enum=("title", "summary", "memory_consolidation"),
                note=(
                    "Which background helper spent the budget. Open: the set grows with "
                    "each helper wired, and refusing an unrecognized one would drop the "
                    "only trace of a charge."
                ),
            ),
            Field("model", JSON_STRING, note="Model the call served on."),
            Field("provider", JSON_STRING, note="Provider."),
            Field("credits", JSON_FLOAT, note="Present only when this provider billed credits."),
            Field(
                "tokens",
                JSON_OBJECT,
                fields=(
                    Field("input", JSON_INT),
                    Field("output", JSON_INT),
                    Field("cache_read", JSON_INT),
                    Field("cache_write", JSON_INT),
                ),
                note=(
                    "Only the dimensions this provider actually billed. Unlike "
                    "turn/completed's mapping, each member is OPTIONAL: the writer drops "
                    "every zero, so a call billed on input alone carries input alone, and "
                    "requiring the four would refuse it. A present dimension is a "
                    "measurement; an absent one is 'this provider does not bill here'."
                ),
            ),
            Field("ms", JSON_INT, note="Wall clock measured around the call itself."),
        ),
        note=(
            "No turn. The call is not part of one -- it runs after a turn ends, on a "
            "separate background session -- and naming the turn that happened to be last "
            "would attribute the cost to work that did not cause it. Titling, summarizing "
            "and memory consolidation spend the user's budget without the user asking, "
            "and this is that trace."
        ),
    ),
    # -- ledger ------------------------------------------------------------- #
    EntryType(
        "ledger/recorded",
        "One session-ledger update: the fields it set, and the event explaining them.",
        (
            Field(
                "slot",
                JSON_STRING,
                required=True,
                note=(
                    "The ledger's key -- the slot this update belongs to. Carried on the "
                    "entry as well as in the header so a reader of one entry can say "
                    "which slot it belongs to; selecting a slot's units is done from "
                    "their headers."
                ),
            ),
            Field("goal", JSON_STRING, note="The workstream's objective, when this call set one."),
            Field(
                "phase",
                JSON_STRING,
                note=(
                    "The new phase. Never written without event and event_kind, which is "
                    "what makes the phase-requires-a-reason rule a property of ONE entry."
                ),
            ),
            Field("next", JSON_STRING, note="The resumable intent -- the concrete next step."),
            Field(
                "tried",
                JSON_OBJECT,
                fields=(
                    Field("approach", JSON_STRING, required=True, note="What was tried."),
                    Field("rejected_because", JSON_STRING, note="Why it was rejected."),
                ),
                note="One rejected approach, appended to the fold's list.",
            ),
            Field(
                "artifacts",
                JSON_OBJECT,
                note=(
                    "String-to-string pointers merged into the fold's map. The MEMBERS are "
                    "the caller's own keys -- worktree, branch, pr -- so they are "
                    "deliberately not declared and are checked for shape by the fold."
                ),
            ),
            Field("event", JSON_STRING, note="One-line progress note appended to the event tail."),
            Field(
                "event_kind",
                JSON_STRING,
                enum=_EVENT_KIND_VALUES,
                enum_closed=True,
                note=(
                    "Which kind of step this records. Closed: the writer coerces an "
                    "unrecognized kind to note before it builds the entry."
                ),
            ),
        ),
        note=(
            "One entry per ``session_ledger_record`` call, carrying only the fields that "
            "call set -- an omitted field means 'unchanged', which is what lets a partial "
            "update be one line. A phase change carries its event in the SAME entry, so "
            "no reader can observe a phase that moved without its logged reason. The "
            "ledger therefore DEPENDS on this log: a gateway started without "
            "``KIROCREW_CREW_LOG=1`` records none, and the tool refuses rather than "
            "keeping a document of its own."
        ),
    ),
    # -- object ------------------------------------------------------------- #
    EntryType(
        "object/observed",
        "The state of an object outside the session, as one named producer observed it.",
        (
            Field(
                "producer",
                JSON_STRING,
                required=True,
                enum=OBJECT_PRODUCERS,
                enum_closed=True,
                note=(
                    "Which mechanism made the observation. Closed: the emitter refuses a "
                    "value outside the vocabulary instead of coercing it, so a reader can "
                    "tell a measured record from a sentence an agent typed. probe is the "
                    "structured monitor's provider probe."
                ),
            ),
            Field(
                "kind",
                JSON_STRING,
                required=True,
                note=(
                    "The monitored kind of the subject, as the monitoring registry names "
                    "it -- github_pull_request, gitlab_merge_request, and so on. Passed "
                    "through from the armed monitor, which validated it at arm time."
                ),
            ),
            Field(
                "target",
                JSON_STRING,
                required=True,
                note="The subject's full URL, exactly as the monitor was armed on it.",
            ),
            Field(
                "fingerprint",
                JSON_STRING,
                required=True,
                note=(
                    "The probe's own dedupe digest of the facts it acts on. An entry is "
                    "written only when this differs from the previous observation's, so "
                    "consecutive entries for one subject are consecutive DISTINCT states, "
                    "never one per poll."
                ),
            ),
            Field(
                "facts",
                JSON_OBJECT,
                required=True,
                note=(
                    "The canonical facts snapshot the probe computed, verbatim -- the "
                    "object the wake envelope is rendered from, including its own kind "
                    "and target. The members are the kind's canonical vocabulary, so "
                    "they are deliberately not declared here: a fact the probe could not "
                    "establish is absent or carries the kind's own unknown marker, never "
                    "a default this registry invented."
                ),
            ),
            Field(
                "facts_omitted",
                JSON_ARRAY,
                item_type=JSON_STRING,
                note=(
                    "Members removed from facts so the entry fits the line ceiling, "
                    "largest first. Absent when nothing was removed, which is the "
                    "ordinary case."
                ),
            ),
            Field(
                "observed_at",
                JSON_FLOAT,
                required=True,
                note=(
                    "When the producer observed the subject, seconds since the epoch. "
                    "Distinct from the envelope's time, which is when the append landed."
                ),
            ),
        ),
        note=(
            "One entry per CHANGE of the subject's fingerprint, appended into the log of "
            "the session the producer works for -- the monitor's owner session. A typed "
            "record carrying its producer is what a reader can trust about an object "
            "outside the session; the agent's own report about that object is a "
            "message/sent entry and is evidence of nothing but the report."
        ),
    ),
)

#: The session types that have a writer. Keyed by ``type`` for the append path.
SESSION_ENTRY_TYPES: dict[str, EntryType] = {item.type: item for item in _SESSION_TYPES}

#: Per kind, because the question "what does this type carry" is asked of a unit.
#: A crew registry drops in beside this one when a crew emitter lands; until then
#: a crew's log's types are simply undeclared and pass through.
ENTRY_TYPES: dict[str, dict[str, EntryType]] = {KIND_SESSION: SESSION_ENTRY_TYPES}


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
    falls outside a CLOSED enum. Returns silently for a type with no declaration,
    which is every crew type and every guest namespace.

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
