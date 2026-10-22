"""Session lifecycle facts: a session opened, adopted, released or spawned, and its class.

:func:`on_session_opened` writes the opening entry -- creating the crew log, resuming it, or
superseding the slot's previous one -- and the other entry points record a takeover, a release,
a spawn, the class a session holds (``session/class``) and a tab that was listed as open but
did not come back.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from kiro_crew.crew_log.emit_runtime import (
    _child_gone_probe,
    _hand_over,
    _handle,
    _queue_tail_repair,
    _remember,
    _report,
    _submit,
    _tracker,
    _write,
    live_turn,
)
from kiro_crew.crew_log.emit_state import (
    _KIND,
    _MAX_OPEN_CREW_LOGS,
    _SRC_GATEWAY,
    _creation_failed,
    _crew_log,
    _last_class,
    _lock,
    _note_eager,
    enabled,
    logger,
)
from kiro_crew.crew_log.emit_tree import (
    _candidate_is_same_slot,
    _durable_previous,
    _parent_citation,
    _record_session_tree_decision,
    _record_session_tree_edge,
    _record_session_tree_spawn,
    _tree_settle_hooks,
)

if TYPE_CHECKING:  # pragma: no cover -- typing only; the runtime imports stay gated
    # Type-only, so the boot-path import gate is untouched: this name exists for the
    # checker and for test_crew_log_exc_info_sites.py, which reads annotations to decide
    # whether a frame can hold a handle. A handle passed in as ``Any`` is invisible to it.
    from kiro_crew.crew_log.store import CrewLog


#: Used when a call site has no agent name, matching the repo's own default.
_DEFAULT_AGENT = "kirocrew"


def _seed_attempts(session_id: str, log: CrewLog) -> None:
    """Rebuild *session_id*'s attempt map from the entries already in its file.

    Runs on the writer thread, and only when memory cannot answer instead: a
    resume, or a session whose map the bound evicted. Without it a restart between
    two retries of one ordinal resets the count and writes attempt 1 twice, which
    is precisely the collision this field exists to prevent -- and the in-memory map
    cannot survive the restart that makes the collision possible.

    NOT on every open. The scan is O(file) and runs on the single writer thread, so
    seeding a session that already holds its counts would cost one full rescan per
    open against a file that only grows -- paid by every other session's appends
    too, since they queue behind it.

    Best-effort: a file this cannot read leaves the map empty, which degrades to
    the old behaviour rather than failing the resume.
    """
    highest: dict[int, int] = {}
    try:
        # Like read_page, this best-effort scan keeps readable records; folds still refuse seq damage.
        for entry in log.iter_from(1, strict_seq=False):
            if entry.type != "turn/started":
                continue
            turn = entry.data.get("turn")
            if not isinstance(turn, int) or isinstance(turn, bool):
                continue
            attempt = entry.data.get("attempt")
            if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
                attempt = 1
            if attempt > highest.get(turn, 0):
                highest[turn] = attempt
    except Exception as exc:
        _report("seeding turn attempts", exc, op="seed-turn-attempts")
        return
    _tracker().seed_attempts(session_id, highest)


def _latch_class(session_id: str, observed: "tuple[str, str, bool, str]") -> None:
    """Remember the class a line just stated for *session_id*.

    Called only AFTER the entry carrying it is on disk, for the reason
    ``on_request_configured`` gives about its own fingerprint: committing the
    value before the append would let one transient failure suppress every later
    statement of the same class, leaving the log permanently without the record.
    """
    with _lock:
        _last_class[session_id] = observed
        _last_class.move_to_end(session_id)
        _tracker().trim(_last_class, _MAX_OPEN_CREW_LOGS, lambda k: k, "session classes")


def _note_class_change(
    session_id: str, log: CrewLog, observed: "tuple[str, str, bool, str] | None"
) -> None:
    """Append ``session/class`` when *observed* is not what this log last stated.

    The class recorded when a log is opened is true of that instant, and a session
    can be given a channel surface, an app owner or a different memory mode while
    it runs. A reader deciding whether one session may read this log has to be able
    to see that, and for a session that has closed the log is the only thing left
    to see it in -- so a move is recorded here rather than left to a live lookup
    that will not be available when the question is asked.

    ``observed`` is ``None`` when the caller supplied no memory mode. Nothing is
    written then, matching the opening entry: a log that states no class refuses
    every test built on one, and appending a transition to it would leave a log
    whose class history has a middle but no beginning.

    A latch MISS appends rather than seeds. The latch is bounded, so an eviction
    is possible while the log stays open, and the two directions are not
    symmetric: a redundant line cannot change a fold that takes the most
    restrictive value each member ever held, while a skipped one silently widens
    who may read the log.
    """
    if observed is None:
        return
    with _lock:
        known = _last_class.get(session_id)
    if known == observed:
        return
    memory, app, channel, workspace = observed
    data: dict[str, Any] = {"memory": memory}
    if app:
        data["app"] = app
    if channel:
        data["channel"] = True
    if workspace:
        data["workspace"] = workspace
    log.append("session/class", data, src=_SRC_GATEWAY)
    _latch_class(session_id, observed)


def on_class_observed(
    session_id: str,
    *,
    memory: str = "",
    app: str = "",
    channel: bool = False,
    workspace: str = "",
) -> None:
    """Record that this session's CLASS is now *(memory, app, channel, workspace)*.

    For the moment a class becomes true rather than the moment someone next samples
    it. The per-turn observation in :func:`on_session_opened` cannot see a channel
    link that commits and is removed inside ONE turn, and content authored through
    that link is in the log with nothing saying it was published -- so the surfaces
    that COMMIT a link call this instead of waiting to be sampled.

    Writes nothing when the class has not moved, and nothing at all when *memory* is
    empty: a log that states no class refuses every test built on one, and appending a
    transition to it would leave a history with a middle and no beginning.

    Returns without waiting, like every other emitter here, which is what lets a
    caller holding a lock use it. That is safe because a write this writer permanently
    loses is itself recorded, and the class fold reads a dropped write as a hole -- so
    a lost move costs a refusal rather than a silent grant.
    """
    if not session_id or not memory:
        return
    observed = (memory, app, channel, workspace)

    def _job() -> None:
        log = _handle(session_id)
        if log is None:
            return
        _note_class_change(session_id, log, observed)

    _submit(_job, "appending session/class", session_id)


def on_session_adopted(
    session_id: str,
    *,
    slot: str,
    parent_slot: str,
    parent_sid: str = "",
    previous_parent_slot: str = "",
    previous_parent_sid: str = "",
    on_settled: "Callable[[bool], None] | None" = None,
) -> None:
    """Record that *parent_slot* has TAKEN OVER the session *session_id*.

    Written on the session that moved, which is the side ``session/opened.parent``
    already puts a creating edge on -- so the tree reads one axis from one place, and a
    takeover of a session that has children costs one entry rather than one per
    descendant, because descendants cite this session's slot and not a path through it.

    Nothing is rewritten, and nothing could be: the log is append-only, and the opening
    entry states who OPENED the session, which stays true. This entry states who holds
    it now, and the fold prefers the newest of the two.

    ``previous_parent`` is recorded for a reader of the log and is not folded. It is
    passed as two plain strings rather than a mapping so this signature says exactly
    which values it accepts, and the ``sid`` half is omitted when the caller has none:
    an empty string would read as a parent whose id is blank.

    Returns without waiting, like every other emitter here, and the projection is
    advanced inside the job AFTER the append succeeds -- durability first, then memory --
    so the disk can never hold a decision the memory lacks, and a lost write leaves the
    tree where it was rather than moving it on the strength of an append that did not
    land.

    ``on_settled`` is how a CALLER waits for that outcome, and this entry point has one
    where the others do not because the append IS the operation here: a verb that told
    its caller "adopted" and then lost the write would have reported a takeover that
    never happened. It is called once, off the caller's thread, with ``True`` when the
    entry is on disk and ``False`` when the write was given up on or there was no log to
    write to. Not awaited HERE -- ``_submit`` must never block the loop -- so the waiting
    is the caller's to bound.
    """
    if not session_id or not slot or not parent_slot:
        # No slot is not a tree edge: the tree is keyed by slot, so an entry with
        # neither side of the edge names nothing a reader could fold.
        if on_settled is not None:
            on_settled(False)
        return
    data: dict[str, Any] = {"parent": _parent_citation(parent_slot, parent_sid)}
    previous = _parent_citation(previous_parent_slot, previous_parent_sid)
    if previous:
        data["previous_parent"] = previous

    settle = _tree_settle_hooks(on_settled)

    def _job() -> None:
        log = _handle(session_id)
        if log is None:
            settle.fail()
            return
        written = log.append("session/adopted", data, src=_SRC_GATEWAY)
        settle.wrote()
        _record_session_tree_decision(session_id, slot, written, parent_slot)
        # After the tree holds the decision, so the wake reaches the creator's cell.
        _note_eager(written, "session/adopted", session_id, data)

    _submit(
        _job,
        "appending session/adopted",
        session_id,
        after=settle.after,
        on_permanent_drop=settle.fail,
    )


def on_session_released(
    session_id: str,
    *,
    slot: str,
    previous_parent_slot: str = "",
    previous_parent_sid: str = "",
    on_settled: "Callable[[bool], None] | None" = None,
) -> None:
    """Record that the session *session_id* has been LET GO and is a root again.

    The counterpart of :func:`on_session_adopted` and the only entry that takes a
    parent edge away. A ``session/opened`` carrying no parent does not: it means that
    entry did not repeat a creator, which a reader must not read as a retraction, so
    the retraction needs a record of its own.

    ``previous_parent`` is the parent that let it go, recorded for a reader and not
    folded. It is optional because the entry's meaning does not depend on it: what this
    says is that there is no parent NOW.

    ``on_settled`` reports the durable outcome, for the reason it does on
    :func:`on_session_adopted`: the append is the operation, so a caller that must not
    claim a release it did not land waits for this.
    """
    if not session_id or not slot:
        if on_settled is not None:
            on_settled(False)
        return
    data: dict[str, Any] = {}
    previous = _parent_citation(previous_parent_slot, previous_parent_sid)
    if previous:
        data["previous_parent"] = previous

    settle = _tree_settle_hooks(on_settled)

    def _job() -> None:
        log = _handle(session_id)
        if log is None:
            settle.fail()
            return
        written = log.append("session/released", data, src=_SRC_GATEWAY)
        settle.wrote()
        _record_session_tree_decision(session_id, slot, written, None)
        # After the tree holds the decision, so the wake reaches the creator's cell.
        _note_eager(written, "session/released", session_id, data)

    _submit(
        _job,
        "appending session/released",
        session_id,
        after=settle.after,
        on_permanent_drop=settle.fail,
    )


def on_session_spawned(
    creator_sid: str,
    *,
    creator_slot: str,
    child_slot: str,
    on_settled: "Callable[[bool], None] | None" = None,
) -> None:
    """Record, on the CREATOR's log, that *creator_slot* created *child_slot*.

    The child has no log yet -- its log is keyed by an ACP session id that only exists
    after its first turn -- so this is the only durable record of the creating edge
    before that turn. Without it a gateway restart in between lost the edge for good.

    The creator is the session whose log holds the entry, so the entry names only the
    child. A creator with no ACP session has no log to write to; ``_handle`` then
    answers None and nothing is written, which leaves today's in-memory witness as the
    only record, as before.

    Returns without waiting, and the projection is advanced inside the job AFTER the
    append succeeds. ``on_settled`` reports the durable outcome the way it does on
    :func:`on_session_adopted`: called once, off the caller's thread, ``True`` when the
    entry is on disk and ``False`` when it was given up on or there was no log.
    """
    if not creator_sid or not creator_slot or not child_slot or creator_slot == child_slot:
        if on_settled is not None:
            on_settled(False)
        return
    data: dict[str, Any] = {"child": {"slot": child_slot}}

    settle = _tree_settle_hooks(on_settled)

    def _job() -> None:
        log = _handle(creator_sid)
        if log is None:
            settle.fail()
            return
        written = log.append("session/spawned", data, src=_SRC_GATEWAY)
        settle.wrote()
        _record_session_tree_spawn(creator_sid, creator_slot, child_slot, written)
        # After the tree holds the create, so the creator's page draws the new worker.
        _note_eager(written, "session/spawned", creator_sid, data)

    _submit(
        _job,
        "appending session/spawned",
        creator_sid,
        after=settle.after,
        on_permanent_drop=settle.fail,
    )


def on_open_tab_unrestored(
    session_id: str,
    *,
    listed: int,
    restored: int,
    kept: int,
) -> None:
    """Record that this session's tab was listed as open but did not come back.

    The durable account of a dashboard startup that could not rebuild a tab. Before
    this, a tab that vanished left NOTHING anywhere: no ``session/closed``, because
    the gateway did not stop serving the session; no delete, because nothing was
    deleted; and the restore's own ``logger.warning`` goes to a rotated gateway log
    that is gone long before anyone asks what happened. So the only account of a lost
    working set was the user noticing it, which is how a loss of sixteen tabs went
    from happening to being understood with nothing in between.

    It is emitted on the DROPPED session's own log rather than on a gateway-wide one
    because that is where a reader goes: the question is always "what happened to this
    conversation", asked of the conversation. The counts put the one tab in context --
    one of sixteen that did not come back reads differently from one of one.

    ``kept`` is how many listed keys this restore DROPPED -- the shown-missing
    subset, this tab included. They are not gone from disk: the keys stay in the
    reopen seed and are re-read next boot. The entry says the tab was not SHOWN,
    which is the fact the user saw. (A remote-only seed that is expected to sit
    unrestored across boots is deliberately NOT in this count -- see
    ``_record_unrestored_open_tabs`` -- so a correctly-behaving system does not
    raise the notice.)
    """
    if not session_id:
        return
    _write(
        session_id,
        "session/unrestored",
        {"listed": int(listed), "restored": int(restored), "kept": int(kept)},
        src=_SRC_GATEWAY,
    )


def on_session_opened(
    session_id: str,
    *,
    agent: str = "",
    slot: str = "",
    model: str = "",
    model_requested: str = "",
    cwd: str = "",
    owner: str = "default",
    resumed: bool = False,
    parent_slot: str = "",
    parent_sid: str = "",
    depth: int | None = None,
    memory: str = "",
    app: str = "",
    channel: bool = False,
    workspace: str = "",
    previous_sid: str = "",
    previous_undecided: bool | None = None,
) -> None:
    """Create the crew log if this session has none, then echo its header.

    Called once per turn, because the per-turn session claim is where the ACP
    id becomes known -- but an entry is written only when there is something new
    to say: the crew log was just created, or this claim RE-ATTACHED to an existing
    conversation (a new gateway process taking over the same session id). A warm
    reuse of a session already carrying a crew log adds nothing, so it is silent.

    ``owner`` and ``agent`` are header fields, written once at create time and
    never rewritten. A resumed session id reuses its existing crew log and
    appends, so an agent or model switch that kept the conversation continues
    one log rather than starting a second one. ``model`` is not a header field
    in the storage schema, so it is carried on this entry instead.

    ``model`` is the id the backend CONFIRMED, and it is empty whenever that id
    is not known. It alone cannot say what the gateway chose: no tier resolved
    anything above the backend's own default, a chosen model was applied, or a
    chosen one never took effect and the backend's choice serves instead (a model
    this account cannot run is withheld before it is sent, and a ``set_model``
    that raises is logged and left alone). Each of the last two can end with an id
    here or without one.

    So ``model_requested`` records what the gateway SELECTED for the ALLOCATION
    that produced this session, and its presence is not conditioned on ``model``.
    The caller resolves that value once, hands it to the provider and retains it
    with the slot, because the turn that observes a session is not always the one
    that allocated it: an eager allocation can outlive a config change, and
    re-resolving at the first turn would record a model that session never used.
    Selection is not transmission either: the withhold happens inside the
    provider, so this field names the choice rather than a message the backend
    received. The pair is the record -- ``model`` states what serves the session,
    ``model_requested`` what was chosen -- and the entry infers nothing from the
    two. A difference between them is not by itself a refusal, because the backend
    serves the spelling it resolved; whether a choice was APPLIED is not something
    this entry knows, and a reader that needs it reads the provider's own outcome
    rather than comparing these strings. Absent ``model_requested`` means no tier
    resolved one, OR that this process did not observe the allocation (a re-attach
    carries provenance from a process that is gone) -- and on an entry written
    before the field existed it means nothing at all.

    ``parent_slot`` names the session that made this one through
    ``session_create`` (the slot's ``_created_by``), and ``parent_sid`` the
    creator's ACP session id as ``session_create`` froze it at mint (the slot's
    ``_created_by_sid``) -- the creator crew log that holds the call. The edge is
    written on the CHILD because that is the side that knows it: the creator is
    stamped on the slot at mint, before any turn, while the creator never learns
    the child's session id, which is assigned at the child's first turn. The sid
    is NOT read live here: a creator slot can be closed and replaced between the
    mint and the child's first turn, and a live read would cite the replacement's
    crew log in an entry that can never be corrected. Both empty means nobody
    created this session (a person's own tab, a fork) and no ``parent`` is
    written at all, so a fold can tell "no creator" from "creator unknown".

    ``depth`` is how many creators stand ABOVE this session: ``0`` for a session
    nobody created, and a child's creator's depth plus one. It is the caller's
    reading, resolved before the call and passed in rather than derived here,
    which is the whole point of the field. The creator chain lives across OTHER
    sessions' logs, so the only two ways to answer are to join those logs or to
    ask the tree a reader already holds in memory -- and this runs on the writer
    thread, where neither belongs. The caller is where the no-I/O read already
    happens (the same fold ``parent_slot`` itself can come from), so the depth
    arrives as a value and this function only records it.

    ``None`` -- the default -- writes no key, and that is the honest answer for
    every caller and every state that cannot establish the number: a writer that
    does not resolve one, and a session whose creator is KNOWN to exist while the
    tree could not place it. It is deliberately NOT inferred from ``parent_slot``
    being empty, which would be the obvious shortcut and is wrong: an empty
    creator covers both a session nobody made and one whose creator this entry
    may not cite, and writing ``0`` for the second would record a child as the
    root of its own tree in an entry nothing rewrites. A caller that knows there
    is no creator says ``0`` and says it on purpose.

    ``memory``, ``app`` and ``channel`` record WHAT KIND of session this log
    belongs to: the slot's memory mode verbatim, the app that owns it if one does,
    and whether its conversation is published to a messaging channel by a link or
    a mirror. They are written as one ``class`` object and only when ``memory`` is
    given, which makes that member the witness that the class was recorded -- so a
    reader can tell a session with nothing to declare from a log written before
    this existed, and must refuse rather than assume on the second.

    They belong on the record rather than in a live lookup because the question
    they answer -- may another session read this log -- is asked about sessions
    that have CLOSED, and a closed session has no slot left to ask. They are also
    facts and not a verdict: recording "readable" would freeze this build's reading
    of a rule into an entry that can never be corrected. The facts are true when
    the log is opened; a class a session ACQUIRES later (a channel link added
    mid-conversation) is not in them, so a reader that can also see the live
    session applies both and refuses on either.

    ``previous_sid`` names the crew log this SLOT was writing before, and it
    answers the continuity ``resumed`` cannot. ``resumed`` is true only when this
    claim re-attached to the same crew log; when the ACP session was instead torn
    down and a successor cold-started, the successor has a different id and
    therefore a different unit, and nothing in the record joined the two. So a
    ``create`` that is handed a DIFFERENT prior id writes ``previous {sid}``, and
    the comparison is made here rather than trusted from the caller: on the resume
    path the prior id and this one are the same store, and an edge pointing at
    itself would make a chain walker loop. Empty, or equal to this session, means
    no edge is written -- the slot's first crew log, and a predecessor the gateway
    could not name, are both "nothing to follow" rather than a store with an empty
    name.

    The edge itself is a citation and nothing more: this entry records which store
    came before, and no writer here touches that store. Closing the superseded
    store's own dangling turn and tool calls is a SEPARATE job, queued under that
    store's id so it runs behind whatever that store still owes -- see
    :func:`_repair_superseded`. Keeping the two apart is what lets the entry land
    at once while the repair waits for an ordering it cannot have yet.
    """
    if not session_id or not enabled():
        return
    # Read BEFORE the release below, which is what makes this the only place the
    # evidence still exists: a claim ends every turn this process believes is
    # running, so by the time the write job runs the record is already gone.
    live_at_claim = live_turn(session_id)
    # A claim ends every turn this process has no closer coming for. A turn whose
    # terminal event is already queued keeps its record, because the write job
    # owes the release and the file still shows that turn open until the entry
    # lands.
    _tracker().release_unclosed(session_id)
    # Latched on the FIRST attempt and read back on every later one. The decisions
    # below are derived from filesystem state this job itself changes: a retry after
    # the header landed but the entry did not finds ``exists`` true and ``created``
    # false, so an unlatched decision would flip to "nothing new to say" and skip
    # the entry it still owes -- permanently, and without counting the loss. It
    # holds the supersede edge too, which is a session id rather than a flag, so
    # the values are not all bools.
    announce: "dict[str, Any]" = {}

    def _job() -> None:
        created = False
        if _crew_log().CrewLog.exists(_KIND, session_id):
            # THE resume path, and the only caller that may repair. ``resumed``
            # means this claim re-attached to a conversation a DIFFERENT gateway
            # process was writing, so a turn left open in that file belongs to a
            # writer that is gone and closing it records what happened. A warm
            # reuse inside this process passes ``resumed=False`` and must not
            # repair: its turn may still be running. A reconnect after a cache
            # eviction never reaches here at all -- it goes through ``_handle``,
            # which opens without repair.
            #
            # A live turn of OUR OWN contradicts the claim. ``resumed`` is the
            # caller's belief that the writer is gone, and this process holding a
            # running turn for that id is direct evidence that it is not -- it is
            # us. Repairing then closes a turn that is still producing entries, and
            # the file ends up saying the turn completed as interrupted and then
            # completed again for real, with the same tool closed both unknown and
            # completed: a fold reads two outcomes for one turn and cannot tell
            # which happened. Local evidence beats the flag, so this degrades to a
            # reconnect. A writer in ANOTHER process is caught one layer down
            # instead of here: the repair is an append, so it takes that unit's
            # write ownership and is refused while another process holds it, and
            # this check is what covers the same-process race a cache eviction
            # produces -- which no kernel lock can see, both handles being ours.
            may_repair = bool(resumed) and not live_at_claim
            if resumed and live_at_claim:
                logger.warning(
                    "session log %s: opened as a resume while turn %d is still "
                    "running in this process, so the open turn is NOT repaired -- "
                    "closing it would record an outcome the turn never had and then "
                    "be followed by the rest of that turn",
                    session_id,
                    live_at_claim,
                )
            # A repair may close an unmatched `subagent/spawned` only for a child
            # with no outcome still coming, and `resumed` cannot answer that: it is
            # raised for an IN-PROCESS `session/load` too, so it does not even imply
            # a different process wrote this file, let alone that the children that
            # process spawned have stopped. The registry of running children is the
            # only thing that knows, and a process with none registered closes no
            # child at all.
            log = _crew_log().CrewLog.open(
                _KIND, session_id, repair=may_repair, child_gone=_child_gone_probe(session_id)
            )
            # Rebuild the attempt counts from what is already in the file, for
            # the two cases where memory cannot answer: a resume, whose counts
            # belong to a process that is gone, and a session whose map was
            # evicted by the bound. A warm reuse still HOLDS its counts, and
            # re-seeding it would rescan the whole file on the one thread that
            # serializes every session's appends -- once per open, against a file
            # that only grows.
            needs_seed = bool(resumed) or not _tracker().knows_attempts(session_id)
            if needs_seed:
                _seed_attempts(session_id, log)
            if slot:
                # CRASH RECOVERY. The edge is DURABLE and the repair job is not: the
                # opening entry carrying ``previous.sid`` is an append, while the
                # repair rides the in-memory buffer, so a crash between the two loses
                # the repair and a superseded id is never resumed to re-queue it. A
                # re-attach is the one moment a later process holds this crew log
                # again and can read its OWN edge back, so the repair is recovered
                # from the file rather than from state that did not survive.
                #
                # This log's own entry, never the caller's ``previous_sid``. The latch
                # below refuses the caller's value on a re-attach for a stated reason
                # -- the unit it names may be this same one or an unrelated one that
                # is still LIVE -- and that reason does not apply here: this value was
                # written by an earlier attempt of THIS session's opening entry, which
                # verified it against the slot before writing it.
                #
                # Gated on ``slot`` because the repair verifies the candidate against
                # that slot and refuses without one, and nothing is lost by the gate:
                # an edge is only ever written for two crew logs KNOWN to be one
                # slot's, so a session with no slot has no durable edge to recover.
                #
                # Latched like every other decision in this job, so a retry acts on
                # the first attempt's reading rather than re-deriving from a file the
                # attempt before it may have changed.
                recovered = announce.setdefault(
                    "recovered_previous",
                    _durable_previous(session_id),
                )
                if recovered:
                    logger.info(
                        "session log %s: recovered the superseded crew log %s from "
                        "this log's own session/opened entry, so its interrupted "
                        "tail is repaired even though the queued repair did not "
                        "survive",
                        session_id,
                        recovered,
                    )
        else:
            log = _crew_log().CrewLog.create(
                _KIND,
                session_id,
                owner=owner or "default",
                agent=agent or _DEFAULT_AGENT,
                slot=slot or None,
                cwd=cwd or None,
            )
            created = True
        _remember(session_id, log)
        # The class as observed for THIS turn. The caller reads it off the live slot
        # on every turn rather than only the first, which is what lets a class the
        # session acquires LATER reach the log at all.
        observed = (memory, app, bool(channel), workspace) if memory else None
        # Latched on the FIRST attempt and read back on every later one, exactly like
        # ``owed`` below and for the same reason: the decision is derived from
        # filesystem state this job itself changes, so a retry after the header
        # landed reads ``exists`` true and ``created`` false, and an unlatched edge
        # would be dropped there -- silently, and permanently, while the entry it
        # belongs to still gets written. The latch holds the ID rather than a flag,
        # so the retry writes the edge the first attempt decided on, and an empty
        # string is a latched "no edge" that nothing downstream re-tests.
        #
        # ``created`` is part of the condition, not just the ``previous_sid``
        # comparison. A re-attach has a store already, so the unit the caller names
        # is either this same one or an unrelated one that may still be LIVE, and
        # repairing that is how a running turn gets an outcome it never had.
        superseded = announce.setdefault(
            "superseded",
            (
                previous_sid
                if (
                    created
                    and previous_sid
                    and previous_sid != session_id
                    and _candidate_is_same_slot(previous_sid, slot)
                )
                else ""
            ),
        )
        # Buffered beside the id above and gated the same way, because it answers the
        # same question: what this entry says about the slot's earlier store. It is
        # only meaningful when NOTHING was named -- a named edge already says the
        # predecessor is known -- so a caller passing both leaves the id winning.
        #
        # THREE values, not two, and the third is the one that keeps this honest. A
        # caller that looked reports what it found; a caller that never looked passes
        # nothing, and this entry then says nothing either way. Collapsing the last
        # two would make the emitter state a conclusion on behalf of a caller that
        # never reached one, which is the same defect as reading an absent key as a
        # conclusion, written from the other side.
        determined = announce.setdefault(
            "previous_determined", bool(created and previous_undecided is not None)
        )
        unresolved = announce.setdefault(
            "previous_unresolved",
            bool(created and previous_undecided is True and not superseded),
        )
        if not announce.setdefault("owed", created or bool(resumed)):
            # Nothing new to say about the OPENING, which is what this entry
            # records. A class that has moved since the last statement of it is
            # something new to say about the session, and it goes out as its own
            # entry rather than as a second opening entry: the opener is read as
            # what the session was CREATED as, and a fold over the transitions
            # after it is what gives a reader the whole life.
            _note_class_change(session_id, log, observed)
            return
        data: dict[str, Any] = {
            "agent": agent or _DEFAULT_AGENT,
            "slot": slot,
            "model": model,
            "cwd": cwd,
            "owner": owner or "default",
            "resumed": bool(resumed),
        }
        if model_requested:
            # Written whenever the gateway resolved one, and never conditioned on
            # ``model``. Both guards tried before this inferred the application
            # outcome from the two ids and both lost the record: suppressing on a
            # DIFFERENCE reported an honoured request as unconfirmed, because the
            # backend serves the spelling it resolved; suppressing on a KNOWN
            # ``model`` dropped the request whenever a refused pin left the session
            # on a concrete backend default rather than the auto sentinel, which is
            # ordinary operation. Recording the request outright costs one short
            # string and cannot lose the requested/served pair. The entry states two
            # facts and infers nothing: what the gateway asked for, and what serves.
            data["model_requested"] = model_requested
        if superseded:
            # No ``slot`` inside: it is the slot in ``data.slot``, and repeating it
            # would invite a reader to trust a second copy of one fact.
            data["previous"] = {"sid": superseded}
        elif unresolved:
            # A predecessor EXISTS and could not be named. Recorded BESIDE the
            # citation rather than as an empty one, because ``previous.sid`` is
            # required and a citation naming nothing would be a weaker promise for
            # every reader of it. This is a third thing from the two a reader already
            # tells apart: a named edge, and neither key, which means this log starts
            # the slot's chain. Without it this log would read as that chain start,
            # and a fold ranking the slot's logs would pass over it and elect the log
            # before it -- the citation this read refused to guess, written anyway by
            # another route and frozen into an append-only entry.
            data["previous_undecided"] = True
        elif created and determined and not previous_sid:
            # The caller LOOKED and there is no predecessor: this is the slot's first
            # store. Stated rather than left to the absence of the other two keys,
            # because a store written before any of these keys existed also has none of
            # them -- and ITS omission may equally be a predecessor the gateway of the
            # day failed to name. Only a store that says this may be passed over when a
            # later reader ranks the slot's stores. A named predecessor that was
            # REJECTED for belonging to another slot says nothing either way, so it
            # falls through to writing no key at all.
            #
            # ``determined`` is what makes the claim answerable for, and it is not a
            # formality: a caller that hands over an id it read from one source and
            # never established whether a predecessor exists would otherwise have this
            # entry declare, in an append-only record, that the slot has none. An empty
            # id from such a caller means "I have nothing to give you", which is the
            # unexplained silence this key exists to be distinguished FROM.
            data["previous_none"] = True
        if parent_slot:
            # Written only when there IS a creator, and ``sid`` only when the
            # creator still had a live handle: an empty string in either place
            # would read as a creator with an empty name.
            parent: dict[str, str] = {"slot": parent_slot}
            if parent_sid:
                parent["sid"] = parent_sid
            data["parent"] = parent
        if isinstance(depth, int) and not isinstance(depth, bool) and depth >= 0:
            # Written only for a reading the caller actually made. ``None`` is the
            # ordinary absence and costs nothing to check; the type and sign checks
            # cover a caller handing over something that is not a depth at all --
            # a bool, which is an int in Python and would land as 0 or 1, or a
            # negative, which no walk of a tree can produce. Each of those would be
            # a number in an append-only entry that no later read can correct, and
            # the alternative to refusing them here is a counting reader learning to
            # distrust the field. A depth that cannot be stated is simply absent,
            # which every reader of this field already has to handle.
            data["depth"] = depth
        if memory:
            # The class of session this log belongs to, as facts. Written only when
            # the caller supplied ``memory``, which every live slot has: that makes
            # one member the witness that the class was recorded at all, so a reader
            # can tell "recorded, and nothing applies" from "not recorded", and an
            # object that is never empty carries that distinction without a flag
            # saying so. A caller that passes no memory mode -- a test fixture, an
            # older build's log -- records no class, and a reader that needs one
            # must refuse rather than read the absence as "nothing applies".
            #
            # These live HERE, on the opening entry, because the crew log is the
            # authoritative record of a session and the question they answer is
            # asked about sessions that have CLOSED. A gate that read them from the
            # live slot instead can answer only for a session still being served,
            # which is the one case it does not need.
            #
            # Facts, not a verdict: what owns the session, whether it keeps memory,
            # whether its conversation is published to a channel. A verdict recorded
            # here would be this build's reading of a rule that may change, and the
            # entry cannot be rewritten.
            session_class: dict[str, Any] = {"memory": memory}
            if app:
                session_class["app"] = app
            if channel:
                session_class["channel"] = True
            if workspace:
                session_class["workspace"] = workspace
            data["class"] = session_class
        log.append("session/opened", data, src=_SRC_GATEWAY)
        # DURABILITY FIRST, THEN MEMORY. The session tree is a projection folded in
        # memory and advanced here, at commit, so no reader ever has to re-derive it
        # from disk; this line is the only thing that keeps it current. It runs AFTER
        # the append, never before, so the disk can never hold an edge the memory
        # lacks -- and if it did run first, an append that then failed would leave a
        # creator edge that no log records.
        #
        # This gateway is the store's only writer (a pod or the internal gateway has
        # its own data home), which is what makes an in-process projection complete
        # rather than a guess about somebody else's writes.
        #
        # Never raises: ``record_opened`` swallows its own failures, because an append
        # that already succeeded must not be reported as failed on account of the
        # memory image of it, and a projection that missed a record self-heals through
        # the tail replay on the next cold start.
        _record_session_tree_edge(session_id, slot, log, parent_slot, superseded)
        if observed is not None:
            # This line states the class, so it is also what later turns compare
            # themselves against. Seeding it here is what stops the first warm turn
            # from restating an unchanged class as though it had moved.
            _latch_class(session_id, observed)
        # The caller's edge when this open wrote one, else the durable edge recovered
        # from this log's own opening entry on a re-attach. ONE submission site for
        # both, so the guards, the bucket and the ceiling exemption cannot differ
        # between a first pass and a recovery.
        repair_target = superseded or str(announce.get("recovered_previous") or "")
        if repair_target:
            # AFTER the append, so the repair is queued exactly once: a failed
            # ``session/opened`` is retained and this job runs again, and queueing
            # above would queue one repair per attempt.
            _queue_tail_repair(repair_target, slot)

    def _flag_creation_failed() -> None:
        # The creating record died with no crew log file behind it: no later append
        # for this session can land, so _handle stops treating its absence as a
        # policy no-op and later discards are COUNTED instead of silently dropped.
        with _lock:
            _creation_failed.add(session_id)

    from kiro_crew.crew_log.writer import WriteJob

    _hand_over(
        session_id,
        WriteJob.opening(_job, "opening a crew log", on_drop=_flag_creation_failed),
    )
