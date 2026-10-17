"""What a transcript line carries and how its timestamps order.

The metadata keys each rebuilding writer owns, a row's provenance and human-turn marker,
its delivery identity, and the one spelling and ordering of transcript timestamps.
"""

from __future__ import annotations

import uuid
from collections.abc import Container
from datetime import datetime, timedelta

# The keys :meth:`ConversationLog.compact` is authoritative for; every other field
# on the metadata line is another layer's and is carried through.
_COMPACT_OWNED_META_KEYS: frozenset[str] = frozenset(
    {"_type", "created_at", "last_consolidated", "compacted_at"}
)

# The keys the dashboard's slot save is authoritative for. It reconstructs the
# line from the slot's in-memory state, so for THESE absence is meaningful (a
# cleared title, an un-pinned slot, a reopened tab) — hence they are named here
# and not preserved, while everything else survives the save.
SLOT_OWNED_META_KEYS: frozenset[str] = frozenset(
    {
        "_type",
        "created_at",
        "last_consolidated",
        "closed",
        "closed_at",
        "memory_mode",
        "title",
        "agent",
        "model",
        "reasoning_effort",
        "autocompact_pct",
        "turn_timeout_secs",
        # Source-link dismissals: the slot save is authoritative (it rebuilds the
        # line from ``slot._dismissed_source_links``), so on a FULL save absence
        # means "no dismissals". Naming it here also folds it into
        # ROWS_ONLY_DEFERRED_META_KEYS (= SLOT_OWNED - ROWS_ONLY_OWNED), so a
        # ROWS-ONLY save onto a transcript another live slot owns DEFERS it and
        # carries the on-disk set back verbatim — without this a popped alias's
        # rows-only handover would overwrite the replacement slot's own dismissals.
        "dismissed_source_links",
        "mode",
        "workspace",
        # Slot-owned so ABSENCE can retract it. A crew rebound from a named
        # memory store back to the default writes no key at all, and an unowned
        # key is carried forward forever by ``carry_unowned_metadata`` -- so the
        # rebind would be un-erasable and the session would keep consolidating
        # into the silo it left.
        "memory_store",
        # The namespace the agent was picked in. Slot-owned for the same reason
        # as memory_store: a name-only pick after a template pick writes no key,
        # and an unowned key would carry the stale "template" forward forever.
        "agent_kind",
        # The app-owned agent binding (see _ChatSlot.app_agent_owner). Slot-owned so a
        # session that carries none clears it instead of inheriting a stale one.
        "app_agent_owner",
        "project",
        # Remote-execution binding: owned by the slot, so clearing it in memory
        # clears it on disk. Left unowned, a rebind or an unbind would be undone
        # on the next save by the carried-forward copy.
        "executor",
        "instance_id",
        "remote_slot",
        # Local-turn crash marker: the generation of the turn ``_run_chat``
        # admitted, written before provider dispatch and omitted once the turn
        # reaches teardown. Owned because absence IS the clear: a
        # carried-forward value would flag every later restart as interrupted
        # after one clean completion.
        "turn_in_flight_generation",
        # The row that opened that turn (role, content, cls, ts, meta), carried
        # with the generation so a restart inside the periodic flush window
        # can put the prompt back before flagging the interruption. Cleared by
        # omission alongside the generation.
        "turn_in_flight_prompt",
        "folder_id",
        "app",
        "artifact",
        # Durable copy of the slot's held /note lines. Owned, not
        # monotonic: the hold is written while notes are held and must be
        # CLEARED by absence once the flush delivers them — carried forward
        # instead, a restart would re-deliver a note the user already saw.
        "deferred_notes",
        # Durable copy of the queued user prompts. Owned, not monotonic: the
        # value is written while prompts wait and must be CLEARED by absence
        # once the drain consumes them — carried forward instead, a restart
        # would hand back a prompt whose turn already ran.
        "queued_prompts",
        "pinned",
        # Slot-owned so ABSENCE retracts it: an unmute writes no key, and an
        # unowned key would be carried forward forever by
        # ``carry_unowned_metadata`` -- the session could never be un-muted.
        "mutes_opened",
        # Same reason: an unmute of the per-row mute writes no key.
        "muted",
        "color_index",
        "color_hex",
        "color_theme",
        "tags",
        "forked_from",
        "linked_session_key",
        "tab_id",
    }
)

# The subset of :data:`SLOT_OWNED_META_KEYS` a ROWS-ONLY slot save still owns.
#
# A save that must persist a slot's messages onto a transcript whose metadata line
# describes a DIFFERENT live slot cannot use the whole ownership claim above: the
# rebuild would revert the other slot's title, folder, tags or pin. Such a save
# preserves every slot-owned field the line already carries and keeps authority
# over only these — the file's identity and accounting, which every writer
# maintains and which the save carries forward from disk anyway.
#
# ``closed``/``closed_at`` are NOT here, even though the write is open-shaped and
# the whole claim above would erase them. On a line another slot published, a
# ``closed`` flag is that holder's own DISMISSAL, and the two mistakes cost
# differently. Erasing a dismissal the holder just committed resurfaces a tab the
# user put away and re-arms the channel reconciler on it, with the holder already
# popped so nothing rewrites the flag. Leaving a stale flag in place instead costs
# nothing durable: the live holder owns these keys on its own next full save.
#
# The one path that DOES clear a stale flag from outside the holder is the resume
# route, and it only clears one it can prove predates its own boundary
# (``clear_closed(..., only_if_closed_before=...)``, compared inside the store's
# lock) — precisely because an unconditional clear "reopens a replacement the user
# closed". A rows-only save carries no such boundary, so it defers, the same way
# every other field on another writer's line does.
#
# Narrowing this far is only correct against ANOTHER slot's line, so the save
# establishes that first (from the line's ``tab_id``) and falls back to the full
# claim otherwise. Applied to a slot's own line it would strand that slot's
# uncommitted metadata instead of protecting anyone's — and that fallback is where
# an open-shaped write still clears a stale ``closed``, because a line this slot
# published carries no other holder's dismissal to lose.
ROWS_ONLY_OWNED_META_KEYS: frozenset[str] = frozenset({"_type", "created_at", "last_consolidated"})

# The keys a ROWS-ONLY slot save must DROP from its rebuild so the on-disk values
# are carried back verbatim.
#
# Named here in full rather than derived at the call site as
# ``SLOT_OWNED_META_KEYS - ROWS_ONLY_OWNED_META_KEYS``, because that difference
# under-approximates: the slot save also writes fields that DESCRIBE an owned one
# without being owned themselves (absence must not erase them, so they are
# deliberately outside the ownership claim and survive via
# :func:`carry_unowned_metadata`). Deferring the described field while keeping the
# describing one commits a line that matches NEITHER slot — worse than either,
# because each half is separately valid and nothing downstream can detect the
# mismatch. ``title_origin`` and ``title_refresh_mark`` are the title's provenance
# and its background-refresh budget: read back beside another slot's title they
# either unlock the refresh on a name a user typed by hand or lock a generated name
# out of refresh permanently. They travel WITH the title, so they are deferred with
# it. ``title_low_signal`` is the same shape — the early-refresh eligibility of
# THIS slot's title — so a popped slot's stale flag carried over a live
# replacement's would wrongly suppress or re-arm the replacement's turn-one
# refresh after restart. It defers with the title too.
#
# ``created_by`` and ``origin`` are the same shape and the highest-consequence
# instance of it, because what they describe is AUTHORIZATION rather than
# presentation. ``created_by`` is the attribution the member ownership boundary in
# session-control reads, and it is meaningless without the ``mode`` that is deferred
# beside it — a member ``mode`` from the live holder read next to a different
# principal's ``created_by`` names an owner who never opened this session.
# ``origin`` must round-trip with ``app``, also deferred: split, a tab reads back as
# one holder's slot kind wearing the other's app binding, which is what decides
# ``slots:user`` visibility and the unattended approval window. Both are attributes
# of the SLOT, not facts about the conversation, so on a transcript with a live
# holder the holder's are the true ones. Deferring them also fails CLOSED where the
# line carries none: an absent ``created_by`` denies rather than grants, and an
# absent ``origin`` restores to the empty sentinel the rehydrate paths already treat
# that way.
#
# What is left out is left out deliberately: ``auto_tagged``, ``human_seen``,
# ``channel_origin`` and ``channel_folder_filed`` are MONOTONE once-flags about the
# CONVERSATION, set and never cleared, so a shared transcript's two writers cannot
# disagree about them in a way that outlives the pair.
ROWS_ONLY_DEFERRED_META_KEYS: frozenset[str] = (
    SLOT_OWNED_META_KEYS - ROWS_ONLY_OWNED_META_KEYS
) | frozenset({"title_origin", "title_refresh_mark", "title_low_signal", "created_by", "origin"})


def carry_unowned_metadata(
    rebuilt: dict,
    existing: dict,
    owned: Container[str],
) -> dict:
    """Carry every pre-existing metadata field the rebuilding writer does not own.

    A writer that reconstructs a transcript's metadata line from its own state
    (the dashboard slot save, :meth:`ConversationLog.compact`) is authoritative
    ONLY for the keys it writes: for those, absence is meaningful and must erase
    (clearing a title, un-pinning, reopening a closed tab). For every OTHER key
    absence means "not mine to know about", so reconstructing the subset silently
    deletes another layer's durable state.

    Enumerating the foreign keys to preserve instead is the failure mode this
    replaces: each new field has to be added to every rebuilder, and the one that
    is missed loses data with no error — the rotation generation and the
    consolidation retry accounting were both erased that way. So *owned* names the
    writer's OWN keys and everything else is carried through verbatim, making
    preservation the default and erasure the deliberate act.

    Preservation is unconditional, INCLUDING the consolidation retry accounting
    (:data:`_CONSOLIDATION_META_KEYS`), because a rebuild is not evidence about
    content: a slot flush re-serializes the same window, and a compaction archives
    turns the budget has already measured without introducing any the LLM has not
    seen — erasing the accounting there resets a live backoff and resumes billed
    retries.

    A save that genuinely EDITS the conversation is distinguished by the content
    identity it writes, not by what this helper drops: the dashboard rewrite path
    advances ``rotation_generation``, which releases the budget through the span
    identity the accounting is stamped with (see :class:`AttemptedSpan` and
    :meth:`ConversationLog._attempts_describe_current_span`) and invalidates any
    in-flight attempt's marker write. Keeping that in ONE counter is what makes an
    edit and a rotation behave identically; a second, parallel drop-the-keys valve
    here would additionally discard the armed backoff deadline, handing a session
    whose consolidation keeps failing a free billed turn on every user edit.

    Returns *rebuilt*, mutated in place.
    """
    for meta_key, value in existing.items():
        if meta_key in owned or meta_key in rebuilt:
            continue
        rebuilt[meta_key] = value
    return rebuilt


# The fields that record where a message came from: the session key it arrived
# on (``source_thread``, e.g. ``slack:1785861252.833429``) and the platform user
# who sent it (``source_user``). Written by :meth:`ConversationLog.append`, read
# by :meth:`ConversationLog.get_source_threads` for cross-session citation and
# by SEL attribution.


PROVENANCE_FIELDS = ("source_thread", "source_user")


def carry_provenance(dest: dict, src: dict) -> None:
    """Copy *src*'s recorded provenance onto *dest*, leaving absent fields out.

    A message's origin is a property of the message, not of the file it happens
    to be stored in, so any path that copies or re-serializes a persisted line
    must carry these fields across or the copy claims a different origin than
    the original.

    Absent stays absent, and an empty or non-string value is treated as absent:
    :meth:`ConversationLog.append` writes each field only when it is truthy and
    :meth:`get_source_threads` filters on the same truthiness, so writing
    ``""`` would create a third state that reads as present-but-unusable.
    """
    for field in PROVENANCE_FIELDS:
        value = src.get(field)
        if isinstance(value, str) and value:
            dest[field] = value


#: ``meta`` key marking a row a PERSON authored, set by the send paths a human
#: actually reaches. An ALLOWLIST on purpose: ``role == "user"`` does not mean a
#: person typed it, because the gateway drives agent turns through the same shape
#: -- ``_ChatSlot.enqueue_or_run_prompt`` appends ``("user", prompt, "msg msg-u")``
#: for an Issue Radar wake, identical in role AND presentation class to a typed
#: message. So a reader that wants human activity must require this marker rather
#: than exclude the machine callers it happens to know about: an unmarked row
#: simply does not count, which keeps the next machine caller harmless by default.
HUMAN_TURN_META_KEY = "human"


def transcript_sort_key(ts: str) -> tuple[int, float]:
    """Sort key for a transcript timestamp: ``(bucket, epoch_seconds)``.

    Shared by every path that has to put two independently written streams of
    transcript lines into one chronological order.

    Timestamps in one transcript are not guaranteed to share one format. Both
    the dashboard and the channel path now write offset-aware values (message
    rows via :func:`monotonic_transcript_ts`, metadata via
    :func:`metadata_now_iso`), but transcripts written by older builds still
    hold naive ``datetime.now().isoformat()`` rows. Comparing those as STRINGS
    orders them by their text, so on any host that is not UTC a naive
    ``10:00:00`` sorts before an aware ``09:30:00+00:00`` that actually happened
    later — and this merge deletes the source file afterwards, so the wrong order
    is what survives.

    Naive values are interpreted as local time, matching the writer that
    produced them. ``bucket`` keeps unparseable values (bucket 1) after every
    real instant instead of letting a fallback epoch interleave them into the
    middle of the conversation.
    """
    parsed = _parse_transcript_ts(ts)
    if parsed is None:
        return (1, 0.0)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return (0, parsed.timestamp())


def _parse_transcript_ts(ts: str) -> datetime | None:
    """Parse a transcript ``ts`` in either stored format, or ``None``.

    Shared by :func:`transcript_sort_key` and :func:`monotonic_transcript_ts` so
    the two agree on what counts as a readable timestamp: whatever one of them
    can order, the other can stamp against.
    """
    try:
        return datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def metadata_now_iso() -> str:
    """Offset-aware ISO-8601 stamp for a transcript metadata timestamp.

    A transcript's metadata line carries absolute-instant fields --
    ``created_at``, ``updated_at``, ``compacted_at``, ``rotated_at`` -- that are
    read back as points in time, not wall clocks: the dashboard renders a
    session's ``created_at`` as a local-time timestamp, and
    :func:`transcript_sort_key` orders lines against it. A bare
    ``datetime.now().isoformat()`` records naive local wall-clock with no
    offset, so a reader (the browser, or a merge running on another host) has no
    way to know which timezone produced it -- the dashboard then renders it
    verbatim, showing a Slack/channel session's creation time in UTC instead of
    the viewer's local zone. Resolving to an absolute instant with
    ``astimezone()`` records the offset, matching the message-row convention in
    :func:`monotonic_transcript_ts` so both the metadata line and the rows below
    it speak the same, unambiguous format.
    """
    return datetime.now().astimezone().isoformat()


def mint_row_mid() -> str:
    """Mint a durable per-row delivery identity for a transcript row.

    The ONE place the ``meta.mid`` format is spelled. ``_ChatSlot.append`` mints
    the id for a row that enters a dashboard window, and the dashboard
    dual-writers (``cron_inject``, ``workflow_inject``) read it back
    off that append to stamp their durable copy (``row_mid``). A writer with no
    slot to mint from -- a channel dispatcher persisting a turn it ran on its own
    session -- has to mint the id itself, and it must produce the SAME shape,
    because the readers match on the value, not on who wrote it.

    Why a channel row needs one AT WRITE TIME: the dashboard's merge keys on
    ``meta.mid`` and nothing else. ``isRedeliveredMessage`` drops a redelivered row
    by it, ``olderHeadAbovePage`` cuts the retained scrollback head at it, and
    ``rowIdentities``/``tailNotInPage`` decide by it which prior rows a page already
    carries -- and every one of those DECLINES rather than guesses when the id is
    absent or has changed. A row persisted without one is re-minted by each surface
    that materializes it (``channel_slots._rebuild_window`` /
    ``refresh_channel_window``), so one logical row carries a different identity on
    every pass, silently degrading all three at once.

    Random rather than a per-key counter, for the reason ``_ChatSlot.append``
    gives: a counter rebased after a restore can reissue an id a restored row
    already holds, and a colliding id makes a client DROP a real message. A
    random id has no such failure mode.
    """
    return f"m-{uuid.uuid4().hex[:16]}"


def monotonic_transcript_ts(previous: str | None, now: datetime) -> str:
    """Stamp a transcript row so it sorts strictly AFTER *previous*.

    The two rows of one turn -- the message a person sent and the reply to it --
    are written microseconds apart. A host whose clock ticks coarsely returns
    the SAME value for both reads (Windows advances the system clock in ~15.6 ms
    steps), so the pair lands on disk carrying an identical ``ts``. Every
    consumer that orders a transcript by ``ts`` -- the dashboard, and the merge
    built on :func:`transcript_sort_key` -- is then free to render the answer
    above the question that prompted it.

    Returning ``previous`` plus one microsecond when the clock has not advanced
    makes the order a property of the write sequence rather than of the clock's
    resolution. The correction only ever moves a row FORWARD, and only as far as
    the previous row already claims to be, so it cannot push a row past one that
    genuinely happened later. For a coarse clock that is one tick; it is larger
    only when *previous* is a legacy naive value whose lost fold (below) makes it
    read up to one local offset ahead.

    Everything here is compared and emitted as an ABSOLUTE INSTANT, never as a
    wall clock. A local wall clock repeats itself for an hour when daylight
    saving ends, and ``isoformat`` does not record which of the two passes a
    naive value belongs to -- so a naive stamp is not orderable against the
    offset-aware rows the dashboard writes into the same file, and one written
    during the repeat can read back an hour early. A naive *now* is therefore
    resolved to its offset before use, and the returned value always carries
    that offset. A naive *previous* is an older row from before this rule: it is
    read as local time, the same reading :func:`transcript_sort_key` gives it,
    which is the most its lost fold allows. An absent or unparseable *previous*
    yields *now*.
    """
    if now.tzinfo is None:
        now = now.astimezone()
    if previous:
        prior = _parse_transcript_ts(previous)
        if prior is not None:
            if prior.tzinfo is None:
                prior = prior.astimezone()
            if now <= prior:
                now = prior + timedelta(microseconds=1)
    return now.isoformat()


def latest_transcript_ts(*candidates: str | None) -> str | None:
    """The latest of several candidate predecessor timestamps, or ``None``.

    A writer can have more than one row to order itself against, and the two
    writers of a session transcript learn about predecessors differently:
    :meth:`ConversationLog.append` reads the authoritative file tail under the
    cross-process flock, while the dashboard's ``_ChatSlot.append`` runs on the
    event loop and may only consult in-process state (a ``stat`` plus a file read
    per append is what ``AUTOSDE.yaml``'s ``no-blocking-call-on-event-loop`` rule
    forbids). The slot therefore floors on the later of its in-memory window tail
    and the last on-disk tail it was told about at the previous save.

    Comparison goes through :func:`transcript_sort_key`, never string ordering:
    rows carry two stored formats (aware and naive isoformat), so ``"a" > "b"``
    on the raw strings compares different domains and can pick the earlier row.

    An UNPARSEABLE candidate is skipped rather than ranked. ``transcript_sort_key``
    deliberately buckets a value it cannot parse *after* every real instant, so
    that a corrupt line displays at the end of a transcript rather than in the
    middle of the conversation. That is right for sorting and wrong for a floor:
    ranked, one malformed ``ts`` would win here, and
    :func:`monotonic_transcript_ts` ignores a previous value it cannot parse — so
    a single corrupt row on disk would silently switch the whole ordering
    guarantee off and let the next row tie its predecessor again.

    ``None`` and empty candidates are ignored too, so a caller can pass a value it
    does not have yet without branching. Returns ``None`` when nothing usable was
    supplied — which correctly means "no floor", not "floor of zero".
    """
    best: str | None = None
    best_key: tuple[int, float] | None = None
    for candidate in candidates:
        if not candidate:
            continue
        key = transcript_sort_key(candidate)
        if key[0] != 0:
            # Unparseable: see the docstring. Not a usable floor.
            continue
        if best_key is None or key > best_key:
            best, best_key = candidate, key
    return best
