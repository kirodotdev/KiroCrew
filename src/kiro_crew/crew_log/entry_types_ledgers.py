"""Crew log declarations for state a session's log keeps beside the conversation.

The ``ledger/``, ``object/``, ``radar/`` and ``work/`` types: three ledgers a fold
rebuilds from the log (the session ledger, the Issue Radar crew ledger and the conductor
work board), and an outside object's state as a named producer observed it. The Issue
Radar vocabulary and the object producers are declared here, beside the types that clamp
to them; the session ledger's event kinds and the work board's vocabularies are imported
from their writers' modules, :mod:`kiro_crew.session_ledger` and
:mod:`kiro_crew.work_vocab`.

:mod:`kiro_crew.crew_log.entry_types` composes these declarations into the session table
and re-exports this module's public names other than ``TYPES``; the rules every
declaration follows are in that module's docstring.
"""

from __future__ import annotations

from kiro_crew.crew_log.entry_types_fields import (
    JSON_ARRAY,
    JSON_BOOL,
    JSON_FLOAT,
    JSON_INT,
    JSON_OBJECT,
    JSON_STRING,
    EntryType,
    Field,
)

# The ledger subsystem owns the event vocabulary its own writer clamps to, so the
# declaration below reads it from there instead of restating it. Importing the
# producer is what this module already does for every other type -- the difference
# is only that this producer's vocabulary is a named constant. The import is safe
# in this direction: ``session_ledger`` reaches the crew log lazily, inside the
# functions that need it, so nothing here pulls the storage package onto the
# gateway's boot path.
from kiro_crew.session_ledger import EVENT_KINDS as _LEDGER_EVENT_KINDS
from kiro_crew.work_vocab import (
    WORK_ACTIONS,
    WORK_ACTORS,
    WORK_BLOCKED_REASONS,
    WORK_EVENT_KINDS,
    WORK_ITEM_STATES,
    WORK_VERDICTS,
    WORK_WORKER_STATUSES,
)

#: The ledger's event kinds, in a stable order for the reference tables. Derived
#: from the writer's own set so the two cannot drift.
_EVENT_KIND_VALUES: tuple[str, ...] = tuple(sorted(_LEDGER_EVENT_KINDS))
#: The Issue Radar crew ledger's entry type, and the closed vocabularies its
#: fields clamp to. DECLARED HERE, in the registry, and imported by the app that
#: writes them: the crew log is core and the app depends on core, so the direction
#: an app-owned copy would need (core importing an app module to learn what
#: ``phase`` may hold) is the wrong one. The app re-exports these under its own
#: names so its callers and the fold in ``projection`` read one set of values.
RADAR_ENTRY_TYPE = "radar/recorded"

#: The conductor work board's entry type, named beside the radar one and for the same
#: reason: the fold in ``projection`` declares which entry types can move it, and a
#: set that is narrower than the truth drops a real change with nothing raised -- so
#: the type the fold matches on and the type the registry declares are one value.
WORK_ENTRY_TYPE = "work/recorded"

#: One worker charge relayed into the board's own log (see the type's note below).
WORK_CHARGE_ENTRY_TYPE = "work/charged"

#: Work-item phases. Two classifications hang off this enum and do not coincide:
#: the TTL-active phases age toward the claim TTL, and the editing phases are the
#: ones a crew may hold at most ONE item in. Neither can be collapsed into a bool
#: on the record, which is why both sets are named beside the enum.
RADAR_PHASES: tuple[str, ...] = (
    "selected",
    "claimed",
    "investigating",
    "implementing",
    "awaiting-ci",
    "addressing-review",
    "awaiting-merge",
    "awaiting-reply",
    "resolved",
    "skipped",
    "yielded",
    "handed-back",
    "preempted",
)
RADAR_TERMINAL_PHASES: frozenset[str] = frozenset(
    {"resolved", "skipped", "yielded", "handed-back", "preempted"}
)
RADAR_TTL_ACTIVE_PHASES: frozenset[str] = frozenset({"claimed", "investigating", "implementing"})
RADAR_EDITING_PHASES: frozenset[str] = frozenset({"implementing", "addressing-review"})

#: Progress-line kinds. ``sweep`` is the one kind that belongs to no issue: it
#: records that the crew looked at the queue and took nothing, so it is the only
#: kind an entry without ``number`` may carry, and it never carries one.
RADAR_EVENT_KINDS: tuple[str, ...] = (
    "claim",
    "investigate",
    "reply",
    "implement",
    "ci",
    "review",
    "conflict",
    "merge",
    "handback",
    "skip",
    "yield",
    "sweep",
)
RADAR_CREW_LEVEL_EVENT_KIND = "sweep"

#: Why an issue was passed over. Closed so a crew can calibrate against the
#: recent passes and a human can see whether they cluster; an unrecognised value
#: is coerced to ``other`` by the writer before the entry is built.
RADAR_SKIP_SCOPES: tuple[str, ...] = (
    "architecture",
    "new-feature",
    "needs-design",
    "needs-decision",
    "needs-investigation",
    "duplicate",
    "already-fixed",
    "not-reproducible",
    "wrong-root-cause",
    "breaking-change",
    "gate-config",
    "other",
)
RADAR_DEFAULT_SKIP_SCOPE = "other"

#: Work-item fields an update may CLEAR by name. An explicit ``null`` in a record
#: call means "empty this field", and a typed field cannot carry a null, so the
#: writer lists the cleared names here instead; the fold empties each one.
RADAR_CLEARABLE_FIELDS: tuple[str, ...] = (
    "decision",
    "why",
    "next",
    "worktree",
    "branch",
    "base_sha",
    "pr_number",
    "claim_comment_id",
    "ci_state",
    "labels_applied",
    "outcome",
)

#: The members a CI reading carries. The fold keeps these and NO other key, so a
#: reading merged into an item key by key cannot grow the item by key; the route
#: assembles exactly these from the record tool's flat ``ci_*`` arguments.
RADAR_CI_KEYS: tuple[str, ...] = ("state", "passed", "total", "round", "inherited_reds")

#: Each CI member's type and ceiling -- the record tool's own bounds on its ``ci_*``
#: arguments (``validation_parts/app_schemas.py``), restated here so the fold re-applies them to the
#: bytes it reads and the carry applies them to a pre-projection file: a string
#: verdict clipped to its length, a counter kept only as a non-negative int within
#: the tool's range. A test pins this table against the tool's field specs.
RADAR_CI_BOUNDS: dict[str, tuple[type, int]] = {
    "state": (str, 32),
    "passed": (int, 100_000),
    "total": (int, 100_000),
    "round": (int, 1_000),
    "inherited_reds": (int, 100_000),
}

#: The most labels an item retains -- the record tool's own ``max_items`` on
#: ``labels_applied``, re-applied by the fold to the bytes it reads.
RADAR_LABELS_LIMIT = 20

#: Each retained numeric field's inclusive range -- again the record tool's own
#: ``min_val``/``max_val``, restated so the fold bounds the MAGNITUDE of a number it
#: reads off a file, not only its type. Without this a single crafted or damaged line
#: carrying a thousand-digit ``number`` is retained verbatim, and an item or skip row
#: keyed on ``str(number)`` then carries those digits into every checkpoint and
#: response for as long as the row survives. A test pins this table against the tool's
#: field specs.
RADAR_NUMBER_BOUNDS: dict[str, tuple[int, int]] = {
    "number": (1, 1_000_000_000),
    "pr_number": (1, 1_000_000_000),
    "claim_comment_id": (1, 10**18),
}

#: Who may write an ``object/observed`` entry. CLOSED, and closed on purpose: the
#: value is what lets a reader tell a measured record from anything an agent typed,
#: so the emitter REFUSES a value outside this tuple rather than coercing it -- a
#: coerced producer would be a record attributed to a mechanism that did not make
#: it. ``probe`` is the structured monitor's provider probe. A second producer (a
#: recogniser on the tool-result path, say) is added here, in one commit with the
#: site that writes it, or not at all.
OBJECT_PRODUCER_PROBE = "probe"
OBJECT_PRODUCERS: tuple[str, ...] = (OBJECT_PRODUCER_PROBE,)

#: The conductor work board's vocabularies live in :mod:`kiro_crew.work_vocab`, a
#: pure-data leaf outside this package, so the type declared below, the store and
#: the tool schemas clamp to ONE set without the boot path loading this module.
TYPES: tuple[EntryType, ...] = (
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
            "ledger therefore DEPENDS on this log: a gateway started with "
            "``KIROCREW_CREW_LOG=0`` records none, and the tool refuses rather than "
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
    # -- radar (Issue Radar crew ledger) ------------------------------------ #
    EntryType(
        RADAR_ENTRY_TYPE,
        "One Issue Radar crew-ledger update: the work-item fields it set, and the event explaining them.",
        (
            Field("crew_id", JSON_STRING, required=True, note="The crew this update belongs to."),
            Field("owner", JSON_STRING, required=True, note="Repository owner the crew works in."),
            Field("repo", JSON_STRING, required=True, note="Repository name the crew works in."),
            Field(
                "number",
                JSON_INT,
                note=(
                    "The issue this update is about. ABSENT on a crew-level step (a queue "
                    "sweep that took nothing), which is the only kind of entry that patches "
                    "no work item."
                ),
            ),
            Field(
                "phase",
                JSON_STRING,
                enum=RADAR_PHASES,
                enum_closed=True,
                note=(
                    "The item's new phase. Never written without event and event_kind, which "
                    "is what makes the phase-requires-a-reason rule a property of ONE entry."
                ),
            ),
            Field("outcome", JSON_STRING, note="Terminal outcome; an empty string clears it."),
            Field("decision", JSON_STRING, note="What the crew decided to do."),
            Field("why", JSON_STRING, note="On what grounds."),
            Field("next", JSON_STRING, note="The resumable intent -- the concrete next step."),
            Field(
                "tried",
                JSON_OBJECT,
                fields=(
                    Field("approach", JSON_STRING, required=True, note="What was tried."),
                    Field("rejected_because", JSON_STRING, note="Why it was rejected."),
                ),
                note="One rejected approach, appended to the item's list.",
            ),
            Field("worktree", JSON_STRING, note="Local only; never echoed into a comment."),
            Field("branch", JSON_STRING, note="Local only."),
            Field("base_sha", JSON_STRING, note="Local only."),
            Field("pr_number", JSON_INT, note="The pull request this item opened."),
            Field(
                "ci_state",
                JSON_OBJECT,
                note=(
                    "CI reading merged into the item's ci_state map, key by key. Members "
                    "are state, passed, total, round, inherited_reds; the fold keeps no "
                    "other key."
                ),
            ),
            Field("claim_comment_id", JSON_INT, note="Which forge comment carries the claim."),
            Field(
                "labels_applied",
                JSON_ARRAY,
                item_type=JSON_STRING,
                note="Labels this crew put on the issue, replaced whole.",
            ),
            Field(
                "clear",
                JSON_ARRAY,
                item_type=JSON_STRING,
                enum=RADAR_CLEARABLE_FIELDS,
                note=(
                    "Work-item fields this update EMPTIES, by name. The way an explicit "
                    "null in a record call is carried: a typed field cannot hold one, so "
                    "the writer names the cleared fields here and the fold empties them "
                    "before applying the fields the same update sets."
                ),
            ),
            Field(
                "skip",
                JSON_OBJECT,
                fields=(
                    Field(
                        "reason", JSON_STRING, required=True, note="Why the issue was passed over."
                    ),
                    Field(
                        "scope",
                        JSON_STRING,
                        required=True,
                        enum=RADAR_SKIP_SCOPES,
                        enum_closed=True,
                        note="Closed vocabulary; the writer coerces an unknown scope to other.",
                    ),
                    Field(
                        "crew_id",
                        JSON_STRING,
                        note=(
                            "The crew that decided the pass, when it is not the entry's own -- "
                            "only a carried entry sets it."
                        ),
                    ),
                    Field(
                        "decided_at",
                        JSON_STRING,
                        note="When the pass was decided, when not this entry's time -- carry only.",
                    ),
                    Field(
                        "deferred",
                        JSON_BOOL,
                        note=(
                            "True when another crew's decision on this number already stood "
                            "in the shared index as this pass was recorded. A deferred pass "
                            "never stands over the decision it saw, whatever the clocks say: "
                            "the writer's own observation is the first-writer token, not a "
                            "timestamp."
                        ),
                    ),
                ),
                note=(
                    "Present when this update records a PASS on the issue. The repository's "
                    "shared skip index is a fold of these across every crew of the repository."
                ),
            ),
            Field(
                "carried",
                JSON_BOOL,
                note=(
                    "True on an entry that carries a pre-projection on-disk record forward, "
                    "once, so a crew upgraded mid-work keeps its items and the repository "
                    "keeps its passes."
                ),
            ),
            Field(
                "claimed_at",
                JSON_STRING,
                note="The carried record's own stamp; the fold stamps every other entry itself.",
            ),
            Field("last_progress_at", JSON_STRING, note="Carry only, as claimed_at."),
            Field("finished_at", JSON_STRING, note="Carry only, as claimed_at."),
            Field("event", JSON_STRING, required=True, note="The public progress line."),
            Field(
                "event_kind",
                JSON_STRING,
                required=True,
                enum=RADAR_EVENT_KINDS,
                enum_closed=True,
                note=(
                    "Which kind of step this records. sweep is the one crew-level kind and "
                    "the only one an entry without number may carry."
                ),
            ),
        ),
        note=(
            "One entry per issue_radar_crew_record call, carrying only the fields that call "
            "set -- an omitted field means 'unchanged'. A phase change carries its event in "
            "the SAME entry, and a pass carries its skip row in the same entry as the phase "
            "that records it, so no reader can observe a phase that moved without its reason "
            "or an issue skipped without its index entry. The crew ledger DEPENDS on this log: "
            "a crew whose session has no crew log cannot record, and the tool refuses rather "
            "than keeping a document of its own."
        ),
    ),
    # -- work --------------------------------------------------------------- #
    EntryType(
        WORK_ENTRY_TYPE,
        "One work-board mutation: who acted, on which item, and the fields it set.",
        (
            Field(
                "slot",
                JSON_STRING,
                required=True,
                note=(
                    "The board's key -- the conductor slot this mutation belongs to. A "
                    "worker's report names the conductor's slot, not its own, so every "
                    "entry of one board folds under one key whichever party wrote it."
                ),
            ),
            Field(
                "actor",
                JSON_STRING,
                required=True,
                enum=WORK_ACTORS,
                enum_closed=True,
                note="Which party wrote this entry; the fields the two may set are disjoint.",
            ),
            Field(
                "by",
                JSON_STRING,
                required=True,
                note=(
                    "The acting session's slot key. Equals slot for a conductor entry and "
                    "the bound worker's key for a report, so a reader of one entry can "
                    "say who wrote it without opening the unit's header."
                ),
            ),
            Field(
                "action",
                JSON_STRING,
                required=True,
                enum=WORK_ACTIONS,
                enum_closed=True,
                note=(
                    "The one mutation this entry records. The fields below are the "
                    "ones that action set; an omitted field means 'unchanged'."
                ),
            ),
            Field(
                "item_id",
                JSON_STRING,
                note="The item acted on. Absent only for goal, the board-level header write.",
            ),
            Field("goal", JSON_STRING, note="The board's objective, when goal set one."),
            Field("round", JSON_INT, note="The board's or the item's round counter, when set."),
            Field(
                "generation",
                JSON_STRING,
                note=(
                    "An opaque id minted when the conductor record was created. A slot "
                    "reused after its board was purged mints a new one; the fold keeps "
                    "only the latest board, transitioning in log order."
                ),
            ),
            Field(
                "depth",
                JSON_INT,
                note="The board's nesting depth, carried by the first entry of a board.",
            ),
            Field(
                "parent_item",
                JSON_STRING,
                note="The parent board's item this board works, carried with depth.",
            ),
            Field("title", JSON_STRING, note="The item's title, set by create."),
            Field(
                "acceptance",
                JSON_OBJECT,
                note=(
                    "The acceptance criteria object, set by create or accept. Its members "
                    "are the caller's and are checked for shape by the writer."
                ),
            ),
            Field(
                "state",
                JSON_STRING,
                enum=WORK_ITEM_STATES,
                enum_closed=True,
                note="The item's new state, set by close.",
            ),
            Field(
                "verdict",
                JSON_STRING,
                enum=WORK_VERDICTS,
                enum_closed=True,
                note="The acceptance verdict, set by verdict.",
            ),
            Field("decision", JSON_STRING, note="The conductor's decision text, set by decide."),
            Field(
                "worker_session_key",
                JSON_STRING,
                note="The worker slot bound to the item, set by bind.",
            ),
            Field(
                "worker_instance_id",
                JSON_STRING,
                note=(
                    "The remote crew the bound worker runs on, set by a remote bind "
                    "beside worker_session_key (then the peer's slot key)."
                ),
            ),
            Field("fails", JSON_INT, note="The item's failed-verdict count, when it moved."),
            Field(
                "status",
                JSON_STRING,
                enum=WORK_WORKER_STATUSES,
                enum_closed=True,
                note="The worker's status, set by report.",
            ),
            Field(
                "reason",
                JSON_STRING,
                enum=WORK_BLOCKED_REASONS,
                enum_closed=True,
                note=(
                    "Why a blocked or question report is stuck, set by report. A report "
                    "without it clears the item's reason."
                ),
            ),
            Field("summary", JSON_STRING, note="The worker's summary, set by report."),
            Field(
                "artifacts",
                JSON_OBJECT,
                note=(
                    "String-to-string pointers replacing the item's map, set by report. "
                    "The members are the worker's own keys and are checked for shape."
                ),
            ),
            Field("pr", JSON_INT, note="The pull request number, set by report."),
            Field(
                "event_id",
                JSON_STRING,
                note="The store's content-addressed id of the event this write appended.",
            ),
            Field("event_ts", JSON_STRING, note="The store's stamp on that event."),
            Field("created_at", JSON_STRING, note="The item's committed creation stamp."),
            Field("last_report_at", JSON_STRING, note="The item's committed last-report stamp."),
            Field("closed_at", JSON_STRING, note="The item's committed close stamp."),
            Field("decided_at", JSON_STRING, note="The item's committed decide stamp."),
            Field(
                "pending_since",
                JSON_STRING,
                note=(
                    "When the item's run of pending verdicts began, carried by a pending "
                    "verdict; any other verdict, or an accept, clears it."
                ),
            ),
            Field(
                "board_round",
                JSON_INT,
                note="The board's committed round, carried by a baseline entry.",
            ),
            Field(
                "board_created_at",
                JSON_STRING,
                note="The board's committed creation stamp, carried by a baseline entry.",
            ),
            Field(
                "goal_version",
                JSON_INT,
                note="The header's goal-write count after this goal write, set by goal.",
            ),
            Field(
                "baseline",
                JSON_BOOL,
                note=(
                    "True when the entry carries the WHOLE committed item, not a delta: "
                    "written for an item the record has never held whole (one from before "
                    "the projection), so a lost file rebuilds from it."
                ),
            ),
            Field(
                "event",
                JSON_STRING,
                note="The one-line item event this mutation appends to the item's tail.",
            ),
            Field(
                "event_kind",
                JSON_STRING,
                enum=WORK_EVENT_KINDS,
                enum_closed=True,
                note="Which kind of item event this is; absent only for goal.",
            ),
            Field(
                "board_copy",
                JSON_BOOL,
                note=(
                    "True on the copy of a worker's report the gateway wrote into the "
                    "board's own log. A report without it is a worker-log copy, which the "
                    "rebuild's join of worker logs never lets undo a board copy."
                ),
            ),
            Field(
                "mirror",
                JSON_BOOL,
                note=(
                    "True on the worker's own copy of a report whose board copy landed. "
                    "The work and worktree folds skip it (the board copy is the record); "
                    "the workers fold reads it as the worker's own report."
                ),
            ),
            Field(
                "relay",
                JSON_STRING,
                note=(
                    "Set on a copy the gateway relayed up from a sub-board's log into its "
                    "parent board's log: the origin entry as '<unit>:<seq>'. A relayed "
                    "copy is relayed again only upward, so a chain cannot loop."
                ),
            ),
        ),
        note=(
            "One entry per work-ledger write, appended to the ACTING session's log and "
            "keyed by the conductor's slot. A conductor action and a worker report are "
            "the two writers, each sets only its own fields. A worker report is also "
            "written into the conductor's own log, and a sub-board's entries are relayed "
            "into its parent board's log, so the work fold rebuilds a board from the "
            "conductor's own units alone and the ledger's files are a cache of the crew "
            "log rather than a record beside it. The work ledger therefore DEPENDS on this log: with the "
            "emitter off the tools refuse rather than keeping a document of their own."
        ),
    ),
    EntryType(
        WORK_CHARGE_ENTRY_TYPE,
        "One bound worker's charge, relayed by the gateway into the board's own log.",
        (
            Field(
                "slot",
                JSON_STRING,
                required=True,
                note="The board whose log this copy lands in: the conductor slot.",
            ),
            Field(
                "by",
                JSON_STRING,
                required=True,
                note="The worker session key that incurred the charge.",
            ),
            Field(
                "source",
                JSON_STRING,
                required=True,
                enum=("turn", "subagent", "background"),
                enum_closed=True,
                note="Which kind of closer in the worker's log carried the charge.",
            ),
            Field(
                "closer",
                JSON_STRING,
                required=True,
                note=(
                    "The closer this charge copies, as '<unit>:<seq>' of the worker's log. "
                    "A reader counts one closer once however many copies it meets."
                ),
            ),
            Field("credits", JSON_FLOAT, note="What the closer billed. Absent is not zero."),
            Field("duration_ms", JSON_INT, note="How long the closer's work ran."),
        ),
        note=(
            "Written when a turn, subagent or background closer lands in a session bound "
            "to a work item, into the newest unit of the board that bound it; a board "
            "bound to a parent board relays it up again. The project-report fold costs "
            "tasks from these, so it reads only the board's own units."
        ),
    ),
)
