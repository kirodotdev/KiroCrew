"""The durable notification log: load, sweep, trim, rewrite and persist.

``notifications.jsonl`` under the data home, the redaction a persisted note gets,
the servability filter, and the one-thread IO pool the persists run on (its
global is rebound only here).
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import math
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from kiro_crew.config.loader import config_dir
from kiro_crew.notifications.bus import normalize_note
from kiro_crew.owner_only_files import owner_only_opener
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger("kiro_crew.dashboard.state")


_NOTIFICATIONS_FILE = "notifications.jsonl"


_MAX_PERSISTED_NOTIFICATIONS = 200


# Fraction of the recency cap reserved for UNSERVABLE notification lines -- a line
# ``_servable_note`` rejects, so ``_load_notifications`` can never serve it. Such a
# line is kept rather than destroyed, but in its own window: see
# ``_maybe_trim_notifications`` for why a shared window turns an append into an
# eviction.
#
# A fraction rather than a second full cap. Two full windows put the post-trim file
# exactly AT the trim threshold, so every later append would re-read and re-write the
# whole file; and this window is a sample of recent damage for a human to look at, not
# history the product serves, so it does not need history's budget.
_UNSERVABLE_NOTIFICATION_CAP_DIVISOR = 4


# ── Notification persistence ──


def _redact_note_value(value: Any) -> Any:
    """Recursively redact every string inside a notification note value.

    Notes carry LLM-derived content in nested structures too (e.g. the
    ``actions`` field is a list of dicts whose ``label`` values may be
    model output), so redaction must descend into lists and dicts rather
    than only scanning top-level strings.
    """
    if isinstance(value, str):
        if not value:
            return value
        value, _ = redact_exfiltration_urls(value)
        value, _ = redact_credentials(value)
        return value
    if isinstance(value, list):
        return [_redact_note_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_note_value(item) for key, item in value.items()}
    return value


def _notifications_path() -> Path:
    """Path to the notifications JSONL file."""
    return config_dir() / _NOTIFICATIONS_FILE


def _note_ts_epoch(note: dict[str, Any]) -> float | None:
    """Best-effort epoch seconds for a note's ``ts`` (ISO string or epoch str)."""
    ts = note.get("ts")
    if ts is None:
        return None
    try:
        parsed = float(ts)
        # float() of a numeric STRING beyond float range (e.g. "-1e999")
        # returns inf/-inf without raising — a -inf epoch would make every
        # TTL comparison read "expired" and the sweep would destroy the row,
        # violating the never-destroy-on-ambiguity rule.
        # NaN likewise carries no ordering meaning. Treat both as
        # unparseable (note kept).
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError, OverflowError):
        # OverflowError: float() of a JSON integer beyond float range (e.g.
        # 10**400) raises rather than returning inf — one poison row must
        # not abort the whole sweep.
        pass
    try:
        return datetime.fromisoformat(str(ts)).timestamp()
    except (ValueError, OverflowError, OSError):
        # .timestamp() raises OverflowError/OSError (not just ValueError) for
        # platform-unrepresentable datetimes -- pre-epoch or far-future ISO
        # strings, most acutely on Windows. Treat them as unparseable (note
        # kept) rather than letting the error escape the sweep: at load time
        # that escape would hit _load_notifications' blanket handler, empty
        # the history, and the next mutation would persist the loss.
        return None


def sweep_expired_notifications(log: list[dict[str, Any]], *, now: float | None = None) -> int:
    """Remove expired PASSIVE notes in place (RFC Phase 5 TTL sweeper).

    A note expires when it is passive, carries a positive integer ``ttl``
    (seconds), and ``ts + ttl`` is in the past. Only passive notes sweep —
    critical/default history has recall value and stays until the user acts.
    Notes with unparseable timestamps are kept (never destroy on ambiguity).
    Returns the number of rows removed.
    """
    now = time.time() if now is None else now
    kept: list[dict[str, Any]] = []
    removed = 0
    try:
        for note in log:
            ttl = note.get("ttl")
            epoch = _note_ts_epoch(note)
            if (
                note.get("priority") == "passive"
                and isinstance(ttl, int)
                and not isinstance(ttl, bool)  # bool is an int subclass
                and ttl > 0
                and epoch is not None
                # ttl < now - epoch (not epoch + ttl < now): adding an
                # arbitrarily large int TTL to a float epoch raises
                # OverflowError, and the sweep-wide guard would abort the
                # whole sweep. int-vs-float comparison never overflows.
                and ttl < now - epoch
            ):
                removed += 1
                continue
            kept.append(note)
    except Exception:
        # The sweep is an optimization -- it must NEVER cost data. A poison
        # row escaping here at load time would hit _load_notifications'
        # blanket handler and empty the entire history (persisted on the
        # next mutation); in _deliver_note it would break every delivery.
        logger.warning("Notification TTL sweep aborted", exc_info=True)
        return 0
    if removed:
        log[:] = kept
    return removed


def _read_notification_lines(path: Path) -> list[str]:
    """Every line of the notifications file, terminators intact.

    ``newline=""`` disables the newline translation text mode applies by default. With
    translation on, a read turns a CRLF or a bare CR into a bare LF, so a caller that
    writes those lines back silently rewrites bytes it meant to preserve: the snapshot
    dedupe key for a timestamp-less row is its RAW bytes, so a rewritten terminator
    makes the row differ from its source record, and a later merge appends a duplicate
    instead of collapsing it. A bare CR is worse than a terminator change, because it
    is the byte that split a record into the fragments the merge deliberately keeps.

    Line BOUNDARIES are identical either way: ``str.splitlines`` breaks on CR, LF and
    CRLF whether or not the read translated them. Only the retained bytes differ, so
    reading this way changes what is preserved and never what counts as a line.
    """
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read().splitlines(keepends=True)


def _servable_note(line: str) -> dict[str, Any] | None:
    """The note a persisted JSONL line yields, or ``None`` when none can be served.

    The single acceptance test for a notification row, shared by the loader and by the
    append-time trim so the two cannot drift apart. A line the loader would skip must
    not occupy a slot in the trim's live window: it would displace a servable row, and
    the rewrite that follows deletes that row permanently.

    Parsing to a JSON object is not sufficient on its own. ``normalize_note`` raises
    for an object whose ``channel`` is unhashable, so such a row parses here and is
    still unservable, and a trim that asked only ``isinstance(row, dict)`` would count
    it as live history.

    Redaction is part of acceptance rather than the caller's job, for two reasons. Rows
    written before delivery-time redaction existed may carry unredacted LLM-derived
    content and are served to SSE clients straight from the loader's list; and the
    redactor is one of the two steps that can reject a row, so leaving it out of the
    test would reopen the divergence this function closes.

    The note is normalized and redacted in place. A caller that writes the file back
    writes the ORIGINAL bytes, never this dict, so nothing here migrates what is on
    disk.

    Leading and trailing whitespace is stripped HERE rather than by a caller, because
    ``str.strip()`` removes whitespace ``json`` does not accept -- a no-break space, for
    one -- so a caller that strips and a caller that does not reach opposite verdicts on
    the same row. This stripping decides only whether a row can be served; it never
    reaches disk, so it is not the stripping hazard the snapshot dedupe key avoids,
    where a stripped key makes two distinct byte sequences collide and deletes one.
    """
    try:
        note = normalize_note(json.loads(line.strip()))
        for key, value in note.items():
            if key != "ts":
                note[key] = _redact_note_value(value)
        return note
    except Exception:  # noqa: BLE001 -- skip the bad row, not the whole file
        # normalize_note/_redact_note_value can raise on valid-JSON rows with
        # unexpected shapes (e.g. a top-level array); keep the per-line skip
        # semantics instead of losing all history to a caller's outer except.
        logger.debug("Skipping malformed notification row", exc_info=True)
        return None


def _load_notifications() -> list[dict[str, Any]]:
    """Load persisted notifications from disk (newest last)."""
    path = _notifications_path()
    if not path.exists():
        return []
    try:
        entries: list[dict[str, Any]] = []
        for line in _read_notification_lines(path):
            parsed = _servable_note(line)
            if parsed is None:
                continue
            entries.append(parsed)
        # RFC Phase 5: drop expired passive rows BEFORE the recency cap.
        # Sweeping after truncation loses data: with more than N rows on
        # disk, newer expired-passive rows would displace older LIVE rows
        # during truncation, and the next full rewrite would delete those
        # live rows permanently. Disk rewrites lazily on
        # the next mutation; the in-memory view is authoritative for serving.
        sweep_expired_notifications(entries)
        # Keep only the most recent N live rows
        entries = entries[-_MAX_PERSISTED_NOTIFICATIONS:]
        return entries
    except Exception:
        logger.debug("Failed to load notifications", exc_info=True)
        return []


# Notification file I/O runs exclusively on this single-worker executor when
# an event loop is running: appends (from the delivery sink) and rewrites
# (from delete/ack/clear) execute strictly in submission order, so the QUEUE
# needs no lock and the loop never blocks on file I/O. Creating the executor
# does need one -- see _notification_io_executor.
_notification_io_pool: concurrent.futures.ThreadPoolExecutor | None = None


_notification_io_pool_lock = threading.Lock()


def _notification_io_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Lazily create the single-worker executor for notification persistence.

    The creation is locked, not just the queue. An unlocked check-then-set let two
    threads each observe ``None``, each construct a pool, and each proceed: one
    assignment won the global while the loser's worker was already live with its job
    already queued, so the two callers were not serialised against one another at all
    -- which is the single guarantee this executor exists to provide. Narrow window
    (the first notification I/O of the process, twice at once) and unbounded harm: an
    append landing inside a ``_rewrite_notifications`` whole-file write is simply gone,
    because the rewrite did not include it and overwrote the bytes.

    Double-checked so the lock is paid once rather than on every call.
    """
    global _notification_io_pool
    if _notification_io_pool is None:
        with _notification_io_pool_lock:
            if _notification_io_pool is None:
                _notification_io_pool = concurrent.futures.ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="notif-io"
                )
    return _notification_io_pool


def _persist_notification(note: dict[str, str]) -> bool:
    """Append a single notification to the JSONL file on disk.

    Returns True on success. Failures are swallowed (legacy system producers
    are explicitly best-effort — history is a cache, delivery is the
    broadcast) but reported via the return value so callers that need
    durability (the app push endpoint) can surface them.
    """
    path = _notifications_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", opener=owner_only_opener) as f:
            f.write(json.dumps(note) + "\n")
        # Trim if file grows too large (keep last N lines)
        _maybe_trim_notifications(path)
        return True
    except Exception:
        logger.debug("Failed to persist notification", exc_info=True)
        return False


def _rewrite_notifications(notifications: list[dict[str, str]]) -> None:
    """Rewrite the entire notifications file from the in-memory list."""
    path = _notifications_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(n) + "\n" for n in notifications[-_MAX_PERSISTED_NOTIFICATIONS:]]
        with open(path, "w", encoding="utf-8", opener=owner_only_opener) as f:
            f.write("".join(lines))
    except Exception:
        logger.debug("Failed to rewrite notifications file", exc_info=True)


def _maybe_trim_notifications(path: Path) -> None:
    """Trim the notifications file if it exceeds 2x the max.

    Expired passive rows are discarded BEFORE the recency cap — the same
    displacement hazard as the load path: trimming the
    raw tail first would retain newer expired-passive rows while deleting
    older LIVE rows, permanently losing history after the next load-time
    sweep.

    The recency cap counts only LIVE lines: a line ``_servable_note`` accepts and this
    sweep does not find expired, which is exactly what ``_load_notifications`` goes on
    to serve. Both paths ask that one predicate, so the trim cannot come to disagree
    with the loader about what counts as history. An UNSERVABLE line, one the predicate
    rejects, is still kept, because destroying a line on ambiguity is worse than
    holding one nobody can read. It is kept in a separate, smaller window so that it
    cannot displace a live notification.

    Two windows rather than one, because one shared window turns an append into an
    eviction. An unservable line has no dedupe key, so a merge appends it instead of
    collapsing it, which puts it among the NEWEST lines; a single newest-N window over
    the combined list then discards valid older notifications in its favour, and the
    rule meant to avoid destroying data is what destroys it. Neither the framing that
    produces unparseable fragments nor the withheld dedupe key is the thing to change:
    both are deliberate, and both are what stop a fragment being skipped as a false
    duplicate and its bytes lost.

    Retained lines keep their original file order AND their exact bytes, terminator
    included, so a fragment stays beside the neighbours that explain it, a final line
    with no terminator stays final instead of gluing onto the row written after it, and
    a CRLF or bare-CR row still matches the raw dedupe key its source record carries.
    """
    try:
        lines = _read_notification_lines(path)
        if len(lines) <= _MAX_PERSISTED_NOTIFICATIONS * 2:
            return
        live: list[int] = []
        unservable: list[int] = []
        for index, line in enumerate(lines):
            row = _servable_note(line)
            if row is None:
                unservable.append(index)
                continue
            if sweep_expired_notifications([row]) == 1:
                continue  # expired passive row -- drop before the cap
            live.append(index)
        # Never zero. A zero cap does not empty the window, it removes the bound:
        # ``unservable[-0:]`` is the WHOLE list, so a small cap would silently
        # retain every unservable line instead of a recent sample of them.
        unservable_cap = max(
            1, _MAX_PERSISTED_NOTIFICATIONS // _UNSERVABLE_NOTIFICATION_CAP_DIVISOR
        )
        kept = sorted(set(live[-_MAX_PERSISTED_NOTIFICATIONS:]) | set(unservable[-unservable_cap:]))
        path.write_text("".join(lines[index] for index in kept), encoding="utf-8", newline="")
    except Exception:
        pass
