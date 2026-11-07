"""Durable Run lifecycle and projection for BYO turn execution.

The Crew host owns task assignment and completion. These methods persist
BYO execution evidence, fence mutating batches and maintain lease safety.
The executing harness supplies heartbeat, trace and event callbacks.
"""
from __future__ import annotations

import uuid
from typing import Any

from .agent_runtime import AgentTask, ExecutionPlan
from .crew_role_evidence import projected_crew_agent_id
from .durable_runs import AgentStateStore, DurableRunStore
from .session_events import append_lifecycle
from .turn_loop_types import DurableCheckpointError


class DurableRunLifecycleMixin:
    def _durable_run_store(self) -> DurableRunStore:
        store = getattr(self, "_kiro_durable_run_store", None)
        if store is None:
            store = DurableRunStore()
            setattr(self, "_kiro_durable_run_store", store)
        return store

    def _agent_state_store(self) -> AgentStateStore:
        store = getattr(self, "_kiro_agent_state_store", None)
        if store is None:
            store = AgentStateStore()
            setattr(self, "_kiro_agent_state_store", store)
        return store

    def _project_agent_state(self, task: AgentTask, *, event: str = "") -> None:
        try:
            self._agent_state_store().upsert(task.public_dict(), event=event)
        except OSError as exc:
            self.trace(f"agent state projection skipped: {type(exc).__name__}")

    def _run_agent_snapshots(self, session: Any) -> list[dict[str, Any]]:
        registry = getattr(session, "agent_registry", None)
        plan_id = str(getattr(session, "active_agent_plan_id", "") or "")
        if registry is None or not plan_id:
            return []
        fields = ("task_id", "name", "role", "status", "provider_id", "model_id", "generation")
        return [
            {field: item.get(field, 0 if field == "generation" else "") for field in fields}
            for item in (task.public_dict() for task in registry.list(plan_id=plan_id))
        ]

    def _checkpoint_durable_run(self, session: Any, name: str, *, data: dict[str, Any] | None = None, critical: bool = False) -> None:
        run_id = str(getattr(session, "active_run_id", "") or "")
        if not run_id:
            # Real ACP turns have an active_turn_id set by the prompt handler.
            # An earlier failed Run creation must never let such a turn execute
            # a mutating batch without a persisted risk boundary. A handful
            # of low-level characterization tests call this internal helper
            # directly, without beginning a host-managed ACP transaction.
            if critical and str(getattr(session, "active_turn_id", "") or ""):
                raise DurableCheckpointError(name)
            return
        if critical and bool(getattr(session, "active_run_lease_lost", False)):
            raise DurableCheckpointError(name)
        try:
            event_seq = len(getattr(session, "events", ()) or ()) - 1
            cp = self._durable_run_store().checkpoint(
                run_id,
                step_id=str(getattr(session, "active_run_step_id", "") or ""),
                attempt_id=str(getattr(session, "active_run_attempt_id", "") or ""),
                name=name,
                owner_token=str(getattr(session, "active_run_lease_token", "") or ""),
                session_event_seq=event_seq,
                agent_tasks=self._run_agent_snapshots(session),
                data=dict(data or {}),
            )
            if critical and cp is None:
                raise DurableCheckpointError(name)
            if cp is not None:
                self._runtime_event(
                    "run.checkpoint", session,
                    data={
                        "run_id": run_id, "checkpoint_id": cp.get("checkpoint_id", ""),
                        "name": name, "checkpoint_event_seq": cp.get("session_event_seq", -1),
                    },
                )
        except (OSError, ValueError, TimeoutError) as exc:
            # A pre-execution risk boundary (``critical=True``) must fail closed:
            # if we cannot durably commit "a side-effecting batch is about to
            # run" then a crash mid-call would leave an unknown_side_effect that
            # recovery cannot classify, contradicting the fail-closed contract.
            # Post-execution and non-boundary checkpoints stay best-effort.
            if critical:
                self.trace(f"durable checkpoint FAILED (critical) name={name}: {type(exc).__name__}")
                raise DurableCheckpointError(name) from exc
            self.trace(f"durable checkpoint skipped: {type(exc).__name__}")

    def _begin_durable_run(
        self, session: Any, execution_plan: ExecutionPlan | None, *, selection: str,
        provider_id: str, model_id: str, history_start: int = -1, user_event_seq: int = -1,
        resume_run_id: str = "", recovery_strategy: str = "",
    ) -> str:
        try:
            event_seq = len(getattr(session, "events", ()) or ()) - 1
            store = self._durable_run_store()
            lease_token = uuid.uuid4().hex
            strategy = str(recovery_strategy or "").strip().lower()
            if resume_run_id:
                strategy = strategy if strategy in {"retry", "resume", "replan"} else "resume"
                record = store.prepare_resume(
                    str(resume_run_id), session_id=str(session.session_id), strategy=strategy,
                    owner_token=lease_token, lease_ttl=120.0,
                )
                if record is None:
                    return ""
                run_id = str(resume_run_id)
                step_kind = strategy
                objectives = {
                    "retry": "Retry durable ACP turn on the existing selection",
                    "resume": "Resume durable ACP turn",
                    "replan": "Replan and resume durable ACP turn",
                }
                step = store.start_step(run_id, kind=step_kind, objective=objectives[strategy], owner_token=lease_token)
                lifecycle = f"run/{strategy}"
                runtime_event = {"retry": "run.retried", "resume": "run.resumed", "replan": "run.replanned"}[strategy]
            else:
                record = store.create_run(
                    str(session.session_id),
                    plan_id=str(getattr(session, "active_agent_plan_id", "") or ""),
                    mode=str(getattr(execution_plan, "mode", "single") if execution_plan is not None else "single"),
                    selection=str(selection or ""),
                    session_event_seq=event_seq,
                    lease_owner=lease_token, lease_ttl=120.0,
                    metadata={
                        "provider_id": provider_id, "model_id": model_id,
                        "history_start": int(history_start), "user_event_seq": int(user_event_seq),
                        "host_work_link": dict(getattr(session, "host_work_link", {}) or {}),
                        "crew_projected_agent_id": projected_crew_agent_id(getattr(session, "agent", None)),
                    },
                )
                run_id = str(record["run_id"])
                step = store.start_step(run_id, kind="turn", objective="Execute one ACP prompt turn", owner_token=lease_token)
                lifecycle = "run/start"
                runtime_event = "run.started"
            step_id = str((step or {}).get("step_id") or "")
            if not step_id:
                raise ValueError("durable step creation failed")
            attempt = store.start_attempt(
                run_id, step_id, provider_id=provider_id, model_id=model_id, owner_token=lease_token
            )
            if not (attempt or {}).get("attempt_id"):
                raise ValueError("durable attempt creation failed")
            session.active_run_lease_token = lease_token
            session.active_run_id = run_id
            session.active_run_step_id = step_id
            session.active_run_attempt_id = str((attempt or {}).get("attempt_id") or "")
            self._start_run_lease_heartbeat(session, run_id, lease_token)
            payload = {
                "run_id": run_id, "step_id": step_id,
                "attempt_id": session.active_run_attempt_id,
                "plan_id": str(getattr(session, "active_agent_plan_id", "") or ""),
                "recovery_strategy": strategy if resume_run_id else "",
            }
            append_lifecycle(session, lifecycle, payload)
            self._runtime_event(runtime_event, session, data=payload)
            self._checkpoint_durable_run(
                session, "recovery_ready" if resume_run_id else "turn_ready",
                data={
                    "provider_id": provider_id, "model_id": model_id,
                    "strategy": strategy if resume_run_id else "",
                },
            )
            return run_id
        except (OSError, ValueError, TimeoutError) as exc:
            self.trace(f"durable run start skipped: {type(exc).__name__}")
            return ""

    def _finish_durable_run(
        self, session: Any, *, outcome: str, error_kind: str = "", error_category: str = "",
        provider_id: str = "", model_id: str = "", side_effect_risk: bool = False,
    ) -> bool | None:
        run_id = str(getattr(session, "active_run_id", "") or "")
        if not run_id:
            return None
        step_id = str(getattr(session, "active_run_step_id", "") or "")
        attempt_id = str(getattr(session, "active_run_attempt_id", "") or "")
        status = "completed" if outcome == "success" else "cancelled" if outcome == "cancelled" else "failed"
        if side_effect_risk and status != "completed":
            recovery_state = "unknown_side_effect"
        elif status == "failed" and str(error_category or "") in {"transport", "timeout", "rate_limit", "overloaded"}:
            recovery_state = "retryable"
        elif status == "failed":
            recovery_state = "replan_required"
        else:
            recovery_state = "clean"
        self._stop_run_lease_heartbeat(session)
        owner_token = str(getattr(session, "active_run_lease_token", "") or "")
        try:
            self._checkpoint_durable_run(
                session, "terminal",
                data={"outcome": outcome, "error_kind": error_kind, "error_category": error_category, "recovery_state": recovery_state},
            )
            store = self._durable_run_store()
            event_seq = len(getattr(session, "events", ()) or ()) - 1
            sealed = store.finalize_turn(
                run_id, step_id=step_id, attempt_id=attempt_id, status=status,
                provider_id=provider_id, model_id=model_id,
                error_kind=error_kind, error_category=error_category,
                recovery_state=recovery_state, session_event_seq=event_seq,
                owner_token=owner_token,
            )
            if sealed is None:
                raise ValueError("durable run disappeared before terminal commit")
            payload = {"run_id": run_id, "status": status, "recovery_state": recovery_state}
            append_lifecycle(session, f"run/{status}", payload)
            self._runtime_event(f"run.{status}", session, data=payload)
            return True
        except (OSError, ValueError, TimeoutError) as exc:
            self.trace(f"durable run finish uncommitted: {type(exc).__name__}")
            self._runtime_event(
                "run.finalization_failed", session,
                data={"run_id": run_id, "outcome": outcome, "error": type(exc).__name__},
            )
            return False
        finally:
            session.active_run_id = ""
            session.active_run_lease_token = ""
            session.active_run_lease_lost = False
            session.active_run_step_id = ""
            session.active_run_attempt_id = ""

