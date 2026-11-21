"""Product-neutral execution-plan lifecycle and structured runtime traces.

Agent tasks and teams are projections within one BYO turn; KiroCrew remains
authoritative over its cross-session tickets, worker assignments, and Attention.
"""
from __future__ import annotations

import time
from typing import Any

from .agent_runtime import AgentResult, ExecutionPlan
from .session_events import append_lifecycle


class AgentPlanLifecycleMixin:
    def _runtime_event(
        self,
        event: str,
        session: Any,
        *,
        data: dict[str, Any] | None = None,
        content: dict[str, Any] | None = None,
        turn_id: str = "",
        run_id: str = "",
        step_id: str = "",
        attempt_id: str = "",
    ) -> None:
        """Best-effort structured runtime trace; never break the agent loop."""
        store = getattr(self, "runtime_trace", None)
        if store is None:
            return
        try:
            event_data = dict(data or {})
            events = getattr(session, "events", None)
            if isinstance(events, list) and events:
                event_data.setdefault("session_event_seq", len(events) - 1)
            store.record(
                event,
                session_id=str(getattr(session, "session_id", "") or ""),
                turn_id=str(turn_id or getattr(session, "active_turn_id", "") or ""),
                run_id=str(run_id or getattr(session, "active_run_id", "") or ""),
                step_id=str(step_id or getattr(session, "active_run_step_id", "") or ""),
                attempt_id=str(attempt_id or getattr(session, "active_run_attempt_id", "") or ""),
                data=event_data,
                content=content,
            )
        except OSError as exc:
            self.trace(f"runtime trace write skipped: {type(exc).__name__}")

    def _begin_agent_plan(self, session: Any, plan: ExecutionPlan) -> str:
        """Register one product-neutral execution graph for the current turn."""
        registry = getattr(session, "agent_registry", None)
        if registry is None:
            return ""
        plan_id, tasks = registry.begin_plan(str(session.session_id), plan.agents)
        leader_task = next((task for task in tasks if task.spec.role == "lead"), tasks[0] if tasks else None)
        if leader_task is not None:
            for child_task in tasks:
                if child_task.task_id != leader_task.task_id:
                    registry.set_parent(child_task.task_id, leader_task.task_id)
        session.active_agent_plan_id = plan_id
        payload = {
            "plan_id": plan_id,
            "mode": plan.mode,
            "topology": plan.topology,
            "max_agents": int(plan.max_agents),
            "max_parallel": int(plan.max_parallel),
            "max_depth": int(plan.max_depth),
            "task_profile": plan.task_profile.public_dict(),
            "agents": [task.spec.public_dict() for task in tasks],
        }
        team_registry = getattr(session, "team_registry", None)
        if team_registry is not None and tasks:
            leader = next((task for task in tasks if task.spec.role == "lead"), tasks[0])
            team = team_registry.create_for_plan(
                session_id=str(session.session_id), plan_id=plan_id, leader_task_id=leader.task_id,
                member_task_ids=[task.task_id for task in tasks],
                max_agents=int(plan.max_agents), max_parallel=int(plan.max_parallel), max_depth=int(plan.max_depth),
                objectives={task.task_id: task.spec.objective for task in tasks},
            )
            payload["team_id"] = team.team_id
            self._runtime_event("agent.team.created", session, data=team.public_dict(include_messages=False))
        append_lifecycle(session, "agent/plan", payload)
        self._runtime_event("agent.plan.created", session, data=payload)
        for task in tasks:
            self._project_agent_state(task, event="planned")
        return plan_id

    def _agent_transition(
        self,
        session: Any,
        role: str,
        status: str,
        *,
        provider_id: str = "",
        model_id: str = "",
        summary: str = "",
        error: str = "",
        usage: dict[str, Any] | None = None,
    ) -> None:
        """Move one logical role through the unified Agent Runtime lifecycle."""
        plan_id = str(getattr(session, "active_agent_plan_id", "") or "")
        registry = getattr(session, "agent_registry", None)
        if not plan_id or registry is None:
            return
        task = registry.task_for_role(plan_id, str(role or ""))
        if task is None:
            return
        previous_status = task.status
        previous_provider = task.provider_id
        previous_model = task.model_id
        result = None
        if status in {"completed", "failed", "cancelled", "skipped"}:
            now = time.time()
            started = float(task.started_at or task.created_at or now)
            result = AgentResult(
                status=status,
                summary=str(summary or "")[:4000],
                provider_id=str(provider_id or task.provider_id or ""),
                model_id=str(model_id or task.model_id or ""),
                duration_ms=max(0.0, (now - started) * 1000.0),
                usage=dict(usage or {}),
            )
        updated = registry.transition(
            task.task_id,
            status,
            provider_id=str(provider_id or ""),
            model_id=str(model_id or ""),
            result=result,
            error=str(error or ""),
        )
        if updated is None:
            return
        team_registry = getattr(session, "team_registry", None)
        if team_registry is not None:
            team = team_registry.for_plan(plan_id)
            if team is not None:
                team_registry.mark_member_status(team.team_id, updated.task_id, status)
        self._project_agent_state(updated, event=status)
        # A lead can span several provider/tool rounds. Re-entering the same
        # running role must update its concrete route without pretending a new
        # logical agent was spawned each round. Emit a lightweight reroute only
        # when the concrete provider/model actually changed.
        if status == "running" and previous_status == "running":
            if (updated.provider_id, updated.model_id) == (previous_provider, previous_model):
                return
            event_name = "rerouted"
        else:
            event_name = {
                "running": "started",
                "completed": "completed",
                "failed": "failed",
                "cancelled": "cancelled",
                "skipped": "skipped",
            }.get(status, status)
        data = {
            "plan_id": plan_id,
            "task_id": updated.task_id,
            "role": updated.spec.role,
            "status": updated.status,
            "provider_id": updated.provider_id,
            "model": updated.model_id,
        }
        if error and event_name != "rerouted":
            data["error"] = str(error)
        append_lifecycle(session, f"agent/{event_name}", data)
        self._runtime_event(f"agent.{event_name}", session, data=data)

    def _finalize_agent_plan(
        self, session: Any, outcome: str, *, provider_id: str = "", model_id: str = ""
    ) -> None:
        """Close every task by identity so multi-member roles never alias."""
        plan_id = str(getattr(session, "active_agent_plan_id", "") or "")
        registry = getattr(session, "agent_registry", None)
        if not plan_id or registry is None:
            return
        terminal = {"completed", "failed", "cancelled", "skipped"}
        team_registry = getattr(session, "team_registry", None)
        team = team_registry.for_plan(plan_id) if team_registry is not None else None
        for task in registry.list(plan_id=plan_id):
            if task.status in terminal:
                continue
            if outcome == "success":
                status = "completed" if task.spec.role == "lead" and task.status == "running" else "skipped"
            elif outcome in {"cancelled", "interrupted"}:
                status = "cancelled" if task.status == "running" or task.spec.role == "lead" else "skipped"
            else:
                status = "failed" if task.status == "running" or task.spec.role == "lead" else "skipped"
            result = AgentResult(
                status=status, provider_id=provider_id if task.spec.role == "lead" else task.provider_id,
                model_id=model_id if task.spec.role == "lead" else task.model_id,
                unresolved=("turn ended before role completed",) if status == "failed" else (),
            )
            updated = registry.transition(
                task.task_id, status, provider_id=result.provider_id, model_id=result.model_id,
                result=result, error=result.unresolved[0] if result.unresolved else "",
            )
            if updated is not None:
                self._project_agent_state(updated, event=status)
                if team_registry is not None and team is not None:
                    team_registry.mark_member_status(team.team_id, updated.task_id, status)
        if team_registry is not None and team is not None:
            team_status = "completed" if outcome == "success" else "cancelled" if outcome in {"cancelled", "interrupted"} else "failed"
            team_registry.set_status(team.team_id, team_status)
        self._runtime_event(
            "agent.plan.completed", session,
            data={
                "plan_id": plan_id, "team_id": team.team_id if team is not None else "",
                "outcome": outcome, "agents": [t.public_dict() for t in registry.list(plan_id=plan_id)],
            },
        )
        append_lifecycle(
            session, "agent/plan_end",
            {"plan_id": plan_id, "team_id": team.team_id if team is not None else "", "outcome": outcome},
        )
        session.active_agent_plan_id = ""

