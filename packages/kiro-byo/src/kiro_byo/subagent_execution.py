"""Isolated child-Agent execution and continuation for the ACP turn runtime.

The owning TurnRunner delegates to this mixin through the original method names.
Children share only the parent session's approved host resources, not its transcript.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

from .agent_runtime import AgentResult, AgentRuntime, AgentTask, AgentTaskBusyError, ExecutionPlan
from .acp_worker import AcpAgentCatalog, run_acp_agent_task
from .native_tools import NativeToolRuntime
from .providers import ProviderError, ProviderInterrupted
from .recovery_policy import RecoveryPolicy
from .sandbox import SandboxError
from .session_events import append_lifecycle, append_message
from .subagent_tool_policy import filter_isolated_subagent_tools, subagent_system_instruction


class SubagentExecutionMixin:
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
            presentation_language=str(getattr(parent, "presentation_language", "") or ""),
            tool_display_language=str(getattr(parent, "tool_display_language", "") or ""),
            ui_language_preference=str(getattr(parent, "ui_language_preference", "auto") or "auto"),
            response_language=str(getattr(parent, "response_language", "") or ""),
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
            usage = dict.fromkeys(("input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens",
                                   "reasoning_tokens", "provider_rounds", "tool_calls", "transport_retries"), 0)
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
                tools = filter_isolated_subagent_tools(
                    self._active_tools_for_turn(child), writer_mode=writer_mode,
                    native=child.native,
                    is_gathering_tool=lambda name: self._relay_gathering_tool(child, name),
                )
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
                    {"role": "system", "content": subagent_system_instruction(task.spec.objective, writer_mode=writer_mode)},
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
                        turn = self._subagent_chat_completion_turn(
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
                for counter in ("input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens", "reasoning_tokens"):
                    usage[counter] += int(getattr(turn.usage, counter))
                for key in ("cached_tokens_reported", "cache_write_tokens_reported", "reasoning_tokens_reported"):
                    usage[key] = bool(usage.get(key)) or bool(getattr(turn.usage, key, False))
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
        parallel_limit = min(int(execution_plan.max_parallel), len(runnable))
        append_lifecycle(
            parent, "agent/subagents_started",
            {
                "plan_id": plan_id, "count": len(runnable),
                "max_parallel": parallel_limit,
                "task_ids": [task.task_id for task in runnable],
            },
        )
        self._runtime_event(
            "agent.subagents.started", parent,
            data={
                "plan_id": plan_id, "count": len(runnable),
                "max_parallel": parallel_limit,
                "tasks": [task.public_dict() for task in runnable],
            },
        )

        def runner(task: AgentTask) -> AgentResult:
            provider, model = assignments[task.task_id]
            return self._run_isolated_subagent(
                parent, task, provider, model, reasoning_profile=reasoning_profile,
                reasoning_signals=reasoning_signals, runtime_mode_context=runtime_mode_context,
            )

        completed = runtime.run_group(runnable, runner, max_parallel=parallel_limit)
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
        self._checkpoint_durable_run(parent, "subagents_completed", data={
            "count": len(completed), "task_ids": [task.task_id for task, _ in completed],
        })
        return completed


    def _resume_agent_task(self, session: Any, task_id: str, *, message: str = "") -> AgentResult:
        """Continue a child once, or queue a steer for its existing execution.

        A resume RPC must never fork another runner for an in-flight task. The
        parent/Crew owns its task queue; BYO only holds messages for this child
        until its next model boundary or a subsequent explicit resume.
        """
        registry = getattr(session, "agent_registry", None)
        task = registry.get(task_id) if registry is not None else None
        if task is None:
            return AgentResult(status="failed", unresolved=("unknown agent task",))
        # resume_lock is separate from task.lock, since the worker's transition
        # and output reporting require task.lock in another thread.
        if not task.resume_lock.acquire(blocking=False):
            if message.strip() and not task.cancel_event.is_set():
                registry.send_message(task_id, message.strip())
            return AgentResult(
                status="cancelled" if task.cancel_event.is_set() else "running",
                unresolved=("cancellation is in progress" if task.cancel_event.is_set()
                            else "task is already executing; use its existing mailbox",),
                provider_id=task.provider_id, model_id=task.model_id,
            )
        try:
            runtime = self._agent_runtime_for_session(session)
            if task.status == "running" or (runtime is not None and runtime.is_active(task_id)):
                if message.strip() and not task.cancel_event.is_set():
                    registry.send_message(task_id, message.strip())
                return AgentResult(
                    status="cancelled" if task.cancel_event.is_set() else "running",
                    unresolved=("cancellation is in progress" if task.cancel_event.is_set()
                                else "task is already executing; message retained for the next boundary",),
                    provider_id=task.provider_id, model_id=task.model_id,
                )
            return self._resume_agent_task_once(session, task_id, message=message)
        except AgentTaskBusyError:
            # Defense-in-depth if a different execution entry point claimed
            # the task between the active check and actual submission.
            if message.strip() and not task.cancel_event.is_set():
                registry.send_message(task_id, message.strip())
            return AgentResult(
                status="cancelled" if task.cancel_event.is_set() else "running",
                unresolved=("task is already executing",),
                provider_id=task.provider_id, model_id=task.model_id,
            )
        finally:
            task.resume_lock.release()

    def _resume_agent_task_once(self, session: Any, task_id: str, *, message: str = "") -> AgentResult:
        """Run a new generation after admission has been serialised."""
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
