"""Trusted workflow execution identity, separate from scripts and run snapshots.

The binding is published under the existing read-only member binding root.
Private payloads live under the existing hidden memory root. Neither a script's
session label nor editable run metadata can mint or replace that authority.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat, windows_acl
from kiro_crew.atomic_write import atomic_write, fsync_dir
from kiro_crew.config.paths import config_dir
from kiro_crew.member_memory_auth import (
    _publish_private_binding_dir,
    bind_private_session_store,
    private_memory_store_for_session,
    require_private_memory_execution,
)
from kiro_crew.memory_stores import require_memory_store
from kiro_crew.session_pid_sig import _read_regular_nofollow


class WorkflowMemoryError(RuntimeError):
    """The workflow cannot safely continue under its original identity."""


def binding_path(run_id: str) -> Path:
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    path = config_dir().resolve() / "member-memory-bindings" / "workflows" / digest / "memory.json"
    if platform_compat.strip_extended_length_prefix(
        path.resolve()
    ) != platform_compat.strip_extended_length_prefix(path):
        raise WorkflowMemoryError("Workflow binding path is redirected")
    return path


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
        root = config_dir().resolve() / "member-memory-bindings" / "workflows"
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
                    binding = binding_path(candidate)
                    legacy = root / ".reserved" / binding.parent.name
                    _allocator_path(legacy)
                    # A directory without memory.json still proves prior use.
                    if binding.parent.exists() or legacy.exists():
                        continue
                    _write_run_high_water(counter, high)
                    return candidate
                _write_run_high_water(counter, high)
                raise WorkflowMemoryError("Workflow run ID space is exhausted")
        finally:
            os.close(fd)
    except OSError as exc:
        raise WorkflowMemoryError("Workflow allocator I/O failed") from exc


def read_binding(run_id: str, *, required: bool = False) -> dict[str, Any] | None:
    path = binding_path(run_id)
    raw = _read_regular_nofollow(path)
    if raw is None:
        if required or path.parent.exists():
            raise WorkflowMemoryError("The protected workflow binding is missing or unreadable")
        return None
    try:
        row = json.loads(raw)
        if (
            not isinstance(row, dict)
            or row.get("version") != 1
            or row.get("run_id") != run_id
            or not isinstance(row.get("memory_store"), str)
            or row["memory_store"] == "default"
            or not isinstance(row.get("origin"), str)
            or (
                "memory_mode" in row
                and row["memory_mode"] not in ("persistent", "incognito", "temporary")
            )
        ):
            raise ValueError("invalid workflow identity")
        return row
    except (ValueError, TypeError) as exc:
        raise WorkflowMemoryError("The protected workflow binding is invalid") from exc


def publish_binding(
    run_id: str, store: str, origin: str, *, memory_mode: str = "persistent"
) -> None:
    """Called only after trusted gateway admission, never from an HTTP body."""
    if memory_mode not in {"persistent", "incognito", "temporary"}:
        raise WorkflowMemoryError("Workflow memory mode is unavailable")
    path = binding_path(run_id)
    row = {
        "version": 1,
        "run_id": run_id,
        "memory_store": store,
        "origin": origin,
        "memory_mode": memory_mode,
    }
    for directory in (path.parent.parent.parent, path.parent.parent):
        platform_compat.make_owner_only_dir(directory)
        platform_compat.restrict_dir_to_owner(directory)
    existing = read_binding(run_id)
    if existing is not None:
        if existing != row:
            raise WorkflowMemoryError("Workflow execution identity is immutable")
        return
    stage = Path(tempfile.mkdtemp(prefix=".workflow-", dir=path.parent.parent))
    try:
        platform_compat.restrict_dir_to_owner(stage)
        atomic_write(stage / path.name, json.dumps(row), fsync=True, restrict_to_owner=True)
        fsync_dir(stage)
        try:
            _publish_private_binding_dir(stage, path.parent)
        except FileExistsError:
            if read_binding(run_id, required=True) != row:
                raise WorkflowMemoryError("Workflow execution identity is immutable")
        else:
            fsync_dir(path.parent.parent)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def private_payload_path(run_id: str) -> Path:
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    path = config_dir().resolve() / "memory_stores" / ".workflow-runs" / f"{digest}.json"
    if platform_compat.strip_extended_length_prefix(
        path.resolve()
    ) != platform_compat.strip_extended_length_prefix(path):
        raise WorkflowMemoryError("Private workflow payload path is redirected")
    return path


@dataclass(frozen=True)
class WorkflowScope:
    run_id: str
    store: str
    origin: str
    memory_mode: str | None = "persistent"

    @property
    def anchor(self) -> str:
        return f"wf-scope:{self.run_id}"

    @classmethod
    async def admit(
        cls,
        run_id: str,
        context: Any,
        *parents: str,
        origin: str | None = None,
        expected_store: str | None = None,
        inherited_modes: tuple[str, ...] = (),
    ) -> WorkflowScope:
        """Freeze verified gateway parent selections before scheduling a run."""
        from kiro_crew.messaging.privacy_mode import strictest

        resolver = (
            getattr(context, "memory_mode_for_session", None)
            if isinstance(getattr(context, "_session_memory_modes", None), dict)
            else None
        )
        try:
            modes = list(inherited_modes)
            if resolver:
                modes.extend([await resolver(key) for key in dict.fromkeys(parents) if key])
            if any(mode not in ("persistent", "incognito", "temporary") for mode in modes):
                raise ValueError("unknown mode")
        except (OSError, ValueError) as exc:
            raise WorkflowMemoryError("Workflow memory mode is unavailable") from exc
        memory_mode = strictest(modes) or "persistent"
        stores = [
            await asyncio.to_thread(private_memory_store_for_session, key)
            for key in dict.fromkeys(key for key in parents if key)
        ]
        if len(set(stores)) > 1:
            raise WorkflowMemoryError("Workflow caller and delivery memory must match")
        store = stores[0] if stores else ""
        if expected_store is not None and store != expected_store:
            raise WorkflowMemoryError("Workflow caller memory changed during admission")
        scope = cls(
            run_id,
            store,
            origin if origin is not None else next((key for key in parents if key), ""),
            memory_mode,
        )
        existing = await asyncio.to_thread(read_binding, run_id)
        if existing is not None:
            restored = await cls.restore(run_id)
            if restored != scope:
                raise WorkflowMemoryError("Workflow execution identity is immutable")
            return restored
        if store:
            if context is None or getattr(context, "conversation_log", None) is None:
                raise WorkflowMemoryError("Private workflow context is unavailable")
            await asyncio.to_thread(require_private_memory_execution, session_key=scope.anchor)
            await asyncio.to_thread(require_memory_store, store)
            await asyncio.to_thread(bind_private_session_store, scope.anchor, store)
            await asyncio.to_thread(
                context.conversation_log.update_metadata, scope.anchor, {"memory_store": store}
            )
        await asyncio.to_thread(
            publish_binding, run_id, store, scope.origin, memory_mode=memory_mode
        )
        await scope.validate()
        return scope

    @classmethod
    async def restore(cls, run_id: str) -> WorkflowScope:
        row = await asyncio.to_thread(read_binding, run_id, required=True)
        assert row is not None
        scope = cls(run_id, row["memory_store"], row["origin"], row.get("memory_mode"))
        await scope.validate()
        return scope

    async def validate(self) -> None:
        try:
            row = await asyncio.to_thread(read_binding, self.run_id, required=True)
            expected = {
                "version": 1,
                "run_id": self.run_id,
                "memory_store": self.store,
                "origin": self.origin,
            }
            if self.memory_mode is not None:
                expected["memory_mode"] = self.memory_mode
            if row != expected:
                raise WorkflowMemoryError("Workflow execution identity changed")
            if self.store:
                await asyncio.to_thread(require_memory_store, self.store)
                await asyncio.to_thread(require_private_memory_execution, session_key=self.anchor)
                actual = await asyncio.to_thread(private_memory_store_for_session, self.anchor)
                if actual != self.store:
                    raise WorkflowMemoryError("Workflow anchor no longer matches its store")
        except Exception as exc:
            raise WorkflowMemoryError(
                "Workflow memory is unavailable; Global V1 was not used"
            ) from exc

    def worker_key(self, label: str) -> str:
        # Hash only a local label. It is not an existing chat/session capability.
        digest = hashlib.sha256(label.encode("utf-8")).hexdigest()
        return f"wf-worker:{self.run_id}:{digest}"

    async def prepare(self, context: Any, key: str) -> str:
        await self.validate()
        if self.memory_mode not in ("persistent", "incognito", "temporary"):
            raise WorkflowMemoryError("Workflow memory mode is unavailable")
        modes = getattr(context, "_session_memory_modes", None)
        if isinstance(modes, dict):
            from kiro_crew.messaging.privacy_mode import strictest

            modes[self.anchor] = self.memory_mode
            modes[key] = strictest((self.memory_mode, modes.get(key, "persistent"))) or "persistent"
        if self.store:
            from kiro_crew.context import inherit_session_memory

            actual = await inherit_session_memory(context, self.anchor, key)
            if actual != self.store:
                raise WorkflowMemoryError("Workflow worker memory does not match the run")
        elif await asyncio.to_thread(private_memory_store_for_session, key):
            raise WorkflowMemoryError("Global workflow cannot acquire a private conversation")
        log = getattr(context, "conversation_log", None)
        if log is not None:
            await asyncio.to_thread(log.update_metadata, key, {"memory_mode": self.memory_mode})
        return self.store

    async def prompt(
        self,
        context: Any,
        key: str,
        text: str,
        *,
        is_new: bool,
        agent: str | None,
        cwd: str | None,
        provider: Any,
        resumed: bool = False,
    ) -> str:
        await self.prepare(context, key)
        if not self.store:
            return text
        from kiro_crew.context import prepare_store_vectors
        from kiro_crew.executors import run_in_embed_pool

        if self.memory_mode != "temporary":
            await prepare_store_vectors(context, self.store, session_key=key)
        full, _ = await run_in_embed_pool(
            context.build_message,
            text,
            is_new,
            key,
            agent=agent,
            project=cwd,
            memory_store=self.store,
            runtime_source="workflow",
            blocks_reads=self.memory_mode == "temporary",
            context_provider=provider,
            resumed=resumed,
        )
        await self.validate()
        return full


async def authorize_run(
    run_id: str,
    caller: str,
    *,
    owner: bool = False,
    required: bool = False,
    require_active: bool = True,
) -> WorkflowScope | None:
    """Authorize before reading run content; absence is only legacy V1."""
    row = await asyncio.to_thread(read_binding, run_id, required=required)
    caller_store = (
        "" if owner else await asyncio.to_thread(private_memory_store_for_session, caller)
    )
    if row is None:
        if caller_store:
            raise WorkflowMemoryError("A private caller cannot access another workflow")
        return None
    scope = WorkflowScope(run_id, row["memory_store"], row["origin"], row.get("memory_mode"))
    if require_active or not owner:
        await scope.validate()
    if not owner and scope.store != caller_store:
        raise WorkflowMemoryError("The workflow belongs to another memory scope")
    return scope


def admission_errors(function):
    """Keep expected binding refusals in the workflow service response contract."""
    import functools

    from kiro_crew.memory_stores import UnknownMemoryStore

    @functools.wraps(function)
    async def guarded(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except (WorkflowMemoryError, UnknownMemoryStore):
            message = "Workflow memory is unavailable; no Global V1 fallback was used."
            return {
                "ok": False,
                "error": message,
                "errors": [message],
                "code": "workflow_memory_unavailable",
                "admission_rejected": True,
            }

    return private_task_operation(guarded)


def task_snapshot_path(public_path: Path) -> Path:
    """Private task state never lives in the agent-readable execution directory."""
    digest = hashlib.sha256(str(public_path.resolve()).encode("utf-8")).hexdigest()
    path = config_dir().resolve() / "memory_stores" / ".task-runs" / f"{digest}.json"
    if platform_compat.strip_extended_length_prefix(
        path.resolve()
    ) != platform_compat.strip_extended_length_prefix(path):
        raise WorkflowMemoryError("Private task snapshot path is redirected")
    return path


def _private_task_rows(path: Path) -> tuple[dict[str, dict], list[dict] | None]:
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, None
    public = None
    if isinstance(rows, dict):
        if type(rows.get("version")) is not int or rows["version"] != 1:
            raise WorkflowMemoryError("Private task snapshot version is invalid")
        public = rows.get("public")
        if not isinstance(public, list) or any(not isinstance(row, dict) for row in public):
            raise WorkflowMemoryError("Private task snapshot directory is invalid")
        rows = rows.get("private")
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("task_id"), str) for row in rows
    ):
        raise WorkflowMemoryError("Private task snapshot is invalid")
    return {row["task_id"]: row for row in rows}, public


def read_task_registry(public_path: Path, *, strict: bool = False) -> str:
    """Prefer the committed directory, even if its public projection is missing.

    Legacy sidecars contain historical rows, not a directory. Never discover
    tasks by enumerating those rows. An unreadable sidecar retains the existing
    public-reference/unavailable recovery path.
    """
    try:
        _, public = _private_task_rows(task_snapshot_path(public_path))
    except (WorkflowMemoryError, ValueError, OSError):
        if strict:
            raise TaskSnapshotError(
                "Task snapshot recovery incomplete; restart after storage recovers"
            ) from None
        public = None
    if public is not None:
        return json.dumps(public)
    return public_path.read_text(encoding="utf-8")


class TaskSnapshotError(OSError):
    """A snapshot was not acknowledged; it may have committed before I/O failed."""


def _task_private_binding(row: dict) -> str | None:
    from kiro_crew.member_memory_auth import read_private_session_store

    task_id = row.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        return None
    return read_private_session_store(f"taskrunner:{task_id}:runtime")


def write_task_snapshot(public_path: Path, payload: str, *, writer: Any = atomic_write) -> None:
    """Commit directory and payload together, then refresh the public projection.

    Called under TaskRunner's existing snapshot sequence/write lock. A failure
    after the hidden replace is an unacknowledged complete commit, not a rollback.
    Missing authority never converts an already-private row into public state.
    """
    private_path = task_snapshot_path(public_path)
    previous, committed_public = _private_task_rows(private_path)
    private, public = [], []
    for row in json.loads(payload):
        if row.get("private_payload") is True:
            # An unavailable run is a reference, never a replacement payload.
            public.append({"task_id": row["task_id"], "private_payload": True})
        elif _task_private_binding(row) is not None or row.get("task_id") in previous:
            private.append(row)
            public.append({"task_id": row["task_id"], "private_payload": True})
        else:
            public.append(row)
    if private or committed_public is not None:
        platform_compat.make_owner_only_dir(private_path.parent)
        platform_compat.restrict_dir_to_owner(private_path.parent)
        retained = dict(previous)
        retained.update({row["task_id"]: row for row in private})
        writer(
            private_path,
            json.dumps({"version": 1, "public": public, "private": list(retained.values())}),
            fsync=True,
            restrict_to_owner=True,
        )
    writer(public_path, json.dumps(public), fsync=True)


def read_task_snapshot(
    public_path: Path,
    *,
    public_payload: str | None = None,
    preserve_unavailable: bool = False,
    private_task_ids: set[str] | None = None,
) -> str:
    """Hydrate authorized rows; optionally retain unavailable rows as references.

    Report private provenance separately so a caller can preserve a reference if
    constructing the hydrated task fails. This set grants no execution authority.
    """
    rows = json.loads(read_task_registry(public_path) if public_payload is None else public_payload)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Task registry must contain a list of records")
    try:
        private, committed_public = _private_task_rows(task_snapshot_path(public_path))
        if committed_public is not None:
            rows = committed_public
    except (WorkflowMemoryError, ValueError, OSError) as exc:
        if not preserve_unavailable:
            raise
        logging.getLogger("kiro_crew.taskrunner").warning(
            "Private task snapshot unavailable (%s)", type(exc).__name__
        )
        private = {}
    hydrated = []
    for row in rows:
        task_id = row.get("task_id")
        try:
            if (
                row.get("private_payload") is True
                or task_id in private
                or _task_private_binding(row) is not None
            ):
                if private_task_ids is not None:
                    private_task_ids.add(task_id)
                original = private.get(task_id)
                if original is None or _task_private_binding(original) is None:
                    raise WorkflowMemoryError("Private task runtime binding is unavailable")
                hydrated.append(original)
            else:
                hydrated.append(row)
        except (WorkflowMemoryError, ValueError, OSError) as exc:
            if not preserve_unavailable:
                raise
            hydrated.append({"task_id": task_id, "private_payload": True})
            logging.getLogger("kiro_crew.taskrunner").warning(
                "Private task snapshot row unavailable (%s)", type(exc).__name__
            )
    return json.dumps(hydrated)


# Task workers inherit the creator's context, including after the HTTP request
# returns. Only task diagnostics are filtered; owner and public tasks stay intact.

_PRIVATE_DIAGNOSTIC_SCOPE = contextvars.ContextVar("workflow_private_diagnostics", default="")


class _PrivateTaskDiagnostics(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        scope = _PRIVATE_DIAGNOSTIC_SCOPE.get()
        if scope:
            failure = (
                record.exc_info[0].__name__
                if record.exc_info and record.exc_info[0]
                else record.levelname
            )
            record.msg = "Private task %s: %s"
            record.args = (scope, failure)
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


_TASK_DIAGNOSTIC_FILTER = _PrivateTaskDiagnostics()
for _logger_name in (
    "taskrunner",
    "task_executor",
    "task_planner",
    "task_reporter",
    "git_coord",
    "workflows.service",
    "workflows.agent_exec",
    "workflows.agent_pool",
    "workflows.runner",
):
    logging.getLogger(f"kiro_crew.{_logger_name}").addFilter(_TASK_DIAGNOSTIC_FILTER)


@contextmanager
def private_task_diagnostics(session_key: str):
    scope = hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:12]
    token = _PRIVATE_DIAGNOSTIC_SCOPE.set(scope)
    try:
        yield
    finally:
        _PRIVATE_DIAGNOSTIC_SCOPE.reset(token)


def private_task_operation(function):
    """Scope task logs from protected identity, never an environment flag."""
    import functools

    from kiro_crew.member_memory_auth import read_private_session_store

    @functools.wraps(function)
    async def wrapped(self, *args, **kwargs):
        origin = (
            kwargs.get("session_key") or kwargs.get("author") or kwargs.get("caller_session", "")
        )
        task_id = kwargs.get("task_id")
        if not task_id and function.__name__ in {"execute_plan", "retry_from_task"} and args:
            task_id = args[0]
        key = f"taskrunner:{task_id}:runtime" if task_id else origin
        if not key and function.__name__ == "rerun_subtree" and args:
            try:
                row = await asyncio.to_thread(read_binding, args[0])
            except WorkflowMemoryError:
                row = {"memory_store": "unavailable"}
            if row is not None and row["memory_store"]:
                key = f"wf-scope:{args[0]}"
        private = False
        if key:
            # Missing/degraded identity must not expose a failure's private text.
            try:
                private = await asyncio.to_thread(read_private_session_store, key) is not None
            except Exception:
                private = True
        if private:
            with private_task_diagnostics(key):
                return await function(self, *args, **kwargs)
        return await function(self, *args, **kwargs)

    return wrapped
