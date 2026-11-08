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

from kiro_crew import work_preview
from kiro_crew.atomic_write import atomic_write
from kiro_crew.session_ledger import _store_name
from kiro_crew.work_ledger_records import (
    BLOCKED_REASONS,
    CODE_ALREADY_BOUND,
    CODE_DEPTH_EXCEEDED,
    CODE_DUPLICATE_ITEM,
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
    MAX_EVENT_TEXT_CHARS,
    MAX_GOAL_CHARS,
    MAX_GOAL_CONCURRENCY_CAP,
    MAX_GOAL_ITEM_CAP,
    MAX_ITEMS_PER_CONDUCTOR,
    MAX_SUMMARY_CHARS,
    MAX_TITLE_CHARS,
    SCHEMA_VERSION,
    TERMINAL_ITEM_STATES,
    VERDICTS,
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
from kiro_crew.work_vocab import WORK_REASON_STATUSES, worker_statuses

# Bindings: one file per worker, replaced whole only by ``bind`` under the worker's
# binding lock, so two conductors cannot both believe they bound it.


def read_binding(worker_slot_key: str, *, strict: bool = False) -> tuple[str, str] | None:
    """The ``(conductor_slot_key, item_id)`` *worker_slot_key* is bound to.

    ``None`` when unbound (answered as ``not_bound``), including a binding that names
    a malformed item id: a worker cannot repair its own binding. With ``strict``, a
    present but unreadable file raises instead, so the bind guard fails closed.
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

    A worker has ONE binding file, so a second bind would permanently repoint its
    only report channel away from the first item. A binding is stale, and may be
    replaced, when its item is terminal, its item file is gone, or its item does not
    name this worker back (a bind killed between its binding and item writes).

    FAILS CLOSED on a transient read error: the prior item is not under this lock,
    so an ``OSError`` propagates and the caller retries.

    CALL UNDER THE WORKER'S BINDING LOCK.
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


# Conductor writes


def ensure_conductor(
    slot_key: str,
    *,
    goal: str = "",
    depth: int = 0,
    parent_item: str | None = None,
) -> ConductorRecord:
    """Create *slot_key*'s ledger if absent, and return the record either way.

    Idempotent; never overwrites an existing goal, round or depth. Use
    ``apply_conductor_action(slot_key, "goal", ...)`` to change the goal.

    *depth* and *parent_item* are server-owned (from :func:`child_depth`, never a
    model). A depth :func:`may_conduct` denies is refused here too, because this is
    also reachable with a depth that did not come through :func:`child_depth`.
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

    Never writes the worker's fields (``status``, ``summary``, ``artifacts``, ``pr``,
    ``last_report_at``); only :func:`apply_worker_report` does. Each sits behind its
    own tool, so ownership is enforced by which function a caller can reach.

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

    Refuses with ``CODE_NO_LEDGER`` when the header file is ABSENT: the ledger was
    purged while this writer waited, and writing the pre-lock snapshot back would
    resurrect a header (with no breadcrumb a later purge could name).

    Refuses with the same code when the header is PRESENT but unreadable: only it
    holds :attr:`ConductorRecord.created_total`, and the pre-lock snapshot may hold
    it low, which would admit creates past what the fold retains. The message says
    which case it found; the remedy is :func:`rebuild_from_projection`.
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

    Bounds are checked before the lock; omitted fields come from the record re-read
    inside it, never from the pre-lock *record*, so two racing partial updates
    cannot restore each other's stale values.

    ``new_goal=True`` (with the new goal text) restarts the goal's item count:
    :attr:`ConductorRecord.goal_items_base` moves to the create total. Rewording the
    goal alone keeps the count.
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
        # Re-read under the lock and required present and readable; see
        # ``_header_under_lock``.
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

    The caps are checked by :func:`_admit_create_locked`. The goal's
    :attr:`ConductorRecord.item_cap` refuses nothing (it is reported as
    :attr:`ConductorRecord.cap_exceeded`), and nothing bounds a board's lifetime
    creates, since the ``work`` fold forgets old closed items.

    The header (with ``created_total`` bumped) is written FIRST, then the item, and
    restored if the item write raises. A failure between them over-counts, the safe
    side. Every refusal, the item's size included, comes before the header write, so
    a refused create leaves every file byte-identical.

    The conductor lock, not the item's, because the caps are properties of the set:
    two creates must not both count thirty-one and both write.

    A depth :func:`may_conduct` denies is refused: an item is a dispatch, and
    :func:`rebuild_from_projection` can keep a stored depth no guard admitted.
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
        live = _admit_create_locked(slot_key)
        if checked_round is None:
            checked_round = live.round
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


def _admit_create_locked(slot_key: str) -> ConductorRecord:
    """The live header for one more create, or the refusal. HOLD THE CONDUCTOR LOCK.

    Every bound a create is held to, shared by :func:`_create_item` and
    :func:`create_bound_item` so both count and refuse alike. Writes nothing; the
    caller bumps ``created_total`` in the same hold.
    """
    # Re-read inside the lock, so the default round sees a concurrent ``goal``
    # bump; a missing or torn header refuses (see ``_header_under_lock``).
    live = _header_under_lock(slot_key, "no item can be created in it")
    if live.created_total == 0:
        # One-time backfill for a header from before the counter: every stored
        # record is a create it admitted. Persisted by the caller's header write.
        live.created_total = len(_stored_item_ids(slot_key))
    # A header from before the per-goal count: its goal counts from here.
    _settle_goal_base(live)
    # Only OPEN items count toward either fan-out cap; a torn record has no
    # state and counts toward neither.
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
    # The goal's own fan-out bound, only on a board that recorded a goal (a queue
    # board never does). A person decides past it.
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
    return live


def create_bound_item(
    slot_key: str,
    item_id: str,
    *,
    title: Any,
    acceptance: Any,
    decision: Any,
    worker_session_key: str,
) -> dict[str, Any]:
    """Create an item under a caller-chosen *item_id*, already bound to a worker.

    A peer's create for a hub's item (remote dispatch), keeping the hub's id. Like
    :func:`_create_item` then ``bind`` in one hold, with the same caps and lock order
    (conductor, item, binding). A held *item_id* is refused
    :data:`CODE_DUPLICATE_ITEM`. Refusals leave every record byte-identical (an
    ``already_bound`` one may leave an empty lock file, which no reader counts); a
    failure after the first write restores the header and binding.
    """
    checked_id = _require_item_id(item_id)
    checked_title = _require_text(title, MAX_TITLE_CHARS, "title")
    checked_acceptance = _require_acceptance(acceptance)
    checked_decision = _require_text(decision, MAX_DECISION_CHARS, "decision", required=False)
    checked_worker = _require_text(worker_session_key, 512, "worker_session_key")
    with _existing_conductor_lock(slot_key):
        live = _admit_create_locked(slot_key)
        if (items_dir(slot_key) / f"{checked_id}.json").exists():
            raise WorkLedgerError(
                f"item {checked_id!r} already exists on this board",
                code=CODE_DUPLICATE_ITEM,
                field="item_id",
            )
        now = _now_iso()
        item = WorkItem(
            item_id=checked_id,
            title=checked_title,
            acceptance=checked_acceptance,
            state="open",
            round=live.round,
            decision=checked_decision,
            decided_at=now if checked_decision else None,
            worker_session_key=checked_worker,
            created_at=now,
        )
        _require_record_fits(item.to_dict())
        header_path = conductor_dir(slot_key) / _CONDUCTOR_FILE
        with item_lock(slot_key, checked_id), binding_lock(checked_worker):
            prior_binding = _read_binding_text(checked_worker)
            _refuse_if_worker_holds_open_item(checked_worker)
            header_before = _read_text_or_none(header_path)
            live.created_total += 1
            _write_record(header_path, live.to_dict())
            try:
                # Binding before item, as ``bind`` orders them: a failure between
                # the two leaves a stale binding, never an item naming a worker
                # with no binding behind it.
                _write_binding(checked_worker, slot_key, checked_id)
                event = _commit_item_locked(slot_key, item, "create", checked_title)
            except BaseException:
                _restore_binding_text(checked_worker, prior_binding)
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

    Fields are validated before the lock, so a shape refusal creates no lock file;
    refusals that need the stored record (unknown, closed, already bound) happen
    under it.
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
                # Binding FIRST: an item naming a worker with no binding would
                # refuse every retry with ``already_bound``, while a stale binding
                # is replaced by the next bind. A failed item write restores it.
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


# Worker writes


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

    No ``verdict``, ``state``, ``acceptance``, ``decision`` or ``round`` parameter,
    so a worker cannot accept itself or widen its bar; ``status: done`` only
    triggers the conductor's ``accept_eval.py``.

    *slot_key* and *item_id* are the conductor's key and the bound item, resolved
    by the caller from the worker's own binding (:func:`read_binding`), never taken
    from the worker.

    ``status: progress`` twice in a row collapses in the event log — see
    :func:`_coalesce_progress` — but the item's own fields always hold the newest
    report.
    """
    # ``reply`` is admitted only under the remote-dispatch preview. Checked before
    # the item is read, so with the preview off a ``reply`` on a closed item is
    # refused ``invalid_status``, not ``item_closed``.
    admitted = frozenset(worker_statuses(work_preview.remote_dispatch()))
    checked_status = _require_choice(status, admitted, "status", CODE_INVALID_STATUS)
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


# Person writes


def apply_person_steer(
    slot_key: str, item_id: str, text: Any, *, id_salt: str = ""
) -> dict[str, Any] | None:
    """Record a person's steer into a worker as one ``steer`` event on its item.

    *id_salt* tells apart two steers with the same text in the same second: an
    approval answer passes the id of the report it answers.

    The event is the whole write; no item field moves. *text* is already redacted
    by the caller and is cut at the event length cap. ``None`` for a closed item.
    """
    checked_text = text[:MAX_EVENT_TEXT_CHARS] if isinstance(text, str) else ""
    checked_id = _require_item_id(item_id)
    with item_lock(slot_key, checked_id, create=False):
        item = read_work_item(slot_key, checked_id)
        if item is None:
            raise WorkLedgerError(
                f"unknown item {checked_id!r}", code=CODE_UNKNOWN_ITEM, field="item_id"
            )
        if item.is_terminal:
            return None
        event = _commit_item_locked(slot_key, item, "steer", checked_text, id_salt=id_salt)
        return {"item": item, "event": event}


def read_work_brief(slot_key: str, item_id: str) -> dict[str, Any] | None:
    """What ONE worker may see about its own item, and nothing more.

    Not the conductor's goal, its other items or a sibling's state. The
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

    Composed from ``acceptance`` ALONE: filling ``acceptance.pr`` from a worker's
    claimed ``pr`` would let a worker name any green pull request and pass. A
    conductor promotes the claim explicitly (:func:`apply_acceptance_update`).

    An item whose bar is not concrete (a ``"TBD"`` pull request, a blank field) is
    left out; :func:`is_acceptance_concrete` is the test, surfaced per item on read.

    ``status`` rides along but the batch is NOT filtered by it: the "only ``done``"
    filter is the conductor's own judgement. ``accept_eval.py`` reads ``id`` and
    ``accept`` and ignores the rest.
    """
    return {
        "items": [
            # Renamed to accept_eval.py's keys here, at the one seam between them.
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

    The write behind the ``accept`` tool action: a conductor that has checked a
    worker's claimed ``pr`` puts the real number into the bar, the promotion
    :func:`accept_batch` refuses to do. Only the conductor's key reaches it.

    The event is kind ``decision`` because promoting a bar is a conductor decision
    and :data:`EVENT_KINDS` is a closed vocabulary readers dispatch on.
    """
    checked_id = _require_item_id(item_id)
    if not acceptance:
        # Refused, not coerced: :func:`_require_acceptance` maps ``None`` to ``{}``
        # for ``create``, but a promotion that cleared the bar would silently drop
        # the item from :func:`accept_batch` and lose conductor-owned state.
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
