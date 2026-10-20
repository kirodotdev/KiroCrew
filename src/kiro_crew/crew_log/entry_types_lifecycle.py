"""Crew log declarations for a session's own lifecycle and its turns.

The ``session/``, ``turn/`` and ``write/`` types: a session's log opening, its class
changing, the session closing, failing to come back at startup, being adopted or
released and creating another session; a turn starting, being refused and completing;
and the writer's own account of appends it lost. Beside them sit the members a session's
class and a parent edge are recorded with, the billed token dimensions a completed turn
carries, and the vocabulary of who caused a turn.

:mod:`kiro_crew.crew_log.entry_types` composes these declarations into the session table
and re-exports this module's public names other than ``TYPES``; the rules every
declaration follows are in that module's docstring.
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
    _turn,
)

#: The four billed token dimensions, each required INSIDE the mapping.
#: ``on_turn_completed`` builds the whole mapping in one literal, defaulting each
#: dimension to zero, so a present ``tokens`` always carries all four. The parent
#: field stays optional: the crash-repair closer omits ``tokens`` altogether, so does
#: a measured closer whose provider reported no count on any dimension (four zeros
#: would be an absence written as a measurement), and a nested requirement is
#: checked only once its object is there.
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

#: The members of a parent citation on the two entries that MOVE a session in the
#: tree. One tuple rather than two identical ones, for the reason
#: :data:`_SESSION_CLASS_FIELDS` is one: an adoption and the release that undoes it
#: cite a parent the same way, and a member declared on one and not the other would
#: be readable from one half of a takeover and silently missing from the other.
#:
#: ``session/opened.parent`` deliberately keeps its own copy. It carries the same two
#: keys, but its notes describe what ``session_create`` attributed at birth, which is
#: not what these two record -- and the reference tables are read per entry type.
_PARENT_EDGE_FIELDS: tuple[Field, ...] = (
    Field(
        "slot",
        JSON_STRING,
        required=True,
        note=(
            "The parent session's slot key. The tree's own key, so this is the "
            "member a fold reads."
        ),
    ),
    Field(
        "sid",
        JSON_STRING,
        note=(
            "The parent's ACP session id at the moment of the call -- a citation of "
            "that session's log for a reader, never a tree key, since a slot outlives "
            "its ACP session. Absent when the gateway had no live handle for it, and "
            "when the id exceeded MAX_ACP_SESSION_ID_LEN."
        ),
    ),
)

#: This family's declarations, in the order the registry renders them.
TYPES: tuple[EntryType, ...] = (
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
                "previous_none",
                JSON_BOOL,
                note=(
                    "True when the gateway determined this store has NO predecessor -- it "
                    "is the slot's first. Written because the ABSENCE of every predecessor "
                    "key cannot say that: a store written before these keys existed also "
                    "has none of them, and its omission may equally be a predecessor the "
                    "old gateway failed to name. A reader ranking a slot's stores may pass "
                    "over a store that states this, and may not pass over one that merely "
                    "omits everything, because that one's omission is unexplained. Absent "
                    "when ``previous`` names one, when ``previous_undecided`` reports one "
                    "it could not determine, and on a store whose named predecessor was "
                    "rejected for belonging to another slot. Also absent -- deliberately "
                    "-- on a create whose caller DETERMINED nothing: an opener handed one "
                    "captured id and no finding either way writes no key here, because an "
                    "empty id from such a caller means only that it had nothing to give, "
                    "and reading that as a conclusion would have this entry declare a slot "
                    "with earlier stores to be its own first. The same holds for a store "
                    "read that HANDED the question on rather than answering it: units of "
                    "the slot exist and could not be ranked, so the next source coming "
                    "back empty is not a finding about this slot either."
                ),
            ),
            Field(
                "previous_undecided",
                JSON_BOOL,
                note=(
                    "True when a predecessor EXISTS and the gateway could not determine "
                    "it, which ``previous`` being absent cannot express: that also "
                    "describes the slot's FIRST store. The two demand opposite treatment "
                    "from a reader ranking a slot's stores -- a first store may be passed "
                    "over, an undetermined one may not, because passing over it elects "
                    "the store before it and freezes a citation the gateway declined to "
                    "guess. Absent when the predecessor is named, and on a first store."
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
                "depth",
                JSON_INT,
                note=(
                    "How many creators stand above this session in the session tree, as "
                    "the writer could establish it at the moment the log was opened. "
                    "``0`` is a session nobody created -- a person's own tab, a fork, a "
                    "channel conversation -- and a child is its creator's depth plus "
                    "one.\n\n"
                    "Recorded rather than derived because the reader this serves counts "
                    "a DISTRIBUTION over many sessions and may hold nothing but a few "
                    "integers: a fold that derived depth instead would have to join "
                    "this log to its creator's, which means keeping slot names and so "
                    "keeping identity out of a counter that has no business holding "
                    "any. One number written once by the side that can see the whole "
                    "chain is what keeps that reader additive.\n\n"
                    "ABSENT means not established, never zero. Three cases produce it "
                    "and a reader must not separate them: a log opened before this "
                    "field existed, a writer that states no depth at all, and a session "
                    "whose creator is known to exist while the tree could not place it "
                    "-- an unseeded fold, a fold reported incomplete, a cited creator "
                    "with no log of its own, a slot on a cycle. Reading an absent depth "
                    "as a root would count each of those as its own tree, so a counting "
                    "reader leaves them out of the count entirely rather than "
                    "defaulting them.\n\n"
                    "A WRITE-TIME reading, like ``parent`` beside it and for the same "
                    "reason: this entry states what the session was created as, and "
                    "nothing rewrites it. A later ``session/adopted`` or "
                    "``session/released`` moves the session and so changes its true "
                    "depth; this number does not follow, and a reader that needs the "
                    "CURRENT depth reads the tree rather than this field."
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
            "entry HAS a class: a class on the opener means the writer also records "
            "transitions. An opener with no class says nothing about either, and "
            "refuses."
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
                enum=("reset", "destroyed", "destroyed_sid_retained"),
                note=(
                    "The gateway's own teardown reason, verbatim. Open: reset comes "
                    "from the reset route, destroyed or destroyed_sid_retained from "
                    "destroy (the latter defined in session_lifecycle when another "
                    "key may still map to the session id), and a new teardown path "
                    "may pass its own word."
                ),
            ),
        ),
    ),
    EntryType(
        "session/unrestored",
        "This session's tab was listed as open at startup and did not come back.",
        (
            Field(
                "listed",
                JSON_INT,
                required=True,
                note="Keys the open-tab snapshot listed this boot.",
            ),
            Field(
                "restored",
                JSON_INT,
                required=True,
                note="How many of them became tabs.",
            ),
            Field(
                "kept",
                JSON_INT,
                required=True,
                note=(
                    "How many listed keys this restore DROPPED -- the shown-missing "
                    "subset, this one included. A dropped key is not gone from the "
                    "reopen seed; it is not SHOWN."
                ),
            ),
        ),
        note=(
            "Not a close. session/closed says the gateway stopped serving the session "
            "for a stated reason, which is the opposite of what happened here: the "
            "session is intact and the registry of open TABS lost it. All three fields "
            "are required because the one tab only means something against the set -- "
            "one of sixteen that did not come back is a lost working set, one of one "
            "is a single unreadable transcript -- and the emitter counts all three "
            "before it writes any of them."
        ),
    ),
    EntryType(
        "session/adopted",
        "Another session took this one over, so it now hangs under that session.",
        (
            Field(
                "parent",
                JSON_OBJECT,
                required=True,
                fields=_PARENT_EDGE_FIELDS,
                note=(
                    "The session that took this one over, resolved by the gateway from "
                    "the calling connection rather than named by the caller. Required: "
                    "an adoption with no adopter records nothing, and the entry that "
                    "means 'this session has no parent' is session/released."
                ),
            ),
            Field(
                "previous_parent",
                JSON_OBJECT,
                fields=_PARENT_EDGE_FIELDS,
                note=(
                    "The parent this adoption replaced, for a reader of the log. "
                    "Absent when the session was a root. No fold reads it: the tree "
                    "takes the parent from ``parent`` alone, so a reader reconstructing "
                    "who held the session and when has this and the tree still has one "
                    "statement of the current edge."
                ),
            ),
        ),
        note=(
            "Recorded on the session that was taken over, which is where "
            "``session/opened.parent`` already puts a creating edge -- one axis, one "
            "place to read it, and a takeover that moves a whole subtree writes one "
            "entry rather than one per descendant, because descendants hang on this "
            "session's slot and not on a path.\n\n"
            "The creating edge is not rewritten and cannot be: the log is append-only "
            "and that entry states who OPENED the session, which stays true. This "
            "entry states who holds it now, and the fold prefers the newest of the two "
            "rather than merging them."
        ),
    ),
    EntryType(
        "session/released",
        "This session's parent let it go, so it stands on its own again.",
        (
            Field(
                "previous_parent",
                JSON_OBJECT,
                fields=_PARENT_EDGE_FIELDS,
                note=(
                    "The parent that let this session go. Absent when the gateway could "
                    "not name it, which is why it is not required: the entry's meaning "
                    "is that there is no parent NOW, and that does not depend on being "
                    "able to name the one there was."
                ),
            ),
        ),
        note=(
            "The counterpart of session/adopted, and the only entry that takes a parent "
            "edge away. A session/opened carrying no parent does not: it means the "
            "entry did not repeat a creator, which a reader must not read as a "
            "retraction, so the retraction needs a record of its own."
        ),
    ),
    EntryType(
        "session/spawned",
        "This session created another one with session_create or session_fork.",
        (
            Field(
                "child",
                JSON_OBJECT,
                required=True,
                fields=(
                    Field(
                        "slot",
                        JSON_STRING,
                        required=True,
                        note=(
                            "The new session's slot key, minted by the gateway in the same "
                            "call. The tree's own key, so this is the member a fold reads."
                        ),
                    ),
                ),
                note=(
                    "The session this one created. The creator is not named in the "
                    "entry: it is the session whose log holds it, which the log's own "
                    "header states."
                ),
            ),
        ),
        note=(
            "Recorded on the CREATOR's log at the moment of creation, because the "
            "child has no log yet: its log is keyed by an ACP session id that only "
            "exists after its first turn. Without this entry the creating edge lived "
            "only in gateway memory until that turn, and a restart in between lost it.\n\n"
            "A fold reads it as the child's creating edge only when none of the "
            "child's own session/opened entries names a parent. A session/adopted or "
            "session/released on the child's log still wins over it, the same way they "
            "win over session/opened.parent."
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
                note=(
                    "Present only when the provider billed credits, positive and "
                    "finite; absent on a synthesized close and on a measured one the "
                    "provider did not bill in credits."
                ),
            ),
            Field(
                "tokens",
                JSON_OBJECT,
                fields=_TOKEN_FIELDS,
                note=(
                    "Present only when the provider reported a count above zero on some "
                    "dimension, then with all four; absent on a synthesized close and on "
                    "a measured one that reported no count."
                ),
            ),
            Field(
                "context",
                JSON_OBJECT,
                fields=(
                    Field(
                        "used",
                        JSON_INT,
                        required=True,
                        note="Tokens the provider reported occupying the window.",
                    ),
                    Field(
                        "window",
                        JSON_INT,
                        required=True,
                        note="Window size that reading was taken against.",
                    ),
                ),
                note=(
                    "The provider's own OCCUPANCY reading, a different quantity from "
                    "tokens above: tokens is summed over every model call the turn "
                    "made and answers what it cost, while this answers how full the "
                    "window was. The two travel as one object because a used count "
                    "from one turn over a window from another describes no turn, and "
                    "a model switch moves the window. Absent when the provider "
                    "reports no occupancy, so unmeasured reads as unmeasured."
                ),
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
)
