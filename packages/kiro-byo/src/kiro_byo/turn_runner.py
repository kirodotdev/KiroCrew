"""One-turn execution loop for the Kiro BYO ACP harness.

The transport/session server owns JSON-RPC framing and lifecycle.  This mixin owns
provider inference, streaming, tool-search/tool-call iteration and per-turn
telemetry.  Keeping this loop out of ``acp_server.py`` prevents session protocol
changes from sharing a 700-line method with model/tool execution.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any

RUN_LEASE_HEARTBEAT_SECONDS = 20.0
from .host_work_link import validate_work_link
from .routing_events import selected_route_event_data
from .crew_turn_context import (
    extend_crew_context, crew_artifact_tool_names_for_turn,
    crew_browser_action_requested,
)
from .agent_runtime import AgentTask, ExecutionPlan
from .durable_run_lifecycle import DurableRunLifecycleMixin
from .agent_plan_lifecycle import AgentPlanLifecycleMixin
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
from .mcp_client import DEFERRED_TOOL_ACTIVE_LIMIT, DEFERRED_TOOL_SEARCH_NAME
from .mcp_tool_dispatch import McpToolDispatchMixin
from .native_tool_dispatch import NativeToolDispatchMixin
from .provider_loop_terminal import ProviderLoopTerminalMixin
from .prompt_routing import PromptRoutingMixin
from .native_tools import NativeToolRuntime
from .protocol import MODEL_ECHO, MODEL_UPPER
from .mode_router import (
    ReviewTarget,
    build_signals,
    model_in_tier,
)
from .providers import ProviderError, ProviderInterrupted, ProviderTransportError, chat_completion_turn
from .reasoning_control import (
    ReasoningInvocation,
    resolve_effort_level,
    resolve_thinking_mode,
    thinking_auto_suggestion,
    review_effort_level,
    review_thinking_mode,
)
from .presentation_language import resolve_presentation_language
from .tool_display import session_display_locale
from .recovery_policy import RecoveryPolicy
from .session_events import (
    append_lifecycle,
    append_message,
    clear_messages,
    clear_route_pin,
    revise_last_assistant,
    truncate_messages,
)
from .turn_review_messages import build_review_messages
from .subagent_execution import SubagentExecutionMixin
from .tool_batch_execution import ToolBatchExecutionMixin
from .turn_state import RoundDisposition, RoundOutcome, TurnPhase, TurnState
from .turn_loop_types import (
    _PostStageOutcome, _ProviderRoundOutcome, _LoopDirective,
    _ProviderLoopState, _ProviderRoundContext, DurableCheckpointError, _ToolBatchOutcome,
)
from .state_io import _file_transaction_lock

# Native capabilities that only read or look things up.
_RELAY_GATHERING_CAPABILITIES = frozenset({"fs_read", "web_fetch", "web_search"})
_SAFE_TOOL_EFFECTS = frozenset({"pure", "read"})
_MUTATING_NATIVE_TOOLS = frozenset({
    "write", "edit", "delete_file", "shell", "execute_shell_process", "control_shell_process"
})



class TurnRunnerMixin(DurableRunLifecycleMixin, AgentPlanLifecycleMixin, SubagentExecutionMixin, PromptRoutingMixin, NativeToolDispatchMixin, McpToolDispatchMixin, ProviderLoopTerminalMixin, ToolBatchExecutionMixin):
    @staticmethod
    def _subagent_chat_completion_turn(*args: Any, **kwargs: Any) -> Any:
        """Keep child inference on the same replaceable provider boundary as parent turns."""
        return chat_completion_turn(*args, **kwargs)

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

    def _start_run_lease_heartbeat(self, session: Any, run_id: str, owner_token: str) -> None:
        """Keep a live turn leased even during long provider waits.

        Losing the lease never grants a new worker permission to replay a tool;
        a critical checkpoint will reject the stale owner before tool dispatch.
        """
        stopped = threading.Event()
        session.active_run_lease_stop = stopped
        session.active_run_lease_lost = False

        def beat() -> None:
            while not stopped.wait(RUN_LEASE_HEARTBEAT_SECONDS):
                try:
                    renewed = self._durable_run_store().renew_lease(
                        run_id, owner_token=owner_token, lease_ttl=120.0,
                    )
                    # A missing ledger returns None instead of raising. This
                    # must fail closed just like an expired or stolen lease.
                    if renewed is None:
                        raise ValueError("durable run lease no longer exists")
                except (OSError, ValueError, TimeoutError):
                    # stop() has a bounded join: an old renewal may finish
                    # after a *new* turn installs its heartbeat. Never let
                    # that stale worker poison the new run's lease flag.
                    if (not stopped.is_set()
                            and getattr(session, "active_run_lease_stop", None) is stopped):
                        session.active_run_lease_lost = True
                        self.trace("durable run lease lost; subsequent tool admission is denied")
                    return

        worker = threading.Thread(target=beat, name="kiro-byo-run-lease", daemon=True)
        session.active_run_lease_thread = worker
        worker.start()

    def _stop_run_lease_heartbeat(self, session: Any) -> None:
        stopped = getattr(session, "active_run_lease_stop", None)
        if stopped is not None:
            stopped.set()
        worker = getattr(session, "active_run_lease_thread", None)
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=0.2)
        session.active_run_lease_stop = None
        session.active_run_lease_thread = None



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
        decision = resolve_presentation_language(
            current_text, history,
            ui_preference=str(getattr(session, "ui_language_preference", "auto") or "auto"),
            response_preference=str(getattr(session, "response_language_preference", "auto") or "auto"),
            reasoning_preference=preference,
        )
        session.presentation_language = decision.language
        session.presentation_language_source = decision.source
        session.tool_display_language = decision.tool_display_language
        session.reasoning_language_preference = preference
        session.reasoning_language = decision.reasoning_language
        session.reasoning_language_source = decision.reasoning_source
        session.reasoning_language_instruction = decision.reasoning_instruction
        session.response_language = decision.response_language
        session.response_language_source = decision.source
        session.response_language_instruction = decision.response_instruction
        session.language_instruction = f"{decision.reasoning_instruction}\n{decision.response_instruction}"
        self._runtime_event(
            "reasoning.language.resolved",
            session,
            data={
                "preference": preference,
                "language": decision.reasoning_language,
                "source": decision.reasoning_source,
                "response_language": session.response_language,
                "tool_display_language": decision.tool_display_language,
                "presentation_language": decision.language,
                "presentation_source": decision.source,
                "reasoning_source": decision.reasoning_source,
                "ui_preference": session.ui_language_preference,
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

    def _pre_dispatch_tool_interrupt(
        self, session: Any, call_id: str, tool_name: str, *, kind: str,
    ) -> tuple[str, str]:
        """Recheck Crew control state after approval and before side effects.

        Approval is not a license to dispatch after a cancel or steer arrives.
        The result is (message, outcome); an empty outcome means dispatch may
        proceed.  Callers are responsible for revoking any temporary grants.
        """
        reason = self._tool_interrupt_reason(session)
        if not reason:
            return "", ""
        message = (
            "Tool execution stopped before dispatch: user cancelled the turn."
            if reason == "cancelled" else
            "Tool execution stopped before dispatch: user steering changed the task."
        )
        self._emit_tool_status(session, call_id, status="failed", text=message)
        self._runtime_event(
            "tool.dispatch.blocked", session,
            data={"kind": kind, "call_id": call_id, "tool": tool_name, "reason": reason},
        )
        return message, reason

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
        # MCP annotations are untrusted server-supplied hints, not an enforced
        # permission or idempotency guarantee.  In particular, treating a
        # readOnlyHint as proven read-only could erase the execution transcript
        # after a failed turn and cause the model to replay external effects.
        # Until an authenticated Crew capability contract attests effects,
        # external tools must preserve the uncertain-side-effect boundary.
        return "unknown_write"

    def _consume_queued_steers_for_turn(
        self, session: Any, partial_assistant: str = "", *, close_if_empty: bool = False
    ) -> bool:
        """Inject a batch at a model boundary with an atomic admission fence.

        Incoming steering races against provider completion. If no correction
        is pending at the final candidate, close admission while holding the
        same state lock as _queue_steer; Crew can then queue late messages for
        the next turn instead of receiving a false queued=True receipt.
        """
        with session.state_lock:
            if session.cancel_event.is_set():
                return False
            if not session.steer_queue:
                if close_if_empty:
                    session.steer_accepting = False
                return False
            # Queue and id list are updated under the same state lock. A steer
            # can only be acknowledged after both entries have been created.
            # Legacy/in-process steering callers may have placed plain strings
            # in steer_queue without ACP message ids. Normal ACP requests always
            # provide matching IDs; fill missing IDs for compatibility only.
            while len(session.steer_pending_ids) < len(session.steer_queue):
                session.steer_pending_ids.append(f"legacy-{len(session.steer_pending_ids)}")
            if len(session.steer_pending_ids) != len(session.steer_queue):
                raise RuntimeError("steering receipt ledger out of sync")
            steers = list(zip(session.steer_pending_ids, session.steer_queue))
            if partial_assistant:
                append_message(session, {"role": "assistant", "content": partial_assistant})
            for message_id, steer_text in steers:
                append_message(session, {"role": "user", "content": steer_text})
                append_lifecycle(session, "turn/steer", {
                    "chars": len(steer_text), "message_id": message_id,
                })
                session.steer_receipts[message_id] = (steer_text, "injected")
                # Mid-turn consumption is only PROVISIONALLY injected: a clean
                # turn end confirms it, a cancelled turn rolls it back to
                # "cleared" (official steer-loss-recovery semantics).
                session.turn_injected_steer_ids.append(message_id)
            session.steer_queue.clear()
            session.steer_pending_ids.clear()
            session.steer_event.clear()
        # Trigger best-effort session persistence before the injection event.
        # The session store deliberately tolerates disk failures, so this event
        # means "in provider-facing transcript", not "model executed it" or
        # "disk write verified". Crew retains ownership of its durable queue.
        self._persist_session(session)
        for message_id, steer_text in steers:
            self._apply_reasoning_language_policy(
                session,
                str(getattr(session, "reasoning_language_preference", "auto") or "auto"),
                steer_text,
            )
            self._emit_steer_consumed(session, steer_text, message_id)
        return True

    def _rollback_turn_injected_steers(self, session: Any) -> None:
        """Roll back this turn's provisionally-injected steers on cancellation.

        Official KiroCrew/kiro-cli report a steer consumed only when its turn
        ends cleanly; a cancelled turn hands the steer back to the caller. BYO
        consumes at a mid-turn model boundary, so on a cancelled turn each steer
        marked "injected" this turn is rolled back to "cleared" and a
        steering_cleared info event is emitted, matching that contract instead
        of leaving a false "injected" receipt behind.
        """
        with session.state_lock:
            ids = list(session.turn_injected_steer_ids)
            session.turn_injected_steer_ids.clear()
            rolled: list[str] = []
            for message_id in ids:
                prior = session.steer_receipts.get(message_id)
                if prior is not None and prior[1] == "injected":
                    session.steer_receipts[message_id] = (prior[0], "cleared")
                    rolled.append(message_id)
        for message_id in rolled:
            self._emit_steer_info(session, "steering_cleared", message_id=message_id)

    def _confirm_turn_injected_steers(self, session: Any) -> None:
        """Confirm this turn's injected steers after a clean turn end."""
        with session.state_lock:
            session.turn_injected_steer_ids.clear()

    @staticmethod
    def _browser_action_requested(text: str) -> bool:
        return crew_browser_action_requested(text)

    @classmethod
    def _prepare_crew_browser_for_turn(cls, session: Any, text: str) -> None:
        """Avoid losing Crew browser when a cold MCP catalogue is still loading.

        No Crew settings file, local browser launcher or credential is accessed.
        We only wait for the caller-projected MCP catalogue; an absent or denied
        browser stays absent, and Crew's own browser tool decides fallback.
        """
        mcp = getattr(session, "mcp", None)
        if (mcp is None or not cls._browser_action_requested(text) or not mcp.servers):
            return
        # A cached Crew schema is not proof that this session's broker is ready.
        # Join the authenticated core handshake for explicit browser actions.
        if hasattr(mcp, "wait_for_live_crew_catalog") and "kirocrew-core" in mcp.servers:
            mcp.wait_for_live_crew_catalog("kirocrew-core", timeout=8.0)
        elif not mcp.catalog_ready():
            # Older/alternate bridges only expose the classic catalogue barrier.
            mcp.wait_for_catalog(timeout=8.0)

    @staticmethod
    def _native_visible_to_agent(session: Any, available: set[str]) -> set[str]:
        # Crew only claims full Saved Agent loading for backends that explicitly
        # support it. Until such a projection arrives, a member control mount
        # is NOT authority to grant this harness's native shell/write/read.
        mcp = getattr(session, "mcp", None)
        if (mcp is not None and hasattr(mcp, "has_crew_member_mount")
                and mcp.has_crew_member_mount() and not session.agent.active):
            return set()
        return session.agent.allowed_native_names(available)

    @staticmethod
    def _mcp_visible_to_agent(session: Any, binding: Any) -> bool:
        """Combine saved-spec limits with Crew's separate member-session mount.

        The exception applies ONLY to the two Crew member control servers
        session-scoped by Crew. Host permissions, live-binding verification,
        explicit excludedTools and the Crew server's own identity verification
        are still mandatory; an ordinary third-party MCP gains nothing.
        """
        names = dict(server_name=binding.server_name, tool_name=binding.tool_name,
                     public_name=binding.public_name)
        if session.agent.excludes_mcp(**names):
            return False
        mcp = session.mcp
        # With no confirmed full Saved Agent projection, restrict this member
        # thread to its own host-mounted control/panel tools only. Do not expose
        # a foreign MCP merely because the default Agent is 'unrestricted'.
        if (mcp is not None and hasattr(mcp, "has_crew_member_mount")
                and mcp.has_crew_member_mount() and not session.agent.active):
            return bool(hasattr(mcp, "is_crew_member_mount")
                        and mcp.is_crew_member_mount(binding.server_name))
        if session.agent.allows_mcp(**names):
            return True
        return bool(mcp is not None and hasattr(mcp, "is_crew_member_mount")
                    and mcp.is_crew_member_mount(binding.server_name))

    def _sync_host_capabilities(self, session: Any) -> None:
        """Reconcile current Crew-supplied session tools before a model turn.

        ACP v1 supplies MCP grants on session/new/load/resume, not through a
        standard live settings subscription. A bounded periodic tools/list on
        an *already authenticated* Crew broker detects schema changes without
        reading dashboard preferences or inventing new permissions.
        """
        mcp = getattr(session, "mcp", None)
        if mcp is None or not hasattr(mcp, "host_capability_snapshot"):
            return
        # Member control servers are session-specific; the first model turn
        # must not accidentally see only a stale/empty cached catalogue.
        if hasattr(mcp, "is_crew_member_mount") and hasattr(mcp, "wait_for_live_crew_catalog"):
            for server_name in ("kirocrew-dashboard", "kirocrew-panel"):
                if mcp.is_crew_member_mount(server_name):
                    mcp.wait_for_live_crew_catalog(server_name, timeout=8.0)
        if hasattr(mcp, "refresh_host_capabilities"):
            mcp.refresh_host_capabilities()
        allowed = lambda binding: self._mcp_visible_to_agent(session, binding)
        snapshot = mcp.host_capability_snapshot(allowed=allowed)
        available = set(snapshot["available_tools"])
        with session.state_lock:
            session.loaded_tool_names[:] = [
                name for name in session.loaded_tool_names if name in available
            ]
            fingerprint = hashlib.sha256(
                json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()[:16]
            if fingerprint == getattr(session, "host_capability_fingerprint", ""):
                return
            session.host_capability_fingerprint = fingerprint
        # Only safe names/statuses are traced. This is not a second settings DB
        # and never caches permissions or client-projected broker tokens.
        self._runtime_event(
            "crew.capabilities.changed", session,
            data={
                "fingerprint": fingerprint,
                "servers": snapshot["servers"],
                "verified_crew_servers": snapshot["verified_crew_servers"],
                "pending_crew_servers": snapshot["pending_crew_servers"],
                "failed_servers": snapshot["failed_servers"],
                "tool_count": len(available),
                "tool_names": sorted(available)[:200],  # names only; never tokens or schemas
                "tool_names_truncated": len(available) > 200,
                "crew_browser": snapshot["crew_browser"],
            },
        )

    @staticmethod
    def _active_tools_for_turn(session: Any) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        if session.native is not None:
            available = {spec.name for spec in session.native.specs()}
            visible = TurnRunnerMixin._native_visible_to_agent(session, available)
            if isinstance(session.native, NativeToolRuntime):
                tools.extend(session.native.tool_specs(
                    visible, ui_language=session_display_locale(session)
                ))
            else:
                # Preserve alternate native runtime bridges used by Crew/tests.
                tools.extend(session.native.tool_specs(visible))
        if session.mcp is not None and session.mcp.servers and (session.agent.allows_any_mcp() or
                (hasattr(session.mcp, "has_crew_member_mount") and session.mcp.has_crew_member_mount())):
            visible_loaded = [
                name
                for name in session.loaded_tool_names
                if (binding := session.mcp.bindings.get(name)) is not None
                and (not hasattr(session.mcp, "is_live_binding") or session.mcp.is_live_binding(name))
                and TurnRunnerMixin._mcp_visible_to_agent(session, binding)
            ]
            # Crew's first-party browser is a stable, session-scoped MCP
            # capability. Expose it directly when the host has advertised it,
            # rather than depending on a model to guess a tool-search query.
            # The allowlist is checked both here and again on actual invocation.
            browser_names = session.mcp.crew_browser_tool_names(
                allowed=lambda binding: TurnRunnerMixin._mcp_visible_to_agent(session, binding)
            ) if hasattr(session.mcp, "crew_browser_tool_names") else ()
            skill_names = session.mcp.crew_skill_tool_names(
                allowed=lambda binding: TurnRunnerMixin._mcp_visible_to_agent(session, binding)
            ) if hasattr(session.mcp, "crew_skill_tool_names") else ()
            visible_loaded = list(dict.fromkeys([*visible_loaded, *browser_names, *skill_names, *crew_artifact_tool_names_for_turn(session, TurnRunnerMixin._mcp_visible_to_agent)]))
            # Older external tool bridges implement tool_specs(names) without
            # the new verification flag. Only our own manager owns that flag;
            # do not break ACP compatibility tests or alternate bridges.
            if hasattr(session.mcp, "is_live_binding"):
                visible_specs = session.mcp.tool_specs(visible_loaded, verified_only=True)
            else:
                visible_specs = session.mcp.tool_specs(visible_loaded)
            tools.extend([session.mcp.deferred_tool_search_schema(), *visible_specs])
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
        search_options = {
            "limit": arguments.get("limit", 8),
            "server": str(arguments.get("server") or ""),
            "timeout": 20.0,
            "allowed": lambda binding: self._mcp_visible_to_agent(session, binding),
        }
        if hasattr(session.mcp, "is_live_binding"):
            search_options["verified_only"] = True
        matches, waited_ms = session.mcp.search_tools(
            str(arguments.get("query") or ""), **search_options,
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
    def _policy_thinking_mode(model: Any, session: Any, reasoning_intent: str) -> str:
        """Compatibility façade for the model-scoped review thinking policy."""
        del session
        return review_thinking_mode(model, reasoning_intent)

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
            review_thinking = review_thinking_mode(review_model, post_stage.reasoning_intent)
            review_effort = review_effort_level(
                review_model, session.effort, session.effort_user_set, post_stage.reasoning_intent
            )

        review_messages = build_review_messages(
            agent_prompt=session.agent.prompt,
            runtime_mode_context=runtime_mode_context,
            language_instruction=str(getattr(session, "language_instruction", "") or ""),
            role=post_stage.role,
            transcript=transcript,
            draft=final_text,
        )
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
                    "error": type(exc).__name__, "error_category": str(getattr(exc, "category", "provider") or "provider"),
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
                    crew_browser_available=any(
                        str((tool.get("function") or {}).get("name") or "")
                        == "mcp__kirocrew-core__browser"
                        for tool in tools
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
            self._rollback_turn_injected_steers(session)
            return "cancelled"
        if self._consume_queued_steers_for_turn(session, interruption.partial_text):
            return "continue"
        # should_interrupt fires on steer OR cancel, so a steer-triggered
        # interruption can reach here with cancel arriving just after the first
        # check (consume then refuses and returns False). Re-read cancel once
        # more, else the turn is mislabelled "interrupted" and session/cancel
        # never gets its terminal response.
        if session.cancel_event.is_set():
            self._recover_current_turn(
                session, history_start, reason="provider_cancelled",
                preserve_side_effects=preserve_side_effects,
                partial_text=interruption.partial_text,
            )
            self._rollback_turn_injected_steers(session)
            return "cancelled"
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
            state.add_usage(child_usage)
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
            return self._finish_interrupted_provider_round(
                req_id, session, state, exc, history_start=history_start,
                t_prompt=t_prompt, routing_decision=routing_decision,
            ), None
        except ProviderError as exc:
            state.upstream_ms += (time.perf_counter() - t_upstream) * 1000.0
            # A cancellation can close the socket while an adapter is reading
            # its body. If that close races with its transport normalization,
            # the exception can be ProviderTransportError, not Interrupted.
            # Control requests always take precedence over retry/fallback.
            if self._interrupt_requested(session):
                return self._finish_interrupted_provider_round(
                    req_id, session, state, ProviderInterrupted(""),
                    history_start=history_start, t_prompt=t_prompt,
                    routing_decision=routing_decision,
                ), None
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
                fallback_data = {
                    "from_provider": failed_provider.id,
                    "from_model": failed_model.model_id,
                    "to_provider": state.provider.id,
                    "to_model": state.model.model_id,
                    "error": type(exc).__name__,
                    "error_category": error_category,
                }
                append_lifecycle(session, "model/fallback", fallback_data)
                self._runtime_event("model.route.fallback", session, data=fallback_data)
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
        state.add_usage(turn.usage)

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
        if self._consume_queued_steers_for_turn(session, close_if_empty=True):
            self._advance_turn_phase(
                session, state.turn_state, TurnPhase.CONTINUE,
                reason="queued_steer_after_final_candidate",
            )
            return _LoopDirective.QUEUED_STEER_AFTER_FINAL
        state.final_text = turn.text or "".join(state.round_text_parts)
        return _LoopDirective.FINAL

    def _abort_tool_batch_checkpoint_failure(
        self, req_id: Any, session: Any, state: _ProviderLoopState,
        *, history_start: int, t_prompt: float, routing_decision: Any,
        reason: str, message: str, preserve_side_effects: bool,
    ) -> _LoopDirective:
        """Fail closed on either side of the durable tool-dispatch fence."""
        self.emit_runtime_error(session.session_id, message)
        self._recover_current_turn(
            session, history_start, reason=reason,
            preserve_side_effects=preserve_side_effects,
        )
        self._advance_turn_phase(
            session, state.turn_state, TurnPhase.COMPLETE, reason=reason,
        )
        self._record_provider_turn_telemetry(
            session, state, outcome="error", t_prompt=t_prompt,
            routing_decision=routing_decision, error_category="durable_checkpoint",
        )
        self.result(req_id, self._provider_prompt_result(state, "error"))
        return _LoopDirective.RETURN

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

        # Opaque and deterministic within one durable turn. A repeated tool
        # dispatch at the same logical step must not get a fresh admission ID.
        # Never persist tool arguments, names, or provider-supplied call IDs in
        # the run ledger: only this internal digest and aggregate metadata.
        batch_id = hashlib.sha256(
            f"{getattr(session, 'active_run_id', '')}:"
            f"{getattr(session, 'active_run_step_id', '')}:"
            f"{state.step}".encode("utf-8")
        ).hexdigest()
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
                    "tool_count": len(turn.tool_calls),
                    "batch_id": batch_id,
                    "effects": sorted(batch_effects),
                    "side_effect_risk": state.side_effect_risk_seen,
                },
                critical=True,
            )
        except DurableCheckpointError:
            # No tool may execute without a durable admission receipt.
            return self._abort_tool_batch_checkpoint_failure(
                req_id, session, state,
                history_start=history_start, t_prompt=t_prompt,
                routing_decision=routing_decision,
                reason="checkpoint_failed_pre_tools",
                message=("durable checkpoint failed before tool execution; "
                         "stopping to avoid an unrecoverable side effect"),
                preserve_side_effects=state.side_effect_risk_seen,
            )

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
        if batch_outcome.cancelled:
            # A cancellation may interrupt a multi-tool batch after one tool
            # has already changed the workspace. Do not seal the durable
            # receipt as completed: the admitted batch must remain unresolved
            # so restart reconciliation cannot mistake partial work for a
            # fully executed batch.
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
        try:
            self._checkpoint_durable_run(
                session, "tool_batch_completed",
                data={
                    "step": state.step,
                    "tool_calls_total": state.tool_calls_total,
                    "batch_id": batch_id,
                    "effects": sorted(batch_effects),
                    "side_effect_risk": state.side_effect_risk_seen,
                },
                critical=True,
            )
        except DurableCheckpointError:
            # Tools may have run: retain admission for manual effect review.
            return self._abort_tool_batch_checkpoint_failure(
                req_id, session, state,
                history_start=history_start, t_prompt=t_prompt,
                routing_decision=routing_decision,
                reason="checkpoint_failed_post_tools",
                message=("durable tool completion receipt failed; external effect "
                         "may already have happened; verify before retrying"),
                preserve_side_effects=True,
            )
        if batch_outcome.stop_requested:
            return self._handle_tool_guard_stop(session, state, round_ctx)

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

    def _handle_tool_guard_stop(
        self, session: Any, state: _ProviderLoopState,
        round_ctx: _ProviderRoundContext,
    ) -> _LoopDirective:
        """Finish a stopped tool batch without scheduling any further tools."""
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
                state.add_usage(post_outcome.usage)
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
                    self._finish_provider_loop_cancelled_before_invoke(
                        req_id, session, state, history_start=history_start,
                        t_prompt=t_prompt, routing_decision=routing_decision,
                    )
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
            self._finish_provider_loop_provider_error(
                req_id, session_id, session, state, exc,
                history_start=history_start, t_prompt=t_prompt,
                t_ready=t_ready, routing_decision=routing_decision,
            )
            return
        except Exception as exc:
            self._finish_provider_loop_internal_error(
                req_id, session_id, session, state, exc,
                history_start=history_start, t_prompt=t_prompt,
                routing_decision=routing_decision,
            )
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

    def _rewind_durable_prompt(
        self, req_id: Any, session_id: str, session: Any, config: Any,
        resume_run_id: str, recovery_strategy: str,
    ) -> bool:
        """Revalidate and rewind only after the session turn lock was acquired."""
        record = self._durable_run_store().get(resume_run_id)
        recovery = RecoveryPolicy.durable_run(record, strategy=recovery_strategy or "resume")
        if (
            not recovery.safe or record is None
            or str(record.get("session_id") or "") != session_id
        ):
            self.error(req_id, -32010, f"durable recovery is no longer safe: {recovery.reason}")
            return False
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        # A pooled Crew Worker can be rebound between turns. Resuming an old
        # Run under a different Work Ledger task would replay its original
        # prompt under the new task's identity. Check before rewinding any
        # history; a legacy Run without a link cannot prove a new binding.
        try:
            run_link = validate_work_link(metadata.get("host_work_link", {}))
            active_link = validate_work_link(getattr(session, "host_work_link", {}) or {})
        except (ValueError, TypeError):
            self.error(req_id, -32010, "durable recovery blocked: invalid Crew work association")
            return False
        if run_link != active_link:
            self.error(req_id, -32010, "durable recovery blocked: Crew work assignment changed")
            return False
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
        return True

    def _execute_prompt_request_locked(
        self, req_id: Any, params: dict[str, Any], session: Any, session_id: str,
        t_prompt: float, resume_run_id: str, recovery_strategy: str,
    ) -> None:
        """Execute one admitted turn while the caller owns both turn locks."""
        history_start = len(session.messages)
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
        if resume_run_id and not self._rewind_durable_prompt(
            req_id, session_id, session, config, resume_run_id, recovery_strategy,
        ):
            return

        route = self._prepare_prompt_routing(
            req_id, session_id, session, config, text, has_image,
        )
        if route is None:
            return
        provider, model = route.provider, route.model
        selection_truth = route.selection_truth
        mode_fallbacks = route.mode_fallbacks
        post_stage = route.post_stage
        primary_effort, primary_thinking = route.primary_effort, route.primary_thinking
        reasoning_profile, routing_signals = route.reasoning_profile, route.routing_signals
        runtime_mode_context, relay = extend_crew_context(route.runtime_mode_context, session, config, route.decision, text, self._mcp_visible_to_agent), route.relay
        execution_plan, decision = route.execution_plan, route.decision
        review_target = route.post_stage
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
        # transcript is later transactionally truncated. If the anchor
        # cannot be durably recorded, no model or tool may run on this turn.
        if self._persist_session(session) is False:
            self.emit_runtime_error(
                session_id, "session checkpoint could not be saved; no model or tools were invoked",
            )
            self.result(req_id, {"stopReason": "end_turn"})
            return

        # Materialize the logical agent graph only after all prompt-level
        # early exits have passed. This keeps AgentTask lifecycle aligned
        # with a real turn transaction instead of leaving orphan plans for
        # /compact or unsupported-input requests.
        if execution_plan is not None:
            self._begin_agent_plan(session, execution_plan)
        started_run_id = self._begin_durable_run(
            session, execution_plan, selection=str(selection_truth.get("id") or session.model),
            provider_id=provider.id, model_id=model.model_id, history_start=history_start,
            user_event_seq=user_event_seq, resume_run_id=resume_run_id,
            recovery_strategy=recovery_strategy,
        )
        if not started_run_id:
            # A failed resume admission or partial ledger write must not
            # silently turn into an untracked, possibly mutating ACP turn.
            self._finalize_agent_plan(
                session, "error", provider_id=provider.id, model_id=model.model_id,
            )
            self.emit_runtime_error(
                session_id, "durable execution could not be started; no model or tools were invoked",
            )
            self.result(req_id, {"stopReason": "end_turn"})
            return

        # A shared, Mode-neutral projection of the real decision. Emit only
        # after durable admission so run_id/turn_id are authoritative.
        if decision is not None:
            self._runtime_event(
                "model.route.selected", session,
                data=selected_route_event_data(
                    requested_model=session.model,
                    selection=selection_truth,
                    decision=decision,
                    execution_plan=execution_plan,
                    review_target=review_target,
                    provider=provider,
                    model=model,
                ),
                content={"prompt": text},
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
        # A stable per-session advisory lock excludes concurrent ACP prompt
        # execution in other processes; storage revisions additionally fence
        # delayed/stale snapshots after a turn has completed.
        turn_guard = _file_transaction_lock(
            self._session_path(session_id).with_suffix(".turn"), timeout=0.01
        )
        acquired = False
        try:
            turn_guard.__enter__()
            acquired = True
            self._assert_session_current(session)
        except (TimeoutError, OSError, RuntimeError) as exc:
            if acquired:
                turn_guard.__exit__(None, None, None)
            session.turn_lock.release()
            self.error(req_id, -32002, f"session cannot start a turn: {exc}")
            return

        from .work_link_control import admit_prompt_work_link
        if not admit_prompt_work_link(self, req_id, params, session, turn_guard):
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

        resume_run_id = str(params.get("_kiro_resume_run_id") or "")
        recovery_strategy = str(params.get("_kiro_recovery_strategy") or "")
        try:
            self._execute_prompt_request_locked(
                req_id, params, session, session_id, t_prompt, resume_run_id, recovery_strategy,
            )
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
            try:
                turn_guard.__exit__(None, None, None)
            finally:
                session.turn_lock.release()
            terminal_payload = self._take_prompt_response_buffer(req_id)
            if terminal_payload is not None:
                self.send(terminal_payload)
