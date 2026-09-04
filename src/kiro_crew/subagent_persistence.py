"""Subagent persistence — disk I/O for agent folders.

Each subagent gets a folder at ``~/.kiro/crew/subagents/{id}/`` containing:
- ``state.json``   — running state (task, PID, turns, last_tool)
- ``result.txt``   — streamed result text
- ``tombstone.json`` — written on abnormal exit only
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import weakref
from enum import Enum
from pathlib import Path
from typing import Callable

from kiro_crew import platform_compat
from kiro_crew.acp.types import PROVIDER_LABEL_DEFAULT
from kiro_crew.config.paths import data_home, kiro_sessions_dir
from kiro_crew.jsonl_util import rotate_jsonl_at
from kiro_crew.providers.cleanup import _is_safe_path

logger = logging.getLogger(__name__)

# Resolved per call, never captured at import: an import-time binding freezes
# the data home and defeats pod isolation, the lazy legacy-home migration and
# test isolation. The name below is an opt-in override (None = live home) so
# existing monkeypatch call sites keep working. See config.md "Data Home";
# dashboard/handlers/usage.py is the reference implementation.
_SUBAGENTS_DIR: Path | None = None

# Promotion and prune arbitrate per agent; the weak lock registry is defined
# beside the state-writer registry below so unrelated conversations never couple.
SUBAGENT_CONVERSATION_PREFIX = "subagent:"
_CLEANUP_IDENTITIES_FILE = "cleanup-identities.json"
_CLEANUP_IDENTITIES_TRUST_DIR = "subagent-cleanup-identities"
_CLEANUP_IDENTITY_LOCK = threading.Lock()
_LIVE_CLEANUP_IDENTITIES: dict[str, list[dict[str, object]]] = {}
_LIVE_CLEANUP_HINTS: set[str] = set()


class RetentionPromotionResult(Enum):
    """Outcome of the non-blocking promotion transaction."""

    PROMOTED = "promoted"
    RETRYABLE = "retryable"


def _cleanup_identities_path(agent_id: str) -> Path:
    # Validate with the canonical agent-directory guard, but keep this durable
    # cleanup authority OUTSIDE the agent-writable run folder. ``trust`` is on
    # the shared file gate's read+write sensitive floor, so a subagent cannot
    # replace another session ID and trick prune into deleting its transcript.
    _agent_dir(agent_id)
    return (
        _subagents_dir().parent
        / "trust"
        / _CLEANUP_IDENTITIES_TRUST_DIR
        / agent_id
        / _CLEANUP_IDENTITIES_FILE
    )


def _protect_cleanup_identities_path(agent_id: str) -> Path:
    """Return the cleanup record only after fail-loud owner-only lockdown."""
    path = _cleanup_identities_path(agent_id)
    for protected_dir in (path.parents[2], path.parents[1], path.parent):
        platform_compat.make_owner_only_dir(protected_dir)
        # ``make_owner_only_dir`` is best-effort by contract. Cleanup identity
        # authorizes transcript deletion, so failure here must abort the read or
        # write on Windows as well as POSIX instead of trusting a permissive ACL.
        platform_compat.restrict_dir_to_owner(protected_dir)
    if path.exists():
        # Tightening a parent does not retrofit an existing Windows file DACL.
        platform_compat.restrict_to_owner(path)
    return path


def _delete_cleanup_identities_file(agent_id: str) -> None:
    """Remove the protected generation record after its run folder is gone."""
    shutil.rmtree(_cleanup_identities_path(agent_id).parent, ignore_errors=True)


def _read_cleanup_identities_file(agent_id: str) -> list[dict[str, object]]:
    path = _protect_cleanup_identities_path(agent_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(payload, dict) or not isinstance(payload.get("identities"), list):
        raise ValueError("invalid protected cleanup identity payload")
    raw = payload["identities"]
    records: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("invalid protected cleanup identity record")
        sid = item.get("session_id")
        if not isinstance(sid, str) or not sid:
            raise ValueError("invalid protected cleanup identity SID")
        record: dict[str, object] = {"session_id": sid}
        provider = item.get("provider")
        cwd = item.get("cwd")
        keep = item.get("keep")
        conversation_key = item.get("conversation_key")
        if "provider" in item and (not isinstance(provider, str) or not provider):
            raise ValueError("invalid protected cleanup identity provider")
        if "cwd" in item and (not isinstance(cwd, str) or not cwd):
            raise ValueError("invalid protected cleanup identity CWD")
        if "keep" in item and not isinstance(keep, bool):
            raise ValueError("invalid protected cleanup identity retention")
        if "conversation_key" in item and (
            not isinstance(conversation_key, str) or not conversation_key
        ):
            raise ValueError("invalid protected cleanup identity owner")
        if isinstance(provider, str):
            record["provider"] = provider
        if isinstance(cwd, str):
            record["cwd"] = cwd
        if isinstance(keep, bool):
            record["keep"] = keep
        if isinstance(conversation_key, str):
            record["conversation_key"] = conversation_key
        records.append(record)
    return records


def _merge_cleanup_identity_records(
    *groups: object,
) -> list[dict[str, object]]:
    """Merge identity groups left-to-right, keyed by SID with richer fields kept."""
    by_sid: dict[str, dict[str, object]] = {}
    for group in groups:
        if not isinstance(group, (list, tuple)):
            continue
        for item in group:
            if not isinstance(item, dict):
                continue
            sid = item.get("session_id")
            if not isinstance(sid, str) or not sid:
                continue
            record = by_sid.setdefault(sid, {"session_id": sid})
            provider = item.get("provider")
            cwd = item.get("cwd")
            keep = item.get("keep")
            conversation_key = item.get("conversation_key")
            if isinstance(provider, str) and provider:
                record["provider"] = provider
            if isinstance(cwd, str) and cwd:
                record["cwd"] = cwd
            if isinstance(keep, bool):
                record["keep"] = keep
            if isinstance(conversation_key, str) and conversation_key:
                record["conversation_key"] = conversation_key
    return list(by_sid.values())


def _cleanup_identity_record(
    *,
    session_id: str,
    provider: str,
    cwd: str,
    keep: bool | None,
    conversation_key: str,
) -> dict[str, object]:
    record: dict[str, object] = {"session_id": session_id}
    if provider:
        record["provider"] = provider
    if cwd:
        record["cwd"] = cwd
    if isinstance(keep, bool):
        record["keep"] = keep
    if conversation_key:
        record["conversation_key"] = conversation_key
    return record


def publish_live_cleanup_identity(
    agent_id: str,
    *,
    session_id: str = "",
    provider: str = "",
    cwd: str = "",
    keep: bool | None = None,
    conversation_key: str = "",
) -> None:
    """Publish one generation in memory without waiting or filesystem I/O."""
    if not session_id:
        return
    record = _cleanup_identity_record(
        session_id=session_id,
        provider=provider,
        cwd=cwd,
        keep=keep,
        conversation_key=conversation_key,
    )
    # list.append is atomic under the interpreter lock. Deliberately do not
    # acquire _CLEANUP_IDENTITY_LOCK: its owner may be blocked in fsync, while
    # this path runs on the gateway event loop and must publish before a queued
    # to_thread call can be cancelled. Deduplication belongs to snapshots and
    # sidecar serialization; replacing the list here can lose a concurrent SID.
    records = _LIVE_CLEANUP_IDENTITIES.setdefault(agent_id, [])
    records.append(record)


def remember_live_cleanup_identity(
    agent_id: str,
    *,
    session_id: str = "",
    provider: str = "",
    cwd: str = "",
    keep: bool | None = None,
    conversation_key: str = "",
) -> None:
    """Durably append one complete live session generation for later cleanup."""
    if not session_id:
        return
    publish_live_cleanup_identity(
        agent_id,
        session_id=session_id,
        provider=provider,
        cwd=cwd,
        keep=keep,
        conversation_key=conversation_key,
    )
    with _CLEANUP_IDENTITY_LOCK:
        durable = _read_cleanup_identities_file(agent_id)
        records = _merge_cleanup_identity_records(
            durable,
            _LIVE_CLEANUP_IDENTITIES.get(agent_id, []),
        )
        path = _protect_cleanup_identities_path(agent_id)
        _atomic_write(path, {"identities": records})
        platform_compat.restrict_to_owner(path)


def _live_cleanup_identities(agent_id: str) -> list[dict[str, object]]:
    """Snapshot already-published identities without performing filesystem I/O."""
    if not _CLEANUP_IDENTITY_LOCK.acquire(blocking=False):
        # The writer publishes its merged in-memory fallback before fsync, so a
        # contending event-loop tombstone can snapshot it without waiting.
        fallback = _LIVE_CLEANUP_IDENTITIES.get(agent_id, [])
        return _merge_cleanup_identity_records(fallback)
    try:
        fallback = _LIVE_CLEANUP_IDENTITIES.get(agent_id, [])
        return _merge_cleanup_identity_records(fallback)
    finally:
        _CLEANUP_IDENTITY_LOCK.release()


def publish_live_cleanup_hint(agent_id: str) -> None:
    """Mark run identity as cleanup-owned without trusting agent SID fields."""
    _LIVE_CLEANUP_HINTS.add(agent_id)


def trusted_cleanup_identity_record(
    agent_id: str,
    session_id: str,
    conversation_key: str,
) -> dict[str, object] | None:
    """Return the trusted generation matching restart state, or ``None``.

    Agent-folder state may request registry rebuild, but it cannot choose the SID,
    owner, provider, or CWD that the TTL release path will later clean. Those
    fields must match/source from gateway-published live or protected authority.
    """
    default_key = f"{SUBAGENT_CONVERSATION_PREFIX}{agent_id}"
    records = _merge_cleanup_identity_records(
        _read_cleanup_identities_file(agent_id),
        _live_cleanup_identities(agent_id),
    )
    for record in records:
        record_sid = record.get("session_id")
        record_key = record.get("conversation_key") or default_key
        if record_sid == session_id and record_key == conversation_key:
            return record
    return None


def has_live_cleanup_identity(agent_id: str) -> bool:
    """Return whether cleanup retention is hinted, without disk I/O."""
    return agent_id in _LIVE_CLEANUP_HINTS or bool(_live_cleanup_identities(agent_id))


def subagent_id_from_conversation_key(key: str) -> str | None:
    """Return the subagent owner ID encoded by *key*, or ``None``."""
    if not key.startswith(SUBAGENT_CONVERSATION_PREFIX):
        return None
    owner_id = key[len(SUBAGENT_CONVERSATION_PREFIX) :]
    return owner_id or None


def _subagents_dir() -> Path:
    """Subagents registry directory, resolved against the live data home."""
    return _SUBAGENTS_DIR if _SUBAGENTS_DIR is not None else data_home() / "subagents"


def _agent_dir(agent_id: str) -> Path:
    if (
        not agent_id
        or agent_id == "."
        or ".." in agent_id
        or "/" in agent_id
        or "\\" in agent_id
        or "\0" in agent_id
    ):
        raise ValueError(f"Invalid agent_id: {agent_id!r}")
    base = _subagents_dir()
    resolved = (base / agent_id).resolve()
    parent = base.resolve()
    if resolved == parent or not resolved.is_relative_to(parent):
        raise ValueError(f"Path traversal blocked for agent_id: {agent_id!r}")
    return resolved


def agent_dir_for_display(agent_id: str) -> Path:
    """The run directory in the home spelling the reader's own tooling uses.

    :func:`_agent_dir` returns a symlink-RESOLVED path, and must: a traversal
    check is only sound against the canonical target. That resolved spelling is
    the right one to open a file with, and the wrong one to hand to somebody as
    a path to go read.

    On a host whose home is itself a symlink the two spellings differ. An Amazon
    cloud desktop's ``/home/<user> -> /local/home/<user>`` is the ordinary case,
    and there ``data_home()`` under ``$HOME`` resolves to a ``/local/home/...``
    prefix that the reader's path allowlist -- keyed on the ``$HOME`` it was
    given -- does not match. The file is readable; the spelling is not
    recognized. So a result path emitted in resolved form is refused, while the
    identical file in declared form is allowed, and the refusal arrives as an
    approval prompt that times out rather than as an error anyone can act on.

    Hence: validate on the resolved form, hand out the declared one. Callers
    doing file I/O keep using :func:`_agent_dir`; this is for a path that a
    human or an agent will read and then act on.

    Raises the same ``ValueError`` as :func:`_agent_dir` for a rejected
    ``agent_id`` -- the validation is not duplicated here, it is delegated, so
    the two cannot drift apart.
    """
    _agent_dir(agent_id)  # validation only; the return value is deliberately unused
    return _subagents_dir() / agent_id


# ── create ───────────────────────────────────────────────────────────


def create_agent_folder(
    agent_id: str,
    *,
    task: str = "",
    agent: str = "",
    parent_session: str = "",
    max_turns: int = 0,
    context_groups: str = "",
) -> Path:
    """Create ``~/.kiro/crew/subagents/{id}/`` with ``state.json``.

    ``context_groups`` is the run's injected-context scope, as a comma-joined
    list of the switchable groups it KEEPS. It is recorded here, at folder
    creation, because that is the first moment it is known: a continuation
    resolves an evicted run's scope from this file, and deferring the write to a
    later read-modify-write would let a failed update silently widen the scope
    of the follow-up turn. An empty string means every switchable group was
    withheld — distinct from the key being absent, which marks a run from before
    the field existed and resolves to all-on.
    """
    d = _agent_dir(agent_id)
    d.mkdir(parents=True, exist_ok=True)
    state = {
        "id": agent_id,
        "task": task,
        "agent": agent,
        "parent_session": parent_session,
        "started": time.time(),
        "max_turns": max_turns,
        "status": "running",
        "pid": None,
        "turns": 0,
        "last_tool": "",
        "context_groups": context_groups,
        "updated_at": time.time(),
    }
    _atomic_write(d / "state.json", state)
    return d


# ── read / update ────────────────────────────────────────────────────


def read_state(agent_id: str) -> dict | None:
    """Read state.json. Returns None on missing, corrupt, or non-object data."""
    try:
        p = _agent_dir(agent_id) / "state.json"
        state = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return state if isinstance(state, dict) else None


def read_tombstone(agent_id: str) -> dict | None:
    """Read tombstone.json as an object. Return None on missing/invalid data."""
    try:
        p = _agent_dir(agent_id) / "tombstone.json"
        tombstone = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return tombstone if isinstance(tombstone, dict) else None


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
#: pool-side one (#6298). Cancellation widens it: cancelling a ``to_thread``
#: await DETACHES the worker rather than stopping it, so it finishes carrying a
#: read that is already stale (#6308).
#:
#: SCOPE -- ordinary ``update_state`` callers take the lock OFF-LOOP only.
#: Serializing every loop-side write by waiting would block the event loop behind
#: a pool thread's fsync, which the no-blocking anchor forbids. Retention promotion
#: instead probes the same per-agent lock non-blocking and returns RETRYABLE when
#: busy; once acquired, its existing on-loop keep write cannot be overwritten by
#: an older pool writer. Other on-loop callers keep their pre-existing unlocked
#: behavior -- see :func:`update_state` for the remaining #6308 limitation.
#:
#: The ordinary acquire is UNBOUNDED, and can be, because no on-loop caller reaches
#: it: only pool workers block there, and their own read + fsync + rename already
#: exposes them to a wedged filesystem. Promotion may hold the same lock around its
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
    return _try_acquire_registry_lock(
        agent_id, _RETENTION_LOCKS, _RETENTION_LOCKS_GUARD
    )


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
    provenance write, #5394) need to see the skip to retry rather than mistake
    a silent no-op for success.

    The read / merge / rewrite is serialized per agent for OFF-LOOP callers (see
    :data:`_STATE_LOCKS`), so two pool writers can no longer rewrite a snapshot
    that predates the other's write.

    KNOWN LIMITATION: ordinary ON-LOOP callers do not take the lock, because waiting
    on a pool thread's fsync from the event loop is exactly the blocking call the
    repo's anchor forbids. Every writer inside a run now goes off-loop through
    ``_write_state_off_loop`` and is drained on cancellation (#6298 / #6308 /
    #7302); an abandoned writer holds the conversation until it settles, so the
    on-loop retention writes are deferred past it. Retention promotion adds a
    second defense: on the event loop it probes the same per-agent lock
    non-blocking and returns RETRYABLE on contention, while off-loop promotion
    lets ``update_state`` acquire the lock normally. Thus no stale writer can
    roll back ``keep=True`` and no loop-side caller waits for a pool writer's
    fsync. The remaining on-loop callers are the synchronous retention writers;
    they still pay their own fsync on the loop, and moving that I/O while keeping
    their ``SessionMap`` mutation on-loop is the rest of #7302.
    """
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
            state = json.loads(p.read_text(encoding="utf-8"))
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


def write_result_chunk(agent_id: str, text: str) -> None:
    """Append *text* to ``result.txt``."""
    p = _agent_dir(agent_id) / "result.txt"
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        logger.debug("write_result_chunk failed for %s", agent_id, exc_info=True)


# ── tombstone ────────────────────────────────────────────────────────


def _check_result_available(path: Path) -> bool:
    """Check if result file exists and is non-empty (TOCTOU-safe)."""
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def write_tombstone(
    agent_id: str,
    *,
    cause: str,
    recovery_action: str,
    **extra: object,
) -> None:
    """Write ``tombstone.json`` for an abnormally exited agent."""
    d = _agent_dir(agent_id)
    state = read_state(agent_id) or {}
    cleanup_identity = {
        key: state[key]
        for key in ("session_id", "provider", "cwd")
        if state.get(key)
    }
    live_cleanup_identities = _live_cleanup_identities(agent_id)
    latest_live_identity = live_cleanup_identities[-1] if live_cleanup_identities else {}
    generation_metadata: dict[str, object] = {}
    if live_cleanup_identities:
        generation_metadata["cleanup_identities"] = live_cleanup_identities
    tombstone = {
        "id": agent_id,
        "task": state.get("task", ""),
        "agent": state.get("agent", ""),
        "parent_session": state.get("parent_session", ""),
        "started": state.get("started"),
        "died": time.time(),
        "cause": cause,
        "recovery_action": recovery_action,
        "result_available": _check_result_available(d / "result.txt"),
        "result_path": str(d / "result.txt"),
        **cleanup_identity,
        **latest_live_identity,
        **generation_metadata,
        **extra,
    }
    try:
        _atomic_write(d / "tombstone.json", tombstone)
    except OSError:
        logger.warning("write_tombstone failed for %s", agent_id, exc_info=True)
    state_sid = cleanup_identity.get("session_id")
    if not live_cleanup_identities and isinstance(state_sid, str) and state_sid:
        # The run folder is agent-writable. Preserve only the retention/exemption
        # signal here; provider-deletion authority requires gateway-published live
        # identity or the protected durable record.
        publish_live_cleanup_hint(agent_id)


def mark_delivered(agent_id: str) -> None:
    """Mark a successfully-delivered subagent for deferred TTL cleanup.

    Writes a ``cause="delivered"`` tombstone instead of deleting the folder
    immediately, so (a) orphan reconciliation skips it on restart and (b) the
    reaper prunes it after the (short) delivered TTL — giving the parent a grace
    window to read ``result.txt`` via ``spawn_status`` / read / grep after the
    completion event, rather than re-running the subagent.
    """
    write_tombstone(agent_id, cause="delivered", recovery_action="delivered")


def clear_tombstone(agent_id: str) -> bool:
    """Remove ``tombstone.json`` so the agent is visible to orphan recovery again.

    A tombstone is the marker :func:`list_orphans` uses to EXCLUDE a folder from
    restart reconciliation. That is correct once the outcome has reached the
    parent, but the terminal record is written BEFORE delivery is attempted — so
    if delivery is then abandoned (gateway shutdown cancelling a still-pending
    terminal report), the tombstone would suppress the one mechanism that could
    still hand the result to the parent, losing it permanently.

    Clearing it re-admits the folder to the next start's reconciliation, which
    sees ``result.txt`` and re-delivers. Returns True if a tombstone was
    removed. Best-effort: never raises to the caller.
    """
    try:
        p = _agent_dir(agent_id) / "tombstone.json"
        existed = p.exists()
        p.unlink(missing_ok=True)
        return existed
    except OSError:
        logger.warning("clear_tombstone failed for %s", agent_id, exc_info=True)
        return False


# ── slow-command record (stalled but STILL RUNNING) ──────────────────


# Rotate ``slow_commands.jsonl`` once it exceeds this size, keeping ONE
# previous generation (``.jsonl.1``) — the same 1 MiB cap / ~2 MiB total
# shape as ``mcp_gateway.stub._FALLBACK_LOG_MAX_BYTES``. The log lives at
# the subagents-dir root so it survives per-agent folder cleanup, which
# also keeps it outside ``prune_stale_tombstones``'s sweep (that prune
# skips non-directories) — this cap is its only bound.
_SLOW_LOG_MAX_BYTES = 1024 * 1024


def record_slow_command(agent_id: str, **fields: object) -> None:
    """Append a stalled subagent's slow command to ``slow_commands.jsonl``.

    Unlike :func:`write_tombstone`, this does NOT mark the agent dead — a
    stalled subagent is still running; the record is purely for later analysis
    of which commands run slow. At the subagents-dir root so it survives
    per-agent folder cleanup; rotated at :data:`_SLOW_LOG_MAX_BYTES` keeping
    one previous generation. Best-effort: never raises to the caller.

    Bounded via rotate-by-rename (``os.replace``, O(1)) rather than a
    read-and-rewrite trim: this is invoked synchronously from the async
    stall detector (``subagent._maybe_flag_stall``), so whole-file work
    here would stall the gateway event loop.
    """
    entry = {"id": agent_id, "flagged": time.time(), **fields}
    base = _subagents_dir()
    try:
        base.mkdir(parents=True, exist_ok=True)
        log_path = base / "slow_commands.jsonl"
        # Rotation (shared helper): O(1) rotate-by-rename at the cap, guarded
        # by a non-blocking try-lock so two writers hitting the cap together
        # cannot both rotate, and a loser never waits — no call can stall the
        # gateway event loop. The helper is best-effort by contract: ANY of
        # its failures — the lock file unopenable (fd exhaustion, read-only or
        # ACL-restricted dir), a fresh-boot missing log, a Windows sharing
        # violation rejecting the rename — degrades to appending without
        # rotating. Fd/disk exhaustion is a leading cause of the very stalls
        # this log diagnoses, so a rotation failure must never cost the
        # record; only a failure of the append itself may.
        rotate_jsonl_at(log_path, _SLOW_LOG_MAX_BYTES)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        logger.warning("record_slow_command failed for %s", agent_id, exc_info=True)


# ── delete ───────────────────────────────────────────────────────────


def delete_agent_folder(agent_id: str) -> None:
    """Remove the entire agent directory and its in-process identity fallback."""
    d = _agent_dir(agent_id)
    with _CLEANUP_IDENTITY_LOCK:
        shutil.rmtree(d, ignore_errors=True)
        if not d.exists():
            _delete_cleanup_identities_file(agent_id)
            _LIVE_CLEANUP_IDENTITIES.pop(agent_id, None)
            _LIVE_CLEANUP_HINTS.discard(agent_id)


# ── list orphans ─────────────────────────────────────────────────────


def list_orphans() -> list[dict]:
    """Return parsed state for all non-tombstoned agent folders."""
    results: list[dict] = []
    try:
        dirs = sorted(_subagents_dir().iterdir())
    except (FileNotFoundError, OSError):
        return results
    for d in dirs:
        if not d.is_dir():
            continue
        if (d / "tombstone.json").exists():
            continue
        state = read_state(d.name)
        if state is None:
            logger.debug("list_orphans: skipping corrupt state in %s", d.name)
            continue
        results.append(state)
    return results


# ── prune ────────────────────────────────────────────────────────────

# Preserve unknown retention state long enough to outlive the six-hour
# continuable-conversation window, then reclaim it using tombstone metadata.
_UNREADABLE_STATE_GRACE_SECS = 24 * 3600
_UNRECLAIMABLE_LOOKUP_MAX_AGE_SECS = 90 * 86400


def _tombstone_died(ts: dict[str, object], path: Path, now: float) -> int | float:
    """Return a finite, positive, non-future death time with bounded fallback."""
    died = ts.get("died")
    if (
        isinstance(died, (int, float))
        and not isinstance(died, bool)
        and 0 < died <= now
    ):
        return died
    try:
        fallback = path.stat().st_mtime
    except OSError:
        return now
    if 0 < fallback <= now:
        return fallback
    return now


def _should_defer_tombstone_cleanup(
    *,
    retention_state: dict[str, object],
    retention_unknown: bool,
    cleanup_session_id: object,
    died: object,
    cutoff: float,
    now: float,
) -> bool:
    """Return whether prune must preserve provider files and identity folder."""
    if retention_unknown:
        if not cleanup_session_id or not (
            isinstance(died, (int, float))
            and not isinstance(died, bool)
            and 0 < died <= now
        ):
            return False
        return died >= cutoff - _UNREADABLE_STATE_GRACE_SECS
    return retention_state.get("keep") is True


def _cleanup_identity_fallback_record(
    agent_id: str, session_id: object
) -> dict[str, object] | None:
    """Return the matching generation, or latest when top-level SID is absent."""
    records = _merge_cleanup_identity_records(
        _read_cleanup_identities_file(agent_id),
        _live_cleanup_identities(agent_id),
    )
    if not records:
        return None
    if isinstance(session_id, str) and session_id:
        for record in reversed(records):
            if record.get("session_id") == session_id:
                return record
        # Agent-writable state cannot suppress protected retention authority by
        # naming an unrelated SID. Fall back to the latest trusted generation;
        # this record can preserve ownership but never expands deletion authority.
    return records[-1]


def _cleanup_retention_fallback(
    agent_id: str, session_id: object
) -> tuple[bool | None, str, str]:
    """Return trusted fallback retention, owner, and SID."""
    record = _cleanup_identity_fallback_record(agent_id, session_id)
    if record is None:
        return None, "", ""
    keep = record.get("keep")
    conversation_key = record.get("conversation_key")
    record_sid = record.get("session_id")
    return (
        keep if isinstance(keep, bool) else None,
        conversation_key if isinstance(conversation_key, str) else "",
        record_sid if isinstance(record_sid, str) else "",
    )


def _tombstone_cleanup_identities(agent_id: str) -> list[tuple[str, str, str]]:
    """Return gateway-authorized cleanup generations only.

    Tombstone and state identity fields live in the agent-writable run folder.
    They remain compatibility/display hints, but cannot expand the set of provider
    transcripts prune may delete. Durable protected records and synchronous live
    gateway publication are the only deletion authorities.
    """
    records = _merge_cleanup_identity_records(
        _read_cleanup_identities_file(agent_id),
        _live_cleanup_identities(agent_id),
    )
    identities: list[tuple[str, str, str]] = []
    for identity_record in records:
        sid = identity_record.get("session_id")
        if not isinstance(sid, str) or not sid:
            continue
        record_provider = identity_record.get("provider")
        record_cwd = identity_record.get("cwd")
        identities.append(
            (
                sid,
                record_provider
                if isinstance(record_provider, str) and record_provider
                else PROVIDER_LABEL_DEFAULT,
                record_cwd if isinstance(record_cwd, str) else "",
            )
        )
    return identities


def prune_stale_tombstones(max_age_days: int = 7, delivered_ttl_secs: int = 3600) -> int:
    """Delete tombstoned folders past their retention window. Returns count pruned.

    Two windows: abnormal-exit tombstones (timeout / error / orphan) are kept for
    *max_age_days* for post-mortem diagnostics; ``cause="delivered"`` tombstones
    (successful deliveries retained so the parent can read the full transcript)
    are pruned after the shorter *delivered_ttl_secs* to bound disk growth.
    """
    now = time.time()
    default_cutoff = now - (max_age_days * 86400)
    delivered_cutoff = now - max(0, delivered_ttl_secs)
    pruned = 0
    try:
        dirs = sorted(_subagents_dir().iterdir())
    except (FileNotFoundError, OSError):
        return 0
    for d in dirs:
        if not d.is_dir():
            continue
        ts_path = d / "tombstone.json"
        if not ts_path.exists():
            continue
        try:
            ts = read_tombstone(d.name)
            if ts is None:
                logger.debug("prune: skipping corrupt tombstone in %s", d.name)
                continue
            cutoff = delivered_cutoff if ts.get("cause") == "delivered" else default_cutoff
            died = _tombstone_died(ts, ts_path, now)
            if died <= cutoff:
                tombstone_session_id = ts.get("session_id", "")
                state_hint = read_state(d.name)
                claim_hint = d.name
                hinted_key = ""
                if isinstance(state_hint, dict):
                    hinted_session_id = state_hint.get("session_id", "")
                    _, fallback_key, fallback_sid = _cleanup_retention_fallback(
                        d.name,
                        hinted_session_id,
                    )
                    # Protected acquisition-time ownership outranks all
                    # agent-writable state, including an empty or conflicting
                    # conversation key. Legacy runs without a generation retain
                    # the compatibility hint from state.
                    hinted_key = (
                        fallback_key
                        if fallback_sid
                        else str(state_hint.get("conversation_key") or "")
                    )
                else:
                    hinted_record = _cleanup_identity_fallback_record(d.name, "")
                    if hinted_record is not None:
                        hinted_key = str(hinted_record.get("conversation_key") or "")
                hinted_owner = subagent_id_from_conversation_key(hinted_key)
                if hinted_owner:
                    try:
                        _agent_dir(hinted_owner)
                    except ValueError:
                        pass
                    else:
                        claim_hint = hinted_owner
                locked_agent_ids = {d.name, claim_hint}
                retention_holders = _acquire_retention_locks(*locked_agent_ids)
                try:
                    state = read_state(d.name)
                    claim_agent_id = d.name
                    lookup_session_id = tombstone_session_id
                    if isinstance(state, dict):
                        lookup_session_id = state.get("session_id") or tombstone_session_id
                        retention_unknown = False
                        retention_state: dict[str, object] = state
                        session_id = state.get("session_id", "")
                        # Every completed plain run records keep=False. A continuation
                        # follows readable original-owner state; unreadable owner
                        # intent receives bounded grace.
                        state_key = str(state.get("conversation_key") or "")
                        (
                            fallback_keep,
                            fallback_key,
                            fallback_sid,
                        ) = _cleanup_retention_fallback(
                            d.name,
                            session_id,
                        )
                        if fallback_sid:
                            # State is agent-writable. Once gateway-published
                            # identity exists, it cannot erase or redirect the
                            # continuation owner used for retention arbitration.
                            conversation_key = fallback_key
                            if not session_id:
                                session_id = fallback_sid
                            if "keep" not in state and fallback_keep is not None:
                                retention_state = dict(state)
                                retention_state["keep"] = fallback_keep
                        else:
                            conversation_key = state_key
                        owner_id = subagent_id_from_conversation_key(conversation_key)
                        if owner_id and owner_id != d.name:
                            try:
                                _agent_dir(owner_id)
                            except ValueError:
                                retention_unknown = True
                            else:
                                claim_agent_id = owner_id
                                owner_state = read_state(owner_id)
                                if isinstance(owner_state, dict):
                                    retention_state = owner_state
                                else:
                                    retention_unknown = True
                    else:
                        fallback_record = _cleanup_identity_fallback_record(d.name, "")
                        retention_state = {}
                        retention_unknown = True
                        session_id = ""
                        conversation_key = ""
                        if fallback_record is not None:
                            session_id = fallback_record.get("session_id", session_id)
                            sidecar_keep = fallback_record.get("keep")
                            if isinstance(sidecar_keep, bool):
                                retention_state["keep"] = sidecar_keep
                                # A durable false predates any later promotion
                                # that landed only in now-unreadable state. It
                                # cannot authorize immediate deletion; keep the
                                # bounded unknown-retention grace. True is safe
                                # to honor because it only preserves material.
                                if sidecar_keep:
                                    retention_unknown = False
                            conversation_key = str(
                                fallback_record.get("conversation_key") or ""
                            )
                        owner_id = subagent_id_from_conversation_key(conversation_key)
                        if owner_id and owner_id != d.name:
                            try:
                                _agent_dir(owner_id)
                            except ValueError:
                                retention_unknown = True
                            else:
                                claim_agent_id = owner_id
                                owner_state = read_state(owner_id)
                                if isinstance(owner_state, dict):
                                    retention_state = owner_state
                                    retention_unknown = False
                                else:
                                    retention_unknown = True
                    if claim_agent_id not in locked_agent_ids:
                        continue
                    defer_cleanup = _should_defer_tombstone_cleanup(
                        retention_state=retention_state,
                        retention_unknown=retention_unknown,
                        cleanup_session_id=session_id,
                        died=died,
                        cutoff=cutoff,
                        now=now,
                    )
                    if defer_cleanup:
                        continue
                    # Hold arbitration through cleanup and rmtree. A promotion
                    # arriving after the keep=False decision returns retryable
                    # instead of writing keep=True just before deletion.
                    cleanup_identities = _tombstone_cleanup_identities(d.name)
                    within_retry_window = (
                        died >= now - _UNRECLAIMABLE_LOOKUP_MAX_AGE_SECS
                    )
                    # Legacy/pre-upgrade runs can carry a SID only in the
                    # agent-writable state/tombstone. It is not safe deletion
                    # authority, but the folder is useful for a later trusted
                    # migration. Bound that lookup window so an unavailable
                    # migration cannot accumulate private run folders forever.
                    if (
                        lookup_session_id
                        and not cleanup_identities
                        and within_retry_window
                    ):
                        continue
                    cleanup_succeeded = True
                    for cleanup_sid, cleanup_provider, cleanup_cwd in cleanup_identities:
                        try:
                            if (
                                _cleanup_session_files_sync(
                                    cleanup_sid,
                                    cleanup_provider,
                                    cwd=cleanup_cwd,
                                )
                                is False
                            ):
                                cleanup_succeeded = False
                        except Exception:
                            cleanup_succeeded = False
                            logger.debug(
                                "prune: session cleanup failed for %s (%s)",
                                d.name,
                                cleanup_sid,
                                exc_info=True,
                            )
                    if not cleanup_succeeded and within_retry_window:
                        continue
                    shutil.rmtree(d, ignore_errors=True)
                    if not d.exists():
                        with _CLEANUP_IDENTITY_LOCK:
                            _delete_cleanup_identities_file(d.name)
                            _LIVE_CLEANUP_IDENTITIES.pop(d.name, None)
                            _LIVE_CLEANUP_HINTS.discard(d.name)
                    pruned += 1
                finally:
                    _release_retention_locks(retention_holders)
        except (OSError, ValueError, RecursionError):
            logger.debug("prune: tombstone processing failed for %s", d.name)
    return pruned


# ── session file cleanup ──────────────────────────────────────────────


def _cleanup_session_files_sync(
    session_id: str, provider: str = PROVIDER_LABEL_DEFAULT, *, cwd: str = ""
) -> bool:
    """Delete provider session files, returning whether cleanup completed.

    Synchronous — used during tombstone pruning (which runs in the reaper loop).
    A false result preserves the run folder and protected identity record so a
    future provider cleanup implementation or transient filesystem recovery can
    retry instead of making the leaked transcript permanently unreachable.

    Only the kiro-cli backend stores transcripts where this function can reach
    them. Any other *provider* is logged and its files are left in place, since
    reporting success without deleting anything hides the leak.
    """
    if not session_id:
        return True
    if session_id in (".", ".."):
        return False
    try:
        if provider == PROVIDER_LABEL_DEFAULT:
            sessions_dir = kiro_sessions_dir()
            succeeded = True
            for suffix in (".json", ".jsonl"):
                target = sessions_dir / f"{session_id}{suffix}"
                if not _is_safe_path(target, sessions_dir):
                    logger.error(
                        "_cleanup_session_files_sync: path traversal blocked for %s",
                        target,
                    )
                    return False
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    succeeded = False
                    logger.warning(
                        "_cleanup_session_files_sync: failed to delete %s",
                        target,
                        exc_info=True,
                    )
            return succeeded
        # Every other backend owns its own session storage, which this function
        # has no route to. Report failure so prune retains retry metadata.
        logger.debug(
            "_cleanup_session_files_sync: no cleanup route for provider %s; "
            "session %s files retained",
            provider,
            session_id,
        )
        return False
    except Exception:
        logger.warning(
            "_cleanup_session_files_sync: unexpected error cleaning session %s",
            session_id,
            exc_info=True,
        )
        return False


# ── helpers ──────────────────────────────────────────────────────────


def _atomic_write(path: Path, data: dict) -> None:
    """Write JSON atomically via temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with open(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
