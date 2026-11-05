"""Heartbeat service — periodic background tasks.

Runs on a configurable interval (default 60s):
- Reads HEARTBEAT.md for pending tasks → sends to agent
- Rebuilds FTS index every 15 min
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
import threading
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Coroutine, NamedTuple

from kiro_crew import memory_backup, platform_compat, shutdown_event
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.executors import maintenance_executor
from kiro_crew.llm_helpers import append_fallback_story
from kiro_crew.memory import MemoryStore, workspace_dir
from kiro_crew.metrics.provider import get_recorder
from kiro_crew.owner_only_files import (
    ensure_directory,
    owner_only_opener_for,
    write_text_owner_only_in_home,
)
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.history import HistoryConsolidator

logger = logging.getLogger(__name__)

# Deliver target extracted from <!-- deliver:xxx --> in heartbeat entries
_DELIVER_RE = re.compile(r"<!--\s*deliver:(\S+)\s*-->")
# The pieces the comment scan reads: a whole deliver tag, an opener, a closer.
# A deliver tag sits on one line; a comment that spans lines is never one, so
# its padding is spaces and tabs only, never a newline. HTML also ends a
# comment at ``--!>``, so the closer accepts it too.
_COMMENT_TOKEN_RE = re.compile(r"<!--[ \t]*deliver:\S+[ \t]*-->|<!--|--!?>")


# A leading list bullet (``-``, ``*``, ``+``, ``1.``, ``1)``), then a leading
# checkbox (``[ ]``, ``[x]``, ``[X]``). Each is a marker only when whitespace or
# the end of the text follows it, so ``-flag`` or ``[x]y`` stay task text.
_LIST_BULLET_RE = re.compile(r"^(?:[-*+]|\d{1,9}[.)])(?:\s+|$)")
_CHECKBOX_RE = re.compile(r"^\[[ xX]\](?:\s+|$)")


def _comment_spans(content: str) -> list[tuple[int, int]]:
    """Return the ``(start, end)`` offsets of every hidden HTML comment in *content*.

    One scan, left to right. Outside a comment a deliver tag is kept for the
    per-line parse. Inside one, a deliver tag is part of the comment, so a task
    commented out together with its tag stays hidden. A ``<!--`` with no
    ``-->`` after it hides only the rest of its own line and logs a warning:
    hiding every later line would drop those tasks without a word.
    """
    spans: list[tuple[int, int]] = []
    pos = 0
    while True:
        opener = next(
            (m.start() for m in _COMMENT_TOKEN_RE.finditer(content, pos) if m.group() == "<!--"),
            -1,
        )
        if opener == -1:
            return spans
        closer = next(
            (
                m.end()
                for m in _COMMENT_TOKEN_RE.finditer(content, opener + 4)
                if m.group() in ("-->", "--!>")
            ),
            -1,
        )
        if closer == -1:
            logger.warning(
                "HEARTBEAT.md line %d opens an HTML comment that is never closed;"
                " ignoring the rest of that line only",
                content.count("\n", 0, opener) + 1,
            )
            end_of_line = content.find("\n", opener)
            closer = len(content) if end_of_line == -1 else end_of_line
        spans.append((opener, closer))
        pos = closer


def _hide_spans(content: str, spans: list[tuple[int, int]]) -> str:
    """Replace each span with just its newlines, so every line keeps its index."""
    parts: list[str] = []
    pos = 0
    for start, end in spans:
        parts.append(content[pos:start])
        parts.append("\n" * content.count("\n", start, end))
        pos = end
    parts.append(content[pos:])
    return "".join(parts)


class _FileLine(NamedTuple):
    """One ``\\n``-separated line of HEARTBEAT.md as the rewrite sees it.

    ``head`` is the part of a comment that began on an earlier line and ends on
    this one; ``tail`` is the part of a comment that begins here and runs on.
    Dropping a finished task's line keeps both, so a comment never loses its
    opener or closer.
    """

    raw: str
    tasks: list[tuple[str, str]]
    head: str
    tail: str


def _parse_lines(content: str) -> list[_FileLine]:
    """Split *content* on ``\\n`` and parse the tasks each line holds."""
    spans = _comment_spans(content)
    visible = _hide_spans(content, spans).split("\n")
    lines: list[_FileLine] = []
    start = 0
    first = 0  # index of the first span that ends after this line starts
    for index, raw in enumerate(content.split("\n")):
        end = start + len(raw)
        while first < len(spans) and spans[first][1] <= start:
            first += 1
        head = tail = ""
        if first < len(spans) and spans[first][0] < start:
            head = content[start : spans[first][1]]
        last = first
        while last + 1 < len(spans) and spans[last + 1][0] < end:
            last += 1
        if last < len(spans) and start <= spans[last][0] < end < spans[last][1]:
            tail = content[spans[last][0] : end]
        tasks = [t for piece in visible[index].splitlines() if (t := _parse_task(piece))]
        lines.append(_FileLine(raw, tasks, head, tail))
        start = end + 1
    return lines


def _format_task(task_text: str, deliver: str) -> str:
    suffix = f"  <!-- deliver:{deliver} -->" if deliver else ""
    return f"- {task_text}{suffix}"


# Agent can include this sentinel in its response to signal the task is not done
_KEEP_SENTINEL = "HEARTBEAT_KEEP"
_KEEP_RE = re.compile(_KEEP_SENTINEL, re.IGNORECASE)

_DEFAULT_INTERVAL = 60
_FTS_REBUILD_TICKS = 15  # rebuild every 15 ticks (15 min at 60s interval)
_PRUNE_TICKS = 1440  # prune old history once per day (1440 min at 60s interval)
# Memory backup runs on its OWN counter, offset from _PRUNE_TICKS rather than sharing
# it: pruning history and copying every store are both minutes-long on a large
# install, and landing them on the same tick puts two of four maintenance workers on
# the same second once a day. The offset costs nothing and keeps them apart.
_MEMORY_BACKUP_TICKS = 1440
_MEMORY_BACKUP_OFFSET = 30
# Per-task hard deadline for an unattended heartbeat turn. Mirrors cron's
# _JOB_TIMEOUT_SECS (1800s / 30 min): a heartbeat turn runs without a human
# present, so it MUST be bounded — otherwise a single non-allowlisted tool
# approval blocks HEARTBEAT.md task processing indefinitely.
HEARTBEAT_TASK_TIMEOUT_SECS = 1800  # 30 min per heartbeat task
# How long gateway shutdown waits for cancelled heartbeat tasks to unwind.
_STOP_DRAIN_SECS = 10
HEARTBEAT_FILE = "HEARTBEAT.md"
_HEADER = (
    "# Heartbeat Tasks\n\n<!-- Add tasks below (one per line). "
    "KiroCrew picks them up on next heartbeat. -->\n"
)


def heartbeat_path() -> Path:
    return workspace_dir() / HEARTBEAT_FILE


def heartbeat_lock_path(path: Path | None = None) -> Path:
    """Return the sibling lock file shared by all HEARTBEAT.md writers."""
    target = path or heartbeat_path()
    return target.with_name(f"{target.name}.lock")


def append_heartbeat_task(entry: str, path: Path | None = None) -> None:
    """Append one heartbeat entry under the cross-process writer lock.

    Internal producers must use this helper rather than opening HEARTBEAT.md
    directly, so the service's final read→replace transaction cannot overwrite
    an append from another process.
    """
    target = path or heartbeat_path()
    ensure_directory(target.parent)
    lock_path = heartbeat_lock_path(target)
    with open(lock_path, "a+b", opener=owner_only_opener_for(lock_path)) as lock_file:
        with platform_compat.file_lock(lock_file.fileno(), exclusive=True):
            if not target.exists():
                atomic_write(target, _HEADER, fsync=True)
            # A file whose last line has no newline (a hand edit, or a pass
            # that kept such a line) would glue the entry onto that line.
            unterminated = False
            with open(target, "rb") as existing:
                if existing.seek(0, os.SEEK_END) > 0:
                    existing.seek(-1, os.SEEK_END)
                    unterminated = existing.read(1) != b"\n"
            with open(
                target, "a", encoding="utf-8", opener=owner_only_opener_for(target)
            ) as heartbeat_file:
                prefix = "\n" if unterminated else ""
                heartbeat_file.write(prefix + entry.rstrip("\n") + "\n")
                heartbeat_file.flush()
                os.fsync(heartbeat_file.fileno())


def _rewrite_heartbeat_locked(
    path: Path,
    original_tasks: list[tuple[str, str]],
    keep: list[tuple[str, str]],
) -> int:
    """Re-read the file and drop the finished tasks, all under an OS lock.

    Only the lines that hold a finished task change. Every other line --
    headings, notes, commented-out tasks, kept tasks and tasks appended while
    the cycle ran -- is written back byte for byte, in order. Returns how many
    tasks were appended mid-cycle.
    """
    lock_path = heartbeat_lock_path(path)
    with open(lock_path, "a+b", opener=owner_only_opener_for(lock_path)) as lock_file:
        with platform_compat.file_lock(lock_file.fileno(), exclusive=True):
            try:
                # newline="" on both ends: the line endings stay as they were.
                with open(path, encoding="utf-8", newline="") as heartbeat_file:
                    content = heartbeat_file.read()
            except FileNotFoundError:
                # Nothing left to preserve: seed a fresh file with the kept tasks.
                body = "".join(_format_task(t, d) + "\n" for t, d in keep)
                atomic_write(path, _HEADER + body, fsync=True)
                return 0
            except (OSError, UnicodeDecodeError) as exc:
                # Rewriting would replace content nobody could read; leave it.
                logger.warning("HEARTBEAT.md unreadable, not rewriting it: %s", exc)
                return 0

            snapshot = Counter(t for t, _ in original_tasks)
            # Keyed on text AND deliver target: of two same-text tasks bound
            # for different channels, the finished one goes, the kept one stays.
            finished = Counter(original_tasks) - Counter(keep)
            appended = 0
            out: list[str] = []
            lines = _parse_lines(content)
            for index, line in enumerate(lines):
                # Each kept line keeps its own terminator: only the last
                # line of the split has none, so dropping it never strips
                # the newline off the line before it.
                newline = "\n" if index < len(lines) - 1 else ""
                left: list[tuple[str, str]] = []
                for task in line.tasks:
                    if snapshot[task[0]] > 0:
                        snapshot[task[0]] -= 1
                    else:
                        appended += 1
                    if finished[task] > 0:
                        finished[task] -= 1
                    else:
                        left.append(task)
                if len(left) == len(line.tasks):
                    out.append(line.raw + newline)
                    continue
                pieces = [line.head, *(_format_task(t, d) for t, d in left), line.tail]
                pieces = [p.removesuffix("\r") for p in pieces if p]
                if pieces:
                    # A line split in two keeps the file's own line ending.
                    cr = "\r" if line.raw.endswith("\r") else ""
                    out.append(f"{cr}\n".join(pieces) + cr + newline)
            atomic_write(path, "".join(out), fsync=True, newline="")
            return appended


def _ensure_heartbeat_file(path: Path) -> None:
    """Create the workspace dir and seed HEARTBEAT.md. Blocking; call off-loop."""
    ensure_directory(path.parent)
    if not path.exists():
        write_text_owner_only_in_home(path, _HEADER)


def _read_heartbeat_file(path: Path) -> str | None:
    """Return HEARTBEAT.md's stripped content, or ``None`` if it is absent or unreadable.

    Blocking; call off-loop. The existence check rides with the read so a file
    removed between them cannot turn a missing file into a raised
    ``FileNotFoundError``, and so the pair costs one worker hop rather than two.
    An unreadable or non-UTF-8 file is treated as absent: raising here would
    skip the rest of every tick, retention pruning included.
    """
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("HEARTBEAT.md unreadable, skipping its tasks: %s", exc)
        return None


class HeartbeatService:
    """Periodic wake-up that runs background maintenance tasks."""

    def __init__(
        self,
        memory: MemoryStore,
        on_task: Callable[[str, str], Coroutine] | None = None,
        interval: int = _DEFAULT_INTERVAL,
        consolidator: HistoryConsolidator | None = None,
        on_cycle_end: Callable[[], Coroutine] | None = None,
    ) -> None:
        self._memory = memory
        self._on_task = on_task
        self._interval = interval
        self._consolidator = consolidator
        # Called once after every cycle's tasks finish (regardless of
        # individual task success/fail).  Owner uses this to recycle the
        # shared heartbeat session at cycle boundaries — the per-task
        # ``finally`` block is too aggressive when concurrent tasks share
        # one session key.
        self._on_cycle_end = on_cycle_end
        self._tick = 0
        self._processing = False
        self._task: asyncio.Task | None = None  # type: ignore[type-arg]
        # HEARTBEAT.md processing runs as its own task so a long agent turn
        # (up to HEARTBEAT_TASK_TIMEOUT_SECS) does not hold the tick loop:
        # FTS rebuild, prune and idle consolidation keep their cadence.
        self._process_task: asyncio.Task | None = None
        # Loop tasks stop() cancelled, kept so wait_closed() can await them.
        self._stopped_tasks: list[asyncio.Task] = []
        self._memory_backup_task: asyncio.Task | None = None
        self._memory_backup_stop = threading.Event()
        self._memory_backup_started = False
        # Serializes the HEARTBEAT.md read→process→rewrite window within this
        # process so two cycles can't clobber each other's rewrite.
        self._file_lock = asyncio.Lock()

    async def start(self) -> None:
        # Off the loop: start() is awaited on the gateway boot path, before
        # KIROCREW_READY, and workspace_dir() may sit on a network or synced
        # volume where mkdir + write_text block (AUTOSDE
        # no-blocking-call-on-event-loop, no-new-work-on-gateway-boot-path).
        await asyncio.to_thread(_ensure_heartbeat_file, heartbeat_path())
        self._task = asyncio.create_task(self._loop())
        logger.info("Heartbeat started (interval=%ds)", self._interval)

    def stop(self) -> None:
        self._memory_backup_stop.set()
        if self._memory_backup_task is not None:
            self._memory_backup_task.cancel()
        if self._process_task is not None:
            self._process_task.cancel()
        if self._task:
            self._task.cancel()
            self._stopped_tasks.append(self._task)
            self._task = None

    async def wait_closed(self) -> None:
        """Await the tasks ``stop()`` cancelled, bounded by ``_STOP_DRAIN_SECS``.

        A cancelled heartbeat turn unwinds its ``finally`` blocks (session
        release, cycle-end recycle) here rather than after the gateway has
        moved on to closing the sessions those blocks touch.
        """
        tasks = [
            t
            for t in (*self._stopped_tasks, self._process_task, self._memory_backup_task)
            if t is not None and not t.done()
        ]
        self._stopped_tasks.clear()
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=_STOP_DRAIN_SECS)
        if pending:
            logger.warning(
                "Heartbeat: %d task(s) still running %ss after stop", len(pending), _STOP_DRAIN_SECS
            )

    async def _loop(self) -> None:
        while not shutdown_event.is_set():
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=self._interval)
                return  # shutdown signaled
            except asyncio.TimeoutError:
                pass  # normal wake-up
            self._tick += 1
            try:
                await self._beat()
            except Exception:
                logger.warning("Heartbeat tick failed", exc_info=True)

    async def _beat(self) -> None:
        from kiro_crew.memory_startup import (
            MemoryStartupUnavailable,
            require_memory_prepared,
            require_memory_ready,
        )

        require_memory_prepared()
        if (
            not self._memory_backup_started
            or self._tick % _MEMORY_BACKUP_TICKS == _MEMORY_BACKUP_OFFSET
        ):
            self._schedule_memory_backup()
        try:
            require_memory_ready()
        except MemoryStartupUnavailable:
            # Healthy member backups and idle consolidation remain eligible
            # while this gateway's Global memory awaits owner recovery.
            if self._consolidator:
                self._consolidator.check_idle_sessions()
            return
        self._schedule_heartbeat_file()

        if self._tick % _FTS_REBUILD_TICKS == 0:
            loop = asyncio.get_running_loop()
            count = await loop.run_in_executor(maintenance_executor(), self._memory.rebuild_index)
            logger.info("FTS index rebuilt: %d files", count)

        if self._tick % _PRUNE_TICKS == 0:
            max_days = KiroCrewConfig.load().memory.history_max_days
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                maintenance_executor(),
                functools.partial(self._memory.prune_history, keep_days=max_days),
            )

            # Prune security event log per retention policy.
            #
            # maintenance_executor is deliberate, despite this pool's "fast
            # periodic sweeps" charter and prune taking seconds on a large log.
            # The three pools split on what makes a worker UN-RECLAIMABLE, not
            # on duration: subprocess_executor is for work that can hang
            # indefinitely on a wedged kernel resource, discovery_executor for
            # work whose concurrency is set by remote callers.  prune is
            # neither -- it terminates, it is awaited so it never overlaps
            # itself, and it fires once per _PRUNE_TICKS, so one of four
            # mc-maint workers once a day cannot starve the orphan-reaping
            # sweeps this pool protects.  prune_history above is the same
            # retention work on the same pool.
            try:
                await loop.run_in_executor(maintenance_executor(), sel().prune)
            except Exception:
                # WARNING, not debug: a dead prune means retention silently
                # stops -- the unbounded-growth failure prune exists to
                # prevent.  Counter so it is alarmable; guarded so a telemetry
                # fault cannot take the heartbeat loop down with it.
                logger.warning("SEL prune failed", exc_info=True)
                try:
                    get_recorder().counter("kirocrew.sel.prune_failed.count")
                except Exception:
                    pass

        # Check for idle sessions needing history consolidation (every tick)
        if self._consolidator:
            self._consolidator.check_idle_sessions()

    def _schedule_heartbeat_file(self) -> None:
        """Start one HEARTBEAT.md pass unless the previous one is still running.

        The tick does not await it: a task turn can run for
        ``HEARTBEAT_TASK_TIMEOUT_SECS``, and the rest of the beat must not wait
        that long. A tick that finds a pass in flight skips the file, so two
        passes never overlap.
        """
        if self._processing or (self._process_task is not None and not self._process_task.done()):
            return
        self._process_task = asyncio.create_task(self._run_heartbeat_file())

    async def _run_heartbeat_file(self) -> None:
        # The pass logs its own failure: a raise here must not escape as an
        # unretrieved task exception.
        try:
            await self._process_heartbeat_file()
        except Exception:
            logger.warning("Heartbeat task processing failed", exc_info=True)

    def _schedule_memory_backup(self) -> None:
        """One owned pass; ticks keep serving while a large store is copied."""
        if self._memory_backup_stop.is_set():
            return
        if self._memory_backup_task is not None and not self._memory_backup_task.done():
            return
        self._memory_backup_started = True
        self._memory_backup_task = asyncio.create_task(self._back_up_memory())

    async def _back_up_memory(self) -> None:
        """Take a rotating copy of every active memory store, the default one first.

        The default store is the one every install has and the one the module
        exists for; a sweep that visited only member silos protected the small
        stores and left the largest one with no copy at all.

        Offloaded to ``maintenance_executor`` because the SQLite backup API is blocking
        and copies the whole file; on the event loop a large store would stall every
        task. Same pool and same rationale as ``prune_history`` above — it terminates,
        the retained task prevents overlapping passes. The first eligible
        heartbeat checks existing backup freshness, so frequent restarts cannot
        postpone durability indefinitely; subsequent passes use the daily tick.

        Guarded so a backup failure cannot take the heartbeat down, and logged at
        WARNING rather than debug: a dead backup means durability silently stops, which
        is the whole failure this exists to prevent, so it has to be alarmable.
        """

        def copy_active_stores():
            if self._memory_backup_stop.is_set():
                return None
            cfg = KiroCrewConfig.load().memory
            if not cfg.backup_enabled:
                return None
            return memory_backup.back_up_all_stores(
                int(cfg.backup_keep),
                should_stop=self._memory_backup_stop.is_set,
            )

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(maintenance_executor(), copy_active_stores)
            if result is None:
                return
            if result["failed"]:
                # WARNING on its own, so a store whose copy fails every pass is
                # alarmable. Durability stopping quietly is the failure this exists to
                # prevent, and an INFO line among the copied counts is not visible.
                logger.warning(
                    "Memory backup: %d store(s) FAILED to copy (%d copied, %d skipped)",
                    result["failed"],
                    result["backed_up"],
                    result["skipped"],
                )
            elif result["backed_up"]:
                logger.info(
                    "Memory backup: %d store(s) copied, %d old removed, %d skipped",
                    result["backed_up"],
                    result["pruned"],
                    result["skipped"],
                )
        except Exception:
            logger.warning("Memory backup pass failed", exc_info=True)

    async def _run_one_task(self, task_text: str, deliver: str) -> str | None:
        """Execute a single heartbeat task (used by gather).

        Returns the agent response text, or ``None`` if no callback.
        """
        assert self._on_task is not None
        return await self._on_task(task_text, deliver)

    async def _process_heartbeat_file(self) -> None:
        path = heartbeat_path()
        # Hold the file lock across the whole read→process→rewrite window so a
        # concurrent cycle can't rewrite the file from a stale snapshot.
        async with self._file_lock:
            # Off the loop, like the rewrite that closes this same lock window
            # below: this runs every tick for the life of the process, and the
            # file lives under workspace_dir().
            content = await asyncio.to_thread(_read_heartbeat_file, path)
            if content is None:
                return
            tasks = _extract_tasks(content)
            if not tasks or not self._on_task:
                return

            self._processing = True
            try:
                logger.info("Heartbeat: %d task(s) found", len(tasks))
                keep: list[tuple[str, str]] = []
                results = await asyncio.gather(
                    *[self._run_one_task(t, d) for t, d in tasks],
                    return_exceptions=True,
                )
                for (task_text, deliver), result in zip(tasks, results):
                    if isinstance(result, BaseException):
                        # A chain-exhaustion failure carries the fallback story
                        # on the exception; this log line is the heartbeat's
                        # terminal error text, so append it here — the walk
                        # must not be reported as just the last candidate's
                        # failure (llm_helpers.FALLBACK_STORY_ATTR).
                        logger.warning(
                            "Heartbeat task failed: %s",
                            append_fallback_story(task_text[:80], result),
                            exc_info=result,
                        )
                        keep.append((task_text, deliver))
                    elif _should_keep(result):
                        logger.info("Heartbeat task incomplete, keeping: %s", task_text[:80])
                        keep.append((task_text, deliver))

                # Re-read, merge mid-cycle appends, and replace atomically while
                # holding the sibling OS lock. Every internal writer uses the
                # same lock via append_heartbeat_task(), so no append can land
                # between this final read and os.replace. The entire durable
                # transaction runs off the gateway event-loop thread.
                appended_count = await asyncio.to_thread(
                    _rewrite_heartbeat_locked,
                    path,
                    tasks,
                    keep,
                )
                if appended_count:
                    logger.info(
                        "Heartbeat: preserving %d task(s) appended mid-cycle",
                        appended_count,
                    )
            finally:
                self._processing = False
                # Cycle-end teardown — runs once after ALL tasks in this cycle
                # complete.  Owner can use this to conditionally recycle the
                # shared heartbeat session (e.g. only when context > threshold)
                # so multi-task cycles don't tear down the session another
                # in-flight task is still using.
                if self._on_cycle_end is not None:
                    try:
                        await self._on_cycle_end()
                    except Exception:
                        logger.warning("Heartbeat: on_cycle_end callback failed", exc_info=True)


def _should_keep(result: str | None) -> bool:
    """Return True if the agent response signals the task is incomplete."""
    if result is None:
        return False
    return bool(_KEEP_RE.search(result))


def strip_keep_sentinel(text: str) -> str:
    """Remove HEARTBEAT_KEEP sentinel from text."""
    return _KEEP_RE.sub("", text).strip()


def is_keep_response(text: str | None) -> bool:
    """Return True if *text* contains the HEARTBEAT_KEEP sentinel (case-insensitive).

    Use this to check whether a heartbeat task signaled "not done, retry next cycle".
    """
    if text is None:
        return False
    return _KEEP_SENTINEL in text.upper()


def _extract_tasks(content: str) -> list[tuple[str, str]]:
    """Extract tasks as ``(text, deliver_target)`` tuples.

    ``deliver_target`` comes from an inline ``<!-- deliver:xxx -->`` comment.
    Empty string when absent.
    """
    return [task for line in _parse_lines(content) for task in line.tasks]


def _parse_task(line: str) -> tuple[str, str] | None:
    """Parse one comment-free line into ``(text, deliver_target)``, or ``None``."""
    stripped = line.strip()
    if not stripped:
        return None
    # Standalone comment line (<!-- ... --> on one line, no task text)
    if stripped.startswith("<!--") and stripped.endswith("-->"):
        return None
    if stripped.startswith("#"):
        return None
    # Extract inline deliver target before stripping comments
    deliver = ""
    m = _DELIVER_RE.search(stripped)
    if m:
        deliver = m.group(1)
        stripped = stripped[: m.start()].rstrip()
    # Strip a leading list bullet, then a leading checkbox
    stripped = _LIST_BULLET_RE.sub("", stripped, count=1)
    stripped = _CHECKBOX_RE.sub("", stripped, count=1).strip()
    return (stripped, deliver) if stripped else None
