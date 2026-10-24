"""One-turn execution loop for the Kiro BYO ACP harness.

The transport/session server owns JSON-RPC framing and lifecycle.  This mixin owns
provider inference, streaming, tool-search/tool-call iteration and per-turn
telemetry.  Keeping this loop out of ``acp_server.py`` prevents session protocol
changes from sharing a 700-line method with model/tool execution.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from types import SimpleNamespace
from typing import Any

from .policy_packs import runtime_policy_context_for_request
from .agent_runtime import AgentResult, AgentRuntime, AgentTask, ExecutionPlan
from .durable_runs import AgentStateStore, DurableRunStore
from .capability_seams import (
    BoundPostStageRunner,
    BoundProviderInvoker,
    BoundReasoningResolver,
    BoundToolExecutor,
    DefaultModelRouter,
    EventSessionProjection,
    RuntimeSeams,
)
from .config import ConfigError
from .errors import ClientRequestError
from .execution_guard import ExecutionGuard
from .mcp_client import DEFERRED_TOOL_ACTIVE_LIMIT, DEFERRED_TOOL_SEARCH_NAME
from .native_tools import NativeInvocation, NativeToolError, NativeToolRuntime, PermissionDecision, ToolValidationError
from .acp_worker import AcpAgentCatalog, run_acp_agent_task
from .workspace_manager import WorkspaceError, WorkspaceManager
from .protocol import MODEL_ECHO, MODEL_UPPER
from .mode_router import (
    ReviewTarget,
    build_execution_plan,
    build_signals,
    infer_tools_required,
    model_in_tier,
)
from .providers import ProviderError, ProviderInterrupted, ProviderTransportError, chat_completion_turn
from .reasoning_control import (
    ReasoningInvocation,
    resolve_effort_level,
    resolve_thinking_mode,
    thinking_auto_suggestion,
)
from .reasoning_language import resolve_reasoning_language, resolve_response_language
from .recovery_policy import RecoveryPolicy
from .sandbox import SandboxError
from .session_events import (
    append_lifecycle,
    append_message,
    clear_messages,
    clear_route_pin,
    revise_last_assistant,
    set_route_pin,
    truncate_messages,
)
from .telemetry import TurnTelemetry, now_timestamp
from .turn_state import RoundDisposition, RoundOutcome, TurnPhase, TurnState

# Native capabilities that only read or look things up.
_RELAY_GATHERING_CAPABILITIES = frozenset({"fs_read", "web_fetch", "web_search"})
_SAFE_TOOL_EFFECTS = frozenset({"pure", "read"})
_MUTATING_NATIVE_TOOLS = frozenset({
    "write", "edit", "delete_file", "shell", "execute_shell_process", "control_shell_process"
})


@dataclass(frozen=True)
class _PostStageOutcome:
    text: str
    provider_id: str
    model_id: str
    usage: Any | None
    thinking: str = ""
    effort: str = ""
    elapsed_ms: float = 0.0
    applied: bool = False


@dataclass(frozen=True)
class _ProviderRoundOutcome:
    """Concrete provider result plus the small Turn Engine control summary."""

    turn: Any
    elapsed_ms: float
    control: RoundOutcome


@dataclass(frozen=True)
class _ToolBatchOutcome:
    """Aggregated effects of executing one already-emitted tool-call batch."""

    cancelled: bool = False
    steered: bool = False
    replan_requested: bool = False
    stop_requested: bool = False
    guard_reason: str = ""
    pending_replan_notice: str = ""
    tool_ms: float = 0.0
    mcp_wait_ms: float = 0.0
    native_calls: int = 0
    tool_search_calls: int = 0
    tool_search_loaded: int = 0


class _LoopDirective(str, Enum):
    """Explicit control-flow edges of ``_run_provider_tool_loop``.

    The names intentionally mirror the ten historical ``continue`` branches so
    characterization tests and code review can prove that step rewind semantics
    did not change while the 0.2.2 loop is split into smaller responsibilities.
    """

    NONE = "none"
    SUBAGENTS_HAND_BACK = "subagents_hand_back"
    STEER_AFTER_INTERRUPT = "steer_after_interrupt"
    WORKER_FAILED_HAND_BACK = "worker_failed_hand_back"
    RETRY_SAME_MODEL = "retry_same_model"
    PRISTINE_FALLBACK = "pristine_fallback"
    WORKER_FINDINGS_HAND_BACK = "worker_findings_hand_back"
    QUEUED_STEER_AFTER_FINAL = "queued_steer_after_final"
    WORKER_GUARD_HAND_BACK = "worker_guard_hand_back"
    EXECUTION_GUARD_REPLAN = "execution_guard_replan"
    STEER_AFTER_TOOLS = "steer_after_tools"
    FINAL = "final"
    STOP = "stop"
    RETURN = "return"


@dataclass
class _ProviderLoopState:
    """Mutable state shared across provider/tool rounds for one user turn."""

    provider: Any
    model: Any
    mode_fallbacks: list[tuple[Any, Any]]
    relay: Any
    turn_effort: str
    turn_thinking: str
    turn_run_id: str
    turn_trace_id: str

    step: int = 0
    relay_next_role: str = "lead"
    relay_rounds: int = 0
    first_visible_at: float | None = None
    last_visible_at: float | None = None
    previous_visible_at: float | None = None
    upstream_ms: float = 0.0
    tool_ms: float = 0.0
    mcp_wait_ms: float = 0.0
    tool_calls_total: int = 0
    provider_rounds_total: int = 0
    transport_retries_total: int = 0
    fallbacks_total: int = 0
    side_effect_risk_seen: bool = False
    side_effect_classes_seen: set[str] = field(default_factory=set)
    execution_guard_halted: bool = False
    execution_guard_reason: str = ""
    pending_replan_notice: str = ""
    execution_guard: ExecutionGuard = field(default_factory=ExecutionGuard)
    native_tool_calls_total: int = 0
    tool_search_calls_total: int = 0
    tool_search_loaded_total: int = 0
    tool_schema_bytes: int = 0
    active_tools_count: int = 0
    stream_chunks: int = 0
    stream_chars: int = 0
    max_chunk_chars: int = 0
    max_gap_ms: float = 0.0
    round_text_parts: list[str] = field(default_factory=list)
    round_reasoning_emitted: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cache_usage_reported: bool = False
    cache_write_usage_reported: bool = False
    reasoning_usage_reported: bool = False
    served_model_id: str = ""
    served_provider_id: str = ""
    telemetry_recorded: bool = False
    final_text: str = ""
    observed_thinking_modes: set[str] = field(default_factory=set)
    observed_efforts: set[str] = field(default_factory=set)
    turn_state: TurnState = field(default_factory=TurnState)


@dataclass(frozen=True)
class _ProviderRoundContext:
    provider: Any
    model: Any
    role: str
    stage: str
    effort: str
    thinking: str
    reasoning_signals: Any
    tools: list[dict[str, Any]]
    replan_notice: str = ""


class DurableCheckpointError(RuntimeError):
    """Raised when a fail-closed (pre-execution) durable checkpoint cannot be
    committed. The turn must abort before running side-effecting tools so a
    crash cannot leave an unclassifiable unknown_side_effect."""


class TurnRunnerMixin:
    def _runtime_seams(self) -> RuntimeSeams:
        """Return the narrow capabilities consumed by the Turn Engine.

        The bundle is lazy so AcpServer construction stays unchanged and tests
        may inject a replacement bundle by assigning ``_kiro_runtime_seams``.
        """
        seams = getattr(self, "_kiro_runtime_seams", None)
        if seams is None:
            seams = RuntimeSeams(
                model_router=DefaultModelRouter(),
                provider_invoker=BoundProviderInvoker(self._invoke_provider_round_impl),
                tool_executor=BoundToolExecutor(self._execute_tool_batch_impl),
                session_projection=EventSessionProjection(),
                post_stage_runner=BoundPostStageRunner(self._execute_post_stage_impl),
                reasoning_resolver=BoundReasoningResolver(self._resolve_invocation_reasoning_impl),
            )
            setattr(self, "_kiro_runtime_seams", seams)
        return seams

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
        rows: list[dict[str, Any]] = []
        for task in registry.list(plan_id=plan_id):
            item = task.public_dict()
            rows.append({
                "task_id": item.get("task_id", ""),
                "name": item.get("name", ""),
                "role": item.get("role", ""),
                "status": item.get("status", ""),
                "provider_id": item.get("provider_id", ""),
                "model_id": item.get("model_id", ""),
                "generation": item.get("generation", 0),
            })
        return rows

    def _checkpoint_durable_run(self, session: Any, name: str, *, data: dict[str, Any] | None = None, critical: bool = False) -> None:
        run_id = str(getattr(session, "active_run_id", "") or "")
        if not run_id:
            return
        try:
            event_seq = len(getattr(session, "events", ()) or ()) - 1
            cp = self._durable_run_store().checkpoint(
                run_id,
                step_id=str(getattr(session, "active_run_step_id", "") or ""),
                attempt_id=str(getattr(session, "active_run_attempt_id", "") or ""),
                name=name,
                session_event_seq=event_seq,
                agent_tasks=self._run_agent_snapshots(session),
                data=dict(data or {}),
            )
            if cp is not None:
                self._runtime_event(
                    "run.checkpoint", session,
                    data={"run_id": run_id, "checkpoint_id": cp.get("checkpoint_id", ""), "name": name},
                )
        except OSError as exc:
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
            strategy = str(recovery_strategy or "").strip().lower()
            if resume_run_id:
                strategy = strategy if strategy in {"retry", "resume", "replan"} else "resume"
                record = store.prepare_resume(
                    str(resume_run_id), session_id=str(session.session_id), strategy=strategy,
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
                step = store.start_step(run_id, kind=step_kind, objective=objectives[strategy])
                lifecycle = f"run/{strategy}"
                runtime_event = {"retry": "run.retried", "resume": "run.resumed", "replan": "run.replanned"}[strategy]
            else:
                record = store.create_run(
                    str(session.session_id),
                    plan_id=str(getattr(session, "active_agent_plan_id", "") or ""),
                    mode=str(getattr(execution_plan, "mode", "single") if execution_plan is not None else "single"),
                    selection=str(selection or ""),
                    session_event_seq=event_seq,
                    metadata={
                        "provider_id": provider_id, "model_id": model_id,
                        "history_start": int(history_start), "user_event_seq": int(user_event_seq),
                    },
                )
                run_id = str(record["run_id"])
                step = store.start_step(run_id, kind="turn", objective="Execute one ACP prompt turn")
                lifecycle = "run/start"
                runtime_event = "run.started"
            step_id = str((step or {}).get("step_id") or "")
            attempt = store.start_attempt(
                run_id, step_id, provider_id=provider_id, model_id=model_id
            )
            session.active_run_id = run_id
            session.active_run_step_id = step_id
            session.active_run_attempt_id = str((attempt or {}).get("attempt_id") or "")
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
        except (OSError, ValueError) as exc:
            self.trace(f"durable run start skipped: {type(exc).__name__}")
            return ""

    def _finish_durable_run(
        self, session: Any, *, outcome: str, error_kind: str = "", error_category: str = "",
        provider_id: str = "", model_id: str = "", side_effect_risk: bool = False,
    ) -> None:
        run_id = str(getattr(session, "active_run_id", "") or "")
        if not run_id:
            return
        step_id = str(getattr(session, "active_run_step_id", "") or "")
        attempt_id = str(getattr(session, "active_run_attempt_id", "") or "")
        status = "completed" if outcome == "success" else "cancelled" if outcome == "cancelled" else "failed"
        attempt_status = "completed" if status == "completed" else "cancelled" if status == "cancelled" else "failed"
        if side_effect_risk and status != "completed":
            recovery_state = "unknown_side_effect"
        elif status == "failed" and str(error_category or "") in {"transport", "timeout", "rate_limit", "overloaded"}:
            recovery_state = "retryable"
        elif status == "failed":
            recovery_state = "replan_required"
        else:
            recovery_state = "clean"
        try:
            self._checkpoint_durable_run(
                session, "terminal",
                data={"outcome": outcome, "error_kind": error_kind, "error_category": error_category, "recovery_state": recovery_state},
            )
            store = self._durable_run_store()
            if step_id and attempt_id:
                store.finish_attempt(
                    run_id, step_id, attempt_id, status=attempt_status, provider_id=provider_id,
                    model_id=model_id, error_kind=error_kind, error_category=error_category,
                )
            if step_id:
                store.finish_step(run_id, step_id, status=status)
            event_seq = len(getattr(session, "events", ()) or ()) - 1
            store.finish_run(run_id, status=status, recovery_state=recovery_state, session_event_seq=event_seq)
            payload = {"run_id": run_id, "status": status, "recovery_state": recovery_state}
            append_lifecycle(session, f"run/{status}", payload)
            self._runtime_event(f"run.{status}", session, data=payload)
        except OSError as exc:
            self.trace(f"durable run finish skipped: {type(exc).__name__}")
        finally:
            session.active_run_id = ""
            session.active_run_step_id = ""
            session.active_run_attempt_id = ""

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
            if (updated.provider_id, updated.model_id) != (previous_provider, previous_model):
                data = {
                    "plan_id": plan_id,
                    "task_id": updated.task_id,
                    "role": updated.spec.role,
                    "status": updated.status,
                    "provider_id": updated.provider_id,
                    "model": updated.model_id,
                }
                append_lifecycle(session, "agent/rerouted", data)
                self._runtime_event("agent.rerouted", session, data=data)
            return
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
        if error:
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


    def _apply_reasoning_language_policy(
        self, session: Any, preference: str, current_text: str
    ) -> None:
        """Resolve and attach the per-turn visible-reasoning language policy.

        The decision is ephemeral session state so every provider round, relay
        worker and detached reviewer sees the same language contract. It is
        recomputed on the next user turn (and on mid-turn steering) rather than
        persisted as conversation truth.
        """
        with session.state_lock:
            history = list(getattr(session, "messages", ()) or ())
        fallback = str(getattr(session, "ui_language_preference", "auto") or "auto")
        decision = resolve_reasoning_language(
            preference, current_text, history, fallback_language=fallback
        )
        session.reasoning_language_preference = decision.preference
        session.reasoning_language = decision.language
        session.reasoning_language_source = decision.source
        session.reasoning_language_instruction = decision.instruction
        response, source, instruction = resolve_response_language(
            str(getattr(session, "response_language_preference", "auto") or "auto"),
            current_text, history, fallback_language=fallback
        )
        session.response_language = response
        session.response_language_source = source
        session.response_language_instruction = instruction
        session.language_instruction = f"{decision.instruction}\n{instruction}"
        self._runtime_event(
            "reasoning.language.resolved",
            session,
            data={
                "preference": decision.preference,
                "language": decision.language,
                "source": decision.source,
            },
        )

    @staticmethod
    def _tool_interrupt_reason(session: Any) -> str:
        """Map ACP control-plane state to one native-tool interruption reason."""
        if session.cancel_event.is_set():
            return "cancelled"
        if session.steer_event.is_set():
            return "steered"
        return ""

    @staticmethod
    def _rollback_current_turn(session: Any, history_start: int, *, reason: str = "turn_failure") -> None:
        with session.state_lock:
            truncate_messages(session, history_start, reason="turn_rollback")
            append_lifecycle(
                session, "turn/rollback",
                {"history_start": int(history_start), "reason": str(reason or "turn_failure")},
            )

    def _recover_current_turn(
        self, session: Any, history_start: int, *, reason: str, preserve_side_effects: bool,
        partial_text: str = "",
    ) -> None:
        """Return a failed/cancelled turn to a reusable session state.

        Read-only/pristine turns can be transactionally truncated. Once a tool
        with possible external side effects has been admitted, the transcript is
        retained so a later turn can see what was already attempted instead of
        blindly replaying it. A hidden system boundary warns the next model not
        to repeat mutations automatically.
        """
        if not preserve_side_effects:
            self._rollback_current_turn(session, history_start, reason=reason)
            self._runtime_event(
                "turn.rollback", session,
                data={"history_start": int(history_start), "reason": reason, "preserved_side_effects": False},
            )
        else:
            with session.state_lock:
                append_lifecycle(
                    session, "turn/recovery_preserved",
                    {"history_start": int(history_start), "reason": str(reason or "turn_failure")},
                )
                # Persist any provider text the user already saw before the
                # interruption, so a cancel does not silently erase it from the
                # transcript. Only on the preserve path -- a rollback turn is
                # transactionally truncated and the partial text goes with it.
                if partial_text.strip():
                    append_message(
                        session,
                        {"role": "assistant", "content": partial_text},
                    )
                append_message(
                    session,
                    {
                        "role": "system",
                        "content": (
                            "Recovery boundary: the previous turn stopped after one or more tools with "
                            "possible external side effects were admitted. Treat completed tool results "
                            "above as authoritative and do not automatically repeat mutating actions. "
                            "If an attempted action has no result, verify its real-world state before retrying."
                        ),
                    },
                )
            self._runtime_event(
                "turn.recovery_preserved", session,
                data={"history_start": int(history_start), "reason": reason, "preserved_side_effects": True},
            )
        # _persist_session already swallows and traces OSError internally; an
        # outer try/except OSError here was dead code and is intentionally gone.
        self._persist_session(session)

    @staticmethod
    def _tool_effect_class(session: Any, name: str) -> str:
        """Classify retry risk without trusting tool output text.

        MCP annotations are hints, so unknown third-party tools fail closed as
        possible writes. Native tools have app-owned capability contracts.
        """
        if not name:
            return "unknown_write"
        if name == DEFERRED_TOOL_SEARCH_NAME:
            return "pure"
        native = getattr(session, "native", None)
        if native is not None and native.has_tool(name):
            try:
                capability = str(native.capability_for(name) or "")
            except Exception:
                capability = ""
            if capability in _RELAY_GATHERING_CAPABILITIES:
                return "read"
            if name == "write":
                return "idempotent_write"
            if name in _MUTATING_NATIVE_TOOLS or capability in {"fs_write", "shell"}:
                return "non_idempotent_write"
            return "unknown_write"
        mcp = getattr(session, "mcp", None)
        binding = getattr(mcp, "bindings", {}).get(name) if mcp is not None else None
        annotations = getattr(binding, "annotations", {}) if binding is not None else {}
        if isinstance(annotations, dict):
            if annotations.get("readOnlyHint") is True:
                return "read"
            if annotations.get("idempotentHint") is True and annotations.get("destructiveHint") is not True:
                return "idempotent_write"
            if annotations.get("destructiveHint") is True:
                return "non_idempotent_write"
        return "unknown_write"

    def _consume_queued_steers_for_turn(
        self, session: Any, partial_assistant: str = ""
    ) -> bool:
        steers = self._consume_steers(session)
        if not steers:
            return False
        with session.state_lock:
            if partial_assistant:
                append_message(session, {"role": "assistant", "content": partial_assistant})
            for steer_text in steers:
                append_message(session, {"role": "user", "content": steer_text})
                append_lifecycle(session, "turn/steer", {"chars": len(steer_text)})
        for steer_text in steers:
            self._apply_reasoning_language_policy(
                session,
                str(getattr(session, "reasoning_language_preference", "auto") or "auto"),
                steer_text,
            )
            self._emit_steer_consumed(session, steer_text)
        return True

    @staticmethod
    def _active_tools_for_turn(session: Any) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        if session.native is not None:
            available = {spec.name for spec in session.native.specs()}
            visible = session.agent.allowed_native_names(available)
            tools.extend(session.native.tool_specs(visible))
        if session.mcp is not None and session.mcp.servers and session.agent.allows_any_mcp():
            visible_loaded = [
                name
                for name in session.loaded_tool_names
                if (binding := session.mcp.bindings.get(name)) is not None
                and session.agent.allows_mcp(
                    server_name=binding.server_name,
                    tool_name=binding.tool_name,
                    public_name=binding.public_name,
                )
            ]
            tools.extend(
                [
                    session.mcp.deferred_tool_search_schema(),
                    *session.mcp.tool_specs(visible_loaded),
                ]
            )
        return tools

    @staticmethod
    def _append_tool_result(session: Any, call_id: str, result_text: str) -> None:
        with session.state_lock:
            append_message(
                session,
                {"role": "tool", "tool_call_id": call_id, "content": result_text},
            )

    def _deferred_tool_search_result(
        self, session: Any, arguments: dict[str, Any]
    ) -> tuple[str, float, int]:
        if session.mcp is None:
            return (
                json.dumps(
                    {"loaded": [], "error": "Crew tool bridge unavailable"},
                    ensure_ascii=False,
                ),
                0.0,
                0,
            )
        matches, waited_ms = session.mcp.search_tools(
            str(arguments.get("query") or ""),
            limit=arguments.get("limit", 8),
            server=str(arguments.get("server") or ""),
            timeout=20.0,
            allowed=lambda binding: session.agent.allows_mcp(
                server_name=binding.server_name,
                tool_name=binding.tool_name,
                public_name=binding.public_name,
            ),
        )
        loaded_names = [binding.public_name for binding in matches]
        with session.state_lock:
            existing = set(session.loaded_tool_names)
            newly_loaded = sum(1 for name in loaded_names if name not in existing)
            for name in loaded_names:
                if name in session.loaded_tool_names:
                    session.loaded_tool_names.remove(name)
                session.loaded_tool_names.append(name)
            if len(session.loaded_tool_names) > DEFERRED_TOOL_ACTIVE_LIMIT:
                del session.loaded_tool_names[:-DEFERRED_TOOL_ACTIVE_LIMIT]
        result_text = json.dumps(
            {
                "loaded": [
                    {
                        "name": binding.public_name,
                        "server": binding.server_name,
                        "description": binding.description[:240],
                    }
                    for binding in matches
                ],
                "loaded_count": len(matches),
                "newly_loaded": newly_loaded,
                "active_real_tools": len(session.loaded_tool_names),
                "instruction": (
                    "The listed function definitions are now available on the next "
                    "model step. Call the best match directly, or search again with "
                    "different keywords if none fits."
                ),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self._runtime_event(
            "tool.search.completed",
            session,
            data={
                "query_chars": len(str(arguments.get("query") or "")),
                "loaded_count": len(matches),
                "newly_loaded": newly_loaded,
                "wait_ms": waited_ms,
            },
            content={"arguments": arguments, "result": result_text},
        )
        return result_text, waited_ms, newly_loaded

    def _execute_native_tool(
        self, session: Any, call: Any, arguments: dict[str, Any]
    ) -> tuple[str, float, str]:
        self._runtime_event(
            "tool.call.started",
            session,
            data={"kind": "native", "call_id": call.call_id, "tool": call.name},
            content={"arguments": arguments},
        )
        invocation: NativeInvocation | None = None
        try:
            invocation = session.native.validate(call.name, arguments)
        except ToolValidationError as exc:
            result_text = f"Error: invalid {call.name} arguments: {exc}"
            self._emit_tool_call(session, call.call_id, call.name, arguments)
            self._emit_tool_status(session, call.call_id, status="failed", text=result_text)
            session.native.audit.record(
                session_id=session.session_id,
                call_id=call.call_id,
                tool=call.name,
                capability=session.native.capability_for(call.name),
                permission="not_requested",
                outcome="validation_failed",
                exit_code=None,
                detail=str(exc),
            )
            return result_text, 0.0, ""

        decision = session.native.permission_engine.evaluate(invocation)
        if decision is PermissionDecision.DENY:
            result_text = (
                f"Permission denied by the Kiro BYO native policy for {invocation.spec.name}."
            )
            self._emit_tool_call(session, call.call_id, invocation.spec.name, invocation.arguments)
            self._emit_tool_status(session, call.call_id, status="refused", text=result_text)
            session.native.audit.record(
                session_id=session.session_id,
                call_id=call.call_id,
                tool=invocation.spec.name,
                capability=invocation.spec.capability,
                permission="local_deny",
                outcome="refused",
                exit_code=None,
            )
            return result_text, 0.0, ""

        # Filesystem targets outside cwd/additionalDirectories are not an
        # automatic hard failure. Ask Crew/the user for that exact tool call,
        # then grant only the narrow directory roots needed by the invocation.
        # ``allow_once`` is revoked immediately after execution; ``allow_always``
        # lives only in this NativeToolRuntime/session and is never persisted.
        try:
            scope_roots = session.native.required_scope_roots(invocation)
        except (SandboxError, NativeToolError, OSError) as exc:
            result_text = f"Error: {exc}"
            self._emit_tool_call(session, call.call_id, invocation.spec.name, invocation.arguments)
            self._emit_tool_status(session, call.call_id, status="failed", text=result_text)
            session.native.audit.record(
                session_id=session.session_id,
                call_id=call.call_id,
                tool=invocation.spec.name,
                capability=invocation.spec.capability,
                permission="not_requested",
                outcome="scope_resolution_failed",
                exit_code=None,
                detail=str(exc),
            )
            return result_text, 0.0, ""

        permission_choice = ""
        added_scope_roots: tuple[Any, ...] = ()
        permission_already_requested = False
        if scope_roots:
            try:
                permission_choice = self._request_tool_permission_choice(
                    session, call.call_id, invocation.spec.name, invocation.arguments
                )
                permission_already_requested = True
            except ClientRequestError as exc:
                interruption = self._tool_interrupt_reason(session)
                if interruption == "cancelled":
                    return "", 0.0, "cancelled"
                if interruption == "steered":
                    result_text = "Interrupted before execution: user steering changed the task."
                    self._emit_tool_status(
                        session, call.call_id, status="failed", text=result_text
                    )
                    session.native.audit.record(
                        session_id=session.session_id,
                        call_id=call.call_id,
                        tool=invocation.spec.name,
                        capability=invocation.spec.capability,
                        permission="interrupted",
                        outcome="steered",
                        exit_code=130,
                        detail="directory permission wait interrupted by user steering",
                    )
                    return result_text, 0.0, "steered"
                self.trace(
                    f"native directory permission request failed session={session.session_id} "
                    f"tool={invocation.spec.name}: {exc}"
                )
                permission_choice = ""

            if not permission_choice:
                result_text = "Permission denied by the KiroCrew host."
                self._emit_tool_status(session, call.call_id, status="refused", text=result_text)
                session.native.audit.record(
                    session_id=session.session_id,
                    call_id=call.call_id,
                    tool=invocation.spec.name,
                    capability=invocation.spec.capability,
                    permission="scope_denied",
                    outcome="refused",
                    exit_code=None,
                    touched_paths=tuple(str(root) for root in scope_roots),
                )
                return result_text, 0.0, ""

            try:
                added_scope_roots = session.native.grant_scope_roots(scope_roots)
            except (SandboxError, NativeToolError, OSError) as exc:
                result_text = f"Error: approved directory scope could not be applied: {exc}"
                self._emit_tool_status(session, call.call_id, status="failed", text=result_text)
                session.native.audit.record(
                    session_id=session.session_id,
                    call_id=call.call_id,
                    tool=invocation.spec.name,
                    capability=invocation.spec.capability,
                    permission=permission_choice,
                    outcome="scope_grant_failed",
                    exit_code=None,
                    detail=str(exc),
                )
                return result_text, 0.0, ""

        try:
            session.native.preflight(invocation)
        except (SandboxError, NativeToolError, OSError) as exc:
            if permission_choice == "allow_once" and added_scope_roots:
                session.native.revoke_scope_roots(added_scope_roots)
            result_text = f"Error: {exc}"
            if not permission_already_requested:
                self._emit_tool_call(session, call.call_id, invocation.spec.name, invocation.arguments)
            self._emit_tool_status(session, call.call_id, status="failed", text=result_text)
            session.native.audit.record(
                session_id=session.session_id,
                call_id=call.call_id,
                tool=invocation.spec.name,
                capability=invocation.spec.capability,
                permission=permission_choice or "not_requested",
                outcome="preflight_failed",
                exit_code=None,
                detail=str(exc),
            )
            return result_text, 0.0, ""

        auto_approved = session.agent.auto_approves_native(invocation.spec.name)
        if permission_already_requested:
            allowed = True
        elif auto_approved:
            # ``allowedTools`` skips ordinary per-call prompting, but it never
            # silently widens filesystem roots. Out-of-scope access took the
            # explicit directory permission path above.
            self._emit_tool_call(session, call.call_id, invocation.spec.name, invocation.arguments)
            allowed = True
        else:
            try:
                allowed = self._request_tool_permission(
                    session, call.call_id, invocation.spec.name, invocation.arguments
                )
            except ClientRequestError as exc:
                interruption = self._tool_interrupt_reason(session)
                if interruption == "cancelled":
                    return "", 0.0, "cancelled"
                if interruption == "steered":
                    result_text = "Interrupted before execution: user steering changed the task."
                    self._emit_tool_status(
                        session, call.call_id, status="failed", text=result_text
                    )
                    session.native.audit.record(
                        session_id=session.session_id,
                        call_id=call.call_id,
                        tool=invocation.spec.name,
                        capability=invocation.spec.capability,
                        permission="interrupted",
                        outcome="steered",
                        exit_code=130,
                        detail="permission wait interrupted by user steering",
                    )
                    return result_text, 0.0, "steered"
                allowed = False
                self.trace(
                    f"native permission request failed session={session.session_id} "
                    f"tool={invocation.spec.name}: {exc}"
                )
        if not allowed:
            if permission_choice == "allow_once" and added_scope_roots:
                session.native.revoke_scope_roots(added_scope_roots)
            result_text = "Permission denied by the KiroCrew host."
            self._emit_tool_status(session, call.call_id, status="refused", text=result_text)
            session.native.audit.record(
                session_id=session.session_id,
                call_id=call.call_id,
                tool=invocation.spec.name,
                capability=invocation.spec.capability,
                permission="denied",
                outcome="refused",
                exit_code=None,
            )
            return result_text, 0.0, ""

        self._emit_tool_status(session, call.call_id, status="in_progress")
        started = time.perf_counter()
        interruption = ""
        try:
            try:
                if invocation.spec.name in {"shell", "execute_shell_process", "control_shell_process"}:
                    native_result = session.native.execute(
                        invocation,
                        cancel_event=session.cancel_event,
                        interrupt_probe=lambda: self._tool_interrupt_reason(session),
                    )
                else:
                    native_result = session.native.execute(invocation)
                result_text = native_result.text
                exit_code = native_result.exit_code
                native_status = "completed" if exit_code == 0 else "failed"
                touched_paths = native_result.touched_paths
                interruption = native_result.interruption
                detail = ""
            except (SandboxError, NativeToolError, OSError) as exc:
                result_text = f"Error: {exc}"
                native_status = "failed"
                exit_code = None
                touched_paths = ()
                detail = str(exc)
        finally:
            if permission_choice == "allow_once" and added_scope_roots:
                session.native.revoke_scope_roots(added_scope_roots)

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if interruption == "steered":
            native_status = "failed"
        self._emit_tool_status(session, call.call_id, status=native_status, text=result_text)
        session.native.audit.record(
            session_id=session.session_id,
            call_id=call.call_id,
            tool=invocation.spec.name,
            capability=invocation.spec.capability,
            permission=(
                "scope_once" if permission_choice == "allow_once"
                else "scope_session" if permission_choice in {"allow_always", "allow"}
                else "allowed"
            ),
            outcome=interruption or ("cancelled" if session.cancel_event.is_set() else native_status),
            exit_code=exit_code,
            touched_paths=touched_paths,
            detail=detail,
        )
        self._runtime_event(
            "tool.call.completed",
            session,
            data={
                "kind": "native",
                "call_id": call.call_id,
                "tool": invocation.spec.name,
                "status": native_status,
                "exit_code": exit_code,
                "elapsed_ms": elapsed_ms,
                "interruption": interruption,
            },
            content={"arguments": invocation.arguments, "result": result_text},
        )
        if session.cancel_event.is_set():
            return result_text, elapsed_ms, "cancelled"
        if interruption == "steered" or session.steer_event.is_set():
            return result_text, elapsed_ms, "steered"
        return result_text, elapsed_ms, ""

    def _execute_mcp_tool(
        self, session: Any, call: Any, arguments: dict[str, Any]
    ) -> tuple[str, float, str]:
        self._runtime_event(
            "tool.call.started",
            session,
            data={"kind": "mcp", "call_id": call.call_id, "tool": call.name},
            content={"arguments": arguments},
        )
        if session.mcp is None:
            result_text = f"Error: tool bridge unavailable for {call.name}"
            self._emit_tool_status(session, call.call_id, status="failed", text=result_text)
            return result_text, 0.0, ""
        binding = session.mcp.bindings.get(call.name)
        auto_approved = bool(
            binding is not None
            and session.agent.auto_approves_mcp(
                server_name=binding.server_name,
                tool_name=binding.tool_name,
                public_name=call.name,
            )
        )
        if auto_approved:
            self._emit_tool_call(session, call.call_id, call.name, arguments)
            allowed = True
        else:
            try:
                allowed = self._request_tool_permission(
                    session, call.call_id, call.name, arguments
                )
            except ClientRequestError as exc:
                interruption = self._tool_interrupt_reason(session)
                if interruption == "cancelled":
                    return "", 0.0, "cancelled"
                if interruption == "steered":
                    result_text = "Interrupted before execution: user steering changed the task."
                    self._emit_tool_status(session, call.call_id, status="failed", text=result_text)
                    return result_text, 0.0, "steered"
                allowed = False
                self.trace(
                    f"permission request failed session={session.session_id} "
                    f"tool={call.name}: {exc}"
                )
        if not allowed:
            result_text = "Permission denied by the KiroCrew host."
            self._emit_tool_status(session, call.call_id, status="refused", text=result_text)
            return result_text, 0.0, ""

        self._emit_tool_status(session, call.call_id, status="in_progress")
        started = time.perf_counter()
        result_text = session.mcp.call(call.name, arguments)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        status = "failed" if result_text.startswith("Error:") else "completed"
        self._emit_tool_status(session, call.call_id, status=status, text=result_text)
        self._runtime_event(
            "tool.call.completed",
            session,
            data={"kind": "mcp", "call_id": call.call_id, "tool": call.name, "status": status, "elapsed_ms": elapsed_ms},
            content={"arguments": arguments, "result": result_text},
        )
        return result_text, elapsed_ms, ""

    @staticmethod
    def _relay_gathering_tool(session: Any, name: str) -> bool:
        """Whether a tool only gathers information (safe for the fast teammate).

        Fail-closed: MCP/third-party tools, shell and every write are NOT
        gathering, because their side effects cannot be proven from here.
        """
        if not name:
            return False
        if name == DEFERRED_TOOL_SEARCH_NAME:
            return True
        native = getattr(session, "native", None)
        if native is None or not native.has_tool(name):
            return False
        try:
            return native.capability_for(name) in _RELAY_GATHERING_CAPABILITIES
        except Exception:
            return False

    def _relay_teammate(
        self, decision: Any, provider: Any, model: Any, *, config: Any = None, signals: Any = None,
        excluded_models: set[tuple[str, str]] | None = None,
    ) -> Any:
        """Pick a relay teammate without reusing the leader's task ranking.

        When the routing config is available, worker selection is recomputed
        from the current subtask signals and the policy's collaboration tier.
        Provider affinity is a preference only. The legacy candidate scan is
        retained for direct/internal callers that do not pass a config.
        """
        if config is not None and decision is not None:
            worker = self._runtime_seams().model_router.collaboration(
                config, decision.execution_intent, signals or decision.signals,
                leader_provider=provider, leader_model=model,
                excluded_models=excluded_models,
            )
            if worker is not None:
                return worker.provider, worker.model
            return None
        for row in getattr(decision, "candidates", ()) or ():
            if row.provider.id != provider.id or row.model.model_id == model.model_id:
                continue
            if not model_in_tier(row.model, "fast"):
                continue
            return row.provider, row.model
        return None

    @staticmethod
    def _agent_runtime_for_session(session: Any) -> AgentRuntime | None:
        registry = getattr(session, "agent_registry", None)
        if registry is None:
            return None
        runtime = getattr(session, "_kiro_agent_runtime", None)
        if runtime is None:
            runtime = AgentRuntime(registry, max_workers=8)
            setattr(session, "_kiro_agent_runtime", runtime)
        return runtime

    @staticmethod
    def _shutdown_agent_runtime(session: Any) -> None:
        runtime = getattr(session, "_kiro_agent_runtime", None)
        if runtime is None:
            return
        try:
            runtime.shutdown(wait=False)
        finally:
            try:
                delattr(session, "_kiro_agent_runtime")
            except AttributeError:
                pass

    @staticmethod
    def _subagent_child_session(
        parent: Any, task: AgentTask, messages: list[dict[str, Any]], *,
        native: Any = None, cwd: str = "",
    ) -> Any:
        """Create an isolated transcript view while sharing safe host resources.

        Native/MCP runtimes belong to the ACP session and are intentionally
        shared; the child owns its transcript, loaded-tool view and cancellation
        token. Child tool policy remains read-only and fail-closed.
        """
        return SimpleNamespace(
            session_id=str(getattr(parent, "session_id", "")),
            cwd=str(cwd or getattr(parent, "cwd", "")),
            model=str(getattr(parent, "model", "")),
            agent=getattr(parent, "agent", None),
            messages=messages,
            events=[],
            mcp=getattr(parent, "mcp", None),
            native=native if native is not None else getattr(parent, "native", None),
            state_lock=threading.RLock(),
            cancel_event=task.cancel_event,
            steer_event=threading.Event(),
            steer_queue=[],
            loaded_tool_names=list(getattr(parent, "loaded_tool_names", ()) or ()),
            additional_directories=tuple(getattr(parent, "additional_directories", ()) or ()),
            reasoning_language_instruction=str(getattr(parent, "reasoning_language_instruction", "") or ""),
            response_language_instruction=str(getattr(parent, "response_language_instruction", "") or ""),
            language_instruction=str(getattr(parent, "language_instruction", "") or ""),
            response_language_preference=str(getattr(parent, "response_language_preference", "auto") or "auto"),
            reasoning_language=str(getattr(parent, "reasoning_language", "") or ""),
            reasoning_language_preference=str(getattr(parent, "reasoning_language_preference", "auto") or "auto"),
            active_turn=True,
        )

    def _run_isolated_subagent(
        self,
        parent: Any,
        task: AgentTask,
        provider: Any,
        model: Any,
        *,
        reasoning_profile: Any,
        reasoning_signals: Any,
        runtime_mode_context: str,
    ) -> AgentResult:
        """Run one read-only child with an isolated transcript and sticky model."""
        registry = getattr(parent, "agent_registry", None)
        started = time.perf_counter()
        if registry is None:
            return AgentResult(status="failed", summary="agent registry unavailable")
        registry.pin_model(task.task_id, provider.id, model.model_id)
        messages = [dict(row) for row in registry.context_snapshot(task.task_id)]
        for continuation in registry.consume_messages(task.task_id):
            messages.append({"role": "user", "content": f"[Continuation from parent]\n{continuation}"})
        writer_mode = task.spec.tool_policy == "isolated_write"
        writer_native = None
        if writer_mode:
            if not task.workspace_path:
                return AgentResult(
                    status="failed", unresolved=("isolated writer has no workspace",),
                    provider_id=provider.id, model_id=model.model_id,
                )
            try:
                writer_native = NativeToolRuntime(task.workspace_path)
            except (OSError, SandboxError) as exc:
                return AgentResult(
                    status="failed", unresolved=(f"writer workspace unavailable: {exc}",),
                    provider_id=provider.id, model_id=model.model_id,
                )
        child = self._subagent_child_session(
            parent, task, messages, native=writer_native, cwd=task.workspace_path if writer_mode else "",
        )
        writer_native_closed = False

        def _close_writer() -> None:
            nonlocal writer_native_closed
            if writer_native is not None and not writer_native_closed:
                writer_native_closed = True
                try:
                    writer_native.close()
                except Exception:
                    # Cleanup must not mask the child result. The worktree itself
                    # remains managed independently by WorkspaceManager.
                    pass

        def finish(result: AgentResult) -> AgentResult:
            _close_writer()
            return result

        try:
            max_turns = max(1, min(12, int(task.spec.max_turns or 6)))
            usage = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "reasoning_tokens": 0, "provider_rounds": 0, "tool_calls": 0, "transport_retries": 0}
            summary = ""
            error = ""

            for child_step in range(1, max_turns + 1):
                if child_step > 1:
                    # A parent may steer a long-lived child by task_id. Messages are
                    # consumed only at model boundaries, never injected mid-response.
                    for continuation in registry.consume_messages(task.task_id):
                        messages.append({
                            "role": "user",
                            "content": f"[Continuation from parent]\n{continuation}",
                        })
                if task.cancel_event.is_set() or getattr(parent, "cancel_event", threading.Event()).is_set():
                    registry.update_context(task.task_id, messages)
                    return finish(AgentResult(
                        status="cancelled", summary=summary, provider_id=provider.id, model_id=model.model_id,
                        duration_ms=(time.perf_counter() - started) * 1000.0, usage=usage,
                    ))
                thinking, effort = self._runtime_seams().reasoning_resolver.resolve(
                    model, child, reasoning_profile, reasoning_signals, role="worker", stage="gathering"
                ) if reasoning_profile is not None else ("", "")
                tools = []
                for tool in self._active_tools_for_turn(child):
                    name = str(((tool or {}).get("function") or {}).get("name") or "")
                    # Child agents never inherit third-party MCP authority. A writer
                    # may use native tools only inside its isolated worktree; a reader
                    # remains limited to proven read-only native capabilities.
                    if name == DEFERRED_TOOL_SEARCH_NAME:
                        continue
                    if writer_mode:
                        if child.native is not None and child.native.has_tool(name):
                            tools.append(tool)
                    elif self._relay_gathering_tool(child, name):
                        tools.append(tool)
                system_messages = [
                    *([{"role": "system", "content": child.agent.prompt}] if getattr(child.agent, "prompt", "") else []),
                    self._runtime_system_message(
                        provider, model, effort, thinking_mode=thinking,
                        deferred_tool_search=False, mcp_server_names=(),
                        native_tool_names=tuple(
                            str(((tool or {}).get("function") or {}).get("name") or "") for tool in tools
                        ),
                        mode_context=runtime_mode_context,
                        reasoning_language_context=str(getattr(child, "language_instruction", "") or ""),
                    ),
                    {
                        "role": "system",
                        "content": (
                            (
                                "Kiro BYO isolated writer. You work only inside your private git worktree. "
                                f"Your objective is: {task.spec.objective}. Implement a candidate solution, run only "
                                "the checks needed for your candidate, never modify the parent workspace, and return "
                                "a concise hand-back describing files changed, checks run, and unresolved risk."
                            ) if writer_mode else (
                                "Kiro BYO isolated subagent. You are a read-only child of the lead agent. "
                                f"Your objective is: {task.spec.objective}. "
                                "Inspect independently and avoid duplicating obvious work from other children. "
                                "You may only use the read-only tools exposed to you. Never propose or perform writes, "
                                "and do not answer the end user. When enough evidence is gathered, return a concise "
                                "factual hand-back containing findings, evidence and unresolved uncertainty."
                            )
                        ),
                    },
                ]
                provider_messages = [
                    *system_messages,
                    *self._messages_for_provider(messages, provider, model),
                ]
                self._runtime_event(
                    "agent.subagent.provider_started", parent,
                    data={
                        "task_id": task.task_id, "name": task.spec.agent_name, "step": child_step,
                        "provider_id": provider.id, "model": model.model_id, "tool_count": len(tools),
                    },
                )
                round_retries = 0
                while True:
                    try:
                        turn = chat_completion_turn(
                            provider, model.model_id, provider_messages, lambda _chunk: None,
                            on_reasoning=lambda _chunk: None,
                            reasoning_effort=self._reasoning_wire_value(model, thinking, effort) or None,
                            thinking_mode=thinking, model_spec=model,
                            cache_key=f"{parent.session_id}:agent:{task.task_id}", tools=tools,
                            should_interrupt=lambda: task.cancel_event.is_set() or parent.cancel_event.is_set(),
                        )
                        break
                    except ProviderInterrupted as exc:
                        summary = str(getattr(exc, "partial_text", "") or summary)
                        registry.update_context(task.task_id, messages)
                        return finish(AgentResult(
                            status="cancelled" if (task.cancel_event.is_set() or parent.cancel_event.is_set()) else "failed",
                            summary=summary, provider_id=provider.id, model_id=model.model_id,
                            duration_ms=(time.perf_counter() - started) * 1000.0, usage=usage,
                        ))
                    except ProviderError as exc:
                        recovery = RecoveryPolicy.live_provider_failure(
                            exc, pristine=True, side_effect_risk=False,
                            same_model_retries=round_retries, has_fallback=False, can_replan=False,
                        )
                        if recovery.action == "retry_same_model":
                            round_retries += 1
                            usage["transport_retries"] += 1
                            self._runtime_event(
                                "agent.subagent.retry", parent,
                                data={
                                    "task_id": task.task_id, "name": task.spec.agent_name,
                                    "step": child_step, "attempt": round_retries,
                                    "provider_id": provider.id, "model": model.model_id,
                                    "error": type(exc).__name__, "reason": recovery.reason,
                                },
                            )
                            continue
                        error = f"{type(exc).__name__}: {exc}"
                        registry.update_context(task.task_id, messages)
                        return finish(AgentResult(
                            status="failed", summary=summary, unresolved=(error,),
                            provider_id=provider.id, model_id=model.model_id,
                            duration_ms=(time.perf_counter() - started) * 1000.0, usage=usage,
                        ))

                usage["provider_rounds"] += 1
                usage["tool_calls"] += len(turn.tool_calls)
                usage["input_tokens"] += int(turn.usage.input_tokens)
                usage["output_tokens"] += int(turn.usage.output_tokens)
                usage["cached_tokens"] += int(turn.usage.cached_tokens)
                usage["reasoning_tokens"] += int(turn.usage.reasoning_tokens)
                self._runtime_event(
                    "agent.subagent.provider_completed", parent,
                    data={
                        "task_id": task.task_id, "name": task.spec.agent_name, "step": child_step,
                        "provider_id": provider.id, "model": model.model_id,
                        "tool_calls": len(turn.tool_calls),
                    },
                )
                if not turn.tool_calls:
                    summary = str(turn.text or "").strip()
                    messages.append({"role": "assistant", "content": summary})
                    registry.update_context(task.task_id, messages)
                    return finish(AgentResult(
                        status="completed", summary=summary, provider_id=provider.id, model_id=model.model_id,
                        duration_ms=(time.perf_counter() - started) * 1000.0, usage=usage,
                    ))

                assistant_tool_calls = [
                    {
                        "id": call.call_id, "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments_text},
                    } for call in turn.tool_calls
                ]
                messages.append({"role": "assistant", "content": turn.text or None, "tool_calls": assistant_tool_calls})
                for call in turn.tool_calls:
                    if task.cancel_event.is_set() or parent.cancel_event.is_set():
                        break
                    if writer_mode and child.native is not None and child.native.has_tool(call.name):
                        result_text, _elapsed, outcome = self._execute_native_tool(child, call, call.arguments())
                        if outcome == "cancelled":
                            task.cancel_event.set()
                    elif writer_mode:
                        result_text = "Skipped: isolated writers may only use native tools inside their private worktree."
                    elif not self._relay_gathering_tool(child, call.name):
                        result_text = "Skipped: this subagent is read-only and the requested tool is not proven safe."
                    elif child.native is not None and child.native.has_tool(call.name):
                        # Shared NativeToolRuntime may hold session-scoped sandbox
                        # grants. Serialize the actual tool call while model requests
                        # remain parallel so we never race scope mutation/audit state.
                        tool_lock = getattr(parent, "_kiro_subagent_tool_lock", None)
                        if tool_lock is None:
                            tool_lock = threading.RLock()
                            setattr(parent, "_kiro_subagent_tool_lock", tool_lock)
                        with tool_lock:
                            result_text, _elapsed, outcome = self._execute_native_tool(
                                child, call, call.arguments()
                            )
                        if outcome == "cancelled":
                            task.cancel_event.set()
                    else:
                        result_text = "Skipped: third-party/MCP tools are not admitted to read-only child agents yet."
                    messages.append({"role": "tool", "tool_call_id": call.call_id, "content": result_text})

            registry.update_context(task.task_id, messages)
            return finish(AgentResult(
                status="failed", summary=summary,
                unresolved=(error or f"subagent reached max_turns={max_turns}",),
                provider_id=provider.id, model_id=model.model_id,
                duration_ms=(time.perf_counter() - started) * 1000.0, usage=usage,
            ))
        finally:
            _close_writer()

    def _run_parallel_subagents(
        self, parent: Any, execution_plan: ExecutionPlan, *, routing_config: Any, routing_decision: Any,
        leader_provider: Any, leader_model: Any, reasoning_profile: Any, reasoning_signals: Any,
        runtime_mode_context: str,
    ) -> tuple[tuple[AgentTask, AgentResult], ...]:
        registry = getattr(parent, "agent_registry", None)
        plan_id = str(getattr(parent, "active_agent_plan_id", "") or "")
        runtime = self._agent_runtime_for_session(parent)
        if registry is None or runtime is None or not plan_id:
            return ()
        tasks = tuple(
            task for task in registry.tasks_for_role(plan_id, "worker")
            if task.status in {"planned", "paused"}
        )
        if not tasks:
            return ()
        with parent.state_lock:
            parent_snapshot = [dict(row) for row in getattr(parent, "messages", ()) if isinstance(row, dict)]
        assignments: dict[str, tuple[Any, Any]] = {}
        excluded: set[tuple[str, str]] = {(leader_provider.id, leader_model.model_id)}
        for task in tasks:
            registry.attach_context(task.task_id, parent_snapshot)
            routed = self._relay_teammate(
                routing_decision, leader_provider, leader_model, config=routing_config,
                signals=reasoning_signals, excluded_models=excluded,
            )
            if routed is None:
                # If the configured pool only contains one eligible teammate,
                # reuse it rather than pretending a second independent model exists.
                routed = self._relay_teammate(
                    routing_decision, leader_provider, leader_model, config=routing_config,
                    signals=reasoning_signals, excluded_models=None,
                )
            if routed is None:
                registry.transition(task.task_id, "skipped", result=AgentResult(status="skipped"))
                continue
            provider, model = routed
            registry.pin_model(task.task_id, provider.id, model.model_id)
            assignments[task.task_id] = (provider, model)
            excluded.add((provider.id, model.model_id))

        runnable = tuple(task for task in tasks if task.task_id in assignments)
        if not runnable:
            return ()
        append_lifecycle(
            parent, "agent/subagents_started",
            {
                "plan_id": plan_id, "count": len(runnable),
                "max_parallel": min(int(execution_plan.max_parallel), len(runnable)),
                "task_ids": [task.task_id for task in runnable],
            },
        )
        self._runtime_event(
            "agent.subagents.started", parent,
            data={
                "plan_id": plan_id, "count": len(runnable),
                "max_parallel": min(int(execution_plan.max_parallel), len(runnable)),
                "tasks": [task.public_dict() for task in runnable],
            },
        )

        def runner(task: AgentTask) -> AgentResult:
            provider, model = assignments[task.task_id]
            return self._run_isolated_subagent(
                parent, task, provider, model, reasoning_profile=reasoning_profile,
                reasoning_signals=reasoning_signals, runtime_mode_context=runtime_mode_context,
            )

        completed = runtime.run_group(
            runnable, runner, max_parallel=min(int(execution_plan.max_parallel), len(runnable))
        )
        for task, result in completed:
            data = {
                "plan_id": plan_id, "task_id": task.task_id, "name": task.spec.agent_name,
                "status": result.status, "provider_id": result.provider_id, "model": result.model_id,
                "duration_ms": round(float(result.duration_ms), 3),
            }
            append_lifecycle(parent, f"agent/{result.status}", data)
            self._runtime_event(f"agent.subagent.{result.status}", parent, data=data)
            self._project_agent_state(task, event=result.status)
            team_registry = getattr(parent, "team_registry", None)
            if team_registry is not None:
                team = team_registry.for_plan(plan_id)
                if team is not None:
                    team_registry.mark_member_status(team.team_id, task.task_id, result.status)
            if result.summary:
                with parent.state_lock:
                    append_message(
                        parent,
                        {
                            "role": "system",
                            "content": (
                                f"[Subagent findings: {task.spec.agent_name}; task_id={task.task_id}]\n"
                                f"{result.summary}"
                            ),
                        },
                    )
        self._checkpoint_durable_run(
            parent, "subagents_completed",
            data={"count": len(completed), "task_ids": [task.task_id for task, _ in completed]},
        )
        return completed


    def _resume_agent_task(self, session: Any, task_id: str, *, message: str = "") -> AgentResult:
        """Continue one bounded child through its configured execution backend."""
        registry = getattr(session, "agent_registry", None)
        if registry is None:
            return AgentResult(status="failed", unresolved=("agent registry unavailable",))
        task = registry.get(task_id)
        if task is None:
            return AgentResult(status="failed", unresolved=("unknown agent task",))
        if task.spec.role not in {"worker", "writer", "specialist"}:
            return AgentResult(
                status="failed", unresolved=("this agent role does not support continuation",),
                provider_id=task.provider_id, model_id=task.model_id,
            )
        if task.spec.tool_policy not in {"read_only", "isolated_write"} and task.spec.backend != "acp":
            return AgentResult(
                status="failed", unresolved=("agent tool policy is not safely continuable",),
                provider_id=task.provider_id, model_id=task.model_id,
            )
        # A freshly spawned task is already in the planned state and has not
        # consumed a generation yet. Treat its first execution as generation 0;
        # only completed/paused/cancelled tasks increment generation on resume.
        if task.status == "planned" and task.generation == 0 and task.result is None and not task.started_at:
            if message.strip():
                registry.send_message(task_id, message.strip())
        else:
            registry.resume(task_id, message=message)
        runtime = self._agent_runtime_for_session(session)
        if runtime is None:
            return AgentResult(status="failed", unresolved=("agent runtime unavailable",))

        if task.spec.backend == "acp":
            catalog = getattr(self, "acp_agent_catalog", None) or AcpAgentCatalog()
            definition = catalog.get(task.spec.backend_id or task.model_id)
            if definition is None:
                result = AgentResult(
                    status="failed", provider_id="acp", model_id=task.spec.backend_id or task.model_id,
                    unresolved=("external ACP agent is no longer configured",),
                )
                registry.transition(task_id, "failed", result=result, error=result.unresolved[0])
                return result
            registry.pin_model(task_id, "acp", definition.agent_id)
            completed = runtime.run_group(
                (task,),
                lambda row: run_acp_agent_task(
                    row, definition, cwd=row.workspace_path or str(getattr(session, "cwd", "") or "")
                ),
                max_parallel=1,
            )
        else:
            config = self._config()
            provider = next((row for row in config.providers if row.id == task.provider_id), None)
            model = None
            if provider is not None:
                model = next((row for row in provider.models if row.model_id == task.model_id), None)
            if provider is None or model is None:
                registry.transition(
                    task_id, "failed", error="pinned child model is no longer configured",
                    result=AgentResult(
                        status="failed", provider_id=task.provider_id, model_id=task.model_id,
                        unresolved=("pinned child model is no longer configured",),
                    ),
                )
                current = registry.get(task_id)
                return current.result if current is not None and current.result is not None else AgentResult(status="failed")
            completed = runtime.run_group(
                (task,),
                lambda row: self._run_isolated_subagent(
                    session, row, provider, model, reasoning_profile=None,
                    reasoning_signals=SimpleNamespace(
                        complexity=0.65 if row.spec.tool_policy == "isolated_write" else 0.5,
                        code_task=row.spec.tool_policy == "isolated_write", tools_required=True, has_image=False
                    ),
                    runtime_mode_context="",
                ),
                max_parallel=1,
            )
        result = completed[0][1] if completed else AgentResult(status="failed")
        if task.spec.tool_policy == "isolated_write" and task.workspace_id:
            manager = getattr(self, "workspace_manager", None)
            if manager is not None:
                try:
                    diff = manager.diff(task.workspace_id)
                    result = AgentResult(
                        status=result.status,
                        summary=(result.summary + f"\n\n[Isolated workspace: {task.workspace_id}; diff_chars={len(diff)}]").strip(),
                        findings=result.findings,
                        evidence=tuple(result.evidence) + (f"workspace:{task.workspace_id}",),
                        unresolved=result.unresolved, provider_id=result.provider_id, model_id=result.model_id,
                        duration_ms=result.duration_ms, usage={**result.usage, "workspace_diff_chars": len(diff)},
                    )
                    registry.transition(task.task_id, result.status, result=result)
                except Exception as exc:
                    self._runtime_event(
                        "agent.workspace.diff_failed", session,
                        data={"task_id": task.task_id, "workspace_id": task.workspace_id, "error": type(exc).__name__},
                    )
        self._runtime_event(
            "agent.subagent.continued", session,
            data={
                "task_id": task_id, "generation": task.generation, "status": result.status,
                "provider_id": result.provider_id, "model": result.model_id,
                "backend": task.spec.backend, "workspace": task.workspace_path,
            },
        )
        current = registry.get(task_id)
        if current is not None:
            self._project_agent_state(current, event="continued")
        return result


    @staticmethod
    def _scoped_provider_context(raw: Any, provider: Any, model: Any, *, branch: str = "main") -> dict[str, Any] | None:
        """Attach provenance to provider-private continuation state.

        Provider adapters intentionally own the shape of ``data``.  The turn
        runner owns the cross-provider/model safety boundary, so every blob
        persisted in shared session history is stamped here.
        """
        if not isinstance(raw, dict):
            return None
        scoped = dict(raw)
        scoped["provider_id"] = str(getattr(provider, "id", "") or scoped.get("provider_id") or "")
        scoped["transport"] = str(getattr(provider, "type", "") or scoped.get("transport") or "")
        scoped["model_id"] = str(getattr(model, "model_id", "") or "")
        scoped["branch"] = str(branch or "main")
        return scoped

    @staticmethod
    def _messages_for_provider(messages: list[dict[str, Any]], provider: Any, model: Any) -> list[dict[str, Any]]:
        """Filter continuation blobs that do not belong to this exact model.

        Older persisted sessions have no model/branch provenance. They degrade
        safely by dropping only the private blob; user-visible transcript text
        remains intact and is replayed normally.
        """
        provider_id = str(getattr(provider, "id", "") or "")
        transport = str(getattr(provider, "type", "") or "")
        model_id = str(getattr(model, "model_id", "") or "")
        out: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            copied = dict(message)
            raw = copied.get("_kiro_provider_context")
            if isinstance(raw, dict):
                matches = (
                    str(raw.get("provider_id") or "") == provider_id
                    and str(raw.get("transport") or "") == transport
                    and str(raw.get("model_id") or "") == model_id
                    and str(raw.get("branch") or "") == "main"
                )
                if not matches:
                    copied.pop("_kiro_provider_context", None)
            out.append(copied)
        return out

    @staticmethod
    def _review_transcript(session: Any, history_start: int, *, max_chars: int = 48000) -> str:
        """Render the current turn into a provider-neutral review transcript.

        Detached review never replays provider-private continuation blobs or raw
        tool schemas. It sees only the user/assistant/tool text already accepted
        into the session transcript. Keeping this bounded avoids turning the
        quality pass into an accidental context-window denial of service.
        """
        lines: list[str] = []
        with session.state_lock:
            messages = list(session.messages[history_start:])
        # The final assistant draft is supplied separately to the reviewer.
        if messages and messages[-1].get("role") == "assistant":
            messages = messages[:-1]
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "message").upper()
            content = msg.get("content")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                parts: list[str] = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if isinstance(part.get("text"), str):
                        parts.append(part["text"])
                    elif part.get("type") == "image_url":
                        parts.append("[image input]")
                text = "\n".join(parts)
            if msg.get("tool_calls"):
                calls = []
                for call in msg.get("tool_calls") or []:
                    if not isinstance(call, dict):
                        continue
                    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                    calls.append(f"{fn.get('name') or 'tool'}({fn.get('arguments') or ''})")
                if calls:
                    text = (text + "\n" if text else "") + "TOOL CALLS: " + "; ".join(calls)
            if text:
                lines.append(f"{role}: {text}")
        rendered = "\n\n".join(lines)
        if len(rendered) > max_chars:
            rendered = rendered[-max_chars:]
            rendered = "[earlier turn context truncated]\n" + rendered
        return rendered

    @staticmethod
    def _invocation_reasoning_context(
        signals: Any = None, *, role: str = "lead", stage: str = "turn"
    ) -> ReasoningInvocation:
        return ReasoningInvocation(
            complexity=float(getattr(signals, "complexity", 0.0) or 0.0),
            code_task=bool(getattr(signals, "code_task", False)),
            tools_expected=bool(getattr(signals, "tools_expected", False)),
            has_image=bool(getattr(signals, "has_image", False)),
            role=role,
            stage=stage,
        )

    @staticmethod
    def _relay_subtask_snapshot(messages: list[dict[str, Any]]) -> tuple[str, int, int]:
        """Describe the concrete gathering subtask behind the next relay call.

        A read-only worker receives the continuation after one or more
        information-gathering tool calls.  The whole user turn may be very hard,
        while the *current* relay job can be as small as reading one file (or the
        reverse: several lookups may need non-trivial synthesis).  Reasoning
        therefore needs a local signal derived from the most recent tool batch,
        not just the original turn-level complexity.

        Returns ``(subtask_text, tool_call_count, tool_result_chars)``. Provider
        private continuation blobs are deliberately ignored.
        """
        rows = list(messages or [])
        assistant_index = -1
        calls: list[dict[str, Any]] = []
        for index in range(len(rows) - 1, -1, -1):
            message = rows[index]
            if not isinstance(message, dict):
                continue
            raw_calls = message.get("tool_calls")
            if str(message.get("role") or "") == "assistant" and isinstance(raw_calls, list) and raw_calls:
                assistant_index = index
                calls = [call for call in raw_calls if isinstance(call, dict)]
                break
            # Crossing a newer user request means an older tool batch no longer
            # describes the active subtask.
            if str(message.get("role") or "") == "user":
                break

        if assistant_index < 0 or not calls:
            return "", 0, 0

        parts: list[str] = []
        call_ids: set[str] = set()
        for call in calls:
            call_id = str(call.get("id") or "")
            if call_id:
                call_ids.add(call_id)
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = str(fn.get("name") or "tool").strip()
            arguments = str(fn.get("arguments") or "").strip()
            item = f"{name}: {arguments}" if arguments else name
            parts.append(item[:1600])

        result_chars = 0
        for message in rows[assistant_index + 1:]:
            if not isinstance(message, dict) or str(message.get("role") or "") != "tool":
                continue
            tool_call_id = str(message.get("tool_call_id") or "")
            if call_ids and tool_call_id and tool_call_id not in call_ids:
                continue
            result_chars += len(str(message.get("content") or ""))

        return "\n".join(parts), len(calls), result_chars

    @classmethod
    def _relay_subtask_signals(cls, session: Any, base_signals: Any = None) -> Any:
        """Build per-subtask routing signals for a read-only worker invocation.

        The local tool batch determines task shape/code cues.  Result volume and
        fan-out add bounded complexity because comparing several large read
        results is harder than one tiny lookup, but large file contents alone do
        not inherit the original turn's full difficulty.
        """
        with session.state_lock:
            text, call_count, result_chars = cls._relay_subtask_snapshot(list(session.messages))
        if not text:
            return base_signals

        local = build_signals(
            text,
            [],
            has_image=False,
            # A read-only worker exists specifically to continue an information-
            # gathering tool sequence. If tools are available, tool capability
            # is therefore a requirement for worker selection, not merely an
            # ambient agent capability.
            tools_required=bool(getattr(base_signals, "tools_available", False)),
            tools_available=bool(getattr(base_signals, "tools_available", False)),
            # Preserve an explicit Crew effort lock so worker routing cannot
            # pick a model that cannot express the user's requested level.
            reasoning_effort=str(getattr(base_signals, "reasoning_effort", "") or ""),
        )
        complexity = float(local.complexity)
        if call_count >= 2:
            complexity += 0.08
        if call_count >= 4:
            complexity += 0.06
        if result_chars >= 12_000:
            complexity += 0.05
        if result_chars >= 60_000:
            complexity += 0.05
        complexity = max(0.0, min(1.0, complexity))

        # Avoid importing dataclasses.replace into the hot module namespace;
        # RoutingSignals is immutable, so reconstruct through its concrete type.
        return type(local)(
            prompt_chars=local.prompt_chars,
            history_chars=local.history_chars,
            history_messages=local.history_messages,
            has_image=local.has_image,
            tools_available=local.tools_available,
            tools_required=local.tools_required,
            reasoning_effort=local.reasoning_effort,
            code_task=local.code_task,
            complexity=complexity,
            estimated_input_tokens=max(
                local.estimated_input_tokens,
                max(1, int(result_chars / 3.6)) if result_chars else 1,
            ),
        )

    @classmethod
    def _resolve_invocation_reasoning_impl(
        cls, model: Any, session: Any, reasoning_profile: Any, signals: Any = None,
        *, role: str = "lead", stage: str = "turn",
    ) -> tuple[str, str]:
        """Resolve thinking and effort for one provider invocation.

        Explicit user controls are hard locks. AUTO delegates to the generic
        policy-pack strategy; concrete model capabilities remain authoritative.
        """
        context = cls._invocation_reasoning_context(signals, role=role, stage=stage)
        thinking_strategy = str(getattr(reasoning_profile, "thinking", "inherit") or "inherit")
        effort_strategy = str(getattr(reasoning_profile, "effort", "inherit") or "inherit")
        suggested = thinking_auto_suggestion(thinking_strategy, context)
        thinking = resolve_thinking_mode(
            model, getattr(model, "thinking_preference", "auto"), suggested
        )
        effort = resolve_effort_level(
            model,
            user_level=str(getattr(session, "effort", "") or ""),
            user_locked=bool(getattr(session, "effort_user_set", False)),
            strategy=effort_strategy,
            context=context,
            effective_thinking=thinking,
        )
        return thinking, effort

    def _resolve_invocation_reasoning(
        self, model: Any, session: Any, reasoning_profile: Any, signals: Any = None,
        *, role: str = "lead", stage: str = "turn",
    ) -> tuple[str, str]:
        """Compatibility facade routed through the replaceable reasoning seam."""
        return self._runtime_seams().reasoning_resolver.resolve(
            model, session, reasoning_profile, signals, role=role, stage=stage
        )

    @staticmethod
    def _type_thinking_suggestion(reasoning_intent: str) -> str:
        """Translate only the Type's coarse intent into an Auto recommendation.

        This never overrides the per-model user preference. Multi/inherit stays
        undecided here; its task-sensitive Auto decision is a separate routing
        concern and may provide a stronger per-turn hint later.
        """
        intent = str(reasoning_intent or "inherit").lower()
        if intent == "economy":
            return "off"
        if intent in {"high", "maximum"}:
            return "on"
        return ""

    @classmethod
    def _review_effort(cls, model: Any, session: Any, reasoning_intent: str) -> str:
        """Map a Type effort intent without crossing the thinking hard lock."""
        effective_thinking = resolve_thinking_mode(
            model,
            getattr(model, "thinking_preference", "auto"),
            cls._type_thinking_suggestion(reasoning_intent),
        )
        if effective_thinking == "off":
            return ""
        current = str(getattr(session, "effort", "") or "").lower()
        if bool(getattr(session, "effort_user_set", False)):
            return current
        levels = [str(x).lower() for x in getattr(model, "effort_supported_levels", ()) if str(x)]
        if not levels:
            return current
        intent = str(reasoning_intent or "inherit").lower()
        orders = {
            "inherit": (current,),
            "economy": ("low", "medium", "high", "xhigh", "max"),
            "balanced": (current, "medium", "high", "low", "xhigh", "max"),
            "high": ("high", "xhigh", "max", "medium", "low"),
            "maximum": ("max", "xhigh", "high", "medium", "low"),
        }
        order = orders.get(intent, (current,))
        for preferred in order:
            if preferred and preferred in levels:
                return preferred
        return current

    def _policy_thinking_mode(self, model: Any, session: Any, reasoning_intent: str) -> str:
        """Resolve model preference + Type recommendation into effective ON/OFF."""
        del session  # Deep-thinking preference is per model in BYO, not per Crew session.
        return resolve_thinking_mode(
            model,
            getattr(model, "thinking_preference", "auto"),
            self._type_thinking_suggestion(reasoning_intent),
        )

    def _execute_post_stage_impl(
        self,
        session: Any,
        *,
        history_start: int,
        primary_provider: Any,
        primary_model: Any,
        post_stage: ReviewTarget | None,
        final_text: str,
        tool_calls_total: int,
        output_tokens: int,
        execution_guard_halted: bool,
        reasoning_profile: Any,
        routing_signals: Any,
        runtime_mode_context: str,
        on_reasoning: Any,
    ) -> _PostStageOutcome | None:
        """Run the optional bounded, tool-free verification/review phase.

        Keeping this phase outside the main provider/tool loop is intentional:
        Sky/Multi/Power share one normal agent loop and only the declared policy
        may append this detached, side-effect-free stage. Policies that require
        mandatory final review use the same generic seam.
        """
        if post_stage is None:
            return None
        if not final_text or session.cancel_event.is_set() or execution_guard_halted:
            self._agent_transition(
                session, post_stage.role,
                "cancelled" if session.cancel_event.is_set() else "skipped",
                provider_id=post_stage.provider.id, model_id=post_stage.model.model_id,
            )
            return None
        substantive_work = (
            tool_calls_total > 0
            or output_tokens >= 300
            or len(final_text.strip()) >= 1000
        )
        if post_stage.requires_substantive_work and not substantive_work:
            self._runtime_event(
                f"model.execution.{post_stage.pack}.{post_stage.policy}.{post_stage.role}.skipped",
                session,
                data={
                    "reason": "primary turn was not substantive enough for verification",
                    "tool_calls": tool_calls_total,
                    "output_tokens": output_tokens,
                    "answer_chars": len(final_text.strip()),
                },
            )
            append_lifecycle(
                session, "review/skipped",
                {"role": post_stage.role, "reason": "not_substantive"},
            )
            self._agent_transition(
                session, post_stage.role, "skipped",
                provider_id=post_stage.provider.id, model_id=post_stage.model.model_id,
            )
            return None

        review_provider = post_stage.provider
        review_model = post_stage.model
        transcript = self._review_transcript(session, history_start)
        if reasoning_profile is not None:
            review_thinking, review_effort = self._runtime_seams().reasoning_resolver.resolve(
                review_model, session, reasoning_profile, routing_signals,
                role=post_stage.role, stage="review",
            )
        else:
            review_effort = self._review_effort(review_model, session, post_stage.reasoning_intent)
            review_thinking = self._policy_thinking_mode(review_model, session, post_stage.reasoning_intent)

        review_messages: list[dict[str, Any]] = []
        if session.agent.prompt:
            review_messages.append({"role": "system", "content": session.agent.prompt})
        if runtime_mode_context:
            review_messages.append({"role": "system", "content": runtime_mode_context})
        language_instruction = str(
            getattr(session, "language_instruction", "") or ""
        )
        if language_instruction:
            review_messages.append({"role": "system", "content": language_instruction})
        review_messages.append({
            "role": "system",
            "content": (
                f"Kiro BYO detached {post_stage.role} pass. "
                "Check the draft against the turn transcript. Return only the final answer for the user. "
                + (
                    "For verification, preserve the draft unless a material correctness, omission, or reasoning issue needs fixing. "
                    if post_stage.role == "verify" else
                    "For final review, actively improve correctness, completeness, clarity, and reasoning where useful. "
                )
                + "Do not claim tools were used in this secondary stage and do not mention this stage. "
                + "Never add actions, file changes, command results, or facts that the turn transcript does not show; "
                "for long agentic work, keep the draft's account of what was actually done and only fix errors, gaps, or unclear conclusions."
            ),
        })
        if language_instruction:
            # Keep the final deliverable language close to the review task;
            # provider/model changes must not silently change its script.
            review_messages.append({"role": "system", "content": language_instruction})
        review_messages.append({
            "role": "user",
            "content": f"TURN TRANSCRIPT:\n{transcript}\n\nDRAFT ANSWER:\n{final_text}",
        })
        self._runtime_event(
            f"model.execution.{post_stage.pack}.{post_stage.policy}.{post_stage.role}.started",
            session,
            data={
                "primary_provider": primary_provider.id,
                "primary_model": primary_model.model_id,
                "review_provider": review_provider.id,
                "review_model": review_model.model_id,
                "review_effort": review_effort or "default",
                "review_thinking": review_thinking or "default",
                "reason": post_stage.reason,
            },
            content={"draft": final_text, "review_transcript": transcript},
        )
        append_lifecycle(
            session, "review/start",
            {
                "role": post_stage.role,
                "provider_id": review_provider.id,
                "model": review_model.model_id,
            },
        )
        self._agent_transition(
            session, post_stage.role, "running",
            provider_id=review_provider.id, model_id=review_model.model_id,
        )
        chunks: list[str] = []
        started = time.perf_counter()
        try:
            turn = chat_completion_turn(
                review_provider,
                review_model.model_id,
                review_messages,
                chunks.append,
                on_reasoning=on_reasoning,
                reasoning_effort=self._reasoning_wire_value(
                    review_model, review_thinking, review_effort
                ) or None,
                thinking_mode=review_thinking,
                model_spec=review_model,
                cache_key=f"{session.session_id}:{post_stage.pack}-{post_stage.policy}-{post_stage.role}",
                tools=[],
                should_interrupt=lambda: self._interrupt_requested(session),
            )
            elapsed = (time.perf_counter() - started) * 1000.0
            reviewed = (turn.text or "".join(chunks)).strip()
            if reviewed and not turn.tool_calls:
                with session.state_lock:
                    revise_last_assistant(
                        session, {"role": "assistant", "content": reviewed},
                        reason="post_stage_review",
                    )
                self._runtime_event(
                    f"model.execution.{post_stage.pack}.{post_stage.policy}.{post_stage.role}.completed",
                    session,
                    data={
                        "review_provider": review_provider.id,
                        "review_model": review_model.model_id,
                        "elapsed_ms": elapsed,
                        "changed": reviewed != final_text.strip(),
                    },
                    content={"final": reviewed},
                )
                append_lifecycle(
                    session, "review/end",
                    {"role": post_stage.role, "status": "applied", "changed": reviewed != final_text.strip()},
                )
                self._agent_transition(
                    session, post_stage.role, "completed",
                    provider_id=review_provider.id, model_id=turn.served_model or review_model.model_id,
                    summary=reviewed,
                )
                return _PostStageOutcome(
                    reviewed, review_provider.id, turn.served_model or review_model.model_id,
                    turn.usage, review_thinking, review_effort, elapsed, True,
                )
            self._runtime_event(
                f"model.execution.{post_stage.pack}.{post_stage.policy}.{post_stage.role}.skipped",
                session, data={"reason": "empty review or unexpected tool call"},
            )
            append_lifecycle(
                session, "review/end",
                {"role": post_stage.role, "status": "ignored"},
            )
            self._agent_transition(
                session, post_stage.role, "skipped",
                provider_id=review_provider.id, model_id=review_model.model_id,
            )
            return _PostStageOutcome(
                final_text, primary_provider.id, primary_model.model_id, turn.usage,
                review_thinking, review_effort, elapsed, False,
            )
        except (ProviderError, ProviderInterrupted) as exc:
            elapsed = (time.perf_counter() - started) * 1000.0
            self._runtime_event(
                f"model.execution.{post_stage.pack}.{post_stage.policy}.{post_stage.role}.failed",
                session,
                data={
                    "review_provider": review_provider.id,
                    "review_model": review_model.model_id,
                    "error": type(exc).__name__,
                },
            )
            append_lifecycle(
                session, "review/end",
                {"role": post_stage.role, "status": "failed", "error": type(exc).__name__},
            )
            self._agent_transition(
                session, post_stage.role, "failed",
                provider_id=review_provider.id, model_id=review_model.model_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return _PostStageOutcome(
                final_text, primary_provider.id, primary_model.model_id, None,
                review_thinking, review_effort, elapsed, False,
            )

    def _invoke_provider_round_impl(
        self,
        session: Any,
        *,
        step: int,
        round_role: str,
        round_stage: str,
        round_provider: Any,
        round_model: Any,
        round_effort: str,
        round_thinking: str,
        round_reasoning_signals: Any,
        tools: list[dict[str, Any]],
        runtime_mode_context: str,
        pending_replan_notice: str,
        on_text: Any,
        on_reasoning: Any,
    ) -> _ProviderRoundOutcome:
        """Execute exactly one model/provider round.

        The turn engine owns *when* another round is needed.  This helper owns
        only the provider-facing request construction, invocation and the
        provider-round lifecycle/diagnostic events.  It deliberately lets
        ProviderError/ProviderInterrupted escape so fallback, steering and
        cancellation remain turn-level decisions rather than transport policy.
        """
        with session.state_lock:
            projected_session = self._runtime_seams().session_projection.snapshot(session)
            projected_messages = list(projected_session.messages)
            provider_messages = [
                *([{"role": "system", "content": session.agent.prompt}] if session.agent.prompt else []),
                self._runtime_system_message(
                    round_provider,
                    round_model,
                    round_effort,
                    thinking_mode=round_thinking,
                    deferred_tool_search=bool(session.mcp is not None and session.mcp.servers),
                    mcp_server_names=tuple(
                        session.mcp.servers.keys() if session.mcp is not None else ()
                    ),
                    native_tool_names=tuple(
                        spec.name for spec in session.native.specs()
                    ) if session.native is not None else (),
                    mode_context=runtime_mode_context,
                    reasoning_language_context=str(
                        getattr(session, "language_instruction", "") or ""
                    ),
                ),
                *self._messages_for_provider(
                    projected_messages, round_provider, round_model
                ),
            ]
            if round_role == "worker":
                provider_messages.insert(
                    len(provider_messages) - len(projected_messages),
                    {
                        "role": "system",
                        "content": (
                            "Kiro BYO isolated worker: you are a read-only teammate of the lead model. "
                            "Continue ONLY the information gathering the lead started (reading, "
                            "searching, fetching) with the tools available to you. Do not plan "
                            "changes and do not answer the user. When you have gathered what the "
                            "lead needs, stop calling tools and reply with a concise factual "
                            "summary of your findings for the lead."
                        ),
                    },
                )
            if pending_replan_notice:
                provider_messages.insert(
                    len(provider_messages) - len(projected_messages),
                    {"role": "system", "content": pending_replan_notice},
                )

        append_lifecycle(
            session,
            "model/invoke",
            {
                "step": step,
                "role": round_role,
                "stage": round_stage,
                "provider_id": round_provider.id,
                "model": round_model.model_id,
                "thinking_mode": round_thinking or "default",
                "effort": round_effort or "default",
            },
        )
        self._runtime_event(
            "provider.round.started",
            session,
            data={
                "step": step,
                "role": round_role,
                "provider_id": round_provider.id,
                "model": round_model.model_id,
                "message_count": len(provider_messages),
                "tool_count": len(tools),
                "effort": round_effort or "default",
                "thinking_mode": round_thinking or "default",
                "reasoning_stage": round_stage,
                "reasoning_complexity": round(
                    float(getattr(round_reasoning_signals, "complexity", 0.0) or 0.0), 4
                ),
            },
            content={"messages": provider_messages, "tools": tools},
        )
        started = time.perf_counter()
        turn = chat_completion_turn(
            round_provider,
            round_model.model_id,
            provider_messages,
            on_text,
            on_reasoning=on_reasoning,
            reasoning_effort=self._reasoning_wire_value(
                round_model, round_thinking, round_effort
            ) or None,
            thinking_mode=round_thinking,
            model_spec=round_model,
            cache_key=(
                session.session_id
                if round_role == "lead"
                else f"{session.session_id}:relay"
            ),
            tools=tools,
            should_interrupt=lambda: self._interrupt_requested(session),
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        append_lifecycle(
            session,
            "model/result",
            {
                "step": step,
                "role": round_role,
                "provider_id": round_provider.id,
                "requested_model": round_model.model_id,
                "served_model": turn.served_model or round_model.model_id,
                "tool_calls": len(turn.tool_calls),
                "input_tokens": turn.usage.input_tokens,
                "output_tokens": turn.usage.output_tokens,
            },
        )
        self._runtime_event(
            "provider.round.completed",
            session,
            data={
                "step": step,
                "role": round_role,
                "provider_id": round_provider.id,
                "requested_model": round_model.model_id,
                "served_model": turn.served_model or round_model.model_id,
                "tool_calls": len(turn.tool_calls),
                "input_tokens": turn.usage.input_tokens,
                "output_tokens": turn.usage.output_tokens,
                "cached_tokens": turn.usage.cached_tokens,
                "reasoning_tokens": turn.usage.reasoning_tokens,
            },
            content={
                "text": turn.text,
                "tool_calls": [
                    {
                        "id": call.call_id,
                        "name": call.name,
                        "arguments": call.arguments_text,
                    }
                    for call in turn.tool_calls
                ],
                "provider_context": turn.provider_context,
            },
        )
        if round_model.context_window:
            self.emit_usage_update(
                session.session_id,
                used=turn.usage.input_tokens + turn.usage.output_tokens,
                size=round_model.context_window,
            )
        control = RoundOutcome(
            disposition=RoundDisposition.TOOLS if turn.tool_calls else RoundDisposition.FINAL,
            provider_id=str(round_provider.id),
            model_id=str(round_model.model_id),
            served_model_id=str(turn.served_model or round_model.model_id),
            text=str(turn.text or ""),
            tool_call_count=len(turn.tool_calls),
        )
        return _ProviderRoundOutcome(turn=turn, elapsed_ms=elapsed_ms, control=control)

    def _observe_tool_execution_guard(
        self,
        session: Any,
        execution_guard: ExecutionGuard,
        *,
        round_role: str,
        call: Any,
        arguments: Any,
        result_text: str,
        step: int,
        provider_rounds_total: int,
    ) -> tuple[Any, str, str]:
        """Observe one tool result and return decision + optional notices."""
        decision = execution_guard.observe(
            f"{round_role}:{getattr(call, 'name', '')}", arguments, result_text
        )
        pending_replan_notice = ""
        execution_guard_reason = ""
        if decision.adaptive_threshold_reached:
            self._runtime_event(
                "execution.guard.adaptive_threshold",
                session,
                data={
                    "tool_calls_seen": decision.tool_calls_seen,
                    "step": step,
                    "attempt": provider_rounds_total,
                    "role": round_role,
                },
            )
        if decision.action == "warn":
            pending_replan_notice = (
                "Execution guard: recent tool actions are repeating without new "
                "information. Re-evaluate the plan before using another tool. "
                "Do not repeat the same call/result cycle; either take a different "
                "productive action or conclude/hand back with what is already known."
            )
            self._runtime_event(
                "execution.guard.replan_requested",
                session,
                data={
                    "reason": decision.reason,
                    "tool_calls_seen": decision.tool_calls_seen,
                    "repeated_count": decision.repeated_count,
                    "cycle_length": decision.cycle_length,
                    "step": step,
                    "role": round_role,
                    "tool": getattr(call, "name", ""),
                },
            )
        elif decision.action == "halt":
            execution_guard_reason = decision.reason
            self._runtime_event(
                "execution.guard.halt_requested",
                session,
                data={
                    "reason": decision.reason,
                    "tool_calls_seen": decision.tool_calls_seen,
                    "repeated_count": decision.repeated_count,
                    "cycle_length": decision.cycle_length,
                    "step": step,
                    "role": round_role,
                    "tool": getattr(call, "name", ""),
                },
            )
        return decision, pending_replan_notice, execution_guard_reason

    def _execute_tool_batch_impl(
        self,
        session: Any,
        *,
        tool_calls: tuple[Any, ...] | list[Any],
        round_role: str,
        round_provider: Any,
        round_model: Any,
        step: int,
        execution_guard: ExecutionGuard,
        provider_rounds_total: int,
    ) -> _ToolBatchOutcome:
        """Execute one model-emitted tool batch without deciding turn control flow."""
        for call in tool_calls:
            effect_class = self._tool_effect_class(session, call.name)
            append_lifecycle(
                session,
                "tool/call",
                {
                    "call_id": call.call_id,
                    "name": call.name,
                    "role": round_role,
                    "provider_id": round_provider.id,
                    "model": round_model.model_id,
                    "arguments_chars": len(call.arguments_text or ""),
                    "effect_class": effect_class,
                },
            )
            self._runtime_event(
                "tool.effect.classified",
                session,
                data={
                    "call_id": call.call_id,
                    "tool": call.name,
                    "role": round_role,
                    "effect_class": effect_class,
                },
            )

        steered_during_tools = False
        guard_replan_requested = False
        guard_stop_requested = False
        guard_reason = ""
        pending_replan_notice = ""
        tool_ms = 0.0
        mcp_wait_ms = 0.0
        native_calls = 0
        tool_search_calls = 0
        tool_search_loaded = 0

        for call in tool_calls:
            if session.cancel_event.is_set():
                return _ToolBatchOutcome(
                    cancelled=True,
                    steered=steered_during_tools,
                    replan_requested=guard_replan_requested,
                    stop_requested=guard_stop_requested,
                    guard_reason=guard_reason,
                    pending_replan_notice=pending_replan_notice,
                    tool_ms=tool_ms,
                    mcp_wait_ms=mcp_wait_ms,
                    native_calls=native_calls,
                    tool_search_calls=tool_search_calls,
                    tool_search_loaded=tool_search_loaded,
                )

            if guard_stop_requested:
                # Deliberately NOT observed by the execution guard: the guard
                # already halted this batch, and feeding a fixed skip-text back
                # would produce a constant signature (name+args+sha256(text))
                # that could be misread as fresh stagnation. Skips are not model
                # progress and must stay out of the stagnation window.
                self._append_tool_result(
                    session,
                    call.call_id,
                    "Skipped because the execution guard stopped repeated/unbounded tool work.",
                )
                continue
            if guard_replan_requested:
                # Deliberately NOT observed (see guard_stop_requested above):
                # a re-plan was requested, the rest of the batch is skipped, and
                # a constant skip-text signature must not pollute the guard.
                self._append_tool_result(
                    session,
                    call.call_id,
                    "Skipped because the execution guard requested a re-plan before more tool work.",
                )
                continue

            if steered_during_tools or session.steer_event.is_set():
                steered_during_tools = True
                # Deliberately NOT observed: this is a USER steering event, not
                # model stagnation. Counting it would charge user control
                # against the guard's repetition window and could falsely halt.
                self._append_tool_result(
                    session,
                    call.call_id,
                    "Skipped before execution because user steering changed the task.",
                )
                continue

            arguments = call.arguments()
            if call.name == DEFERRED_TOOL_SEARCH_NAME:
                tool_search_calls += 1
                result_text, waited_ms, newly_loaded = self._deferred_tool_search_result(
                    session, arguments
                )
                mcp_wait_ms += waited_ms
                tool_search_loaded += newly_loaded
                self._append_tool_result(session, call.call_id, result_text)
                decision, notice, reason = self._observe_tool_execution_guard(
                    session,
                    execution_guard,
                    round_role=round_role,
                    call=call,
                    arguments=arguments,
                    result_text=result_text,
                    step=step,
                    provider_rounds_total=provider_rounds_total,
                )
                pending_replan_notice = notice or pending_replan_notice
                guard_reason = reason or guard_reason
                guard_replan_requested = guard_replan_requested or decision.action == "warn"
                guard_stop_requested = guard_stop_requested or decision.action == "halt"
                continue

            if session.native is not None and session.native.has_tool(call.name):
                native_calls += 1
                result_text, elapsed_ms, outcome = self._execute_native_tool(
                    session, call, arguments
                )
            else:
                result_text, elapsed_ms, outcome = self._execute_mcp_tool(
                    session, call, arguments
                )
            tool_ms += elapsed_ms
            if outcome == "cancelled":
                return _ToolBatchOutcome(
                    cancelled=True,
                    steered=steered_during_tools,
                    replan_requested=guard_replan_requested,
                    stop_requested=guard_stop_requested,
                    guard_reason=guard_reason,
                    pending_replan_notice=pending_replan_notice,
                    tool_ms=tool_ms,
                    mcp_wait_ms=mcp_wait_ms,
                    native_calls=native_calls,
                    tool_search_calls=tool_search_calls,
                    tool_search_loaded=tool_search_loaded,
                )
            self._append_tool_result(session, call.call_id, result_text)
            decision, notice, reason = self._observe_tool_execution_guard(
                session,
                execution_guard,
                round_role=round_role,
                call=call,
                arguments=arguments,
                result_text=result_text,
                step=step,
                provider_rounds_total=provider_rounds_total,
            )
            pending_replan_notice = notice or pending_replan_notice
            guard_reason = reason or guard_reason
            guard_replan_requested = guard_replan_requested or decision.action == "warn"
            guard_stop_requested = guard_stop_requested or decision.action == "halt"
            if outcome == "steered":
                steered_during_tools = True

        return _ToolBatchOutcome(
            cancelled=False,
            steered=steered_during_tools,
            replan_requested=guard_replan_requested,
            stop_requested=guard_stop_requested,
            guard_reason=guard_reason,
            pending_replan_notice=pending_replan_notice,
            tool_ms=tool_ms,
            mcp_wait_ms=mcp_wait_ms,
            native_calls=native_calls,
            tool_search_calls=tool_search_calls,
            tool_search_loaded=tool_search_loaded,
        )

    def _handle_provider_interruption(
        self,
        session: Any,
        *,
        history_start: int,
        interruption: ProviderInterrupted,
        preserve_side_effects: bool = False,
    ) -> str:
        """Resolve provider interruption ownership at the turn boundary.

        Returns one of ``cancelled``, ``continue`` (a queued steer was consumed)
        or ``interrupted`` (unclaimed interruption; preserve partial text and end).
        """
        if session.cancel_event.is_set():
            self._recover_current_turn(
                session, history_start, reason="provider_cancelled",
                preserve_side_effects=preserve_side_effects,
                partial_text=interruption.partial_text,
            )
            return "cancelled"
        if self._consume_queued_steers_for_turn(session, interruption.partial_text):
            return "continue"
        if interruption.partial_text:
            with session.state_lock:
                append_message(
                    session,
                    {"role": "assistant", "content": interruption.partial_text},
                )
        self._persist_session(session)
        return "interrupted"

    def _advance_turn_phase(
        self,
        session: Any,
        state: TurnState,
        target: TurnPhase,
        *,
        reason: str,
    ) -> None:
        """Advance the live provider-neutral Turn Engine lifecycle."""
        if state.phase is target:
            return
        state.transition(target, reason=reason)
        transition = state.transitions[-1]
        self._runtime_event(
            "turn.phase",
            session,
            data={
                "from": transition.source.value,
                "to": transition.target.value,
                "reason": transition.reason,
                "round_index": state.round_index,
                "provider_rounds": state.provider_rounds,
                "tool_batches": state.tool_batches,
            },
        )

    def _build_provider_loop_state(
        self,
        session: Any,
        provider: Any,
        model: Any,
        *,
        mode_fallbacks: list[tuple[Any, Any]] | None,
        primary_effort: str,
        primary_thinking: str,
        relay: Any,
        reasoning_profile: Any,
        routing_signals: Any,
    ) -> _ProviderLoopState:
        """Create the single mutable state object for one provider/tool loop."""
        if reasoning_profile is not None:
            turn_thinking, turn_effort = self._runtime_seams().reasoning_resolver.resolve(
                model, session, reasoning_profile, routing_signals,
                role="lead", stage="planning",
            )
        else:
            # Compatibility for direct/internal callers that pre-resolved the
            # legacy coarse intent.
            turn_thinking = primary_thinking
            turn_effort = "" if turn_thinking == "off" else (
                session.effort if getattr(session, "effort_user_set", False)
                else (primary_effort or session.effort)
            )
        state = _ProviderLoopState(
            provider=provider,
            model=model,
            mode_fallbacks=list(mode_fallbacks or []),
            relay=relay,
            turn_effort=turn_effort,
            turn_thinking=turn_thinking,
            turn_run_id=str(getattr(session, "active_run_id", "") or ""),
            turn_trace_id=str(getattr(session, "active_turn_id", "") or ""),
            served_model_id=model.model_id,
            served_provider_id=provider.id,
        )
        self._advance_turn_phase(session, state.turn_state, TurnPhase.ROUTE, reason="turn_ready")
        return state

    @staticmethod
    def _reasoning_summary(values: set[str]) -> str:
        if not values:
            return "default"
        if len(values) == 1:
            return next(iter(values))
        return "mixed"

    @staticmethod
    def _provider_prompt_result(state: _ProviderLoopState, stop_reason: str) -> dict[str, Any]:
        # Crew 0.9 reads these exact PromptResponse fields as per-turn
        # billing/token usage. Cache read/write are separate dimensions.
        return {
            "stopReason": stop_reason,
            "inputTokens": int(state.input_tokens),
            "outputTokens": int(state.output_tokens),
            "cachedReadTokens": int(state.cached_tokens),
            "cachedWriteTokens": int(state.cache_write_tokens),
            "reasoningTokens": int(state.reasoning_tokens),
        }

    @staticmethod
    def _mark_provider_visible(state: _ProviderLoopState) -> None:
        now = time.perf_counter()
        if state.first_visible_at is None:
            state.first_visible_at = now
        if state.previous_visible_at is not None:
            state.max_gap_ms = max(
                state.max_gap_ms, (now - state.previous_visible_at) * 1000.0
            )
        state.previous_visible_at = now
        state.last_visible_at = now

    def _emit_provider_text(
        self, session: Any, state: _ProviderLoopState, chunk: str
    ) -> None:
        if not chunk:
            return
        # Provider content is not automatically "thinking". A tool-calling
        # round may later prove to be an internal draft, while a no-tool round is
        # the final answer, so buffer it until the round outcome is known.
        if state.turn_state.phase is TurnPhase.INVOKE:
            self._advance_turn_phase(
                session, state.turn_state, TurnPhase.STREAM,
                reason="provider_text_stream",
            )
        state.stream_chunks += 1
        state.stream_chars += len(chunk)
        state.max_chunk_chars = max(state.max_chunk_chars, len(chunk))
        state.round_text_parts.append(chunk)
        self._runtime_event(
            "provider.stream.delta",
            session,
            data={"step": state.step, "chars": len(chunk)},
            content={"text": chunk},
        )

    def _emit_provider_reasoning(
        self,
        session_id: str,
        session: Any,
        state: _ProviderLoopState,
        chunk: str,
    ) -> None:
        if not chunk:
            return
        if state.turn_state.phase is TurnPhase.INVOKE:
            self._advance_turn_phase(
                session, state.turn_state, TurnPhase.STREAM,
                reason="provider_reasoning_stream",
            )
        state.round_reasoning_emitted = True
        self._mark_provider_visible(state)
        self.emit_text(session_id, chunk, is_thinking=True)
        self._runtime_event(
            "provider.stream.reasoning_delta",
            session,
            data={"step": state.step, "chars": len(chunk)},
            content={"text": chunk},
        )

    def _record_provider_turn_telemetry(
        self,
        session: Any,
        state: _ProviderLoopState,
        *,
        outcome: str,
        t_prompt: float,
        routing_decision: Any = None,
        error_kind: str = "",
        error_category: str = "",
    ) -> None:
        """Record exactly one terminal telemetry/lifecycle projection."""
        if state.telemetry_recorded:
            return
        state.telemetry_recorded = True
        terminal_event = (
            "turn/failed" if outcome == "error"
            else "turn/cancelled" if outcome == "cancelled"
            else "turn/interrupted" if outcome == "interrupted"
            else ""
        )
        if terminal_event:
            append_lifecycle(
                session, terminal_event,
                {
                    "outcome": outcome,
                    "error_kind": error_kind,
                    "error_category": error_category,
                    "cancel_source": str(getattr(session, "cancel_source", "") or ""),
                },
            )
            self._runtime_event(
                terminal_event.replace("/", "."), session,
                data={
                    "outcome": outcome,
                    "error_kind": error_kind,
                    "error_category": error_category,
                    "cancel_source": str(getattr(session, "cancel_source", "") or ""),
                    "provider_id": state.served_provider_id,
                    "model": state.served_model_id,
                },
            )
        self._finalize_agent_plan(
            session, outcome,
            provider_id=state.served_provider_id,
            model_id=state.served_model_id,
        )
        append_lifecycle(
            session,
            "turn/end",
            {
                "outcome": outcome,
                "error_kind": error_kind,
                "error_category": error_category,
                "provider_id": state.served_provider_id,
                "model": state.served_model_id,
                "tool_calls": state.tool_calls_total,
                "provider_rounds": state.provider_rounds_total,
            },
        )
        self._finish_durable_run(
            session,
            outcome=outcome,
            error_kind=error_kind,
            error_category=error_category,
            provider_id=state.served_provider_id,
            model_id=state.served_model_id,
            side_effect_risk=state.side_effect_risk_seen,
        )
        now = time.perf_counter()
        first_ms = (
            (state.first_visible_at - t_prompt) * 1000.0
            if state.first_visible_at is not None
            else -1.0
        )
        try:
            selected_route = None
            if routing_decision is not None:
                selected_route = next((
                    row for row in getattr(routing_decision, "candidates", ())
                    if row.provider.id == state.served_provider_id
                    and row.model.model_id == state.served_model_id
                ), None)
            completed_at = now_timestamp()
            total_ms = (now - t_prompt) * 1000.0
            served_spec = (
                getattr(selected_route, "model", None)
                if selected_route is not None else state.model
            )
            self.telemetry.record(
                TurnTelemetry(
                    timestamp=completed_at,
                    session_id=session.session_id,
                    outcome=outcome,
                    requested_model=session.model,
                    provider_id=state.served_provider_id,
                    served_model=state.served_model_id,
                    effort=self._reasoning_summary(state.observed_efforts),
                    thinking_mode=self._reasoning_summary(state.observed_thinking_modes),
                    total_ms=total_ms,
                    first_visible_ms=first_ms,
                    upstream_ms=state.upstream_ms,
                    tool_ms=state.tool_ms,
                    mcp_wait_ms=state.mcp_wait_ms,
                    tool_calls=state.tool_calls_total,
                    native_calls=state.native_tool_calls_total,
                    tool_search_calls=state.tool_search_calls_total,
                    active_tools=state.active_tools_count,
                    tool_schema_bytes=state.tool_schema_bytes,
                    stream_chunks=state.stream_chunks,
                    stream_chars=state.stream_chars,
                    input_tokens=state.input_tokens,
                    output_tokens=state.output_tokens,
                    cached_tokens=state.cached_tokens,
                    cache_write_tokens=state.cache_write_tokens,
                    reasoning_tokens=state.reasoning_tokens,
                    cache_usage_reported=state.cache_usage_reported,
                    cache_write_usage_reported=state.cache_write_usage_reported,
                    reasoning_usage_reported=state.reasoning_usage_reported,
                    error_kind=error_kind,
                    error_category=error_category,
                    transport_retries=state.transport_retries_total,
                    fallbacks=state.fallbacks_total,
                    cancel_source=str(getattr(session, "cancel_source", "") or ""),
                    run_id=state.turn_run_id,
                    turn_id=state.turn_trace_id,
                    mode_pack=(
                        str(getattr(routing_decision, "pack", ""))
                        if routing_decision is not None else ""
                    ),
                    mode_type=(
                        str(getattr(routing_decision, "policy", ""))
                        if routing_decision is not None else "concrete"
                    ) or "concrete",
                    task_profile=(
                        str(getattr(getattr(routing_decision, "signals", None), "task_profile", ""))
                        if routing_decision is not None else "concrete"
                    ) or "concrete",
                    routing_score=(
                        float(selected_route.score) if selected_route is not None else 0.0
                    ),
                    routing_confidence=(
                        selected_route.runtime_confidence if selected_route is not None else ""
                    ),
                    started_at=max(0.0, completed_at - (total_ms / 1000.0)),
                    completed_at=completed_at,
                    selection_kind=str(getattr(session, "selection_kind", "") or ""),
                    selection_source=str(getattr(session, "selection_source", "") or ""),
                    selection_requested=str(getattr(session, "selection_requested", "") or ""),
                    thinking_preference=str(getattr(served_spec, "thinking_preference", "") or ""),
                    route_signals=(
                        {
                            key: round(max(0.0, min(1.0, float(getattr(routing_decision.signals, key, 0.0)))), 4)
                            for key in (
                                "complexity", "breadth", "parallelizability", "risk",
                                "tool_intensity", "write_intensity", "context_demand",
                                "verification_value", "latency_sensitivity",
                            )
                            if isinstance(getattr(routing_decision.signals, key, None), (int, float))
                        }
                        if routing_decision is not None else None
                    ),
                )
            )
        except OSError as exc:
            self.trace(f"telemetry write skipped: {type(exc).__name__}")
        self._runtime_event(
            "turn.completed",
            session,
            turn_id=state.turn_trace_id,
            run_id=state.turn_run_id,
            data={
                "outcome": outcome,
                "error_kind": error_kind,
                "error_category": error_category,
                "provider_id": state.served_provider_id,
                "requested_model": session.model,
                "served_model": state.served_model_id,
                "tool_calls": state.tool_calls_total,
                "provider_rounds": state.provider_rounds_total,
                "execution_guard_tool_calls": state.execution_guard.tool_calls_seen,
                "execution_guard_halted": state.execution_guard_halted,
                "execution_guard_reason": state.execution_guard_reason,
                "total_ms": (now - t_prompt) * 1000.0,
            },
        )
        try:
            self._persist_session(session)
        except OSError as exc:
            self.trace(f"agent/turn final persist skipped: {type(exc).__name__}")

    def _run_planned_subagents_for_round(
        self,
        session: Any,
        state: _ProviderLoopState,
        *,
        execution_plan: ExecutionPlan | None,
        routing_config: Any,
        routing_decision: Any,
        reasoning_profile: Any,
        routing_signals: Any,
        runtime_mode_context: str,
    ) -> bool:
        """Run isolated planned children and hand the same logical step to lead."""
        use_worker = state.relay is not None and state.relay_next_role == "worker"
        if not (
            use_worker and execution_plan is not None and execution_plan.workers
            and routing_config is not None and routing_decision is not None
        ):
            return False
        round_reasoning_signals = self._relay_subtask_signals(session, routing_signals)
        completed_children = self._run_parallel_subagents(
            session, execution_plan,
            routing_config=routing_config,
            routing_decision=routing_decision,
            leader_provider=state.provider,
            leader_model=state.model,
            reasoning_profile=reasoning_profile,
            reasoning_signals=round_reasoning_signals,
            runtime_mode_context=runtime_mode_context,
        )
        for _task, child_result in completed_children:
            child_usage = dict(child_result.usage or {})
            state.input_tokens += int(child_usage.get("input_tokens", 0) or 0)
            state.output_tokens += int(child_usage.get("output_tokens", 0) or 0)
            state.cached_tokens += int(child_usage.get("cached_tokens", 0) or 0)
            state.reasoning_tokens += int(child_usage.get("reasoning_tokens", 0) or 0)
            state.provider_rounds_total += int(child_usage.get("provider_rounds", 0) or 0)
            state.tool_calls_total += int(child_usage.get("tool_calls", 0) or 0)
        state.relay_rounds += len(completed_children)
        state.relay = None
        state.relay_next_role = "lead"
        self._advance_turn_phase(
            session, state.turn_state, TurnPhase.CONTINUE,
            reason="subagents_handback",
        )
        return True

    def _prepare_provider_round(
        self,
        session: Any,
        state: _ProviderLoopState,
        *,
        reasoning_profile: Any,
        routing_signals: Any,
        routing_config: Any,
        routing_decision: Any,
    ) -> _ProviderRoundContext:
        """Resolve one lead/worker invocation and its visible tool catalogue."""
        use_worker = state.relay is not None and state.relay_next_role == "worker"
        legacy_round_effort = state.relay[2] if state.relay is not None else ""
        round_reasoning_signals = routing_signals
        if use_worker:
            round_reasoning_signals = self._relay_subtask_signals(session, routing_signals)
            if routing_config is not None and routing_decision is not None:
                teammate = self._relay_teammate(
                    routing_decision, state.provider, state.model,
                    config=routing_config, signals=round_reasoning_signals,
                )
                if teammate is not None:
                    round_provider, round_model = teammate
                    state.relay = (round_provider, round_model, legacy_round_effort)
                else:
                    use_worker = False
            else:
                round_provider, round_model, legacy_round_effort = state.relay

        if use_worker:
            round_role = "worker"
            state.relay_rounds += 1
            round_stage = "gathering"
            if reasoning_profile is not None:
                round_thinking, round_effort = self._runtime_seams().reasoning_resolver.resolve(
                    round_model, session, reasoning_profile, round_reasoning_signals,
                    role="worker", stage=round_stage,
                )
            else:
                round_effort = legacy_round_effort
                round_thinking = self._policy_thinking_mode(round_model, session, "economy")
        else:
            round_role = "lead"
            round_provider, round_model = state.provider, state.model
            round_stage = (
                "planning" if state.step == 1
                else "synthesis" if state.relay_rounds > 0
                else "analysis"
            )
            if reasoning_profile is not None:
                round_thinking, round_effort = self._runtime_seams().reasoning_resolver.resolve(
                    round_model, session, reasoning_profile, routing_signals,
                    role="lead", stage=round_stage,
                )
            else:
                round_effort = state.turn_effort
                round_thinking = state.turn_thinking

        state.observed_thinking_modes.add(round_thinking or "default")
        state.observed_efforts.add(round_effort or "default")
        self._agent_transition(
            session, round_role, "running",
            provider_id=round_provider.id, model_id=round_model.model_id,
        )
        tools = self._active_tools_for_turn(session)
        if round_role == "worker":
            tools = [
                tool for tool in tools
                if self._relay_gathering_tool(
                    session, str(((tool or {}).get("function") or {}).get("name") or "")
                )
            ]
        state.active_tools_count = len(tools)
        state.tool_schema_bytes = (
            len(json.dumps(tools, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            if tools else 0
        )
        state.turn_state.note_provider_round()
        self._advance_turn_phase(
            session, state.turn_state, TurnPhase.INVOKE,
            reason=f"round_{state.step}:{round_role}",
        )
        state.provider_rounds_total = state.turn_state.provider_rounds
        replan_notice = state.pending_replan_notice
        state.pending_replan_notice = ""
        return _ProviderRoundContext(
            provider=round_provider,
            model=round_model,
            role=round_role,
            stage=round_stage,
            effort=round_effort,
            thinking=round_thinking,
            reasoning_signals=round_reasoning_signals,
            tools=tools,
            replan_notice=replan_notice,
        )

    def _invoke_provider_loop_round(
        self,
        req_id: Any,
        session_id: str,
        session: Any,
        state: _ProviderLoopState,
        round_ctx: _ProviderRoundContext,
        *,
        history_start: int,
        t_prompt: float,
        routing_decision: Any,
        runtime_mode_context: str,
    ) -> tuple[_LoopDirective, _ProviderRoundOutcome | None]:
        """Invoke one provider round, including retry/fallback control edges."""
        t_upstream = time.perf_counter()
        try:
            outcome = self._runtime_seams().provider_invoker.invoke(
                session,
                step=state.step,
                round_role=round_ctx.role,
                round_stage=round_ctx.stage,
                round_provider=round_ctx.provider,
                round_model=round_ctx.model,
                round_effort=round_ctx.effort,
                round_thinking=round_ctx.thinking,
                round_reasoning_signals=round_ctx.reasoning_signals,
                tools=round_ctx.tools,
                runtime_mode_context=runtime_mode_context,
                pending_replan_notice=round_ctx.replan_notice,
                on_text=lambda chunk: self._emit_provider_text(session, state, chunk),
                on_reasoning=lambda chunk: self._emit_provider_reasoning(
                    session_id, session, state, chunk
                ),
            )
        except ProviderInterrupted as exc:
            state.upstream_ms += (time.perf_counter() - t_upstream) * 1000.0
            interruption_action = self._handle_provider_interruption(
                session,
                history_start=history_start,
                interruption=exc,
                preserve_side_effects=state.side_effect_risk_seen,
            )
            if interruption_action == "cancelled":
                self._advance_turn_phase(
                    session, state.turn_state, TurnPhase.COMPLETE,
                    reason="provider_cancelled",
                )
                self._record_provider_turn_telemetry(
                    session, state, outcome="cancelled", t_prompt=t_prompt,
                    routing_decision=routing_decision, error_category="control",
                )
                self.result(req_id, self._provider_prompt_result(state, "cancelled"))
                return _LoopDirective.RETURN, None
            if interruption_action == "continue":
                self._advance_turn_phase(
                    session, state.turn_state, TurnPhase.CONTINUE,
                    reason="steer_after_interrupt",
                )
                return _LoopDirective.STEER_AFTER_INTERRUPT, None
            self._advance_turn_phase(
                session, state.turn_state, TurnPhase.COMPLETE,
                reason="provider_interrupted",
            )
            self._record_provider_turn_telemetry(
                session, state, outcome="interrupted", t_prompt=t_prompt,
                routing_decision=routing_decision, error_category="control",
            )
            self.result(req_id, self._provider_prompt_result(state, "end_turn"))
            return _LoopDirective.RETURN, None
        except ProviderError as exc:
            state.upstream_ms += (time.perf_counter() - t_upstream) * 1000.0
            error_category = str(getattr(exc, "category", "provider") or "provider")
            pristine = (
                state.step == 1
                and not state.round_text_parts
                and not state.round_reasoning_emitted
                and state.tool_calls_total == 0
            )
            self._runtime_event(
                "provider.round.failed", session,
                data={
                    "step": state.step,
                    "role": round_ctx.role,
                    "provider_id": round_ctx.provider.id,
                    "model": round_ctx.model.model_id,
                    "error": type(exc).__name__,
                    "message": str(exc),
                    "error_category": error_category,
                    "retryable": bool(getattr(exc, "retryable", False)),
                    "pristine": pristine,
                },
            )
            if round_ctx.role == "worker":
                self._agent_transition(
                    session, "worker", "failed",
                    provider_id=round_ctx.provider.id,
                    model_id=round_ctx.model.model_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                self._runtime_event(
                    "model.relay.worker_failed", session,
                    data={
                        "provider_id": round_ctx.provider.id,
                        "model": round_ctx.model.model_id,
                        "error": type(exc).__name__,
                        "error_category": error_category,
                    },
                )
                state.relay = None
                state.relay_next_role = "lead"
                self._advance_turn_phase(
                    session, state.turn_state, TurnPhase.CONTINUE,
                    reason="worker_failed_handback",
                )
                return _LoopDirective.WORKER_FAILED_HAND_BACK, None

            recovery = RecoveryPolicy.live_provider_failure(
                exc,
                pristine=pristine,
                side_effect_risk=state.side_effect_risk_seen,
                same_model_retries=state.transport_retries_total,
                has_fallback=bool(state.mode_fallbacks),
                can_replan=False,
            )
            self._runtime_event(
                "recovery.decision", session,
                data={
                    "action": recovery.action,
                    "reason": recovery.reason,
                    "safe": recovery.safe,
                    "step": state.step,
                    "provider_id": round_ctx.provider.id,
                    "model": round_ctx.model.model_id,
                },
            )
            if recovery.action == "retry_same_model":
                state.transport_retries_total += 1
                append_lifecycle(
                    session, "model/retry",
                    {
                        "provider_id": round_ctx.provider.id,
                        "model": round_ctx.model.model_id,
                        "error": type(exc).__name__,
                        "attempt": state.transport_retries_total,
                        "policy_reason": recovery.reason,
                    },
                )
                self._runtime_event(
                    "provider.retry", session,
                    data={
                        "provider_id": round_ctx.provider.id,
                        "model": round_ctx.model.model_id,
                        "error": type(exc).__name__,
                        "attempt": state.transport_retries_total,
                        "policy_reason": recovery.reason,
                    },
                )
                self._advance_turn_phase(
                    session, state.turn_state, TurnPhase.ROUTE,
                    reason="policy_same_model_retry",
                )
                return _LoopDirective.RETRY_SAME_MODEL, None
            if recovery.action == "fallback" and state.mode_fallbacks and pristine:
                failed_provider, failed_model = state.provider, state.model
                state.provider, state.model = state.mode_fallbacks.pop(0)
                state.fallbacks_total += 1
                state.served_provider_id = state.provider.id
                state.served_model_id = state.model.model_id
                append_lifecycle(
                    session,
                    "model/fallback",
                    {
                        "from_provider": failed_provider.id,
                        "from_model": failed_model.model_id,
                        "to_provider": state.provider.id,
                        "to_model": state.model.model_id,
                        "error": type(exc).__name__,
                        "error_category": error_category,
                    },
                )
                self._runtime_event(
                    "model.route.fallback", session,
                    data={
                        "from_provider": failed_provider.id,
                        "from_model": failed_model.model_id,
                        "to_provider": state.provider.id,
                        "to_model": state.model.model_id,
                        "error": type(exc).__name__,
                        "error_category": error_category,
                    },
                )
                self._advance_turn_phase(
                    session, state.turn_state, TurnPhase.ROUTE,
                    reason="pristine_fallback",
                )
                return _LoopDirective.PRISTINE_FALLBACK, None
            raise
        state.upstream_ms += outcome.elapsed_ms
        return _LoopDirective.NONE, outcome

    @staticmethod
    def _apply_provider_round_usage(
        state: _ProviderLoopState, turn: Any, *, lead: bool
    ) -> None:
        if lead:
            state.served_model_id = turn.served_model or state.served_model_id
        state.input_tokens += turn.usage.input_tokens
        state.output_tokens += turn.usage.output_tokens
        state.cached_tokens += turn.usage.cached_tokens
        state.cache_write_tokens += turn.usage.cache_write_tokens
        state.reasoning_tokens += turn.usage.reasoning_tokens
        state.cache_usage_reported = state.cache_usage_reported or turn.usage.cached_tokens_reported
        state.cache_write_usage_reported = (
            state.cache_write_usage_reported or turn.usage.cache_write_tokens_reported
        )
        state.reasoning_usage_reported = (
            state.reasoning_usage_reported or turn.usage.reasoning_tokens_reported
        )

    def _handle_no_tool_round(
        self,
        session: Any,
        state: _ProviderLoopState,
        round_ctx: _ProviderRoundContext,
        turn: Any,
    ) -> _LoopDirective:
        if round_ctx.role == "worker":
            worker_summary = (turn.text or "".join(state.round_text_parts)).strip()
            self._agent_transition(
                session, "worker", "completed",
                provider_id=round_ctx.provider.id,
                model_id=round_ctx.model.model_id,
                summary=worker_summary,
            )
            with session.state_lock:
                append_message(
                    session,
                    {"role": "assistant", "content": "[Relay worker findings]\n" + worker_summary},
                )
            state.relay_next_role = "lead"
            self._runtime_event(
                "model.relay.handback", session,
                data={
                    "step": state.step,
                    "worker_model": round_ctx.model.model_id,
                    "relay_rounds": state.relay_rounds,
                },
            )
            self._advance_turn_phase(
                session, state.turn_state, TurnPhase.CONTINUE,
                reason="worker_findings_handback",
            )
            return _LoopDirective.WORKER_FINDINGS_HAND_BACK

        with session.state_lock:
            assistant_message: dict[str, Any] = {"role": "assistant", "content": turn.text}
            if turn.provider_context:
                assistant_message["_kiro_provider_context"] = self._scoped_provider_context(
                    turn.provider_context, round_ctx.provider, round_ctx.model
                )
            append_message(session, assistant_message)
        if self._consume_queued_steers_for_turn(session):
            self._advance_turn_phase(
                session, state.turn_state, TurnPhase.CONTINUE,
                reason="queued_steer_after_final_candidate",
            )
            return _LoopDirective.QUEUED_STEER_AFTER_FINAL
        state.final_text = turn.text or "".join(state.round_text_parts)
        return _LoopDirective.FINAL

    def _handle_tool_round(
        self,
        req_id: Any,
        session: Any,
        state: _ProviderLoopState,
        round_ctx: _ProviderRoundContext,
        turn: Any,
        *,
        history_start: int,
        t_prompt: float,
        routing_decision: Any,
    ) -> _LoopDirective:
        """Admit/execute one tool batch and return the historical loop edge."""
        self._advance_turn_phase(
            session, state.turn_state, TurnPhase.TOOL,
            reason="model_requested_tools",
        )
        state.turn_state.note_tool_batch()
        state.tool_calls_total += len(turn.tool_calls)
        assistant_tool_calls: list[dict[str, Any]] = []
        for call in turn.tool_calls:
            assistant_tool_calls.append({
                "id": call.call_id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments_text},
            })
        with session.state_lock:
            assistant_message = {
                "role": "assistant",
                "content": turn.text or None,
                "tool_calls": assistant_tool_calls,
            }
            if turn.provider_context and round_ctx.role == "lead":
                assistant_message["_kiro_provider_context"] = self._scoped_provider_context(
                    turn.provider_context, round_ctx.provider, round_ctx.model
                )
            append_message(session, assistant_message)

        batch_effects = {
            self._tool_effect_class(session, call.name) for call in turn.tool_calls
        }
        state.side_effect_classes_seen.update(batch_effects)
        if any(effect not in _SAFE_TOOL_EFFECTS for effect in batch_effects):
            state.side_effect_risk_seen = True
        try:
            self._checkpoint_durable_run(
                session, "tool_batch_admitted",
                data={
                    "step": state.step,
                    "tool_calls_total": state.tool_calls_total,
                    "effects": sorted(batch_effects),
                    "side_effect_risk": state.side_effect_risk_seen,
                },
                critical=True,
            )
        except DurableCheckpointError:
            # P0 safety invariant: no mutating batch executes unless its risk
            # boundary is durably committed first.
            self.emit_runtime_error(
                session.session_id,
                "durable checkpoint failed before tool execution; "
                "stopping to avoid an unrecoverable side effect",
            )
            self._recover_current_turn(
                session, history_start,
                reason="checkpoint_failed_pre_tools",
                preserve_side_effects=state.side_effect_risk_seen,
            )
            self._advance_turn_phase(
                session, state.turn_state, TurnPhase.COMPLETE,
                reason="checkpoint_failed_pre_tools",
            )
            self._record_provider_turn_telemetry(
                session, state, outcome="error", t_prompt=t_prompt,
                routing_decision=routing_decision,
                error_category="durable_checkpoint",
            )
            self.result(req_id, self._provider_prompt_result(state, "error"))
            return _LoopDirective.RETURN

        batch_outcome = self._runtime_seams().tool_executor.execute(
            session,
            tool_calls=turn.tool_calls,
            round_role=round_ctx.role,
            round_provider=round_ctx.provider,
            round_model=round_ctx.model,
            step=state.step,
            execution_guard=state.execution_guard,
            provider_rounds_total=state.provider_rounds_total,
        )
        state.tool_ms += batch_outcome.tool_ms
        state.mcp_wait_ms += batch_outcome.mcp_wait_ms
        state.native_tool_calls_total += batch_outcome.native_calls
        state.tool_search_calls_total += batch_outcome.tool_search_calls
        state.tool_search_loaded_total += batch_outcome.tool_search_loaded
        if batch_outcome.pending_replan_notice:
            state.pending_replan_notice = batch_outcome.pending_replan_notice
        if batch_outcome.guard_reason:
            state.execution_guard_reason = batch_outcome.guard_reason
        self._checkpoint_durable_run(
            session, "tool_batch_completed",
            data={
                "step": state.step,
                "tool_calls_total": state.tool_calls_total,
                "effects": sorted(batch_effects),
                "side_effect_risk": state.side_effect_risk_seen,
            },
        )
        if batch_outcome.cancelled:
            self._recover_current_turn(
                session, history_start,
                reason="cancelled_during_tools",
                preserve_side_effects=state.side_effect_risk_seen,
            )
            self._advance_turn_phase(
                session, state.turn_state, TurnPhase.COMPLETE,
                reason="cancelled_during_tools",
            )
            self._record_provider_turn_telemetry(
                session, state, outcome="cancelled", t_prompt=t_prompt,
                routing_decision=routing_decision, error_category="control",
            )
            self.result(req_id, self._provider_prompt_result(state, "cancelled"))
            return _LoopDirective.RETURN

        if batch_outcome.stop_requested:
            if (
                round_ctx.role == "worker"
                and state.execution_guard_reason != "emergency_tool_call_limit"
            ):
                self._runtime_event(
                    "model.relay.guard_handback",
                    session,
                    data={
                        "reason": state.execution_guard_reason,
                        "worker_model": round_ctx.model.model_id,
                        "step": state.step,
                    },
                )
                state.relay = None
                state.relay_next_role = "lead"
                state.pending_replan_notice = (
                    "The read-only worker was stopped because its tool work became repetitive. "
                    "Re-plan from the lead role using the results already gathered; do not repeat "
                    "the same tool/action cycle."
                )
                state.execution_guard.reset_stagnation_window()
                state.execution_guard_reason = ""
                self._advance_turn_phase(
                    session, state.turn_state, TurnPhase.CONTINUE,
                    reason="worker_guard_handback",
                )
                return _LoopDirective.WORKER_GUARD_HAND_BACK

            state.execution_guard_halted = True
            if state.execution_guard_reason == "emergency_tool_call_limit":
                state.final_text = (
                    "本轮已触发异常执行保护：工具调用量达到灾难级安全上限。"
                    "已停止继续调用工具，并保留当前已经完成的工作记录。"
                )
            else:
                state.final_text = (
                    "检测到工具调用在重新规划后仍持续重复、没有产生新进展，"
                    "本轮已停止继续调用工具。当前已完成的工作记录已保留。"
                )
            with session.state_lock:
                append_message(session, {"role": "assistant", "content": state.final_text})
            self._runtime_event(
                "execution.guard.stopped",
                session,
                data={
                    "reason": state.execution_guard_reason,
                    "tool_calls_seen": state.execution_guard.tool_calls_seen,
                    "step": state.step,
                    "role": round_ctx.role,
                },
            )
            self._advance_turn_phase(
                session, state.turn_state, TurnPhase.CONTINUE,
                reason="execution_guard_stopped",
            )
            return _LoopDirective.STOP

        if batch_outcome.replan_requested:
            self._advance_turn_phase(
                session, state.turn_state, TurnPhase.CONTINUE,
                reason="execution_guard_replan",
            )
            return _LoopDirective.EXECUTION_GUARD_REPLAN

        if self._consume_queued_steers_for_turn(session):
            state.relay_next_role = "lead"
            self._advance_turn_phase(
                session, state.turn_state, TurnPhase.CONTINUE,
                reason="steer_after_tools",
            )
            return _LoopDirective.STEER_AFTER_TOOLS
        if state.relay is not None:
            gathering_only = all(
                self._relay_gathering_tool(session, call.name) for call in turn.tool_calls
            )
            state.relay_next_role = "worker" if gathering_only else "lead"
        self._advance_turn_phase(
            session, state.turn_state, TurnPhase.CONTINUE,
            reason="tool_results_ready",
        )
        return _LoopDirective.NONE

    def _run_provider_post_stage(
        self,
        req_id: Any,
        session_id: str,
        session: Any,
        state: _ProviderLoopState,
        *,
        history_start: int,
        t_prompt: float,
        t_ready: float,
        post_stage: ReviewTarget | None,
        reasoning_profile: Any,
        routing_signals: Any,
        runtime_mode_context: str,
        routing_decision: Any,
    ) -> None:
        self._advance_turn_phase(
            session, state.turn_state, TurnPhase.POST_STAGE,
            reason="primary_complete",
        )
        post_outcome = self._runtime_seams().post_stage_runner.run(
            session,
            history_start=history_start,
            primary_provider=state.provider,
            primary_model=state.model,
            post_stage=post_stage,
            final_text=state.final_text,
            tool_calls_total=state.tool_calls_total,
            output_tokens=state.output_tokens,
            execution_guard_halted=state.execution_guard_halted,
            reasoning_profile=reasoning_profile,
            routing_signals=routing_signals,
            runtime_mode_context=runtime_mode_context,
            on_reasoning=lambda chunk: self._emit_provider_reasoning(
                session_id, session, state, chunk
            ),
        )
        if post_outcome is not None:
            state.upstream_ms += post_outcome.elapsed_ms
            state.observed_thinking_modes.add(post_outcome.thinking or "default")
            state.observed_efforts.add(post_outcome.effort or "default")
            if post_outcome.usage is not None:
                review_usage = post_outcome.usage
                state.input_tokens += review_usage.input_tokens
                state.output_tokens += review_usage.output_tokens
                state.cached_tokens += review_usage.cached_tokens
                state.cache_write_tokens += review_usage.cache_write_tokens
                state.reasoning_tokens += review_usage.reasoning_tokens
                state.cache_usage_reported = (
                    state.cache_usage_reported or review_usage.cached_tokens_reported
                )
                state.cache_write_usage_reported = (
                    state.cache_write_usage_reported or review_usage.cache_write_tokens_reported
                )
                state.reasoning_usage_reported = (
                    state.reasoning_usage_reported or review_usage.reasoning_tokens_reported
                )
            if post_outcome.applied:
                state.final_text = post_outcome.text
                state.served_provider_id = post_outcome.provider_id
                state.served_model_id = post_outcome.model_id

        if state.final_text:
            self._mark_provider_visible(state)
            self.emit_text(session_id, state.final_text, is_thinking=False)

        self._advance_turn_phase(
            session, state.turn_state, TurnPhase.COMPLETE,
            reason="success",
        )
        self._persist_session(session)
        self._record_provider_turn_telemetry(
            session, state, outcome="success", t_prompt=t_prompt,
            routing_decision=routing_decision,
        )
        self.result(req_id, self._provider_prompt_result(state, "end_turn"))
        t_end = time.perf_counter()
        first_ms = (
            (state.first_visible_at - t_prompt) * 1000.0
            if state.first_visible_at is not None else -1.0
        )
        stream_window_ms = (
            (state.last_visible_at - state.first_visible_at) * 1000.0
            if state.first_visible_at is not None and state.last_visible_at is not None
            else 0.0
        )
        cache_hits = len(session.mcp.catalog_cache_hits) if session.mcp is not None else 0
        mcp_server_ms = ""
        mcp_failures = ""
        if session.mcp is not None:
            mcp_server_ms = ",".join(
                f"{name}:{duration:.0f}"
                for name, duration in sorted(session.mcp.startup_durations_ms.items())
            )
            mcp_failures = ",".join(sorted(session.mcp.startup_failures))
        self.trace(
            f"session/prompt total={(t_end-t_prompt)*1000:.0f}ms "
            f"first_visible={first_ms:.0f}ms "
            f"pre_upstream={(t_ready-t_prompt)*1000:.0f}ms "
            f"mcp_wait={state.mcp_wait_ms:.0f}ms upstream={state.upstream_ms:.0f}ms tool={state.tool_ms:.0f}ms "
            f"tool_calls={state.tool_calls_total} native_calls={state.native_tool_calls_total} "
            f"tool_search_calls={state.tool_search_calls_total} "
            f"tool_search_loaded={state.tool_search_loaded_total} active_tools={state.active_tools_count} "
            f"loaded_real_tools={len(session.loaded_tool_names)} tool_schema_bytes={state.tool_schema_bytes} "
            f"catalog_tools={len(session.mcp.bindings) if session.mcp is not None else 0} "
            f"catalog_cache_hits={cache_hits} chunks={state.stream_chunks} chars={state.stream_chars} "
            f"max_chunk_chars={state.max_chunk_chars} stream_window={stream_window_ms:.0f}ms "
            f"max_gap={state.max_gap_ms:.0f}ms mcp_server_ms=[{mcp_server_ms}] "
            f"mcp_failures=[{mcp_failures}] model={state.model.model_id} "
            f"served_model={state.served_model_id} effort={session.effort or 'default'} "
            f"input_tokens={state.input_tokens} output_tokens={state.output_tokens} "
            f"cached_tokens={state.cached_tokens} cache_write_tokens={state.cache_write_tokens} "
            f"reasoning_tokens={state.reasoning_tokens}"
        )


    def _run_provider_tool_loop(
        self,
        req_id: Any,
        session_id: str,
        session: Any,
        provider: Any,
        model: Any,
        history_start: int,
        t_prompt: float,
        t_ready: float,
        mode_fallbacks: list[tuple[Any, Any]] | None = None,
        post_stage: ReviewTarget | None = None,
        primary_effort: str = "",
        primary_thinking: str = "",
        relay: Any = None,
        reasoning_profile: Any = None,
        routing_signals: Any = None,
        runtime_mode_context: str = "",
        routing_config: Any = None,
        routing_decision: Any = None,
        execution_plan: ExecutionPlan | None = None,
    ) -> None:
        """Run one provider/tool turn while keeping the state-machine edges explicit.

        The ten historical ``continue`` branches stay visible here. Four rewind
        ``state.step`` before continuing and six intentionally advance to a new
        logical step. Helpers own responsibilities, not loop-control semantics.
        """
        state = self._build_provider_loop_state(
            session,
            provider,
            model,
            mode_fallbacks=mode_fallbacks,
            primary_effort=primary_effort,
            primary_thinking=primary_thinking,
            relay=relay,
            reasoning_profile=reasoning_profile,
            routing_signals=routing_signals,
        )

        try:
            while True:
                # The only step increment in the turn engine. Same-step retries
                # rewind immediately before their corresponding ``continue``.
                state.step += 1
                state.round_text_parts.clear()
                state.round_reasoning_emitted = False

                if self._run_planned_subagents_for_round(
                    session,
                    state,
                    execution_plan=execution_plan,
                    routing_config=routing_config,
                    routing_decision=routing_decision,
                    reasoning_profile=reasoning_profile,
                    routing_signals=routing_signals,
                    runtime_mode_context=runtime_mode_context,
                ):
                    # C1 / historical line 3080: child workers hand back to lead
                    # without consuming a logical step.
                    state.step -= 1
                    continue

                round_ctx = self._prepare_provider_round(
                    session,
                    state,
                    reasoning_profile=reasoning_profile,
                    routing_signals=routing_signals,
                    routing_config=routing_config,
                    routing_decision=routing_decision,
                )

                if session.cancel_event.is_set():
                    self._recover_current_turn(
                        session,
                        history_start,
                        reason="cancelled_before_invoke",
                        preserve_side_effects=state.side_effect_risk_seen,
                    )
                    self._advance_turn_phase(
                        session, state.turn_state, TurnPhase.COMPLETE,
                        reason="cancelled_before_invoke",
                    )
                    self._record_provider_turn_telemetry(
                        session, state, outcome="cancelled", t_prompt=t_prompt,
                        routing_decision=routing_decision, error_category="control",
                    )
                    self.result(req_id, self._provider_prompt_result(state, "cancelled"))
                    return

                directive, round_outcome = self._invoke_provider_loop_round(
                    req_id,
                    session_id,
                    session,
                    state,
                    round_ctx,
                    history_start=history_start,
                    t_prompt=t_prompt,
                    routing_decision=routing_decision,
                    runtime_mode_context=runtime_mode_context,
                )
                if directive is _LoopDirective.RETURN:
                    return
                if directive is _LoopDirective.STEER_AFTER_INTERRUPT:
                    # C2 / historical line 3201: steering after interruption is
                    # a new logical step, so there is deliberately no rewind.
                    continue
                if directive is _LoopDirective.WORKER_FAILED_HAND_BACK:
                    # C3 / historical line 3246: failed worker gives the same
                    # logical step back to the lead.
                    state.step -= 1
                    continue
                if directive is _LoopDirective.RETRY_SAME_MODEL:
                    # C4 / historical line 3286: pristine transport retry keeps
                    # the same logical step identity for logs/cache/guard.
                    state.step -= 1
                    continue
                if directive is _LoopDirective.PRISTINE_FALLBACK:
                    # C5 / historical line 3322: pristine model fallback also
                    # preserves the logical step identity.
                    state.step -= 1
                    continue
                if round_outcome is None:
                    raise RuntimeError("provider loop produced no round outcome")

                turn = round_outcome.turn
                control = round_outcome.control
                if state.round_text_parts or state.round_reasoning_emitted:
                    self._advance_turn_phase(
                        session, state.turn_state, TurnPhase.STREAM,
                        reason="provider_stream_observed",
                    )
                self._apply_provider_round_usage(
                    state, turn, lead=(round_ctx.role == "lead")
                )

                if not control.requires_tool_batch:
                    directive = self._handle_no_tool_round(
                        session, state, round_ctx, turn
                    )
                    if directive is _LoopDirective.WORKER_FINDINGS_HAND_BACK:
                        # C6 / historical line 3364: a completed worker round
                        # consumed a logical step; lead synthesis advances.
                        continue
                    if directive is _LoopDirective.QUEUED_STEER_AFTER_FINAL:
                        # C7 / historical line 3376: a steer after a final
                        # candidate starts the next logical step.
                        continue
                    if directive is _LoopDirective.FINAL:
                        break
                    raise RuntimeError(f"unexpected no-tool directive: {directive}")

                directive = self._handle_tool_round(
                    req_id,
                    session,
                    state,
                    round_ctx,
                    turn,
                    history_start=history_start,
                    t_prompt=t_prompt,
                    routing_decision=routing_decision,
                )
                if directive is _LoopDirective.RETURN:
                    return
                if directive is _LoopDirective.WORKER_GUARD_HAND_BACK:
                    # C8 / historical line 3518: guard hand-back advances; the
                    # worker really executed a tool batch in this step.
                    continue
                if directive is _LoopDirective.EXECUTION_GUARD_REPLAN:
                    # C9 / historical line 3551: explicit re-plan is a new step.
                    continue
                if directive is _LoopDirective.STEER_AFTER_TOOLS:
                    # C10 / historical line 3556: steering after tools advances.
                    continue
                if directive is _LoopDirective.STOP:
                    break
                if directive is not _LoopDirective.NONE:
                    raise RuntimeError(f"unexpected tool directive: {directive}")
                # Normal tool results fall through to the loop head. This is the
                # historical implicit continuation after line 3565 and advances
                # the logical step exactly once at the next iteration.

        except ProviderError as exc:
            error_category = str(getattr(exc, "category", "provider") or "provider")
            self._recover_current_turn(
                session,
                history_start,
                reason=f"{error_category}_error",
                preserve_side_effects=state.side_effect_risk_seen,
            )
            if not state.turn_state.complete:
                self._advance_turn_phase(
                    session, state.turn_state, TurnPhase.COMPLETE,
                    reason="provider_error",
                )
            self.emit_runtime_error(session_id, str(exc))
            self._record_provider_turn_telemetry(
                session,
                state,
                outcome="error",
                t_prompt=t_prompt,
                routing_decision=routing_decision,
                error_kind=type(exc).__name__,
                error_category=error_category,
            )
            self.result(req_id, self._provider_prompt_result(state, "end_turn"))
            t_end = time.perf_counter()
            self.trace(
                f"session/prompt ERROR total={(t_end-t_prompt)*1000:.0f}ms "
                f"pre_upstream={(t_ready-t_prompt)*1000:.0f}ms "
                f"mcp_wait={state.mcp_wait_ms:.0f}ms upstream={state.upstream_ms:.0f}ms tool={state.tool_ms:.0f}ms "
                f"provider_rounds={state.provider_rounds_total} tool_calls={state.tool_calls_total} native_calls={state.native_tool_calls_total} "
                f"tool_search_calls={state.tool_search_calls_total} "
                f"loaded_tools={len(session.loaded_tool_names)} model={state.model.model_id} "
                f"error_category={error_category} side_effects={sorted(state.side_effect_classes_seen)} error={exc}"
            )
            return
        except Exception as exc:
            self._recover_current_turn(
                session,
                history_start,
                reason="internal_error",
                preserve_side_effects=state.side_effect_risk_seen,
            )
            if not state.turn_state.complete:
                self._advance_turn_phase(
                    session, state.turn_state, TurnPhase.COMPLETE,
                    reason="internal_error",
                )
            self._runtime_event(
                "runtime.internal_error",
                session,
                data={
                    "error": type(exc).__name__,
                    "provider_id": state.served_provider_id,
                    "model": state.served_model_id,
                    "side_effects": sorted(state.side_effect_classes_seen),
                },
            )
            self.trace(
                f"recovered internal turn error session={session_id} "
                f"{type(exc).__name__}: {exc}"
            )
            self.emit_runtime_error(
                session_id,
                f"Kiro BYO runtime error ({type(exc).__name__}). The session was recovered and can continue.",
            )
            self._record_provider_turn_telemetry(
                session,
                state,
                outcome="error",
                t_prompt=t_prompt,
                routing_decision=routing_decision,
                error_kind=type(exc).__name__,
                error_category="internal",
            )
            self.result(req_id, self._provider_prompt_result(state, "end_turn"))
            return

        self._run_provider_post_stage(
            req_id,
            session_id,
            session,
            state,
            history_start=history_start,
            t_prompt=t_prompt,
            t_ready=t_ready,
            post_stage=post_stage,
            reasoning_profile=reasoning_profile,
            routing_signals=routing_signals,
            runtime_mode_context=runtime_mode_context,
            routing_decision=routing_decision,
        )

    def _handle_prompt_request(self, req_id: Any, params: dict[str, Any]) -> None:
        t_prompt = time.perf_counter()
        session_id = str(params.get("sessionId") or "")
        session = self._get_session(session_id)
        if session is None:
            self.error(req_id, -32602, "unknown session")
            return
        if not session.turn_lock.acquire(blocking=False):
            # Standard ACP prompts are single-turn.  KiroCrew owns the product
            # queue and, when the running harness advertises it, injects a
            # mid-turn message through ``_session/steer``.  Do not invent a
            # second queue inside the harness: doing so makes delivery ordering
            # differ from native Kiro and breaks Crew's durable queue receipts.
            #
            # Durable recovery (resume/retry/replan) reuses the caller's req_id
            # but is NOT a client prompt: its own entry point already reported a
            # dedicated busy code (-32002), and steering is not the remedy for a
            # recovery race. Emit the recovery-consistent error so a lost
            # turn_lock race (session.active_turn check -> acquire TOCTOU) does
            # not surface a misleading "use _session/steer" terminal response
            # against the recovery request's req_id (P0-4).
            if str(params.get("_kiro_recovery_strategy") or "").strip():
                self.error(req_id, -32002, "session is busy; wait before resuming a durable run")
            else:
                self.error(req_id, -32000, "session busy; use the advertised _session/steer extension or let the client queue the message")
            return

        # Registry replacement may have retired this object while this worker
        # waited to enter the turn. Never start work on a retired runtime.
        with self._sessions_lock:
            unavailable = session.retired or session.replacing or self.sessions.get(session_id) is not session
        if unavailable:
            session.turn_lock.release()
            self.error(req_id, -32002, "session is being replaced or has been closed")
            return

        self._begin_prompt_response_buffer(req_id)

        with session.state_lock:
            session.cancel_event.clear()
            session.steer_event.clear()
            session.steer_queue.clear()
            session.cancel_source = ""
            session.cancel_requested_at = 0.0
            session.turn_started_at = t_prompt
            session.active_turn = True

        history_start = len(session.messages)
        resume_run_id = str(params.get("_kiro_resume_run_id") or "")
        recovery_strategy = str(params.get("_kiro_recovery_strategy") or "")
        try:
            text = self.prompt_text(params)
            user_message, has_image = self.prompt_message(params)

            # Echo fallback remains useful for installation diagnostics.
            if session.model in {MODEL_ECHO, MODEL_UPPER}:
                if text.strip().startswith("/compact"):
                    with session.state_lock:
                        clear_messages(session, reason="legacy_compact_reset")
                    self._persist_session(session)
                    self.result(req_id, {"stopReason": "end_turn"})
                    return
                reply = f"Echo: {text}"
                if session.model == MODEL_UPPER:
                    reply = reply.upper()
                self.emit_text(session_id, reply)
                self.result(req_id, {"stopReason": "end_turn"})
                return

            try:
                config = self._config()
            except ConfigError as exc:
                self.emit_runtime_error(session_id, f"configuration error: {exc}")
                self.result(req_id, {"stopReason": "end_turn"})
                return

            # Durable recovery mutates transcript/routing state only after this
            # prompt owns the session turn lock. The public recovery handler does
            # validation and prompt reconstruction but never rewinds state itself,
            # avoiding a race with a concurrently arriving ordinary prompt.
            if resume_run_id:
                record = self._durable_run_store().get(resume_run_id)
                recovery = RecoveryPolicy.durable_run(record, strategy=recovery_strategy or "resume")
                if (
                    not recovery.safe or record is None
                    or str(record.get("session_id") or "") != session_id
                ):
                    self.error(req_id, -32010, f"durable recovery is no longer safe: {recovery.reason}")
                    return
                metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
                try:
                    recovery_history_start = max(0, int(metadata.get("history_start", 0)))
                except (TypeError, ValueError):
                    recovery_history_start = 0
                with session.state_lock:
                    if len(session.messages) > recovery_history_start:
                        truncate_messages(session, recovery_history_start, reason="durable_recovery_rewind")
                        append_lifecycle(
                            session, "run/recovery_rewind",
                            {
                                "run_id": resume_run_id, "history_start": recovery_history_start,
                                "strategy": recovery_strategy or "resume",
                            },
                        )
                    if (recovery_strategy or "").lower() == "replan":
                        selected_type_for_replan = self._type_for_selection(config, session.model)
                        if selected_type_for_replan is not None:
                            clear_route_pin(
                                session, f"{config.mode_pack}:{selected_type_for_replan}",
                                reason="durable_replan",
                            )
                self._persist_session(session)
                self._runtime_event(
                    "run.recovery.rewound", session,
                    data={
                        "run_id": resume_run_id, "history_start": recovery_history_start,
                        "strategy": recovery_strategy or "resume",
                    },
                )

            selection_truth = self._ensure_selection_truth(session, config)
            session.response_language_preference = getattr(config, "response_language", "auto")
            session.ui_language_preference = getattr(config, "ui_language", "auto")
            self._apply_reasoning_language_policy(
                session, getattr(config, "reasoning_language", "auto"), text
            )
            mode_fallbacks: list[tuple[Any, Any]] = []
            post_stage: ReviewTarget | None = None
            primary_effort = ""
            primary_thinking = ""
            reasoning_profile = None
            routing_signals = None
            runtime_mode_context = ""
            relay: Any = None
            execution_plan: ExecutionPlan | None = None
            # Routed Type turns populate a RouteDecision. Concrete-model turns
            # intentionally bypass the router, so keep the value explicitly
            # absent instead of leaving a branch-local name unbound.
            decision = None
            selected_type = self._type_for_selection(config, session.model)
            if selected_type is not None:
                # Tool visibility and current-turn tool need are independent. A
                # Personal Agent may expose many tools while the user only says
                # "hello"; that must not exclude otherwise valid models.
                route_tools = self._active_tools_for_turn(session)
                tools_available = bool(route_tools)
                tools_required = infer_tools_required(
                    text, tools=route_tools, history=self._runtime_seams().session_projection.snapshot(session).messages
                )
                decision = self._runtime_seams().model_router.route(
                    config,
                    pack=config.mode_pack,
                    policy=selected_type,
                    prompt=text,
                    history=self._runtime_seams().session_projection.snapshot(session).messages,
                    has_image=has_image,
                    tools_required=tools_required,
                    tools_available=tools_available,
                    # Type effort is chosen only after a concrete model is selected.
                    # Never let a stale/manual session level filter the Type pool.
                    reasoning_effort="",
                )
                if decision is None:
                    self._runtime_event(
                        "model.route.failed", session,
                        data={"pack": config.mode_pack, "policy": selected_type, "reason": "no eligible model"},
                    )
                    self.emit_runtime_error(session_id, "Kiro BYO mode could not find an eligible configured model for this turn")
                    self.result(req_id, {"stopReason": "end_turn"})
                    return
                provider, model = decision.provider, decision.model
                execution_intent = decision.execution_intent
                runtime_mode_context = runtime_policy_context_for_request(
                    decision.pack, decision.policy, text, active=True
                )
                routing_signals = decision.signals
                pin_key = f"{decision.pack}:{decision.policy}"
                if execution_intent.sticky:
                    pins = self._runtime_seams().session_projection.snapshot(session).route_pins
                    pinned = pins.get(pin_key) if isinstance(pins, dict) else None
                    if pinned:
                        # Keep a long task on the model it started with, as long
                        # as that model is still eligible for this turn.
                        for row in decision.candidates:
                            if (row.provider.id, row.model.model_id) == pinned:
                                provider, model = row.provider, row.model
                                break
                    if isinstance(pins, dict):
                        set_route_pin(
                            session, pin_key, provider.id, model.model_id, reason="sticky_policy"
                        )
                if provider is not decision.provider or model is not decision.model:
                    from dataclasses import replace as _dc_replace

                    decision = _dc_replace(decision, provider=provider, model=model)
                mode_fallbacks = [
                    (row.provider, row.model) for row in decision.execution_fallbacks
                    if (row.provider.id, row.model.model_id) != (provider.id, model.model_id)
                ]
                # AUTO thinking and AUTO effort are independent policy axes.
                # The concrete model's capabilities and explicit user locks are
                # resolved before anything reaches a provider adapter.
                reasoning_profile = execution_intent.reasoning
                primary_thinking, primary_effort = self._runtime_seams().reasoning_resolver.resolve(
                    model, session, reasoning_profile, decision.signals,
                    role="lead", stage="planning",
                )
                execution_plan = build_execution_plan(decision)
                if execution_plan.worker is not None:
                    teammate = self._relay_teammate(
                        decision, provider, model, config=config, signals=decision.signals
                    )
                    if teammate is not None:
                        # The tuple remains a compatibility carrier for the
                        # currently routed worker. The plan, not a Type-specific
                        # branch, decides whether this role exists.
                        relay = (teammate[0], teammate[1], "")
                review_target = execution_plan.post_stage
                post_stage = review_target
                append_lifecycle(
                    session,
                    "model/selected",
                    {
                        "pack": decision.pack,
                        "policy": decision.policy,
                        "provider_id": provider.id,
                        "model": model.model_id,
                        "execution_mode": execution_plan.mode,
                        "selection_kind": selection_truth["kind"],
                        "selection_source": selection_truth["source"],
                        "routing_applied": True,
                    },
                )
                self._runtime_event(
                    "model.route.selected", session,
                    data={
                        "requested_model": session.model,
                        "selection": selection_truth,
                        "pack": decision.pack,
                        "pack_label": decision.pack_label,
                        "policy": decision.policy,
                        "policy_label": decision.policy_label,
                        "execution_mode": execution_plan.mode,
                        "execution_rationale": list(execution_plan.rationale),
                        "secondary_stage": (
                            {
                                "role": review_target.role,
                                "provider_id": review_target.provider.id,
                                "model": review_target.model.model_id,
                                "reason": review_target.reason,
                            } if review_target else None
                        ),
                        "provider_id": provider.id,
                        "resolved_model": model.model_id,
                        "resolved_model_name": model.name,
                        "signals": {
                            "prompt_chars": decision.signals.prompt_chars,
                            "history_chars": decision.signals.history_chars,
                            "history_messages": decision.signals.history_messages,
                            "has_image": decision.signals.has_image,
                            "tools_available": decision.signals.tools_available,
                            "tools_required": decision.signals.tools_required,
                            # Compatibility field for older Runtime Inspector readers.
                            "tools_expected": decision.signals.tools_required,
                            "reasoning_effort": decision.signals.reasoning_effort,
                            "code_task": decision.signals.code_task,
                            "complexity": round(decision.signals.complexity, 4),
                            "breadth": round(decision.signals.breadth, 4),
                            "parallelizability": round(decision.signals.parallelizability, 4),
                            "risk": round(decision.signals.risk, 4),
                            "tool_intensity": round(decision.signals.tool_intensity, 4),
                            "write_intensity": round(decision.signals.write_intensity, 4),
                            "context_demand": round(decision.signals.context_demand, 4),
                            "verification_value": round(decision.signals.verification_value, 4),
                            "latency_sensitivity": round(decision.signals.latency_sensitivity, 4),
                            "task_profile": decision.signals.task_profile,
                            "estimated_input_tokens": decision.signals.estimated_input_tokens,
                        },
                        "reasons": list(decision.reason),
                        "candidates": [
                            {
                                "provider_id": row.provider.id,
                                "model": row.model.model_id,
                                "name": row.model.name,
                                "score": round(row.score, 5),
                                "quality": round(row.quality, 4),
                                "speed": round(row.speed, 4),
                                "economy": round(row.economy, 4),
                                "reliability": round(row.reliability, 4),
                                "runtime_samples": row.runtime_samples,
                                "runtime_confidence": row.runtime_confidence,
                                "static_speed": round(row.static_speed, 4),
                                "runtime_speed": (round(row.runtime_speed, 4) if row.runtime_speed is not None else None),
                                "reasons": list(row.reasons),
                            }
                            for row in decision.candidates[:12]
                        ],
                    },
                    content={"prompt": text},
                )
            else:
                resolved = self._resolved_model(config, session)
                if resolved is None:
                    self.emit_runtime_error(
                        session_id,
                        f"model {session.model!r} is no longer present in config/providers.json",
                    )
                    self.result(req_id, {"stopReason": "end_turn"})
                    return
                provider, model = resolved
                runtime_mode_context = runtime_policy_context_for_request(
                    config.mode_pack, config.mode_type, text, active=False
                )
                # A concrete model selection still obeys that model's persistent
                # BYO Auto/On/Off preference. With no Type suggestion, Auto
                # preserves the verified model/provider default.
                primary_thinking = resolve_thinking_mode(
                    model, getattr(model, "thinking_preference", "auto"), ""
                )
                append_lifecycle(
                    session,
                    "model/selected",
                    {
                        "pack": "",
                        "policy": "concrete",
                        "provider_id": provider.id,
                        "model": model.model_id,
                        "execution_mode": "single_pass",
                        "selection_kind": selection_truth["kind"],
                        "selection_source": selection_truth["source"],
                        "routing_applied": False,
                    },
                )
                self._runtime_event(
                    "model.route.bypassed", session,
                    data={
                        "requested_model": session.model,
                        "selection": selection_truth,
                        "provider_id": provider.id,
                        "resolved_model": model.model_id,
                        "reason": "concrete model selected",
                    },
                    content={"prompt": text},
                )
            t_ready = time.perf_counter()

            if text.strip().startswith("/compact"):
                # Compatibility route for Crew 0.9.0-insider.1, which still sends
                # manual compaction as a prompt. Current Kiro V3 clients should
                # use the advertised ``_kiro/session/compact`` extension instead.
                # The compaction body + lifecycle emission is shared with the
                # typed extension via _run_compaction; this path only adds its
                # own JSON-RPC response.
                summary, detail = self._run_compaction(
                    session, provider, model, self._compact_context_suffix(text)
                )
                if summary is None:
                    if detail == "cancelled":
                        self.result(req_id, {"stopReason": "cancelled"})
                    else:
                        self.error(req_id, -32603, f"compaction failed: {detail}")
                    return
                self.result(req_id, {"stopReason": "end_turn"})
                return

            # Empty modalities mean "provider did not declare this capability",
            # not an explicit text-only claim. In that unknown case let the
            # upstream decide; only block images when the provider supplied a
            # non-empty modality list that excludes image input.
            if has_image and model.input_modalities and "image" not in model.input_modalities:
                self.emit_runtime_error(
                    session_id,
                    f"{model.name} explicitly does not advertise image input. Choose an image-capable model.",
                )
                self.result(req_id, {"stopReason": "end_turn"})
                return

            with session.state_lock:
                history_start = len(session.messages)
                append_lifecycle(session, "turn/start", {
                    "history_start": history_start,
                    "recovery_run_id": resume_run_id,
                    "recovery_strategy": recovery_strategy,
                })
                append_message(session, user_message)
                user_event_seq = len(session.events) - 1
                session.active_turn_id = f"{session.session_id}:{user_event_seq}"
            # Persist the user-message anchor before any provider/tool side effect.
            # Durable recovery references this immutable event even if the active
            # transcript is later transactionally truncated.
            self._persist_session(session)

            # Materialize the logical agent graph only after all prompt-level
            # early exits have passed. This keeps AgentTask lifecycle aligned
            # with a real turn transaction instead of leaving orphan plans for
            # /compact or unsupported-input requests.
            if execution_plan is not None:
                self._begin_agent_plan(session, execution_plan)
            self._begin_durable_run(
                session, execution_plan, selection=str(selection_truth.get("id") or session.model),
                provider_id=provider.id, model_id=model.model_id, history_start=history_start,
                user_event_seq=user_event_seq, resume_run_id=resume_run_id,
                recovery_strategy=recovery_strategy,
            )

            self._run_provider_tool_loop(
                req_id, session_id, session, provider, model, history_start, t_prompt, t_ready, mode_fallbacks, post_stage,
                primary_effort=primary_effort,
                primary_thinking=primary_thinking,
                relay=relay,
                reasoning_profile=reasoning_profile,
                routing_signals=routing_signals,
                runtime_mode_context=runtime_mode_context,
                routing_config=config,
                routing_decision=decision,
                execution_plan=execution_plan,
            )
            return
        finally:
            with session.state_lock:
                session.active_turn = False
                session.active_turn_id = ""
                session.cancel_event.clear()
                session.steer_event.clear()
                session.steer_queue.clear()
                session.turn_started_at = 0.0
                session.cancel_requested_at = 0.0
                session.cancel_source = ""
            # Release the authoritative turn boundary before exposing the
            # terminal JSON-RPC response. A client may enqueue the next prompt
            # immediately after receiving it.
            session.turn_lock.release()
            terminal_payload = self._take_prompt_response_buffer(req_id)
            if terminal_payload is not None:
                self.send(terminal_payload)
