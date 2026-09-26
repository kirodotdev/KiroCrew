"""Subagent persistence — disk I/O for agent folders.

Each subagent gets a folder at ``~/.kiro/crew/subagents/{id}/`` containing:
- ``state.json``   — running state (task, PID, turns, last_tool)
- ``result.txt``   — streamed result text
- ``tombstone.json`` — written on abnormal exit only
"""

from __future__ import annotations

import asyncio
import heapq
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import weakref
from collections.abc import Collection
from enum import Enum
from pathlib import Path
from typing import Callable, NamedTuple

from kiro_crew import platform_compat
from kiro_crew.acp.types import PROVIDER_LABEL_DEFAULT
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home, kiro_sessions_dir
from kiro_crew.execution_context import ExecutionContext
from kiro_crew.jsonl_util import rotate_jsonl_at
from kiro_crew.providers.cleanup import _is_safe_path
from kiro_crew.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls

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
_LIVE_RUN_STATES: dict[tuple[str, str], dict] = {}


def _live_run_key(agent_id: str) -> tuple[str, str]:
    return str(_subagents_dir()), agent_id


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


# ── panel dismissals ─────────────────────────────────────────────────

# One file per dismissed run, named for the run, so the whole set is one
# directory listing and two dismissals never contend over a shared file.
#
# A top-level leaf of the data home, NOT a subdirectory of ``trust``. The same
# name is registered in ``security/paths.py`` and in three lists in
# ``sandbox.py``; a rename here without those is a silently unprotected store,
# which ``test_subagent_panel_durable_replay.py`` pins by asserting this
# constant is present in each.
_PANEL_DISMISSAL_LEAF = "panel-dismissals"


def _panel_dismissals_dir() -> Path:
    """Gateway-owned home for panel dismissals, outside the run folders.

    A dismissal decides what the panel HIDES, so neither the run it is about nor
    any other sandboxed process may write one. That makes it an OWNER-authority
    record, and this repository already settled where those live: ``crew-panels``,
    ``crew-teams`` and ``tag-grants`` are each their own top-level leaf
    specifically because ``trust`` is a declared sandbox READ-WRITE exception
    (in-sandbox ``verify_session_pid`` reads ``trust/sel_hmac.key`` and the
    in-sandbox MCP servers append to the audit log). A record under ``trust``
    stays writable by a sandboxed command that builds the path at runtime, which
    command matching cannot catch because it has no literal path to match. So
    this is a sibling of those three, not of the cleanup identities that predate
    the rule, and it is registered in all four places one requires:
    ``paths.SENSITIVE_*`` for the file gate, ``sandbox._CREW_HIDDEN_LEAVES`` for
    the bind mask, ``_CREW_PRECREATE_HIDDEN_DIR_LEAVES`` because the mask loop
    SKIPS a name that does not exist yet and this directory is created on the
    first dismissal, and ``_CREW_NO_ALIAS_LEAVES`` because a link would attach
    the mask to the target while leaving the name writable.

    ``tombstone.json`` and ``state.json``, by contrast, sit in the agent-writable
    run folder, where a run could hide its own card from every later rebuild.

    Resolved through ``_subagents_dir().parent`` rather than ``data_home()``
    directly, which is this module's existing way of naming a data-home sibling
    (see the member-memory bindings): in production the two are the same path,
    and only the former follows the ``_SUBAGENTS_DIR`` test hook, so a test's
    registry and its dismissals cannot end up in different homes.
    """
    return _subagents_dir().parent / _PANEL_DISMISSAL_LEAF


def _panel_dismissal_path(agent_id: str) -> Path:
    """The record's path, for an id that is safe to spell as a file name.

    The canonical agent-directory guard settles traversal, and the remaining
    charset rule is about the FILE this id names: a run id reaches here from the
    route's URL, and ``:`` or a trailing dot is a legal folder name on POSIX and
    an illegal file name on Windows. Ids are hex from ``_mint_agent_id``, so the
    rule costs a real dismissal nothing.
    """
    _agent_dir(agent_id)
    if len(agent_id) > _PANEL_ID_CAP or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", agent_id):
        raise ValueError(f"Invalid agent_id for a dismissal record: {agent_id!r}")
    return _panel_dismissals_dir() / agent_id


def record_panel_dismissal(agent_id: str) -> bool:
    """Record that a finished run's panel card was dismissed. True when stored.

    A dismissal has to be durable because the panel's durable half reads run
    FOLDERS: dropping the run from the manager hid the card only until the next
    rebuild, which found the folder the delete left behind and sent the card
    again. This record is what that reader consults.

    The folder itself is deliberately left alone. ``spawn_continue`` reseeds its
    session map from ``state.json`` in there, so deleting a folder to hide a card
    would also destroy a conversation the user can still continue, and would take
    the run's result text and its prune bookkeeping with it.

    False when the run has no folder: nothing durable can resurrect that card, so
    there is nothing to suppress, and a record for it would outlive every path
    that reclaims one.
    """
    try:
        if not _agent_dir(agent_id).is_dir():
            return False
        path = _panel_dismissal_path(agent_id)
    except (OSError, ValueError):
        return False
    try:
        directory = path.parent
        directory.mkdir(parents=True, exist_ok=True)
        platform_compat.make_owner_only_dir(directory)
        platform_compat.restrict_dir_to_owner(directory)
        # ``atomic_write``, not a write-then-restrict pair: it applies the
        # owner-only lockdown to the TEMP file before any content reaches it and
        # before the rename, so the record never exists in a world-readable file.
        # ``scripts/check_lockdown_before_publish.py`` enforces that ordering.
        # Every failure it raises -- a lockdown that cannot be applied, and the
        # linked-parent refusal ``restrict_to_owner`` implies -- is an OSError,
        # so the fail-open contract below still covers all of them.
        atomic_write(
            path,
            json.dumps({"id": agent_id, "at": time.time()}),
            restrict_to_owner=True,
        )
    except OSError:
        # The card returns on the next rebuild, and no reader can explain why, so
        # the operator log carries the reason. Raising instead would leave the run
        # gone from the manager and still undismissed -- strictly worse.
        logger.warning("panel dismissal not recorded for %s", agent_id, exc_info=True)
        return False
    return True


def dismissed_panel_ids() -> frozenset[str]:
    """Run ids whose panel card the user dismissed.

    Fails OPEN. An unreadable record yields no id, so a filesystem fault
    resurrects a dismissed card -- which the user can dismiss again -- rather
    than hiding a run the operator never dismissed. Hiding is the direction that
    must not happen by accident.
    """
    try:
        scan = os.scandir(_panel_dismissals_dir())
    except OSError:
        return frozenset()
    found: set[str] = set()
    with scan:
        for entry in scan:
            try:
                if entry.is_file():
                    found.add(entry.name)
            except OSError:
                continue
    return frozenset(found)


def _delete_panel_dismissal(agent_id: str) -> None:
    """Drop the record once its run folder is gone, so the set stays bounded."""
    try:
        _panel_dismissal_path(agent_id).unlink(missing_ok=True)
    except (OSError, ValueError):
        logger.debug("panel dismissal cleanup failed for %s", agent_id, exc_info=True)


def prune_orphan_panel_dismissals() -> int:
    """Drop records whose run folder is already gone. Returns the count removed.

    The two folder-removal paths delete a run's record with the folder, so this
    answers for a folder that left by some other route -- an operator clearing the
    registry by hand, a restore from a backup taken before the dismissal.

    Unlinks by DIRECTORY ENTRY rather than by rebuilding the path from the name: a
    name this directory holds but the id rule rejects would otherwise be
    unremovable, and counted as removed on every cycle. A scandir name is always
    one path component, so the entry's own path needs no traversal check.
    """
    removed = 0
    try:
        scan = os.scandir(_panel_dismissals_dir())
    except OSError:
        return 0
    with scan:
        for entry in scan:
            try:
                if not entry.is_file():
                    continue
                if _agent_dir(entry.name).is_dir():
                    continue
            except OSError:
                continue
            except ValueError:
                pass  # not an id any run could carry, so no folder can claim it
            try:
                os.unlink(entry.path)
            except OSError:
                logger.debug("panel dismissal sweep failed for %s", entry.name, exc_info=True)
                continue
            removed += 1
    return removed


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


def read_run_agent_selection(agent_id: str) -> tuple[str, str]:
    execution = read_run_execution(agent_id)
    return execution.selection_kind, (
        execution.selection_name if execution.selection_kind == "member" else execution.template_id
    )


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
        "status": "running",
        "pid": None,
        "turns": 0,
        "last_tool": "",
        "context_groups": context_groups,
        "delegation": dict(delegation or {}),
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
        d.mkdir(parents=True, exist_ok=True)
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
        p = _agent_dir(agent_id) / "state.json"
        state = json.loads(p.read_text(encoding="utf-8"))
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
    fsync. The remaining on-loop callers are the synchronous retention writers;
    they still pay their own fsync on the loop, and moving that I/O while keeping
    their ``SessionMap`` mutation on-loop remains outstanding.
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
    if _live_run_key(agent_id) in _LIVE_RUN_STATES:
        return
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
    if _live_run_key(agent_id) in _LIVE_RUN_STATES:
        return
    d = _agent_dir(agent_id)
    state = read_state(agent_id)
    # A delayed delivery acknowledgement cannot recreate a released transient
    # run. Abnormal-exit diagnostics may still record missing state.
    if state is None and cause == "delivered":
        return
    state = state or {}
    execution = state.get("execution_context")
    if isinstance(execution, dict) and execution.get("memory_mode") in ("incognito", "temporary"):
        return
    cleanup_identity = {
        key: state[key] for key in ("session_id", "provider", "cwd") if state.get(key)
    }
    live_cleanup_identities = _live_cleanup_identities(agent_id)
    latest_live_identity = live_cleanup_identities[-1] if live_cleanup_identities else {}
    generation_metadata: dict[str, object] = {}
    if live_cleanup_identities:
        generation_metadata["cleanup_identities"] = live_cleanup_identities
    # A terminal outcome, once recorded, is not erased by a later write that does
    # not carry one. This write REPLACES the file, and only ``extra`` can supply
    # ``outcome``, so a caller that has no opinion about the outcome -- a delivery
    # acknowledgement is the ordinary one -- would otherwise drop the outcome an
    # earlier write recorded, and the reader would fall back to deriving one from
    # ``cause``. A user stop then reads as a completion. Carried forward here, at
    # the single write, rather than left for each call site to remember.
    #
    # ``died`` is carried through the SAME read, on the same grounds, with one
    # difference: every write stamps it, so the carry is what keeps it meaning the
    # run's ENDING rather than the moment of the most recent bookkeeping write. A
    # delivery acknowledgement lands whenever the parent got round to the result,
    # and the panel reads this field as the end of the run, so an uncarried stamp
    # shows the delivery wait as runtime.
    #
    # The read is taken on exactly one condition, ``outcome`` absent, and that is
    # deliberate rather than incidental: a caller supplying an outcome is RECORDING
    # an ending, so its own fresh stamp IS that ending and there is nothing to
    # carry. Widening the condition to ``died`` absent as well would take the read
    # on every such caller -- including sync writers reached from a coroutine, where
    # this function's synchronous file I/O must not land.
    carried: dict[str, object] = {}
    if "outcome" not in extra:
        prior, _present = _read_tombstone_at(d / "tombstone.json")
        if isinstance(prior, dict):
            if prior.get("outcome"):
                carried["outcome"] = prior["outcome"]
            if "died" not in extra and _finite_time(prior.get("died")):
                carried["died"] = prior["died"]
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
        **carried,
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


def settle_delivered_batch(
    agent_ids: "Collection[str]",
    *,
    writer: "Callable[[str], None] | None" = None,
) -> None:
    """Mark every id in *agent_ids* delivered, as ONE indivisible operation.

    For a caller that has already detached its ids from the state that held
    them: awaiting :func:`mark_delivered` once per id makes each write a
    cancellation point, and ``CancelledError`` is not an ``Exception``, so a
    shutdown or a cancel arriving mid-batch discards the ids it has not reached
    with nothing left holding them. Each unwritten tombstone leaves a folder that
    restart reconciliation admits, which replays as a duplicate completion.
    Offloaded whole by such a caller, this runs to the end of the batch whether or
    not its waiter is still waiting.

    ``writer`` is the write to perform per id, defaulting to
    :func:`mark_delivered`. A caller inside the subagent manager passes its OWN
    resolved ``mark_delivered`` instead, because the manager's modules resolve
    that name through the facade and the facade is the single point anything
    substituting the write replaces -- a batch that reached past it would be the
    one delivery path that ignored the substitution.

    A failing write is logged and skipped rather than raised, so one unwritable
    run folder cannot strand the rest of the batch.
    """
    write = mark_delivered if writer is None else writer
    for agent_id in agent_ids:
        try:
            write(agent_id)
        except Exception:
            logger.debug("Failed to settle delivered subagent %s", agent_id, exc_info=True)


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


def forget_live_run_state(agent_id: str) -> None:
    """Release transient run state after its lifecycle writers have settled."""
    _LIVE_RUN_STATES.pop(_live_run_key(agent_id), None)


def delete_agent_folder(agent_id: str) -> None:
    """Remove the entire agent directory and its in-process identity fallback."""
    d = _agent_dir(agent_id)
    forget_live_run_state(agent_id)
    with _CLEANUP_IDENTITY_LOCK:
        shutil.rmtree(d, ignore_errors=True)
        if not d.exists():
            _delete_cleanup_identities_file(agent_id)
            _delete_panel_dismissal(agent_id)
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


#: The one tombstone cause that marks a run whose result reached its parent.
_DELIVERED_CAUSE = "delivered"

#: Named caps on every string a panel record RETAINS. ``keep`` bounds the ROW
#: count only, and a bounded number of unbounded rows is unbounded, so each
#: retained field carries its own limit and a cut field says that it was cut.
_PANEL_TASK_CAP = 2000
_PANEL_AGENT_CAP = 200
_PANEL_ERROR_CAP = 500
_PANEL_RESULT_CAP = 3000
_PANEL_TRUNC_MARKER = " ...(truncated)"

#: Extra bytes read past ``_PANEL_RESULT_CAP`` before redaction runs, so a
#: credential straddling the cut is present whole when it is matched. Bounded on
#: purpose: the alternative is reading the file whole, which the cap exists to
#: refuse. Wide enough for any single token the redactors recognise.
_PANEL_REDACT_MARGIN = 1024

#: Ceilings on the record's EQUALITY KEYS. These REFUSE the record rather than
#: clamping it, because each one is compared for equality and a truncated key
#: would compare unequal while still looking like a value: the id addresses the
#: run folder, the app is matched against a slot's current owner, and the parent
#: session is matched against a caller and resolved into a slot key.
#:
#: Every string a record retains is accounted for here or clamped above, and the
#: two lists are the whole record: ``id``, ``app`` and ``parent_session`` are
#: keys; ``task``, ``agent``, ``error`` and ``result`` are text; ``started``,
#: ``elapsed`` and ``stopped`` are numbers and a flag; ``outcome`` is one of
#: three fixed words. A field added to the record belongs in one of the two.
_PANEL_ID_CAP = 255
_PANEL_APP_CAP = 200
_PANEL_PARENT_SESSION_CAP = 400


def _panel_keys_plausible(agent_id: str, app: str, parent_session: str) -> bool:
    """Whether every equality key on a record is a plausible size.

    One predicate over all three, so a key added to the record is added here too
    rather than acquiring its own scattered guard.
    """
    return all(
        len(value) <= cap
        for value, cap in (
            (agent_id, _PANEL_ID_CAP),
            (app, _PANEL_APP_CAP),
            (parent_session, _PANEL_PARENT_SESSION_CAP),
        )
    )


#: Ceiling on the scan's own working set, as a multiple of ``keep``. The walk
#: retains at most this many folder candidates at a time, so a registry holding
#: hundreds of thousands of folders costs what one holding a hundred costs.
#: Above ``keep`` so a run of skipped folders still leaves depth to fill a page.
_PANEL_CANDIDATE_MULTIPLE = 4

#: Terminal values a tombstone may record, and which of them is a user stop.
_PANEL_OUTCOMES = ("completed", "failed", "stopped")


def _clamp(text: str, cap: int) -> str:
    """Return *text* within *cap*, ending in the marker when it had to be cut.

    The marker IS the truncation report: it travels inside the value to every
    reader, so no separate flag has to be carried and kept in step.
    """
    if len(text) <= cap:
        return text
    return text[:cap] + _PANEL_TRUNC_MARKER


def _redact_then_clamp(text: str, cap: int) -> str:
    """Redact the WHOLE value, then clamp it.

    Order matters and this is the order the native card path already requires: a
    credential cut at the cap first loses the tail its pattern needs, so the
    consumers' own redaction cannot match the fragment that survives. Redacting
    before the cut leaves nothing for the boundary to split.
    """
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return _clamp(text, cap)


def _record_memory_mode(state: dict) -> str:
    """The run's memory mode, from the top-level field or the execution record.

    Answers ``""`` when neither spells one, which every caller treats as "not
    persistent". Failing closed here costs at most one card; failing open would
    put an incognito run's task text on screen.
    """
    mode = state.get("memory_mode")
    if isinstance(mode, str) and mode:
        return mode
    execution = state.get("execution_context")
    if isinstance(execution, dict):
        nested = execution.get("memory_mode")
        if isinstance(nested, str) and nested:
            return nested
    return ""


def _record_app(state: dict) -> str:
    """The app that executed the run, from the top level or the execution record.

    Answers ``""`` for a run no app owns, which is a real value and not a
    fallback: a slot a person created has no app owner either, so the two compare
    equal and a human run replays to a human slot.
    """
    app = state.get("app")
    if isinstance(app, str) and app:
        return app
    execution = state.get("execution_context")
    if isinstance(execution, dict):
        nested = execution.get("app")
        if isinstance(nested, str) and nested:
            return nested
    return ""


def _finite_time(value: object) -> float:
    """A finite, non-negative timestamp, or ``0.0`` for anything unusable."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")) or number < 0:
        return 0.0
    return number


def classify_persisted_ending(agent_dir: Path) -> tuple[str, str, bool]:
    """How a run on disk ended, as ``(outcome, error, stopped)``.

    The ONE classification site for a persisted run, shared by the panel list
    and the single-card read, so one folder cannot read "completed" in a list
    and "Orphaned: delivered" when it is opened.

    Takes the run's resolved folder rather than its id, so a caller that already
    resolved it -- every caller does, to reach ``result.txt`` -- resolves it once
    for the whole response instead of once per field.

    The tombstone's own ``outcome`` is preferred over its ``cause``, because the
    writer records the outcome directly: a user stop is ``stopped`` there, while
    deriving from ``cause`` alone reports that routine stop as a failure.
    ``detail`` carries the specific reason where ``cause`` is a coarse bucket.

    An absent tombstone means NOTHING recorded an ending, which is not an outcome
    and must not be reported as one: calling it ``completed`` would show a green
    terminal card for a run the restart killed, contradicting the orphan notice
    injected for that same run. It answers ``""`` instead, and a caller that
    needs an outcome skips the folder until an ending is on disk. The orphan
    reconciler writes one, but it yields between folders, so a tab reconnecting
    mid-scan observes this state rather than racing past it. A tombstone that is
    PRESENT but unreadable is the opposite case: an ending was recorded and
    cannot be read, which is reported as an unknown cause.
    """
    tombstone, present = _read_tombstone_at(agent_dir / "tombstone.json")
    if tombstone is None:
        if present:
            return "failed", "Orphaned (unknown cause)", False
        return "", "", False
    recorded = tombstone.get("outcome")
    cause = str(tombstone.get("cause") or "")
    if recorded in _PANEL_OUTCOMES:
        outcome = str(recorded)
    elif cause == _DELIVERED_CAUSE:
        outcome = "completed"
    else:
        outcome = "failed"
    if outcome == "stopped":
        # A stop is not an error, and the consumers drop error text for it.
        return outcome, "", True
    if outcome == "completed":
        return outcome, "", False
    detail = str(tombstone.get("detail") or "")
    return outcome, detail or f"Orphaned: {cause or 'unknown'}", False


def _panel_result_text(agent_dir: Path) -> str:
    """Capped result text for a panel record, or ``""`` when unreadable."""
    path = agent_dir / "result.txt"
    if is_sensitive_path(str(path)):
        return ""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            # Read a bounded MARGIN past the cap so a credential straddling the
            # cut is present whole when redaction runs, and one byte past that so
            # a value exactly at the cap is not marked as cut. The margin keeps
            # the read bounded; nothing here reads the file whole.
            text = handle.read(_PANEL_RESULT_CAP + _PANEL_REDACT_MARGIN + 1)
    except OSError:
        return ""
    return _redact_then_clamp(text, _PANEL_RESULT_CAP)


def _result_written_at(agent_dir: Path) -> float:
    """When the run's own output last reached disk, or ``0.0`` when it never did.

    The run writes ``result.txt`` itself as its output streams, and the stream's
    completion caps that file in the same step, so its last write is the moment
    the run stopped producing. Delivery and retention only read it or remove it,
    which is what makes the timestamp the run's and not a later caller's. A run
    that produced no output has no file, and the caller falls back to the
    tombstone.
    """
    try:
        return _finite_time((agent_dir / "result.txt").stat().st_mtime)
    except OSError:
        return 0.0


def _panel_record(agent_id: str, *, cutoff: float, include_result: bool) -> dict | None:
    """One panel record, or ``None`` when the folder supplies no usable one."""
    state = read_state(agent_id)
    if state is None:
        return None
    if _record_memory_mode(state) != "persistent":
        return None
    parent_session = str(state.get("parent_session") or "")
    if not parent_session:
        # Without an owning conversation there is no slot to route a frame to,
        # and an empty slot is read by older clients as the active one.
        return None
    try:
        agent_dir = _agent_dir(agent_id)
    except ValueError:
        return None
    app = _record_app(state)
    if not _panel_keys_plausible(agent_id, app, parent_session):
        # An equality key this size is refused outright: clamping one would make
        # it compare unequal while still reading as a value.
        return None
    started = _finite_time(state.get("started"))
    tombstone, _present = _read_tombstone_at(agent_dir / "tombstone.json")
    ended = _finite_time((tombstone or {}).get("died")) or _finite_time(state.get("updated_at"))
    # The end of the RUN, not of the bookkeeping about it. A tombstone's ``died``
    # is carried across writes, so where a write happened at the ending it is the
    # ending. The queued and digest-held paths have no write at completion at all
    # -- the delivery acknowledgement is their FIRST tombstone -- and it lands
    # whenever the parent got round to the result, so on those the stamp is a wait
    # this panel would otherwise show as runtime. The run's own output file is the
    # completion evidence that exists on every path that produced output, and the
    # earlier of the two is the one the run itself wrote. Only the reported
    # duration uses it: the retention window below stays on the latest timestamp,
    # because a run whose result reached its parent moments ago belongs in the
    # panel whenever it happens to have finished.
    written = _result_written_at(agent_dir)
    ran_until = min(ended, written) if (ended and written) else (ended or written)
    if max(started, ended) < cutoff:
        return None
    outcome, error, stopped = classify_persisted_ending(agent_dir)
    if not outcome:
        # No ending is recorded yet, so this run has no outcome to show. A card
        # now could only carry an invented one; the folder is picked up on a
        # later read, once the reconciler has written its ending.
        return None
    record: dict = {
        "id": agent_id,
        "task": _redact_then_clamp(str(state.get("task") or ""), _PANEL_TASK_CAP),
        "agent": _redact_then_clamp(str(state.get("agent") or ""), _PANEL_AGENT_CAP),
        "app": app,
        "parent_session": parent_session,
        "started": started,
        "elapsed": ran_until - started if ran_until > started else 0.0,
        "outcome": outcome,
        "error": _redact_then_clamp(error, _PANEL_ERROR_CAP),
        "stopped": stopped,
    }
    if include_result:
        record["result"] = _panel_result_text(agent_dir)
    return record


class PanelRecords(NamedTuple):
    """One panel rebuild's records, plus what the count bound left out.

    The overflow is carried out with the records rather than dropped, because a
    truncated tail is otherwise indistinguishable from a population that never
    held those runs -- a panel showing 50 of 51 looks exactly like a panel showing
    all 50 there were. Every consumer surfaces it once per rebuild.

    ``overflow_is_lower_bound`` says the scan's own candidate window filled up, so
    more admissible folders may exist past it and the count is a floor.
    """

    records: list[dict]
    overflow: int
    overflow_is_lower_bound: bool


def read_panel_records(
    *,
    keep: int,
    max_age_secs: float,
    exclude_ids: Collection[str] = (),
    include_result: bool = False,
    admit: "Callable[[dict], bool] | None" = None,
) -> PanelRecords:
    """Bounded, newest-first run records for rebuilding a subagent panel.

    A panel's live source is gateway memory, so a replacement gateway process
    has nothing to show for the runs it never tracked. These records are the
    durable half of that answer: one entry per run folder, carrying the fields a
    terminal panel frame needs.

    ``exclude_ids`` carries the ids the caller's live manager already holds.
    Those folders are skipped, never merged: a live entry is the authority on its
    own run, and a record here describes only a run nobody is tracking. Runs the
    user DISMISSED are skipped on the same terms, from the durable record the
    delete route leaves -- without it, dropping a run from the manager hid its
    card only until this reader found the folder again.

    ``admit`` is the caller's OWN visibility filter, and it runs before the cap
    rather than after it. Filtering afterwards lets a record the caller may not
    see occupy a slot its own runs need, and makes the overflow count describe a
    population that is not the caller's -- which discloses how many foreign runs
    exist. Applied here, both the cap and the count are the caller's own.

    ``keep`` and ``max_age_secs`` bound different things -- the size of the burst
    a caller must deliver at once, and how far back a rebuild reaches. Ages are
    measured against the times the run itself recorded. Because a row count is
    not a memory bound, every retained string is clamped to its own named cap and
    the three equality keys refuse an oversized record outright. What ``keep``
    cut is COUNTED and returned, never silently dropped.

    A run whose memory mode is not ``persistent`` keeps its state in memory and
    writes no folder, so it cannot be discovered here; a folder whose record
    spells a non-persistent mode is skipped explicitly too, so the exclusion
    holds whichever way the folder came to exist.

    Blocking. A caller on the event loop must wrap this in
    :func:`asyncio.to_thread`.
    """
    if keep <= 0:
        return PanelRecords([], 0, False)
    cutoff = time.time() - max_age_secs if max_age_secs > 0 else float("-inf")
    # Two exclusions, one set, both applied before the heap below. A dismissed
    # folder filtered out here cannot spend a candidate slot a visible run needs,
    # which a filter after the walk would let it do.
    skip = set(exclude_ids) | dismissed_panel_ids()
    # At least one candidate BEYOND ``keep`` has to fit, or the window could not
    # see the very overflow it has to report: a heap sized exactly ``keep``
    # admits every entry as a record and truncates in silence.
    candidate_cap = max(keep + 1, keep * _PANEL_CANDIDATE_MULTIPLE)
    # A bounded min-heap on mtime, so the walk's own working set is capped
    # whatever the registry holds. Folders outside the age window and ids the
    # caller already knows are dropped HERE, before they can occupy the heap or
    # cost a state read.
    newest: list[tuple[float, str]] = []
    saturated = False
    try:
        scan = os.scandir(_subagents_dir())
    except OSError:
        return PanelRecords([], 0, False)
    with scan:
        for entry in scan:
            try:
                if not entry.is_dir():
                    continue
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff or entry.name in skip:
                continue
            if len(newest) < candidate_cap:
                heapq.heappush(newest, (mtime, entry.name))
            else:
                saturated = True
                if mtime > newest[0][0]:
                    heapq.heapreplace(newest, (mtime, entry.name))
    records: list[dict] = []
    overflow = 0
    for _mtime, agent_id in sorted(newest, reverse=True):
        at_cap = len(records) >= keep
        # Past the cap the record itself is not wanted, only the answer to
        # "would this one have been shown", so the result read is skipped.
        record = _panel_record(
            agent_id, cutoff=cutoff, include_result=include_result and not at_cap
        )
        if record is None:
            continue
        if admit is not None and not admit(record):
            # The caller's OWN filter runs before the cap, not after it. A record
            # this caller may not see must not occupy a slot its own runs need,
            # and must not be counted as something the cap withheld from it --
            # that count would disclose how many foreign runs exist.
            continue
        if at_cap:
            overflow += 1
            continue
        records.append(record)
    # ``saturated`` stands on its own, NOT ANDed with the count. The candidate
    # window is a second bound and it closes on mtime BEFORE validity and
    # ``admit`` run, so a window filled entirely by records that are then
    # rejected yields a zero count while admissible older folders were never
    # inspected. ANDing the two would report that case as "nothing was cut",
    # which is the silence this bound is required not to keep.
    return PanelRecords(records, overflow, saturated)


# ── prune ────────────────────────────────────────────────────────────

# Preserve unknown retention state long enough to outlive the six-hour
# continuable-conversation window, then reclaim it using tombstone metadata.
_UNREADABLE_STATE_GRACE_SECS = 24 * 3600
_UNRECLAIMABLE_LOOKUP_MAX_AGE_SECS = 90 * 86400


def _tombstone_died(ts: dict[str, object], path: Path, now: float) -> int | float:
    """Return a finite, positive, non-future death time with bounded fallback."""
    died = ts.get("died")
    if isinstance(died, (int, float)) and not isinstance(died, bool) and 0 < died <= now:
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
            isinstance(died, (int, float)) and not isinstance(died, bool) and 0 < died <= now
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


def _cleanup_retention_fallback(agent_id: str, session_id: object) -> tuple[bool | None, str, str]:
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
                (
                    record_provider
                    if isinstance(record_provider, str) and record_provider
                    else PROVIDER_LABEL_DEFAULT
                ),
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
    # Once per cycle, and before the walk: a record whose folder left by some
    # other route has nothing left to suppress, and this sweep is what reclaims
    # it. Not added to ``pruned``, which counts folders.
    prune_orphan_panel_dismissals()
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
                            conversation_key = str(fallback_record.get("conversation_key") or "")
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
                    within_retry_window = died >= now - _UNRECLAIMABLE_LOOKUP_MAX_AGE_SECS
                    # Legacy/pre-upgrade runs can carry a SID only in the
                    # agent-writable state/tombstone. It is not safe deletion
                    # authority, but the folder is useful for a later trusted
                    # migration. Bound that lookup window so an unavailable
                    # migration cannot accumulate private run folders forever.
                    if lookup_session_id and not cleanup_identities and within_retry_window:
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
                            _delete_panel_dismissal(d.name)
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
