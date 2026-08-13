"""Queue file operations for Mochi's task queue (``mochi-queue.json``).

Ported from ``src/main/queueFile.ts``. Pure functions for filtering, routing and
updating the queue, plus atomic read/write.

PORTING NOTES — behaviours preserved deliberately, do not "fix" without a
decision:

* JavaScript's ``new Date(bad).getTime()`` is ``NaN``, and every comparison
  against ``NaN`` is false. That silently shapes three functions:
  - ``cleanup_done_tasks``: a done task with an unparseable ``executed_at`` is
    DROPPED (``NaN > cutoff`` is false).
  - ``get_executable_tasks``: a task with an unparseable ``execute_after`` is
    NOT executable (``NaN <= now`` is false).
  - ``route_poll``: an unparseable ``planned_until`` yields ``execute``, not
    ``plan`` (``NaN < now + ahead`` is false).
  ``_epoch_ms`` returns ``None`` for unparseable input and each call site spells
  out which way the comparison falls, so the asymmetry is visible rather than
  accidental.
* ``requires_agent`` is compared with strict ``=== true`` in the original, so a
  truthy-but-not-True value does NOT count against the agent budget. The port
  uses ``is True``.
* Warning strings are user-visible and are reproduced verbatim, including the
  single space joining the two possible warnings.

The ``now_ms`` parameter is injected everywhere the original read the wall clock
directly. The original called ``Date.now()`` inside three functions, which makes
differential testing impossible (two runtimes never agree on the millisecond).
Callers that want current time pass nothing and get ``time.time()``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, TypedDict

from kiro_crew.atomic_write import atomic_write
from kiro_crew.platform_compat import file_lock

logger = logging.getLogger(__name__)

#: The queue file's name, owned HERE because this module owns queue file
#: operations. It used to be defined independently in both ``hooks`` and
#: ``mcp_server`` — two copies of one literal, which meant renaming the file in
#: one place left the other reading a path nothing writes, with no error to see.
#: Every reader now takes it from this single definition.
QUEUE_FILE = "mochi-queue.json"

# ── Task type constants (mirror shared/queueTypes.ts) ──────────────────────

DETERMINISTIC_TYPES = ("move", "notify", "mood")
AGENT_TYPES = ("freestyle",)

# Default TTL for done-task cleanup: 2 hours.
DEFAULT_TTL_MS = 2 * 60 * 60 * 1000

# How far before planned_until to trigger an early replan.
PLAN_AHEAD_MS = 3 * 60 * 1000

# Max agent tasks (requires_agent is True) allowed per full_replace.
MAX_AGENT_TASKS = 2

PollRoute = Literal["plan", "replan", "execute"]


class PlannerNotes(TypedDict, total=False):
    skipped_tips: list[str]
    watching_last_status: dict[str, str]
    user_pattern: str


# QueueTask / MochiQueue are intentionally plain dicts rather than dataclasses:
# the original is a structural type over parsed JSON, and ``applyUpdate`` merges
# arbitrary partials into tasks (``{...task, ...partial}``). Modelling that with
# dataclasses would either drop unknown keys or need a passthrough bag, both of
# which change round-trip behaviour. Dicts round-trip exactly.
QueueTask = dict[str, Any]
MochiQueue = dict[str, Any]


def _now_ms() -> int:
    """Current wall clock in epoch milliseconds, matching ``Date.now()``."""
    return int(time.time() * 1000)


def _epoch_ms(value: Any) -> int | None:
    """Parse an ISO timestamp to epoch ms, or None when unparseable.

    Mirrors ``new Date(value).getTime()`` returning NaN: callers must decide
    explicitly how a None falls, because in JavaScript every NaN comparison is
    false and that asymmetry is load-bearing here.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    # datetime.fromisoformat rejects the trailing 'Z' before Python 3.11.
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # JS treats a bare date-time as local time; the queue is written with
        # toISOString() (always UTC), so bare values only appear in hand-edited
        # or corrupted files. Assume UTC rather than the host zone so the result
        # does not depend on where the gateway runs.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _iso(epoch_ms: int) -> str:
    """Format epoch ms as an ISO-8601 UTC string, matching ``toISOString()``."""
    dt = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ── I/O ────────────────────────────────────────────────────────────────────


@contextlib.contextmanager
def queue_mutation(queue_path: str | Path) -> Iterator[None]:
    """Cross-process lock for queue read-modify-write sequences.

    ``write_queue_atomic`` makes each WRITE atomic, but a read-modify-write is
    not: the MCP server (a separate process appending agent actions) and the
    gateway's poller (marking tasks done) can interleave between one side's
    read and its write, and the later replace silently drops the other side's
    update. Every mutation therefore takes this advisory lock — a sibling
    ``.lock`` file, so the lock fd is never the file being atomically replaced
    (flock follows the inode; locking the data file would guard a vanishing
    inode after the first rename).

    Plain reads stay lock-free on purpose: the atomic replace already
    guarantees a reader sees a complete document, and the read paths include
    the poller's per-tick poll where added latency is felt.
    """
    lock_path = Path(queue_path).with_name(Path(queue_path).name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with file_lock(fd, exclusive=True):
            yield
    finally:
        os.close(fd)


def read_queue(queue_path: str | Path) -> MochiQueue | None:
    """Read and parse the queue file. None on any failure.

    Failure means: missing, unreadable, not JSON, not an object, no ``tasks``
    list, or ``planned_at``/``planned_until`` not strings. A missing file is
    silent; anything else warns — same split as the original.
    """
    path = Path(queue_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as err:
        logger.warning("[read_queue] Failed to read %s: %s", path, err)
        return None

    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as err:
        logger.warning("[read_queue] Failed to read %s: %s", path, err)
        return None

    if not isinstance(parsed, dict) or not isinstance(parsed.get("tasks"), list):
        logger.warning("[read_queue] Invalid queue shape in %s — missing tasks array", path)
        return None
    if not isinstance(parsed.get("planned_at"), str) or not isinstance(
        parsed.get("planned_until"), str
    ):
        logger.warning("[read_queue] Invalid queue shape in %s — missing timestamps", path)
        return None
    return parsed


def write_queue_atomic(queue_path: str | Path, data: MochiQueue) -> None:
    """Atomically write the queue: temp file in the same dir, then rename.

    POSIX rename is atomic, so a concurrent reader never observes a partial
    write. The temp name carries the pid, matching the original, so two
    processes cannot clobber each other's temp file.

    0600 like every other Mochi state file: the queue is the planner's task
    list — what the user asked for and what the pet intends to do — and it was
    the one file landing at the umask default (0644) because it wrote its own
    temp file rather than going through ``atomic_write``. It now goes through
    ``atomic_write`` with an explicit 0600, which also cleans up the temp when
    the write fails (the hand-rolled version left it behind) and retries the
    rename on a Windows sharing violation.
    """
    atomic_write(
        Path(queue_path),
        json.dumps(data, indent=2, ensure_ascii=False),
        mode=0o600,
    )


# ── Pure functions ─────────────────────────────────────────────────────────


def cleanup_done_tasks(
    queue: MochiQueue,
    ttl_ms: int = DEFAULT_TTL_MS,
    now_ms: int | None = None,
) -> MochiQueue:
    """Drop done tasks whose ``executed_at`` is older than ``ttl_ms``.

    Kept: not done; done with no ``executed_at``. Dropped: done and older than
    the cutoff — and also done with an UNPARSEABLE ``executed_at``, because the
    original's ``NaN > cutoff`` is false. Does not mutate the input.
    """
    cutoff = (_now_ms() if now_ms is None else now_ms) - ttl_ms

    def keep(task: QueueTask) -> bool:
        if not task.get("done"):
            return True
        stamp = task.get("executed_at")
        if not stamp:
            return True  # done but no timestamp — keep it
        executed = _epoch_ms(stamp)
        if executed is None:
            return False  # NaN > cutoff is false
        return executed > cutoff

    out = dict(queue)
    out["tasks"] = [t for t in queue.get("tasks", []) if keep(t)]
    return out


def get_executable_tasks(queue: MochiQueue, now_ms: int) -> list[QueueTask]:
    """Tasks due for execution: not done and ``execute_after`` at or before now.

    An unparseable ``execute_after`` is NOT executable, matching the original's
    ``NaN <= nowMs`` being false.
    """
    result: list[QueueTask] = []
    for task in queue.get("tasks", []):
        if task.get("done"):
            continue
        due = _epoch_ms(task.get("execute_after"))
        if due is None:
            continue  # NaN <= now is false
        if due <= now_ms:
            result.append(task)
    return result


def route_poll(queue: MochiQueue | None, now_ms: int | None = None) -> PollRoute:
    """Decide what the poller should do next.

    Priority order:
      1. missing queue        -> ``plan``   (first run)
      2. ``needs_replan``     -> ``replan`` (user interaction triggered)
      3. ``planned_until`` expired, or expiring within PLAN_AHEAD_MS -> ``plan``
      4. otherwise            -> ``execute``

    "All tasks done" deliberately does NOT trigger ``plan``: when everything in
    the current window is finished the pet idles until ``planned_until`` expires.
    Re-planning there caused a rapid loop whenever the planner produced a
    minimal plan that completed in seconds.

    An unparseable ``planned_until`` yields ``execute``, because the original's
    ``NaN < Date.now() + PLAN_AHEAD_MS`` is false. That is arguably wrong — a
    corrupt timestamp arguably ought to force a re-plan — but it is the shipped
    behaviour and changing it is a decision, not a port.
    """
    if queue is None:
        return "plan"
    if queue.get("needs_replan"):
        return "replan"
    until = _epoch_ms(queue.get("planned_until"))
    if until is None:
        return "execute"  # NaN < threshold is false
    now = _now_ms() if now_ms is None else now_ms
    if until < now + PLAN_AHEAD_MS:
        return "plan"
    return "execute"


# ── Update logic ───────────────────────────────────────────────────────────


class ApplyUpdateResult(TypedDict, total=False):
    queue: MochiQueue
    warning: str


def apply_update(
    queue: MochiQueue,
    params: dict[str, Any],
    now_ms: int | None = None,
) -> ApplyUpdateResult:
    """Apply an update-plan payload to a queue.

    ``full_replace`` swaps the whole queue, subject to two guards:

    * Agent-task budget — at most ``MAX_AGENT_TASKS`` tasks with
      ``requires_agent is True`` survive; the rest are dropped and a warning is
      returned. Non-agent tasks are never dropped.
    * ``planned_until`` validity — if it is unparseable or less than five minutes
      away, the planning agent probably made a timezone mistake, so it is
      clamped to one hour out. Without this the queue immediately routes back to
      ``plan`` and loops.

    Unexecuted reminder tasks from the old queue are carried over, because
    reminders are time-sensitive and a chat-inserted reminder should survive a
    planner replace. A reminder already completed in the watch list may be
    carried over too (this function has no watch-list access); the spawned agent
    re-checks status, so the worst case is one no-op spawn.

    Without ``full_replace`` the changes apply in order: add, remove by id,
    modify by id (unknown ids silently skipped), then metadata.
    """
    now = _now_ms() if now_ms is None else now_ms

    full_replace = params.get("full_replace")
    if full_replace:
        replaced: MochiQueue = dict(full_replace)
        warning: str | None = None

        tasks = list(replaced.get("tasks", []))
        agent_tasks = [t for t in tasks if t.get("requires_agent") is True]
        if len(agent_tasks) > MAX_AGENT_TASKS:
            kept: list[QueueTask] = []
            agent_count = 0
            for task in tasks:
                if task.get("requires_agent") is not True:
                    kept.append(task)
                    continue
                agent_count += 1
                if agent_count <= MAX_AGENT_TASKS:
                    kept.append(task)
            tasks = kept
            warning = (
                f"Agent task budget exceeded: {len(agent_tasks)} agent tasks found, "
                f"truncated to {MAX_AGENT_TASKS}. Excess tasks were removed."
            )
        replaced["tasks"] = tasks

        if replaced.get("planned_until"):
            until = _epoch_ms(replaced["planned_until"])
            min_window = now + 5 * 60_000
            if until is None or until < min_window:
                corrected = _iso(now + 60 * 60_000)
                old_val = replaced["planned_until"]
                replaced["planned_until"] = corrected
                warning = (warning + " " if warning else "") + (
                    f"planned_until was in the past or too short ({old_val}), "
                    f"corrected to {corrected}."
                )

        preserved = [
            t
            for t in queue.get("tasks", [])
            if t.get("category") == "reminder" and not t.get("done")
        ]
        new_ids = {t.get("id") for t in tasks}
        for reminder in preserved:
            if reminder.get("id") not in new_ids:
                tasks.append(reminder)

        result: ApplyUpdateResult = {"queue": replaced}
        if warning is not None:
            result["warning"] = warning
        return result

    # ── incremental path ───────────────────────────────────────────────────
    tasks = list(queue.get("tasks", []))

    to_add = params.get("tasks_to_add")
    if to_add:
        tasks = tasks + list(to_add)

    to_remove = params.get("tasks_to_remove")
    if to_remove:
        remove_set = set(to_remove)
        tasks = [t for t in tasks if t.get("id") not in remove_set]

    to_modify = params.get("tasks_to_modify")
    if to_modify:
        for partial in to_modify:
            task_id = partial.get("id")
            if not task_id:
                continue
            idx = next((i for i, t in enumerate(tasks) if t.get("id") == task_id), -1)
            if idx == -1:
                continue  # silently skip
            merged = dict(tasks[idx])
            merged.update(partial)
            tasks[idx] = merged

    updated: MochiQueue = dict(queue)
    updated["tasks"] = tasks
    for key in ("mood", "narrative", "planner_notes", "needs_replan"):
        if key in params and params[key] is not None:
            updated[key] = params[key]

    return {"queue": updated}
