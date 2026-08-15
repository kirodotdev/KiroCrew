"""Append-only writer for the structured lifecycle event log.

Layout: ``$KIROCREW_HOME/events/YYYY-MM-DD.jsonl`` — daily shards, one compact
JSON envelope per line, routed by **append time** (the moment of the write),
never by the event's own timestamp. Routing by append time is what makes the
reader's forward-only watermark sound: a shard once passed can only ever grow
at its tail until the calendar day rolls over, so a late write (a backfill of
historical events, a delayed emit) always lands AT or AFTER the watermark.
``ts_ms`` still carries the event's own time for consumers to order by.

Write discipline (mirrors the repo's transcript rules without importing them):

- ``emit()`` never raises to the caller and never blocks a running asyncio
  event loop — inside a loop the entire write job (sequence seeding,
  serialization, size check, file append) runs on the default executor,
  fire-and-forget; failures are logged, never propagated.
- One ``write()`` call per line; before appending, a missing trailing newline
  left by a crashed writer is repaired so a torn tail can never fuse with the
  next record.
- Lines above :data:`MAX_LINE_BYTES` are refused (logged, dropped): this log
  carries lifecycle facts and pointers, not bulk content — bulk bodies stay in
  the existing stores.
- ``seq`` is monotonically increasing per shard **per writer process**
  (seeded by counting existing lines on first touch). Readers must order by
  file position, which is authoritative even across processes; ``seq`` is a
  debugging aid, not a global coordinate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from kiro_crew.config.paths import data_home
from kiro_crew.events.base import Event, serialize
from kiro_crew.platform_compat import file_lock, is_link_or_junction

logger = logging.getLogger(__name__)

#: Hard per-line cap. A lifecycle fact that does not fit in 4 KiB is carrying
#: content that belongs in an existing store, referenced by pointer.
MAX_LINE_BYTES = 4096

#: Default shard retention for :meth:`EventLog.prune`.
DEFAULT_RETENTION_DAYS = 30

_SHARD_SUFFIX = ".jsonl"


def default_events_dir() -> Path:
    """``$KIROCREW_HOME/events`` under the live data home."""
    return data_home() / "events"


def shard_name_for(ts_ms: int) -> str:
    """Daily shard filename for an epoch-milliseconds timestamp (UTC)."""
    day = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
    return day + _SHARD_SUFFIX


def is_shard_name(name: str) -> bool:
    """True only for names this writer produces: ``YYYY-MM-DD.jsonl`` with a
    REAL date (strptime round-trip). Writer, reader, and prune must all use
    this one predicate — a looser check anywhere lets a garbage file like
    ``9999-99-99.jsonl`` win the newest-shard clamp forever while prune
    (rightly) refuses to delete it.
    """
    if not name.endswith(_SHARD_SUFFIX):
        return False
    day = name[: -len(_SHARD_SUFFIX)]
    try:
        parsed = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return False
    return parsed.strftime("%Y-%m-%d") == day


class EventLog:
    """Fire-and-forget append-only writer over daily JSONL shards.

    ``now_ms`` is the append-time clock used for shard routing; it exists so
    tests can pin the calendar day. Production always uses the real clock.
    """

    def __init__(
        self,
        directory: Path | None = None,
        *,
        now_ms: Optional[Callable[[], int]] = None,
    ) -> None:
        self._dir = directory if directory is not None else default_events_dir()
        self._now_ms = now_ms if now_ms is not None else (lambda: int(time.time() * 1000))
        self._lock = threading.Lock()
        self._next_seq: dict[str, int] = {}

    @property
    def directory(self) -> Path:
        return self._dir

    # ── emit ──────────────────────────────────────────────────────────

    def emit(self, event: Event, *, src: str) -> bool:
        """Append *event*. Returns ``False`` when the event was dropped.

        The target shard is chosen by APPEND time (see module docstring), so
        late-arriving events can never land behind a reader's watermark.
        Never raises; never blocks a running event loop — when a loop is
        detected the ENTIRE write job (sequence seeding, serialization, size
        check, file append) runs on the default executor, fire-and-forget, so
        no shard I/O ever happens on the loop thread. A ``False`` return means
        the line was refused (oversized) or the append failed synchronously;
        offloaded failures are reported to the log only — callers must not
        depend on delivery.
        """
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                fut = loop.run_in_executor(None, self._write_event, event, src)
                fut.add_done_callback(_report_offloaded_failure)
                return True
            return self._write_event(event, src)
        except Exception:
            logger.warning("events: emit failed key=%s", event.key, exc_info=True)
            return False

    def _write_event(self, event: Event, src: str) -> bool:
        """Seed seq, serialize, size-check, append — one job, off-loop.

        A cross-process advisory lock (``platform_compat.file_lock`` on a
        dedicated lockfile) spans shard selection through append: without it,
        writer A could pick a shard, writer B could create a NEWER shard that
        a reader then passes, and A's delayed append would land behind the
        watermark. The shard is chosen from the clock INSIDE the lock — at
        actual append time, not emit time — so an offloaded emit delayed
        across midnight (or past a concurrent prune of yesterday) cannot
        recreate a deleted shard behind a reader's watermark. Inside the lock
        the target is additionally clamped to the newest shard on disk (this
        also covers a clock stepped backwards), so shards only ever grow at
        the newest edge. The in-process threading lock still serializes this
        writer's own threads; a refused (oversized) line does not consume a
        sequence number.
        """

        with self._lock:
            self._dir.mkdir(parents=True, exist_ok=True)
            lock_fd = os.open(self._dir / ".writer.lock", os.O_CREAT | os.O_WRONLY)
            try:
                with file_lock(lock_fd, exclusive=True, required=True):
                    shard = shard_name_for(self._now_ms())
                    newest = self._newest_shard_name()
                    if newest is not None and shard < newest:
                        shard = newest
                    seq = self._seed_seq_locked(shard)
                    line = serialize(event, src=src, seq=seq)
                    if len(line.encode("utf-8")) > MAX_LINE_BYTES:
                        logger.warning(
                            "events: dropped oversized %s line (%d bytes > %d) key=%s",
                            shard,
                            len(line.encode("utf-8")),
                            MAX_LINE_BYTES,
                            event.key,
                        )
                        return False
                    self._next_seq[shard] = seq + 1
                    self._append_line(shard, line)
                    return True
            finally:
                os.close(lock_fd)

    def _newest_shard_name(self) -> str | None:
        """Newest VALID shard filename on disk, or ``None``.

        Only names passing :func:`is_shard_name` participate — see its
        docstring for why the predicate must be strict here.
        """
        try:
            names = [p.name for p in self._dir.glob("*" + _SHARD_SUFFIX) if is_shard_name(p.name)]
        except OSError:
            return None
        return max(names) if names else None

    def _seed_seq_locked(self, shard: str) -> int:
        """Next seq for *shard*, counting existing lines on first touch."""
        cached = self._next_seq.get(shard)
        if cached is not None:
            return cached
        path = self._dir / shard
        count = 0
        try:
            if path.exists():
                with open(path, "rb") as fh:
                    count = sum(1 for _ in fh)
        except OSError:
            count = 0
        return count

    def _append_line(self, shard: str, line: str) -> None:
        """Append one line, repairing a torn tail first.

        A writer that died mid-``write`` can leave the shard without a trailing
        newline; appending directly would fuse the torn fragment with this
        record, corrupting BOTH. Repairing with a lone newline keeps the torn
        fragment isolated on its own (unparseable, skipped) line and this
        record intact.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / shard
        if path.exists() and path.stat().st_size > 0:
            with open(path, "rb") as check:
                check.seek(-1, 2)
                torn = check.read(1) != b"\n"
            if torn:
                with open(path, "ab") as repair:
                    repair.write(b"\n")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    # ── prune ─────────────────────────────────────────────────────────

    def prune(self, retention_days: int = DEFAULT_RETENTION_DAYS) -> list[str]:
        """Delete shards strictly older than the retention window.

        Returns the deleted shard filenames. Only files matching the
        ``YYYY-MM-DD.jsonl`` pattern are considered; anything else in the
        directory is left alone. ``retention_days`` must be non-negative — a
        negative value would place the cutoff in the future and delete
        current history, so it is refused loudly.
        """
        if retention_days < 0:
            raise ValueError(f"retention_days must be >= 0, got {retention_days}")
        # A linked events directory would aim the deletes at the link's
        # target -- usage shards share the same YYYY-MM-DD.jsonl naming, so
        # prune through a link is indistinguishable from deleting them.
        if is_link_or_junction(self._dir):
            raise ValueError(f"refusing to prune a linked events directory: {self._dir}")
        cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=retention_days)).strftime(
            "%Y-%m-%d"
        )
        deleted: list[str] = []
        if not self._dir.exists():
            return deleted
        # Deletions take the same cross-process writer lock as appends: an
        # unlocked prune racing a writer at day rollover could delete a
        # shard between the writer's newest-shard clamp and its append,
        # recreating the file at offset 0 behind reader watermarks.
        with self._lock:
            lock_fd = os.open(self._dir / ".writer.lock", os.O_CREAT | os.O_WRONLY)
            try:
                with file_lock(lock_fd, exclusive=True, required=True):
                    for path in sorted(self._dir.glob("*" + _SHARD_SUFFIX)):
                        if not is_shard_name(path.name):
                            continue
                        day = path.name[: -len(_SHARD_SUFFIX)]
                        if day < cutoff:
                            try:
                                path.unlink()
                                deleted.append(path.name)
                            except OSError:
                                logger.warning(
                                    "events: prune failed for %s", path.name, exc_info=True
                                )
            finally:
                os.close(lock_fd)
        return deleted


def _report_offloaded_failure(fut: "asyncio.Future[bool]") -> None:
    exc = fut.exception()
    if exc is not None:
        logger.warning("events: offloaded append failed: %r", exc)


def _main() -> int:
    parser = argparse.ArgumentParser(description="Structured event log maintenance.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_prune = sub.add_parser("prune", help="delete shards older than the retention window")
    p_prune.add_argument("--days", type=int, default=DEFAULT_RETENTION_DAYS)
    p_prune.add_argument("--dir", type=Path, default=None)
    args = parser.parse_args()
    if args.cmd == "prune":
        log = EventLog(args.dir)
        deleted = log.prune(args.days)
        print(json.dumps({"deleted": deleted, "at": time.time()}))
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    raise SystemExit(_main())
