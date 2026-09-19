"""Workflow memory routing carried by each ordinary run record."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, overload

from kiro_crew import platform_compat, windows_acl
from kiro_crew.atomic_write import atomic_write, fsync_dir
from kiro_crew.execution_context import (
    ExecutionContext,
    MemoryStoreRef,
    bind_session_execution,
    execution_from_record,
    stricter_memory_mode,
    validate_execution,
)


class WorkflowMemoryError(RuntimeError):
    """A run's original execution context is unavailable."""


_RUN_ID_MAX = (1 << 64) - 1
_RUN_ID_ENABLED = b"1"


def _allocator_path(path: Path) -> None:
    if platform_compat.strip_extended_length_prefix(
        path.resolve()
    ) != platform_compat.strip_extended_length_prefix(path):
        raise WorkflowMemoryError("Workflow allocator path is redirected")


def _allocator_owner(path: Path, info: os.stat_result) -> None:
    if platform_compat.IS_POSIX:
        owned = info.st_uid == platform_compat.local_user_id()
    else:
        sid = platform_compat.current_user_sid()
        try:
            owned = bool(sid) and windows_acl.describe(path).owner_sid == sid
        except windows_acl.AclUnavailable as exc:
            raise WorkflowMemoryError("Workflow allocator owner is unavailable") from exc
    if not owned:
        raise WorkflowMemoryError("Workflow allocator owner is invalid")


def _allocator_file(path: Path, fd: int) -> None:
    """Validate the opened object, not just its pre-open spelling."""
    _allocator_path(path)
    info = os.fstat(fd)
    named = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not os.path.samestat(info, named):
        raise WorkflowMemoryError("Workflow allocator file identity is invalid")
    _allocator_owner(path, info)
    # On Windows owner-only access is a DACL, not synthesized st_uid/mode bits.
    platform_compat.restrict_to_owner(path)


def _read_run_high_water(path: Path) -> int | None:
    _allocator_path(path)
    try:
        fd = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
    except FileNotFoundError:
        return None
    try:
        _allocator_file(path, fd)
        raw = os.read(fd, 257)
    finally:
        os.close(fd)
    try:
        row = json.loads(raw)
        if (
            len(raw) > 256
            or not isinstance(row, dict)
            or set(row) != {"version", "high_water"}
            or type(row["version"]) is not int
            or row["version"] != 1
            or type(row["high_water"]) is not int
            or not 0 <= row["high_water"] <= _RUN_ID_MAX
        ):
            raise ValueError("invalid high water")
        return row["high_water"]
    except (ValueError, TypeError) as exc:
        raise WorkflowMemoryError("Workflow allocator counter is invalid") from exc


def _write_run_high_water(path: Path, value: int) -> None:
    _allocator_path(path)
    atomic_write(
        path,
        json.dumps({"version": 1, "high_water": value}),
        fsync=True,
        restrict_to_owner=True,
    )
    fsync_dir(path.parent)


def allocate_run_id(floor: int = 0) -> str:
    """Burn one numeric ID without granting authority (one off-loop transaction).

    An empty lock is NOT an enablement witness. Whichever contender takes the
    lock first persists the initial counter, then writes/fsyncs the witness on
    this permanent inode, before allocating. A creator delayed before locking
    is therefore harmless. A crash before the witness can resume initialization;
    after the witness, missing/corrupt counters fail closed, never reseed.
    """
    if type(floor) is not int or not 0 <= floor <= _RUN_ID_MAX:
        raise WorkflowMemoryError("Workflow allocator recovery floor is invalid")
    try:
        from kiro_crew.workflows.store import default_workflows_dir

        root = default_workflows_dir().absolute()
        for directory in (root.parent, root):
            _allocator_path(directory)
            platform_compat.make_owner_only_dir(directory)
            _allocator_owner(directory, directory.stat())
            platform_compat.restrict_dir_to_owner(directory)
        lock = root / ".run-id.lock"
        counter = root / ".run-id.json"
        _allocator_path(lock)
        # Never truncate/replace/unlink the lock inode, including during init.
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with platform_compat.file_lock(fd, exclusive=True, required=True):
                _allocator_file(lock, fd)
                os.lseek(fd, 0, os.SEEK_SET)
                witness = os.read(fd, 2)
                if witness not in (b"", _RUN_ID_ENABLED):
                    raise WorkflowMemoryError("Workflow allocator witness is invalid")
                high = _read_run_high_water(counter)
                if high is None:
                    if witness:
                        raise WorkflowMemoryError("Workflow allocator counter is missing")
                    high = floor
                if not witness:
                    # Re-sync even an existing initial counter: its previous
                    # publisher may have died between replace and directory sync.
                    _write_run_high_water(counter, high)
                    os.lseek(fd, 0, os.SEEK_SET)
                    if os.write(fd, _RUN_ID_ENABLED) != 1:
                        raise OSError("short workflow allocator witness write")
                # A prior witness sync may have failed after its byte was
                # visible. Every successful transaction firms it up before issue.
                os.fsync(fd)
                fsync_dir(root)
                fsync_dir(root.parent)
                fsync_dir(root.parent.parent)
                high = max(high, floor)
                while high < _RUN_ID_MAX:
                    high += 1
                    candidate = f"wf_{high:06d}"
                    _write_run_high_water(counter, high)
                    return candidate
                _write_run_high_water(counter, high)
                raise WorkflowMemoryError("Workflow run ID space is exhausted")
        finally:
            os.close(fd)
    except OSError as exc:
        raise WorkflowMemoryError("Workflow allocator I/O failed") from exc


@overload
def read_binding(run_id: str, *, required: Literal[True], record=None) -> dict[str, Any]: ...


@overload
def read_binding(
    run_id: str, *, required: Literal[False] = False, record=None
) -> dict[str, Any] | None: ...


@overload
def read_binding(run_id: str, *, required: bool, record=None) -> dict[str, Any] | None: ...


def read_binding(run_id: str, *, required: bool = False, record=None) -> dict[str, Any] | None:
    """Read only the run's owner record, with no parallel identity registry."""
    if record is None:
        from kiro_crew.workflows.store import WorkflowRunStore

        path = WorkflowRunStore()._path_for(run_id)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if required:
                raise WorkflowMemoryError("Workflow run is unavailable") from None
            return None
    if not isinstance(record, dict):
        raise WorkflowMemoryError("Workflow run is malformed")
    execution = execution_from_record(record, required=required)
    if execution is None:
        return None
    return {
        "run_id": run_id,
        "memory_store": execution.store.legacy_name,
        "origin": record.get("session_key", ""),
        "memory_mode": execution.memory_mode,
        "execution_context": execution.to_record(),
    }


def capture_execution(*session_keys: str) -> ExecutionContext:
    """Capture routing before an asynchronous operation can change its parent."""
    from kiro_crew.execution_context import capture_session_execution

    items = [capture_session_execution(key) for key in dict.fromkeys(session_keys) if key]
    contexts = [item for item in items if item is not None]
    if not contexts:
        return ExecutionContext(None, MemoryStoreRef("default"), "template", "kirocrew")
    selected = contexts[0]
    if any(item.store != selected.store for item in contexts):
        raise WorkflowMemoryError("Workflow caller and delivery memory must match")
    return selected.with_mode(stricter_memory_mode(*(item.memory_mode for item in contexts)))


async def capture_admission_execution(
    context, *parents, execution_context=None, inherited_modes=(), capture_fn=None
) -> ExecutionContext:
    """Freeze live restrictions before off-loop capture, without rereading an owned parent."""
    modes = [*inherited_modes]
    live_modes = getattr(context, "_session_memory_modes", None)
    live_resolver = getattr(context, "live_memory_mode_for_session", None)
    if isinstance(live_modes, dict):
        for key in dict.fromkeys(parents):
            if not key:
                continue
            if key in live_modes:
                modes.append(live_modes[key])
            if callable(live_resolver):
                live_mode = live_resolver(key)
                if live_mode is not None:
                    modes.append(live_mode)
    inherited_mode = stricter_memory_mode("persistent", *modes)
    execution = execution_context or await asyncio.to_thread(
        capture_fn or capture_execution, *parents
    )
    mode = stricter_memory_mode(execution.memory_mode, inherited_mode)
    return execution if mode == execution.memory_mode else execution.with_mode(mode)


@dataclass(frozen=True)
class WorkflowScope:
    run_id: str
    store: str
    origin: str
    memory_mode: str = "persistent"
    execution_context: ExecutionContext | None = None

    @property
    def anchor(self) -> str:
        return f"wf-scope:{self.run_id}"

    @classmethod
    async def admit(
        cls,
        run_id,
        context,
        *parents,
        origin=None,
        expected_store=None,
        inherited_modes=(),
        execution_context=None,
    ):
        # Capture once off-loop before admission. A caller that already owns a
        # run passes its frozen carrier directly, even if its parent closed.
        execution = await capture_admission_execution(
            context, *parents, execution_context=execution_context, inherited_modes=inherited_modes
        )
        if execution.member_id is not None and context is None:
            raise WorkflowMemoryError("Member context builder is unavailable")
        if expected_store is not None and execution.store.legacy_name != expected_store:
            raise WorkflowMemoryError("Workflow caller memory changed during admission")
        return cls(
            run_id,
            execution.store.legacy_name,
            origin if origin is not None else next((key for key in parents if key), ""),
            execution.memory_mode,
            execution,
        )

    @classmethod
    async def restore(cls, run_id: str, *, record=None):
        row = await asyncio.to_thread(read_binding, run_id, required=True, record=record)
        execution = execution_from_record(row)
        return cls(
            run_id, execution.store.legacy_name, row["origin"], execution.memory_mode, execution
        )

    async def validate(self) -> None:
        if self.execution_context is None:
            raise WorkflowMemoryError("Workflow execution context is unavailable")
        try:
            await asyncio.to_thread(
                validate_execution, self.execution_context, validate_memory_files=False
            )
        except (OSError, ValueError, RuntimeError) as exc:
            raise WorkflowMemoryError(
                "Workflow memory is unavailable; Global was not used"
            ) from exc

    def worker_key(self, label: str) -> str:
        return f"wf-worker:{self.run_id}:{hashlib.sha256(label.encode()).hexdigest()}"

    async def prepare(self, context: Any, key: str) -> str:
        execution = self.execution_context
        if execution is None:
            raise WorkflowMemoryError("Workflow execution context is unavailable")
        # Runtime session records carry this same immutable snapshot. The run
        # record remains the authority on restart; no workflow anchor is stored.
        await asyncio.to_thread(bind_session_execution, key, execution)
        modes = getattr(context, "_session_memory_modes", None)
        if isinstance(modes, dict):
            modes[key] = stricter_memory_mode(execution.memory_mode, modes.get(key, "persistent"))
        return self.store

    async def prompt(self, context, key, text, *, is_new, agent, cwd, provider, resumed=False):
        await self.prepare(context, key)
        from kiro_crew.executors import run_in_embed_pool

        if context is None:
            if self.execution_context is None or self.execution_context.member_id is not None:
                raise WorkflowMemoryError("Member context builder is unavailable")
            return text
        if self.memory_mode != "temporary":
            from kiro_crew.context import prepare_store_vectors

            try:
                await prepare_store_vectors(context, self.store, session_key=key)
            except (OSError, ValueError, RuntimeError):
                # Optional learned lessons must not suppress manual essentials.
                # The prompt builder reports an unavailable member cache and
                # never substitutes Global memory. V2 preparation only opens
                # existing SQLite; empty-query lessons perform no embedding.
                pass
        full, _ = await run_in_embed_pool(
            context.build_message,
            text,
            is_new,
            key,
            agent=agent,
            project=cwd,
            memory_store=self.store,
            execution_context=self.execution_context,
            runtime_source="workflow",
            blocks_reads=self.memory_mode == "temporary",
            context_provider=provider,
            resumed=resumed,
        )
        return full


async def authorize_run(
    run_id, caller, *, owner=False, required=False, require_active=True, record=None
):
    # Caller/owner/app authorization belongs to the ordinary workflow endpoint.
    # This gate checks availability and never treats member memory as permission.
    row = await asyncio.to_thread(read_binding, run_id, required=required, record=record)
    if row is None:
        return None
    execution = execution_from_record(row)
    scope = WorkflowScope(
        run_id, execution.store.legacy_name, row["origin"], execution.memory_mode, execution
    )
    if require_active:
        await scope.validate()
    return scope


def admission_errors(function):
    import functools

    from kiro_crew.memory_stores import UnknownMemoryStore

    @functools.wraps(function)
    async def guarded(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except (WorkflowMemoryError, UnknownMemoryStore):
            message = "Workflow memory is unavailable; no Global fallback was used."
            return {
                "ok": False,
                "error": message,
                "errors": [message],
                "code": "workflow_memory_unavailable",
                "admission_rejected": True,
            }

    return guarded


class TaskSnapshotError(OSError):
    """A task snapshot was not acknowledged."""


def read_task_registry(public_path: Path) -> str:
    return public_path.read_text(encoding="utf-8")


def write_task_snapshot(public_path: Path, payload: str, *, writer: Any = atomic_write) -> None:
    rows = json.loads(payload)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise TaskSnapshotError("Task registry must contain records")
    persistent = []
    for row in rows:
        execution = execution_from_record(row, required=False)
        if execution is None or execution.memory_mode == "persistent":
            persistent.append(row)
    writer(public_path, json.dumps(persistent), fsync=True)


def read_task_snapshot(public_path: Path, *, public_payload=None) -> str:
    rows = json.loads(read_task_registry(public_path) if public_payload is None else public_payload)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Task registry must contain records")
    for row in rows:
        if row.get("private_payload"):
            raise TaskSnapshotError("Unsupported task record; Global was not used")
        execution_from_record(row, required=False)
    return json.dumps(rows)
