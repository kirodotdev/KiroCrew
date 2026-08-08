"""Subagent persistence — disk I/O for agent folders.

Each subagent gets a folder at ``~/.kiro/crew/subagents/{id}/`` containing:
- ``state.json``   — running state (task, PID, turns, last_tool)
- ``result.txt``   — streamed result text
- ``tombstone.json`` — written on abnormal exit only
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

from kiro_crew.config.paths import data_home, kiro_sessions_dir
from kiro_crew.providers.cleanup import _is_safe_path

logger = logging.getLogger(__name__)

# Resolved per call, never captured at import: an import-time binding freezes
# the data home and defeats pod isolation, the lazy legacy-home migration and
# test isolation. The name below is an opt-in override (None = live home) so
# existing monkeypatch call sites keep working. See config.md "Data Home";
# dashboard/handlers/usage.py is the reference implementation.
_SUBAGENTS_DIR: Path | None = None


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
    """Read state.json. Returns None on missing/corrupt."""
    try:
        p = _agent_dir(agent_id) / "state.json"
    except ValueError:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def update_state(agent_id: str, **fields: object) -> None:
    """Merge *fields* into state.json (atomic rewrite)."""
    p = _agent_dir(agent_id) / "state.json"
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        logger.debug("update_state: cannot read state for %s, skipping", agent_id)
        return
    state.update(fields)
    state["updated_at"] = time.time()
    _atomic_write(p, state)


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
        **extra,
    }
    try:
        _atomic_write(d / "tombstone.json", tombstone)
    except OSError:
        logger.warning("write_tombstone failed for %s", agent_id, exc_info=True)


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


def record_slow_command(agent_id: str, **fields: object) -> None:
    """Append a stalled subagent's slow command to ``slow_commands.jsonl``.

    Unlike :func:`write_tombstone`, this does NOT mark the agent dead — a
    stalled subagent is still running; the record is purely for later analysis
    of which commands run slow. Append-only, at the subagents-dir root so it
    survives per-agent folder cleanup. Best-effort: never raises to the caller.
    """
    entry = {"id": agent_id, "flagged": time.time(), **fields}
    base = _subagents_dir()
    try:
        base.mkdir(parents=True, exist_ok=True)
        with open(base / "slow_commands.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        logger.warning("record_slow_command failed for %s", agent_id, exc_info=True)


# ── delete ───────────────────────────────────────────────────────────


def delete_agent_folder(agent_id: str) -> None:
    """Remove the entire agent directory."""
    d = _agent_dir(agent_id)
    shutil.rmtree(d, ignore_errors=True)


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
            ts = json.loads(ts_path.read_text(encoding="utf-8"))
            cutoff = delivered_cutoff if ts.get("cause") == "delivered" else default_cutoff
            if ts.get("died", 0) < cutoff:
                # Best-effort session cleanup — must not block folder removal
                try:
                    state = read_state(d.name)
                    session_id = ts.get("session_id") or (state.get("session_id", "") if state else "")
                    provider = ts.get("provider") or (state.get("provider", "acp") if state else "acp")
                    cwd = ts.get("cwd") or (state.get("cwd", "") if state else "")
                    # keep=True conversations retain their session files as
                    # resume material for spawn_continue; the conversation
                    # TTL sweep (SubagentManager reaper) owns their deletion.
                    _keep = bool(state.get("keep")) if state else False
                    if session_id and not _keep:
                        _cleanup_session_files_sync(session_id, provider, cwd=cwd)
                except Exception:
                    logger.debug("prune: session cleanup failed for %s", d.name, exc_info=True)
                shutil.rmtree(d, ignore_errors=True)
                pruned += 1
        except (json.JSONDecodeError, OSError):
            logger.debug("prune: skipping corrupt tombstone in %s", d.name)
    return pruned


# ── session file cleanup ──────────────────────────────────────────────


def _cleanup_session_files_sync(
    session_id: str, provider: str = "acp", *, cwd: str = ""
) -> None:
    """Delete LLM provider session files for a completed subagent.

    Synchronous — used during tombstone pruning (which runs in the reaper loop).
    Best-effort: logs warnings on failure, never raises.

    For ``claude_code`` provider, *cwd* is required to derive the CC
    project directory name.  If not provided, cleanup is skipped with a
    debug-level log (the files will leak until manual deletion).
    """
    if not session_id or session_id in (".", ".."):
        return
    try:
        if provider == "acp":
            sessions_dir = kiro_sessions_dir()
            for suffix in (".json", ".jsonl"):
                target = sessions_dir / f"{session_id}{suffix}"
                if not _is_safe_path(target, sessions_dir):
                    logger.error(
                        "_cleanup_session_files_sync: path traversal blocked for %s",
                        target,
                    )
                    return
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    logger.warning(
                        "_cleanup_session_files_sync: failed to delete %s",
                        target,
                        exc_info=True,
                    )
    except Exception:
        logger.warning(
            "_cleanup_session_files_sync: unexpected error cleaning session %s",
            session_id,
            exc_info=True,
        )


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
