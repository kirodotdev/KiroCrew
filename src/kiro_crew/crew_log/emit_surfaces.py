"""Surface facts: the records a session keeps of its ledgers, boards, panels and dashboards.

A session's own ledger updates (``ledger/recorded``, ``radar/recorded``), an outside object's
state as a producer observed it, a work-board mutation, a published crew webview, an accepted or
refused dashboard value and a dashboard's instance history, and the change cards and guides the
agent offered in the conversation -- with the ``*_entry_fits`` checks a caller runs before it
asks for one of them.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from kiro_crew.crew_log.emit_runtime import _handle, _submit, _write, live_turn
from kiro_crew.crew_log.emit_state import _SRC_GATEWAY, _note_eager, enabled
from kiro_crew.crew_log.emit_text import _clip, _entry_line_fits, _safe_text


def on_ledger_recorded(session_id: str, data: dict[str, Any]) -> None:
    """Append ONE ``ledger/recorded`` entry -- a session's own durable work state.

    The write half of the session ledger. Every field the caller set rides on this
    single entry, including the event that explains a phase change, so the rule
    that a phase never moves without a logged reason is a property of one append
    rather than of two writes that a crash can separate.

    Queued through the same writer as every other entry, deliberately. The ledger
    could not open the file itself: an append takes that unit's WRITE OWNERSHIP,
    and while the emitter holds this session's handle a second handle in this
    process is refused -- so a ledger that wrote around the emitter would fail for
    exactly the sessions that are running. Going through the writer also keeps this
    entry ordered against the turn it was recorded inside.

    The caller is the one that establishes the session has a crew log to write to;
    this is the ordinary ``_write``, so a session without one is a policy no-op
    here and the refusal belongs where a user can be told about it.
    """
    _write(session_id, "ledger/recorded", data, src=_SRC_GATEWAY)


def ledger_entry_fits(data: dict[str, Any]) -> bool:
    """Whether *data* would fit one ``ledger/recorded`` entry.

    Beside the write rather than inside it, because the two answers have different
    owners. ``_write`` is a policy no-op for a session with no crew log and refuses an
    oversized entry by COUNTING it -- both correct there, since it serves callers that
    cannot act on either. The ledger's caller can act on this one: an entry over the
    ceiling by construction can never land, so the record it would report as taken
    never exists, and only that caller can turn the refusal into an answer a user sees.

    It asks the same question the append will, through the same serializer with the
    same entry type and src, so the two cannot disagree about what fits. Not a
    reservation: a later entry does not make this one smaller, and nothing else
    consumes the budget.
    """
    return _entry_line_fits("ledger/recorded", data, src=_SRC_GATEWAY)


def on_object_observed(
    session_id: str,
    *,
    producer: str,
    kind: str,
    target: str,
    fingerprint: str,
    facts: "Mapping[str, Any]",
    observed_at: float,
) -> None:
    """Record the state of an object outside the session, as *producer* observed it.

    The producer half of the crew log's external-state record. A structured
    monitor's probe computes a canonical snapshot of the pull request it watches
    and, before this, threw that snapshot away once the wake was decided. This
    appends it into the OWNER session's log -- the session the monitor works for --
    so "what state is that pull request in" becomes a typed read beside the holder
    fold's "which session holds it", instead of a text search over whatever an
    agent happened to say about it.

    *producer* is refused outside
    :data:`~kiro_crew.crew_log.entry_types.OBJECT_PRODUCERS`, and refused HERE
    rather than coerced. The value is the point of the entry: a reader trusts a
    measured record because it can see which mechanism measured it, and a producer
    coerced to some default would attribute the record to a mechanism that did not
    make it. The ``ValueError`` is a programming error surfaced at the site that
    made it; the registry's closed enum behind this is the guard a caller cannot
    skip by writing around this function.

    The caller decides WHEN: one call per change of the probe's fingerprint, never
    one per poll, so the log holds distinct states rather than a heartbeat.

    *facts* is recorded verbatim. When the whole line would cross the store's
    ceiling -- a review host reporting hundreds of long check identities can do
    it -- the largest members are removed until it fits and are named in
    ``facts_omitted``, so the record is short by a NAMED part rather than lost
    whole or silently trimmed. Recording nothing was rejected: the change
    happened, and a reader that finds no entry cannot tell "unchanged" from
    "did not fit".
    """
    from kiro_crew.crew_log.entry_types import OBJECT_PRODUCERS

    if producer not in OBJECT_PRODUCERS:
        raise ValueError(
            f"object/observed producer must be one of {list(OBJECT_PRODUCERS)}, not {producer!r}"
        )
    # Off is free: the fit loop below serializes the snapshot and reaches the
    # storage package, work no disabled launch should do on the event loop.
    if not session_id or not enabled():
        return
    snapshot: dict[str, Any] = dict(facts)
    data: dict[str, Any] = {
        "producer": producer,
        "kind": str(kind),
        "target": str(target),
        "fingerprint": str(fingerprint),
        "facts": snapshot,
        "observed_at": float(observed_at),
    }
    omitted: list[str] = []
    while snapshot and not _entry_line_fits("object/observed", data, src=_SRC_GATEWAY):
        largest = max(
            snapshot,
            key=lambda name: len(json.dumps(snapshot[name], ensure_ascii=True, default=str)),
        )
        del snapshot[largest]
        omitted.append(largest)
        data["facts_omitted"] = omitted
    _write(session_id, "object/observed", data, src=_SRC_GATEWAY)


def on_radar_recorded(session_id: str, data: dict[str, Any]) -> None:
    """Append ONE ``radar/recorded`` entry -- an Issue Radar crew's own ledger update.

    The write half of the crew ledger. Every field the caller set rides on this single
    entry, including the event that explains a phase change and the skip row that
    indexes a pass, so the rules that a phase never moves without a logged reason and
    that an issue is never skipped without being indexed are properties of one append
    rather than of three writes a crash can separate.

    Queued through the same writer as every other entry, for the reason the session
    ledger gives: an append takes the unit's WRITE OWNERSHIP, and while the emitter
    holds a running session's handle a second handle in this process is refused, so a
    ledger that wrote around the emitter would fail for exactly the crews that are
    working. Going through the writer also keeps the entry ordered against the turn
    it was recorded inside.

    The caller establishes that the crew's session has a crew log to write to; this
    is the ordinary ``_write``, so a session without one is a policy no-op here and the
    refusal belongs where the crew can be told about it.
    """
    _write(session_id, "radar/recorded", data, src=_SRC_GATEWAY)


def radar_entry_fits(data: dict[str, Any]) -> bool:
    """Whether *data* would fit one ``radar/recorded`` entry.

    Asked beside the write rather than inside it, because the caller can act on the
    answer and ``_write`` cannot: an entry over the ceiling by construction can never
    land, so the update it would report as taken never exists. Same serializer, same
    entry type and src as the append, so the two cannot disagree about what fits.
    """
    return _entry_line_fits("radar/recorded", data, src=_SRC_GATEWAY)


def work_entry_fits(data: dict[str, Any]) -> bool:
    """Whether a ``work/recorded`` entry carrying *data* fits one log line.

    The work ledger asks this BEFORE its own store commits, with the widest
    payload the commit can produce, so a mutation whose record could not be
    written is refused whole and no cache byte is touched: the store's caps
    refuse and never truncate, and this keeps that rule for the one bound the
    store cannot see, the line limit.
    """
    return _entry_line_fits("work/recorded", data, src=_SRC_GATEWAY)


def on_panel_published(session_id: str, data: dict[str, Any], *, timeout: float = 5.0) -> bool:
    """One publish of a crew's webview, appended to its DM session log, acknowledged.

    The write half of the crew panel. A publish REPLACES the whole panel, so the
    entry carries the document whole rather than the fields that changed: a panel
    describes one cycle's state, and a partial update would leave last cycle's rows
    beside this cycle's counters with nothing marking which is which. That is the
    one way this differs from the session ledger's entry, whose absent field means
    unchanged.

    WAITS, like ``on_work_recorded``, though not because this record is the panel's
    only one -- the file is, and the route has already written it. It waits so the
    caller learns whether THIS publish's history row landed, and so an append the
    waiter gave up on cannot land later. It returns ``True`` once the writer has
    appended, and ``False`` when the append was refused, permanently dropped, or not
    started within *timeout* seconds. ``False`` is FINAL -- an entry the waiter gave
    up on is abandoned and will not land later even if the writer retries the job.
    Without that, a slow store could report the row missing and commit it anyway, so
    one publish would end up with two history rows. An append already STARTED is waited to
    completion however long the store takes, and its outcome reported truthfully
    rather than guessed.

    Queued through the same writer as every other entry, for the reason the session
    ledger gives: an append takes the unit's WRITE OWNERSHIP, and while the emitter
    holds a running session's handle a second handle in this process is refused, so
    a store that wrote around the emitter would fail for exactly the crews that are
    publishing. Going through the writer also orders the entry against the turn the
    crew published inside.

    *session_id* is the PUBLISHING session's, which is the member's own DM session:
    the panel tool is mounted nowhere else, so the unit this lands in belongs to
    that member's slot and the slug-keyed read finds it without a binding of its
    own. A session with no crew log answers ``False`` here, and the refusal belongs
    where the crew can be told about it.
    """
    if not session_id or not enabled():
        return False
    landed = threading.Event()
    gate = threading.Lock()
    outcome = {"ok": False, "abandoned": False}

    def _job() -> None:
        with gate:
            # A waiter that gave up has abandoned the entry: it must not land later,
            # or a publish the crew was told failed would reappear on the next fold.
            # Under the gate the two outcomes cannot cross.
            if outcome["abandoned"]:
                return
            log = _handle(session_id)
            if log is None:
                return
            entry = log.append("panel/published", data, src=_SRC_GATEWAY)
            # The panel fold spans replacement sessions, and a unit header's clock can
            # step BACKWARD, which would fold a retired session's publish last and make
            # it the current panel with history built against the wrong predecessor.
            # Publish the causal order only after this append has really landed.
            from kiro_crew import session_ledger

            session_ledger.note_panel_unit_recorded("", session_id)
            # AFTER the order is recorded, never before. The fold the wake triggers reads
            # that order to decide which unit applies last, so a wake enqueued first can
            # be folded on a thread that still sees this unit unordered -- and the panel
            # fold takes the newest entry whole, so it would serve a retired session's
            # panel as the current one.
            _note_eager(entry, "panel/published", session_id, data)
            outcome["ok"] = True

    _submit(_job, "appending panel/published", session_id, after=landed.set)
    if landed.wait(timeout):
        return outcome["ok"]
    with gate:
        if outcome["ok"]:
            return True
        outcome["abandoned"] = True
    return False


def _append_dashboard(
    session_id: str, entry_type: str, data: dict[str, Any], timeout: float
) -> bool:
    """Append one dashboard entry and report whether it landed. Shared by both types.

    ONE helper for the accepted write and the refusal, because the two differ only
    in their entry type and must not differ in anything else: a refusal that landed
    while the write it refused did not, or the reverse, would leave the mistake book
    and the dashboard disagreeing about what happened.

    WAITS, like :func:`on_panel_published`, and for a sharper reason than that one
    has: this log IS the dashboard's record. An agentic value lives nowhere else --
    there is no file beside it, by design, because no host Python computes a
    dashboard value -- so a caller told its write succeeded when the append did not
    land would see the cell stay empty with nothing saying why. ``False`` is FINAL:
    an entry the waiter gave up on is abandoned and will not land later, so one
    write cannot end up as two values.

    Queued through the same writer as every other entry, for the reason the session
    ledger gives: an append takes the unit's WRITE OWNERSHIP, and while the emitter
    holds a running session's handle a second handle in this process is refused.
    Going through the writer also orders the entry against the turn the crewmate
    wrote inside, which is what makes the mistake book's own ordering true.
    """
    if not session_id or not enabled():
        return False
    landed = threading.Event()
    gate = threading.Lock()
    outcome = {"ok": False, "abandoned": False}

    def _job() -> None:
        with gate:
            # A waiter that gave up has abandoned the entry: it must not land later,
            # or a write the crewmate was told failed would reappear on the next
            # fold. Under the gate the two outcomes cannot cross.
            if outcome["abandoned"]:
                return
            log = _handle(session_id)
            if log is None:
                return
            entry = log.append(entry_type, data, src=_SRC_GATEWAY)
            _note_eager(entry, entry_type, session_id, data)
            outcome["ok"] = True

    _submit(_job, f"appending {entry_type}", session_id, after=landed.set)
    if landed.wait(timeout):
        return outcome["ok"]
    with gate:
        if outcome["ok"]:
            return True
        outcome["abandoned"] = True
    return False


def on_dashboard_agentic(session_id: str, data: dict[str, Any], *, timeout: float = 5.0) -> bool:
    """One accepted agentic dashboard value, appended to the crewmate's DM log.

    *data* is the ``dashboard/agentic_value`` payload the write path has already
    checked against the live manifest: the field exists, it is agentic, and the
    value is of the declared type. This emitter does not re-check -- the caller is
    the only party holding the manifest -- and the FOLD re-checks on read, which is
    where a line off disk needs it.

    Each write replaces ONE field. There is no whole-document form of this entry
    and deliberately so: the fields are independent cells, so a crewmate that
    learns one number writes that one rather than restating the other twenty-three.
    """
    # THE TYPE IS A LITERAL HERE, not the imported constant, and that is the
    # convention this module already follows (``on_panel_published`` appends
    # ``"panel/published"`` the same way). It is what makes the declared vocabulary
    # and the APPENDED vocabulary provably equal: the ratchet in
    # ``test_crew_log_types`` reads the appended set out of this file's own syntax,
    # so a type reaching ``log.append`` through a variable is a type it cannot see
    # -- and a declaration no site provably produces is exactly what that test
    # exists to refuse.
    return _append_dashboard(session_id, "dashboard/agentic_value", data, timeout)


def on_dashboard_refused(session_id: str, data: dict[str, Any], *, timeout: float = 5.0) -> bool:
    """One refused agentic write -- or one correction of an earlier refusal.

    TWO shapes on one type, which the fold tells apart by which keys are present: a
    refusal carries ``code``, and a correction carries ``corrects`` and the field
    that worked. One type because they are one story -- the mistake and its answer
    -- and a reader that had to join two types to tell whether a mistake was ever
    fixed would be joining them on exactly the thing that is hard to get right.

    BEST-EFFORT at the call site, unlike the accepted write: by the time this is
    called the refusal is already on its way back to the caller, which is the part
    that matters. A log that is off costs the mistake book this row and nothing
    else, so a caller treats ``False`` as "not recorded" and never as "not refused".
    """
    # A literal, for the reason its sibling above spells out.
    return _append_dashboard(session_id, "dashboard/agentic_refused", data, timeout)


def dashboard_entry_fits(entry_type: str, data: dict[str, Any]) -> bool:
    """Whether *data* would fit one entry of *entry_type*.

    Asked beside the write rather than inside it, for :func:`panel_entry_fits`'
    reason: the caller can act on the answer and ``_write`` cannot. An agentic value
    over the ceiling can never land, so a crewmate told its write succeeded would
    watch the cell stay empty -- and the refusal it should have had instead is one
    the mistake book can teach.
    """
    return _entry_line_fits(entry_type, data, src=_SRC_GATEWAY)


def panel_entry_fits(data: dict[str, Any]) -> bool:
    """Whether *data* would fit one ``panel/published`` entry.

    Asked beside the write rather than inside it, because the caller can act on the
    answer and ``_write`` cannot: an entry over the ceiling by construction can
    never land, so the panel it would report as published never exists. The store's
    own byte ceiling bounds the payload, and this bounds the one thing the store
    cannot see -- the whole serialized line, envelope included. Same serializer,
    same entry type and src as the append, so the two cannot disagree about what
    fits.
    """
    return _entry_line_fits("panel/published", data, src=_SRC_GATEWAY)


def on_dashboard_instance_changed(session_id: str, data: dict[str, Any]) -> None:
    """Append ONE ``dashboard/instance_changed`` entry -- a crewmate's dashboard history.

    The history half of the dynamic dashboard instance. The current value is a file
    under the member's own space and the route has already written it, so this append
    is the HISTORY: the instance's change log is a fold over these entries.

    Does NOT wait, which is the difference from ``on_panel_published`` and is decided by
    what the caller can do with the answer. The instance store's caller is told the
    change landed because the record file landed; the entry is the record of HOW it got
    there, and a caller that cannot undo the committed version has nothing to do with
    "the history row is still queued". The store flushes once after the append so the
    row is durable by the time the call returns in the ordinary case.

    The entry carries what changed and never the page, so it is bounded by construction
    and no size check is needed beside it -- unlike the ledger's and the panel's, whose
    payloads are caller-sized.

    *session_id* is the crewmate's own DM session, the unit a dashboard change belongs
    to. A session with no crew log is a policy no-op here, as it is for every other
    ``_write``.
    """
    _write(session_id, "dashboard/instance_changed", data, src=_SRC_GATEWAY)


#: A card title or reason is display text a person reads, never an identity, so a
#: bounded slice of it is enough for a reader to name the card.
_CARD_TEXT_MAX = 300

_CARD_ID_MAX = 64


def _card_id(value: Any) -> str:
    return value if isinstance(value, str) and 0 < len(value) <= _CARD_ID_MAX else ""


def on_card_proposed(
    session_id: str,
    *,
    slot: str,
    card_id: str,
    kind: str,
    title: str,
    risk: str = "",
    revision: int = 1,
    mid: str = "",
) -> None:
    """The agent proposed a change card in this session's conversation.

    Written at the point the proposal landed in the transcript, so the log orders it
    against the tool call that made it. ``mid`` is the transcript row the card is
    drawn at: the row and this entry are the same fact in the two records, and the
    id is what lets a reader join them. Names only -- a card's parameters are never
    recorded, and a secret card has no value field to record in the first place.
    """
    cid = _card_id(card_id)
    if not cid:
        return
    data: dict[str, Any] = {
        "slot": slot,
        "card_id": cid,
        "kind": str(kind)[:64],
        "title": _clip(_safe_text(title), _CARD_TEXT_MAX),
        "revision": int(revision) if isinstance(revision, int) and revision > 0 else 1,
    }
    if risk:
        data["risk"] = str(risk)[:32]
    turn = live_turn(session_id) if session_id else 0
    if turn:
        data["turn"] = turn
    if mid:
        data["mid"] = str(mid)[:64]
    _write(session_id, "card/proposed", data, src=_SRC_GATEWAY)


def on_card_finished(session_id: str, *, card_id: str, status: str, revision: int = 1) -> None:
    """A change card reached a finished status: applied, undone, cancelled, expired...

    One entry per status the card ENTERS, so a failed apply followed by a successful
    retry is two entries and the newest is the card's outcome. No turn: a card is
    confirmed by a person, usually between turns, and the turn that proposed it is
    already on ``card/proposed``.
    """
    # Lazy, like OBJECT_PRODUCERS above: this module is the boot-path import gate.
    from kiro_crew.crew_log.entry_types import CARD_FINISHED_STATUSES

    cid = _card_id(card_id)
    # Refused rather than coerced: the card store's finished statuses are the one
    # vocabulary, and a value outside it is a bug at the call site.
    if not cid or status not in CARD_FINISHED_STATUSES:
        return
    data = {
        "card_id": cid,
        "status": status,
        "revision": int(revision) if isinstance(revision, int) and revision > 0 else 1,
    }
    _write(session_id, "card/finished", data, src=_SRC_GATEWAY)


def on_goal_recorded(
    session_id: str,
    data: dict[str, Any],
    *,
    on_settled: "Callable[[bool], None] | None" = None,
) -> None:
    """A crewmate's goal record changed: append its snapshot to the crewmate's DM log.

    *data* is ``{goals, milestones}`` as ``dashboard.crewmate_feed`` builds it. The
    ``goal`` fold keeps the newest one whole, which is what a Dashboard page binds.
    *on_settled* is handed True only once the append committed.
    """
    _write(session_id, "dashboard/goal_recorded", data, src=_SRC_GATEWAY, on_settled=on_settled)


def on_waiting_changed(
    session_id: str,
    data: dict[str, Any],
    *,
    on_settled: "Callable[[bool], None] | None" = None,
) -> None:
    """What a crewmate waits on the person for changed: append the new list.

    *data* is ``{items}`` from ``dashboard.member_waiting.waiting_items``. The
    ``waiting`` fold keeps the newest one whole. *on_settled* is handed True only
    once the append committed.
    """
    _write(session_id, "dashboard/waiting_changed", data, src=_SRC_GATEWAY, on_settled=on_settled)


def on_guide_offered(
    session_id: str,
    *,
    slot: str,
    guide_id: str,
    actions: Sequence[str],
    mid: str = "",
) -> None:
    """The agent offered a guide in this session's conversation.

    ``actions`` are the registered action ids only, never their parameters. ``mid``
    joins the entry to its transcript row, as on ``card/proposed``.
    """
    gid = _card_id(guide_id)
    if not gid:
        return
    data: dict[str, Any] = {
        "slot": slot,
        "guide_id": gid,
        "actions": [str(a)[:64] for a in actions if isinstance(a, str)][:16],
    }
    turn = live_turn(session_id) if session_id else 0
    if turn:
        data["turn"] = turn
    if mid:
        data["mid"] = str(mid)[:64]
    _write(session_id, "guide/offered", data, src=_SRC_GATEWAY)


def on_guide_started(session_id: str, *, guide_id: str) -> None:
    """The person pressed Start on an offered guide and a tab took it over."""
    gid = _card_id(guide_id)
    if gid:
        _write(session_id, "guide/started", {"guide_id": gid}, src=_SRC_GATEWAY)


def on_guide_finished(
    session_id: str, *, guide_id: str, status: str, reason: str | None = None
) -> None:
    """A guide ended: completed, cancelled (by the person, the agent or a closed
    conversation) or expired. *reason* is kept only when it is a known one."""
    from kiro_crew.crew_log.entry_types import GUIDE_FINISHED_REASONS, GUIDE_FINISHED_STATUSES

    gid = _card_id(guide_id)
    if not gid or status not in GUIDE_FINISHED_STATUSES:
        return
    data: dict[str, Any] = {"guide_id": gid, "status": status}
    if reason in GUIDE_FINISHED_REASONS:
        data["reason"] = reason
    _write(session_id, "guide/finished", data, src=_SRC_GATEWAY)


def on_work_recorded(session_id: str, data: dict[str, Any], *, timeout: float = 5.0) -> bool:
    """One work-board mutation, appended to the ACTING session's log, acknowledged.

    *data* is the ``work/recorded`` payload the work ledger already validated
    against its own caps and against the declared type. Unlike the other
    emitters this one WAITS: it returns ``True`` once the writer has appended the
    entry, and ``False`` when the append was refused, permanently dropped, or
    not started within *timeout* seconds. ``False`` is final: an entry the
    waiter gave up on is abandoned and will not land later even if the writer
    retries the job, so the caller's answer and the record cannot diverge. An
    append the writer had already STARTED is waited to completion, however long
    the store takes: its outcome is then reported truthfully rather than guessed.
    The work ledger is a projection of these entries, so its routes report
    success only on ``True``.
    """
    if not session_id or not enabled():
        return False
    landed = threading.Event()
    gate = threading.Lock()
    outcome = {"ok": False, "abandoned": False}

    def _job() -> None:
        with gate:
            # A waiter that gave up has abandoned the entry: it must not land
            # later, or a write the caller was told failed would come back on
            # the next rebuild. Under the gate the two outcomes cannot cross.
            if outcome["abandoned"]:
                return
            log = _handle(session_id)
            if log is None:
                return
            entry = log.append("work/recorded", data, src=_SRC_GATEWAY)
            # The work fold spans replacement sessions, so header wall clocks are
            # not a causal order. Publish only after this append has really landed.
            from kiro_crew import session_ledger

            session_ledger.note_work_unit_recorded(str(data.get("by") or ""), session_id)
            # AFTER the order is recorded, for the reason the panel emitter gives: the
            # fold this wake triggers reads that order.
            _note_eager(entry, "work/recorded", session_id, data)
            outcome["ok"] = True

    _submit(_job, "appending work/recorded", session_id, after=landed.set)
    if landed.wait(timeout):
        return outcome["ok"]
    with gate:
        if outcome["ok"]:
            return True
        outcome["abandoned"] = True
    return False
