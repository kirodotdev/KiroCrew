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
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import data_home
from kiro_crew.context_management import apply_completion_keep
from kiro_crew.dashboard.remote_relay import ensure_version_parity, peer_is_connected
from kiro_crew.platform_compat import rmtree_force
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.subagent import SubagentInfo

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState
    from kiro_crew.subagent import SubagentManager

logger = logging.getLogger(__name__)

_REMOTE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}\Z")
_MAX_REPLY_BYTES = 8 * 1024 * 1024
_MAX_WORKSPACE_ARCHIVE_BYTES = 64 * 1024 * 1024
_POLL_SECONDS = 2.0
_RETRY_SECONDS = 5.0
_MAX_NOT_FOUND_POLLS = 3
_PRUNE_INTERVAL_SECONDS = 60.0


class RemoteSubagentError(RuntimeError):
    """A remote run could not be started or controlled."""

    def __init__(self, message: str, *, code: str, status: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _redact(text: object) -> str:
    value, _ = redact_exfiltration_urls(str(text or ""))
    value, _ = redact_credentials(value)
    return value


def _state_name(status: object) -> str:
    value = getattr(status, "state", "")
    return str(getattr(value, "value", value) or "")


class RemoteSubagentService:
    """Proxy remote runs while preserving the local subagent wire contract."""

    def __init__(self, state: DashboardState, manager: SubagentManager) -> None:
        self._state = state
        self._manager = manager
        self._monitors: dict[str, asyncio.Task[None]] = {}
        self._closed = False
        self._tie_cursor = 0
        self._restore_task: asyncio.Task[None] | None = None
        self._restored = False
        self._last_prune = 0.0
        self._prune_lock = asyncio.Lock()
        binder = getattr(manager, "set_external_canceller", None)
        if callable(binder):
            binder(self.cancel)

    @staticmethod
    def _run_dir(run_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{8}", run_id):
            raise ValueError("invalid local remote-run id")
        return data_home() / "remote-subagents" / run_id

    @classmethod
    def _persist_info(cls, info: SubagentInfo, *, delivered: bool) -> None:
        run_dir = cls._run_dir(info.id)
        run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        state = {
            "version": 1,
            "updated": time.time(),
            "id": info.id,
            "executor": "remote",
            "instance_id": info.instance_id,
            "remote_id": info.remote_id,
            "parent_session": info.parent_session_key,
            "task": info.task,
            "agent": info.agent,
            "started": info.started,
            "done": info.done,
            "delivered": delivered,
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
        base = data_home() / "remote-subagents"
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
        base = data_home() / "remote-subagents"
        try:
            children = tuple(base.iterdir())
        except FileNotFoundError:
            return []
        except OSError:
            logger.warning("Remote subagent state directory is unreadable", exc_info=True)
            return []
        records: list[tuple[SubagentInfo, bool]] = []
        for child in children:
            if child.is_symlink() or not child.is_dir() or not re.fullmatch(
                r"[a-f0-9]{8}", child.name
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
                or not isinstance(raw.get("remote_id"), str)
                or not _REMOTE_ID_RE.fullmatch(raw["remote_id"])
            ):
                logger.warning("Remote subagent state %s is invalid", child.name)
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
                task=_redact(raw.get("task")),
                started=started,
                done=bool(raw.get("done")),
                error=_redact(raw.get("error")),
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
            )
            info._exec_started = info.started
            info._slot_released = True
            if full_result:
                info.result_path = str(result_path)
                info.result = full_result
                info.result_truncated = False
            records.append((info, bool(raw.get("delivered"))))
        return records

    async def ensure_restored(self) -> None:
        """Restore mappings once, then periodically prune delivered terminals."""
        if not self._restored:
            if self._restore_task is None:
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
        for info, _delivered in records:
            if info.result:
                full_result = info.result
                info.result_truncated = keep_chars > 0 and len(full_result) > keep_chars
                info.result = apply_completion_keep(full_result, keep_mode, keep_chars)
            if self._manager.get(info.id) is None:
                self._manager.register_external(info)
        self._restored = True
        for info, delivered in records:
            if info.done:
                if not delivered:
                    reported = await self._manager.report_external(info)
                    if reported:
                        await asyncio.to_thread(self._persist_info, info, delivered=True)
                continue
            monitor = asyncio.create_task(self._monitor(info))
            self._monitors[info.id] = monitor
            monitor.add_done_callback(
                lambda _task, run_id=info.id: self._monitors.pop(run_id, None)
            )

    def _instances(self) -> Any:
        instances = getattr(self._state, "instances_manager", None)
        if instances is None:
            raise RemoteSubagentError(
                "Remote crews are not available on this gateway.",
                code="remote_instances_unavailable",
                status=503,
            )
        return instances

    def _choose_instance(self, requested: str) -> str:
        instances = self._instances()
        if requested:
            if not peer_is_connected(instances, requested):
                raise RemoteSubagentError(
                    "The selected remote crew is not connected.",
                    code="remote_instance_not_connected",
                    status=409,
                )
            return requested

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
        selected = tied[self._tie_cursor % len(tied)]
        self._tie_cursor += 1
        return selected

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
                raw = await response.content.read(_MAX_REPLY_BYTES + 1)
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

    def _parent_project(self, parent_session: str) -> Path:
        from kiro_crew.dashboard.chat_utils import dashboard_slot_key

        slot_name = dashboard_slot_key(parent_session)
        slot = self._state.get_slot(slot_name) if slot_name else None
        raw = getattr(slot, "project", "") if slot is not None else ""
        if not isinstance(raw, str) or not raw:
            raise RemoteSubagentError(
                "The parent session has no project to synchronize.",
                code="remote_project_missing",
                status=409,
            )
        project = Path(raw).expanduser().resolve()
        if not project.is_dir():
            raise RemoteSubagentError(
                "The parent project directory is unavailable.",
                code="remote_project_unavailable",
                status=409,
            )
        return project

    @staticmethod
    def _build_project_archive(project: Path) -> tuple[bytes, str, str]:
        from kiro_crew.cloud.source import build_source_tarball

        archive = build_source_tarball(project)
        try:
            if archive.stat().st_size > _MAX_WORKSPACE_ARCHIVE_BYTES:
                raise RemoteSubagentError(
                    "The tracked project snapshot is too large for remote execution.",
                    code="remote_project_too_large",
                    status=413,
                )
            payload = archive.read_bytes()
            if not payload:
                raise RemoteSubagentError(
                    "The tracked project snapshot is empty.",
                    code="remote_project_empty",
                    status=400,
                )
            env = dict(os.environ)
            env["GIT_OPTIONAL_LOCKS"] = "0"
            completed = subprocess.run(  # noqa: S603 -- fixed argv, no shell
                ["git", "-C", str(project), "rev-parse", "HEAD"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                env=env,
            )
            commit = completed.stdout.strip() if completed.returncode == 0 else ""
            if not re.fullmatch(r"[a-f0-9]{40,64}", commit):
                raise RemoteSubagentError(
                    "The parent project does not have a verifiable Git HEAD.",
                    code="remote_project_commit_unknown",
                    status=409,
                )
            return payload, hashlib.sha256(payload).hexdigest(), commit
        finally:
            archive.unlink(missing_ok=True)

    async def _sync_parent_project(self, instance_id: str, parent_session: str) -> str:
        project = self._parent_project(parent_session)
        payload, digest, commit = await asyncio.to_thread(
            self._build_project_archive, project
        )
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
                raw = await response.content.read(_MAX_REPLY_BYTES + 1)
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
                _redact(response_body.get("error"))
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
    ) -> SubagentInfo:
        await self.ensure_restored()
        if self._closed:
            raise RemoteSubagentError(
                "Remote subagent execution is shutting down.",
                code="remote_executor_stopping",
                status=503,
            )
        selected = self._choose_instance(instance_id)
        await ensure_version_parity(self._instances(), selected)
        remote_cwd = cwd
        if include_project and not remote_cwd:
            remote_cwd = await self._sync_parent_project(selected, parent_session)

        peer_body: dict[str, object] = {
            "task": task,
            "parent_session": "",
            "silent": True,
            "include_memory": include_memory,
            "include_lessons": include_lessons,
            "include_project": include_project,
            "memory_mode": memory_mode,
        }
        if agent:
            peer_body["agent"] = agent
        if max_turns:
            peer_body["max_turns"] = max_turns
        if remote_cwd:
            peer_body["cwd"] = remote_cwd
        if model:
            peer_body["model"] = model
        if reasoning_effort:
            peer_body["reasoning_effort"] = reasoning_effort

        status, payload = await self._request_json(selected, "POST", "api/spawn", body=peer_body)
        if not 200 <= status < 300:
            raise RemoteSubagentError(
                _redact(payload.get("error")) or f"The remote crew refused the run (HTTP {status}).",
                code=str(payload.get("code") or "remote_spawn_refused"),
                status=status,
            )
        remote_id = str(payload.get("id") or "")
        if not _REMOTE_ID_RE.fullmatch(remote_id):
            raise RemoteSubagentError(
                "The remote crew accepted the run but returned an invalid identifier.",
                code="remote_run_id_invalid",
            )

        local_id = uuid.uuid4().hex[:8]
        info = SubagentInfo(
            id=local_id,
            task=_redact(task),
            parent_session_key=parent_session,
            agent=agent,
            max_turns=max_turns,
            cwd=remote_cwd,
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
            remote_id=remote_id,
        )
        info._exec_started = info.started
        info._slot_released = True
        self._manager.register_external(info)
        try:
            await asyncio.to_thread(self._persist_info, info, delivered=False)
        except Exception:
            logger.warning("Could not persist remote subagent mapping %s", info.id, exc_info=True)
        await self._manager._fire_event(
            "subagent_spawn",
            info,
            {
                "task": info.task,
                "agent": agent,
                "model": "",
                "requested_model": _redact(model),
                "child_session": f"remote:{selected}:{remote_id}",
                "executor": "remote",
                "instance_id": selected,
            },
        )
        monitor = asyncio.create_task(self._monitor(info))
        self._monitors[local_id] = monitor
        monitor.add_done_callback(lambda _task, run_id=local_id: self._monitors.pop(run_id, None))
        return info

    async def _monitor(self, info: SubagentInfo) -> None:
        missing = 0
        while not self._closed and not info.done:
            try:
                status, payload = await self._request_json(
                    info.instance_id, "GET", f"api/spawn/{info.remote_id}"
                )
            except RemoteSubagentError:
                await asyncio.sleep(_RETRY_SECONDS)
                continue
            if status == 404:
                missing += 1
                if missing < _MAX_NOT_FOUND_POLLS:
                    await asyncio.sleep(_POLL_SECONDS)
                    continue
                await self._finish(
                    info,
                    {
                        "error": "The remote run is no longer available on its crew.",
                        "done": True,
                    },
                )
                return
            missing = 0
            if not 200 <= status < 300:
                await asyncio.sleep(_RETRY_SECONDS)
                continue
            raw_turns = payload.get("turns")
            try:
                info.turns = max(
                    0,
                    int(str(raw_turns)) if raw_turns is not None else int(info.turns or 0),
                )
            except (TypeError, ValueError):
                pass
            info.last_tool = _redact(payload.get("last_tool"))
            if payload.get("done") is True:
                await self._finish(info, payload)
                return
            await asyncio.sleep(_POLL_SECONDS)

    async def _finish(self, info: SubagentInfo, payload: dict[str, object]) -> None:
        full_result = _redact(payload.get("result")) or "_No response._"
        info.error = _redact(payload.get("error"))
        info.user_stopped = info.user_stopped or bool(payload.get("stopped"))
        info.stop_reason = _redact(payload.get("stop_reason"))
        info.stop_class = _redact(payload.get("stop_class"))
        info.partial = bool(payload.get("partial"))
        info.elapsed = max(0.0, time.time() - info.started)

        try:
            result_dir = data_home() / "remote-subagents" / info.id
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
            logger.warning("Could not persist remote subagent result %s", info.id, exc_info=True)
            info.result_path = ""

        keep_chars = int(getattr(self._manager, "_completion_keep_chars", 0) or 0)
        keep_mode = str(getattr(self._manager, "_completion_keep", "head") or "head")
        info.result_truncated = keep_chars > 0 and len(full_result) > keep_chars
        info.result = apply_completion_keep(full_result, keep_mode, keep_chars)
        info.done = True
        try:
            await asyncio.to_thread(self._persist_info, info, delivered=False)
        except Exception:
            logger.warning("Could not persist terminal remote run %s", info.id, exc_info=True)
        reported = await self._manager.report_external(info)
        if reported:
            try:
                await asyncio.to_thread(self._persist_info, info, delivered=True)
            except Exception:
                logger.warning("Could not mark remote run %s delivered", info.id, exc_info=True)

    async def cancel(self, info: SubagentInfo) -> bool:
        if info.executor != "remote" or not info.instance_id or not info.remote_id:
            return False
        status, payload = await self._request_json(
            info.instance_id, "DELETE", f"api/spawn/{info.remote_id}"
        )
        if not 200 <= status < 300:
            raise RemoteSubagentError(
                _redact(payload.get("error")) or f"The remote crew refused cancellation (HTTP {status}).",
                code=str(payload.get("code") or "remote_cancel_refused"),
                status=status,
            )
        cancelled = bool(payload.get("cancelled", True))
        if cancelled:
            info.user_stopped = True
        return cancelled

    async def close(self) -> None:
        """Stop local monitors without cancelling work that continues remotely."""
        self._closed = True
        tasks = list(self._monitors.values())
        if self._restore_task is not None and not self._restore_task.done():
            tasks.append(self._restore_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._monitors.clear()
        binder = getattr(self._manager, "set_external_canceller", None)
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
