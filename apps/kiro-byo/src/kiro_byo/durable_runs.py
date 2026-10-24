"""Durable run/checkpoint state for long-running BYO execution.

The store is intentionally small and provider-neutral.  A *Run* is a durable
business execution owned by a session, a *Step* is a restartable unit of work,
and an *Attempt* is one concrete execution of that step.  Checkpoints only
store control-plane facts and references into the session event log; they do
not duplicate the conversation transcript or credentials.

Hotfix6 is the persistence foundation.  Automatic background scheduling and
cross-process auto-resume are deliberately not implemented yet; callers can
inspect pending runs and decide when it is safe to resume.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from .paths import app_home

_RUN_TERMINAL = frozenset({"completed", "failed", "cancelled"})
_STEP_TERMINAL = frozenset({"completed", "failed", "cancelled", "skipped", "interrupted"})
_ATTEMPT_TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})


def _now() -> float:
    return time.time()


def _safe_id(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isalnum() or ch in "-_.")[:160] or "invalid"


class DurableRunStore:
    """Atomic JSON run ledger shared by the ACP runtime and diagnostics UI."""

    _locks: dict[str, threading.RLock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, root: Path | None = None, *, max_runs: int = 200) -> None:
        self.root = root or (app_home() / "data" / "runs")
        self.max_runs = max(16, min(int(max_runs), 2000))
        key = str(self.root.resolve()) if self.root.exists() else str(self.root)
        with self._locks_guard:
            self._lock = self._locks.setdefault(key, threading.RLock())

    def _path(self, run_id: str) -> Path:
        return self.root / f"{_safe_id(run_id)}.json"

    def _read(self, run_id: str) -> dict[str, Any] | None:
        try:
            raw = json.loads(self._path(run_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        return raw if isinstance(raw, dict) else None

    def _write(self, record: Mapping[str, Any]) -> None:
        run_id = str(record.get("run_id") or "")
        if not run_id:
            raise ValueError("run_id is required")
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(run_id)
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex}")
        try:
            tmp.write_text(json.dumps(dict(record), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _mutate(self, run_id: str, fn) -> dict[str, Any] | None:
        with self._lock:
            record = self._read(run_id)
            if record is None:
                return None
            fn(record)
            record["updated_at"] = _now()
            self._write(record)
            return record

    def create_run(
        self,
        session_id: str,
        *,
        plan_id: str = "",
        mode: str = "",
        selection: str = "",
        session_event_seq: int = -1,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        run_id = uuid.uuid4().hex
        now = _now()
        record: dict[str, Any] = {
            "schema_version": 1,
            "run_id": run_id,
            "session_id": str(session_id or ""),
            "plan_id": str(plan_id or ""),
            "mode": str(mode or ""),
            "selection": str(selection or ""),
            "status": "running",
            "created_at": now,
            "updated_at": now,
            "completed_at": 0.0,
            "session_event_seq": int(session_event_seq),
            "recovery_state": "clean",
            "retry_count": 0,
            "resume_count": 0,
            "replan_count": 0,
            "last_recovery_action": "",
            "metadata": dict(metadata or {}),
            "steps": [],
            "checkpoints": [],
        }
        with self._lock:
            self._write(record)
            self._prune_locked()
        return record

    def start_step(self, run_id: str, *, kind: str, objective: str = "") -> dict[str, Any] | None:
        step_id = uuid.uuid4().hex
        now = _now()
        out: dict[str, Any] = {}
        def mutate(record: dict[str, Any]) -> None:
            step = {
                "step_id": step_id,
                "kind": str(kind or "turn"),
                "objective": str(objective or "")[:1000],
                "status": "running",
                "created_at": now,
                "updated_at": now,
                "completed_at": 0.0,
                "attempts": [],
            }
            record.setdefault("steps", []).append(step)
            out.update(step)
        return out if self._mutate(run_id, mutate) is not None else None

    def start_attempt(
        self, run_id: str, step_id: str, *, provider_id: str = "", model_id: str = ""
    ) -> dict[str, Any] | None:
        attempt_id = uuid.uuid4().hex
        now = _now()
        out: dict[str, Any] = {}
        def mutate(record: dict[str, Any]) -> None:
            for step in record.get("steps", []):
                if step.get("step_id") != step_id:
                    continue
                attempt = {
                    "attempt_id": attempt_id,
                    "status": "running",
                    "created_at": now,
                    "updated_at": now,
                    "completed_at": 0.0,
                    "provider_id": str(provider_id or ""),
                    "model_id": str(model_id or ""),
                    "error_kind": "",
                    "error_category": "",
                }
                step.setdefault("attempts", []).append(attempt)
                step["updated_at"] = now
                out.update(attempt)
                break
        return out if self._mutate(run_id, mutate) is not None and out else None

    def checkpoint(
        self,
        run_id: str,
        *,
        step_id: str = "",
        attempt_id: str = "",
        name: str,
        session_event_seq: int = -1,
        agent_tasks: list[Mapping[str, Any]] | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        now = _now()
        checkpoint_id = uuid.uuid4().hex
        out: dict[str, Any] = {}
        def mutate(record: dict[str, Any]) -> None:
            cp = {
                "checkpoint_id": checkpoint_id,
                "name": str(name or "checkpoint"),
                "created_at": now,
                "step_id": str(step_id or ""),
                "attempt_id": str(attempt_id or ""),
                "session_event_seq": int(session_event_seq),
                "agents": [dict(row) for row in (agent_tasks or [])],
                "data": dict(data or {}),
            }
            record.setdefault("checkpoints", []).append(cp)
            record["checkpoints"] = record["checkpoints"][-64:]
            record["session_event_seq"] = int(session_event_seq)
            out.update(cp)
        return out if self._mutate(run_id, mutate) is not None else None

    def finish_attempt(
        self,
        run_id: str,
        step_id: str,
        attempt_id: str,
        *,
        status: str,
        provider_id: str = "",
        model_id: str = "",
        error_kind: str = "",
        error_category: str = "",
    ) -> dict[str, Any] | None:
        if status not in _ATTEMPT_TERMINAL:
            raise ValueError(f"invalid attempt terminal status: {status}")
        now = _now()
        def mutate(record: dict[str, Any]) -> None:
            for step in record.get("steps", []):
                if step.get("step_id") != step_id:
                    continue
                for attempt in step.get("attempts", []):
                    if attempt.get("attempt_id") != attempt_id:
                        continue
                    attempt.update({
                        "status": status, "updated_at": now, "completed_at": now,
                        "provider_id": str(provider_id or attempt.get("provider_id") or ""),
                        "model_id": str(model_id or attempt.get("model_id") or ""),
                        "error_kind": str(error_kind or ""),
                        "error_category": str(error_category or ""),
                    })
                    break
                step["updated_at"] = now
                break
        return self._mutate(run_id, mutate)

    def finish_step(self, run_id: str, step_id: str, *, status: str) -> dict[str, Any] | None:
        if status not in _STEP_TERMINAL:
            raise ValueError(f"invalid step terminal status: {status}")
        now = _now()
        def mutate(record: dict[str, Any]) -> None:
            for step in record.get("steps", []):
                if step.get("step_id") == step_id:
                    step.update({"status": status, "updated_at": now, "completed_at": now})
                    break
        return self._mutate(run_id, mutate)

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        recovery_state: str = "clean",
        session_event_seq: int = -1,
    ) -> dict[str, Any] | None:
        if status not in _RUN_TERMINAL:
            raise ValueError(f"invalid run terminal status: {status}")
        now = _now()
        def mutate(record: dict[str, Any]) -> None:
            record.update({
                "status": status,
                "completed_at": now,
                "recovery_state": str(recovery_state or "clean"),
                "session_event_seq": int(session_event_seq),
            })
        return self._mutate(run_id, mutate)

    def mark_recovery(self, run_id: str, state: str) -> dict[str, Any] | None:
        return self._mutate(run_id, lambda record: record.__setitem__("recovery_state", str(state or "pending")))

    def prepare_resume(
        self, run_id: str, *, session_id: str, strategy: str = "resume"
    ) -> dict[str, Any] | None:
        """Re-open a recoverable run and close any orphaned live attempt.

        This method never decides whether resume is safe; callers must apply the
        recovery policy first. It only makes the durable ledger internally
        consistent before a new Step/Attempt is created.
        """
        now = _now()
        strategy = str(strategy or "resume").lower()
        if strategy not in {"retry", "resume", "replan"}:
            raise ValueError(f"unsupported recovery strategy: {strategy}")

        def mutate(record: dict[str, Any]) -> None:
            if str(record.get("session_id") or "") != str(session_id or ""):
                raise ValueError("durable run belongs to a different session")
            for step in record.get("steps", []):
                if str(step.get("status") or "") != "running":
                    continue
                for attempt in step.get("attempts", []):
                    if str(attempt.get("status") or "") == "running":
                        attempt.update({
                            "status": "interrupted",
                            "updated_at": now,
                            "completed_at": now,
                            "error_kind": str(attempt.get("error_kind") or "ProcessRestart"),
                            "error_category": str(attempt.get("error_category") or "recovery"),
                        })
                step.update({"status": "interrupted", "updated_at": now, "completed_at": now})
            record["status"] = "running"
            record["completed_at"] = 0.0
            record["recovery_state"] = (
                "replanning" if strategy == "replan" else "retrying" if strategy == "retry" else "resuming"
            )
            key = {"retry": "retry_count", "resume": "resume_count", "replan": "replan_count"}[strategy]
            record[key] = int(record.get(key) or 0) + 1
            record["last_recovery_action"] = strategy

        return self._mutate(run_id, mutate)

    @staticmethod
    def restart_recovery_state(record: Mapping[str, Any]) -> str:
        """Classify an interrupted process restart from durable checkpoints.

        A checkpoint recorded before/after a potentially mutating tool batch is
        enough to make whole-turn replay unsafe. Unknown data fails closed only
        when it explicitly reports side-effect risk; older clean runs remain
        resumable for backward compatibility.
        """
        checkpoints = record.get("checkpoints") if isinstance(record, Mapping) else None
        if isinstance(checkpoints, list):
            for cp in reversed(checkpoints):
                if not isinstance(cp, Mapping):
                    continue
                data = cp.get("data") if isinstance(cp.get("data"), Mapping) else {}
                if bool(data.get("side_effect_risk")):
                    return "unknown_side_effect"
        return "pending_restart"

    def resumable(self, *, session_id: str = "", limit: int = 50) -> list[dict[str, Any]]:
        """Return runs that are not completed and are not blocked by side effects."""
        blocked = {"unknown_side_effect", "manual_verification_required", "unsafe"}
        rows = self.list(limit=max(limit, self.max_runs), session_id=session_id, include_terminal=True)
        out: list[dict[str, Any]] = []
        for row in rows:
            if str(row.get("status") or "") == "completed":
                continue
            if str(row.get("recovery_state") or "clean") in blocked:
                continue
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            try:
                anchor = int(metadata.get("user_event_seq", -1))
            except (TypeError, ValueError):
                anchor = -1
            if anchor < 0:
                continue
            out.append(row)
            if len(out) >= max(1, min(int(limit), 500)):
                break
        return out

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._read(run_id)

    def list(self, *, limit: int = 50, session_id: str = "", include_terminal: bool = True) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            try:
                paths = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            except OSError:
                return []
            out: list[dict[str, Any]] = []
            for path in paths:
                if len(out) >= limit:
                    break
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(raw, dict):
                    continue
                if session_id and str(raw.get("session_id") or "") != session_id:
                    continue
                if not include_terminal and str(raw.get("status") or "") in _RUN_TERMINAL:
                    continue
                out.append(raw)
            return out

    def pending(self, *, session_id: str = "") -> list[dict[str, Any]]:
        return self.list(limit=self.max_runs, session_id=session_id, include_terminal=False)

    def clear(self) -> int:
        removed = 0
        with self._lock:
            if not self.root.exists():
                return 0
            for path in self.root.glob("*.json"):
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    def _prune_locked(self) -> None:
        try:
            paths = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return
        for path in paths[self.max_runs:]:
            try:
                path.unlink()
            except OSError:
                pass


class AgentStateStore:
    """Small cross-process projection for the management UI.

    The authoritative task object remains the in-process AgentRegistry.  This
    projection contains no transcript and no queued message text; it exists so
    the App can show task/model/lifecycle truth even though the ACP harness runs
    in a separate process.
    """

    def __init__(self, path: Path | None = None, *, max_tasks: int = 200) -> None:
        self.path = path or (app_home() / "data" / "agent_runtime" / "tasks.json")
        self.max_tasks = max(20, min(int(max_tasks), 2000))
        self._lock = threading.RLock()

    @staticmethod
    def _project(task: Mapping[str, Any]) -> dict[str, Any]:
        result = task.get("result") if isinstance(task.get("result"), Mapping) else None
        return {
            "task_id": str(task.get("task_id") or ""),
            "plan_id": str(task.get("plan_id") or ""),
            "session_id": str(task.get("session_id") or ""),
            "parent_task_id": str(task.get("parent_task_id") or ""),
            "name": str(task.get("name") or ""),
            "role": str(task.get("role") or ""),
            "objective": str(task.get("objective") or "")[:500],
            "backend": str(task.get("backend") or "model"),
            "backend_id": str(task.get("backend_id") or ""),
            "tool_policy": str(task.get("tool_policy") or ""),
            "workspace_policy": str(task.get("workspace_policy") or ""),
            "depth": int(task.get("depth") or 0),
            "workspace_id": str(task.get("workspace_id") or ""),
            "workspace_kind": str(task.get("workspace_kind") or ""),
            "workspace_state": str(task.get("workspace_state") or ""),
            "status": str(task.get("status") or ""),
            "provider_id": str(task.get("provider_id") or ""),
            "model_id": str(task.get("model_id") or ""),
            "created_at": float(task.get("created_at") or 0.0),
            "started_at": float(task.get("started_at") or 0.0),
            "completed_at": float(task.get("completed_at") or 0.0),
            "generation": int(task.get("generation") or 0),
            "continuation_count": int(task.get("continuation_count") or 0),
            "pending_messages": int(task.get("pending_messages") or 0),
            "cancel_requested": bool(task.get("cancel_requested")),
            "error": str(task.get("error") or "")[:1000],
            "duration_ms": float((result or {}).get("duration_ms") or 0.0),
            "result_status": str((result or {}).get("status") or ""),
        }

    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return {"schema_version": 1, "tasks": {}}
        if not isinstance(raw, dict) or not isinstance(raw.get("tasks"), dict):
            return {"schema_version": 1, "tasks": {}}
        return raw

    def _write(self, state: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.tmp-{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex}")
        try:
            tmp.write_text(json.dumps(dict(state), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def upsert(self, task: Mapping[str, Any], *, event: str = "") -> None:
        projected = self._project(task)
        task_id = projected["task_id"]
        if not task_id:
            return
        with self._lock:
            state = self._read()
            tasks = state.setdefault("tasks", {})
            projected["updated_at"] = _now()
            projected["event"] = str(event or "")
            projected["controls"] = {
                "cancel": projected["status"] in {"planned", "running", "paused"},
                "send": projected["status"] in {"running", "completed", "paused"},
                "resume": projected["status"] in {"completed", "failed", "cancelled", "paused"} and projected["role"] in {"worker", "writer", "specialist"},
                "transport": "ACP _kiro/agent/*",
            }
            tasks[task_id] = projected
            rows = sorted(tasks.values(), key=lambda row: float(row.get("updated_at") or 0.0), reverse=True)
            state["tasks"] = {str(row["task_id"]): row for row in rows[:self.max_tasks]}
            state["updated_at"] = _now()
            self._write(state)

    def list(self, *, limit: int = 50, session_id: str = "") -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            rows = list(self._read().get("tasks", {}).values())
        if session_id:
            rows = [row for row in rows if str(row.get("session_id") or "") == session_id]
        rows.sort(key=lambda row: float(row.get("updated_at") or 0.0), reverse=True)
        return rows[:limit]

    def mark_session_stale(self, session_id: str) -> int:
        changed = 0
        with self._lock:
            state = self._read()
            tasks = state.get("tasks", {})
            now = _now()
            for row in tasks.values():
                if str(row.get("session_id") or "") != str(session_id or ""):
                    continue
                if str(row.get("status") or "") not in {"planned", "running", "paused"}:
                    continue
                row["status"] = "paused"
                row["event"] = "process_restart"
                row["updated_at"] = now
                row["controls"] = {
                    "cancel": False, "send": False, "resume": False,
                    "transport": "ACP _kiro/agent/*",
                    "note": "in-process context was lost; durable child resume is not available yet",
                }
                changed += 1
            if changed:
                state["updated_at"] = now
                self._write(state)
        return changed

    def clear(self) -> int:
        with self._lock:
            count = len(self._read().get("tasks", {}))
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                return 0
            return count
