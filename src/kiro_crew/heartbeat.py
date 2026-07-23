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
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Coroutine

from kiro_crew import platform_compat, shutdown_event
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.executors import maintenance_executor
from kiro_crew.memory import MemoryStore, workspace_dir
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.history import HistoryConsolidator

logger = logging.getLogger(__name__)

# Deliver target extracted from <!-- deliver:xxx --> in heartbeat entries
_DELIVER_RE = re.compile(r"<!--\s*deliver:(\S+)\s*-->")

# Agent can include this sentinel in its response to signal the task is not done
_KEEP_SENTINEL = "HEARTBEAT_KEEP"
_KEEP_RE = re.compile(_KEEP_SENTINEL, re.IGNORECASE)

_DEFAULT_INTERVAL = 60
_FTS_REBUILD_TICKS = 15  # rebuild every 15 ticks (15 min at 60s interval)
_PRUNE_TICKS = 1440  # prune old history once per day (1440 min at 60s interval)
# Per-task hard deadline for an unattended heartbeat turn. Mirrors cron's
# _JOB_TIMEOUT_SECS (1800s / 30 min): a heartbeat turn runs without a human
# present, so it MUST be bounded — otherwise a single non-allowlisted tool
# approval blocks the whole heartbeat subsystem indefinitely.
HEARTBEAT_TASK_TIMEOUT_SECS = 1800  # 30 min per heartbeat task
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
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = heartbeat_lock_path(target)
    with open(lock_path, "a+b") as lock_file:
        with platform_compat.file_lock(lock_file.fileno(), exclusive=True):
            if not target.exists():
                atomic_write(target, _HEADER, fsync=True)
            with open(target, "a", encoding="utf-8") as heartbeat_file:
                heartbeat_file.write(entry.rstrip("\n") + "\n")
                heartbeat_file.flush()
                os.fsync(heartbeat_file.fileno())


def _rewrite_heartbeat_locked(
    path: Path,
    original_tasks: list[tuple[str, str]],
    keep: list[tuple[str, str]],
) -> int:
    """Re-read, merge, and atomically replace HEARTBEAT.md under an OS lock."""
    lock_path = heartbeat_lock_path(path)
    with open(lock_path, "a+b") as lock_file:
        with platform_compat.file_lock(lock_file.fileno(), exclusive=True):
            try:
                current_tasks = _extract_tasks(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                current_tasks = []

            remaining = Counter(t for t, _ in original_tasks)
            appended: list[tuple[str, str]] = []
            for task_text, deliver in current_tasks:
                if remaining.get(task_text, 0) > 0:
                    remaining[task_text] -= 1
                else:
                    appended.append((task_text, deliver))

            lines = _HEADER
            for task_text, deliver in keep + appended:
                suffix = f"  <!-- deliver:{deliver} -->" if deliver else ""
                lines += f"- {task_text}{suffix}\n"
            atomic_write(path, lines, fsync=True)
            return len(appended)


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
        # Serializes the HEARTBEAT.md read→process→rewrite window within this
        # process so two cycles can't clobber each other's rewrite.
        self._file_lock = asyncio.Lock()

    async def start(self) -> None:
        path = heartbeat_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(_HEADER, encoding="utf-8")
        self._task = asyncio.create_task(self._loop())
        logger.info("Heartbeat started (interval=%ds)", self._interval)

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

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
        if not self._processing:
            await self._process_heartbeat_file()

        if self._tick % _FTS_REBUILD_TICKS == 0:
            loop = asyncio.get_running_loop()
            count = await loop.run_in_executor(
                maintenance_executor(), self._memory.rebuild_index
            )
            logger.info("FTS index rebuilt: %d files", count)

        if self._tick % _PRUNE_TICKS == 0:
            max_days = KiroCrewConfig.load().memory.history_max_days
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                maintenance_executor(),
                functools.partial(self._memory.prune_history, keep_days=max_days),
            )

            # Prune security event log per retention policy
            try:
                await loop.run_in_executor(maintenance_executor(), sel().prune)
            except Exception:
                logger.debug("SEL prune failed", exc_info=True)

        # Check for idle sessions needing history consolidation (every tick)
        if self._consolidator:
            self._consolidator.check_idle_sessions()

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
            if not path.exists():
                return
            content = path.read_text(encoding="utf-8").strip()
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
                        logger.warning(
                            "Heartbeat task failed: %s", task_text[:80], exc_info=result,
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
                    _rewrite_heartbeat_locked, path, tasks, keep,
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
    tasks: list[tuple[str, str]] = []
    in_comment = False
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Track multi-line HTML comments (standalone comment lines)
        if "<!--" in stripped and "-->" not in stripped:
            in_comment = True
            continue
        if in_comment:
            if "-->" in stripped:
                in_comment = False
            continue
        # Standalone comment line (<!-- ... --> on one line, no task text)
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            continue
        if stripped.startswith("#"):
            continue
        # Extract inline deliver target before stripping comments
        deliver = ""
        m = _DELIVER_RE.search(stripped)
        if m:
            deliver = m.group(1)
            stripped = stripped[: m.start()].rstrip()
        # Strip leading list markers
        for prefix in ("- [x] ", "- [ ] ", "- ", "* "):
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix) :]
                break
        stripped = stripped.strip()
        if stripped and stripped != "-":
            tasks.append((stripped, deliver))
    return tasks
