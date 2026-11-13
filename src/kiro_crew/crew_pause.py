"""Ultra Mode "Pause all": what a paused crew holds and what it stopped.

Kept next to the work ledger (``<data home>/work-ledger/crew-pause.json``) so a page
reload or a gateway restart still reads it. One record per paused root (``paused``):

* ``tree``: the root's crew when it was paused, every depth. While the record
  stands none of them, nor the root, may open a new session
  (:func:`blocks_create`).
* ``held``: the crew members that start no turn (:func:`holds_turn`): the whole
  tree at first, less each member whose own Continue the person pressed
  (:func:`release`). The root is held for the whole pause. A held session's
  patrol wakes are refused, and its queued and newly sent messages wait.
* ``stopped``: the members whose running turn the pause stopped: what Resume
  all continues.
* ``root_released``: the root itself starts turns again while the members the
  record still names stay held (a Resume that left a turn tearing down).
* ``owed``: the members whose turn the dispatch gate ended because of the pause
  (:func:`owe_turn`). They stay in ``stopped`` whatever their Stop answered.

Beside ``paused`` the file keeps ``resuming``: per root, the stopped members of a
record Resume all dropped whose hand-backs have not finished
(:func:`forget_resuming`). A restart in between ends that Resume, so the load
holds them again (as :func:`keep_pending` does) and the next Resume all
continues them.

The routes in ``dashboard/handlers/crew_pause.py`` write it; the queue drain,
the sends, the autonudge fire and ``session_create`` read it. Reads are in
memory and take no lock: each write publishes a new map, never
editing the published ones, so a reader on the event loop never waits on a
writer's file write. The file is read once per data home (it is small and
capped; the dashboard reads it off-loop at startup with :func:`load`), and every
write goes through :func:`atomic_write`.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home

logger = logging.getLogger(__name__)

#: The largest pause file read back; a bigger one is ignored as damaged.
MAX_FILE_BYTES = 256 * 1024

_lock = threading.Lock()
_records: dict[str, dict[str, Any]] = {}
#: Per root, the stopped members of a record a Resume all dropped, until their
#: hand-backs finish: a turn its dispatch gate ends meanwhile is still continued
#: by that Resume. Stored with the records, so a restart does not lose them.
_resuming: dict[str, list[str]] = {}
_loaded_from: Path | None = None
#: The live records hold an owed turn the pause file refused (:func:`owe_turn`).
#: Every write stores the whole map, so the next one that succeeds stores it.
_unsaved = False


def _path() -> Path:
    return data_home() / "work-ledger" / "crew-pause.json"


def _keys(value: Any) -> list[str]:
    """The session keys a stored field names; anything but a list of keys is none."""
    if not isinstance(value, list):
        return []
    return sorted({k for k in value if isinstance(k, str) and k})


def _clean_paused(raw: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for root, rec in raw.items():
        if not isinstance(root, str) or not root or not isinstance(rec, dict):
            continue
        keys = {field: _keys(rec.get(field)) for field in ("tree", "held", "stopped", "owed")}
        at = rec.get("paused_at")
        out[root] = {
            **keys,
            "paused_at": at if isinstance(at, str) else "",
            "root_released": rec.get("root_released") is True,
        }
    return out


def _clean_resuming(raw: Any) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        return {}
    out = {root: _keys(keys) for root, keys in raw.items() if isinstance(root, str) and root}
    return {root: keys for root, keys in out.items() if keys}


def _fold_resuming(
    records: dict[str, dict[str, Any]], resuming: dict[str, list[str]]
) -> dict[str, dict[str, Any]]:
    """Hold again what an unfinished Resume all still had to continue.

    The restart ended that Resume, so no hand-back is coming: the members stay
    paused until the next Resume all continues them.
    """
    for root, keys in resuming.items():
        _hold_pending(records, root, [k for k in keys if k != root], root_pending=root in keys)
    return records


def _ensure_loaded() -> None:
    """Load the file once per data home. Caller holds ``_lock``."""
    global _loaded_from
    path = _path()
    if _loaded_from == path:
        return
    records: dict[str, dict[str, Any]] = {}
    try:
        if path.is_file() and path.stat().st_size <= MAX_FILE_BYTES:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                records = _fold_resuming(
                    _clean_paused(raw.get("paused")), _clean_resuming(raw.get("resuming"))
                )
    except Exception:  # a damaged file pauses nothing, and is not re-read every turn
        records = {}
        logger.warning("crew pause: could not read %s; no crew is paused", path, exc_info=True)
    # The file's ``resuming`` stays until the next write, which stores the
    # records it was folded into instead.
    _publish(records, {})
    _loaded_from = path


def load() -> None:
    """Read the pause file now. BLOCKING: the gateway calls it off-loop at startup."""
    with _lock:
        _ensure_loaded()


#: Preload tasks in flight, held so the loop does not drop them before they run.
_PRELOAD_TASKS: set[Any] = set()


async def startup_preload(_app: Any = None) -> None:
    """The gateway's startup hook: start :func:`load` in a worker thread and return.

    Nothing is awaited in front of the listener (``no-new-work-on-gateway-boot-path``).
    The read is a small capped file, so it has finished long before a crew is
    asked about. A reader that comes first never reads the file itself: it sees
    no pause yet (:func:`_live`). A route that must know waits with
    :func:`ensure_loaded`.
    """
    import asyncio

    task = asyncio.ensure_future(asyncio.to_thread(load))
    _PRELOAD_TASKS.add(task)
    task.add_done_callback(_preload_done)


def _preload_done(task: Any) -> None:
    """Drop the finished preload and log a failure instead of raising it."""
    _PRELOAD_TASKS.discard(task)
    if task.cancelled():
        return
    if task.exception() is not None:
        logger.warning("crew pause: startup preload failed", exc_info=task.exception())


async def ensure_loaded() -> None:
    """Read the pause file off the loop when it is not read yet; free once it is.

    For a coroutine that must see the stored pause before it acts (Resume all,
    the worker list). The readers below never read the file themselves.
    """
    if _loaded_from != _path():
        import asyncio

        await asyncio.to_thread(load)


def _live() -> dict[str, dict[str, Any]]:
    """The published records, in memory only, without the writer's lock. Never edit them.

    Called on the event loop, so it never touches the file: until the startup
    preload (or a write, or :func:`ensure_loaded`) has read it, no crew counts
    as paused yet.
    """
    if _loaded_from != _path():
        return {}
    return _records


def _commit(
    records: dict[str, dict[str, Any]], resuming: dict[str, list[str]] | None = None
) -> None:
    """Write *records* and *resuming* (the live one when omitted), then publish them.

    Caller holds ``_lock``. Written first and published after: a write that
    fails raises with the live state unchanged, so memory and the file never
    disagree.
    """
    if resuming is None:
        resuming = _resuming
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = {"paused": records}
    if resuming:
        doc["resuming"] = resuming
    body = json.dumps(doc, sort_keys=True, indent=1) + "\n"
    if len(body.encode("utf-8")) > MAX_FILE_BYTES:
        # The loader ignores a bigger file as damaged, so writing one would drop
        # every stored pause at the next restart: refuse it, live state unchanged.
        raise OSError(f"crew pause: the pause file would exceed {MAX_FILE_BYTES} bytes")
    atomic_write(path, body, mode=0o600, restrict_to_owner=True, newline="\n")
    global _unsaved
    _unsaved = False
    _publish(records, resuming)


def _publish(
    records: dict[str, dict[str, Any]], resuming: dict[str, list[str]] | None = None
) -> None:
    """Make *records* (and *resuming*, when given) the live state. Caller holds ``_lock``.

    A new object replaces the old one, which readers may still be reading.
    """
    global _records, _resuming
    _records = records
    if resuming is not None:
        _resuming = resuming


def _copy_records() -> dict[str, dict[str, Any]]:
    return {
        root: {k: list(v) if isinstance(v, list) else v for k, v in rec.items()}
        for root, rec in _records.items()
    }


def record(root: str) -> dict[str, Any] | None:
    """A copy of *root*'s pause record, ``None`` when it is not paused."""
    rec = _live().get(root)
    return {k: list(v) if isinstance(v, list) else v for k, v in rec.items()} if rec else None


def pause(root: str, tree: list[str], stopped: list[str]) -> dict[str, Any]:
    """Record *root* as paused, holding its whole crew. BLOCKING (one small file write).

    A second pause keeps what the first one stopped: Resume all continues both.
    """
    with _lock:
        _ensure_loaded()
        records = _copy_records()
        old = records.get(root) or {}
        records[root] = {
            "tree": sorted(set(tree) | set(old.get("tree") or [])),
            "held": sorted(set(tree) | set(old.get("held") or [])),
            "stopped": sorted(set(stopped) | set(old.get("stopped") or [])),
            "owed": sorted(old.get("owed") or []),
            "paused_at": old.get("paused_at") or datetime.now(timezone.utc).isoformat(),
            "root_released": False,
        }
        _commit(records)
        return dict(records[root])


def settle_stopped(
    root: str, targets: list[str], stopped: list[str], earlier: Iterable[str] = ()
) -> None:
    """Drop from *root*'s stopped set the *targets* of this pause that did not stop. BLOCKING.

    *earlier* is the stopped set from before this pause: a member an earlier Pause
    all stopped is kept even when this pause's stop found its turn already gone,
    so Resume all still continues it. Only obligations this pause added are dropped.
    """
    missed = set(targets) - set(stopped) - set(earlier)
    with _lock:
        _ensure_loaded()
        # A turn the dispatch gate ended meanwhile is owed, whatever its Stop said.
        missed -= set((_records.get(root) or {}).get("owed") or [])
        if root not in _records or not missed & set(_records[root]["stopped"]):
            return
        records = _copy_records()
        records[root]["stopped"] = sorted(set(records[root]["stopped"]) - missed)
        _commit(records)


def owe_turn(slot_key: str) -> bool:
    """Record that a turn the pause's dispatch gate ended is owed to Resume all. BLOCKING.

    The turn's prompt was already in the transcript (a send waiting on a
    compaction, or a path that raced the hold), so Resume all continues it like a
    turn the pause stopped. Answers whether something will continue it: a record
    that owes it, or a Resume all that already dropped the record and has not
    finished handing its stopped members back. ``False`` means the hold was lifted
    with nothing owed, so the gate lets the turn run.
    """
    global _unsaved
    with _lock:
        _ensure_loaded()
        records = _copy_records()
        changed = False
        for root, rec in records.items():
            owes = slot_key in rec["held"] or _is_root_of(slot_key, root, rec)
            if not owes:
                continue
            for field in ("stopped", "owed"):
                if slot_key not in rec.setdefault(field, []):
                    rec[field] = sorted([*rec[field], slot_key])
                    changed = True
        if changed or _unsaved:
            try:
                _commit(records)
            except OSError:
                # The gate ends the turn either way, so a write the disk or the
                # size cap refuses keeps the obligation in memory, marked unsaved:
                # Resume all still continues it, and the next write (any pause
                # write, or this gate's next refusal) retries storing it.
                # shortcut: a restart before a write succeeds forgets it, as main
                # always did; store it elsewhere if that ever shows up.
                logger.warning("crew pause: owed turn of %s kept in memory only", slot_key)
                _unsaved = True
                _publish(records)
        if any(slot_key in rec.get("owed", []) for rec in records.values()):
            return True
        # The record is gone, but the Resume all that dropped it continues this
        # member once its turn has ended: end it rather than run it twice.
        return any(slot_key in keys for keys in _resuming.values())


def forget_resuming(
    keys: list[str],
    *,
    root: str = "",
    pending: list[str] | None = None,
    root_pending: bool = False,
) -> bool:
    """Resume all is done with *keys*: a later gate runs their turns instead of ending them.

    *pending* (and *root* itself when *root_pending*) were not handed back: they
    are held again under *root*, as :func:`keep_pending` does, in the SAME write,
    so at no moment, on disk or in memory, are they owed by nobody. BLOCKING
    (one small file write). Answers whether the file holds the result. A write
    the disk refuses still applies in memory (the gate must stop ending these
    turns), marked unsaved so the next write stores it, and answers False so the
    caller reports it; until then the file still names them as resuming, so a
    restart holds them again rather than losing them.
    """
    global _unsaved
    with _lock:
        _ensure_loaded()
        records = _copy_records()
        if pending or root_pending:
            _hold_pending(records, root, pending or [], root_pending=root_pending)
        drop = set(keys)
        resuming = {r: kept for r, old in _resuming.items() if (kept := sorted(set(old) - drop))}
        if resuming == _resuming and records == _records and not _unsaved:
            return True
        try:
            _commit(records, resuming)
        except OSError:
            logger.warning("crew pause: resume hand-back of %s kept in memory only", keys)
            _unsaved = True
            _publish(records, resuming)
            return False
        return True


def resume(root: str) -> dict[str, Any] | None:
    """Drop *root*'s record and return it, ``None`` when it was not paused. BLOCKING.

    Its stopped members are stored as resuming in the same write, until
    :func:`forget_resuming` says they were handed back.
    """
    with _lock:
        _ensure_loaded()
        records = _copy_records()
        rec = records.pop(root, None)
        if rec is not None:
            resuming = dict(_resuming)
            stopped = set(rec.get("stopped") or [])
            if stopped:
                resuming[root] = sorted(set(resuming.get(root) or []) | stopped)
            _commit(records, resuming)
        return rec


def roots_naming(slot_key: str) -> list[str]:
    """The roots whose record :func:`release` would edit for *slot_key*, sorted."""
    return sorted(
        root
        for root, rec in _live().items()
        if any(slot_key in rec.get(field, []) for field in ("held", "stopped", "owed"))
    )


def release(slot_key: str) -> dict[str, dict[str, Any]]:
    """Stop holding *slot_key* (its own Continue was pressed). BLOCKING when it was held.

    It stays in the crew, so it still opens no new session while the pause stands.
    Returns what it took away, ``{root: {"fields": [...], "dropped": record}}``
    (empty when it held nothing; ``dropped`` is the whole record when this was its
    last obligation and it ended), so a Continue that is then refused can
    :func:`rehold` it.
    """
    with _lock:
        _ensure_loaded()
        records = _copy_records()
        removed: dict[str, dict[str, Any]] = {}
        before = _copy_records()
        for root, rec in list(records.items()):
            for field in ("held", "stopped", "owed"):
                if slot_key in rec.get(field, []):
                    rec[field] = [k for k in rec[field] if k != slot_key]
                    removed.setdefault(root, {"fields": [], "dropped": None})["fields"].append(
                        field
                    )
            if rec.get("root_released") and not rec["held"] and not rec["stopped"]:
                # The root runs again and its last held member was continued: done.
                del records[root]
                if root in removed:
                    removed[root]["dropped"] = before[root]
        if removed:
            _commit(records)
        return removed


def rehold(slot_key: str, removed: dict[str, dict[str, Any]]) -> None:
    """Put back what :func:`release` took from *slot_key*. BLOCKING.

    For a Continue that was refused: the pause still owes that member its
    hand-back. A record the release itself ended comes back whole (a newer
    Pause all for that root keeps its own record, with this member added); a
    record a Resume all dropped meanwhile is not recreated.
    """
    with _lock:
        _ensure_loaded()
        records = _copy_records()
        changed = False
        for root, taken in removed.items():
            rec = records.get(root)
            dropped = taken.get("dropped")
            if rec is None and dropped is not None:
                records[root] = {
                    k: list(v) if isinstance(v, list) else v for k, v in dropped.items()
                }
                changed = True
                continue
            if rec is None:
                continue
            for field in taken.get("fields") or []:
                if slot_key not in rec.setdefault(field, []):
                    rec[field] = sorted([*rec[field], slot_key])
                    changed = True
        if changed:
            _commit(records)


def _is_root_of(key: str, root: str, rec: dict[str, Any]) -> bool:
    return key == root and not rec.get("root_released")


def root_paused(root: str) -> bool:
    """Whether *root* itself is a paused Ultra Mode root (not one released)."""
    rec = _live().get(root)
    return rec is not None and not rec.get("root_released")


def holds_turn(slot_key: str, still_root: Callable[[], bool] | None = None) -> bool:
    """Whether no new turn may start on *slot_key*: a paused root, or a crew member it holds.

    *still_root*, when given, is asked only for a key that is a paused root: a chat
    that is not an Ultra Mode root (a record left behind for it) is not held as one.
    """
    records = _live()
    if any(slot_key in rec["held"] for rec in records.values()):
        return True
    is_root = any(_is_root_of(slot_key, root, rec) for root, rec in records.items())
    return is_root and (still_root is None or still_root())


def holds_slot(slot: Any) -> bool:
    """:func:`holds_turn` for a live slot, which knows whether it is still a root."""
    from kiro_crew import crew_mode

    return holds_turn(slot.key, lambda: crew_mode.slot_is_crew_mode_root(slot))


def blocks_create(caller_key: str, still_root: Callable[[], bool] | None = None) -> bool:
    """Whether *caller_key* (a paused root or a member of its crew) may not open a session.

    *still_root* is asked as in :func:`holds_turn`, only for a key that is a paused root.
    """
    records = _live()
    # A released root's crew blocks only the members still held.
    if any(
        caller_key in rec["held" if rec.get("root_released") else "tree"]
        for rec in records.values()
    ):
        return True
    is_root = any(_is_root_of(caller_key, root, rec) for root, rec in records.items())
    return is_root and (still_root is None or still_root())


def keep_pending(root: str, keys: list[str], *, root_pending: bool) -> None:
    """Keep holding stopped *keys* a Resume could not hand back yet. BLOCKING.

    Their turn was still tearing down: another Resume all continues them. The rest
    of the crew is free.
    """
    with _lock:
        _ensure_loaded()
        records = _copy_records()
        _hold_pending(records, root, keys, root_pending=root_pending)
        _commit(records)


def _hold_pending(
    records: dict[str, dict[str, Any]], root: str, keys: list[str], *, root_pending: bool
) -> None:
    """Hold *keys* (and *root* when *root_pending*) under *root* in *records*, in place."""
    old = records.get(root)
    if old is not None:
        # A newer Pause all landed meanwhile: keep it, and add these to it.
        old["held"] = sorted(set(old["held"]) | set(keys))
        old["tree"] = sorted(set(old["tree"]) | set(keys))
        old["stopped"] = sorted(
            set(old["stopped"]) | set(keys) | ({root} if root_pending else set())
        )
    else:
        records[root] = {
            "tree": sorted(keys),
            "held": sorted(keys),
            "stopped": sorted([*keys, *([root] if root_pending else [])]),
            "owed": [],
            "paused_at": datetime.now(timezone.utc).isoformat(),
            "root_released": not root_pending,
        }
