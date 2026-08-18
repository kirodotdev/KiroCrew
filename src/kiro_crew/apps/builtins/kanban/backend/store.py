"""Kanban task board: data model and file-backed store.

Tasks are persisted in ``~/.kiro/crew/kanban/board.json``.  Cross-process
safety uses the same advisory file-locking pattern as ``cron.py``.

The board is a flat list of :class:`TaskRecord` objects.  State transitions
(status changes, execution lifecycle) are pure functions that return a new
record — the store layer persists after each mutation.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir
from kiro_crew.platform_compat import file_lock

logger = logging.getLogger(__name__)

# ── Constants ──

_STORE_VERSION = 1


class BoardUnreadableError(RuntimeError):
    """An existing ``board.json`` could not be parsed.

    Raised instead of degrading to an empty board, because every mutation is
    read-then-write: treating an unreadable file as "no tasks" let a single bad
    read destroy the entire board on the next write. Callers should surface this
    rather than retry -- the file needs a human or a restore, and it is still
    intact on disk.
    """


TASK_STATUSES = ("backlog", "todo", "running", "done", "failed")

#: The lanes a REQUEST may put a card in. ``running`` is deliberately absent:
#: it means "an agent turn is live for this card", which only the run path can
#: make true, and only a watcher settles. A request that could set it directly
#: would mint a card the reconciler skips (it has no execution to grade) and
#: that nothing will ever move out of Running.
MANUALLY_SETTABLE_STATUSES = ("backlog", "todo", "done", "failed")

#: How a finished execution can have ended. Also the lane each outcome lands the
#: card in -- `cancelled` returns to `todo` because the work is still outstanding.
_RESULT_TO_STATUS = {"succeeded": "done", "failed": "failed", "cancelled": "todo"}
EXECUTION_RESULTS = tuple(_RESULT_TO_STATUS)


# ── Data Model ──


@dataclass
class ExecutionRecord:
    """One execution attempt of a task."""

    id: str
    started_at: float  # epoch seconds
    ended_at: float | None = None
    session_key: str | None = None
    result: str | None = None  # one of EXECUTION_RESULTS while unsettled it is None
    error: str | None = None


@dataclass
class TaskRecord:
    """A kanban board card."""

    id: str
    title: str
    description: str = ""
    prompt: str = ""
    status: str = "todo"  # one of TASK_STATUSES
    created_at: float = 0.0
    updated_at: float = 0.0
    executions: list[ExecutionRecord] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    priority: str = "medium"  # "low" | "medium" | "high"
    # True between "the card exists" and "the background namer has answered".
    # Creation returns immediately with a provisional title taken from the raw
    # prompt, so this flag is what lets the board say the name is still coming
    # rather than presenting a truncated prompt as the final title.
    refining: bool = False


# ── Pure State Transitions ──


def create_task(
    title: str,
    description: str = "",
    prompt: str = "",
    status: str = "todo",
    tags: list[str] | None = None,
    priority: str = "medium",
    refining: bool = False,
) -> TaskRecord:
    """Create a new task with a generated id and timestamps."""
    now = time.time()
    return TaskRecord(
        id=str(uuid.uuid4()),
        title=title,
        description=description,
        prompt=prompt,
        status=status if status in TASK_STATUSES else "todo",
        created_at=now,
        updated_at=now,
        tags=tags or [],
        priority=priority if priority in ("low", "medium", "high") else "medium",
        refining=refining,
    )


def move_task(task: TaskRecord, new_status: str) -> TaskRecord:
    """Move a task to a new column. Returns a new record."""
    if new_status not in TASK_STATUSES:
        raise ValueError(f"Invalid status: {new_status!r}")
    return TaskRecord(
        id=task.id,
        title=task.title,
        description=task.description,
        prompt=task.prompt,
        status=new_status,
        created_at=task.created_at,
        updated_at=time.time(),
        executions=task.executions,
        tags=task.tags,
        priority=task.priority,
        refining=task.refining,
    )


def start_execution(task: TaskRecord) -> tuple[TaskRecord, ExecutionRecord]:
    """Begin a new execution. Returns (updated task, new execution record)."""
    now = time.time()
    execution = ExecutionRecord(id=str(uuid.uuid4()), started_at=now)
    new_task = TaskRecord(
        id=task.id,
        title=task.title,
        description=task.description,
        prompt=task.prompt,
        status="running",
        created_at=task.created_at,
        updated_at=now,
        executions=[*task.executions, execution],
        tags=task.tags,
        priority=task.priority,
        refining=task.refining,
    )
    return new_task, execution


def settle_execution(
    task: TaskRecord,
    execution_id: str,
    outcome: str,
    error: str | None = None,
) -> TaskRecord:
    """Settle an execution: mark it done/failed/cancelled.

    The execution row is always written — a finished run's own outcome is a fact
    about that run and stays recorded. ``task.status`` is only moved when this is
    the task's LATEST unsettled execution: a watcher for a superseded run (the
    card was settled by hand, then started again) would otherwise land its stale
    outcome on top of the new run's ``running``, so the board would show a
    finished state for work still in flight.
    """
    now = time.time()
    new_status = _RESULT_TO_STATUS.get(outcome, "failed")

    # The newest execution with no result yet is the one the card's status belongs
    # to. Scanning from the end makes the common case (settling the run that just
    # finished) the first hit.
    latest_unsettled = next(
        (ex.id for ex in reversed(task.executions) if not ex.result),
        None,
    )
    owns_status = latest_unsettled is None or latest_unsettled == execution_id

    new_executions = []
    for ex in task.executions:
        if ex.id == execution_id:
            new_executions.append(
                ExecutionRecord(
                    id=ex.id,
                    started_at=ex.started_at,
                    ended_at=now,
                    session_key=ex.session_key,
                    result=outcome,
                    error=error,
                )
            )
        else:
            new_executions.append(ex)

    return TaskRecord(
        id=task.id,
        title=task.title,
        description=task.description,
        prompt=task.prompt,
        status=new_status if owns_status else task.status,
        created_at=task.created_at,
        updated_at=now,
        executions=new_executions,
        tags=task.tags,
        priority=task.priority,
        refining=task.refining,
    )


def attach_session_key(task: TaskRecord, execution_id: str, session_key: str) -> TaskRecord:
    """Record which session is running an execution."""
    new_executions = []
    for ex in task.executions:
        if ex.id == execution_id:
            new_executions.append(
                ExecutionRecord(
                    id=ex.id,
                    started_at=ex.started_at,
                    ended_at=ex.ended_at,
                    session_key=session_key,
                    result=ex.result,
                    error=ex.error,
                )
            )
        else:
            new_executions.append(ex)

    return TaskRecord(
        id=task.id,
        title=task.title,
        description=task.description,
        prompt=task.prompt,
        status=task.status,
        created_at=task.created_at,
        updated_at=time.time(),
        executions=new_executions,
        tags=task.tags,
        priority=task.priority,
        refining=task.refining,
    )


# ── Serialization ──


def _task_to_dict(task: TaskRecord) -> dict[str, Any]:
    """Serialize a task to a JSON-safe dict."""
    return asdict(task)


def _task_from_dict(raw: dict[str, Any]) -> TaskRecord:
    """Deserialize one task from a dict.

    Raises :class:`BoardUnreadableError` on a record this cannot read. Skipping
    it instead was silent per-card data loss: every mutation is read-then-write,
    so a record missing an id or a title -- or carrying a field of the wrong type
    -- was dropped on load and then erased from disk, with its whole execution
    history, by the next unrelated move or edit. Refusing the read leaves the
    file untouched so the record can be repaired.

    Types are checked, not just truthiness. The board file is hand-editable and
    these values are handed to the UI as-is, where a non-string title or tag
    reaches ``.toLowerCase()`` in the search filter and takes the whole board
    down with it -- so a wrong type is corruption to refuse here, not something
    to coerce and pass on.
    """
    task_id = raw.get("id", "")
    title = raw.get("title", "")
    if not isinstance(task_id, str) or not isinstance(title, str) or not task_id or not title:
        logger.error("kanban: refusing to read board.json: a task has no usable id or title")
        raise BoardUnreadableError("board.json contains a task with no usable id or title")

    try:
        # Normalize status
        status = raw.get("status", "todo")
        if status not in TASK_STATUSES:
            status = "todo"

        # Parse executions. A malformed entry is refused rather than skipped:
        # dropping one would erase that run from the history on the next write.
        # EVERY field is type-checked, not just the id -- these values are
        # rendered directly by the task detail panel, so an object where a
        # string belongs reaches React as a child and takes the page down.
        executions = []
        for ex_raw in raw.get("executions", []):
            if not isinstance(ex_raw, dict):
                raise BoardUnreadableError("board.json has an execution that is not an object")
            ex_id = ex_raw.get("id")
            if not isinstance(ex_id, str) or not ex_id:
                raise BoardUnreadableError("board.json has an execution with no usable id")
            started_at = ex_raw.get("started_at", 0)
            ended_at = ex_raw.get("ended_at")
            result = ex_raw.get("result")
            error = ex_raw.get("error")
            session_key = ex_raw.get("session_key")
            if not isinstance(started_at, (int, float)) or isinstance(started_at, bool):
                raise BoardUnreadableError("board.json has an execution with a non-numeric start")
            if ended_at is not None and (
                not isinstance(ended_at, (int, float)) or isinstance(ended_at, bool)
            ):
                raise BoardUnreadableError("board.json has an execution with a non-numeric end")
            if result is not None and result not in EXECUTION_RESULTS:
                raise BoardUnreadableError("board.json has an execution with an unknown result")
            if error is not None and not isinstance(error, str):
                raise BoardUnreadableError("board.json has an execution with a non-string error")
            if session_key is not None and not isinstance(session_key, str):
                raise BoardUnreadableError("board.json has an execution with a non-string session")
            executions.append(
                ExecutionRecord(
                    id=ex_id,
                    started_at=float(started_at),
                    ended_at=float(ended_at) if ended_at is not None else None,
                    session_key=session_key,
                    result=result,
                    error=error,
                )
            )

        tags = raw.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise BoardUnreadableError("board.json has a tags value that is not a list of strings")

        return TaskRecord(
            id=task_id,
            title=title,
            description=str(raw.get("description", "")),
            prompt=str(raw.get("prompt", "")),
            status=status,
            created_at=float(raw.get("created_at", 0)),
            updated_at=float(raw.get("updated_at", 0)),
            executions=executions,
            tags=tags,
            priority=(
                raw.get("priority", "medium")
                if raw.get("priority") in ("low", "medium", "high")
                else "medium"
            ),
            # Only a literal true means "still being named". A board file written
            # by an older build has no such key, and a non-bool value is not
            # permission to render a card as perpetually refining.
            refining=raw.get("refining") is True,
        )
    except BoardUnreadableError:
        logger.error("kanban: refusing to read board.json: task %s is invalid", task_id)
        raise
    except (TypeError, ValueError, KeyError) as exc:
        logger.error("kanban: refusing to read board.json: task %s is invalid: %s", task_id, exc)
        raise BoardUnreadableError(f"board.json contains an unreadable task: {exc}") from exc


# ── File Store ──


class KanbanStore:
    """File-backed kanban board store with advisory file locking.

    Storage layout::

        <data_home>/kanban/board.json   — the board state
        <data_home>/kanban/.lock        — advisory lock file
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = (root or config_dir() / "kanban").expanduser()
        self._root.mkdir(parents=True, exist_ok=True)
        self._board_path = self._root / "board.json"
        self._lock_path = self._root / ".lock"

    # ── Public API ──

    def load(self) -> list[TaskRecord]:
        """Load all tasks from disk."""
        with self._locked():
            return self._read()

    def get_task(self, task_id: str) -> TaskRecord | None:
        """Load a single task by id."""
        tasks = self.load()
        for task in tasks:
            if task.id == task_id:
                return task
        return None

    def update_task(self, task_id: str, updater: Any) -> TaskRecord | None:
        """Atomically load, apply updater function, and save.

        ``updater`` is called with the found TaskRecord and must return
        the replacement TaskRecord (or None to delete).
        """
        with self._locked():
            tasks = self._read()
            result = None
            new_tasks = []
            for task in tasks:
                if task.id == task_id:
                    updated = updater(task)
                    if updated is not None:
                        new_tasks.append(updated)
                        result = updated
                else:
                    new_tasks.append(task)
            self._write(new_tasks)
            return result

    def add_task(self, task: TaskRecord) -> None:
        """Append a task to the board."""
        with self._locked():
            tasks = self._read()
            tasks.append(task)
            self._write(tasks)

    def delete_task(self, task_id: str) -> bool:
        """Remove a task by id. Returns True if found."""
        with self._locked():
            tasks = self._read()
            new_tasks = [t for t in tasks if t.id != task_id]
            if len(new_tasks) == len(tasks):
                return False
            self._write(new_tasks)
            return True

    # ── Internal ──

    def _read(self) -> list[TaskRecord]:
        """Read and parse the board file (must hold lock).

        Raises :class:`BoardUnreadableError` when a board file EXISTS but cannot
        be parsed. Returning an empty list instead was silent total data loss:
        every mutation is read-then-write, so one malformed or transiently
        unreadable ``board.json`` (a partial write, a permissions blip, an EIO)
        made the next move or edit replace the whole board with nothing. Refusing
        the read leaves the file untouched and recoverable.

        A board that does not exist yet is genuinely empty and still returns [].
        """
        if not self._board_path.exists():
            return []
        try:
            raw = json.loads(self._board_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("kanban: refusing to overwrite unreadable board.json: %s", exc)
            raise BoardUnreadableError(f"board.json could not be read: {exc}") from exc

        if not isinstance(raw, dict):
            logger.error("kanban: refusing to overwrite board.json: top level is not an object")
            raise BoardUnreadableError("board.json is not a JSON object")

        version = raw.get("version", 1)
        if version != _STORE_VERSION:
            logger.warning("kanban: unknown store version %s, loading best-effort", version)

        tasks: list[TaskRecord] = []
        raw_tasks = raw.get("tasks", [])
        # A non-list `tasks` (including JSON null) is a corrupt board, not an
        # empty one: iterating it raises, and treating it as empty would let the
        # next mutation persist that emptiness over every task. Same for a
        # non-object entry, which `_task_from_dict` cannot read.
        if not isinstance(raw_tasks, list):
            logger.error("kanban: refusing to read board.json: 'tasks' is not an array")
            raise BoardUnreadableError("board.json 'tasks' is not an array")
        for item in raw_tasks:
            if not isinstance(item, dict):
                logger.error("kanban: refusing to read board.json: a task entry is not an object")
                raise BoardUnreadableError("board.json contains a task that is not an object")
            tasks.append(_task_from_dict(item))
        return tasks

    def _write(self, tasks: list[TaskRecord]) -> None:
        """Write the board to disk (must hold lock).

        Atomic because a truncating in-place write that is interrupted leaves
        invalid JSON, which ``_read`` reports as an empty board — and the next
        mutation would then persist that emptiness over every task.
        """
        payload = {
            "version": _STORE_VERSION,
            "tasks": [_task_to_dict(t) for t in tasks],
        }
        content = json.dumps(payload, indent=2, ensure_ascii=False)
        atomic_write(self._board_path, content)

    def _locked(self):
        """Context manager for advisory file lock."""
        return _file_lock(self._lock_path)


# ── File Locking ──


@contextlib.contextmanager
def _file_lock(lock_path: Path):
    """Serialise board writes across processes.

    Delegates to platform_compat rather than calling fcntl directly: fcntl is
    POSIX-only, so importing it at module scope makes the whole app unimportable
    on Windows. The helper carries the msvcrt equivalent and fails closed if the
    lock cannot be taken, rather than entering the critical section unserialized.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # "r+", never "w": msvcrt.locking needs write access on the fd, but "w"
    # TRUNCATES on every acquire, and truncating the file whose byte-0 lock
    # another handle already holds makes the Windows acquire fail and then spin
    # to its 300s ceiling. Create-if-absent, then open without truncating.
    if not lock_path.exists():
        lock_path.touch()
    fd = lock_path.open("r+")
    try:
        with file_lock(fd.fileno()):
            yield
    finally:
        fd.close()
