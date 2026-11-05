"""Crew log declarations for work a session hands off outside its own turns.

The ``subagent/`` and ``background/`` types: a child dispatched, steered, cleared from
the panel, finished or closed without finishing, and a model call the gateway made on
the session's behalf.

:mod:`kiro_crew.crew_log.entry_types` composes these declarations into the session table;
the rules every declaration follows are in that module's docstring.
"""

from __future__ import annotations

from kiro_crew.crew_log.entry_types_fields import (
    JSON_BOOL,
    JSON_FLOAT,
    JSON_INT,
    JSON_OBJECT,
    JSON_STRING,
    EntryType,
    Field,
)

#: This family's declarations, in the order the registry renders them.
TYPES: tuple[EntryType, ...] = (
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
            Field(
                "attempt",
                JSON_INT,
                note=(
                    "Which try at turn asked, matching turn/started's attempt, so a child "
                    "of a regenerated or rewound turn names its own try. Written only "
                    "beside turn. ABSENT means not observed, which is not attempt 1: the "
                    "dispatch can be pinned before the turn's attempt is derived, and "
                    "every log written before the field omits it."
                ),
            ),
            Field("agent", JSON_STRING, note="The child's agent name, when one was resolved."),
            Field("model", JSON_STRING, note="The child's model, when one was resolved."),
            Field(
                "task",
                JSON_STRING,
                note=(
                    "What the child was asked to do, redacted and clipped on the same "
                    "terms as plan/updated's item text. ABSENT when the dispatch carried "
                    "no task text, and absent on every log written before the field, "
                    "which a reader must not read as a dispatch that asked for nothing -- "
                    "a surface draws no task line for it rather than an empty one."
                ),
            ),
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
        "subagent/dismissed",
        "The user cleared a finished child's card from the panel.",
        (Field("agent_id", JSON_STRING, required=True, note="The child's run id."),),
        note=(
            "A fact about the SESSION, which is why it is here rather than in a store "
            "beside the log: the panel's durable half is a fold of this log, so a "
            "dismissal kept anywhere else is a second record that has to be held in step "
            "with it -- and when the other store was reclaimed first, the card came back. "
            "Opens and closes nothing: a dismissal is not an ending, and a dismissed "
            "child keeps whatever outcome its own closer recorded. It may arrive before "
            "any closer, for a child the user cleared while it was still running."
        ),
    ),
    EntryType(
        "subagent/completed",
        "A child closed having finished its work.",
        (
            Field("agent_id", JSON_STRING, required=True, note="The child's run id."),
            Field("ms", JSON_INT, note="Measured run duration; absent when it was not measured."),
            Field(
                "credits",
                JSON_FLOAT,
                note=(
                    "What the child billed, cumulative across every attempted turn "
                    "including billed retries that failed before the last one. Present "
                    "only when this provider billed credits: a provider that does not "
                    "reports zero through the shared TurnUsage contract, which is "
                    "indistinguishable here from a run that was genuinely free, so the "
                    "zero is dropped rather than written -- the same posture "
                    "background/completed takes."
                ),
            ),
        ),
        note=(
            "Only the completed outcome. A stopped or failed child closes through "
            "subagent/failed, because the runtime's three-way outcome exists precisely to "
            "stop consumers reading 'no error' as success. No tokens: the subagent runtime "
            "measures none, and writing zeros would present the absence of a measurement "
            "as a measurement of zero. Credits ARE measured, and a zero or unbilled one is "
            "not written for the same reason."
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
            Field(
                "credits",
                JSON_FLOAT,
                note=(
                    "What the child billed before it stopped, cumulative across every "
                    "attempted turn. A run that did not finish still spent, so this is "
                    "where that charge would otherwise be lost. Absent when unbilled or "
                    "zero, and on the crash-repair closer, which measured nothing."
                ),
            ),
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
                enum=("title", "summary", "memory_consolidation", "dynamic_card"),
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
)
