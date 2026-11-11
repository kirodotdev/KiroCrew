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
from datetime import datetime, timezone
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
    _now_iso,
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
    _CONDUCTOR_FILE,
    _ITEMS_DIR,
    _REMOTE_DIR,
    RESERVED_ROOT_DIRS,
    _commit_item_locked,
    _read_json_record,
    _require_item_id,
    _work_ledger_root,
    _write_record,
    conductor_dir,
    item_lock,
    read_work_item,
)
from kiro_crew.work_vocab import (
    MAX_RETIRED_BINDINGS,
    WORK_MAX_INSTANCE_ID_CHARS,
    WORK_MAX_PEER_SLOT_CHARS,
    WORK_REASON_STATUSES,
    coerce_retired_bindings,
)

#: The root directory of the remote state records; one of the store's reserved dirs.
REMOTE_DIR = _REMOTE_DIR

#: Longest crew id kept, the cap the instance registry puts on an id.
MAX_INSTANCE_ID_CHARS = WORK_MAX_INSTANCE_ID_CHARS

#: Longest peer slot key kept, the cap ``bind`` puts on a worker key.
MAX_PEER_SLOT_CHARS = WORK_MAX_PEER_SLOT_CHARS

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
    #: A digest of the peer snapshot whose filing last failed to reach the crew log,
    #: and when. A pull retries that same snapshot only after
    #: ``UNRECORDED_RETRY_SECS``, so a log that keeps refusing costs one audited
    #: attempt per window, not one per pull; a changed snapshot is filed at once.
    unrecorded_key: str | None = None
    unrecorded_at: str | None = None
    #: Whether the last pull read the crew's slot list. ``False`` means ``running``
    #: and ``closed`` are carried over from an older read.
    roster_read: bool = False
    #: The CURRENT binding's newest filed peer stamp (wave 8): a peer snapshot no
    #: newer is not news. Each crew stamps with its own clock, so a retired
    #: worker's stamp must not gate its replacement. ``report_floor_known`` False
    #: (a record from before wave 8) falls back to the item's ``last_report_at``.
    report_floor: str | None = None
    report_floor_known: bool = False
    #: Which binding this record describes, ``"<crew>/<slot>"`` (wave 8), or
    #: ``None`` for a fresh record or one from before wave 8. A record naming
    #: another pair than the item's is stale: nothing is ingested on it and no
    #: replacement may bind until a fresh one is stored.
    binding: str | None = None

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
            "unrecorded_key": self.unrecorded_key,
            "unrecorded_at": self.unrecorded_at,
            "roster_read": self.roster_read,
            "report_floor": self.report_floor,
            "report_floor_known": self.report_floor_known,
            "binding": self.binding,
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
            unrecorded_key=_text("unrecorded_key"),
            unrecorded_at=_text("unrecorded_at"),
            roster_read=_flag("roster_read"),
            report_floor=_text("report_floor"),
            report_floor_known=_flag("report_floor_known"),
            binding=_binding(raw.get("binding")),
        )


def _binding(value: Any) -> str | None:
    """A stored binding identity, at the pair's own caps, or ``None``."""
    cap = MAX_INSTANCE_ID_CHARS + 1 + MAX_PEER_SLOT_CHARS
    return value if isinstance(value, str) and 0 < len(value) <= cap else None


def binding_id(item: WorkItem) -> str | None:
    """*item*'s current remote pair as a :attr:`RemoteState.binding`, or ``None``."""
    if not item.worker_instance_id or not item.worker_session_key:
        return None
    return f"{item.worker_instance_id}/{item.worker_session_key}"


def read_remote_state(board: str, item_id: str) -> RemoteState | None:
    """The stored record, or ``None`` when there is none or it is unreadable."""
    try:
        raw = _read_json_record(remote_state_path(board, item_id))
    except (WorkLedgerError, OSError):
        return None
    return None if raw is None else RemoteState.from_dict(raw)


def write_remote_state(board: str, item_id: str, state: RemoteState) -> None:
    path = remote_state_path(board, item_id)
    from kiro_crew.owner_only_files import mkdirs_owner_only

    mkdirs_owner_only(path.parent)
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
    held: Any = None,
    remote: tuple[bool, bool] | None = None,
) -> tuple[bool, bool, bool, bool]:
    """``(running, closed, looping, held)`` for *item*'s worker, the one answer every reader uses.

    A remote item answers from its stored record (:func:`remote_liveness`) and never
    from this gateway's slot table, where the peer's key names nothing; it holds no
    local loop. A local item asks the resolvers with its worker key. The read
    rows, the board and the wake probe all call this, so they cannot disagree.

    *remote* is that stored answer already read (off the event loop, by a route);
    omitted, a remote item's record is read here, which blocks.
    """
    if item.worker_instance_id and remote is None:
        remote = remote_liveness(board, item)
    if item.worker_instance_id and remote is not None:
        return remote[0], remote[1], False, False
    key = item.worker_session_key or ""
    return (
        bool(running(key)),
        bool(closed(key)),
        bool(looping(key)) if looping else False,
        bool(held(key)) if held else False,
    )


def remote_liveness_map(board: str, items: Any) -> dict[str, tuple[bool, bool]]:
    """``{item_id: (running, closed)}`` for every remote item in *items*. BLOCKING."""
    out: dict[str, tuple[bool, bool]] = {}
    for item in items:
        answer = remote_liveness(board, item)
        if answer is not None:
            out[item.item_id] = answer
    return out


def _open_remote_items(board_dir: Path) -> list[WorkItem]:
    """The open items in *board_dir* bound to a worker on a crew. Never raises."""
    try:
        entries = sorted((board_dir / _ITEMS_DIR).glob("it_*.json"))
    except OSError:
        return []
    out: list[WorkItem] = []
    for entry in entries:
        try:
            raw = _read_json_record(entry)
        except Exception:  # noqa: BLE001 - one torn file must not hide the rest
            raw = None
        if not isinstance(raw, dict):
            continue
        item = WorkItem.from_dict(raw)
        if item.worker_instance_id and item.worker_session_key and not item.is_terminal:
            out.append(item)
    return out


def _has_pending_close(board_dir: Path) -> bool:
    """Whether *board_dir* holds an item with a ``retired`` binding. Never raises."""
    try:
        entries = sorted((board_dir / _ITEMS_DIR).glob("it_*.json"))
    except OSError:
        return False
    for entry in entries:
        try:
            raw = _read_json_record(entry)
        except Exception:  # noqa: BLE001 - one torn file must not hide the rest
            raw = None
        if isinstance(raw, dict) and any(
            b["state"] == "retired" for b in WorkItem.from_dict(raw).retired_bindings
        ):
            return True
    return False


def _board_dirs() -> list[Path]:
    try:
        return [d for d in _work_ledger_root().iterdir() if d.name not in RESERVED_ROOT_DIRS]
    except OSError:
        return []


def open_remote_bindings() -> dict[str, int]:
    """``{crew instance id: open remote bindings}`` across every ledger here. BLOCKING.

    The hub's reservations (RFC §2b, wave 5): each open item bound to a worker on
    a crew reserves one estimate there, a ``done`` worker the conductor keeps open
    included. Derived from the item files, never stored. An unreadable file or
    directory counts nothing: the peer gate, not this count, admits a create.
    """
    counts: dict[str, int] = {}
    for board in _board_dirs():
        for item in _open_remote_items(board):
            crew = str(item.worker_instance_id)
            counts[crew] = counts.get(crew, 0) + 1
    return counts


def boards_with_open_remote() -> list[str]:
    """The conductor keys of every ledger here the hub's background puller walks. BLOCKING.

    A ledger qualifies with an open crew-bound item, or with a retired binding
    whose peer slot is not yet closed (wave 8), so a reconnect closes it with
    nobody watching. Such an entry reserves nothing (:func:`open_remote_bindings`). The key comes from the board's own
    header, and a header whose key does not name the directory it sits in is
    skipped, so a pull never runs against a board it cannot address. Never raises.
    """
    keys: list[str] = []
    for board in _board_dirs():
        if not _open_remote_items(board) and not _has_pending_close(board):
            continue
        try:
            raw = _read_json_record(board / _CONDUCTOR_FILE)
        except Exception:  # noqa: BLE001 - one torn header must not hide the rest
            raw = None
        key = raw.get("slot_key") if isinstance(raw, dict) else None
        if not isinstance(key, str) or not key:
            continue
        # By name, not by path: ``conductor_dir`` resolves symlinks and the listed
        # directory does not, so a data home under a symlinked $HOME would match none.
        if _store_name(key) != board.name:
            continue
        try:
            conductor_dir(key)
        except WorkLedgerError:
            continue
        keys.append(key)
    return sorted(keys)


#: Seconds between two runs of the hub's background puller
#: (:mod:`kiro_crew.dashboard.handlers.remote_puller`). Here, not there, because the
#: work-ledger probe reads it too and must not import the dashboard.
REMOTE_PULL_INTERVAL_SECS = 30.0

#: How old a remote item's last good pull may be before the probe stops trusting
#: the hub's copy of its fields: three puller runs, so one slow or missed run never
#: drops the person-wait hold, and a puller that stopped does within two minutes.
REMOTE_PULL_FRESH_SECS = 3 * REMOTE_PULL_INTERVAL_SECS


def remote_pull_fresh(board: str, item: WorkItem, now: datetime | None = None) -> bool:
    """Whether the hub's copy of remote *item* is recent enough to stand for the peer's.

    True only when the last pull reached the crew connected, read its slot list,
    filed without error, and did so within :data:`REMOTE_PULL_FRESH_SECS`. Anything else, a missing or
    unreadable record included, answers ``False``. Never raises.
    """
    try:
        stored = read_remote_state(board, item.item_id)
    except Exception:  # noqa: BLE001 - a doubt answers not fresh
        return False
    if stored is None or stored.crew_state != "connected" or stored.error is not None:
        return False
    if not stored.roster_read:
        # The slot list was not read, so whether the worker is gone is unknown.
        return False
    moment = parse_stamp(stored.ingested_at) if stored.ingested_at else None
    if moment is None:
        return False
    age = ((now or datetime.now(timezone.utc)) - moment).total_seconds()
    return -REMOTE_PULL_FRESH_SECS < age <= REMOTE_PULL_FRESH_SECS


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


#: The refusal for a retire while every kept binding still waits to be closed.
CODE_RETIRED_HISTORY_FULL = "retired_history_full"

#: Why a binding was retired (rfc §5b).
RETIRE_REASONS: frozenset[str] = frozenset({"crew_unreachable"})


def apply_remote_retire(
    slot_key: str,
    item_id: str,
    *,
    worker_session_key: Any,
    worker_instance_id: Any,
    reason: Any,
) -> dict[str, Any]:
    """Retire an item's current remote binding. Conductor-owned, event kind ``retire``.

    Reached only from the hub's own ingest. Needs the item open and still bound to
    exactly this pair; moves the pair into ``retired_bindings`` and leaves the item
    unbound, so the next ingest reads nothing from that slot and a placement may
    bind it again. Marking an entry ``destroyed`` once its slot is closed belongs to
    the peer half (wave 8b).
    """
    checked_id = _require_item_id(item_id)
    slot = _require_peer_text(worker_session_key, MAX_PEER_SLOT_CHARS, "worker_session_key")
    crew = _require_peer_text(worker_instance_id, MAX_INSTANCE_ID_CHARS, "worker_instance_id")
    checked_reason = _require_choice(reason, RETIRE_REASONS, "reason", CODE_INVALID_VALUE)
    with item_lock(slot_key, checked_id, create=False):
        item = read_work_item(slot_key, checked_id)
        if item is None:
            raise WorkLedgerError(
                f"unknown item {checked_id!r}", code=CODE_UNKNOWN_ITEM, field="item_id"
            )
        if item.is_terminal:
            raise WorkLedgerError(f"item {checked_id!r} is {item.state}", code=CODE_ITEM_CLOSED)
        if item.worker_session_key != slot or item.worker_instance_id != crew:
            raise WorkLedgerError(
                f"item {checked_id!r} is not bound to that crew session",
                code=CODE_INVALID_VALUE,
            )
        history = [dict(b) for b in item.retired_bindings]
        history.append(
            {
                "worker_session_key": slot,
                "worker_instance_id": crew,
                "branch": item.artifacts.get("branch") or None,
                "state": "retired",
                "reason": checked_reason,
                "retired_at": _now_iso(),
                "destroyed_at": None,
            }
        )
        item.retired_bindings = _trim_history(history)
        item.worker_session_key = None
        item.worker_instance_id = None
        event = _commit_item_locked(
            slot_key, item, "retire", f"retired the binding on crew {crew}: {checked_reason}"
        )
    return {"conductor": None, "item": item, "event": event}


def apply_remote_destroy(
    slot_key: str, item_id: str, *, worker_session_key: Any, worker_instance_id: Any
) -> dict[str, Any]:
    """Mark a ``retired`` binding ``destroyed``: its peer slot is closed. Event ``destroy``.

    Conductor-owned, reached only from the hub after the peer confirmed the slot
    closed or gone. Moves no other field, so it is admitted on a closed item too.
    Refuses a pair the history does not hold as ``retired``.
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
        history = [dict(b) for b in item.retired_bindings]
        entry = next(
            (
                b
                for b in history
                if b["state"] == "retired"
                and b["worker_session_key"] == slot
                and b["worker_instance_id"] == crew
            ),
            None,
        )
        if entry is None:
            raise WorkLedgerError(
                f"item {checked_id!r} holds no retired session {slot} on crew {crew}",
                code=CODE_INVALID_VALUE,
            )
        entry["state"], entry["destroyed_at"] = "destroyed", _now_iso()
        item.retired_bindings = coerce_retired_bindings(history)
        event = _commit_item_locked(
            slot_key, item, "destroy", f"closed the retired session on crew {crew}"
        )
    return {"conductor": None, "item": item, "event": event}


def _trim_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """*history* within :data:`MAX_RETIRED_BINDINGS`, oldest destroyed entry first out.

    A ``retired`` entry is never dropped: it is the only record that admits a
    ``close`` of its slot. A history of live entries only is refused
    ``retired_history_full``, so the binding stays current; an entry becomes
    ``destroyed`` (droppable) only once wave 8b closes its slot.
    """
    while len(history) > MAX_RETIRED_BINDINGS:
        gone = next((b for b in history if b["state"] == "destroyed"), None)
        if gone is None:
            raise WorkLedgerError(
                f"{MAX_RETIRED_BINDINGS} retired sessions still wait to be closed",
                code=CODE_RETIRED_HISTORY_FULL,
            )
        history.remove(gone)
    return coerce_retired_bindings(history)


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
