"""Run managed subagents on connected remote Kiro Crew instances.

The local gateway remains the control plane: it chooses a connected instance,
starts one silent peer run through the existing authenticated instance tunnel,
and polls that run until it reaches a terminal state.  The peer owns execution
and resource use; the local :class:`SubagentManager` owns the visible record and
parent-result delivery.

No peer credential crosses this module.  Every request goes through
``SshTunnelManager.proxy_request``, which adds the port-scoped owner cookie and
performs its bounded re-mint retry.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import kiro_crew
from kiro_crew.apps.version import versions_compatible
from kiro_crew.atomic_write import atomic_write
from kiro_crew.cloud.aws import AWSError
from kiro_crew.config.loader import data_home
from kiro_crew.context_management import apply_completion_keep
from kiro_crew.dashboard.peer_redaction import redact_peer_text
from kiro_crew.dashboard.remote_snapshot import (
    _project_read_check,
    _snapshot_project,
    _SnapshotRefused,
)
from kiro_crew.platform_compat import rmtree_force
from kiro_crew.subagent import SubagentInfo

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState
    from kiro_crew.subagent import SubagentManager

logger = logging.getLogger(__name__)

_REMOTE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}\Z")
_MAX_REPLY_BYTES = 8 * 1024 * 1024
_POLL_SECONDS = 2.0
_RETRY_SECONDS = 5.0
_MAX_NOT_FOUND_POLLS = 3
_PRUNE_INTERVAL_SECONDS = 60.0
# A peer that stays unreachable this long ends the shadow run with an error, so
# a deleted or dead crew cannot keep its parent busy forever.
_UNREACHABLE_DEADLINE_SECONDS = 30 * 60.0
# A peer approval id (``hub_approvals.ask_hub``) and how many one poll relays.
_APPROVAL_ID_RE = re.compile(r"^[a-f0-9]{16}\Z")
_MAX_RELAYED_APPROVALS = 8
# error_code of a run whose spawn approval was lost to a gateway stop.
_APPROVAL_LOST_CODE = "remote_spawn_approval_lost"
_RESULT_NOT_RETAINED = (
    "The run finished, but its incognito/temporary result was held only in memory "
    "and did not survive a gateway restart."
)
_APPROVAL_LOST = (
    "This gateway restarted while the remote run was waiting for approval, so "
    "nothing was sent to the crew."
)
_NEVER_STARTED = (
    "This gateway restarted before the remote crew confirmed the run, and the crew "
    "has no run under the name it was sent with, so it did not start there."
)


# Mapping writes run on worker threads (``asyncio.to_thread``), so a per-run
# read-merge-write needs a thread lock. A fixed stripe keeps the set bounded
# without per-run bookkeeping; two runs sharing a stripe only serialize.
_MAPPING_LOCKS = tuple(threading.Lock() for _ in range(32))


def _mapping_lock(run_id: str) -> threading.Lock:
    return _MAPPING_LOCKS[int(hashlib.sha256(run_id.encode("utf-8")).hexdigest(), 16) % 32]


def _records_root() -> Path:
    """Where remote-placed runs keep their mappings and results.

    A folder inside the local run registry, so the ``subagents`` seal covers it
    (agent file tools may not write there, the sandbox mounts it read-only) with
    no second protected leaf. ``remote`` is never a minted run id, which is hex,
    and every local registry walk skips a folder without its own ``state.json``.
    """
    return data_home() / "subagents" / "remote"


class RemoteSubagentError(RuntimeError):
    """A remote run could not be started or controlled."""

    def __init__(self, message: str, *, code: str, status: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        # True when the peer may hold the run: its spawn answer was lost or
        # unusable and the withdrawing DELETE was not confirmed either, so the
        # record was kept for reconciliation rather than dropped.
        self.unresolved = False


def _audit_remote(info: SubagentInfo, outcome: str) -> None:
    """Record an accepted remote spawn or cancel in the SEL trail.

    A local run logs ``outcome="spawned"`` when it starts and its cancel inside
    the manager; a remote child leaves this gateway's approval ceiling, so its
    placement and its stop must be just as visible. Same privacy rule as the
    local spawn event: the agent name is recorded only for a persistent run.
    Best-effort: the peer already holds the run, so an unloadable SEL must not
    turn an accepted spawn into an orphan nobody can cancel.
    """
    metadata: dict[str, object] = {
        "subagent_id": info.id,
        "executor": "remote",
        "instance_id": info.instance_id,
        "remote_id": info.remote_id,
    }
    if info.memory_mode == "persistent":
        metadata["agent"] = info.agent or "kirocrew"
    try:
        from kiro_crew.sel import sel

        sel().log_tool_invocation(
            session_key=info.parent_session_key or "",
            source="subagent",
            tool_name="spawn_run",
            outcome=outcome,
            metadata=metadata,
        )
    except Exception:
        logger.warning("SEL audit of remote subagent %s (%s) failed", info.id, outcome)


async def _read_reply(response: Any) -> bytes:
    """Read a peer reply to EOF under the reply cap.

    A single ``content.read(n)`` returns as soon as any bytes are buffered, so a
    reply that arrives in more than one chunk would be parsed truncated. The
    shared reader drains to EOF and keeps the ``len > cap`` over-cap sentinel.
    """
    from kiro_crew.dashboard.handlers._shared import read_capped_response

    return await read_capped_response(response, _MAX_REPLY_BYTES)


def _reached_parent(info: SubagentInfo) -> bool:
    """True only when the completion is in the parent's context now.

    ``report_external`` also returns True when the gateway parked the result in
    a wave digest or in the parent's slot queue. Those are not delivered yet; an
    acknowledgement there would make a restart skip a result the parent never
    saw. The settle that later delivers it acknowledges it instead
    (:meth:`RemoteSubagentService.acknowledge_delivered`).
    """
    return not (
        getattr(info, "_digest_held", False)
        or getattr(info, "_delivery_queued", False)
        # The gateway gave up on the injection (a channel or cron parent whose
        # attempts all failed) and swallowed it: the reporter still returns
        # True, but the parent never saw the result.
        or getattr(info, "_report_undelivered", False)
    )


def _state_name(status: object) -> str:
    value = getattr(status, "state", "")
    return str(getattr(value, "value", value) or "")


def _peer_is_connected(instances: Any, instance_id: str) -> bool:
    """True when the tunnel to *instance_id* is up. Any read failure is False."""
    try:
        return _state_name(instances.status(instance_id)) == "connected"
    except Exception:
        return False


async def _ensure_version_parity(instances: Any, instance_id: str) -> None:
    """Refuse a crew that does not run this hub's ``major.minor`` series.

    The spawn, poll and cancel bodies carry no version of their own, so a crew a
    feature release apart can drop a field this hub relies on. An unknown
    version cannot be proven compatible and is refused as well. The crew's
    version string is peer text, so it is redacted before it is bounded.
    """
    ok, value = await instances.peer_version(instance_id)
    local = kiro_crew.__version__
    if not ok:
        raise RemoteSubagentError(
            f"Could not confirm the remote crew's Kiro Crew version (this machine "
            f"runs {local}), so the run was not placed on it.",
            code="remote_version_unknown",
            status=409,
        )
    if not versions_compatible(local, value):
        shown = redact_peer_text(value)[:64]
        raise RemoteSubagentError(
            f"The remote crew runs Kiro Crew {shown} but this machine runs {local}. "
            f"A run is placed only on a crew at the same major.minor version.",
            code="remote_version_mismatch",
            status=409,
        )


async def _ensure_peer_supports(
    instances: Any, instance_id: str, *, memory_mode: str, approval_floor: str
) -> None:
    """Refuse a crew that does not advertise the tightening, BEFORE it gets the task.

    The ``applied`` echo on the spawn reply arrives only after the peer has the
    task text, and a peer that drops ``memory_mode`` has already written that
    text to its queue row by then. So the hub first reads the peer's
    ``/api/version`` ``spawn_enforces`` list, for every run. Every run needs
    ``run_id``: the hub names the run before it sends it, and a peer that ignores
    that name would, after a lost POST reply, leave the hub withdrawing an id the
    peer never used while the child runs on under another name and its
    completion is never delivered. So a peer that does not advertise it gets no
    task at all.
    """
    needed = {"run_id"}
    if memory_mode not in ("", "persistent"):
        needed.add("memory_mode")
    if approval_floor:
        # The floor's person is on this hub, so the peer must hand the run's
        # tool requests back instead of prompting where nobody is watching.
        needed.update(("approval_floor", "approval_relay"))
    try:
        ok, payload = await instances.peer_capability(instance_id, "/api/version")
    except Exception:
        ok, payload = False, None
    advertised = payload.get("spawn_enforces") if ok and isinstance(payload, dict) else None
    if not isinstance(advertised, list) or not needed <= {str(v) for v in advertised}:
        raise RemoteSubagentError(
            "The remote crew does not confirm it keeps the run name it is sent, "
            "or that it enforces this run's memory mode and approval floor, so the "
            "task was not sent; update it to this gateway's version.",
            code="remote_peer_unenforced",
            status=409,
        )


class RemoteSubagentService:
    """Proxy remote runs while preserving the local subagent wire contract."""

    def __init__(self, state: DashboardState, manager: SubagentManager) -> None:
        self._state = state
        self._manager = manager
        self._monitors: dict[str, asyncio.Task[None]] = {}
        # Spawns waiting for the parent's person, then placing; each starts its
        # run's monitor once placed.
        self._approvals: dict[str, asyncio.Task[None]] = {}
        # (local run id, peer approval id) -> the task asking this hub's person.
        self._relays: dict[tuple[str, str], asyncio.Task[None]] = {}
        # Approved runs between the approval and the peer's answer (snapshot,
        # upload, POST), and those of them a stop arrived for in that window.
        self._placing: set[str] = set()
        self._stops: set[str] = set()
        # Stops accepted during placement whose peer DELETE has not been
        # confirmed yet; the monitor re-sends it until the peer confirms.
        self._unconfirmed_stops: set[str] = set()
        self._closed = False
        self._tie_cursor = 0
        self._restore_task: asyncio.Task[None] | None = None
        self._restored = False
        self._last_prune = 0.0
        self._prune_lock = asyncio.Lock()
        binder = getattr(manager, "bind_external_placement", None)
        if callable(binder):
            binder(self)

    async def dismiss(self, info: SubagentInfo) -> bool:
        """Commit an operator dismissal of a finished run before it is forgotten."""
        if info.executor != "remote" or not info.done:
            return False
        await asyncio.to_thread(self._persist_info, info, delivered=True, dismissed=True)
        return True

    async def acknowledge_delivered(self, agent_ids: list[str]) -> None:
        """Acknowledge parked remote results the parent has now received."""
        external = getattr(self._manager, "_external_agents", {})
        for agent_id in agent_ids:
            info = external.get(agent_id)
            if info is None or info.executor != "remote" or not info.done:
                continue
            try:
                await asyncio.to_thread(self._persist_info, info, delivered=True)
            except Exception:
                logger.warning("Could not mark remote run %s delivered", agent_id, exc_info=True)

    @staticmethod
    def _run_dir(run_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{8}", run_id):
            raise ValueError("invalid local remote-run id")
        return _records_root() / run_id

    @classmethod
    def _persist_info(
        cls,
        info: SubagentInfo,
        *,
        delivered: bool,
        done: bool | None = None,
        dismissed: bool = False,
        accepted: bool = True,
        approved: bool = True,
    ) -> None:
        """Write *info*'s mapping; *done* overrides ``info.done`` for a commit
        that must land before the in-memory flag may flip. A *dismissed* mapping
        is never restored and is pruned like any delivered terminal one.
        ``accepted=False`` marks the record written before the peer was asked,
        ``approved=False`` one still waiting for the parent's person.

        Writes for one run are serialized, and a committed dismissal is sticky:
        a delivery acknowledgement that was already in flight when the operator
        dismissed the run must not write ``dismissed: false`` back over it."""
        run_dir = cls._run_dir(info.id)
        with _mapping_lock(info.id):
            if not dismissed:
                dismissed = cls._stored_dismissed(run_dir)
            cls._write_mapping(
                run_dir,
                info,
                delivered=delivered,
                done=done,
                dismissed=dismissed,
                accepted=accepted,
                approved=approved,
            )

    @classmethod
    def _discard_mapping(cls, run_id: str) -> bool:
        """Remove the mapping of a run the peer never started; False if it stays."""
        run_dir = cls._run_dir(run_id)
        with _mapping_lock(run_id):
            return not run_dir.exists() or rmtree_force(run_dir)

    @staticmethod
    def _stored_accepted(run_dir: Path) -> bool:
        """Whether the peer confirmed the run; an unreadable or older record says yes."""
        try:
            raw = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return True
        return not (isinstance(raw, dict) and raw.get("accepted") is False)

    @staticmethod
    def _stored_dismissed(run_dir: Path) -> bool:
        try:
            raw = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return isinstance(raw, dict) and raw.get("dismissed") is True

    @staticmethod
    def _write_mapping(
        run_dir: Path,
        info: SubagentInfo,
        *,
        delivered: bool,
        done: bool | None,
        dismissed: bool,
        accepted: bool = True,
        approved: bool = True,
    ) -> None:
        run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        state = {
            "version": 1,
            "updated": time.time(),
            "id": info.id,
            "executor": "remote",
            "instance_id": info.instance_id,
            "remote_id": info.remote_id,
            # False only for the record written BEFORE the peer is asked: the run
            # is named first, so a restart in between finds it under that name.
            "accepted": accepted,
            # False while the parent's person has not yet approved the spawn:
            # nothing has left this host, and a restart ends the run as such.
            "approved": approved,
            "parent_session": info.parent_session_key,
            # Incognito/temporary runs keep their task and result in memory
            # only, as the local path does; the mapping itself is still needed
            # to recover and cancel the peer run after a restart.
            "task": info.task if info.memory_mode == "persistent" else "",
            "agent": info.agent,
            "started": info.started,
            "done": info.done if done is None else done,
            "delivered": delivered,
            "dismissed": dismissed,
            "error": info.error,
            "stopped": info.user_stopped,
            "stop_reason": info.stop_reason,
            "stop_class": info.stop_class,
            "partial": info.partial,
            "batch_id": info.batch_id,
            "batch_total": info.batch_total,
            "max_turns": info.max_turns,
            "cwd": info.cwd,
            "model": info.model,
            "reasoning_effort": info.reasoning_effort,
            "include_memory": info.include_memory,
            "include_lessons": info.include_lessons,
            "include_project": info.include_project,
            "memory_mode": info.memory_mode,
            "approval_floor": info.approval_floor,
            "silent": info.silent,
        }
        atomic_write(
            run_dir / "state.json",
            json.dumps(state, sort_keys=True).encode("utf-8"),
            fsync=True,
            restrict_to_owner=True,
        )

    @classmethod
    def _prune_records(cls, ttl_secs: int, now: float) -> list[str]:
        """Remove expired delivered terminal mappings; keep all recoverable work."""
        base = _records_root()
        try:
            children = tuple(base.iterdir())
        except FileNotFoundError:
            return []
        except OSError:
            logger.warning("Remote subagent state directory cannot be pruned", exc_info=True)
            return []
        expired: list[str] = []
        ttl = max(0, int(ttl_secs))
        for child in children:
            if (
                not re.fullmatch(r"[a-f0-9]{8}", child.name)
                or child.is_symlink()
                or not child.is_dir()
            ):
                continue
            state_path = child / "state.json"
            if state_path.is_symlink() or not state_path.is_file():
                continue
            try:
                raw = json.loads(state_path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    continue
                updated = float(raw.get("updated") or state_path.stat().st_mtime)
            except (OSError, TypeError, ValueError, RecursionError):
                continue
            if (
                raw.get("version") != 1
                or raw.get("id") != child.name
                or raw.get("executor") != "remote"
                or raw.get("done") is not True
                or raw.get("delivered") is not True
                or now - updated < ttl
            ):
                continue
            if rmtree_force(child):
                expired.append(child.name)
            else:
                logger.warning("Could not prune expired remote subagent record %s", child.name)
        return expired

    @classmethod
    def _load_records(cls) -> list[tuple[SubagentInfo, bool]]:
        base = _records_root()
        try:
            children = tuple(base.iterdir())
        except FileNotFoundError:
            return []
        except OSError:
            logger.warning("Remote subagent state directory is unreadable", exc_info=True)
            return []
        records: list[tuple[SubagentInfo, bool]] = []
        for child in children:
            if (
                child.is_symlink()
                or not child.is_dir()
                or not re.fullmatch(r"[a-f0-9]{8}", child.name)
            ):
                continue
            state_path = child / "state.json"
            if state_path.is_symlink() or not state_path.is_file():
                logger.warning("Remote subagent state %s has an unsafe state file", child.name)
                continue
            try:
                raw = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, RecursionError):
                logger.warning("Remote subagent state %s is unreadable", child.name)
                continue
            if (
                not isinstance(raw, dict)
                or raw.get("version") != 1
                or raw.get("id") != child.name
                or raw.get("executor") != "remote"
                or not isinstance(raw.get("instance_id"), str)
                or not raw["instance_id"]
                or not isinstance(raw.get("remote_id"), str)
                or not _REMOTE_ID_RE.fullmatch(raw["remote_id"])
            ):
                logger.warning("Remote subagent state %s is invalid", child.name)
                continue
            if raw.get("dismissed") is True:
                # Dismissed by the operator: kept on disk only until the prune.
                continue
            memory_mode = raw.get("memory_mode", "persistent")
            if memory_mode not in ("persistent", "incognito", "temporary"):
                logger.warning("Remote subagent state %s has invalid memory mode", child.name)
                continue
            result_path = child / "result.txt"
            full_result = ""
            if not result_path.is_symlink() and result_path.is_file():
                try:
                    full_result = result_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    if raw.get("done") and not raw.get("delivered"):
                        # A finished run's kept result could not be read. Publishing
                        # it as an empty success and acknowledging it would lose the
                        # result for good; leave the record for the next start.
                        logger.warning(
                            "Remote subagent %s result is unreadable; not restored yet",
                            child.name,
                            exc_info=True,
                        )
                        continue
                    full_result = ""
            try:
                started = float(raw.get("started") or time.time())
                max_turns = int(raw.get("max_turns") or 0)
                batch_total = int(raw.get("batch_total") or 0)
            except (TypeError, ValueError, OverflowError):
                logger.warning("Remote subagent state %s has invalid numeric fields", child.name)
                continue
            if not math.isfinite(started) or max_turns < 0 or batch_total < 0:
                logger.warning("Remote subagent state %s has invalid numeric fields", child.name)
                continue
            info = SubagentInfo(
                id=child.name,
                task=redact_peer_text(str(raw.get("task") or "")),
                started=started,
                done=bool(raw.get("done")),
                error=redact_peer_text(str(raw.get("error") or "")),
                parent_session_key=str(raw.get("parent_session") or ""),
                agent=str(raw.get("agent") or ""),
                max_turns=max_turns,
                cwd=str(raw.get("cwd") or ""),
                model=str(raw.get("model") or ""),
                reasoning_effort=str(raw.get("reasoning_effort") or ""),
                include_memory=raw.get("include_memory") is not False,
                include_lessons=raw.get("include_lessons") is not False,
                include_project=raw.get("include_project") is not False,
                memory_mode=memory_mode,
                batch_id=str(raw.get("batch_id") or ""),
                batch_total=batch_total,
                executor="remote",
                instance_id=raw["instance_id"],
                remote_id=raw["remote_id"],
                user_stopped=bool(raw.get("stopped")),
                stop_reason=str(raw.get("stop_reason") or ""),
                stop_class=str(raw.get("stop_class") or ""),
                partial=bool(raw.get("partial")),
                approval_floor=(
                    "interactive" if raw.get("approval_floor") == "interactive" else ""
                ),
                silent=raw.get("silent") is True,
            )
            info._exec_started = info.started
            info._slot_released = True
            if full_result:
                info.result_path = str(result_path)
                info.result = full_result
                info.result_truncated = False
            elif (
                info.done
                and not info.error
                and memory_mode != "persistent"
                and not raw.get("delivered")
            ):
                # An incognito/temporary result lives only in memory, so a
                # restart loses it; redelivering an empty success would read
                # as a run that answered nothing.
                info.error = _RESULT_NOT_RETAINED
            if raw.get("approved") is False and not info.done:
                # The gateway stopped while the spawn waited for its person:
                # nothing was sent, and nobody is asking any more.
                info.done = True
                info.error = _APPROVAL_LOST
                info.error_code = _APPROVAL_LOST_CODE
            records.append((info, bool(raw.get("delivered"))))
        return records

    async def ensure_restored(self) -> None:
        """Restore mappings once, then periodically prune delivered terminals."""
        if not self._restored:
            if self._restore_task is None or (
                self._restore_task.done()
                and not self._restore_task.cancelled()
                and self._restore_task.exception() is not None
            ):
                # A failed restore is retried on the next call instead of
                # re-raising the same stored exception forever.
                self._restore_task = asyncio.create_task(self._restore())
            await asyncio.shield(self._restore_task)
        await self._maybe_prune()

    async def _maybe_prune(self) -> None:
        now_monotonic = time.monotonic()
        if now_monotonic - self._last_prune < _PRUNE_INTERVAL_SECONDS:
            return
        async with self._prune_lock:
            now_monotonic = time.monotonic()
            if now_monotonic - self._last_prune < _PRUNE_INTERVAL_SECONDS:
                return
            try:
                ttl = int(getattr(self._manager, "_result_ttl_secs", 3600) or 0)
                expired = await asyncio.to_thread(self._prune_records, ttl, time.time())
            except Exception:
                logger.warning("Remote subagent record pruning failed", exc_info=True)
                self._last_prune = now_monotonic
                return
            forget = getattr(self._manager, "forget_external", None)
            if callable(forget):
                for run_id in expired:
                    info = self._manager.get(run_id)
                    if info is not None and info.done:
                        forget(run_id)
            if expired:
                logger.info("Pruned %d expired remote subagent record(s)", len(expired))
            self._last_prune = now_monotonic

    async def _restore(self) -> None:
        records = await asyncio.to_thread(self._load_records)
        keep_chars = int(getattr(self._manager, "_completion_keep_chars", 0) or 0)
        keep_mode = str(getattr(self._manager, "_completion_keep", "head") or "head")
        restored: list[tuple[SubagentInfo, bool]] = []
        for info, delivered in records:
            if info.error_code == _APPROVAL_LOST_CODE and not delivered:
                # A terminal derived at load (the gateway stopped during the
                # approval wait) is not on disk yet. Commit it before the run is
                # registered or reported; a failed write leaves the record pending
                # for the next start instead of publishing an ending it lacks.
                try:
                    await asyncio.to_thread(self._persist_info, info, delivered=False, done=True)
                except Exception:
                    logger.warning(
                        "Could not commit the lost approval of remote run %s",
                        info.id,
                        exc_info=True,
                    )
                    continue
            if info.result:
                full_result = info.result
                info.result_truncated = keep_chars > 0 and len(full_result) > keep_chars
                info.result = apply_completion_keep(full_result, keep_mode, keep_chars)
            if self._manager.get(info.id) is None:
                # One record the manager refuses must not fail the whole
                # restore: that would leave every spawn route erroring.
                try:
                    self._manager.register_external(info)
                except ValueError:
                    logger.warning("Skipping unrestorable remote run %s", info.id, exc_info=True)
                    continue
            restored.append((info, delivered))
        records = restored
        self._restored = True
        # Live runs first: their monitors must start whatever happens to the
        # redelivery of finished ones below.
        for info, _delivered in records:
            if info.done:
                continue
            if info.user_stopped:
                # A stop was committed for a run that has not ended: keep asking
                # the peer to stop it until the peer confirms (2xx or 404).
                self._unconfirmed_stops.add(info.id)
            monitor = asyncio.create_task(self._monitor(info))
            self._monitors[info.id] = monitor
            monitor.add_done_callback(self._forget_monitor(info.id))
        for info, delivered in records:
            if not info.done or delivered:
                continue
            # One record's failed redelivery or acknowledgement must not strand
            # the others; an unacknowledged one is retried on the next start.
            try:
                reported = await self._manager.report_external(info)
                if reported and _reached_parent(info):
                    await asyncio.to_thread(self._persist_info, info, delivered=True)
            except Exception:
                logger.warning("Could not redeliver remote run %s", info.id, exc_info=True)

    def _instances(self) -> Any:
        instances = getattr(self._state, "instances_manager", None)
        if instances is None:
            raise RemoteSubagentError(
                "Remote crews are not available on this gateway.",
                code="remote_instances_unavailable",
                status=503,
            )
        return instances

    def _choose_instance(self, requested: str) -> list[str]:
        """The crews to try, in order: the requested one, else least-loaded first."""
        instances = self._instances()
        if requested:
            if not _peer_is_connected(instances, requested):
                raise RemoteSubagentError(
                    "The selected remote crew is not connected.",
                    code="remote_instance_not_connected",
                    status=409,
                )
            return [requested]

        try:
            statuses = instances.status_all()
        except Exception as exc:
            logger.info("Could not enumerate remote crews (%s)", type(exc).__name__)
            statuses = {}
        candidates = sorted(
            instance_id
            for instance_id, status in (statuses.items() if isinstance(statuses, dict) else ())
            if _state_name(status) == "connected"
        )
        if not candidates:
            raise RemoteSubagentError(
                "No connected remote crew is available.",
                code="remote_pool_empty",
                status=409,
            )

        active = {
            instance_id: sum(
                1
                for info in self._manager.external_agents
                if not info.done and info.instance_id == instance_id
            )
            for instance_id in candidates
        }
        least = min(active.values())
        tied = [instance_id for instance_id in candidates if active[instance_id] == least]
        first = tied[self._tie_cursor % len(tied)]
        self._tie_cursor += 1
        # Least-loaded first (the rotated tie winner leading), the rest by load:
        # a crew that fails the version check is skipped, not fatal.
        rest = sorted(
            (instance_id for instance_id in candidates if instance_id != first),
            key=lambda instance_id: (active[instance_id], instance_id),
        )
        return [first, *rest]

    async def _select_compatible(self, requested: str) -> str:
        """The first candidate that passes the ``major.minor`` parity check."""
        first_error: RemoteSubagentError | None = None
        for candidate in self._choose_instance(requested):
            try:
                await _ensure_version_parity(self._instances(), candidate)
                return candidate
            except RemoteSubagentError as exc:
                first_error = first_error or exc
            except Exception as exc:
                # A transport failure while reading the version is not a
                # RemoteSubagentError; the HTTP handler maps only the latter, so
                # translate here instead of letting it surface as a bare 500.
                first_error = first_error or RemoteSubagentError(
                    redact_peer_text(str(exc or "")) or "The remote crew failed the version check.",
                    code=str(getattr(exc, "code", "") or "remote_version_mismatch"),
                    status=409,
                )
        if first_error is None:  # unreachable: _choose_instance never returns []
            raise RemoteSubagentError(
                "No connected remote crew is available.", code="remote_pool_empty", status=409
            )
        raise first_error

    async def _request_json(
        self,
        instance_id: str,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, object]]:
        instances = self._instances()
        encoded = json.dumps(body).encode("utf-8") if body is not None else None
        try:
            async with instances.proxy_request(
                instance_id,
                method,
                path,
                data=encoded,
                content_type="application/json" if encoded is not None else "",
            ) as response:
                raw = await _read_reply(response)
                status = int(response.status)
        except RemoteSubagentError:
            raise
        except Exception as exc:
            code = str(getattr(exc, "code", "") or "remote_peer_unreachable")
            status = int(getattr(exc, "http_status", 502) or 502)
            raise RemoteSubagentError(
                "The remote crew could not be reached.", code=code, status=status
            ) from None
        if len(raw) > _MAX_REPLY_BYTES:
            raise RemoteSubagentError(
                "The remote crew returned an oversized response.",
                code="remote_response_too_large",
            )
        try:
            payload = json.loads(raw) if raw else {}
        except (ValueError, RecursionError):
            raise RemoteSubagentError(
                "The remote crew returned malformed JSON.",
                code="remote_response_invalid",
            ) from None
        if not isinstance(payload, dict):
            raise RemoteSubagentError(
                "The remote crew returned an invalid response.",
                code="remote_response_invalid",
            )
        return status, payload

    def _parent_project(self, parent_session: str) -> Path | None:
        """The parent's project, or ``None`` when it has none to snapshot.

        A Slack, cron or CLI parent, or a project-less chat, has no project;
        with ``include_project`` on by default that must mean "no snapshot",
        not a refused spawn. A project that is set
        but whose directory is gone is still refused.
        """
        from kiro_crew.dashboard.chat_utils import dashboard_slot_key

        slot_name = dashboard_slot_key(parent_session)
        slot = self._state.get_slot(slot_name) if slot_name else None
        raw = getattr(slot, "project", "") if slot is not None else ""
        if not isinstance(raw, str) or not raw:
            return None
        project = Path(raw).expanduser().resolve()
        if not project.is_dir():
            raise RemoteSubagentError(
                "The parent project directory is unavailable.",
                code="remote_project_unavailable",
                status=409,
            )
        return project

    @staticmethod
    def _build_project_archive(
        project: Path, parent_session: str = "", app: str = "", agent: str = ""
    ) -> tuple[bytes, str, str]:
        try:
            may_read = _project_read_check(parent_session, app, agent=agent)
            return _snapshot_project(project, may_read=may_read)
        except _SnapshotRefused as exc:
            raise RemoteSubagentError(exc.message, code=exc.code, status=exc.status) from None

    async def _sync_parent_project(
        self, instance_id: str, parent_session: str, *, app: str = "", agent: str = ""
    ) -> str:
        """Upload the parent's project and return the peer's snapshot path.

        ``agent`` is the agent the parent runs as: a profile bound to it
        (``Bind(type="task", id=<agent>)``) narrows what the parent may read, so
        it narrows what leaves this host too.
        """
        project = self._parent_project(parent_session)
        if project is None:
            return ""
        try:
            payload, digest, commit = await asyncio.to_thread(
                self._build_project_archive, project, parent_session, app, agent
            )
        except RemoteSubagentError:
            raise
        except (OSError, subprocess.SubprocessError, ValueError, AWSError) as exc:
            # build_source_tarball raises its own errors (AWSError for a missing
            # checkout or a git/tar failure, OSError, timeouts); report them as a
            # refusable 400, not a bare 500.
            logger.info("Remote project snapshot failed (%s)", type(exc).__name__)
            raise RemoteSubagentError(
                "The parent project could not be packaged for remote execution.",
                code="remote_project_snapshot_failed",
                status=400,
            ) from None
        instances = self._instances()
        try:
            async with instances.proxy_request(
                instance_id,
                "POST",
                "api/remote-workspaces",
                params={"sha256": digest, "commit": commit},
                data=payload,
                content_type="application/gzip",
            ) as response:
                raw = await _read_reply(response)
                status = int(response.status)
        except Exception as exc:
            code = str(getattr(exc, "code", "") or "remote_workspace_unreachable")
            status = int(getattr(exc, "http_status", 502) or 502)
            raise RemoteSubagentError(
                "The project snapshot could not be sent to the remote crew.",
                code=code,
                status=status,
            ) from None
        if len(raw) > _MAX_REPLY_BYTES:
            raise RemoteSubagentError(
                "The remote workspace endpoint returned an oversized response.",
                code="remote_workspace_response_too_large",
            )
        try:
            response_body = json.loads(raw) if raw else {}
        except (ValueError, RecursionError):
            response_body = {}
        if not isinstance(response_body, dict):
            response_body = {}
        if not 200 <= status < 300:
            raise RemoteSubagentError(
                redact_peer_text(str(response_body.get("error") or ""))
                or f"The remote crew refused the project snapshot (HTTP {status}).",
                code=str(response_body.get("code") or "remote_workspace_refused"),
                status=status,
            )
        remote_path = response_body.get("path")
        if not isinstance(remote_path, str) or not remote_path.startswith("/"):
            raise RemoteSubagentError(
                "The remote crew installed the snapshot without returning a usable path.",
                code="remote_workspace_path_invalid",
            )
        return remote_path

    async def spawn(
        self,
        *,
        task: str,
        parent_session: str,
        agent: str,
        max_turns: int,
        cwd: str,
        model: str,
        reasoning_effort: str,
        include_memory: bool,
        include_lessons: bool,
        include_project: bool,
        memory_mode: str,
        batch_id: str,
        batch_total: int,
        instance_id: str = "",
        approval_mode: str = "",
        app: str = "",
        parent_agent: str = "",
        silent: bool = False,
    ) -> SubagentInfo:
        await self.ensure_restored()
        if self._closed:
            raise RemoteSubagentError(
                "Remote subagent execution is shutting down.",
                code="remote_executor_stopping",
                status=503,
            )
        selected = await self._select_compatible(instance_id)
        approval_floor = self._approval_floor(parent_session, approval_mode)
        await _ensure_peer_supports(
            self._instances(),
            selected,
            memory_mode=memory_mode,
            approval_floor=approval_floor,
        )
        info = SubagentInfo(
            id=uuid.uuid4().hex[:8],
            task=redact_peer_text(str(task or "")),
            parent_session_key=parent_session,
            agent=agent,
            max_turns=max_turns,
            cwd=cwd,
            model=model,
            reasoning_effort=reasoning_effort,
            include_memory=include_memory,
            include_lessons=include_lessons,
            include_project=include_project,
            memory_mode=memory_mode,
            batch_id=batch_id,
            batch_total=batch_total,
            executor="remote",
            instance_id=selected,
            # Named here, in the peer's own minted shape, and recorded BEFORE the
            # peer is asked, so a gateway that dies between the peer's answer and
            # our own record still finds the run on restart (``GET
            # /api/spawn/{id}`` under the same name).
            remote_id=os.urandom(8).hex(),
            approval_floor=approval_floor,
            silent=silent,
        )
        info._slot_released = True
        if approval_floor:
            # A parent that is not on auto would wait for a person before a local
            # spawn starts; a remote one also ships the project and the task to
            # another host, so it waits for the same person before either leaves.
            # ``_exec_started`` stays unset during that wait: it is what marks the
            # run as parked on its spawn approval, so a parent end or Stop-all
            # leaves the person's pending decision alone, as for a local run.
            return await self._spawn_after_approval(info, task, app=app, parent_agent=parent_agent)
        info._exec_started = info.started
        try:
            await self._place(info, task, app=app, parent_agent=parent_agent)
        except RemoteSubagentError as exc:
            if not exc.unresolved:
                raise
            # The peer may hold the run: its answer was lost and the stop was not
            # confirmed, so the record was kept. Refusing here would leave that
            # run unwatched until a restart, possibly after the peer has already
            # dropped its result. Register it and let the monitor settle it, as
            # the approval branch does; a 404 ends it as never started.
            logger.warning(
                "Remote run %s on %s is unconfirmed after placement; reconciling",
                info.remote_id,
                info.instance_id,
            )
        self._publish_spawn(info)
        await self._announce_spawn(info)
        self._start_monitor(info)
        return info

    def _start_monitor(self, info: SubagentInfo) -> None:
        monitor = asyncio.create_task(self._monitor(info))
        self._monitors[info.id] = monitor
        monitor.add_done_callback(self._forget_monitor(info.id))

    async def _place(
        self, info: SubagentInfo, task: str, *, app: str, parent_agent: str = ""
    ) -> None:
        """Snapshot the project, record the run, and start it on its peer.

        Raises :class:`RemoteSubagentError` with nothing left running on the peer
        and no record left behind, except where the peer's answer was lost: then
        the record stays for the next restart to reconcile, and the error says so
        (``unresolved``).
        """
        selected = info.instance_id
        if info.include_project and not info.cwd:
            info.cwd = await self._sync_parent_project(
                selected, info.parent_session_key, app=app, agent=parent_agent
            )
        # The peer is another host: a credential the model quoted into the task
        # must not leave this gateway, so the outbound task is scrubbed the same
        # way as the shadow copy and everything that comes back.
        peer_body: dict[str, object] = {
            "task": redact_peer_text(task),
            "parent_session": "",
            "silent": True,
            "include_memory": info.include_memory,
            "include_lessons": info.include_lessons,
            "include_project": info.include_project,
            "memory_mode": info.memory_mode,
        }
        if info.agent:
            peer_body["agent"] = info.agent
        if info.max_turns:
            peer_body["max_turns"] = info.max_turns
        if info.cwd:
            peer_body["cwd"] = info.cwd
        if info.model:
            peer_body["model"] = info.model
        if info.reasoning_effort:
            peer_body["reasoning_effort"] = info.reasoning_effort
        if info.approval_floor:
            peer_body["approval_floor"] = info.approval_floor
        peer_body["run_id"] = info.remote_id
        try:
            await asyncio.to_thread(self._persist_info, info, delivered=False, accepted=False)
        except Exception:
            logger.warning("Could not persist remote subagent mapping %s", info.id, exc_info=True)
            raise RemoteSubagentError(
                "The remote run could not be recorded locally, so it was not sent.",
                code="remote_mapping_unpersisted",
                status=507,
            ) from None

        if info.id in self._stops:
            # Stopped while the snapshot was taken or uploaded: nothing has asked
            # the peer for the run yet, so it never starts.
            await asyncio.to_thread(self._discard_mapping, info.id)
            raise RemoteSubagentError(
                "Stopped before it reached the crew; nothing was started there.",
                code="remote_spawn_stopped",
                status=409,
            )
        try:
            status, payload = await self._request_json(
                selected, "POST", "api/spawn", body=peer_body
            )
        except RemoteSubagentError as exc:
            # The peer may or may not have accepted it. It is named, so ask it to
            # stop that name; the record goes only once the peer confirms none
            # runs, otherwise a restart reconciles it from the record.
            exc.unresolved = not await self._withdraw_unconfirmed(info)
            raise
        if not 200 <= status < 300:
            await asyncio.to_thread(self._discard_mapping, info.id)
            raise RemoteSubagentError(
                redact_peer_text(str(payload.get("error") or ""))
                or f"The remote crew refused the run (HTTP {status}).",
                code=str(payload.get("code") or "remote_spawn_refused"),
                status=status,
            )
        remote_id = str(payload.get("id") or "")
        if not _REMOTE_ID_RE.fullmatch(remote_id):
            invalid = RemoteSubagentError(
                "The remote crew accepted the run but returned an invalid identifier.",
                code="remote_run_id_invalid",
            )
            invalid.unresolved = not await self._withdraw_unconfirmed(info)
            raise invalid
        # Only a crew that advertises ``run_id`` is sent a run
        # (``_ensure_peer_supports``), so this is the name it was sent; its own
        # echo is still what is recorded.
        info.remote_id = remote_id
        # Persist before publish: without the confirmed mapping a restart would
        # report a started run as never started, so a failed write must not
        # produce a successful receipt. Stop the run on the peer instead.
        try:
            await asyncio.to_thread(self._persist_info, info, delivered=False)
        except Exception:
            logger.warning("Could not persist remote subagent mapping %s", info.id, exc_info=True)
            unpersisted = RemoteSubagentError(
                "The remote run could not be recorded locally and was cancelled on its crew.",
                code="remote_mapping_unpersisted",
                status=507,
            )
            unpersisted.unresolved = not await self._withdraw_unconfirmed(info)
            raise unpersisted from None
        _audit_remote(info, "spawned")

    def _publish_spawn(self, info: SubagentInfo) -> None:
        self._manager.register_external(info)

    async def _announce_spawn(self, info: SubagentInfo) -> None:
        await self._manager._fire_event(
            "subagent_spawn",
            info,
            {
                "task": info.task,
                "agent": info.agent,
                "model": "",
                "requested_model": redact_peer_text(str(info.model or "")),
                "child_session": f"remote:{info.instance_id}:{info.remote_id}",
                "executor": "remote",
                "instance_id": info.instance_id,
            },
        )

    async def _spawn_after_approval(
        self, info: SubagentInfo, task: str, *, app: str, parent_agent: str = ""
    ) -> SubagentInfo:
        """Register *info* now and place it once the parent's person approves it.

        The spawn_run receipt cannot wait for a person, so the run is listed at
        once as awaiting approval, exactly as a local spawn is, and nothing leaves
        this host before the answer: no snapshot, no task, no peer request. A
        gateway without an approval surface refuses the run outright. The record
        written here carries ``approved: false``, so a restart during the wait
        ends the run with an error that says nothing was sent.
        """
        if getattr(self._manager, "_on_spawn_approval", None) is None:
            raise RemoteSubagentError(
                "A remote run from a parent that is not on auto needs a person to "
                "approve it, and this gateway has no approval surface.",
                code="remote_spawn_unapproved",
                status=403,
            )
        try:
            await asyncio.to_thread(
                self._persist_info, info, delivered=False, accepted=False, approved=False
            )
        except Exception:
            logger.warning("Could not persist remote subagent mapping %s", info.id, exc_info=True)
            raise RemoteSubagentError(
                "The remote run could not be recorded locally, so it was not sent.",
                code="remote_mapping_unpersisted",
                status=507,
            ) from None
        info._awaiting_approval = True
        self._publish_spawn(info)
        await self._announce_spawn(info)
        runner = asyncio.create_task(
            self._approve_and_place(info, task, app=app, parent_agent=parent_agent)
        )
        self._approvals[info.id] = runner
        run_id = info.id

        def _forget(_task: asyncio.Task[None]) -> None:
            self._approvals.pop(run_id, None)

        runner.add_done_callback(_forget)
        return info

    async def _approve_and_place(
        self, info: SubagentInfo, task: str, *, app: str, parent_agent: str = ""
    ) -> None:
        from kiro_crew.sel import sel
        from kiro_crew.subagent import SpawnApprovalUnreachable

        approve = self._manager._on_spawn_approval
        preview = redact_peer_text(task)[:80]
        refusal = "The remote run was not approved, so nothing was sent to the crew."
        approved = False
        try:
            if approve is None:
                # Checked at spawn time; a surface cleared since then answers
                # nothing, which is the same as no surface at all.
                raise SpawnApprovalUnreachable("the approval surface is gone")
            approved = bool(
                await approve(
                    f"spawn:{info.id}",
                    f"spawn_run({preview}) on remote crew {info.instance_id}, "
                    "with the project snapshot",
                    info.parent_session_key,
                )
            )
        except SpawnApprovalUnreachable:
            refusal = (
                "No approval surface could be reached for the remote run, so nothing "
                "was sent to the crew."
            )
        except asyncio.CancelledError:
            info._awaiting_approval = False
            raise
        except Exception:
            logger.warning("Remote spawn approval failed for %s", info.id, exc_info=True)
        info._awaiting_approval = False
        sel().log_tool_invocation(
            session_key=info.parent_session_key,
            source="subagent",
            tool_name="spawn_run",
            outcome="approved_spawn" if approved else "rejected_spawn",
            metadata={"subagent_id": info.id, "executor": "remote"},
        )
        if not approved:
            info.error_code = "remote_spawn_rejected"
            await self._end_before_placement(info, {"error": refusal, "done": True})
            return
        # Approved: from here the run is executing, so a parent end stops it.
        info._exec_started = time.time()
        self._placing.add(info.id)
        try:
            await self._place(info, task, app=app, parent_agent=parent_agent)
        except RemoteSubagentError as exc:
            if exc.unresolved:
                # The peer may be running it: its answer was lost and the stop was
                # not confirmed. Ending the run here would persist the kept record
                # as done, so no restart would reconcile it and its result would
                # never reach the parent. Leave it live and let the monitor settle
                # it: a peer that never started it answers 404 and the monitor
                # ends it as never started.
                logger.warning(
                    "Remote run %s on %s is unconfirmed after approval; reconciling",
                    info.remote_id,
                    info.instance_id,
                )
                if info.id in self._stops:
                    # A stop was committed and acknowledged while the POST was out;
                    # the peer may hold the run, so the monitor keeps sending the
                    # stop until the peer confirms it.
                    self._unconfirmed_stops.add(info.id)
                self._start_monitor(info)
                return
            info.error_code = exc.code
            payload: dict[str, object] = {"error": str(exc), "done": True}
            if exc.code == "remote_spawn_stopped":
                payload["stopped"] = True
            await self._end_before_placement(info, payload)
            return
        finally:
            self._placing.discard(info.id)
            stopped_while_placing = info.id in self._stops
            self._stops.discard(info.id)
        if stopped_while_placing:
            # The stop arrived after the POST left: the peer has the run, so stop
            # it there. The monitor reports the peer's own stopped ending and keeps
            # re-sending the stop until the peer confirms it.
            if not await self._send_stop(info):
                self._unconfirmed_stops.add(info.id)
        self._start_monitor(info)

    async def _send_stop(self, info: SubagentInfo) -> bool:
        """Ask the peer to stop *info*; True once it confirms (2xx, or 404: gone)."""
        try:
            status, _payload = await self._request_json(
                info.instance_id, "DELETE", f"api/spawn/{info.remote_id}"
            )
        except RemoteSubagentError:
            status = 0
        if 200 <= status < 300 or status == 404:
            return True
        logger.warning("Remote run %s did not confirm its stop (HTTP %s)", info.id, status)
        return False

    async def _end_before_placement(self, info: SubagentInfo, payload: dict[str, object]) -> bool:
        """End a run that never reached its peer; True once the ending is committed.

        Persist before publish: a failed terminal write leaves the run not done,
        so its parent and batch would wait on it until a restart. Retry the write
        in the background until it commits (the monitor slot is free, since no
        peer run exists to watch) and report whether this first attempt landed.
        """
        if await self._finish(info, dict(payload)):
            return True
        settle = asyncio.create_task(self._finish_until_committed(info, payload))
        self._monitors[info.id] = settle
        settle.add_done_callback(self._forget_monitor(info.id))
        return False

    async def _finish_until_committed(self, info: SubagentInfo, payload: dict[str, object]) -> None:
        while not self._closed and not info.done:
            await asyncio.sleep(_RETRY_SECONDS)
            if await self._finish(info, dict(payload)):
                return

    def _approval_floor(self, parent_session: str, approval_mode: str) -> str:
        """The approval floor a remote child carries: ``""`` or ``"interactive"``.

        A remote child runs under its crew's own approval policy, which may be
        looser than this gateway's. So the crew is told the posture the same
        child would get here, and applies the stricter of the two. The chain
        mirrors the local run step: the parent's own policy, an explicit
        ``approval_mode=auto`` from the SDK, this gateway's yolo, then the
        global ``approval_mode=auto`` only for a parent that is absent or gone.
        Anything else, including a failure to read the posture, is
        ``"interactive"``: an unreadable posture must not loosen the child.
        """
        try:
            sessions = self._manager._sessions
            if sessions.get_approval_policy(parent_session) == "auto":
                return ""
            if approval_mode == "auto":
                return ""
            is_yolo = getattr(self._manager, "_is_yolo", None)
            if is_yolo is not None and is_yolo():
                return ""
            if getattr(self._manager, "_global_approval_mode", "") == "auto" and (
                not parent_session or sessions.has_session(parent_session) is False
            ):
                return ""
        except Exception:
            logger.warning("Could not read the approval posture for a remote spawn", exc_info=True)
        return "interactive"

    def _forget_monitor(self, run_id: str) -> Callable[[asyncio.Task[None]], None]:
        """A done-callback that drops *run_id*'s monitor task once it finishes."""

        def _done(_task: asyncio.Task[None]) -> None:
            self._monitors.pop(run_id, None)

        return _done

    async def _monitor(self, info: SubagentInfo) -> None:
        try:
            await self._monitor_loop(info)
        finally:
            # The run is over (or this hub stops watching it): withdraw any
            # prompt still open for it, which the person cannot settle now.
            self._cancel_relays(info.id)
            self._unconfirmed_stops.discard(info.id)

    async def _monitor_loop(self, info: SubagentInfo) -> None:
        missing = 0
        unreachable_since = 0.0
        while not self._closed and not info.done:
            if info.id in self._unconfirmed_stops and await self._send_stop(info):
                self._unconfirmed_stops.discard(info.id)
            try:
                status, payload = await self._request_json(
                    info.instance_id, "GET", f"api/spawn/{info.remote_id}"
                )
            except RemoteSubagentError:
                status, payload = 0, {}
            if status == 409 and payload.get("code") == "spawn_admission_pending":
                # The peer is still admitting this run id (warming its project,
                # say): it exists in all but name, so earlier 404s stop counting.
                # The wait stays bounded by the unreachable deadline below.
                missing = 0
            if not 200 <= status < 300 and status != 404:
                now = time.monotonic()
                unreachable_since = unreachable_since or now
                if now - unreachable_since >= _UNREACHABLE_DEADLINE_SECONDS:
                    if await self._finish(
                        info,
                        {
                            "error": "The remote crew stayed unreachable; the run's "
                            "outcome is unknown.",
                            "done": True,
                        },
                    ):
                        return
                await asyncio.sleep(_RETRY_SECONDS)
                continue
            unreachable_since = 0.0
            if status == 404:
                missing += 1
                if missing < _MAX_NOT_FOUND_POLLS:
                    await asyncio.sleep(_POLL_SECONDS)
                    continue
                never_started = not await asyncio.to_thread(
                    self._stored_accepted, self._run_dir(info.id)
                )
                if await self._finish(
                    info,
                    {
                        "error": (
                            _NEVER_STARTED
                            if never_started
                            else "The remote run is no longer available on its crew."
                        ),
                        "done": True,
                    },
                ):
                    return
                await asyncio.sleep(_RETRY_SECONDS)
                continue
            missing = 0
            raw_turns = payload.get("turns")
            try:
                info.turns = max(
                    0,
                    int(str(raw_turns)) if raw_turns is not None else int(info.turns or 0),
                )
            except (TypeError, ValueError):
                pass
            info.last_tool = redact_peer_text(str(payload.get("last_tool") or ""))
            self._relay_approvals(info, payload.get("approvals"))
            if payload.get("done") is True:
                if await self._finish(info, payload):
                    return
                # Not committed locally: poll the peer again and retry.
                await asyncio.sleep(_RETRY_SECONDS)
                continue
            await asyncio.sleep(_POLL_SECONDS)

    def _relay_approvals(self, info: SubagentInfo, listed: object) -> None:
        """Mirror the peer's pending requests for *info* into its parent session.

        A request missing from the peer's list was settled there (answered, or
        its wait ran out), so its local prompt is withdrawn.
        """
        current: dict[str, dict[str, object]] = {}
        if info.approval_floor == "interactive" and isinstance(listed, list):
            for item in listed[:_MAX_RELAYED_APPROVALS]:
                if isinstance(item, dict) and _APPROVAL_ID_RE.fullmatch(str(item.get("id") or "")):
                    current[str(item["id"])] = item
        for key in [k for k in self._relays if k[0] == info.id and k[1] not in current]:
            self._relays.pop(key).cancel()
        for approval_id, item in current.items():
            key = (info.id, approval_id)
            if key not in self._relays:
                task = asyncio.create_task(self._relay_one(info, approval_id, item))
                self._relays[key] = task
                task.add_done_callback(functools.partial(self._forget_relay, key))

    def _forget_relay(self, key: tuple[str, str], task: asyncio.Task[None]) -> None:
        if self._relays.get(key) is task:
            self._relays.pop(key, None)

    def _cancel_relays(self, run_id: str) -> None:
        for key in [k for k in self._relays if k[0] == run_id]:
            self._relays.pop(key).cancel()

    async def _relay_one(
        self, info: SubagentInfo, approval_id: str, item: dict[str, object]
    ) -> None:
        """Ask this hub's person about one peer request and send the answer back."""
        from kiro_crew.dashboard.chat_utils import dashboard_slot_key

        request = getattr(self._state, "request_approval", None)
        if not callable(request):
            return
        title = redact_peer_text(str(item.get("title") or ""))
        approved = bool(
            await request(
                f"remote:{info.id}:{approval_id}",
                "subagent",
                f"[remote crew {info.instance_id}] {title}",
                tool_input=redact_peer_text(str(item.get("tool_input") or "")),
                tool_purpose=redact_peer_text(str(item.get("tool_purpose") or "")),
                slot=dashboard_slot_key(info.parent_session_key),
                is_background=False,
            )
        )
        try:
            status, _ = await self._request_json(
                info.instance_id,
                "POST",
                f"api/spawn/{info.remote_id}/approvals/{approval_id}",
                body={"approved": approved},
            )
        except RemoteSubagentError:
            status = 0
        if not 200 <= status < 300:
            # 404: the peer settled it first (its own wait ran out). Anything
            # else leaves the request pending there, so it is relayed again on
            # a later poll rather than approved by default.
            logger.info(
                "Remote crew %s did not take the answer to approval %s of run %s (HTTP %s)",
                info.instance_id,
                approval_id,
                info.id,
                status,
            )

    async def _finish(self, info: SubagentInfo, payload: dict[str, object]) -> bool:
        """Commit the run's terminal record, then publish it; True once committed.

        Persist before publish: the result file (persistent runs) and the
        ``done`` mapping must both be on disk before ``info.done`` flips or the
        parent hears about the run. A failed write leaves the run not done and
        returns False, so the monitor polls the peer again and retries, rather
        than reporting a completion that a restart would contradict or a result
        this gateway could not keep.
        """
        full_result = redact_peer_text(str(payload.get("result") or "")) or "_No response._"
        info.error = redact_peer_text(str(payload.get("error") or ""))
        info.user_stopped = info.user_stopped or bool(payload.get("stopped"))
        info.stop_reason = redact_peer_text(str(payload.get("stop_reason") or ""))
        info.stop_class = redact_peer_text(str(payload.get("stop_class") or ""))
        info.partial = bool(payload.get("partial"))
        info.elapsed = max(0.0, time.time() - info.started)

        # Incognito/temporary runs keep the result in memory only.
        info.result_path = ""
        if info.memory_mode == "persistent":
            try:
                result_dir = _records_root() / info.id
                await asyncio.to_thread(result_dir.mkdir, parents=True, exist_ok=True, mode=0o700)
                result_path = result_dir / "result.txt"
                await asyncio.to_thread(
                    atomic_write,
                    result_path,
                    full_result,
                    fsync=True,
                    restrict_to_owner=True,
                )
                info.result_path = str(result_path)
            except Exception:
                logger.warning(
                    "Could not persist remote subagent result %s; retrying",
                    info.id,
                    exc_info=True,
                )
                return False

        keep_chars = int(getattr(self._manager, "_completion_keep_chars", 0) or 0)
        keep_mode = str(getattr(self._manager, "_completion_keep", "head") or "head")
        info.result_truncated = keep_chars > 0 and len(full_result) > keep_chars
        info.result = apply_completion_keep(full_result, keep_mode, keep_chars)
        try:
            await asyncio.to_thread(self._persist_info, info, delivered=False, done=True)
        except Exception:
            logger.warning(
                "Could not persist terminal remote run %s; retrying", info.id, exc_info=True
            )
            return False
        info.done = True
        reported = await self._manager.report_external(info)
        if reported and _reached_parent(info):
            try:
                await asyncio.to_thread(self._persist_info, info, delivered=True)
            except Exception:
                logger.warning("Could not mark remote run %s delivered", info.id, exc_info=True)
        return True

    async def _withdraw_unconfirmed(self, info: SubagentInfo) -> bool:
        """Stop *info* on its peer, and drop its record once the peer confirms
        nothing runs under its name. Returns whether the record was dropped.

        For a spawn whose answer was lost or unusable. ``404`` means the peer
        never started it, a 2xx that it stopped it. Anything else, an
        unreachable peer or a ``409 spawn_admission_pending`` (the peer is still
        admitting that run id) included, keeps the record (``False``), so the run is
        reconciled from it instead of left running unseen.
        """
        try:
            status, _payload = await self._request_json(
                info.instance_id, "DELETE", f"api/spawn/{info.remote_id}"
            )
        except RemoteSubagentError:
            status = 0
        if 200 <= status < 300 or status == 404:
            try:
                if await asyncio.to_thread(self._discard_mapping, info.id):
                    return True
            except Exception:
                logger.warning("Could not discard remote mapping %s", info.id, exc_info=True)
        logger.warning(
            "Remote run %s on %s was not confirmed stopped; its record is kept for the "
            "next restart to reconcile",
            info.remote_id,
            info.instance_id,
        )
        return False

    async def cancel(self, info: SubagentInfo) -> bool:
        if info.executor != "remote" or not info.instance_id or not info.remote_id:
            return False
        if info._awaiting_approval and not info.done:
            # Still waiting for its person: nothing reached the peer, so the stop
            # is local. Withdraw the wait, then end the run as stopped.
            waiting = self._approvals.get(info.id)
            if waiting is not None and not waiting.done():
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
            info.user_stopped = True
            committed = await self._end_before_placement(
                info,
                {
                    "error": "Stopped before it was approved; nothing was sent to the crew.",
                    "stopped": True,
                    "done": True,
                },
            )
            _audit_remote(info, "cancelled")
            # Acknowledged only once the stopped ending is on disk; until then the
            # retry above keeps committing it and the stop reads as not yet done.
            return committed
        if info.id in self._placing and not info.done:
            # Approved and being placed: the peer may not know the run yet, so a
            # DELETE now would answer 404 while the POST still follows. Record the
            # stop; placement checks it before the POST and stops the peer run
            # after it. The stop is committed before it is acknowledged, so a
            # restart resumes sending it (``_restore``) instead of forgetting it.
            self._stops.add(info.id)
            if not await self._commit_stop(info):
                return False
            _audit_remote(info, "cancelled")
            return True
        try:
            status, payload = await self._request_json(
                info.instance_id, "DELETE", f"api/spawn/{info.remote_id}"
            )
        except RemoteSubagentError:
            await self._keep_stopping(info)
            raise
        if not 200 <= status < 300:
            await self._keep_stopping(info)
            raise RemoteSubagentError(
                redact_peer_text(str(payload.get("error") or ""))
                or f"The remote crew refused cancellation (HTTP {status}).",
                code=str(payload.get("code") or "remote_cancel_refused"),
                status=status,
            )
        cancelled = bool(payload.get("cancelled", True))
        if cancelled:
            info.user_stopped = True
            _audit_remote(info, "cancelled")
        return cancelled

    async def _commit_stop(self, info: SubagentInfo) -> bool:
        """Mark *info* stopped on disk; True once the record is written.

        ``_restore`` re-sends the stop of a run recorded as stopped but not done,
        so a committed stop outlives a restart of this gateway.
        """
        info.user_stopped = True
        try:
            run_dir = self._run_dir(info.id)
            accepted = await asyncio.to_thread(self._stored_accepted, run_dir)
            await asyncio.to_thread(self._persist_info, info, delivered=False, accepted=accepted)
        except Exception:
            logger.warning("Could not commit the stop of remote run %s", info.id, exc_info=True)
            return False
        return True

    async def _keep_stopping(self, info: SubagentInfo) -> None:
        """The peer did not confirm a stop: keep sending it until it does.

        The callers that end with their parent (teardown, Stop-all) swallow the
        error, so without this the peer would go on running the child's tools.
        The monitor re-sends the stop each cycle, and the committed record makes
        a restarted gateway resume it.
        """
        self._unconfirmed_stops.add(info.id)
        await self._commit_stop(info)

    async def close(self) -> None:
        """Stop local monitors without cancelling work that continues remotely."""
        self._closed = True
        tasks = (
            list(self._approvals.values())
            + list(self._monitors.values())
            + list(self._relays.values())
        )
        self._relays.clear()
        if self._restore_task is not None and not self._restore_task.done():
            tasks.append(self._restore_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._monitors.clear()
        binder = getattr(self._manager, "bind_external_placement", None)
        if callable(binder):
            binder(None)


def get_remote_subagent_service(state: DashboardState) -> RemoteSubagentService:
    """Return the state-owned service, creating it lazily for non-dashboard tests."""
    service = getattr(state, "remote_subagents", None)
    if not isinstance(service, RemoteSubagentService):
        manager = getattr(state, "subagents", None)
        if manager is None:
            raise RemoteSubagentError(
                "Subagents are not available on this gateway.",
                code="subagents_unavailable",
                status=503,
            )
        service = RemoteSubagentService(state, manager)
        state.remote_subagents = service
    return service
