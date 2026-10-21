"""Crew log declarations for a crewmate's panel and dashboard, and a conversation's cards.

The ``panel/``, ``dashboard/``, ``card/`` and ``guide/`` types: a crew's
published webview, an accepted change to a crewmate's dynamic dashboard, an agentic value
it wrote and a refused write its mistake book reads back, the change cards and guides the
agent offers in a conversation, with how each one ended, and the crewmate's own goal
record and "waiting on you" list as the gateway records them for its Dashboard page. The entry types, read ceilings
and fold names the panel and dashboard folds share sit beside them, as do the statuses
a card or a guide can finish with.

:mod:`kiro_crew.crew_log.entry_types` composes these declarations into the session table
and re-exports this module's public names other than ``TYPES``; the rules every
declaration follows are in that module's docstring.
"""

from __future__ import annotations

from kiro_crew.crew_log.entry_types_fields import (
    JSON_ARRAY,
    JSON_BOOL,
    JSON_INT,
    JSON_OBJECT,
    JSON_STRING,
    EntryType,
    Field,
)

#: The crew webview's entry type, named here for the reason ``RADAR_ENTRY_TYPE`` and
#: ``WORK_ENTRY_TYPE`` are: the fold in ``projection`` matches on this type, and a
#: type the fold does not match drops a real publish with nothing raised -- so the
#: matched value and the declared one are one constant. The panel store imports it
#: rather than restating it.
PANEL_ENTRY_TYPE = "panel/published"

#: Ceilings the panel fold RE-APPLIES to the bytes it reads. The writer clamps too,
#: but these come off a file a reader does not control, so a planted or damaged line
#: is exactly the input that ignores the writer's rule. Equal to the store's own caps
#: by construction -- ``test_agent_panel_crew_log`` pins them against
#: ``agent_panel`` -- because a lower ceiling here would truncate an ordinary
#: accepted panel on every read, which is silent corruption rather than a bound.
PANEL_TITLE_LIMIT = 200
PANEL_TEMPLATE_LIMIT = 64
#: A SHA-256 hexdigest, which is what ``agent_panel.crew_key`` produces and the read
#: route compares on. Clamped like every other field the fold reads, so a planted
#: line cannot carry an unbounded string into a state a reader retains; the clamp is
#: the digest's own exact width, so no real key is ever shortened by it.
PANEL_CREW_KEY_LIMIT = 64
#: Past publishes the fold keeps, newest last. A HISTORY, not the value: each row is
#: the title and template of a superseded panel, so an operator can see that a crew
#: is publishing and what it called each cycle without the fold retaining every
#: payload it ever held -- which is what the byte ceiling per entry bounds, and what
#: retaining N payloads would multiply.
PANEL_HISTORY_LIMIT = 50
#: Distinct OWNERS one slot's panel fold retains a record for, newest publish wins
#: within each. More than one is possible because a slot is keyed by the member slug
#: and two crews can resolve to one slug (memory provisioning suffixes a persisted
#: ``member_id``, so a name-derived slug can be held by a crew of another name). Each
#: crew then reads the record its OWN ownership digest keys, instead of a newer
#: crew's publish hiding an older one's panel from its own drawer.
#:
#: Small because the number is not a scale: one slug is one member, and a collision
#: is a degraded roster rather than a mode of use. The cap is what keeps the retained
#: state bounded -- each record holds a capped payload, so this multiplies it -- and
#: the oldest publish is evicted when a further owner appears.
PANEL_OWNER_LIMIT = 4
PANEL_FOLD_NAME = "panel"

#: The dynamic dashboard instance's entry type and its closed action vocabulary, named
#: here for the reason the panel's are: the instance store matches on this type when it
#: folds the history, and a type the fold does not match drops a real change with
#: nothing raised -- so the matched value and the declared one are one constant. The
#: store imports both rather than restating them, which is also what lets ``action`` be
#: declared as a CLOSED enum: the writer clamps to exactly this tuple.
DASHBOARD_INSTANCE_ENTRY_TYPE = "dashboard/instance_changed"
DASHBOARD_INSTANCE_ACTIONS: tuple[str, ...] = ("adopted", "edited", "rolled_back")

# -- the dynamic dashboard: agentic values and the mistake book ------------- #

#: The entry a crewmate's own dashboard value lands as.
#:
#: Named here for the reason every type above it is: the fold in ``projection``
#: matches on this value, so a type the fold does not match drops a real write
#: with nothing raised. One constant, matched and declared in one place.
#:
#: A dashboard field is either read from a fold or written by the agent
#: (``{"agentic": true}`` in the template manifest). This is the write path for
#: the second kind: no host Python computes a dashboard value, so a number the
#: log does not already record has to arrive as an entry like any other fact.
DASHBOARD_AGENTIC_ENTRY_TYPE = "dashboard/agentic_value"

#: The entry a REFUSED agentic write lands as -- the mistake book's only input.
#:
#: A write is refused when the manifest does not declare the field, when the
#: field is not agentic, or when the value is not of the declared type. That is a
#: good error exactly once: an agent reaching the same wrong field on its next
#: cycle, from a fresh context, pays the same round trip again and nothing on the
#: gateway remembers that the guess was already answered. This entry is that
#: memory, and the ``mistakes`` fold is how it is read back.
DASHBOARD_REFUSED_ENTRY_TYPE = "dashboard/agentic_refused"

#: Ceilings the two dashboard folds RE-APPLY to the bytes they read, for the
#: reason the panel limits above exist: these come off a file the reader does not
#: control, so a planted or damaged line is exactly the input that ignores the
#: writer's clamp.
DASHBOARD_FIELD_LIMIT = 64
DASHBOARD_TYPE_LIMIT = 16
DASHBOARD_CODE_LIMIT = 64
DASHBOARD_REASON_LIMIT = 240

#: Bytes one agentic VALUE may occupy once serialized.
#:
#: A dashboard field is a cell -- a count, a phrase, a short series a chart draws
#: -- so this is generous for every real one and far under the entry ceiling. It
#: exists because a field's type may be ``array`` or ``object``, and those have no
#: natural width: without it one field could fill the log line and take the other
#: twenty-three fields down with it.
DASHBOARD_VALUE_BYTES = 8 * 1024

#: Distinct field names one slot's agentic fold retains a value for.
#:
#: The manifest's own cap is 24 fields (``manifest.MAX_FIELDS``) and this is wider
#: on purpose: a crewmate that edits its copy keeps values written under the
#: PREVIOUS field set, and evicting those the moment a field is renamed would
#: throw away a value the next edit may restore. Eviction is least-recently
#: written, so what survives is what the crewmate is still writing.
DASHBOARD_VALUE_LIMIT = 64

DASHBOARD_FOLD_NAME = "agentic"

#: Distinct ``(code, field)`` groups the mistake book retains.
#:
#: A BOUND on a fold that grows with agent error, which is the one input nobody
#: controls: a wedged agent writing a generated field name per cycle would
#: otherwise put an unbounded set of groups into retained state. Fifty is far more
#: than a real crewmate produces -- a manifest declares at most 24 fields, so a
#: crewmate cannot be wrong in many more ways than that plus a few typos -- and
#: eviction is least-recently-seen, so the groups kept are the mistakes still
#: being made.
MISTAKE_GROUP_LIMIT = 50

#: Corrections one mistake group remembers.
#:
#: Small because the useful answer is the LATEST one: a group's whole job is to
#: say "you reached this wrongly N times; here is what worked". A long list of
#: past corrections would make the reader choose, which is the problem this fold
#: exists to remove.
MISTAKE_CORRECTION_LIMIT = 3

MISTAKES_FOLD_NAME = "mistakes"

# -- a crewmate's own record, as folds its Dashboard page reads ---------- #

#: The entry a crewmate's goal record lands as each time it changes: the goals, and
#: each milestone with its target, its latest reading and whether that reading meets
#: the target. A SNAPSHOT, so the fold keeps the newest whole and a page bound to it
#: moves the moment the record does, with no value typed by the crewmate.
GOAL_ENTRY_TYPE = "dashboard/goal_recorded"
GOAL_FOLD_NAME = "goal"
#: The entry a crewmate's "waiting on you" list lands as each time it changes: its
#: open ``[OPTIONS]`` questions, ``ask_question`` cards and pending change cards.
#: A snapshot too, so a reply or a card press empties the fold by the next entry.
WAITING_ENTRY_TYPE = "dashboard/waiting_changed"
WAITING_FOLD_NAME = "waiting"
#: Milestones or goals the goal fold keeps, items the waiting fold keeps, choices one
#: waiting item keeps, and characters per text cell: re-applied on read, and sized so a
#: snapshot at every clamp still fits one entry.
CREWMATE_ROW_LIMIT = 12
CREWMATE_WAITING_LIMIT = 6
CREWMATE_OPTION_LIMIT = 6
CREWMATE_TEXT_LIMIT = 240


#: The ``code`` a CORRECTION entry carries.
#:
#: A correction is the other shape on ``dashboard/agentic_refused``: it says an
#: accepted write used a field an earlier refusal had reached wrongly. It needs a
#: code of its own because ``code`` is REQUIRED on the type -- a line that cannot
#: say what it is gets refused at the append, which is the guard working, and was
#: how an earlier draft of this entry (a correction with no code) was caught by
#: the validator rather than by a test. Branching on this value also beats
#: branching on the presence of ``corrects``: the one key that is always there is
#: the one worth deciding on.
MISTAKE_CORRECTED_CODE = "corrected"

#: What a change card can END as -- the card store's finished statuses. CLOSED: the
#: store is the only writer and the emitter refuses anything else, so an entry
#: naming another value is a bug at the call site rather than a vocabulary that grew.
#: ``failed`` is finished but retryable, so a later ``applied`` can follow it.
CARD_FINISHED_STATUSES: tuple[str, ...] = (
    "applied",
    "partial",
    "failed",
    "cancelled",
    "expired",
    "undone",
)

#: What a guide can END as, closed for the same reason.
GUIDE_FINISHED_STATUSES: tuple[str, ...] = ("completed", "cancelled", "expired")
#: Why a guide ended, when that is more than its status says. Closed.
GUIDE_FINISHED_REASONS: tuple[str, ...] = ("saved_without_guide",)


def _conversation_row(what: str) -> Field:
    return Field(
        "mid",
        JSON_STRING,
        note=(
            f"Id of the transcript row the {what} is drawn at. The row and this entry are "
            "the same fact in the two records; the id joins them. Absent when the "
            "conversation had no live window to write the row into."
        ),
    )


#: This family's declarations, in the order the registry renders them.
TYPES: tuple[EntryType, ...] = (
    # -- panel -------------------------------------------------------------- #
    EntryType(
        PANEL_ENTRY_TYPE,
        "One publish of a crew's own webview: the data, and the template that renders it.",
        (
            Field(
                "template",
                JSON_STRING,
                required=True,
                note=(
                    "Id of the human-authored template the data is rendered with. Required "
                    "because the pair is what renders: a payload naming no template has no "
                    "layout to fill, and the store resolves the id at publish so an "
                    "unknown one is refused before it reaches a line."
                ),
            ),
            Field(
                "data",
                JSON_OBJECT,
                required=True,
                note=(
                    "The state the crew published, already scrubbed and depth-checked. The "
                    "MEMBERS are the crew's own field names -- a panel is deliberately "
                    "generic, so nothing here knows what they mean -- which is why they are "
                    "undeclared and bounded by byte and depth ceilings instead."
                ),
            ),
            Field(
                "title",
                JSON_STRING,
                note="Short name for this panel, shown in the page's picker.",
            ),
            Field(
                "crew",
                JSON_STRING,
                note=(
                    "The publishing crew's name as DISPLAY text -- redacted, so it is not "
                    "an identity. Carried so a reader of one entry can say whose panel it "
                    "is without resolving the slot."
                ),
            ),
            Field(
                "crew_key",
                JSON_STRING,
                note=(
                    "Digest of the crew's EXACT name, which is what ownership is decided "
                    "on. Separate from crew because redaction is many-to-one: a "
                    "credential-shaped name redacts to a string that matches no exact name, "
                    "so display text cannot serve as an identity."
                ),
            ),
        ),
        note=(
            "One entry per publish, appended to the publishing member's own DM session log "
            "-- the only session the panel tool is ever mounted on -- so the slot a panel "
            "folds under is the member's. Two crew names can resolve to one slot, so the "
            "fold keys a record per crew_key and answers each crew with its own. Each "
            "publish REPLACES the panel, so the fold takes the newest entry whole and keeps "
            "the earlier ones only as a short history; a partial update has no meaning here, "
            "unlike the ledger's. This log is NOT the panel's only home: the publish writes "
            "crew-panels/<slug>.json first and that file is the durable record, so this "
            "append is best-effort history and a publish with the emitter off still succeeds."
        ),
    ),
    # -- dashboard ----------------------------------------------------------- #
    EntryType(
        DASHBOARD_INSTANCE_ENTRY_TYPE,
        "One accepted change to a crewmate's own dynamic dashboard.",
        (
            Field(
                "slug",
                JSON_STRING,
                required=True,
                note=(
                    "Whose dashboard changed. Carried on the entry because two crew names "
                    "can resolve to one DM slot, so a reader of one entry must be able to "
                    "say which crewmate's instance this is without resolving the slot."
                ),
            ),
            Field(
                "instance_version",
                JSON_INT,
                required=True,
                note=(
                    "The instance's own counter AFTER this change. Never the template's "
                    "version and never a data sequence: a dashboard carries three versions "
                    "and conflating them is what makes a cached page wrong."
                ),
            ),
            Field(
                "action",
                JSON_STRING,
                required=True,
                enum=DASHBOARD_INSTANCE_ACTIONS,
                enum_closed=True,
                note=(
                    "What the change was. CLOSED because the instance store clamps it: "
                    "the vocabulary is declared beside the type and the writer imports it, "
                    "so no caller can produce a fourth value."
                ),
            ),
            Field(
                "template_id",
                JSON_STRING,
                required=True,
                note="Id of the template this instance was COPIED from.",
            ),
            Field(
                "template_version",
                JSON_INT,
                required=True,
                note=(
                    "The template version that was copied, frozen at adopt. An edit does "
                    "not move it: editing a copy does not make the crewmate the template's "
                    "author."
                ),
            ),
            Field(
                "from_version",
                JSON_INT,
                note=(
                    "The instance version this change was derived from -- the edited one, or "
                    "the one a rollback restored. Absent for an adopt, which derives from a "
                    "template rather than from a version."
                ),
            ),
            Field(
                "fields",
                JSON_INT,
                note="How many fields the copied manifest declares after this change.",
            ),
            Field(
                "html_bytes",
                JSON_INT,
                note="Size of the page this version holds, in bytes.",
            ),
            Field("at_ms", JSON_INT, note="When the change was accepted, epoch milliseconds."),
        ),
        note=(
            "One entry per accepted change, appended to the crewmate's own DM session log, "
            "and the instance's history is a fold over them. The entry carries what CHANGED "
            "and never the page: the page is kept per version under the member's own space, "
            "so this entry is bounded by construction and can never be refused for size. "
            "The record of the current value is that file rather than this log -- so a "
            "change with the emitter off still succeeds, and this append is history."
        ),
    ),
    # -- the dynamic dashboard ----------------------------------------------- #
    EntryType(
        DASHBOARD_AGENTIC_ENTRY_TYPE,
        "One agentic dashboard value the crewmate wrote, already type-checked.",
        (
            Field(
                "field",
                JSON_STRING,
                required=True,
                note=(
                    "The manifest field name this value fills. Required because the "
                    "value means nothing without it: a dashboard cell is identified by "
                    "its field, and the fold keys on this."
                ),
            ),
            Field(
                "type",
                JSON_STRING,
                required=True,
                note=(
                    "The type the manifest DECLARED for the field, carried so a reader "
                    "can tell a value that still matches its declaration from one left "
                    "behind by an edit that changed the type. The writer checked the "
                    "value against it; the fold re-checks, because these are bytes off a "
                    "file the reader does not control."
                ),
            ),
            Field(
                "value",
                JSON_OBJECT,
                required=True,
                note=(
                    "A WRAPPER holding the value under the single key v. Wrapped because "
                    "a dashboard value may be any of the manifest's five types and this "
                    "registry has no any-type: an undeclared field is refused by the "
                    "append validator, and a declared OBJECT is the only shape that can "
                    "carry all five, since an object's members are deliberately "
                    "unchecked (the same posture ledger/recorded's artifacts takes). The "
                    "sibling type key says which of the five v holds, and both the "
                    "writer and the fold check v against it."
                ),
            ),
            Field(
                "instance_version",
                JSON_INT,
                note=(
                    "The dashboard instance version this value was written against. "
                    "Carried so a reader can see a value written for a FIELD SET the "
                    "instance has since edited away, which is the one way a type-checked "
                    "value can still be wrong by the time it is read."
                ),
            ),
            Field(
                "crew_key",
                JSON_STRING,
                note=(
                    "Digest of the writing crew's EXACT name, for the reason the panel "
                    "entry carries one: a slot can hold two crews, and one crew's values "
                    "must not fill the other's dashboard."
                ),
            ),
        ),
        note=(
            "One entry per accepted agentic write, appended to the crewmate's own DM "
            "session log and folded under its slot -- the same slot the panel entry folds "
            "under, so a crewmate's dashboard, its values and its mistake book all answer "
            "for one crewmate. Each write replaces that one FIELD and leaves the others "
            "alone, unlike the panel entry's whole-document replacement: the fields are "
            "independent cells, and a crewmate that learns one number should not have to "
            "restate the rest to report it."
        ),
    ),
    EntryType(
        DASHBOARD_REFUSED_ENTRY_TYPE,
        "One REFUSED agentic write: what was refused, on which field, and why.",
        (
            Field(
                "code",
                JSON_STRING,
                required=True,
                note=(
                    "The refusal's machine-readable reason -- unknown_field, "
                    "field_not_agentic, wrong_type, value_too_large, no_instance. "
                    "Required because it is what the mistake book groups on: an entry "
                    "that cannot say WHY it was refused teaches nothing."
                ),
            ),
            Field(
                "field",
                JSON_STRING,
                note=(
                    "The field the write named, which is the teachable half. A group "
                    "keyed on (code, field) is what lets the fold say 'you have written "
                    "credits_total three times and the field is credits'. Absent for a "
                    "refusal about no field in particular."
                ),
            ),
            Field(
                "reason",
                JSON_STRING,
                note=(
                    "The sentence the agent was given, kept so the mistake book can hand "
                    "back the same correction rather than a second wording of it."
                ),
            ),
            Field(
                "corrects",
                JSON_ARRAY,
                item_type=JSON_STRING,
                note=(
                    "Field names an ACCEPTED write used that an earlier refusal had "
                    "reached wrongly, written by the SUCCESS rather than by the refusal. "
                    "It is what turns a list of errors into a book with answers in it: "
                    "the fold attaches each correction to the group it fixes, so the next "
                    "cycle reads the right name beside the wrong one it is about to "
                    "repeat."
                ),
            ),
            Field(
                "crew_key",
                JSON_STRING,
                note="Digest of the refused caller's EXACT crew name, as above.",
            ),
        ),
        note=(
            "One entry per refused write AND one per write that corrected an earlier "
            "refusal, appended to the crewmate's own DM session log and folded under its "
            "slot. Written on the REFUSAL path, which no other entry type covers: every "
            "other type records something that happened, and this one records something "
            "that was prevented. Best-effort like the publish entry -- the refusal is "
            "already on its way back to the caller, so a log that is off costs the "
            "mistake book this row and nothing else."
        ),
    ),
    # -- Change cards and guides ---------------------------------------- #
    EntryType(
        "card/proposed",
        "The agent proposed a change card, at this point in the conversation.",
        (
            Field("slot", JSON_STRING, required=True, note="The slot whose chat shows the card."),
            Field("card_id", JSON_STRING, required=True, note="The card's id in the card store."),
            Field("kind", JSON_STRING, required=True, note="The card kind, e.g. setting.change."),
            Field(
                "title",
                JSON_STRING,
                required=True,
                note=(
                    "The gateway's title for the change, redacted and clipped. Names only: "
                    "a card's parameters are never recorded, and a secret card carries no "
                    "value field at all."
                ),
            ),
            Field("revision", JSON_INT, required=True, note="The card's revision when proposed."),
            Field(
                "risk", JSON_STRING, note="The computed risk: normal, tighten, widen, code_exec."
            ),
            Field(
                "turn",
                JSON_INT,
                note="The turn that proposed it; absent when no turn was live in this log.",
            ),
            _conversation_row("card"),
        ),
        note=(
            "Written when the propose call lands, in the same step that appends the card's "
            "row to the transcript, so seq orders it against the tool call that made it. "
            "The card's LIVE state stays the card store's; this log records the proposal "
            "and each outcome, not the edits in between."
        ),
    ),
    EntryType(
        "card/finished",
        "A change card reached a finished status.",
        (
            Field("card_id", JSON_STRING, required=True, note="The card's id."),
            Field(
                "status",
                JSON_STRING,
                required=True,
                enum=CARD_FINISHED_STATUSES,
                enum_closed=True,
                note=(
                    "The status the card entered. failed is retryable, so the newest entry "
                    "per card_id is its outcome."
                ),
            ),
            Field("revision", JSON_INT, required=True, note="The revision that finished."),
        ),
        note=(
            "One entry per finished status the card ENTERS, written beside the in-place "
            "update of its transcript row. No turn: a person confirms a card, usually "
            "between turns."
        ),
    ),
    EntryType(
        "guide/offered",
        "The agent offered a guide, at this point in the conversation.",
        (
            Field("slot", JSON_STRING, required=True, note="The slot whose chat shows the offer."),
            Field(
                "guide_id", JSON_STRING, required=True, note="The guide's id in the guide store."
            ),
            Field(
                "actions",
                JSON_ARRAY,
                required=True,
                item_type=JSON_STRING,
                note="The registered action ids, in order. Never their parameters.",
            ),
            Field("turn", JSON_INT, note="The turn that offered it, when one was live."),
            _conversation_row("offer"),
        ),
    ),
    EntryType(
        "guide/started",
        "The person started an offered guide.",
        (Field("guide_id", JSON_STRING, required=True, note="The guide's id."),),
        note="Written the first time a tab claims the guide; a re-claim after a lapse is not.",
    ),
    EntryType(
        "guide/finished",
        "A guide ended.",
        (
            Field("guide_id", JSON_STRING, required=True, note="The guide's id."),
            Field(
                "status",
                JSON_STRING,
                required=True,
                enum=GUIDE_FINISHED_STATUSES,
                enum_closed=True,
                note="How it ended.",
            ),
            Field(
                "reason",
                JSON_STRING,
                enum=GUIDE_FINISHED_REASONS,
                enum_closed=True,
                note="Why, when the status alone misleads: a cancelled guide whose save went through.",
            ),
        ),
    ),
    # -- a crewmate's own record ------------------------------------------- #
    EntryType(
        GOAL_ENTRY_TYPE,
        "A crewmate's goal record after a change: its goals and its milestones.",
        (
            Field(
                "goals",
                JSON_ARRAY,
                required=True,
                item_type=JSON_OBJECT,
                fields=(Field("text", JSON_STRING, note="The goal's text."),),
                note="Each goal as {text}, in the record's order.",
            ),
            Field(
                "milestones",
                JSON_ARRAY,
                required=True,
                item_type=JSON_OBJECT,
                fields=(
                    Field("text", JSON_STRING, note="What the milestone measures."),
                    Field("target", JSON_STRING, note="The target, as the crewmate set it."),
                    Field("current", JSON_STRING, note="The latest reading."),
                    Field("met", JSON_BOOL, note="Whether the latest reading meets the target."),
                    Field("at", JSON_STRING, note="When the latest reading was taken, ISO 8601."),
                ),
                note=(
                    "Each milestone as {text, target, current, met, at}: current is the "
                    "latest reading and at its time, met whether the gateway's reading "
                    "of it (member_goals.key_result_met) meets the target."
                ),
            ),
        ),
        note=(
            "Appended by the gateway to the crewmate's own DM session log whenever the "
            "goal record changes (a goal set, milestones applied, a reading recorded), "
            "and folded under its slot as the goal fold."
        ),
    ),
    EntryType(
        WAITING_ENTRY_TYPE,
        "What a crewmate waits on the person for, after that list changed.",
        (
            Field(
                "items",
                JSON_ARRAY,
                required=True,
                item_type=JSON_OBJECT,
                fields=(
                    Field("kind", JSON_STRING, note="options, question or card."),
                    Field("text", JSON_STRING, note="The question or the card's title, redacted."),
                    Field(
                        "options",
                        JSON_ARRAY,
                        item_type=JSON_STRING,
                        note="The choices the person can answer with, when it names any.",
                    ),
                    Field("asked_at", JSON_STRING, note="When it was asked, ISO 8601."),
                ),
                note=(
                    "Each item as {kind, text, options, asked_at}, newest first: kind is "
                    "options, question or card (dashboard.member_waiting.waiting_items)."
                ),
            ),
        ),
        note=(
            "Appended by the gateway to the crewmate's own DM session log when the "
            "list it reads off the thread changes: a question asked, answered by a "
            "reply or an [OPTIONS] chip, or a card raised or pressed. Folded under its "
            "slot as the waiting fold."
        ),
    ),
)
