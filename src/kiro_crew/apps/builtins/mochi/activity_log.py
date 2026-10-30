"""Mochi activity log — today's notifications and events.

Port of the original ``main/activityLog.ts``. Two files live side by side in the
data dir: ``mochi-activity.json`` (today) and ``mochi-activity-yesterday.json``
(the previous day, archived on the first write after a date change). Both names
are load-bearing — the ``mochi-plan`` skill reads them through the
``read_mochi_file`` MCP tool to avoid repeating itself, and the dashboard page
merges them into its "Recent Activity" card.

The original's date key is ``new Date().toISOString().slice(0, 10)`` — a **UTC**
day, not a local one. Kept as-is: the rollover boundary is only a bucketing
detail (readers merge today + yesterday and sort by timestamp), and changing it
would silently desynchronise this file from what the skill was written against.

DEVIATION — entry cap. The original grew this file without bound and relied on
its Electron main process calling ``trimIfNeeded(max)`` on a timer to hand the
overflow to an LLM for summarisation. That caller is not part of the builtin, so
an uncapped file here would grow forever. ``_MAX_ENTRIES`` therefore drops the
oldest entries on write. The other original helpers that only served that
summarisation loop (``getStaleEntries`` / ``trimIfNeeded`` / ``getActivityCounts``)
are intentionally not ported.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, TypedDict

from kiro_crew.platform_compat import chmod_safe, file_lock

logger = logging.getLogger(__name__)

LOG_FILE = "mochi-activity.json"
YESTERDAY_LOG_FILE = "mochi-activity-yesterday.json"

#: Hard cap on retained entries per day file. See the DEVIATION note above.
_MAX_ENTRIES = 500

#: Higher ceiling used only while a rollover archive keeps failing. The normal
#: cap would evict unarchived prior-day entries one per failed write (silent data
#: loss), so the failed-archive path holds entries up to THIS bound instead —
#: finite, so a persistent archive failure cannot grow the file without limit
#: (``a-bound-bounds-every-field-it-retains``). Larger than ``_MAX_ENTRIES`` so an
#: ordinary transient failure window never reaches it; once an archive lands the
#: normal cap applies again. At this extreme the oldest entries are dropped: the
#: failure has long outlasted any transient, and bounding the file wins.
_MAX_ENTRIES_UNARCHIVED = _MAX_ENTRIES * 4


class ActivityEntry(TypedDict):
    ts: str
    type: str
    content: str


class ActivityStore(TypedDict):
    date: str
    entries: list[ActivityEntry]


def _today() -> str:
    """UTC calendar day, matching the original's ``toISOString().slice(0, 10)``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _read_store(path: Path) -> ActivityStore:
    """Load a day file, degrading to an empty store on any read/parse failure.

    A corrupt file must not take the activity log (or the dashboard reading it)
    down — the original swallowed these too, and the content is disposable.
    """
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"date": _today(), "entries": []}
    if not isinstance(raw, dict):
        return {"date": _today(), "entries": []}
    entries_raw = raw.get("entries")
    entries: list[ActivityEntry] = []
    if isinstance(entries_raw, list):
        for item in entries_raw:
            if isinstance(item, dict):
                entries.append(
                    {
                        "ts": str(item.get("ts") or ""),
                        "type": str(item.get("type") or ""),
                        "content": str(item.get("content") or ""),
                    }
                )
    return {"date": str(raw.get("date") or _today()), "entries": entries}


def _write_store(path: Path, store: ActivityStore) -> bool:
    """Atomically replace a day file, returning whether the write landed.

    Same temp-then-rename shape as ``queue_file.write_queue_atomic``: a reader
    (the dashboard polls this) must never observe a half-written file.

    0600 like every other Mochi state file: this is a behaviour record — what the
    pet did and when — and it was landing at the umask default because it writes
    its own temp file instead of using ``atomic_write`` (whose mkstemp is already
    owner-only).

    Returns ``True`` on success and ``False`` when the write failed (swallowed so
    a reader-facing write never crashes the caller). The rollover branch of
    ``log_activity`` relies on this signal to avoid destroying the previous day
    when its archive write did not land.
    """
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
        chmod_safe(tmp, 0o600)
        os.replace(tmp, path)
        return True
    except OSError:
        logger.debug("[mochi] activity log write failed", exc_info=True)
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


@contextmanager
def activity_mutation(data_dir: str | Path) -> Iterator[None]:
    """Cross-process lock for an activity-log read-modify-write.

    The append log is written from several paths (some offloaded to worker
    threads) and cleared by reset, so every RMW — and the reset unlink — must
    serialize on the same sibling ``.lock`` file. ``YESTERDAY_LOG_FILE`` is
    written while holding this same lock, so callers touching either file take
    this one lock.
    """
    base = Path(data_dir)
    base.mkdir(parents=True, exist_ok=True)
    lock_path = base / (LOG_FILE + ".lock")
    lock_path.touch(exist_ok=True)
    with open(lock_path, "r+") as _lf:
        with file_lock(_lf.fileno(), exclusive=True):
            yield


def log_activity(data_dir: str | Path, kind: str, content: str) -> None:
    """Append one entry to today's log, archiving yesterday's on a date change.

    ``content`` is redacted here, at the single write point, rather than at each
    caller. Most entries are agent-authored: ``perform_pet_action``'s summary flows
    straight through ``notify_user`` into this file, and the file is served back out
    over ``/api/apps/mochi/activity`` and read by the plan skill — so an AKIA key or
    a webhook URL that appeared in a model's summary would be persisted verbatim and
    then handed to a browser and a later prompt. The gateway already redacts app
    EventBus payloads with the same two helpers; the activity log is the other
    persisted sink on that path and was missing it.

    Redacting at the chokepoint (not at the ~8 call sites) is deliberate: a new
    caller cannot forget it.
    """
    from kiro_crew.security import redact

    content = redact(content)
    base = Path(data_dir)
    path = base / LOG_FILE
    # Lock the whole read-modify-write. This append log is written from several
    # call paths, and the loop-facing ones are now OFFLOADED to threads (see
    # hooks._log_activity), so two writers could otherwise both read the store and
    # the last write would drop the other's entry. The lock serializes them in the
    # worker threads, off the event loop.
    with activity_mutation(base):
        store = _read_store(path)
        today = _today()
        archive_failed = False
        if store["date"] != today:
            # Partition retained entries by UTC day. Normally the whole store is
            # the prior day, but a FAILED archive on an earlier rollover leaves a
            # mixed store — today's entries appended onto a buffer still dated
            # yesterday (see the no-reset path below). Only prior-day entries get
            # archived; today's stay in the current file, so an entry recorded
            # after a failed archive is not archived into yesterday (and expired a
            # rollover early) once a later archive succeeds.
            prior = [e for e in store["entries"] if str(e.get("ts") or "")[:10] != today]
            todays = [e for e in store["entries"] if str(e.get("ts") or "")[:10] == today]
            # Reset only once the archive has actually landed: _write_store
            # swallows OSError, so a failed archive (full disk, read-only home,
            # Windows sharing violation) would otherwise reset the buffer and let
            # the write below overwrite mochi-activity.json — the sole remaining
            # copy of the previous day — with prior-day data lost, silently. When
            # the archive does not land we keep every entry in place under the old
            # date and append today's onto them, so nothing is lost until an
            # archive succeeds on a later write.
            if not prior or _write_store(
                base / YESTERDAY_LOG_FILE, {"date": store["date"], "entries": prior}
            ):
                store = {"date": today, "entries": todays}
            else:
                # Archive failed and the buffer still holds unarchived prior-day
                # entries. Trimming at the normal cap here would silently evict
                # exactly those unarchived entries (the whole day erodes one entry
                # per failed write when the buffer is already at the cap), so the
                # cap below rises to _MAX_ENTRIES_UNARCHIVED for this write. That
                # bound is still finite — a persistent archive failure cannot grow
                # the file without limit — and a later write whose archive lands
                # re-applies the normal cap to what remains.
                archive_failed = True
        store["entries"].append(
            {"ts": datetime.now(timezone.utc).isoformat(), "type": kind, "content": content}
        )
        cap = _MAX_ENTRIES_UNARCHIVED if archive_failed else _MAX_ENTRIES
        if len(store["entries"]) > cap:
            del store["entries"][: len(store["entries"]) - cap]
        _write_store(path, store)


def read_recent(data_dir: str | Path) -> list[ActivityEntry]:
    """Today + yesterday merged, newest first — what the dashboard renders.

    Yesterday is included because the log resets at UTC midnight, which for most
    users lands mid-afternoon; without it the card would empty out during the
    working day.
    """
    base = Path(data_dir)
    merged: list[ActivityEntry] = []
    for name in (LOG_FILE, YESTERDAY_LOG_FILE):
        path = base / name
        if path.exists():
            merged.extend(_read_store(path)["entries"])
    merged.sort(key=lambda e: str(e.get("ts") or ""), reverse=True)
    return merged[:_MAX_ENTRIES]
