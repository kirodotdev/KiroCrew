"""How many crew-log records a gateway read decodes, against delta + K.

A read that decodes a unit from its first record costs the whole log, and on the
gateway that is GIL time every other thread waits behind. This module counts every
record :meth:`~kiro_crew.crew_log.store.CrewLog.iter_from` decodes, in the gateway
process only (:func:`mark_gateway_process`; a child process never calls it).

One call is one fold pass (:func:`fold_scope`: a slot fold or a session fold) or, outside
a pass, one read: an ``iter_from`` walk, a ``get``, a ``page`` or a raw prefix walk.
``delta`` is what the read answers: the records at or above the seq asked for, the one
entry ``get`` returns, the entries a page holds. A cold fold, and a walk of its own from
the top of a unit with no resume point, have no position, so their delta is 0; a raw
prefix walk re-reads records already folded, so its delta is 0 too. Over ``delta + K`` the call bumps :func:`snapshot`
and logs one WARNING per call site. :data:`MODE_ENV` picks ``report`` or ``off``; see
``docs/system-specs/modules/crew-log-projection.md`` section 4.1.

Stdlib only, so the gateway's boot path can import it for the mark.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Final

logger = logging.getLogger(__name__)

#: The records one call may decode beyond its delta. 256, the same number as
#: ``checkpoint.MIN_ADVANCE_ENTRIES`` (a test pins the two equal), measured on the busy
#: host (``docs/system-specs/modules/crew-log-projection.md``, "Read budget"):
#:
#: * session savepoints there lag their unit by at most 255 entries (16,372 files),
#:   because one is written every 256. The store already pays up to that many unsaved
#:   entries per cell after a restart, so a read inside K costs what the design accepts.
#: * a healthy resume re-reads 1 record per unit it streams; units per slot is 3 at p99.
#: * records per unit is 194 at p50, so a cold fold of a small slot stays under K and is
#:   not reported; records per slot is 1,168 at p90, so the reported ones are the costly.
K: Final[int] = 256

#: The setting: ``report`` (default) or ``off``. Anything else reads as ``report``, so a
#: typo never switches counting off.
MODE_ENV: Final[str] = "KIROCREW_CREW_LOG_READ_BUDGET"
MODE_REPORT: Final[str] = "report"
MODE_OFF: Final[str] = "off"

#: Most call sites remembered as already warned. Past it the counters still move and
#: no new site is logged. shortcut: a fixed cap; raise it if real sites ever exceed it.
_WARNED_LIMIT: Final[int] = 1024

#: Frames skipped when naming the caller: the read machinery itself.
_INTERNAL_FILES: Final[frozenset[str]] = frozenset(
    {
        "read_budget.py",
        "store_handle.py",
        "projection_slot_memo.py",
        "projection_readers.py",
        "projection_session.py",
        "projection_session_memo.py",
        "checkpoint.py",
        "projection.py",
        "contextlib.py",
    }
)

_gateway = False
_lock = threading.Lock()
_local = threading.local()
_warned: "set[tuple[str, str]]" = set()
_counts: "dict[str, int]" = {"over_budget": 0, "cold_over_budget": 0}
_max_records = 0


class Tally:
    """One call's records: everything decoded, and the part below the seq asked for."""

    __slots__ = ("slot", "fold", "unit", "records", "below", "cold")

    def __init__(self, slot: str, fold: str, unit: str = "") -> None:
        self.slot = slot
        self.fold = fold
        self.unit = unit
        self.records = 0
        self.below = 0
        self.cold = False


def mark_gateway_process() -> None:
    """Count this process's reads from now on. Called once, by the gateway's boot."""
    global _gateway
    _gateway = True


def mode() -> str:
    """The live mode. See :data:`MODE_ENV`."""
    raw = os.environ.get(MODE_ENV, "").strip().lower()
    return MODE_OFF if raw == MODE_OFF else MODE_REPORT


def _counting() -> bool:
    return _gateway and mode() != MODE_OFF


def _stack() -> "list[Tally]":
    stack = getattr(_local, "stack", None)
    if stack is None:
        stack = _local.stack = []
    return stack


@contextmanager
def fold_scope(slot: str, fold: str) -> "Iterator[Tally | None]":
    """One slot fold pass as one call: every ``iter_from`` inside adds to it."""
    if not _counting():
        yield None
        return
    tally = Tally(slot, fold)
    stack = _stack()
    stack.append(tally)
    try:
        yield tally
    finally:
        stack.pop()
        _settle(tally)


def note_origin(origin: str) -> None:
    """Record where the open pass got its state; ``cold`` means it had no position."""
    if origin == "cold" and _counting():
        stack = _stack()
        if stack:
            stack[-1].cold = True


def open_read(unit_id: str, fold: str = "iter_from") -> "tuple[Tally, bool] | None":
    """The tally one read of *unit_id* adds to, and whether it owns it; ``None`` off.

    Inside a :func:`fold_scope` the walk adds to that pass. Outside one it is its own
    call, settled by :func:`close_read`.
    """
    if not _counting():
        return None
    stack = _stack()
    if stack:
        return stack[-1], False
    # The unit goes in the log line only: the warn-once key is the call site, so one
    # site walking many units warns once.
    return Tally("", fold, unit_id), True


def count_read(unit_id: str, fold: str, records: int, delta: int) -> None:
    """Count one finished read of *records* records, *delta* of them its answer.

    For the reads that are not an ``iter_from`` walk (``get``, ``page``, the raw prefix
    walks): inside a :func:`fold_scope` they add to that pass, outside one they are a
    call of their own under *fold*. Never raises.
    """
    opened = open_read(unit_id, fold)
    if opened is None:
        return
    tally = opened[0]
    tally.records += records
    tally.below += max(0, records - delta)
    close_read(opened)


def close_read(opened: "tuple[Tally, bool] | None") -> None:
    """Settle a walk :func:`open_read` gave its own tally. Never raises."""
    if opened is not None and opened[1]:
        _settle(opened[0])


def snapshot() -> "dict[str, Any]":
    """The counters, for the diag recorder's gateway row."""
    with _lock:
        out: "dict[str, Any]" = dict(_counts)
        out["max_records"] = _max_records
    out["mode"] = mode() if _gateway else MODE_OFF
    out["k"] = K
    return out


def reset_for_tests() -> None:
    """Forget the counters, the warned sites and the gateway mark."""
    global _gateway, _max_records
    with _lock:
        _gateway = False
        _max_records = 0
        _warned.clear()
        for key in _counts:
            _counts[key] = 0


def _settle(tally: Tally) -> None:
    """Check one finished call against delta + K; count and warn when over. Never raises."""
    global _max_records
    try:
        delta = 0 if tally.cold else tally.records - tally.below
        if tally.records <= delta + K:
            return
        frames = _caller_frames(3)
        site = frames[0] if frames else "?"
        with _lock:
            _counts["over_budget"] += 1
            if tally.cold:
                _counts["cold_over_budget"] += 1
            _max_records = max(_max_records, tally.records)
            key = (tally.fold, site)
            first = key not in _warned and len(_warned) < _WARNED_LIMIT
            if first:
                _warned.add(key)
        if first:
            logger.warning(
                "crew log read over budget: slot=%s fold=%s unit=%s records=%d delta=%d k=%d "
                "cold=%s caller=%s",
                tally.slot or "-",
                tally.fold,
                tally.unit or "-",
                tally.records,
                delta,
                K,
                tally.cold,
                " < ".join(frames) or "?",
            )
    except Exception:  # pragma: no cover - a diagnostic must never fail a read
        logger.debug("crew log read budget could not settle a call")


def _caller_frames(limit: int) -> "list[str]":
    """The first *limit* frames outside the read machinery, as ``file:line func``."""
    out: "list[str]" = []
    frame = sys._getframe(1)
    while frame is not None and len(out) < limit:
        path = frame.f_code.co_filename
        name = os.path.basename(path)
        if name not in _INTERNAL_FILES:
            short = path.split("kiro_crew" + os.sep, 1)[-1] if "kiro_crew" in path else name
            out.append(f"{short}:{frame.f_lineno} {frame.f_code.co_name}")
        frame = frame.f_back  # type: ignore[assignment]
    return out
