"""The peer's copy of a hub's work item: a mailbox, not a second ledger.

A conductor on a hub creates a worker session on this crew with
``POST /api/chat/slots`` carrying a ``work_item`` (rfc-conductor-remote-dispatch
§2, §3). This gateway stores that item under the conductor key
``hub:<the hub conductor's key>`` and binds the new slot to it, so the worker's own
``work_brief`` and ``work_report`` resolve unchanged. This gateway never patrols
the item; the hub reads it back through ``GET api/chat/slots/{slot}/work-item``.

Writers stay disjoint, as :mod:`kiro_crew.work_ledger` splits them: the hub owns
``title``, ``acceptance`` and ``decision`` (set at create, ``decision`` again by
``PATCH``), and the worker owns what its reports write. Nothing here takes a
worker-owned field.

The mailbox writes are cache-only: no conductor session on this crew holds a crew
log for a ``hub:`` board. The item stays unstamped (``recorded_at`` empty), so the
worker's first report carries the whole item into the worker's own log, and a
decision written here clears the stamp so the next report carries it too.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from kiro_crew.session_ledger import ledger_key
from kiro_crew.work_ledger import (
    CODE_INVALID_VALUE,
    CODE_ITEM_CLOSED,
    CODE_UNKNOWN_ITEM,
    MAX_DECISION_CHARS,
    MAX_TITLE_CHARS,
    WorkEvent,
    WorkItem,
    WorkLedgerError,
    _commit_item_locked,
    _now_iso,
    _require_acceptance,
    _require_item_id,
    _require_record_fits,
    _require_text,
    _restore_text,
    _slot_key_is_shaped,
    apply_conductor_action,
    binding_lock,
    binding_path,
    create_bound_item,
    ensure_conductor,
    item_events_path,
    item_lock,
    item_path,
    read_binding,
    read_events,
    read_work_item,
)

logger = logging.getLogger(__name__)

#: The conductor-key prefix that marks a board as a hub's mailbox on this crew.
MAILBOX_PREFIX = "hub:"


#: The decision a mailbox item is closed with once its worker slot is gone.
_CLOSED_DECISION = "the worker's slot on this crew was closed"

#: An item whose worker's report could not carry it whole in one crew-log line.
CODE_WORK_ITEM_TOO_LARGE = "work_item_too_large"

#: The longest hub conductor key accepted, the cap ``bind`` puts on a worker key.
MAX_CONDUCTOR_KEY_CHARS = 512

#: The ``work_item`` keys a create may carry. Anything else is refused, so a hub
#: cannot reach a field the RFC does not give it.
WORK_ITEM_KEYS = frozenset({"conductor_key", "item_id", "title", "acceptance", "decision"})


@dataclass(frozen=True)
class MailboxRequest:
    """A validated ``work_item`` from a create body."""

    conductor_key: str
    item_id: str
    title: str
    acceptance: dict[str, Any]
    decision: str

    @property
    def board(self) -> str:
        return mailbox_key(self.conductor_key)

    def item(self, worker_key: str) -> WorkItem:
        """The item this request stores, bound to *worker_key*."""
        now = _now_iso()
        return WorkItem(
            item_id=self.item_id,
            title=self.title,
            acceptance=self.acceptance,
            state="open",
            decision=self.decision,
            decided_at=now if self.decision else None,
            worker_session_key=worker_key,
            created_at=now,
        )


def mailbox_key(conductor_key: str) -> str:
    return MAILBOX_PREFIX + conductor_key


def is_mailbox(board: str) -> bool:
    return board.startswith(MAILBOX_PREFIX)


def parse_work_item(raw: Any) -> MailboxRequest:
    """Validate a create body's ``work_item``. Raises :class:`WorkLedgerError`.

    Every cap is the local store's own (title, decision, acceptance and the stored
    record's read ceiling), so a peer copy holds exactly what a local item could.
    """
    if not isinstance(raw, dict):
        raise WorkLedgerError("work_item must be an object", code=CODE_INVALID_VALUE)
    unknown = sorted(set(raw) - WORK_ITEM_KEYS)
    if unknown:
        raise WorkLedgerError(
            f"work_item has unknown field(s) {unknown}; expected {sorted(WORK_ITEM_KEYS)}",
            code=CODE_INVALID_VALUE,
            field=unknown[0],
        )
    conductor = _require_text(raw.get("conductor_key"), MAX_CONDUCTOR_KEY_CHARS, "conductor_key")
    if not _slot_key_is_shaped(conductor):
        raise WorkLedgerError(
            "conductor_key must be a slot key", code=CODE_INVALID_VALUE, field="conductor_key"
        )
    item_id = _require_item_id(_require_text(raw.get("item_id"), 64, "item_id"))
    title = _require_text(raw.get("title"), MAX_TITLE_CHARS, "title")
    if raw.get("acceptance") is None:
        raise WorkLedgerError("acceptance is required", code=CODE_INVALID_VALUE, field="acceptance")
    acceptance = _require_acceptance(raw.get("acceptance"))
    decision = _require_text(raw.get("decision"), MAX_DECISION_CHARS, "decision", required=False)
    request = MailboxRequest(
        conductor_key=conductor,
        item_id=item_id,
        title=title,
        acceptance=acceptance,
        decision=decision,
    )
    _require_record_fits(request.item("w" * MAX_CONDUCTOR_KEY_CHARS).to_dict())
    return request


def open_mailbox(request: MailboxRequest, worker_slot_key: str) -> WorkItem:
    """Store *request*'s item and bind *worker_slot_key* to it. BLOCKING.

    Through the store's own create path (``create_bound_item``), so the board's
    open-item caps and ``created_total`` apply as for any other create. A second
    create for the same item is refused ``duplicate_item``.
    """
    board = request.board
    ensure_conductor(board)
    result = create_bound_item(
        board,
        request.item_id,
        title=request.title,
        acceptance=request.acceptance,
        decision=request.decision,
        worker_session_key=ledger_key(worker_slot_key),
    )
    item: WorkItem = result["item"]
    return item


def close_mailbox(worker_slot_key: str) -> str | None:
    """Close the hub item *worker_slot_key* is bound to. BLOCKING.

    Called when the slot is deleted, so a mailbox item never outlives its worker
    open and the board can be swept. A copy the worker never reported on is
    DISCARDED with its binding (nothing on it is the worker's, so nothing is lost),
    which is what lets a hub whose own bind failed dispatch the same item to this
    crew again. A copy that ever reported is closed ``abandoned``, keeping its
    reports for the hub to read. Returns the item id, or ``None`` when the slot
    holds no open hub item.
    """
    bound = bound_mailbox(worker_slot_key)
    if bound is None:
        return None
    if not names_worker(bound, worker_slot_key):
        # A binding the item does not name back is stale and closes nothing.
        return None
    board, item_id = bound
    if _discard_if_unreported(board, item_id, ledger_key(worker_slot_key)):
        return item_id
    try:
        apply_conductor_action(
            board,
            "close",
            item_id=item_id,
            state="abandoned",
            decision=_CLOSED_DECISION,
        )
    except WorkLedgerError as exc:
        if exc.code in (CODE_ITEM_CLOSED, CODE_UNKNOWN_ITEM):
            return None
        raise
    return item_id


def _discard_if_unreported(board: str, item_id: str, worker: str) -> bool:
    """Remove an open mailbox copy with no report, and its binding. Whether it did.

    Under the item lock then the worker's binding lock (the store's order), and
    re-checked under both: a report that lands first keeps the copy. A delete that
    fails answers False, so the copy is closed rather than left half-removed.
    """
    with item_lock(board, item_id, create=False), binding_lock(worker):
        item = read_work_item(board, item_id)
        if (
            item is None
            or item.is_terminal
            or item.status is not None
            or item.worker_session_key != worker
            or read_binding(worker) != (board, item_id)
        ):
            return False
        # The item first and checked: a delete that fails leaves the copy and its
        # binding whole, and the caller closes it instead. The binding goes last;
        # one left behind names an item that is gone and resolves nothing.
        try:
            item_events_path(board, item_id).unlink(missing_ok=True)
            item_path(board, item_id).unlink(missing_ok=True)
        except OSError:
            logger.warning("remote work item %s: could not discard; closing it", item_id)
            return False
        try:
            binding_path(worker).unlink(missing_ok=True)
        except OSError:
            logger.warning("remote work item %s: stale binding left for %s", item_id, worker)
    return True


def discard_mailbox(request: MailboxRequest, worker_slot_key: str) -> None:
    """Undo :func:`open_mailbox` for a create that was retracted after it. BLOCKING.

    When the worker's binding names this item, removes the binding, then the item
    and its event log, in the store's lock order; otherwise touches nothing. Never
    raises: the create is already failing with its own error.
    """
    board = request.board
    worker = ledger_key(worker_slot_key)
    try:
        with item_lock(board, request.item_id, create=False), binding_lock(worker):
            # Only this worker's own write binds it to this item, so an item it
            # is not bound to (another slot's, on a refused duplicate) is left.
            if read_binding(worker) != (board, request.item_id):
                return
            _restore_text(binding_path(worker), None)
            _restore_text(item_path(board, request.item_id), None)
            _restore_text(item_events_path(board, request.item_id), None)
    except (OSError, WorkLedgerError):
        logger.warning("remote work item %s: could not undo the mailbox", request.item_id)


def bound_mailbox(worker_slot_key: str) -> tuple[str, str] | None:
    """``(board, item_id)`` when *worker_slot_key* is bound to a hub mailbox item."""
    binding = read_binding(ledger_key(worker_slot_key))
    if binding is None or not is_mailbox(binding[0]):
        return None
    return binding


def names_worker(bound: tuple[str, str], worker_slot_key: str) -> bool:
    """Whether the mailbox item *bound* names *worker_slot_key* as its worker."""
    item = read_work_item(*bound)
    return item is not None and item.worker_session_key == ledger_key(worker_slot_key)


def read_mailbox(
    board: str, item_id: str, *, limit: int
) -> tuple[WorkItem, list[WorkEvent]] | None:
    """The stored item and its newest *limit* events, or ``None`` when it is gone."""
    item = read_work_item(board, item_id)
    if item is None:
        return None
    return item, read_events(board, item_id, limit=limit)


def item_exists(request: MailboxRequest) -> bool:
    """Whether *request*'s item is already stored on this crew. BLOCKING.

    Open or closed: a closed copy is never replaced here. Giving an item a new
    worker belongs to retire and redispatch (rfc wave 8), with binding history.
    """
    return item_path(request.board, request.item_id).exists()


def write_decision(
    board: str,
    item_id: str,
    decision: Any,
    *,
    fits: Callable[[WorkItem], bool],
) -> tuple[WorkItem, WorkEvent]:
    """Replace the hub-owned ``decision`` on a mailbox item. BLOCKING.

    The only field this writes, with the one event that explains it. A closed or
    missing item is refused. ``recorded_at`` is cleared, so the worker's next report
    carries the whole item, this decision included, into its crew log. *fits* says
    whether that report can still fit one log line; a decision that would make it
    too large is refused with nothing written.
    """
    checked = _require_text(decision, MAX_DECISION_CHARS, "decision")
    with item_lock(board, _require_item_id(item_id), create=False):
        item = read_work_item(board, item_id)
        if item is None:
            raise WorkLedgerError(
                f"unknown item {item_id!r}", code=CODE_UNKNOWN_ITEM, field="item_id"
            )
        if item.is_terminal:
            raise WorkLedgerError(f"item {item_id!r} is {item.state}", code=CODE_ITEM_CLOSED)
        if not fits(dataclasses.replace(item, decision=checked, recorded_at="")):
            raise WorkLedgerError(
                "with this decision the worker's next report could not carry the whole "
                "item in one crew-log line; shorten the decision",
                code=CODE_WORK_ITEM_TOO_LARGE,
                field="decision",
            )
        item.decision = checked
        item.decided_at = _now_iso()
        item.recorded_at = ""
        event = _commit_item_locked(board, item, "decision", checked)
    return item, event
