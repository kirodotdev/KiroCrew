"""A run's folder and its state.

Creating the folder, the execution context it records, ``state.json`` read and written
under the per-agent locks, the tombstone reads, retention promotion, result streaming,
and a session's memory mode.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import weakref
from enum import Enum
from pathlib import Path
from typing import Callable

from kiro_crew.atomic_write import atomic_write, read_bytes_with_retry
from kiro_crew.execution_context import ExecutionContext
from kiro_crew.owner_only_files import ensure_directory
from kiro_crew.subagent_persistence_layout import (
    _agent_dir,
    _atomic_write,
    _subagents_dir,
    logger,
)

_LIVE_RUN_STATES: dict[tuple[str, str], dict] = {}


def _live_run_key(agent_id: str) -> tuple[str, str]:
    return str(_subagents_dir()), agent_id


class RetentionPromotionResult(Enum):
    """Outcome of the non-blocking promotion transaction."""

    PROMOTED = "promoted"
    RETRYABLE = "retryable"


# Named by the path callers import it from: pickle and introspection resolve
# ``kiro_crew.subagent_persistence.RetentionPromotionResult``, not this module.
RetentionPromotionResult.__module__ = "kiro_crew.subagent_persistence"


def write_run_agent(agent_id: str, agent: str | None, *, kind: str = "template") -> None:
    from dataclasses import replace

    execution = read_run_execution(agent_id)
    if (
        not isinstance(agent, str)
        or kind not in ("template", "member")
        or (kind == "member" and not agent)
    ):
        raise ValueError("resume_failed: effective agent template is invalid")
    if kind == "template":
        update_execution_context(
            agent_id,
            replace(execution, template_id=agent or execution.template_id),
            expected=execution,
        )
    elif execution.selection_kind != "member" or agent != execution.selection_name:
        raise ValueError("resume_failed: selected member is unavailable")


# Named by the path callers import it from, as the run coordinator binds it:
# ``kiro_crew.subagent_persistence.write_run_agent``.
write_run_agent.__module__ = "kiro_crew.subagent_persistence"


def read_run_agent_selection(agent_id: str) -> tuple[str, str]:
    execution = read_run_execution(agent_id)
    return execution.selection_kind, (
        execution.selection_name if execution.selection_kind == "member" else execution.template_id
    )


# ── create ───────────────────────────────────────────────────────────


def create_agent_folder(
    agent_id: str,
    *,
    task="",
    agent="",
    parent_session="",
    max_turns=0,
    context_groups="",
    delegation=None,
    memory_store="",
    memory_mode="persistent",
    app="",
    execution_context=None,
    backend=None,
) -> Path:
    from kiro_crew.execution_context import ExecutionContext, execution_for_store

    execution = execution_context or execution_for_store(
        memory_store, memory_mode=memory_mode, app=app, template_id=agent or ""
    )
    if not isinstance(execution, ExecutionContext):
        raise ValueError("memory_unavailable: invalid execution context")
    previous = read_state(agent_id)
    if previous is not None:
        from kiro_crew.execution_context import execution_from_record

        original = execution_from_record(previous)
        if execution_context is not None and execution.store != original.store:
            raise ValueError("memory_unavailable: run identity is immutable")
        execution = original.with_mode(memory_mode)
        if execution.memory_mode != original.memory_mode:
            update_execution_context(agent_id, execution, expected=original)
    execution = execution.with_mode(memory_mode)
    state = {
        "id": agent_id,
        "task": task,
        "agent": agent,
        "parent_session": parent_session,
        "started": time.time(),
        "max_turns": max_turns,
        # No run status: no ending writes one back here, so a status set at
        # creation would read as running forever. How a run ended is the
        # tombstone's (``classify_persisted_ending``).
        "pid": None,
        "turns": 0,
        "last_tool": "",
        "context_groups": context_groups,
        "delegation": dict(delegation or {}),
        # The per-spawn backend, so a continuation after a restart resumes the
        # conversation on the harness that holds it. Absent = the default.
        **({"backend": backend} if isinstance(backend, str) else {}),
        "execution_context": execution.to_record(),
        "memory_store": execution.store.legacy_name,
        "memory_mode": execution.memory_mode,
        "app": execution.app,
        "updated_at": time.time(),
    }
    d = _agent_dir(agent_id)
    if execution.memory_mode != "persistent":
        _LIVE_RUN_STATES[_live_run_key(agent_id)] = state
    else:
        ensure_directory(d)
        _atomic_write(d / "state.json", state)
    return d


def read_run_execution(agent_id: str, *, state=...) -> "ExecutionContext":
    from kiro_crew.execution_context import execution_for_store, execution_from_record

    if state is ...:
        state = read_state(agent_id)
    if state is None:
        raise ValueError("memory_unavailable: run record is unavailable")
    execution = execution_from_record(state, required=False)
    if execution is not None:
        return execution
    # Existing V1 runs retain their ordinary state; V2 never infers identity.
    from kiro_crew.memory_stores import memory_store_version

    store = state.get("memory_store", "")
    if memory_store_version(store) == 2:
        raise ValueError("memory_unavailable: run has no canonical execution")
    legacy = state
    if "memory_binding_version" in state:
        # The old writer used version 2 for V1 runs too. Its ordinary app and
        # retention fields live only in this existing sidecar; defaults would
        # turn an app-owned/restricted run into a personal/persistent run.
        _agent_dir(agent_id)
        path = (
            _subagents_dir().parent.resolve() / "member-memory-bindings" / agent_id / "memory.json"
        )
        if state["memory_binding_version"] != 2 or path.resolve() != path:
            raise ValueError("memory_unavailable: legacy V1 execution is invalid")
        try:
            legacy = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as exc:
            raise ValueError("memory_unavailable: legacy V1 execution is unreadable") from exc
        if (
            not isinstance(legacy, dict)
            or legacy.get("version") != 2
            or legacy.get("memory_store") != store
            or legacy.get("memory_mode") not in ("persistent", "incognito", "temporary")
            or not isinstance(legacy.get("app"), str)
        ):
            raise ValueError("memory_unavailable: legacy V1 execution is invalid")
    return execution_for_store(
        store,
        memory_mode=legacy.get("memory_mode", "persistent"),
        app=legacy.get("app", ""),
        template_id=state.get("agent") or "",
    )


def update_execution_context(agent_id: str, execution, *, expected=...) -> None:
    """Bind *execution* onto a run, rewriting ``state.json`` whole.

    The SECOND whole-file rewrite path for this file, and it does NOT share
    :func:`update_state`'s on-loop/off-loop asymmetry: the per-agent lock is held
    UNCONDITIONALLY, across the admission checks, the read and the write. Session
    binding, mode tightening and the pre-run write all reach this from a pool
    thread, so that hold serializes them like any off-loop writer. Only
    :func:`create_agent_folder` reaches it on the event loop, on the spawn and
    admission path rather than a per-turn one, and that is the bound which makes
    an unconditional acquire affordable there. Its write model is the one
    :func:`update_state` records.

    A non-persistent owner keeps this turn's body out of the durable file: only
    retained identity and mode metadata are tightened on disk, and the live record
    carries the merged state.

    Raises ValueError with a ``memory_unavailable:`` reason when the run changed
    during admission, when the run's memory store would change (a run's store is
    immutable), or when its state record cannot be read.
    """
    holder = _lock_for_agent(agent_id)
    with holder.lock:
        current = read_run_execution(agent_id)
        if expected is not ... and current != expected:
            raise ValueError("memory_unavailable: run changed during admission")
        execution = execution.with_mode(current.memory_mode)
        if execution.store != current.store:
            raise ValueError("memory_unavailable: run memory is immutable")
        state = read_state(agent_id)
        if state is None:
            raise ValueError("memory_unavailable: run record is unavailable")
        state.update(
            execution_context=execution.to_record(),
            memory_mode=execution.memory_mode,
            memory_store=execution.store.legacy_name,
        )
        if execution.memory_mode != "persistent":
            # Tighten only an already persisted owner's retention metadata.
            # This preserves restart policy without writing this turn's body.
            path = _agent_dir(agent_id) / "state.json"
            if path.exists():
                durable = json.loads(path.read_text(encoding="utf-8"))
                durable["execution_context"] = execution.to_record()
                durable["memory_mode"] = execution.memory_mode
                _atomic_write(path, durable)
            _LIVE_RUN_STATES[_live_run_key(agent_id)] = state
        else:
            _atomic_write(_agent_dir(agent_id) / "state.json", state)


def read_run_memory_mode(agent_id: str) -> str:
    return read_run_execution(agent_id).memory_mode


def tighten_run_memory_mode(agent_id: str, memory_mode: str) -> str:
    execution = read_run_execution(agent_id)
    tightened = execution.with_mode(memory_mode)
    update_execution_context(agent_id, tightened, expected=execution)
    return tightened.memory_mode


def read_run_app(agent_id: str) -> str:
    return read_run_execution(agent_id).app


def read_run_memory_store(agent_id: str, *, validate_memory_files: bool = False) -> str:
    from kiro_crew.execution_context import validate_execution

    execution = read_run_execution(agent_id)
    if validate_memory_files:
        validate_execution(execution)
    return execution.store.legacy_name


# ── read / update ────────────────────────────────────────────────────


def read_state(agent_id: str) -> dict | None:
    """Read state.json. Returns None on missing, corrupt, or non-object data."""
    if _live_run_key(agent_id) in _LIVE_RUN_STATES:
        return dict(_LIVE_RUN_STATES[_live_run_key(agent_id)])
    try:
        return _read_state_at(_agent_dir(agent_id))
    except ValueError:
        return None


def _read_state_at(agent_dir: Path) -> dict | None:
    """:func:`read_state` for a folder already resolved."""
    try:
        state = json.loads((agent_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return state if isinstance(state, dict) else None


def _read_tombstone_at(path: Path) -> tuple[dict | None, bool]:
    """Parsed tombstone at *path*, plus whether a tombstone file is PRESENT.

    The two answers are separate because their meanings are: nothing recorded an
    ending, versus an ending was recorded and cannot be read.
    """
    try:
        tombstone = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        try:
            return None, path.exists()
        except OSError:
            return None, False
    return (tombstone, True) if isinstance(tombstone, dict) else (None, True)


def read_tombstone(agent_id: str) -> dict | None:
    """Read tombstone.json as an object. Return None on missing/invalid data."""
    try:
        path = _agent_dir(agent_id) / "tombstone.json"
    except ValueError:
        return None
    return _read_tombstone_at(path)[0]


# ── per-agent write serialization ────────────────────────────────────

#: ``update_state`` is a read / merge / rewrite, and its two halves are split by
#: a blocking ``_atomic_write`` (fsync + rename). Two writers on one ``agent_id``
#: therefore interleave: the second one's read predates the first one's write, so
#: its rewrite restores a stale WHOLE-FILE snapshot and silently rolls back every
#: field the first writer had just landed. Losing the other writer's fields is
#: the visible half; rolling back fields NEITHER writer touched is the damaging
#: half.
#:
#: The overlap is structural, not hypothetical. A run writes state from the event
#: loop (PID, session id, provider, retention ``keep``) AND from the thread pool
#: (model provenance, CC-path model refinement, per-turn diagnostics -- each via
#: ``asyncio.to_thread``), so two pool writers overlap during a run and a
#: loop-side write executes while the run's coroutine is suspended inside a
#: pool-side one. Cancellation widens it: cancelling a ``to_thread``
#: await DETACHES the worker rather than stopping it, so it finishes carrying a
#: read that is already stale.
#:
#: SCOPE -- ordinary ``update_state`` callers take the lock OFF-LOOP only.
#: Serializing every loop-side write by waiting would block the event loop behind
#: a pool thread's fsync, which the no-blocking anchor forbids. Retention promotion
#: instead probes the same per-agent lock non-blocking and returns RETRYABLE when
#: busy; once acquired, its existing on-loop keep write cannot be overwritten by
#: an older pool writer. Other on-loop callers keep their pre-existing unlocked
#: behavior -- see :func:`update_state` for the remaining limitation.
#:
#: The ordinary acquire is UNBOUNDED, and can be, because the only on-loop caller
#: that reaches it is ``create_agent_folder``, on the spawn and admission path
#: rather than a per-turn one. Otherwise only pool workers block there, and their
#: own read + fsync + rename already exposes them to a wedged filesystem.
#: Promotion may hold the same lock around its
#: existing loop-side write, but its acquire is always non-blocking and never parks
#: the event loop.
#:
#: In-process only, mirroring the per-key ``threading.Lock`` registry this repo
#: already uses to serialize file read-modify-write (``learn._lock_for``,
#: ``artifacts._lock_for_root``, ``history.ConversationLog._file_locks``). A
#: filesystem lock is the tool for cross-process contention and there is none
#: here: every ``update_state`` caller lives in the gateway process that owns the
#: run. Keyed by agent id so unrelated runs never queue behind one agent's fsync,
#: SELF-CLEANING, with no explicit eviction anywhere. The values are held
#: WEAKLY and every caller keeps a strong reference for the length of its
#: critical section, so an entry lives exactly as long as some writer is using
#: it and then disappears on its own. That is a correctness property, not a
#: tidiness one: removing an entry explicitly while another writer still holds
#: or is queued on it SPLITS the lock's identity -- the next caller mints a
#: fresh lock and enters ``state.json`` alongside the writer still inside it,
#: which is the very loss this lock exists to prevent. Any hook that evicts
#: without holding the lock (a folder delete, the tombstone pruner) can do
#: exactly that, and the pruner's case is not even hypothetical: ``_atomic_write``
#: re-creates the parent directory, so a writer already inside its critical
#: section resurrects the folder the pruner just removed. Weak values make that
#: unrepresentable -- an entry cannot be dropped while anyone can still reach
#: it -- and agent ids are per-run uuids, so nothing accumulates either.
_STATE_LOCKS: "weakref.WeakValueDictionary[str, _AgentLock]" = weakref.WeakValueDictionary()
_STATE_LOCKS_GUARD = threading.Lock()
_RETENTION_LOCKS: "weakref.WeakValueDictionary[str, _AgentLock]" = weakref.WeakValueDictionary()
_RETENTION_LOCKS_GUARD = threading.Lock()


class _AgentLock:
    """A ``threading.Lock`` that can be weakly referenced.

    ``threading.Lock`` itself cannot, so the registry stores this one-field
    wrapper instead. Callers must retain the WRAPPER (not just ``.lock``) for
    the whole critical section: it is the strong reference that keeps the
    registry entry alive, and therefore keeps every concurrent writer on the
    same lock.
    """

    __slots__ = ("lock", "__weakref__")

    def __init__(self) -> None:
        self.lock = threading.Lock()


def _on_event_loop() -> bool:
    """True when the calling thread is running an asyncio event loop.

    The seam that keeps the lock off the loop. A thread ``asyncio.to_thread`` /
    ``run_in_executor`` dispatched to has no running loop, so pool writers
    serialize; a synchronous call made from a coroutine does, so it does not
    wait. Chosen over an explicit caller flag because ``update_state`` takes
    ``**fields``: any keyword flag would be indistinguishable from a state field
    a caller means to persist.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _lock_for_registry(agent_id, registry, guard):  # type: ignore[no-untyped-def]
    with guard:
        holder = registry.get(agent_id)
        if holder is None:
            holder = _AgentLock()
            registry[agent_id] = holder
        return holder


def _try_acquire_registry_lock(agent_id, registry, guard):  # type: ignore[no-untyped-def]
    if not guard.acquire(blocking=False):
        return None
    try:
        holder = registry.get(agent_id)
        if holder is None:
            holder = _AgentLock()
            registry[agent_id] = holder
    finally:
        guard.release()
    if not holder.lock.acquire(blocking=False):
        return None
    return holder


def _lock_for_agent(agent_id: str) -> "_AgentLock":
    """Return the process-wide ``state.json`` lock holder for *agent_id*."""
    return _lock_for_registry(agent_id, _STATE_LOCKS, _STATE_LOCKS_GUARD)


def _try_acquire_state_lock(agent_id: str) -> "_AgentLock | None":
    """Return the held per-agent writer lock, or None without blocking."""
    return _try_acquire_registry_lock(agent_id, _STATE_LOCKS, _STATE_LOCKS_GUARD)


def _retention_lock_for_agent(agent_id: str) -> "_AgentLock":
    """Return the process-wide retention arbitration holder for *agent_id*."""
    return _lock_for_registry(agent_id, _RETENTION_LOCKS, _RETENTION_LOCKS_GUARD)


def _try_acquire_retention_lock(agent_id: str) -> "_AgentLock | None":
    """Return held per-agent retention arbitration, or None without blocking."""
    return _try_acquire_registry_lock(agent_id, _RETENTION_LOCKS, _RETENTION_LOCKS_GUARD)


def _acquire_retention_locks(*agent_ids: str) -> list["_AgentLock"]:
    """Acquire retention locks in stable order for off-loop prune work."""
    holders = [_retention_lock_for_agent(agent_id) for agent_id in sorted(set(agent_ids))]
    for holder in holders:
        holder.lock.acquire()
    return holders


def _release_retention_locks(holders: list["_AgentLock"]) -> None:
    for holder in reversed(holders):
        holder.lock.release()


def update_state(agent_id: str, **fields: object) -> bool:
    """Merge *fields* into state.json (atomic rewrite).

    Returns True when the merge was written, False when it was SKIPPED because
    the current state could not be read (missing/corrupt/unreadable). The skip
    is deliberate -- fabricating a fresh state here would resurrect a record
    the reaper deleted -- but callers with a durability contract (the pre-spawn
    provenance write) need to see the skip to retry rather than mistake
    a silent no-op for success.

    The read / merge / rewrite is serialized per agent for OFF-LOOP callers (see
    :data:`_STATE_LOCKS`), so two pool writers cannot rewrite a snapshot
    that predates the other's write.

    WRITE MODEL: the whole-file rewrite is the recorded choice for this file, not a
    way station toward a revision counter or a compare-and-swap retry loop.
    ``state.json`` is a run's artifact and evidence record, while scheduling's
    source of truth is the durable task queue with its own generation fencing, so a
    second coordination protocol here would order writes this file does not need at
    the price of a format every reader must agree on. The invariant that keeps the
    rewrite safe instead: every whole-file write happens at a KNOWN site, and each
    site reachable from the event loop carries its own fence.
    ``test_subagent_state_write_model`` holds that census and fails a new site.
    The asymmetry below therefore closes by moving a site OFF the loop, where it
    inherits the lock -- never by changing the on-disk format.

    KNOWN LIMITATION: ordinary ON-LOOP callers do not take the lock, because waiting
    on a pool thread's fsync from the event loop is exactly the blocking call the
    repo's anchor forbids. Every writer inside a run goes off-loop through
    ``_write_state_off_loop`` and is drained on cancellation; an abandoned writer
    holds the conversation until it settles, so the
    on-loop retention writes are deferred past it. Retention promotion adds a
    second defense: on the event loop it probes the same per-agent lock
    non-blocking and returns RETRYABLE on contention, while off-loop promotion
    lets ``update_state`` acquire the lock normally. Thus no stale writer can
    roll back ``keep=True`` and no loop-side caller waits for a pool writer's
    fsync. Release runs its demote off-loop (``release_conversation_async``) while
    its ``SessionMap`` mutation stays on the loop. The one remaining on-loop
    caller is retention promotion, which stays there by design: its busy check,
    promotion and spawn admission must commit with no await between them, and its
    non-blocking probe already keeps a stale writer from rolling it back.
    """
    if _live_run_key(agent_id) in _LIVE_RUN_STATES:
        _LIVE_RUN_STATES[_live_run_key(agent_id)].update(fields)
        return True
    p = _agent_dir(agent_id) / "state.json"
    # Off-loop callers serialize; on-loop callers keep pre-existing behaviour.
    # ``holder`` stays referenced for the whole critical section -- that strong
    # reference is what keeps every concurrent writer on one lock (see
    # :data:`_STATE_LOCKS`), so it must not be narrowed to ``holder.lock``.
    holder = None if _on_event_loop() else _lock_for_agent(agent_id)
    if holder is not None:
        holder.lock.acquire()
    try:
        try:
            # Off the loop, a Windows sharing violation on the read is retried
            # like the rename at the other end of this rewrite (_atomic_write).
            state = json.loads(read_bytes_with_retry(p).decode("utf-8"))
        except (OSError, ValueError, RecursionError):
            logger.debug("update_state: cannot read state for %s, skipping", agent_id)
            return False
        if not isinstance(state, dict):
            logger.debug("update_state: non-object state for %s, skipping", agent_id)
            return False
        state.update(fields)
        state["updated_at"] = time.time()
        _atomic_write(p, state)
    finally:
        if holder is not None:
            holder.lock.release()
    return True


def promote_retention(
    agent_id: str,
    *,
    state_writer: Callable[..., bool] = update_state,
) -> RetentionPromotionResult:
    """Atomically promote *agent_id* against concurrent in-process prune.

    Promotion acquires per-agent retention arbitration and, on the event loop,
    probes the state-writer lock non-blocking. Contention returns ``RETRYABLE``
    without parking the loop. Off-loop callers let ``update_state`` acquire its
    normal non-reentrant state lock. Thus promotion either writes ``keep=True``
    before prune's locked re-read or waits for a later retry; a process crash
    leaves no half-committed claim format, and the next prune re-evaluates state.
    """
    retention_holder = _try_acquire_retention_lock(agent_id)
    if retention_holder is None:
        return RetentionPromotionResult.RETRYABLE
    state_holder = None
    if _on_event_loop():
        state_holder = _try_acquire_state_lock(agent_id)
        if state_holder is None:
            retention_holder.lock.release()
            return RetentionPromotionResult.RETRYABLE
    try:
        if not state_writer(agent_id, keep=True):
            return RetentionPromotionResult.RETRYABLE
        return RetentionPromotionResult.PROMOTED
    finally:
        if state_holder is not None:
            state_holder.lock.release()
        retention_holder.lock.release()


# ── result streaming ─────────────────────────────────────────────────


def _replace_in_run_folder(
    path: Path, content: str | bytes, *, fsync: bool = False, newline: str | None = None
) -> None:
    """:func:`atomic_write` *path*, never recreating a run folder that is gone.

    ``atomic_write`` creates a missing parent; a run's folder that was removed
    (retention, a dismissal) stays removed, as an append into it would leave it.
    """
    if not path.parent.is_dir():
        raise FileNotFoundError(f"run folder {path.parent} is gone")
    atomic_write(path, content, fsync=fsync, newline=newline)


def write_result_chunk(agent_id: str, text: str, *, fresh: bool = False) -> bool:
    """Append *text* to ``result.txt``; *fresh* replaces the file with it.

    An attempt's first chunk is written *fresh*, so ``result.txt`` holds that
    attempt's text alone, never glued to what an earlier attempt on this id
    left. The fresh write is a temp file and rename, so the earlier partial
    stays readable until the new text is on disk, and a write that fails
    leaves it in place. Written as is (``newline=""``), so the file is LF on
    every platform, like the whole rewrite (:func:`write_finished_result`).
    False when nothing was written.
    """
    if _live_run_key(agent_id) in _LIVE_RUN_STATES:
        return True
    p = _agent_dir(agent_id) / "result.txt"
    try:
        if fresh:
            _replace_in_run_folder(p, text, newline="")
        else:
            with p.open("a", encoding="utf-8", newline="") as f:
                f.write(text)
    except OSError:
        logger.debug("write_result_chunk failed for %s", agent_id, exc_info=True)
        return False
    return True


# ── session memory mode ──────────────────────────────────────────────


def read_session_memory_mode(session_key: str) -> str | None:
    from kiro_crew.execution_context import read_session_execution
    from kiro_crew.history import ConversationLog

    execution = read_session_execution(session_key)
    if execution is not None:
        return execution.memory_mode
    metadata, readable = ConversationLog().get_metadata_status(session_key)
    if not readable:
        raise ValueError("memory_unavailable: session mode is unavailable")
    mode = metadata.get("memory_mode")
    if mode is not None and mode not in ("persistent", "incognito", "temporary"):
        raise ValueError("memory_unavailable: invalid session mode")
    return mode


def bind_session_memory_mode(session_key: str, memory_mode: str) -> str:
    from kiro_crew.execution_context import (
        ExecutionContext,
        MemoryStoreRef,
        bind_session_execution,
        read_session_execution,
    )

    execution = read_session_execution(session_key)
    if execution is None:
        execution = ExecutionContext(None, MemoryStoreRef("default"), "template", "kirocrew")
    execution = execution.with_mode(memory_mode)
    bind_session_execution(session_key, execution)
    return execution.memory_mode
