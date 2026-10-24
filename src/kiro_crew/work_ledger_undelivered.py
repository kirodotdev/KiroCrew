"""The work-ledger store's record of a worker-tool call the harness never delivered.

A worker reports only through ``work_report`` (and reads its item through
``work_brief``). When the harness cannot resolve that call -- KAS answers a bare
tool name with ``Tool "work_report" is not available.`` without ever reaching the
server -- nothing lands on the item, and the board shows a worker that went quiet.
The gateway sees the failed call in the worker's own turn
(:mod:`kiro_crew.undelivered_call_watch`) and records it here, and the ledger reads
derive ``report_undelivered`` from it: the conductor and the board say "couldn't
send its report" instead of guessing at a stall.

NOT AN ITEM WRITE. The items are a projection of the crew log, and this is not
something a worker or a conductor said: it is the gateway's observation of the
worker's transport, like the ``stale`` liveness flag. So it is kept beside the
items, one small file per worker under ``work-ledger/undelivered/``
(:func:`~kiro_crew.work_ledger_store.undelivered_path`), never under a conductor's
directory, which a rebuild from the crew log prunes. The whole ``work-ledger`` tree
is withheld from agent file tools, so a model cannot write the record itself.

CLEARED BY THE CALL GETTING THROUGH. Every ``work_brief`` or ``work_report`` that
reaches the ledger deletes its worker's record (:func:`clear_undelivered`, called by
the two route handlers), so the flag lasts exactly as long as the worker has not
got through since. A recorded close of the item deletes it too, and a worker
that never failed never gets a file.

ORDERED BY WHEN EACH OPERATION RAN, NEVER BY A CLOCK. A failure takes a number from
one in-process counter at the moment the watch sees it (:func:`undelivered_seen`),
before the note goes off the event loop. A clear that runs while such a note is
still in flight takes the next number. The note is dropped only when a clear's
number is higher than its own, so a slow note cannot bring back a failure the
worker already recovered from, and a failure seen after a clear always shows.
The numbers are unique, so there are no ties. The counter is per process, which
covers every call: a worker's MCP tools post to the gateway that runs its turn,
and a note still in flight when that gateway stops is lost with it.

STRUCTURAL ONLY. The record holds the item it was bound to, which worker tool
failed, and when. Never the harness's error text or the call's arguments: the flag
reaches the conductor's wake and the board, and a worker's words reach those only
through the identity-gated report path.

:mod:`kiro_crew.work_ledger` binds every name defined here, so each one reads,
imports and patches as ``kiro_crew.work_ledger.<name>``.
"""

from __future__ import annotations

import itertools
import threading
from datetime import datetime
from typing import Any

from kiro_crew.session_ledger import ledger_key
from kiro_crew.work_ledger_records import SCHEMA_VERSION, logger
from kiro_crew.work_ledger_store import (
    _ITEM_ID_RE,
    _read_json_record,
    _write_record,
    binding_lock,
    read_work_item,
    undelivered_path,
)
from kiro_crew.work_ledger_writes import read_binding

#: The tools a dispatched worker uses to read its item and report on it. A failed
#: conductor tool (``work_ledger_*``) is not recorded: the conductor is the one who
#: would read the flag, and it already saw its own call fail.
WORKER_TOOLS: frozenset[str] = frozenset({"work_brief", "work_report"})

#: The operation order. ``_pending`` holds, per worker, the numbers of failures seen
#: whose note has not finished; ``_cleared_at`` holds the number of the newest clear
#: that ran while one was pending. Both drop a worker as soon as nothing is pending,
#: so they hold only notes in flight.
_order_lock = threading.Lock()
_order = itertools.count(1)
_pending: dict[str, set[int]] = {}
_cleared_at: dict[str, int] = {}


def undelivered_seen(worker_slot_key: str) -> int:
    """Take a failed call's place in the order, at the moment it is seen.

    Pair every call with :func:`note_undelivered` (``seen=``), which settles it, or
    with :func:`settle_undelivered` when that note will never run.
    """
    key = ledger_key(worker_slot_key)
    with _order_lock:
        seq = next(_order)
        _pending.setdefault(key, set()).add(seq)
    return seq


def settle_undelivered(worker_slot_key: str, seen: int) -> None:
    """Forget that the failure numbered *seen* is in flight. Idempotent."""
    try:
        key = ledger_key(worker_slot_key)
    except Exception:  # noqa: BLE001 - a key undelivered_seen refused has nothing pending
        return
    with _order_lock:
        waiting = _pending.get(key)
        if waiting is None:
            return
        waiting.discard(seen)
        if not waiting:
            del _pending[key]
            _cleared_at.pop(key, None)


def _undelivered_stamp() -> str:
    """Microsecond precision, so two failures in one second are two wakes, not one.

    Display and wake dedupe only: ordering never reads it.
    """
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def note_undelivered(
    worker_slot_key: str, tool: str, *, seen: int | None = None
) -> dict[str, Any] | None:
    """Record that *worker_slot_key*'s *tool* call never reached its item.

    Only for a session bound to an OPEN item that names it back, which is what makes
    it a dispatched worker; any other session's failed call records nothing.

    *seen* is the failure's number from :func:`undelivered_seen`, taken by the watch
    when it saw the failed result, before this runs off the event loop; ``None``
    takes one now. The write is dropped when a :func:`clear_undelivered` ran after
    that number was taken: the worker got through since, so the failure is over.
    Settles *seen* whatever happens.

    Returns ``{"conductor_slot_key", "item_id", "tool", "at"}`` for what was
    written, ``None`` when nothing was. Never raises (cancellation aside): this runs
    inside a worker's turn, and a bookkeeping failure must not cost that turn.
    """
    if tool not in WORKER_TOOLS:
        if seen is not None:
            settle_undelivered(worker_slot_key, seen)
        return None
    try:
        if seen is None:
            seen = undelivered_seen(worker_slot_key)
        key = ledger_key(worker_slot_key)
        with binding_lock(key):
            binding = read_binding(key)
            if binding is None:
                return None
            conductor_slot_key, item_id = binding
            item = read_work_item(conductor_slot_key, item_id)
            if item is None or item.is_terminal or item.worker_session_key != key:
                return None
            with _order_lock:
                cleared = _cleared_at.get(key)
            if cleared is not None and cleared > seen:
                return None
            record = {
                "schema": SCHEMA_VERSION,
                "conductor_slot_key": conductor_slot_key,
                "item_id": item_id,
                "tool": tool,
                "at": _undelivered_stamp(),
            }
            _write_record(undelivered_path(key), record)
    except Exception:  # noqa: BLE001 - best effort inside a worker's turn
        logger.warning("could not record an undelivered %s call", tool, exc_info=True)
        return None
    finally:
        if seen is not None:
            settle_undelivered(worker_slot_key, seen)
    return {k: record[k] for k in ("conductor_slot_key", "item_id", "tool", "at")}


def clear_undelivered(worker_slot_key: str) -> None:
    """Delete *worker_slot_key*'s record: one of its worker-tool calls got through.

    Called by the ``work_brief`` and ``work_report`` route handlers once the call has
    reached the ledger, which is the one place that knows it did, whichever surface
    the worker's turn ran on. While a note of an earlier failure is still in flight,
    the clear also takes a number, which fences that note off (see
    :func:`note_undelivered`). With no record and no note in flight, which is every
    session that never failed and every harness but KAS, it writes nothing and
    takes no file lock. Never raises (cancellation aside).
    """
    try:
        key = ledger_key(worker_slot_key)
        path = undelivered_path(key)
        with _order_lock:
            in_flight = key in _pending
        if not in_flight and not path.exists():
            return
        with binding_lock(key):
            path.unlink(missing_ok=True)
            with _order_lock:
                if key in _pending:
                    _cleared_at[key] = next(_order)
    except Exception:  # noqa: BLE001 - best effort beside a delivered call
        logger.debug("could not clear an undelivered-call record", exc_info=True)


def read_undelivered(conductor_slot_key: str, item: Any) -> dict[str, str] | None:
    """``{"tool", "at"}`` while *item*'s worker has an undelivered call, else ``None``.

    A record for another item (the worker was bound again since), another conductor,
    a closed item, or a record that does not read back whole is not this item's.
    Never raises.
    """
    worker = getattr(item, "worker_session_key", None) or ""
    if not worker or getattr(item, "is_terminal", False):
        return None
    try:
        raw = _read_json_record(undelivered_path(worker))
    except Exception:  # noqa: BLE001 - an unreadable record is no record
        return None
    if not isinstance(raw, dict):
        return None
    tool, at, item_id = raw.get("tool"), raw.get("at"), raw.get("item_id")
    if (
        not isinstance(tool, str)
        or tool not in WORKER_TOOLS
        or not isinstance(at, str)
        or not isinstance(item_id, str)
        or not _ITEM_ID_RE.match(item_id)
        or item_id != getattr(item, "item_id", None)
        or raw.get("conductor_slot_key") != conductor_slot_key
    ):
        return None
    return {"tool": tool, "at": at}
