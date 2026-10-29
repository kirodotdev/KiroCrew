"""The hub's store half of a remote worker (rfc-conductor-remote-dispatch §2, §3).

A conductor on this gateway (the hub) can bind one of its items to a worker
session on another crew. Three store pieces serve that item, and nothing here
talks to a peer: the gateway half (:mod:`kiro_crew.dashboard.handlers.remote_ingest`)
fetches and calls these.

* :func:`apply_remote_bind` records the pair ``worker_session_key`` (the PEER's
  slot key) + ``worker_instance_id`` (the crew). No binding file is written: no
  local worker exists to resolve one, and the peer's own mailbox is its binding.
* :func:`apply_remote_report` writes a report the peer's worker made, ingested on
  the conductor's read. It is a worker-owned write like
  :func:`~kiro_crew.work_ledger_writes.apply_worker_report`, with one difference:
  ``last_report_at`` is the PEER's stamp, which is what keys the ingest so a
  repeat read adds nothing.
* The remote state record (``work-ledger/remote/<board>/<item>.json``) holds what
  the hub last learnt about the peer slot: ``running``, ``closed``,
  ``pending_approval``, the crew's connection state and since when it is down. It
  is a gateway observation like the undelivered record, kept outside the
  conductor's directory so a rebuild never prunes it. With none yet, both
  liveness answers read ``False``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew.session_ledger import _store_name, resolved_within
from kiro_crew.work_ledger_records import (
    BLOCKED_REASONS,
    CODE_ALREADY_BOUND,
    CODE_INVALID_STATUS,
    CODE_INVALID_VALUE,
    CODE_ITEM_CLOSED,
    CODE_UNKNOWN_ITEM,
    MAX_SUMMARY_CHARS,
    WORKER_STATUSES,
    WorkItem,
    WorkLedgerError,
    logger,
)
from kiro_crew.work_ledger_rules import (
    _require_artifacts,
    _require_choice,
    _require_pr,
    _require_text,
    parse_stamp,
)
from kiro_crew.work_ledger_store import (
    _REMOTE_DIR,
    _commit_item_locked,
    _read_json_record,
    _require_item_id,
    _work_ledger_root,
    _write_record,
    item_lock,
    read_work_item,
)
from kiro_crew.work_vocab import WORK_REASON_STATUSES

#: The root directory of the remote state records; one of the store's reserved dirs.
REMOTE_DIR = _REMOTE_DIR

#: Longest crew id kept, the cap the instance registry puts on an id.
MAX_INSTANCE_ID_CHARS = 128

#: Longest peer slot key kept, the cap ``bind`` puts on a worker key.
MAX_PEER_SLOT_CHARS = 512

#: The crew connection states a record may hold (``TunnelState`` values) plus
#: ``unknown`` for a crew the registry does not name.
CREW_STATES: frozenset[str] = frozenset(
    {"connected", "connecting", "disconnected", "error", "stopped", "unknown"}
)


def remote_dir() -> Path:
    return _work_ledger_root() / REMOTE_DIR


def remote_state_path(board: str, item_id: str) -> Path:
    """Where *board*'s remote record for *item_id* lives. Raises on a bad key or id."""
    if not board or "\0" in board:
        raise WorkLedgerError(f"invalid slot key: {board!r}", code=CODE_INVALID_VALUE)
    checked = _require_item_id(item_id)
    resolved = resolved_within(remote_dir(), f"{_store_name(board)}/{checked}.json")
    if resolved is None:
        raise WorkLedgerError(f"path traversal blocked for {board!r}", code=CODE_INVALID_VALUE)
    return resolved


@dataclass
class RemoteState:
    """What the hub last learnt about one remote worker's slot."""

    running: bool = False
    closed: bool = False
    pending_approval: bool = False
    crew_state: str = "unknown"
    crew_down_since: str | None = None
    ingested_at: str | None = None
    #: When a pull of this item was last tried, whatever came of it. Reads pull
    #: the items tried longest ago first, so a slow crew cannot starve the rest.
    attempted_at: str | None = None
    #: Why the last pull did not ingest, a stable code, or ``None``.
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "closed": self.closed,
            "pending_approval": self.pending_approval,
            "crew_state": self.crew_state,
            "crew_down_since": self.crew_down_since,
            "ingested_at": self.ingested_at,
            "attempted_at": self.attempted_at,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> RemoteState:
        """Coerce a stored record. Never raises; a bad field reads as its default."""
        if not isinstance(raw, dict):
            return cls()

        def _flag(name: str) -> bool:
            return raw.get(name) is True

        def _text(name: str) -> str | None:
            value = raw.get(name)
            return value[:64] if isinstance(value, str) and value else None

        crew_state = raw.get("crew_state")
        return cls(
            running=_flag("running"),
            closed=_flag("closed"),
            pending_approval=_flag("pending_approval"),
            crew_state=crew_state if crew_state in CREW_STATES else "unknown",
            crew_down_since=_text("crew_down_since"),
            ingested_at=_text("ingested_at"),
            attempted_at=_text("attempted_at"),
            error=_text("error"),
        )


def read_remote_state(board: str, item_id: str) -> RemoteState | None:
    """The stored record, or ``None`` when there is none or it is unreadable."""
    try:
        raw = _read_json_record(remote_state_path(board, item_id))
    except (WorkLedgerError, OSError):
        return None
    return None if raw is None else RemoteState.from_dict(raw)


def write_remote_state(board: str, item_id: str, state: RemoteState) -> None:
    path = remote_state_path(board, item_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_record(path, state.to_dict())


def remote_liveness(board: str, item: WorkItem) -> tuple[bool, bool] | None:
    """``(running, closed)`` for a remote item from its stored record, else ``None``.

    ``None`` means *item* is not remote and the caller asks its own slot table.
    A remote item never reaches the peer from here: the answer is what the last
    ingest stored, so a read row and the wake gate agree. With no record yet both
    read ``False``. Never raises.
    """
    if not item.worker_instance_id:
        return None
    try:
        stored = read_remote_state(board, item.item_id)
    except Exception:  # noqa: BLE001 - a liveness read must not fail a read or a tick
        logger.debug("remote state read failed for %s", item.item_id, exc_info=True)
        stored = None
    if stored is None:
        return False, False
    return stored.running, stored.closed


def item_liveness(
    board: str,
    item: WorkItem,
    *,
    running: Any,
    closed: Any,
    looping: Any = None,
    remote: tuple[bool, bool] | None = None,
) -> tuple[bool, bool, bool]:
    """``(running, closed, looping)`` for *item*'s worker, the one answer every reader uses.

    A remote item answers from its stored record (:func:`remote_liveness`) and never
    from this gateway's slot table, where the peer's key names nothing; it holds no
    local loop. A local item asks the three resolvers with its worker key. The read
    rows, the board and the wake probe all call this, so they cannot disagree.

    *remote* is that stored answer already read (off the event loop, by a route);
    omitted, a remote item's record is read here, which blocks.
    """
    if item.worker_instance_id and remote is None:
        remote = remote_liveness(board, item)
    if item.worker_instance_id and remote is not None:
        return remote[0], remote[1], False
    key = item.worker_session_key or ""
    return bool(running(key)), bool(closed(key)), bool(looping(key)) if looping else False


def remote_liveness_map(board: str, items: Any) -> dict[str, tuple[bool, bool]]:
    """``{item_id: (running, closed)}`` for every remote item in *items*. BLOCKING."""
    out: dict[str, tuple[bool, bool]] = {}
    for item in items:
        answer = remote_liveness(board, item)
        if answer is not None:
            out[item.item_id] = answer
    return out


def _require_peer_text(value: Any, cap: int, name: str) -> str:
    text = _require_text(value, cap, name)
    if "\0" in text or "/" in text or "\\" in text:
        raise WorkLedgerError(f"{name} has a bad shape", code=CODE_INVALID_VALUE, field=name)
    return text


def apply_remote_bind(
    slot_key: str, item_id: str, *, worker_session_key: Any, worker_instance_id: Any
) -> dict[str, Any]:
    """Bind *item_id* to a peer slot on a crew. Conductor-owned fields only.

    Refuses as the local bind does: an unknown, closed or already bound item. Writes
    no binding file, so no local session can resolve this item as its own.
    """
    checked_id = _require_item_id(item_id)
    slot = _require_peer_text(worker_session_key, MAX_PEER_SLOT_CHARS, "worker_session_key")
    crew = _require_peer_text(worker_instance_id, MAX_INSTANCE_ID_CHARS, "worker_instance_id")
    with item_lock(slot_key, checked_id, create=False):
        item = read_work_item(slot_key, checked_id)
        if item is None:
            raise WorkLedgerError(
                f"unknown item {checked_id!r}", code=CODE_UNKNOWN_ITEM, field="item_id"
            )
        if item.is_terminal:
            raise WorkLedgerError(f"item {checked_id!r} is {item.state}", code=CODE_ITEM_CLOSED)
        if item.worker_session_key:
            raise WorkLedgerError(
                f"item {checked_id!r} is already bound",
                code=CODE_ALREADY_BOUND,
                field="worker_session_key",
            )
        item.worker_session_key = slot
        item.worker_instance_id = crew
        event = _commit_item_locked(slot_key, item, "bind", f"{slot} on crew {crew}")
    return {"conductor": None, "item": item, "event": event}


def apply_remote_report(
    slot_key: str,
    item_id: str,
    *,
    status: Any,
    summary: Any,
    artifacts: Any = None,
    pr: Any = None,
    reason: Any = None,
    reported_at: Any,
) -> dict[str, Any]:
    """Write one ingested peer report: the worker's fields, stamped *reported_at*.

    The same checks as a local report, and the item must be remote. *reported_at*
    is the peer's stamp, kept as ``last_report_at`` so the next ingest compares
    against it.
    """
    checked_status = _require_choice(status, WORKER_STATUSES, "status", CODE_INVALID_STATUS)
    checked_summary = _require_text(summary, MAX_SUMMARY_CHARS, "summary", required=False)
    checked_artifacts = _require_artifacts(artifacts)
    checked_pr = _require_pr(pr)
    checked_reason: str | None = None
    if reason is not None:
        checked_reason = _require_choice(reason, BLOCKED_REASONS, "reason", CODE_INVALID_VALUE)
        if checked_status not in WORK_REASON_STATUSES:
            checked_reason = None
    stamp = _require_text(reported_at, 64, "last_report_at")
    # The stamp is read back as a time (staleness, the next ingest's compare),
    # so a peer value that is not an ISO-8601 stamp is refused here.
    if parse_stamp(stamp) is None:
        raise WorkLedgerError(
            "last_report_at is not an ISO-8601 timestamp",
            code=CODE_INVALID_VALUE,
            field="last_report_at",
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
        if not item.worker_instance_id:
            raise WorkLedgerError(
                f"item {checked_id!r} has no remote worker", code=CODE_INVALID_VALUE
            )
        item.status = checked_status
        item.reason = checked_reason
        item.summary = checked_summary
        item.artifacts = checked_artifacts
        if checked_pr is not None:
            item.pr = checked_pr
        item.last_report_at = stamp
        # The peer report's own identity rides into the hub event id, so two peer
        # reports filed in one second with the same text stay two events.
        event = _commit_item_locked(
            slot_key,
            item,
            "report",
            checked_summary,
            status=checked_status,
            id_salt="\0" + stamp,
        )
    return {"item": item, "event": event}
