"""Watch list file operations for Mochi — the persistence half of the watcher.

Ported from ``src/main/watchlistFile.ts`` (+ the constants it pulled from
``src/shared/watchlistTypes.ts``). Pure functions for reading, writing and
updating ``mochi-watchlist.json`` and ``mochi-watchlist-archive.json``.

This is a MECHANICAL port. The six primitives the migration plan calls out
(due-time recomputation after every check, priority dispatch, the timestamp
lock, fail-degradation with cooldown, at-least-once delivery, lifecycle and
archiving) live partly here and partly in ``watchlistService``; nothing about
them is redesigned in passing. Upgrading this into a core scheduler is
KiroCrew issue #721's business, with THIS file as the baseline.

PORTING NOTES
-------------

**Every wall-clock read is a ``now_ms`` parameter** (the ``queue_file``
discipline). That includes ``compress_history``, which read
``Date.now()`` internally, and the two readers, which need the clock only to
name a corruption backup.

**Item ids embed the clock and four random base36 chars** —
``w-{Date.now()}-{Math.random().toString(36).slice(2, 6)}``. The random tail is
generated with Python's ``random``; it is identity, not behaviour. (The JS
suffix can be SHORTER than four chars — ``toString(36)`` strips trailing
zeros — so the contract is ``w-<epoch-ms>-<1..4 base36 chars>``, and the
differential harness canonicalises the tail on both sides rather than trying
to reproduce V8's float printing.)

**JS ``??`` semantics, key-presence semantics.** ``??`` treats ``null`` and
``undefined`` alike, so a JSON ``null`` for ``checkIntervalMins`` /
``priority`` / ``notifyOnChange`` / ``autoComplete`` / ``maxWatchDurationHours``
falls back to the default — but ``patch.field !== undefined`` guards treat
``null`` as a VALUE and assign it. In dict terms: for creation params,
``None``-or-absent means default; for update patches, absent means "leave
alone" and ``None`` means "assign null". Both preserved.

**``DEFAULT_MAX_FAIL_COUNT[kind]`` for an unknown kind is ``undefined``**, and
the key vanishes from the persisted JSON. The port sets the key only when the
kind is known.

**``Math.round`` is half-toward-positive-infinity**, not banker's rounding —
``_js_round`` exists because ``round(2.5)`` is 2 in Python and 3 in JS. And
``MAX_CHECK_INTERVAL_MINS`` is ``Infinity`` (the UI grew a unit selector and
the upper clamp was retired), so the clamp is effectively a floor at 3.

**NaN comparison semantics** (see queue_file): an unparseable timestamp makes
every comparison false. Consequences preserved explicitly: bad ``checkedAt``
entries are DISCARDED by compression; bad ``completedAt`` items never archive;
bad ``archivedAt`` items are PRUNED by the merge; bad ``nextCheckAfter`` /
``triggerAt`` / ``createdAt`` items are never due, never overdue, never
expired. In ``search_archive`` an unparseable (or epoch-zero) ``since``
evaluates falsy and DISABLES the since-filter entirely; a NaN sort comparator
acts as "equal", which a stable sort turns into insertion order — mirrored
with ``cmp_to_key``.

**Terminal-to-terminal transitions keep the original completion metadata**
(``done`` → ``cancelled`` changes the status but not ``completedAt`` /
``completionReason``), and a falsy ``completedAt`` (empty string) counts as
unset for the auto-set. Both ship today.
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import string
from contextlib import contextmanager
from functools import cmp_to_key
from pathlib import Path
from typing import Any, Iterator

from kiro_crew.apps.builtins.mochi.queue_file import _epoch_ms, _iso
from kiro_crew.atomic_write import atomic_write
from kiro_crew.platform_compat import file_lock

logger = logging.getLogger(__name__)

WatchList = dict[str, Any]
WatchItem = dict[str, Any]

# ── Constants (from shared/watchlistTypes.ts) ──────────────────────────────

TERMINAL_STATUSES = ("done", "cancelled", "failed", "expired")

DEFAULT_MAX_FAIL_COUNT = {
    "url": 10,
    "reminder": 999,
    "meeting": 999,
    "custom": 5,
    "slack-channel": 10,
    "slack-topic": 10,
}

DEFAULT_CHECK_INTERVAL_MINS = 10
MIN_CHECK_INTERVAL_MINS = 3
# Retired upper clamp (the UI grew a unit selector); kept as +inf so the clamp
# expression stays shaped like the original.
MAX_CHECK_INTERVAL_MINS = math.inf
DEFAULT_MAX_WATCH_DURATION_HOURS = 168  # 7 days
MAX_ACTIVE_ITEMS = 50

# ── Constants (watchlistFile.ts) ───────────────────────────────────────────

# History compression: full records within this window.
HISTORY_FULL_RETENTION_MS = 24 * 60 * 60 * 1000  # 24 hours

# History compression: changed-only records within this window.
HISTORY_CHANGED_RETENTION_MS = 7 * 24 * 60 * 60 * 1000  # 7 days

# History hard cap (safety valve for high-frequency checks).
HISTORY_MAX_ENTRIES = 500

# Archive: max age before auto-delete.
ARCHIVE_MAX_AGE_MS = 90 * 24 * 60 * 60 * 1000  # 90 days

# Archive: max items (estimated ~500 bytes/item ≈ 1MB).
ARCHIVE_MAX_ITEMS = 2000

# Archive: items stay in the active file this long after completion.
ARCHIVE_AFTER_MS = 24 * 60 * 60 * 1000  # 24 hours

# Reader size gate: reject active files larger than this.
_MAX_WATCHLIST_BYTES = 10 * 1024 * 1024

_BASE36 = string.digits + string.ascii_lowercase


def _empty_watchlist() -> WatchList:
    return {"version": 1, "items": []}


def _empty_archive() -> WatchList:
    return {"version": 1, "items": []}


def _js_round(value: float) -> float:
    """``Math.round``: half rounds toward +inf (``round(-2.5)`` is ``-2``)."""
    if math.isinf(value):
        return value
    return math.floor(value + 0.5)


def _new_id(now_ms: int) -> str:
    """``w-{Date.now()}-{Math.random().toString(36).slice(2, 6)}``.

    Always four suffix chars here; JS may emit fewer (trailing-zero stripping in
    float printing). The contract is the shape, not the length — see module
    docstring.
    """
    suffix = "".join(random.choices(_BASE36, k=4))
    return f"w-{now_ms}-{suffix}"


# ── I/O ────────────────────────────────────────────────────────────────────


@contextmanager
def watchlist_mutation(file_path: str | Path) -> Iterator[None]:
    """Cross-process lock for watchlist read-modify-write sequences.

    Same defect and same remedy as ``queue_file.queue_mutation``: ``write_atomic``
    makes each WRITE atomic, but a read-modify-write is not, and this file has THREE
    independent writers — the MCP server process (the agent adding or patching an
    item), the gateway's watchlist service (archiving, status changes, interval
    backoff) and the dashboard routes. Any two interleaving between one side's read
    and its write means the later replace silently drops the other side's update: an
    item the agent just added disappears when the archive sweep writes its own stale
    snapshot back.

    The lock is a sibling ``.lock`` file, so the lock fd is never the file being
    atomically replaced (flock follows the inode; locking the data file would guard
    a vanishing inode after the first rename).

    Plain reads stay lock-free on purpose: the atomic replace already guarantees a
    reader sees a complete document, and the read paths include the poller's
    per-tick check where added latency is felt.
    """
    target = Path(file_path)
    lock_path = target.with_name(target.name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with file_lock(fd, exclusive=True):
            yield
    finally:
        os.close(fd)


def read_watchlist(file_path: str, *, now_ms: int) -> WatchList:
    """Read the active list. Empty list on missing/corrupt/oversized file —
    never None. ``now_ms`` names the corruption backup."""
    try:
        # Size guard: reject files > 10MB (corrupted/bloated).
        size = os.stat(file_path).st_size
    except FileNotFoundError:
        return _empty_watchlist()
    except OSError as err:
        logger.warning("[read_watchlist] Read error: %s", err)
        return _empty_watchlist()

    if size > _MAX_WATCHLIST_BYTES:
        logger.warning("[read_watchlist] File too large (%d bytes), resetting", size)
        _backup_corrupted(file_path, now_ms)
        return _empty_watchlist()

    try:
        raw = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError as err:
        logger.warning("[read_watchlist] Read error: %s", err)
        return _empty_watchlist()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("[read_watchlist] Corrupted JSON in %s, backing up", file_path)
        _backup_corrupted(file_path, now_ms)
        return _empty_watchlist()

    if not isinstance(parsed, dict) or not isinstance(parsed.get("items"), list):
        logger.warning("[read_watchlist] Invalid shape in %s, resetting", file_path)
        _backup_corrupted(file_path, now_ms)
        return _empty_watchlist()

    return parsed


def read_archive(file_path: str, *, now_ms: int) -> WatchList:
    """Read the archive. Empty archive on missing/corrupt file. No size gate —
    the original only guarded the active file."""
    try:
        raw = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return _empty_archive()
    except OSError:
        return _empty_archive()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        _backup_corrupted(file_path, now_ms)
        return _empty_archive()

    if not isinstance(parsed, dict) or not isinstance(parsed.get("items"), list):
        _backup_corrupted(file_path, now_ms)
        return _empty_archive()

    return parsed


def write_atomic(file_path: str, data: WatchList) -> None:
    """Atomic write (tmp + rename, 0600). Raises on failure — unlike
    pinned_files' persist, the original did not swallow errors here."""
    atomic_write(file_path, json.dumps(data, indent=2), mode=0o600)


def _backup_corrupted(file_path: str, now_ms: int) -> None:
    bak_path = f"{file_path}.bak.{now_ms}"
    try:
        os.rename(file_path, bak_path)
        logger.warning("[watchlist_file] Backed up corrupted file to %s", bak_path)
    except OSError:
        pass  # file may not exist


# ── Item creation ──────────────────────────────────────────────────────────


def create_watch_item(params: dict[str, Any], source: str = "chat", *, now_ms: int) -> WatchItem:
    """Create a new WatchItem from add params with defaults applied."""
    kind = params.get("kind")
    interval = _clamp_interval(
        params["checkIntervalMins"]
        if params.get("checkIntervalMins") is not None
        else DEFAULT_CHECK_INTERVAL_MINS
    )
    now_iso = _iso(now_ms)

    if kind in ("reminder", "meeting"):
        next_check = params["triggerAt"] if params.get("triggerAt") is not None else now_iso
    else:
        next_check = _iso(now_ms + int(interval * 60_000))

    item: WatchItem = {
        "id": _new_id(now_ms),
        "status": "watching",
        "priority": (params["priority"] if params.get("priority") is not None else "normal"),
        "createdAt": now_iso,
        "nextCheckAfter": next_check,
        "checkCount": 0,
        "failCount": 0,
        "maxWatchDurationHours": (
            params["maxWatchDurationHours"]
            if params.get("maxWatchDurationHours") is not None
            else DEFAULT_MAX_WATCH_DURATION_HOURS
        ),
        "checkIntervalMins": interval,
        # The cadence the user asked for, remembered so adaptive backoff has a
        # floor to come back to. Without it the "something changed → re-check
        # sooner" rule has no reference point and collapses to a hardcoded few
        # minutes, which silently overrides an explicit "check this once a day"
        # the moment the item first changes.
        "baseIntervalMins": interval,
        "notifyOnChange": (
            params["notifyOnChange"] if params.get("notifyOnChange") is not None else True
        ),
        "autoComplete": (
            params["autoComplete"] if params.get("autoComplete") is not None else True
        ),
        "source": source,
        "history": [],
    }

    # Direct-assignment fields (`label: params.label` in JS): present-in-params
    # copies through even as null; absent stays absent (undefined drops from
    # the wire). Same for the optional trio below.
    for key in ("label", "kind", "target", "notes", "triggerCondition", "triggerAt"):
        if key in params:
            item[key] = params[key]

    # An unknown kind has no default: JS produced `undefined` and the key
    # vanished from the JSON. Set it only when known.
    if kind in DEFAULT_MAX_FAIL_COUNT:
        item["maxFailCount"] = DEFAULT_MAX_FAIL_COUNT[kind]

    return item


def _clamp_interval(mins: float) -> float:
    return max(MIN_CHECK_INTERVAL_MINS, min(MAX_CHECK_INTERVAL_MINS, _js_round(mins)))


# ── Apply updates ──────────────────────────────────────────────────────────


#: Operations carrying item objects, vs operations carrying bare item ids.
_ITEM_OPS = ("add", "update")
_ID_OPS = ("cancel", "remove")


def _reject_malformed_ops(params: dict[str, Any]) -> None:
    """Raise ``ValueError`` unless every present operation has the right shape.

    Nothing upstream type-checks these. The HTTP route forwards a decoded JSON
    body and the MCP tool forwards agent-authored arguments; both only assert
    that at least one operation KEY is present. A wrong TYPE therefore used to
    reach the per-item code, where it read as a server fault rather than a bad
    request:

    - ``{"add": "x"}`` sliced the STRING, then called ``create_watch_item`` on
      each character -> ``AttributeError`` -> HTTP 500.
    - ``{"add": [{"checkIntervalMins": "abc"}]}`` -> ``TypeError`` in
      ``_clamp_interval`` -> HTTP 500.
    - ``{"remove": "abc"}`` built a set of CHARACTERS and deleted every item
      whose id was one of them: silent data loss with no error at all.

    ``ValueError`` is what both callers already treat as the client's mistake
    (the route maps it to 400), so validating here — rather than in the route —
    covers the MCP path too, which is the likelier source of a wrong shape.
    """
    for key in (*_ITEM_OPS, *_ID_OPS):
        value = params.get(key)
        if value is None:
            continue  # absent, or explicitly null: nothing to apply
        if not isinstance(value, list):
            raise ValueError(f"{key!r} must be a list")
        if key in _ID_OPS:
            if not all(isinstance(entry, str) for entry in value):
                raise ValueError(f"every {key!r} entry must be an id string")
            continue
        for entry in value:
            if not isinstance(entry, dict):
                raise ValueError(f"every {key!r} entry must be an object")
            # Only the interval is arithmetic (`_clamp_interval` -> `_js_round`),
            # so it is the one field whose type turns a bad request into a 500.
            # `bool` is excluded deliberately: it passes `isinstance(_, int)`.
            mins = entry.get("checkIntervalMins")
            if mins is None:
                continue
            if isinstance(mins, bool) or not isinstance(mins, (int, float)):
                raise ValueError(f"{key!r} checkIntervalMins must be a number")


def apply_watchlist_update(
    watchlist: WatchList, params: dict[str, Any], *, now_ms: int
) -> dict[str, Any]:
    """Apply add/cancel/remove/update operations. Returns
    ``{"updated": True, "items": [...], "warning": ...?}``.

    Auto-behaviours (all from the original):
    - historyEntry appends and recomputes nextCheckAfter if still watching and
      no explicit override in the same patch
    - status → terminal auto-sets completedAt/completionReason (unless already
      set — falsy check, so an empty string counts as unset)
    - terminal → watching re-watches: NEW id, previousId link, counters reset
    - checkIntervalMins clamped (floor 3, no ceiling)

    Raises ``ValueError`` if any operation has the wrong shape — see
    ``_reject_malformed_ops``. Validation happens BEFORE anything is applied, so
    a bad request cannot half-mutate the list.
    """
    _reject_malformed_ops(params)
    items: list[WatchItem] = list(watchlist.get("items", []))
    warning: str | None = None

    # 1. Add
    add = params.get("add")
    if add:
        active_count = sum(1 for i in items if i.get("status") == "watching")
        addable = MAX_ACTIVE_ITEMS - active_count
        if addable <= 0:
            warning = (
                f"Active items limit reached ({MAX_ACTIVE_ITEMS}). "
                "Remove or complete some items first."
            )
        else:
            to_add = add[:addable]
            if len(to_add) < len(add):
                warning = (
                    f"Only added {len(to_add)} of {len(add)} items "
                    f"(limit: {MAX_ACTIVE_ITEMS} active)."
                )
            for p in to_add:
                items.append(create_watch_item(p, now_ms=now_ms))

    # 2. Cancel
    cancel = params.get("cancel")
    if cancel:
        cancel_set = set(cancel)
        now_iso = _iso(now_ms)
        items = [
            (
                item
                if item.get("id") not in cancel_set or item.get("status") in TERMINAL_STATUSES
                else {
                    **item,
                    "status": "cancelled",
                    "completedAt": now_iso,
                    "completionReason": "cancelled",
                }
            )
            for item in items
        ]

    # 3. Remove — hard delete by id.
    #
    # DISTINCT from cancel: cancel marks an item terminal (it stays visible,
    # then archives after the retention window), while remove drops it outright.
    # The MCP tool schema has always advertised `remove` ("`remove` deletes by
    # id"), so an agent that followed the schema silently did nothing here.
    remove = params.get("remove")
    if remove:
        remove_set = set(remove)
        items = [item for item in items if item.get("id") not in remove_set]

    # 4. Update
    updates = params.get("update")
    if updates:
        for patch in updates:
            idx = next(
                (i for i, it in enumerate(items) if it.get("id") == patch.get("id")),
                -1,
            )
            if idx == -1:
                continue  # silently skip unknown ids

            item = dict(items[idx])
            was_terminal = item.get("status") in TERMINAL_STATUSES

            # Simple field updates: key-present means assign (null included);
            # key-absent means leave alone (JS `!== undefined`).
            for field in (
                "lastResult",
                "lastChecked",
                "checkCount",
                "failCount",
                "priority",
                "notes",
            ):
                if field in patch:
                    item[field] = patch[field]
            if "checkIntervalMins" in patch:
                item["checkIntervalMins"] = _clamp_interval(patch["checkIntervalMins"])

            # Rescheduling. A reminder's / meeting's whole contract is "fire at
            # triggerAt", so a calendar move that cannot be written back leaves
            # the item firing at the old time — with the new time sitting in
            # notes, so the two disagree and nothing errors. ``nextCheckAfter``
            # is derived from triggerAt at creation for these kinds and is kept
            # in step here for the same reason.
            if "triggerAt" in patch:
                item["triggerAt"] = patch["triggerAt"]
                if item.get("kind") in ("reminder", "meeting") and patch["triggerAt"]:
                    item["nextCheckAfter"] = patch["triggerAt"]

            # Status transition
            new_status = patch.get("status")
            if "status" in patch and new_status != item.get("status"):
                new_is_terminal = new_status in TERMINAL_STATUSES

                if was_terminal and new_status == "watching":
                    # Re-watch: new lifecycle under a new id.
                    item["previousId"] = item.get("id")
                    item["id"] = _new_id(now_ms)
                    item["status"] = "watching"
                    item["createdAt"] = _iso(now_ms)
                    item.pop("completedAt", None)
                    item.pop("completionReason", None)
                    item["checkCount"] = 0
                    item["failCount"] = 0
                    item["nextCheckAfter"] = _iso(
                        now_ms + int(item.get("checkIntervalMins", 0) * 60_000)
                    )
                else:
                    item["status"] = new_status
                    # Auto-set completedAt on terminal transition; falsy check,
                    # matching `!item.completedAt`.
                    if new_is_terminal and not item.get("completedAt"):
                        item["completedAt"] = _iso(now_ms)
                        if not item.get("completionReason"):
                            item["completionReason"] = (
                                "completed" if new_status == "done" else new_status
                            )

            # Append history entry
            history_entry = patch.get("historyEntry")
            if history_entry:
                entry: dict[str, Any] = {"checkedAt": _iso(now_ms)}
                # JS assigned .result/.changed unconditionally; when absent
                # they were `undefined` and vanished from the JSON.
                if "result" in history_entry:
                    entry["result"] = history_entry["result"]
                if "changed" in history_entry:
                    entry["changed"] = history_entry["changed"]
                item["history"] = [*item.get("history", []), entry]
                item["lastChecked"] = entry["checkedAt"]
                # `item.lastResult = entry.result`: an absent result ERASES the
                # previous lastResult (assigning undefined deletes on the wire).
                if "result" in entry:
                    item["lastResult"] = entry["result"]
                else:
                    item.pop("lastResult", None)

                # Track the last meaningful change (missed-notification detection).
                if entry.get("changed"):
                    item["lastChangedAt"] = entry["checkedAt"]

                # Auto-compute nextCheckAfter (only if still watching, and only
                # when the same patch does not override it explicitly).
                if item.get("status") == "watching" and "nextCheckAfter" not in patch:
                    item["nextCheckAfter"] = _iso(
                        now_ms + int(item.get("checkIntervalMins", 0) * 60_000)
                    )

                # Compress history if needed.
                if len(item["history"]) % 10 == 0 or len(item["history"]) > 100:
                    item["history"] = compress_history(item["history"], now_ms=now_ms)

            # Mark as notified — atomically with historyEntry/lastChangedAt.
            if patch.get("notified"):
                item["lastNotifiedAt"] = _iso(now_ms)

            # Explicit nextCheckAfter override (null assigns null).
            if "nextCheckAfter" in patch:
                item["nextCheckAfter"] = patch["nextCheckAfter"]

            items[idx] = item

    result: dict[str, Any] = {"updated": True, "items": items}
    if warning is not None:
        result["warning"] = warning
    return result


# ── History compression ────────────────────────────────────────────────────


def compress_history(history: list[dict[str, Any]], *, now_ms: int) -> list[dict[str, Any]]:
    """Compress: last 24h keep all; 24h–7d keep changed-only; older discard;
    hard cap 500 (keep newest). An unparseable ``checkedAt`` is DISCARDED —
    ``NaN <= window`` is false on both branches."""
    result: list[dict[str, Any]] = []

    for entry in history:
        checked_ms = _epoch_ms(entry.get("checkedAt"))
        if checked_ms is None:
            continue  # NaN age fails both windows
        age = now_ms - checked_ms
        if age <= HISTORY_FULL_RETENTION_MS:
            result.append(entry)
        elif age <= HISTORY_CHANGED_RETENTION_MS and entry.get("changed"):
            result.append(entry)
        # older than 7d: discard

    if len(result) > HISTORY_MAX_ENTRIES:
        return result[-HISTORY_MAX_ENTRIES:]
    return result


# ── Archive operations ─────────────────────────────────────────────────────


def partition_for_archive(watchlist: WatchList, *, now_ms: int) -> dict[str, list[WatchItem]]:
    """Split into ``{"toArchive": [...], "remaining": [...]}`` — terminal items
    completed strictly more than 24h ago move; everything else (including items
    with a missing or unparseable ``completedAt``) stays."""
    to_archive: list[WatchItem] = []
    remaining: list[WatchItem] = []

    for item in watchlist.get("items", []):
        is_terminal = item.get("status") in TERMINAL_STATUSES
        completed_ms = _epoch_ms(item.get("completedAt")) if item.get("completedAt") else None
        if is_terminal and completed_ms is not None and now_ms - completed_ms > ARCHIVE_AFTER_MS:
            to_archive.append(item)
        else:
            remaining.append(item)

    return {"toArchive": to_archive, "remaining": remaining}


def merge_into_archive(archive: WatchList, new_items: list[WatchItem], *, now_ms: int) -> WatchList:
    """Merge with id-dedup (crash recovery: archive is written before the
    active file, so a crash duplicates rather than loses), prune entries 90+
    days old (or with unparseable ``archivedAt`` — NaN fails the keep test),
    cap at 2000 keeping the newest by APPEND ORDER, not by timestamp."""
    existing_ids = {i.get("id") for i in archive.get("items", [])}
    archived_iso = _iso(now_ms)

    to_add = [
        {**item, "archivedAt": archived_iso}
        for item in new_items
        if item.get("id") not in existing_ids
    ]

    items = [*archive.get("items", []), *to_add]

    def _keep(item: WatchItem) -> bool:
        archived_ms = _epoch_ms(item.get("archivedAt"))
        if archived_ms is None:
            return False  # NaN < MAX_AGE is false
        return now_ms - archived_ms < ARCHIVE_MAX_AGE_MS

    items = [i for i in items if _keep(i)]

    if len(items) > ARCHIVE_MAX_ITEMS:
        items = items[-ARCHIVE_MAX_ITEMS:]

    return {"version": 1, "items": items}


# ── Query helpers ──────────────────────────────────────────────────────────


def search_archive(
    archive: WatchList,
    query: str,
    since: str | None = None,
    limit: int = 20,
) -> list[WatchItem]:
    """Case-insensitive substring search on labels, newest-archived first.

    Quirks preserved: an unparseable ``since`` (NaN) or epoch-zero ``since``
    is falsy and DISABLES the since-filter; items without ``completedAt`` pass
    it regardless; a NaN in the sort comparator means "equal", which the
    stable sort resolves to insertion order."""
    q = query.lower()
    since_ms = _epoch_ms(since) if since else 0
    if since_ms is None:
        since_ms = 0  # NaN is falsy in the `if (sinceMs && ...)` guard

    results = []
    for item in archive.get("items", []):
        if since_ms and item.get("completedAt"):
            completed_ms = _epoch_ms(item.get("completedAt"))
            # `new Date(bad).getTime() < sinceMs` is false for NaN: keep.
            if completed_ms is not None and completed_ms < since_ms:
                continue
        if q and q not in str(item.get("label", "")).lower():
            continue
        results.append(item)

    def _cmp(a: WatchItem, b: WatchItem) -> int:
        a_ms = _epoch_ms(a.get("archivedAt"))
        b_ms = _epoch_ms(b.get("archivedAt"))
        if a_ms is None or b_ms is None:
            return 0  # a NaN comparator result is treated as 0 (ES2019 sort)
        return b_ms - a_ms  # descending: most recently archived first

    results.sort(key=cmp_to_key(_cmp))
    return results[:limit]


# ── Expiration / due checks ────────────────────────────────────────────────


def find_expired_items(watchlist: WatchList, *, now_ms: int) -> list[WatchItem]:
    """Watching non-reminder items past ``maxWatchDurationHours``."""
    out = []
    for item in watchlist.get("items", []):
        if item.get("status") != "watching" or item.get("kind") == "reminder":
            continue
        created_ms = _epoch_ms(item.get("createdAt"))
        if created_ms is None:
            continue  # NaN age is never past the limit
        max_hours = item.get("maxWatchDurationHours")
        if not isinstance(max_hours, (int, float)):
            continue  # undefined * 3.6e6 is NaN; NaN comparison is false
        if now_ms - created_ms > max_hours * 3_600_000:
            out.append(item)
    return out


def find_due_watch_items(watchlist: WatchList, *, now_ms: int) -> list[WatchItem]:
    """Watching items (not reminder/meeting) with ``nextCheckAfter <= now``."""
    out = []
    for item in watchlist.get("items", []):
        if item.get("status") != "watching":
            continue
        if item.get("kind") in ("reminder", "meeting"):
            continue
        if not item.get("nextCheckAfter"):
            continue  # falsy guard: missing or empty string
        next_ms = _epoch_ms(item.get("nextCheckAfter"))
        if next_ms is None:
            continue  # NaN <= now is false
        if next_ms <= now_ms:
            out.append(item)
    return out


def find_due_reminders(watchlist: WatchList, *, now_ms: int) -> list[WatchItem]:
    """Watching reminders and meetings with ``triggerAt <= now``."""
    out = []
    for item in watchlist.get("items", []):
        if item.get("status") != "watching":
            continue
        if item.get("kind") not in ("reminder", "meeting"):
            continue
        if not item.get("triggerAt"):
            continue
        trigger_ms = _epoch_ms(item.get("triggerAt"))
        if trigger_ms is None:
            continue
        if trigger_ms <= now_ms:
            out.append(item)
    return out


def find_overdue_items(watchlist: WatchList, *, now_ms: int) -> list[WatchItem]:
    """Watching non-reminder items that missed two or more check cycles."""
    out = []
    for item in watchlist.get("items", []):
        if item.get("status") != "watching" or item.get("kind") == "reminder":
            continue
        if not item.get("nextCheckAfter"):
            continue
        next_ms = _epoch_ms(item.get("nextCheckAfter"))
        if next_ms is None:
            continue  # NaN overdue is never > threshold
        interval = item.get("checkIntervalMins")
        if not isinstance(interval, (int, float)):
            continue  # undefined * n is NaN; NaN comparison is false
        overdue_ms = now_ms - next_ms
        if overdue_ms > interval * 2 * 60_000:
            out.append(item)
    return out
