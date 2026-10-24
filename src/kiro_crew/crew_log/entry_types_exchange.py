"""Crew log declarations for what a turn exchanges.

The ``message/``, ``request/``, ``context/``, ``step/``, ``tool/``, ``approval/``,
``model/``, ``compaction/`` and ``plan/`` types: a message the gateway accepted, an
assistant message, one slice of an oversize body, a message that arrived while a turn
was running, the request configuration and the context composed for the model, the model
calls a turn makes, its tool calls and the approvals they wait on, a model swap, a
compaction and the agent's task list. The members a context omission carries sit beside
them.

:mod:`kiro_crew.crew_log.entry_types` composes these declarations into the session table;
the rules every declaration follows are in that module's docstring.
"""

from __future__ import annotations

from kiro_crew import context_omissions as _omissions
from kiro_crew.crew_log.entry_types_fields import (
    JSON_ARRAY,
    JSON_BOOL,
    JSON_FLOAT,
    JSON_INT,
    JSON_OBJECT,
    JSON_STRING,
    EntryType,
    Field,
    _turn,
)

#: One ``context/composed`` omission entry's members, shared by every block kind:
#: a reason from a closed set, then whichever counts that block's decision has.
_OMISSION_MEMBERS: tuple[Field, ...] = (
    Field(
        "reason",
        JSON_STRING,
        required=True,
        enum=_omissions.REASONS,
        enum_closed=True,
        note="budget (a character budget), cap (a hard ceiling) or relevance (withheld).",
    ),
    Field("omitted", JSON_INT, note="Items left out."),
    Field("total", JSON_INT, note="Items the block held before the cut."),
    Field("budget_chars", JSON_INT, note="The budget or ceiling, in characters."),
    Field("omitted_chars", JSON_INT, note="Characters left out, for a block cut by length."),
    Field(
        "sections",
        JSON_ARRAY,
        item_type=JSON_STRING,
        note="Background sections left out: background_context, older_thread_history, skills.",
    ),
)

_OMISSION_NOTES: dict[str, str] = {
    _omissions.LESSON_RULES: "Standing lessons past the rule budget.",
    _omissions.LESSON_FINDINGS: "Past findings past their budget, or withheld as off-topic.",
    _omissions.PREFERENCE_FACTS: "pref.* memory rows past the startup budget.",
    _omissions.PREFERENCES_FILE: "Preferences text past the protected-content ceiling.",
    _omissions.BACKGROUND: "Background sections the admission pool had no room for.",
    _omissions.MEMBER_BRIEFING: "A member briefing cut at its character cap.",
    _omissions.FOLDER_STEERING: "Folder steering documents a collection or size ceiling left out.",
}
_OMISSION_KINDS: tuple[tuple[str, str], ...] = tuple(
    (kind, _OMISSION_NOTES[kind]) for kind in _omissions.KINDS
)

#: This family's declarations, in the order the registry renders them.
TYPES: tuple[EntryType, ...] = (
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
            Field(
                "phase",
                JSON_STRING,
                note=(
                    "session_start or per_turn. Which POPULATION this composition "
                    "belongs to: the one-off session-start injection is many times "
                    "the size of a per-turn one, so a reader that pools them "
                    "describes neither. Absent on a log written before it was "
                    "recorded, and absent rather than guessed when the composer "
                    "does not state it."
                ),
            ),
            Field(
                "omitted",
                JSON_OBJECT,
                fields=tuple(
                    Field(kind, JSON_OBJECT, fields=_OMISSION_MEMBERS, note=note)
                    for kind, note in _OMISSION_KINDS
                ),
                note=(
                    "What the composer's budgets cut, by block: counts, budgets and a "
                    "reason, never the omitted content. Written only when something "
                    "was cut, so an absent field means nothing was omitted."
                ),
            ),
        ),
    ),
    EntryType(
        "context/measured",
        "What the agent engine measured of a turn: its last request by category, and TTFT.",
        (
            _turn(),
            Field("turn_id", JSON_STRING, note="The engine's turn id; repeats on a duplicate."),
            Field(
                "breakdown",
                JSON_OBJECT,
                fields=(
                    Field("schema", JSON_INT, required=True, note="The engine's schema number."),
                    Field(
                        "categories",
                        JSON_OBJECT,
                        required=True,
                        note=(
                            "Category name to characters, for the turn's LAST model "
                            "request. Every category the engine sent is kept, including "
                            "ones this build does not know."
                        ),
                    ),
                    Field("model_calls", JSON_INT, note="Model requests the turn made."),
                    Field("total_chars", JSON_INT, note="Characters in the last request."),
                    Field("compacted", JSON_BOOL, note="True when a compaction ran mid-turn."),
                    Field(
                        "history",
                        JSON_OBJECT,
                        fields=tuple(
                            Field(name, JSON_INT, note=note)
                            for name, note in (
                                ("user", "User-role text, every turn still in context."),
                                ("assistant", "Assistant text."),
                                ("thinking", "Reasoning text."),
                                ("compaction_summary", "The compaction summary."),
                            )
                        ),
                        note="The history category's own split.",
                    ),
                ),
                note="Absent when the engine sent no breakdown for the turn.",
            ),
            Field(
                "ttft",
                JSON_OBJECT,
                fields=(
                    Field("total_ms", JSON_INT, required=True, note="Turn start to first output."),
                    Field("setup_ms", JSON_INT, note="Turn start to first model request."),
                    Field("model_ms", JSON_INT, note="First request to first model content."),
                ),
                note="Absent when the turn showed no text or reasoning.",
            ),
        ),
        note=(
            "Characters, never tokens. Written only when breakdown or ttft is present, "
            "just before the turn's turn/completed."
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
                enum=("completed", "failed", "cancelled", "canceled", "refused", "unknown"),
                note=(
                    "Outcome. Open: the frame's own status is passed through, so a "
                    "value outside this list is possible. The listed terminal words "
                    "are acp.types.TERMINAL_TOOL_STATUSES; unknown is written by the "
                    "turn-end sweep and by crash-repair."
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
            "and recorded_class then refuses, so an undeclared plan/updated would make "
            "the class record unreadable for every session whose agent touched its "
            "task list."
        ),
    ),
)
