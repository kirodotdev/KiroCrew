"""Mention asks a wake carried, kept on disk until the model CONSUMES the turn.

``wake_crew`` reports success when it schedules a turn, but the turn still waits
for a background-turn permit and for the runner's own preparation, and a cancel, a
permit timeout or a gateway restart can end it before the model reads the prompt.
An ask's comment is already marked read by then, so an ask dropped at scheduling
would be lost for good. Instead the sweep records each carried ask here
(:func:`carry`) BEFORE it wakes the crew. The turn's consumption hook marks it
consumed (:func:`_on_admission`), or returned when the turn ended first.
:func:`review` lists every ask that was returned or that an earlier gateway process
carried; the sweep saves those to its queue and only then removes them here
(:func:`settle`), so no crash between the two loses one.
State lives in ``crew-wake-inflight.json``, beside ``crew-wake-checks.json``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from functools import partial
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write, read_json_or

from . import crew_runtime_session, store

logger = logging.getLogger(__name__)

#: This gateway process. An entry stamped with another one is an ask whose turn
#: died with that process, so it goes back to the queue.
BOOT = uuid.uuid4().hex
_RETURNED = "returned"
_SCHEMA = 1
_MAX_PER_CREW = 50
#: A carried ask this process never heard back about (its queued turn was dropped
#: unseen) goes back to the queue after this long: past the 1800 s permit wait.
LAPSE_SECS = 7200
#: A consumed ask's row is kept this long, so a pending copy that a failed save
#: left behind is dropped instead of answered again (:func:`review`).
DONE_KEEP_SECS = 86400
_DONE = "consumed"
_LOCK = threading.Lock()
#: slot key -> (owner, repo, root, crew id), for the admission hook. In memory: after
#: a restart nothing can be admitted, and the BOOT stamp hands those asks back.
_WHERE: dict[str, tuple[str, str, Path | None, str]] = {}


def _path(owner: str, repo: str, root: Path | None) -> Path:
    return store.repo_data_dir(owner, repo, root) / "crew-wake-inflight.json"


class InflightUnreadable(OSError):
    """The file exists but could not be read: nothing is written over it."""


def _read(owner: str, repo: str, root: Path | None) -> dict[str, dict[str, str]]:
    path = _path(owner, repo, root)
    exists = path.is_file()
    doc = read_json_or(path, None, logger=logger, what="crew wake inflight") if exists else None
    if exists and not isinstance(doc, dict):
        # A transient read failure must not become an empty file on the next write:
        # that would drop every carried ask and consumption record it holds.
        raise InflightUnreadable(f"{path} could not be read; not written")
    if not isinstance(doc, dict) or doc.get("schema") != _SCHEMA:
        return {}
    asks = doc.get("asks")
    if not isinstance(asks, dict):
        return {}
    return {
        str(cid): {str(r): str(v) for r, v in rows.items()}
        for cid, rows in asks.items()
        if isinstance(rows, dict)
    }


def _write(owner: str, repo: str, root: Path | None, asks: dict[str, dict[str, str]]) -> None:
    kept = {cid: rows for cid, rows in asks.items() if rows}
    atomic_write(_path(owner, repo, root), _dumps({"schema": _SCHEMA, "asks": kept}))


def _dumps(doc: dict[str, Any]) -> str:
    return json.dumps(doc, indent=2, sort_keys=True)


def carry(
    owner: str, repo: str, root: Path | None, cid: str, slot_key: str, asks: list[str]
) -> list[str]:
    """Record *asks* as carried by a wake of crew *cid* on *slot_key*; returns the
    ones recorded. At the cap only consumed rows make room, oldest first: a row of a
    turn still outstanding is never evicted, so an ask past the cap is left out and
    stays on the caller's pending queue for a later wake."""
    with _LOCK:
        doc = _read(owner, repo, root)
        rows = doc.setdefault(cid, {})
        fresh = [a for a in dict.fromkeys(asks) if a not in rows]
        spare = sorted((rows[a].partition(" ")[2], a) for a in rows if _done(rows[a]))
        for _at, a in spare[: max(0, len(rows) + len(fresh) - _MAX_PER_CREW)]:
            rows.pop(a, None)
        room = max(0, _MAX_PER_CREW - len(rows))
        took = [a for a in dict.fromkeys(asks) if a in rows] + fresh[:room]
        if took:
            rows.update({a: f"{BOOT} {int(time.time())}" for a in took})
            _write(owner, repo, root, doc)
    _WHERE[slot_key] = (owner, repo, root, cid)
    return [a for a in asks if a in took]


def uncarry(owner: str, repo: str, root: Path | None, cid: str, asks: list[str]) -> None:
    """Forget *asks*: the wake that would have carried them started nothing."""
    with _LOCK:
        doc = _read(owner, repo, root)
        rows = doc.get(cid, {})
        for a in asks:
            rows.pop(a, None)
        _write(owner, repo, root, doc)


def review(owner: str, repo: str, root: Path | None, cid: str) -> tuple[list[str], set[str]]:
    """``(returned, held)`` for crew *cid*, read only. *returned*: carried asks their
    turn will never answer; the caller saves them to its pending queue, then calls
    :func:`settle`. *held*: asks a turn still carries or already consumed, which
    the queue must not hold too, or they would be carried, and answered, twice."""
    with _LOCK:
        rows = _read(owner, repo, root).get(cid, {})
    back = [a for a, stamp in rows.items() if not _live(stamp) and not _done(stamp)]
    return back, set(rows) - set(back)


def requeue(mentions: dict[str, Any], cid: str, back: list[str], held: set[str]) -> None:
    """Fold :func:`review`'s answer into the sweep's mention state, in place."""
    pending = mentions.get("pending")
    if not back and not (isinstance(pending, dict) and pending.get(cid)):
        return
    pending = mentions.setdefault("pending", {})
    # Returned asks go first: they were asked before anything queued since.
    rest = [r for r in pending.get(cid) or [] if r not in held and r not in back]
    queue = list(dict.fromkeys(back)) + rest
    if queue:
        pending[cid] = queue
    else:
        pending.pop(cid, None)


def settle(owner: str, repo: str, root: Path | None, cid: str, asks: list[str]) -> None:
    """Forget returned *asks*, now saved in the queue, and long-finished turns'
    rows. An ask carried again since :func:`review` is live and kept."""
    with _LOCK:
        doc = _read(owner, repo, root)
        rows = doc.get(cid, {})
        gone = [a for a in asks if a in rows and not _live(rows[a]) and not _done(rows[a])]
        gone += [a for a, stamp in rows.items() if _done(stamp, DONE_KEEP_SECS)]
        if not gone:
            return
        for a in gone:
            rows.pop(a, None)
        _write(owner, repo, root, doc)


def _live(stamp: str) -> bool:
    """Whether a carried ask may still be answered by the turn that carries it."""
    boot, _, at = stamp.partition(" ")
    return boot == BOOT and at.isdigit() and time.time() - int(at) < LAPSE_SECS


def _done(stamp: str, older_than: float = 0) -> bool:
    """Whether *stamp* marks an ask the model consumed (at least *older_than* ago)."""
    tag, _, at = stamp.partition(" ")
    if tag != _DONE:
        return False
    return not older_than or (at.isdigit() and time.time() - int(at) >= older_than)


def _wake_reasons(prompt: str) -> list[str]:
    first = prompt.split("\n", 1)[0]
    if not (first.startswith("[crew wake: ") and first.endswith("]")):
        return []
    return [r for r in first[len("[crew wake: ") : -1].split("; ") if r]


def _resolve(slot_key: str, reasons: list[str], consumed: bool) -> None:
    where = _WHERE.get(slot_key)
    if where is None or not reasons:
        return
    owner, repo, root, cid = where
    with _LOCK:
        doc = _read(owner, repo, root)
        rows = doc.setdefault(cid, {})
        # A consumed row stays consumed: it is kept, not removed, so the queue
        # can be checked against it (:func:`review`). A consumed ask with no row
        # (handed back while a retry of its turn was still on the way) gets one,
        # so the queue's copy is dropped instead of answered again.
        hit = [
            r
            for r in reasons
            if (r in rows and (consumed or not _done(rows[r])))
            or (consumed and r.split(" ")[1:2] == ["asked"])
        ]
        if not hit:
            return
        for r in hit:
            rows[r] = f"{_DONE} {int(time.time())}" if consumed else _RETURNED
        _write(owner, repo, root, doc)


async def _on_admission(slot_key: str, prompt: str, consumed: bool) -> None:
    """The hook: the model consumed the turn that carries *prompt* (or never will)."""
    await asyncio.to_thread(partial(_resolve, slot_key, _wake_reasons(prompt), consumed))


crew_runtime_session._ADMISSION_HOOK = _on_admission
