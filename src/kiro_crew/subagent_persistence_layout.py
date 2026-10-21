"""Where a subagent run's records live, and the cleanup identities a later sweep trusts.

The registry root (:data:`_SUBAGENTS_DIR` overrides the data home's), a run's folder
(:func:`_agent_dir`, resolved, and :func:`agent_dir_for_display`, in the home spelling a
reader uses), and the cleanup-identity records the stale-tombstone prune reads to remove
a run's provider session files. :func:`_atomic_write` is the owner-only JSON write the
run records go through. Every name is reached as
``kiro_crew.subagent_persistence.<name>``; the facade binds or serves it.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home
from kiro_crew.owner_only_files import ensure_directory

logger = logging.getLogger("kiro_crew.subagent_persistence")

# Resolved per call, never captured at import: an import-time binding freezes
# the data home and defeats pod isolation, the lazy legacy-home migration and
# test isolation. The name below is an opt-in override (None = live home) so
# existing monkeypatch call sites keep working. See config.md "Data Home";
# dashboard/handlers/usage_store.py is the reference implementation.
_SUBAGENTS_DIR: Path | None = None

# Promotion and prune arbitrate per agent; the weak lock registry is defined
# beside the state-writer registry in ``subagent_persistence_state`` so unrelated
# conversations never couple.
SUBAGENT_CONVERSATION_PREFIX = "subagent:"
_CLEANUP_IDENTITIES_FILE = "cleanup-identities.json"
_CLEANUP_IDENTITIES_TRUST_DIR = "subagent-cleanup-identities"
_CLEANUP_IDENTITY_LOCK = threading.Lock()
_LIVE_CLEANUP_IDENTITIES: dict[str, list[dict[str, object]]] = {}
_LIVE_CLEANUP_HINTS: set[str] = set()


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


# Named by the path callers import them from: the run coordinator binds both as
# ``kiro_crew.subagent_persistence`` helpers.
publish_live_cleanup_identity.__module__ = "kiro_crew.subagent_persistence"
remember_live_cleanup_identity.__module__ = "kiro_crew.subagent_persistence"


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

    On a host whose home is itself a symlink the two spellings differ. A cloud
    desktop's ``/home/<user> -> /local/home/<user>`` is the ordinary case,
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


# ── helpers ──────────────────────────────────────────────────────────


def _atomic_write(path: Path, data: dict) -> None:
    """Write JSON atomically via temp file + rename, fsynced, owner-only."""
    ensure_directory(path.parent)
    atomic_write(path, json.dumps(data, ensure_ascii=False), fsync=True, mode=0o600)
