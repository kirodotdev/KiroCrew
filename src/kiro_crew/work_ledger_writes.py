"""The writes a conductor and its workers make to the work ledger.

A worker's binding to the one open item it holds; the conductor's entry points
(``ensure_conductor``, ``apply_conductor_action`` with its goal, create and item actions);
the worker's (``apply_worker_report``); the brief a worker reads; and the acceptance batch
and update. Each runs its field checks first and holds the locks in the order the facade's
docstring states. :mod:`kiro_crew.work_ledger` binds every name defined here, so each one
reads, imports and patches as ``kiro_crew.work_ledger.<name>``.
"""

from __future__ import annotations

import secrets
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.session_ledger import _store_name
from kiro_crew.work_ledger_records import (
    BLOCKED_REASONS,
    CODE_ALREADY_BOUND,
    CODE_DEPTH_EXCEEDED,
    CODE_GOAL_CONCURRENCY_CAP_REACHED,
    CODE_INVALID_ACTION,
    CODE_INVALID_STATUS,
    CODE_INVALID_VALUE,
    CODE_ITEM_CAP_EXCEEDED,
    CODE_ITEM_CLOSED,
    CODE_NO_LEDGER,
    CODE_UNKNOWN_ITEM,
    CONDUCTOR_ACTIONS,
    ITEM_STATES,
    LEGACY_GOAL_BASE,
    MAX_DECISION_CHARS,
    MAX_GOAL_CHARS,
    MAX_GOAL_CONCURRENCY_CAP,
    MAX_GOAL_ITEM_CAP,
    MAX_ITEMS_PER_CONDUCTOR,
    MAX_SUMMARY_CHARS,
    MAX_TITLE_CHARS,
    SCHEMA_VERSION,
    TERMINAL_ITEM_STATES,
    VERDICTS,
    WORKER_STATUSES,
    ConductorRecord,
    WorkItem,
    WorkLedgerError,
    _as_str,
    _now_iso,
    _settle_goal_base,
    logger,
)
from kiro_crew.work_ledger_rules import (
    _require_acceptance,
    _require_artifacts,
    _require_choice,
    _require_count,
    _require_pr,
    _require_text,
    depth_cap_hint,
    is_acceptance_concrete,
    may_conduct,
)
from kiro_crew.work_ledger_store import (
    _CONDUCTOR_FILE,
    _ITEM_ID_RE,
    _KEY_FILE,
    _commit_item_locked,
    _existing_conductor_lock,
    _read_json_record,
    _read_text_or_none,
    _require_item_id,
    _require_record_fits,
    _restore_text,
    _stored_item_ids,
    _write_record,
    binding_lock,
    binding_path,
    conductor_dir,
    conductor_lock,
    item_lock,
    items_dir,
    list_work_items,
    mint_item_id,
    read_conductor,
    read_work_item,
)
from kiro_crew.work_vocab import WORK_REASON_STATUSES

# --------------------------------------------------------------------------- #
# Bindings. One file per worker, written only by a conductor's ``bind`` action, and
# only as a whole-file replacement under that worker's binding lock -- so a
# worker's binding moves to a new item exactly when its prior item is terminal,
# and two conductors cannot both believe they bound it.
# --------------------------------------------------------------------------- #


def read_binding(worker_slot_key: str, *, strict: bool = False) -> tuple[str, str] | None:
    """The ``(conductor_slot_key, item_id)`` *worker_slot_key* is bound to.

    ``None`` when unbound, which is the state Phase 2 answers as ``not_bound``. A
    binding naming a malformed item id also reads as unbound rather than raising:
    a worker cannot repair its own binding, so a clean refusal is the only useful
    answer. ``strict`` has :func:`_read_json_record`'s meaning -- a present but
    momentarily unreadable file raises instead of reading as unbound -- and is
    what the bind guard uses so it fails closed.
    """
    raw = _read_json_record(binding_path(worker_slot_key), strict=strict)
    if not isinstance(raw, dict):
        return None
    conductor = _as_str(raw.get("conductor_slot_key"))
    item_id = _as_str(raw.get("item_id"))
    if not conductor or not _ITEM_ID_RE.match(item_id):
        return None
    return conductor, item_id


def _refuse_if_worker_holds_open_item(worker_slot_key: str) -> None:
    """Refuse a bind that would overwrite a binding whose item is still open.

    A worker has exactly ONE binding file, so a second bind silently repoints its
    only report channel: the first item keeps naming the worker while every report
    lands on the second, and with no unbind path that is permanent. A binding whose
    item is terminal, or whose item FILE IS GONE, is stale and may be replaced --
    that is how a worker session is reused for a new item once its last one closed.

    A binding whose item does NOT name this worker back is stale too. ``bind``
    writes the binding first and the item second, so a process killed between the
    two leaves exactly that half-state; the writer never produces it any other way.
    Treating it as live would make the retry of that very bind refuse forever.

    FAILS CLOSED on a transient read error. The prior item's file is not under this
    lock -- its own conductor may be mid-rename on it -- so a momentary ``OSError``
    is propagated rather than read as "absent": the bind refuses and the caller
    retries, instead of stranding an item that was open all along.

    CALL UNDER THE WORKER'S BINDING LOCK, so the check and the write it guards are
    one critical section.
    """
    existing = read_binding(worker_slot_key, strict=True)
    if existing is None:
        return
    prior_conductor, prior_item_id = existing
    try:
        prior = read_work_item(prior_conductor, prior_item_id, strict=True)
    except WorkLedgerError:
        return
    if prior is None or prior.is_terminal:
        return
    if prior.worker_session_key != worker_slot_key:
        logger.warning(
            "work ledger: binding for a worker names item %s, which does not name the "
            "worker back; treating the binding as an interrupted bind and replacing it",
            prior_item_id,
        )
        return
    raise WorkLedgerError(
        f"worker is already bound to open item {prior_item_id!r}; close that item "
        "before binding the worker again",
        code=CODE_ALREADY_BOUND,
        field="worker_session_key",
    )


def _read_binding_text(worker_slot_key: str) -> str | None:
    """The binding file's bytes-as-text, or ``None`` if absent. For rollback: text
    rather than a parsed record, so what is restored is what was there."""
    try:
        return _read_text_or_none(binding_path(worker_slot_key))
    except OSError:
        return None


def _restore_binding_text(worker_slot_key: str, snapshot: str | None) -> None:
    """Put the binding back as *snapshot* found it. ``None`` means delete. Never
    raises: the caller already holds the error it must surface."""
    _restore_text(binding_path(worker_slot_key), snapshot)


def _write_binding(worker_slot_key: str, conductor_slot_key: str, item_id: str) -> None:
    _write_record(
        binding_path(worker_slot_key),
        {
            "schema": SCHEMA_VERSION,
            "conductor_slot_key": conductor_slot_key,
            "item_id": item_id,
            "worker_slot_key": worker_slot_key,
            "created_at": _now_iso(),
        },
    )


# --------------------------------------------------------------------------- #
# Conductor writes
# --------------------------------------------------------------------------- #


def ensure_conductor(
    slot_key: str,
    *,
    goal: str = "",
    depth: int = 0,
    parent_item: str | None = None,
) -> ConductorRecord:
    """Create *slot_key*'s ledger if absent, and return the record either way.

    Idempotent, and it does NOT overwrite an existing goal, round or depth — a
    conductor that re-enters its own ledger on a later round must not reset it.
    Use ``apply_conductor_action(slot_key, "goal", ...)`` to change the goal.

    *depth* and *parent_item* are SERVER-owned in Phase 2: the value passed here
    comes from the creating session's own record via :func:`child_depth`, never from
    a model. Refuses a depth that :func:`may_conduct` denies, so a ledger cannot
    exist at a level that is not allowed to conduct -- the same question
    :func:`child_depth` already answered, asked again here because this function is
    also reachable with a depth that did not come through it.
    """
    checked_goal = _require_text(goal, MAX_GOAL_CHARS, "goal", required=False)
    checked_depth = _require_count(depth, "depth")
    if not may_conduct(checked_depth):
        raise WorkLedgerError(
            f"depth {checked_depth} may not conduct; {depth_cap_hint()}",
            code=CODE_DEPTH_EXCEEDED,
            field="depth",
        )
    if parent_item is not None:
        _require_item_id(parent_item)
    directory = conductor_dir(slot_key)
    with conductor_lock(slot_key):
        existing = read_conductor(slot_key, strict=True)
        if existing is not None:
            return existing
        record = ConductorRecord(
            slot_key=slot_key,
            goal=checked_goal,
            round=0,
            depth=checked_depth,
            parent_item=parent_item,
            created_at=_now_iso(),
            generation=secrets.token_hex(8),
        )
        _write_record(directory / _CONDUCTOR_FILE, record.to_dict())
        try:
            atomic_write(directory / _KEY_FILE, slot_key + "\n", mode=0o600)
        except OSError:
            logger.debug("work ledger: slot_key breadcrumb write failed", exc_info=True)
        return record


def apply_conductor_action(
    slot_key: str,
    action: str,
    *,
    item_id: str | None = None,
    title: Any = None,
    acceptance: Any = None,
    worker_session_key: Any = None,
    decision: Any = None,
    verdict: Any = None,
    state: Any = None,
    goal: Any = None,
    round_number: Any = None,
    fails: Any = None,
    item_cap: Any = None,
    concurrency_cap: Any = None,
    new_goal: Any = None,
) -> dict[str, Any]:
    """Write the fields the CONDUCTOR owns, and append the one event that explains it.

    Never writes ``status``, ``summary``, ``artifacts``, ``pr`` or ``last_report_at``
    — those are the worker's, and :func:`apply_worker_report` is the only path to
    them. Phase 2 mounts this behind the conductor's tool and that one behind the
    worker's, so ownership is enforced by which function a caller can reach rather
    than by filtering inside a shared one.

    Actions and their fields:

    ``create``  ``title``, ``acceptance``, optional ``round_number`` — mints an id.
    ``bind``    ``item_id``, ``worker_session_key`` — writes the binding file.
    ``decide``  ``item_id``, ``decision``, optional ``round_number``.
    ``verdict`` ``item_id``, ``verdict``, optional ``fails``.
    ``close``   ``item_id``, ``state``, optional ``decision`` — stamps ``closed_at``.
    ``goal``    ``goal``, optional ``round_number``, ``item_cap``,
                ``concurrency_cap`` and ``new_goal`` — the conductor record only.

    Returns ``{"conductor", "item", "event"}``; ``item`` and ``event`` are ``None``
    for ``goal``. Raises :class:`WorkLedgerError` with a ``code`` from the ``CODE_*``
    constants. Every bound is checked before the first write, so a refused call
    leaves every file byte-identical.
    """
    if action not in CONDUCTOR_ACTIONS:
        raise WorkLedgerError(
            f"unknown action {action!r}; expected one of {sorted(CONDUCTOR_ACTIONS)}",
            code=CODE_INVALID_ACTION,
            field="action",
        )
    record = read_conductor(slot_key)
    if record is None:
        raise WorkLedgerError(f"no work ledger for {slot_key!r}", code=CODE_NO_LEDGER)

    if action == "goal":
        return {
            "conductor": _write_goal(
                slot_key,
                record,
                goal,
                round_number,
                item_cap,
                new_goal,
                concurrency_cap=concurrency_cap,
            ),
            "item": None,
            "event": None,
        }
    if action == "create":
        return _create_item(slot_key, record, title, acceptance, round_number)
    return _write_item_action(
        slot_key,
        record,
        action,
        item_id,
        worker_session_key=worker_session_key,
        decision=decision,
        verdict=verdict,
        state=state,
        round_number=round_number,
        fails=fails,
    )


def _header_under_lock(slot_key: str, verb: str) -> ConductorRecord:
    """The live header for a writer that holds the conductor lock, or a refusal.

    Refuses with ``CODE_NO_LEDGER`` when the header FILE IS ABSENT: the ledger was
    purged while this writer waited on the lock, and writing a pre-lock snapshot
    back would resurrect a header into a removed store -- one with no breadcrumb,
    which no later purge could name.

    Refuses with the same code when the header is PRESENT but does not read. The
    header carries :attr:`ConductorRecord.created_total`, the count the stored
    bound is measured against, and only the header holds it: the caller's pre-lock
    snapshot was read before every writer that held the lock since, so a write
    that stood the snapshot in for the torn file would put a lower count back --
    for a ``goal``, into the header the next create then reads; for a create, into
    the bound itself -- and admit creates past what the fold retains. Corruption
    reads as absent everywhere else in this module, and the same header would
    have read as absent to :func:`apply_conductor_action` a moment earlier; the
    refusal says which of the two it found. The remedy is a rebuild from the crew
    log (:func:`rebuild_from_projection`), which writes the header from the record,
    the counter with it.
    """
    live = read_conductor(slot_key, strict=True)
    if live is not None:
        return live
    if not (conductor_dir(slot_key) / _CONDUCTOR_FILE).exists():
        raise WorkLedgerError(
            f"conductor ledger has no header file, so {verb} -- the store was purged "
            "or is incomplete; ensure_conductor recreates the header",
            code=CODE_NO_LEDGER,
        )
    raise WorkLedgerError(
        f"conductor ledger header is present but unreadable, so {verb} -- it holds "
        "the board's create count and a pre-lock snapshot of it cannot stand in; "
        "rebuild the board from the crew log (rebuild_from_projection), which writes "
        "the header from the record",
        code=CODE_NO_LEDGER,
    )


def _write_goal(
    slot_key: str,
    record: ConductorRecord,
    goal: Any,
    round_number: Any,
    item_cap: Any = None,
    new_goal: Any = None,
    *,
    concurrency_cap: Any = None,
) -> ConductorRecord:
    """Partial update of the conductor header, merged UNDER the lock.

    Bounds are checked before the lock; the fields a caller omitted are filled from
    the record re-read inside it, never from the pre-lock snapshot. Otherwise a
    goal-only call and a round-only call racing each other would each restore the
    other's field to the stale value it read before waiting. *record* is the
    caller's pre-lock read, which is how the caller knew a ledger exists; nothing
    written here is taken from it.

    ``new_goal=True`` starts a new goal, so its item count starts again:
    :attr:`ConductorRecord.goal_items_base` moves to the board's create total. It
    is an explicit signal and needs the new goal text with it; a goal text that
    merely differs keeps the count, so rewording cannot reset the spend line.
    """
    checked_goal = None if goal is None else _require_text(goal, MAX_GOAL_CHARS, "goal")
    checked_round = None if round_number is None else _require_count(round_number, "round")
    checked_cap = None if item_cap is None else _require_count(item_cap, "item_cap")
    if checked_cap is not None and not 1 <= checked_cap <= MAX_GOAL_ITEM_CAP:
        raise WorkLedgerError(
            f"item_cap must be between 1 and {MAX_GOAL_ITEM_CAP}; got {checked_cap}",
            code=CODE_INVALID_VALUE,
            field="item_cap",
        )
    checked_concurrency = (
        None if concurrency_cap is None else _require_count(concurrency_cap, "concurrency_cap")
    )
    if checked_concurrency is not None and not 1 <= checked_concurrency <= MAX_GOAL_CONCURRENCY_CAP:
        raise WorkLedgerError(
            f"concurrency_cap must be between 1 and {MAX_GOAL_CONCURRENCY_CAP}; "
            f"got {checked_concurrency}",
            code=CODE_INVALID_VALUE,
            field="concurrency_cap",
        )
    if new_goal is not None and not isinstance(new_goal, bool):
        raise WorkLedgerError(
            f"new_goal must be true or false; got {new_goal!r}",
            code=CODE_INVALID_VALUE,
            field="new_goal",
        )
    if new_goal and checked_goal is None:
        raise WorkLedgerError(
            "new_goal starts a new goal, so it needs the new goal text in `goal`",
            code=CODE_INVALID_VALUE,
            field="new_goal",
        )
    with _existing_conductor_lock(slot_key):
        # The header is re-read under the lock and REQUIRED to be present and
        # readable, like ``_create_item``: a ``goal`` that waited behind a purge
        # must not rewrite its pre-lock snapshot into the removed store, and one
        # that finds the header torn must not rewrite the snapshot's create count
        # over the live one; see ``_header_under_lock``.
        current = _header_under_lock(slot_key, "its goal cannot be updated")
        if checked_goal is not None:
            current.goal = checked_goal
        if current.created_total == 0 and (new_goal or current.goal_items_base == LEGACY_GOAL_BASE):
            # The same one-time backfill ``_create_item`` does, so a board from
            # before the counter starts the goal's count after its records.
            current.created_total = len(_stored_item_ids(slot_key))
        _settle_goal_base(current)
        if new_goal:
            current.goal_items_base = current.created_total
        if checked_round is not None:
            current.round = checked_round
        if checked_cap is not None:
            current.item_cap = checked_cap
        if checked_concurrency is not None:
            current.concurrency_cap = checked_concurrency
        current.goal_version += 1
        _write_record(conductor_dir(slot_key) / _CONDUCTOR_FILE, current.to_dict())
        return current


def _create_item(
    slot_key: str,
    record: ConductorRecord,
    title: Any,
    acceptance: Any,
    round_number: Any,
) -> dict[str, Any]:
    """Mint one item under the conductor lock.

    Two bounds on the OPEN count, each checked under the lock:
    :data:`MAX_ITEMS_PER_CONDUCTOR` bounds the store's live fan-out for every board,
    and :attr:`ConductorRecord.concurrency_cap` bounds the goal's for a board that
    recorded one. Both are read off :func:`list_work_items`, so an item in a terminal
    state is listed but not counted there, and a torn record -- whose state cannot be
    read -- counts toward neither. The header's :attr:`ConductorRecord.created_total`
    counts every create this board has admitted, bumped in the same hold that writes
    the item; a header from before the counter holds zero, and the first create on
    such a board seeds it from the records the board holds (:func:`_stored_item_ids`),
    once. The goal's item cap (:attr:`ConductorRecord.item_cap`) refuses nothing: it
    is the advisory line :attr:`ConductorRecord.cap_exceeded` reports. Nothing bounds
    how many items a board creates over its life: the crew log's ``work`` fold
    forgets old closed items, so it is bounded by what is open.

    The header is written FIRST, then the item. A failure between the two leaves
    the counter one ahead of the board -- a create the fold never sees, so the
    safe side -- and the header is put back when the item write raises. The
    other order would let a failed header write leave an item the counter never
    saw, which is the side that under-counts the goal's spend. Every refusal, the item's own
    size included, is decided before the header write, so a refused create leaves
    every file byte-identical.

    The lock is the conductor's, not the item's, because the caps it enforces are
    properties of the SET: counting the items and adding one must not interleave
    with another call doing the same, or two calls each see thirty-one and both
    write. The new item's own lock is taken from inside that hold, in the
    documented order.

    Refuses when :func:`may_conduct` denies the conductor's depth, because an item
    is a dispatch and a level that may not conduct may not dispatch. No such record
    opens through :func:`ensure_conductor`, but a header on disk can carry a depth
    no guard here admitted -- :func:`rebuild_from_projection` keeps the depth a
    stored header holds -- so the question is asked here too.
    """
    checked_title = _require_text(title, MAX_TITLE_CHARS, "title")
    checked_acceptance = _require_acceptance(acceptance)
    checked_round = None if round_number is None else _require_count(round_number, "round")
    if not may_conduct(record.depth):
        raise WorkLedgerError(
            f"conductor is at depth {record.depth} and {depth_cap_hint()}; it may "
            "not dispatch further work",
            code=CODE_DEPTH_EXCEEDED,
            field="depth",
        )
    with _existing_conductor_lock(slot_key):
        # The header is re-read INSIDE the lock and is REQUIRED to be present and
        # readable. The default round must come from the live header, not the
        # pre-lock snapshot, so a concurrent ``goal`` round bump is seen. A header
        # that is GONE means the ledger was purged while this call waited on the
        # lock: minting an item now would write a record into a store with no
        # header -- a ledger destroyed down to the records that made it one. And
        # the header holds the create count the goal's spend is measured against,
        # which the pre-lock snapshot may hold low, so a header that is present
        # but torn refuses too; see ``_header_under_lock``.
        live = _header_under_lock(slot_key, "no item can be created in it")
        if checked_round is None:
            checked_round = live.round
        if live.created_total == 0:
            # Backfill, once, for a header written before the counter existed:
            # every record the board holds is a create it admitted, so the count
            # starts there rather than at zero. Persisted by the header write
            # below; a refusal here leaves it unwritten and the next create seeds
            # it again, to the same value.
            live.created_total = len(_stored_item_ids(slot_key))
        # A header from before the per-goal count: its goal counts from here.
        _settle_goal_base(live)
        # Only OPEN items count toward either fan-out cap: a closed item is history,
        # not fan-out. It stays on the board and ``list_work_items`` still returns
        # it; it does not block a create. Read off the listing, since only a
        # readable record has a state.
        open_items = [
            item for item in list_work_items(slot_key) if item.state not in TERMINAL_ITEM_STATES
        ]
        if len(open_items) >= MAX_ITEMS_PER_CONDUCTOR:
            raise WorkLedgerError(
                f"conductor holds {len(open_items)} open items; the cap is "
                f"{MAX_ITEMS_PER_CONDUCTOR}",
                code=CODE_ITEM_CAP_EXCEEDED,
                field="items",
            )
        # The goal's own fan-out bound: how much of this goal may run at once. A
        # person decides past it. Only a board that recorded a goal has one: a
        # queue board never writes ``goal``. The goal's item cap is NOT checked
        # here -- what the goal has spent over its life is reported, not refused.
        if live.goal_version > 0 and len(open_items) >= live.concurrency_cap:
            raise WorkLedgerError(
                f"this goal holds {len(open_items)} open items and its concurrency cap "
                f"is {live.concurrency_cap}. Dispatch nothing new until one lands; to "
                "run more at once, ask the user, and if they agree raise the cap with "
                "work_ledger_record action=goal concurrency_cap=<new cap> "
                f"(at most {MAX_GOAL_CONCURRENCY_CAP})",
                code=CODE_GOAL_CONCURRENCY_CAP_REACHED,
                field="concurrency_cap",
            )
        item_id = mint_item_id()
        while (items_dir(slot_key) / f"{item_id}.json").exists():
            item_id = mint_item_id()
        item = WorkItem(
            item_id=item_id,
            title=checked_title,
            acceptance=checked_acceptance,
            state="open",
            round=checked_round,
            created_at=_now_iso(),
        )
        # The last refusal, before the first write: ``_commit_item_locked`` measures
        # the item again, but by then the header below is on disk.
        _require_record_fits(item.to_dict())
        header_path = conductor_dir(slot_key) / _CONDUCTOR_FILE
        header_before = _read_text_or_none(header_path)
        live.created_total += 1
        _write_record(header_path, live.to_dict())
        try:
            with item_lock(slot_key, item_id):
                event = _commit_item_locked(slot_key, item, "create", checked_title)
        except BaseException:
            _restore_text(header_path, header_before)
            raise
        return {"conductor": live, "item": item, "event": event}


def _write_item_action(
    slot_key: str,
    record: ConductorRecord,
    action: str,
    item_id: str | None,
    *,
    worker_session_key: Any,
    decision: Any,
    verdict: Any,
    state: Any,
    round_number: Any,
    fails: Any,
) -> dict[str, Any]:
    """``bind``, ``decide``, ``verdict`` and ``close``, each one item lock deep.

    Field validation is complete before the lock is taken, so a cap or shape refusal
    never creates a lock file for an item it declined to touch. Refusals that need
    the stored record — unknown item, closed item, already bound — happen under the
    lock, after that file exists.
    """
    checked_id = _require_item_id(item_id or "")
    checked_worker: str | None = None
    checked_decision: str | None = None
    checked_verdict: str | None = None
    checked_state: str | None = None
    checked_fails: int | None = None
    checked_round: int | None = None

    if action == "bind":
        checked_worker = _require_text(worker_session_key, 512, "worker_session_key")
        if "\0" in checked_worker:
            raise WorkLedgerError(
                "worker_session_key must not contain a null byte",
                code=CODE_INVALID_VALUE,
                field="worker_session_key",
            )
    elif action == "decide":
        checked_decision = _require_text(decision, MAX_DECISION_CHARS, "decision")
    elif action == "verdict":
        checked_verdict = _require_choice(verdict, VERDICTS, "verdict", CODE_INVALID_VALUE)
        if fails is not None:
            checked_fails = _require_count(fails, "fails")
    else:  # close
        checked_state = _require_choice(state, ITEM_STATES, "state", CODE_INVALID_VALUE)
        if checked_state == "open":
            raise WorkLedgerError(
                "close needs a terminal state, not 'open'",
                code=CODE_INVALID_VALUE,
                field="state",
            )
        if decision is not None:
            checked_decision = _require_text(decision, MAX_DECISION_CHARS, "decision")
    if round_number is not None:
        if action != "decide":
            raise WorkLedgerError(
                f"round_number is not a field of {action!r}; only decide (and create) " "take it",
                code=CODE_INVALID_VALUE,
                field="round_number",
            )
        checked_round = _require_count(round_number, "round")

    with item_lock(slot_key, checked_id, create=False):
        item = read_work_item(slot_key, checked_id)
        if item is None:
            raise WorkLedgerError(
                f"unknown item {checked_id!r}", code=CODE_UNKNOWN_ITEM, field="item_id"
            )
        if item.is_terminal:
            raise WorkLedgerError(f"item {checked_id!r} is {item.state}", code=CODE_ITEM_CLOSED)
        if checked_round is not None:
            item.round = checked_round

        if action == "bind":
            if item.worker_session_key:
                raise WorkLedgerError(
                    f"item {checked_id!r} is already bound",
                    code=CODE_ALREADY_BOUND,
                    field="worker_session_key",
                )
            assert checked_worker is not None
            with binding_lock(checked_worker):
                prior_binding = _read_binding_text(checked_worker)
                _refuse_if_worker_holds_open_item(checked_worker)
                # Binding FIRST, item SECOND, so a failure between the two never
                # leaves an item that names a worker with no binding behind it --
                # that item would refuse every retry with ``already_bound`` and the
                # worker could never report. The other half-state is harmless: a
                # binding naming an item that does not name the worker is simply
                # stale and the next bind replaces it. Should the item write fail
                # anyway, the binding file is put back exactly as it was found.
                _write_binding(checked_worker, slot_key, checked_id)
                try:
                    item.worker_session_key = checked_worker
                    event = _commit_item_locked(slot_key, item, "bind", _store_name(checked_worker))
                except BaseException:
                    _restore_binding_text(checked_worker, prior_binding)
                    raise
        elif action == "decide":
            assert checked_decision is not None
            item.decision = checked_decision
            item.decided_at = _now_iso()
            event = _commit_item_locked(slot_key, item, "decision", checked_decision)
        elif action == "verdict":
            assert checked_verdict is not None
            item.verdict = checked_verdict
            if checked_fails is not None:
                item.fails = checked_fails
            if checked_verdict != "pending":
                item.pending_since = None
            elif not item.pending_since:
                item.pending_since = _now_iso()
            event = _commit_item_locked(slot_key, item, "verdict", checked_verdict)
        else:
            assert checked_state is not None
            item.state = checked_state
            if checked_decision is not None:
                item.decision = checked_decision
            item.closed_at = _now_iso()
            event = _commit_item_locked(slot_key, item, "close", checked_decision or checked_state)
        return {"conductor": record, "item": item, "event": event}


# --------------------------------------------------------------------------- #
# Worker writes
# --------------------------------------------------------------------------- #


def apply_worker_report(
    slot_key: str,
    item_id: str,
    *,
    status: Any,
    summary: Any,
    artifacts: Any = None,
    pr: Any = None,
    reason: Any = None,
) -> dict[str, Any]:
    """Write the fields the WORKER owns: ``status``, ``reason``, ``summary``,
    ``artifacts``, ``pr`` and ``last_report_at``. Nothing else is reachable from here.

    ``reason`` rides only on ``blocked`` / ``question`` and is replaced by every
    report, so a later report without one clears it.

    There is no ``verdict``, ``state``, ``acceptance``, ``decision`` or ``round``
    parameter, so a worker cannot mark itself accepted or widen its own bar — the
    strongest thing it can say is ``status: done``, which is the TRIGGER for the
    conductor to run ``accept_eval.py``, not a substitute for it.

    *slot_key* and *item_id* are the CONDUCTOR's key and the bound item, which
    Phase 2 resolves from the caller's own binding via :func:`read_binding` rather
    than accepting either from the worker. Phase 1 takes them as arguments because
    there is no identity layer yet; that is the whole reason nothing imports this
    module until Phase 2 puts the resolver in front of it.

    ``status: progress`` twice in a row collapses in the event log — see
    :func:`_coalesce_progress` — but the item's own fields always hold the newest
    report.
    """
    checked_status = _require_choice(status, WORKER_STATUSES, "status", CODE_INVALID_STATUS)
    checked_summary = _require_text(summary, MAX_SUMMARY_CHARS, "summary")
    checked_artifacts = _require_artifacts(artifacts)
    checked_pr = _require_pr(pr)
    checked_reason: str | None = None
    if reason is not None:
        checked_reason = _require_choice(reason, BLOCKED_REASONS, "reason", CODE_INVALID_VALUE)
        if checked_status not in WORK_REASON_STATUSES:
            raise WorkLedgerError(
                "reason is allowed only with status blocked or question",
                code=CODE_INVALID_VALUE,
                field="reason",
            )
    checked_id = _require_item_id(item_id)

    with item_lock(slot_key, checked_id, create=False):
        item = read_work_item(slot_key, checked_id)
        if item is None:
            raise WorkLedgerError(
                f"unknown item {checked_id!r}", code=CODE_UNKNOWN_ITEM, field="item_id"
            )
        if item.is_terminal:
            raise WorkLedgerError(f"item {checked_id!r} is {item.state}", code=CODE_ITEM_CLOSED)
        item.status = checked_status
        item.reason = checked_reason
        item.summary = checked_summary
        item.artifacts = checked_artifacts
        if checked_pr is not None:
            item.pr = checked_pr
        item.last_report_at = _now_iso()
        event = _commit_item_locked(
            slot_key, item, "report", checked_summary, status=checked_status
        )
        return {"item": item, "event": event}


def read_work_brief(slot_key: str, item_id: str) -> dict[str, Any] | None:
    """What ONE worker may see about its own item, and nothing more.

    Not the conductor's goal, not its other items, not a sibling's state: a worker
    has no reason to see its peers and every reason not to be able to. Phase 2's
    ``work_brief`` tool is this function behind the binding resolver.
    """
    item = read_work_item(slot_key, item_id)
    if item is None:
        return None
    return {
        "item_id": item.item_id,
        "title": item.title,
        "acceptance": item.acceptance,
        "round": item.round,
        "decision": item.decision,
        "status": item.status,
        "summary": item.summary,
    }


def accept_batch(items: list[WorkItem]) -> dict[str, Any]:
    """The ``{"items": [...]}`` document ``accept_eval.py`` reads on stdin.

    Composed from ``acceptance`` ALONE. The worker's claimed ``pr`` is deliberately
    absent: filling ``acceptance.pr`` from a worker's report would let a worker name
    any already-green pull request and pass. The claim is surfaced beside the item
    for a conductor to promote explicitly, which turns the two-phase acceptance the
    skill performs by hand into a visible field without moving control of the bar.

    An item whose bar is not yet concrete — a ``"TBD"`` pull request number, a blank
    field — is left out, which is the other half of that same two-phase shape: the
    skill promises the omission, and doing it here is what makes the promise true.
    :func:`is_acceptance_concrete` is the test, and the read surfaces it per item so a
    conductor can see WHY an item is missing from the batch.

    ``status`` rides along on each entry, and the batch is deliberately NOT filtered by
    it. The conductor applies the "only ``done`` items" filter — that judgement is its
    own, and the seam is load-bearing — but it should not need a second lookup to
    apply it. ``accept_eval.py`` reads ``id`` and ``accept`` and ignores the rest.
    """
    return {
        "items": [
            # ``id`` / ``accept`` are the keys accept_eval.py reads; ``item_id`` /
            # ``acceptance`` are this store's field names. The rename happens here,
            # at the one seam between the two, so neither side learns the other's
            # vocabulary.
            {"id": item.item_id, "accept": item.acceptance, "status": item.status}
            for item in items
            if not item.is_terminal and is_acceptance_concrete(item.acceptance)
        ]
    }


def apply_acceptance_update(
    slot_key: str,
    item_id: str,
    *,
    acceptance: Any,
) -> dict[str, Any]:
    """Replace ONE item's ``acceptance``, appending a ``decision`` event.

    This is the write behind the ``accept`` tool action, and it exists as its own
    function rather than as a seventh :data:`CONDUCTOR_ACTIONS` member on purpose.
    Its job is the promotion :func:`accept_batch` deliberately refuses to do for
    the worker: a conductor that has READ a worker's claimed ``pr`` and checked it
    substitutes the real number into the bar, turning the manual omission the
    skill performs by hand into one visible write. The bar still only ever moves
    under the conductor's own key — the worker has no parameter that reaches here.

    The event is kind ``decision`` because promoting a bar IS a conductor
    decision, and because :data:`EVENT_KINDS` is a closed vocabulary the store's
    readers (and Phase 3's probe) already dispatch on; a seventh kind would be a
    schema change for a write that is semantically one of the six.
    """
    checked_id = _require_item_id(item_id)
    if not acceptance:
        # REFUSED, not coerced. :func:`_require_acceptance` answers ``{}`` for
        # ``None`` because ``create`` may legitimately open an item whose bar is not
        # known yet — but a PROMOTION never legitimately clears one. Without this,
        # an ``accept`` that omitted its argument replaced the real condition with
        # ``{}``, :func:`accept_batch` then dropped the item for having no
        # acceptance, ``accept_eval.py`` never evaluated it, and the caller was told
        # 200. That is conductor-owned state lost silently and unrecoverably, which
        # is the one outcome a cap-and-refuse store must not produce.
        raise WorkLedgerError(
            "accept needs the acceptance object to promote; it cannot clear the bar",
            code=CODE_INVALID_VALUE,
            field="acceptance",
        )
    checked_acceptance = _require_acceptance(acceptance)

    with item_lock(slot_key, checked_id, create=False):
        item = read_work_item(slot_key, checked_id)
        if item is None:
            raise WorkLedgerError(
                f"unknown item {checked_id!r}", code=CODE_UNKNOWN_ITEM, field="item_id"
            )
        if item.is_terminal:
            raise WorkLedgerError(f"item {checked_id!r} is {item.state}", code=CODE_ITEM_CLOSED)
        item.acceptance = checked_acceptance
        # A pending run belongs to the bar it measured; the new bar starts its own.
        item.pending_since = None
        event = _commit_item_locked(
            slot_key, item, "decision", "acceptance promoted by the conductor"
        )
        return {"item": item, "event": event}
