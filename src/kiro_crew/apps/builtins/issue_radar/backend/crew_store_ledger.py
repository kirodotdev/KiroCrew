"""A crew's ledger: the folded reads and the appends that record its work.

The work items, events and passes folded from the crew log, the repository's shared
skip index, and the two writes -- a work item's progress and a crew-level
checkpoint -- each one append under the crew's write lock. Callers reach every name
here through :mod:`kiro_crew.apps.builtins.issue_radar.backend.crew_store`.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from kiro_crew.config.paths import data_home
from kiro_crew.constants import env_file_display
from kiro_crew.crew_log.entry_types import RADAR_CLEARABLE_FIELDS, RADAR_NUMBER_BOUNDS

from .crew_store_carry import _carry_legacy_forward, _carry_pending, _legacy_preview
from .crew_store_log import (
    _APPEND_FLUSH_SECONDS,
    _ENTRY_SRC,
    FOLD_NAME,
    LEDGER_ENTRY_TYPE,
    CrewLedgerEntryTooLarge,
    CrewLedgerNotRecorded,
    CrewLedgerUnavailable,
    _crew_write_lock,
    _fold_checkpoint,
    _landed_since,
    _projection,
    _undrained,
    crew_log_units,
)
from .crew_store_records import (
    CREW_LEVEL_EVENT_KIND,
    EDITING_PHASES,
    EVENT_KINDS,
    PHASES,
    TERMINAL_PHASES,
    CrewStoreError,
    _finite_int,
    coerce_skip_scope,
    list_crews,
    logger,
)
from .crew_store_unit_order import _record_unit_order


def read_ledger(
    owner: str,
    repo: str,
    crew_id: str,
    root: Path | None = None,
    *,
    live_session_id: str = "",
) -> dict[str, Any]:
    """*crew_id*'s whole ledger -- the ``radar`` projection's value.

    ``items`` newest progress first, ``events`` newest first, ``skips`` keyed by
    ``str(number)`` (this crew's own passes), ``phase_lines`` per item, ``counts``.
    The empty record when the crew has no crew log and nothing to carry forward.

    A crew upgraded from the pre-projection files reads its PENDING carry
    (:func:`_legacy_preview`) until a write consumes it, because the carry can only
    run from a write and the reads that happen first are the ones the crew acts on.
    """
    projection = _projection()
    units = crew_log_units(owner, repo, crew_id, root, live_session_id=live_session_id)
    try:
        base = _fold_checkpoint(crew_id, units)
        value = projection.projection_of(base).value
    except Exception:
        # A crew whose log cannot be FOLDED reads as the empty record, the same answer
        # as a crew whose logs could not be listed and the same contract the
        # pre-projection reader kept when its index would not parse. This is the read
        # path: one crew's damaged log, a segment boundary a reader cannot follow, or
        # an entry type a newer writer introduced would otherwise raise into the crew
        # page and the pre-investigate briefing for EVERY crew in the repository,
        # because those read across crews. The write path does not come through here:
        # it folds strictly (:func:`_prepare_write`), since a write that validated
        # against an empty record could admit a second editor.
        #
        # NOT previewed here either. A refused fold means a reader older than the
        # writer, so the log may hold entries this build could not read -- answering
        # with the pre-projection files would assert that the upgrade has not happened
        # on the strength of a log it failed to read.
        logger.warning(
            "crew ledger: could not fold crew %s's crew log; reading it as empty",
            crew_id,
            exc_info=True,
        )
        return projection.projection_of(projection.initial(FOLD_NAME)).value
    return _legacy_preview(owner, repo, crew_id, root, base) or value


def read_work_item(
    owner: str, repo: str, crew_id: str, number: int, root: Path | None = None
) -> dict[str, Any] | None:
    number = int(number)
    for record in read_ledger(owner, repo, crew_id, root)["items"]:
        if record.get("number") == number:
            return record
    return None


def list_work_items(
    owner: str, repo: str, crew_id: str, root: Path | None = None, *, open_only: bool = False
) -> list[dict[str, Any]]:
    """This crew's work items, newest progress first -- the fold's own order."""
    items = read_ledger(owner, repo, crew_id, root)["items"]
    if open_only:
        items = [record for record in items if record.get("phase") not in TERMINAL_PHASES]
    return items


def open_slot_count(owner: str, repo: str, crew_id: str, root: Path | None = None) -> int:
    """Work items occupying a slot: every unfinished one.

    No exemption, because there is no phase in which the crew is not the
    actor: an item it cannot progress without a human is recorded as a pass and its
    claim released, so anything still open is work this crew owes.
    """
    return len(list_work_items(owner, repo, crew_id, root, open_only=True))


def _editing_item(state: dict[str, Any], *, exclude: int | None = None) -> int | None:
    """The issue number the folded crew is currently editing, if any."""
    for record in state["items"].values():
        num = record.get("number")
        if record.get("phase") in EDITING_PHASES and num != exclude and isinstance(num, int):
            return num
    return None


def read_events(
    owner: str,
    repo: str,
    root: Path | None = None,
    *,
    crew_id: str = "",
    limit: int = 200,
    require_phase: bool = False,
) -> list[dict[str, Any]]:
    """Progress lines, newest first: one crew's, or every crew's in the repository.

    Each crew's fold already collapses a duplicated line and drops a malformed one,
    so the union here only merges and orders. ``require_phase`` keeps the lines that
    carry a ``phase`` -- the ENTRIES into a phase, which is what a dwell reader wants.
    """
    if crew_id:
        sources = [read_ledger(owner, repo, crew_id, root)]
    else:
        sources = [
            read_ledger(owner, repo, str(crew.get("id") or ""), root)
            for crew in list_crews(owner, repo, root, include_retired=True)
            if crew.get("id")
        ]
    rows = [line for source in sources for line in source["events"]]
    if require_phase:
        rows = [line for line in rows if line.get("phase")]
    rows.sort(key=lambda line: str(line.get("ts") or ""), reverse=True)
    return rows[: max(0, limit)]


def read_skips(owner: str, repo: str, root: Path | None = None) -> dict[str, dict[str, Any]]:
    """The repository's shared skip index, keyed by ``str(number)``.

    The one read made ACROSS crews: every crew of the repository, retired ones
    included, contributes the passes its own fold holds, and on each number the
    FIRST decision stands -- the same first-decision-wins rule the index always had.
    Which row is first is decided by :func:`_skip_precedence`: a row recorded while
    another crew's decision already stood carries ``deferred`` and never stands over
    it, so an established decision cannot be replaced by a later pass whatever the
    clocks say; only two passes neither of which saw the other -- recorded inside the
    propagation window below -- fall back to the recorded time, and between two
    concurrent decisions neither was established.

    STALENESS. A pass another crew recorded is visible here once that crew's append
    has DRAINED to its log, and each crew's fold is re-checked against its log on
    every read, so nothing is cached past a write. The flush budget
    (``_APPEND_FLUSH_SECONDS``) does not bound that wait: it bounds how long the
    WRITE waits before answering ``durable: false``, and an append still held by the
    writer past it lands whenever the writer drains, with no bound on when. That is
    why the rule above turns on what was ESTABLISHED rather than on how long ago
    something was recorded -- an unbounded lag cannot let a later pass displace a
    decision that already stood. A pass whose unit crew-log retention has already
    collected is gone from this index; that is the same bound every fold of the crew
    log lives under, and the spec states it.
    """
    out: dict[str, dict[str, Any]] = {}
    for crew in list_crews(owner, repo, root, include_retired=True):
        cid = str(crew.get("id") or "")
        if not cid:
            continue
        for key, row in read_ledger(owner, repo, cid, root)["skips"].items():
            standing = out.get(key)
            if standing is None or _skip_precedence(row) < _skip_precedence(standing):
                out[key] = dict(row)
    return out


def _skip_precedence(row: dict[str, Any]) -> tuple[bool, str, str]:
    """Sort key under which the row that STANDS on a number sorts first.

    A ``deferred`` row -- recorded while another crew's decision already stood --
    sorts after every row that is not, so the writer's own observation orders the
    two and a clock stepped backward cannot re-order them. Rows that never saw each
    other order by recorded time, crew id as the tie-break.
    """
    return (
        row.get("deferred") is True,
        str(row.get("decided_at") or ""),
        str(row.get("crew_id") or ""),
    )


def is_skipped(owner: str, repo: str, number: int, root: Path | None = None) -> bool:
    """Whether any crew in this repo has already passed on *number*."""
    return str(int(number)) in read_skips(owner, repo, root)


def recent_skips(
    owner: str, repo: str, root: Path | None = None, *, limit: int = 20
) -> list[dict[str, Any]]:
    """The newest *limit* entries, newest first, issue number as the tie-break."""
    rows = sorted(
        read_skips(owner, repo, root).values(),
        key=lambda r: (str(r.get("decided_at") or ""), int(r.get("number") or 0)),
        reverse=True,
    )
    return rows[: max(0, limit)]


def _require_crew_log(session_id: str) -> Any:
    """The fold package, once *session_id* is known to HAVE a crew log.

    The ledger writes through the emitter, which treats a session with no crew log
    as a policy no-op -- correct for a turn entry nobody asked for, wrong for an
    update a crew explicitly recorded. So the log's existence is established here,
    where the caller can be told, instead of being discovered as silence.
    """
    from kiro_crew.crew_log import emit as crew_log_emit

    if not session_id:
        raise CrewLedgerUnavailable(
            "this crew has no live session, so there is no crew log to record into"
        )
    if not crew_log_emit.enabled():
        raise CrewLedgerUnavailable(
            "the crew ledger is recorded in the crew's crew log, which is switched off; "
            f"{crew_log_emit.CREW_LOG_ENV} is set to 0, false, no, off or an unrecognised value; "
            f"unset it (or remove it from {env_file_display()}) and restart the gateway "
            "to record one"
        )
    from kiro_crew.crew_log.schema import KIND_SESSION
    from kiro_crew.crew_log.store import CrewLog

    try:
        present = CrewLog.exists(KIND_SESSION, session_id)
    except Exception as exc:
        raise CrewLedgerUnavailable(f"the crew's crew log could not be read: {exc}") from exc
    if not present:
        raise CrewLedgerUnavailable(
            "the crew's session has no crew log yet, so there is nothing to record into; "
            "it is created on the session's first turn"
        )
    return _projection()


def _entry_data(
    owner: str,
    repo: str,
    crew_id: str,
    number: int | None,
    patch: dict[str, Any],
    event_kind: str,
    event_text: str,
    *,
    skip_reason: str | None = None,
    skip_scope: str = "",
    skip_deferred: bool = False,
) -> dict[str, Any]:
    """The ``radar/recorded`` entry for one update -- only the fields the call set.

    An omitted field means "unchanged", which is what lets a partial patch be one
    line. An EXPLICIT null means "empty this field" and is carried as a name in
    ``clear``, because a typed field cannot hold a null: the fold empties each named
    field before applying the fields the same update sets. A number that cannot be
    read as a finite int, or that falls outside the range the record tool would have
    accepted, is left out rather than written, so a hand-typed ``pr_number`` of
    ``Infinity`` or ``0`` costs that field and not the whole entry -- and, because an
    omitted field reads as "unchanged", does not erase the number already stored.
    """
    data: dict[str, Any] = {"crew_id": crew_id, "owner": owner, "repo": repo}
    if number is not None:
        data["number"] = int(number)
    cleared = [key for key in RADAR_CLEARABLE_FIELDS if key in patch and patch[key] is None]
    if cleared:
        data["clear"] = cleared
    for key in ("decision", "why", "next", "worktree", "branch", "base_sha", "outcome"):
        if key in patch and isinstance(patch[key], str):
            data[key] = patch[key]
    if "phase" in patch:
        data["phase"] = str(patch["phase"] or "").strip()
    for key in ("pr_number", "claim_comment_id"):
        if key in patch:
            value = _finite_int(patch[key])
            # The MAGNITUDE bound belongs here, not only in the fold. An out-of-range
            # but finite number (a dashboard ``pr_number`` of 0, a comment id past the
            # forge's range) would otherwise be written, and the fold -- which applies
            # RADAR_NUMBER_BOUNDS to the bytes it reads -- drops it, so the update
            # lands and ERASES the association the item already held. Left out here it
            # reads as "unchanged", which is the same answer this entry already gives
            # a number it cannot read as a finite int at all.
            if value is not None:
                low, high = RADAR_NUMBER_BOUNDS[key]
                if not low <= value <= high:
                    value = None
            if value is not None:
                data[key] = value
    if isinstance(patch.get("ci_state"), dict):
        data["ci_state"] = dict(patch["ci_state"])
    if isinstance(patch.get("labels_applied"), list):
        data["labels_applied"] = [x for x in patch["labels_applied"] if isinstance(x, str)]
    tried = patch.get("tried_approach")
    if isinstance(tried, str) and tried.strip():
        data["tried"] = {
            "approach": tried.strip(),
            "rejected_because": str(patch.get("tried_rejected_because") or ""),
        }
    if skip_reason is not None:
        data["skip"] = {"reason": str(skip_reason or ""), "scope": coerce_skip_scope(skip_scope)}
        if skip_deferred:
            # Another crew's decision on this number already stood when this pass was
            # recorded: the token that orders the two without a clock.
            data["skip"]["deferred"] = True
    data["event"] = event_text
    data["event_kind"] = event_kind
    return data


def _append(
    crew_id: str,
    session_id: str,
    data: dict[str, Any],
    base: Any,
    root: Path | None = None,
) -> dict[str, Any]:
    """Append *data* as ONE entry to *session_id*'s crew log and answer with the
    record it produces. ``{"item", "event", "skip", "coalesced", "durable"}``.

    ``skip`` is the row the SHARED index answers for the number -- the first
    decision across the repository's crews -- so a crew re-skipping an issue is told
    what stands rather than what it sent (:func:`_standing_skip`).

    A crew-level line landing on a crew-level line is COALESCED here, before the
    append, by the same rule the fold applies to the bytes: the existing sweep is
    answered and nothing is written, so an idle crew appends once per stretch.

    The answer is the fold advanced over the entry AS THE FILE HOLDS IT once the
    writer has drained -- the line's id and ``ts`` come off the log's own clock, so
    the answer a crew is handed is the line every later reader sees. Only when the
    writer did not drain inside the budget is the answer the fold advanced over the
    entry on its way to the file, stamped with this side's clock: an approximation
    the next read corrects. ``durable`` says which of the two this was. A writer
    that drained WITHOUT landing the entry refused it, and the write raises
    :class:`CrewLedgerNotRecorded`: a refusal is answered as one, never as a record.
    """
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.crew_log.schema import Entry

    projection = _projection()
    number = data.get("number")
    events = base.state["events"]
    if number is None and events and events[-1].get("kind") == CREW_LEVEL_EVENT_KIND:
        return {
            "item": None,
            "event": dict(events[-1]),
            "skip": None,
            "coalesced": True,
            "durable": True,
        }
    if not crew_log_emit.radar_entry_fits(data):
        raise CrewLedgerEntryTooLarge(
            "this update does not fit one crew log entry; record fewer or shorter fields"
        )
    refused_before = crew_log_emit.dropped_writes()
    overflow_before = crew_log_emit.overflow_writes(session_id)
    recorded_ms = int(time.time() * 1000)
    crew_log_emit.on_radar_recorded(session_id, data)
    drained = crew_log_emit.flush(timeout=_APPEND_FLUSH_SECONDS)
    # Remembered for the crew's NEXT write, which drains again before it folds: a
    # write that validated against a fold missing this entry could pass the
    # one-editor rule a second time. Set and cleared under the crew's write lock.
    undrained_key = (str(data_home()), crew_id)
    if drained:
        _undrained.pop(undrained_key, None)
    else:
        _undrained[undrained_key] = (session_id, base.last_seq, data)
    grown = None
    landed = False
    if drained:
        since, landed = _landed_since(projection, session_id, base.last_seq, data)
        if landed and since is not None:
            grown = projection.advance(base, since)
    if landed:
        # PUBLISH THIS UNIT'S PRECEDENCE ONLY ONCE ITS ENTRY IS IN THE FILE. The order
        # file's whole claim is "among the units that recorded, this one holds the
        # newest entry", so it is earned by an append that LANDED, and noting it any
        # earlier would let a refused or still-queued append claim precedence it never
        # earned -- pinning a unit ahead of its successor, whose next fold would then
        # read the pinned unit's phase and next step as current and act on them.
        # ``_undrained`` (in ``crew_store_log``) is that queued case, so the window is real.
        #
        # Gated on ``landed`` rather than on ``drained`` or the refusal counters:
        # ``landed`` is read back from this unit's OWN log by content, while
        # ``dropped_writes`` is process-wide, so another session's refusal must not
        # suppress a precedence note this unit has genuinely earned.
        _record_unit_order(
            str(data.get("owner", "")), str(data.get("repo", "")), crew_id, session_id, root
        )
    refused = crew_log_emit.dropped_writes() != refused_before
    overflowed = crew_log_emit.overflow_writes(session_id) != overflow_before
    if overflowed and not landed:
        # The buffer rejected one of THIS session's appends at SUBMISSION for
        # crossing its memory ceiling while this one was in flight, and this entry is
        # not in the file: it was never queued and will never land, whatever the
        # drain says. Counted per session and apart from a storage refusal
        # (``overflow_writes`` against ``dropped_writes``), so it is checked apart --
        # a rejected entry answered as queued state would be published without ever
        # being persisted, and another session's rejection cannot flip this one.
        logger.warning("crew ledger: the crew log writer rejected this update at its ceiling")
        _undrained.pop(undrained_key, None)  # nothing of this write is queued to drain
        raise CrewLedgerNotRecorded(
            "this update was not recorded: the crew log writer is at its ceiling and "
            "rejected the append; nothing changed, send it again once it has drained"
        )
    if drained and not landed:
        # The writer is done and the entry is not in the file -- refused (the log
        # deleted under the write, the writer gave up on it) or unreadable twice. A
        # refusal is not a commit, and an entry that cannot be shown to exist is
        # answered the same way: the caller is told, never handed a record that may
        # be nowhere. The synthetic answer is made ONLY while the writer still holds
        # the entry.
        logger.warning("crew ledger: the crew log writer drained without landing this update")
        raise CrewLedgerNotRecorded(
            "this update was not recorded: the crew log did not take the append; "
            "nothing changed, the same update may be sent again"
        )
    durable = drained and landed and not refused
    if not drained:
        logger.warning(
            "crew ledger: the crew log writer did not drain within %.1fs; this update is "
            "queued and counted, not yet durable",
            _APPEND_FLUSH_SECONDS,
        )
    elif not durable:
        logger.warning(
            "crew ledger: the crew log refused an append while this update was in flight; "
            "this update landed, but a neighbouring one may not have"
        )
    if grown is None:
        # Reached only while the writer still holds the entry (not drained): the fold
        # advanced over the entry as sent, stamped with this side's clock -- an
        # approximation the next read corrects.
        pending = Entry(
            type=LEDGER_ENTRY_TYPE,
            seq=base.last_seq + 1,
            time=recorded_ms,
            src=_ENTRY_SRC,
            data=data,
        )
        grown = projection.advance(base, (pending,))
    state = projection.projection_of(grown).value
    key = str(number) if number is not None else ""
    item = next((record for record in state["items"] if str(record.get("number")) == key), None)
    skip_row = state["skips"].get(key)
    if skip_row is not None:
        skip_row = _standing_skip(data["owner"], data["repo"], root, key, skip_row)
    return {
        "item": item,
        "event": state["events"][0] if state["events"] else None,
        "skip": skip_row,
        "coalesced": False,
        "durable": durable,
    }


def _standing_skip(
    owner: str, repo: str, root: Path | None, key: str, own: dict[str, Any]
) -> dict[str, Any]:
    """The row the shared index answers for *key*, given this crew's own row *own*.

    A crew that passes on a number another crew already decided is told what
    STANDS -- the first decision, with the crew that made it -- not what it sent, so
    it can see its own reason was not the one kept. *own* is included explicitly
    because the just-appended entry may not have drained to the log yet.
    """
    rows: list[dict[str, Any]] = [own]
    rows.extend(row for k, row in read_skips(owner, repo, root).items() if k == key)
    standing = min(rows, key=_skip_precedence)
    return dict(standing)


def record_crew_checkpoint(
    owner: str,
    repo: str,
    crew_id: str,
    event_text: str,
    root: Path | None = None,
    *,
    session_id: str = "",
) -> dict[str, Any]:
    """Append one crew-level line -- a step that belongs to no issue.

    Takes no kind: there is exactly one crew-level kind, and the route already
    refuses anything else on the numberless path. Consecutive sweeps coalesce (see
    :func:`_append`): the first sweep after real work is written and a sweep landing
    on a sweep is answered with the existing line, whose timestamp marks when the
    idle stretch BEGAN.

    *session_id* is the crew's live crew log unit; the route resolves it from the
    crew's slot. Without one the write is refused (:class:`CrewLedgerUnavailable`).
    """
    _require_crew_log(session_id)
    with _crew_write_lock(crew_id):
        base = _prepare_write(owner, repo, crew_id, session_id, root)
        data = _entry_data(owner, repo, crew_id, None, {}, CREW_LEVEL_EVENT_KIND, event_text)
        return _append(crew_id, session_id, data, base, root)


def _prepare_write(owner: str, repo: str, crew_id: str, session_id: str, root: Path | None) -> Any:
    """The fold a write validates against, with the one-time carry run -- the SAME
    sequence for every writer.

    The crew's unit order is published by :func:`_append`, once the entry is in this
    unit's log: the order file claims this unit holds the newest entry, and only a
    landed append earns that. The carry runs on a crew's first write after the
    upgrade whichever
    kind of write it is: an idle sweep is a routine first write, and a sweep that
    appended without carrying would leave the crew's open items unread -- reads
    reporting zero open items -- until some later item write happened to carry
    them. Called under the crew's write lock.
    """
    undrained_key = (str(data_home()), crew_id)
    receipt = _undrained.get(undrained_key)
    if receipt is not None:
        # The crew's last append was still in the writer when that write answered.
        # Drain it now, before folding, so this write validates against a record
        # that holds it. A writer STILL not draining refuses this write: a fold taken
        # now would be missing that entry, and the one-editor rule checked against
        # it could admit a second editor -- both entries would later land. Nothing
        # is changed by the refusal; the caller sends the same update again once the
        # writer has caught up, and the mark stays until the entry is SEEN.
        from kiro_crew.crew_log import emit as crew_log_emit

        if not crew_log_emit.flush(timeout=_APPEND_FLUSH_SECONDS):
            raise CrewLedgerNotRecorded(
                "the crew's previous update is still queued in the crew log writer; "
                "send this update again shortly"
            )
        # A QUIET WRITER IS NOT A LANDED ENTRY. A drop empties the buffer exactly as a
        # successful write does, so ``flush`` answers True either way; releasing the
        # mark on that alone would fold a record short one entry and hand the
        # one-editor rule a record that permits a second editor -- silently, because
        # the earlier call already answered. So the receipt is checked against the
        # unit's own log by content, and the mark is released only when the entry is
        # there. It is not, this write is refused like any other undrained one: the
        # caller resends, and that resend is what puts the missing update back.
        if not _landed_since(_projection(), receipt[0], receipt[1], receipt[2])[1]:
            raise CrewLedgerNotRecorded(
                "the crew's previous update never reached its crew log; send that "
                "update again before this one"
            )
        # AND PUBLISH THAT UNIT'S PRECEDENCE NOW, which is the only moment anything
        # can. The append path notes precedence only for an entry it saw land inside
        # the flush budget; an append the writer still held when that budget expired
        # lands later, and its own call has already returned. Its unit may by then be
        # RETIRED, so it has no next write of its own to make the claim -- but the
        # CREW does, and this is it. Left unpublished, a successor unit stamped by a
        # clock that went backwards sorts ahead of it in header order, and every read
        # then folds the retired unit's phase and next step as the current ones.
        #
        # Correctly ordered by WHERE this sits: the receipt's entry is the newest one
        # this crew has landed at this instant, and this runs before this write's own
        # append, whose landing moves the live unit after it.
        _record_unit_order(
            str(receipt[2].get("owner", "")),
            str(receipt[2].get("repo", "")),
            crew_id,
            receipt[0],
            root,
        )
        _undrained.pop(undrained_key, None)
    units = _units_for_write(owner, repo, crew_id, session_id, root)
    base = _fold_checkpoint(crew_id, units)
    # The carry runs when NO radar entry has ever folded into this crew's record --
    # the fold's ``crew_id`` is set by the first entry that names the crew, whichever
    # kind it is -- or when an earlier carry began and did not finish. Not when the
    # record merely holds no items and no passes: a crew whose every write was an
    # idle sweep has that shape for good, and keying on it would carry the
    # pre-projection files again whenever they are present without their marker,
    # re-stating retired work as live over a record that has moved on.
    if not base.state["crew_id"] or _carry_pending(owner, repo, crew_id, root):
        if _carry_legacy_forward(owner, repo, crew_id, session_id, root, folded=base.state):
            units = _units_for_write(owner, repo, crew_id, session_id, root)
            base = _fold_checkpoint(crew_id, units)
    return base


def _units_for_write(
    owner: str, repo: str, crew_id: str, session_id: str, root: Path | None
) -> tuple[str, ...]:
    """The crew's units for a WRITE: listed strictly, a failed listing refuses the write.

    A read that cannot list the units answers the empty record and says nothing
    false about what it could read. A write that folded an empty record because the
    listing failed would validate against it -- and the one-editor rule, checked
    against no items, admits a second editor into a log that cannot take a line back.
    """
    try:
        return crew_log_units(owner, repo, crew_id, root, live_session_id=session_id, strict=True)
    except Exception as exc:
        raise CrewLedgerNotRecorded(
            "the crew's logs could not be listed, so this update was not recorded; "
            "nothing changed, send it again"
        ) from exc


def commit_work_progress(
    owner: str,
    repo: str,
    crew_id: str,
    number: int,
    patch: dict[str, Any],
    event_kind: str,
    event_text: str,
    skip_reason: str | None = None,
    skip_scope: str = "",
    root: Path | None = None,
    *,
    session_id: str = "",
) -> dict[str, Any]:
    """Record one work-item update: the fields it sets, its progress line and, when
    it records a pass, the skip row -- as ONE entry. ``{"item", "event", "skip"}``.

    *skip_reason* is what makes this a pass: ``None`` means "no skip", and any string
    indexes one. The reason is the caller's to choose, but the COUPLING is not: an
    issue cannot be recorded as passed without the same entry carrying the item and
    the line that explain it, and there is no ordering in which a reader can see one
    without the others.

    The store's own refusals happen BEFORE anything is appended, against the crew's
    folded record: an unknown kind or phase, a crew-level kind with a number, and a
    second item entering an editing phase are all :class:`CrewStoreError`, and an
    append-only log cannot take a line back, so nothing is written until they pass.

    On a crew's FIRST write after the upgrade the pre-projection files are carried
    into the log first (:func:`_carry_legacy_forward`), so a crew upgraded mid-work
    finds its open items and the repository keeps its passes.
    """
    number = int(number)
    if event_kind not in EVENT_KINDS:
        raise CrewStoreError(f"unknown event kind {event_kind!r}")
    if event_kind == CREW_LEVEL_EVENT_KIND:
        raise CrewStoreError(f"event kind {event_kind!r} is crew-level and takes no issue number")
    _require_crew_log(session_id)
    with _crew_write_lock(crew_id):
        base = _prepare_write(owner, repo, crew_id, session_id, root)
        current = base.state["items"].get(str(number))
        if "phase" in patch:
            new_phase = str(patch["phase"] or "").strip()
            if new_phase not in PHASES:
                raise CrewStoreError(f"unknown phase {new_phase!r}")
            prev_phase = current["phase"] if current is not None else "selected"
            if new_phase in EDITING_PHASES and prev_phase not in EDITING_PHASES:
                other = _editing_item(base.state, exclude=number)
                if other is not None:
                    raise CrewStoreError(
                        f"crew {crew_id} is already editing #{other} — finish or "
                        "commit that before entering an editing phase on another issue"
                    )
        deferred = False
        if skip_reason is not None:
            # Observe the shared index BEFORE recording: a pass on a number another
            # crew already decided defers to that decision, and says so in the entry,
            # so the union orders the two by what this writer saw and not by clocks.
            standing = read_skips(owner, repo, root).get(str(number))
            deferred = standing is not None and str(standing.get("crew_id") or "") != crew_id
        data = _entry_data(
            owner,
            repo,
            crew_id,
            number,
            patch,
            event_kind,
            event_text,
            skip_reason=skip_reason,
            skip_scope=skip_scope,
            skip_deferred=deferred,
        )
        return _append(crew_id, session_id, data, base, root)
