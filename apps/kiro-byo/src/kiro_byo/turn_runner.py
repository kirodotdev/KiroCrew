"""One-turn execution loop for the Kiro BYO ACP harness.

The transport/session server owns JSON-RPC framing and lifecycle.  This mixin owns
provider inference, streaming, tool-search/tool-call iteration and per-turn
telemetry.  Keeping this loop out of ``acp_server.py`` prevents session protocol
changes from sharing a 700-line method with model/tool execution.
"""
from __future__ import annotations

import json
import time
from typing import Any

from .policy_packs import runtime_policy_context
from .config import ConfigError
from .errors import ClientRequestError
from .execution_guard import ExecutionGuard
from .mcp_client import DEFERRED_TOOL_ACTIVE_LIMIT, DEFERRED_TOOL_SEARCH_NAME
from .native_tools import NativeInvocation, NativeToolError, PermissionDecision, ToolValidationError
from .protocol import MODEL_ECHO, MODEL_UPPER
from .mode_router import ReviewTarget, build_execution_plan, model_tier, route_model
from .providers import ProviderError, ProviderInterrupted, chat_completion_turn
from .reasoning_control import (
    ReasoningInvocation,
    resolve_effort_level,
    resolve_thinking_mode,
    thinking_auto_suggestion,
)
from .sandbox import SandboxError
from .telemetry import TurnTelemetry, now_timestamp

# Native capabilities that only read or look things up.
_RELAY_GATHERING_CAPABILITIES = frozenset({"fs_read", "web_fetch", "web_search"})


class TurnRunnerMixin:
    def _runtime_event(
        self,
        event: str,
        session: Any,
        *,
        data: dict[str, Any] | None = None,
        content: dict[str, Any] | None = None,
    ) -> None:
        """Best-effort structured runtime trace; never break the agent loop."""
        store = getattr(self, "runtime_trace", None)
        if store is None:
            return
        try:
            store.record(
                event,
                session_id=str(getattr(session, "session_id", "") or ""),
                data=data,
                content=content,
            )
        except OSError as exc:
            self.trace(f"runtime trace write skipped: {type(exc).__name__}")

    @staticmethod
    def _tool_interrupt_reason(session: Any) -> str:
        """Map ACP control-plane state to one native-tool interruption reason."""
        if session.cancel_event.is_set():
            return "cancelled"
        if session.steer_event.is_set():
            return "steered"
        return ""

    @staticmethod
    def _rollback_current_turn(session: Any, history_start: int) -> None:
        with session.state_lock:
            del session.messages[history_start:]

    def _consume_queued_steers_for_turn(
        self, session: Any, partial_assistant: str = ""
    ) -> bool:
        steers = self._consume_steers(session)
        if not steers:
            return False
        with session.state_lock:
            if partial_assistant:
                session.messages.append({"role": "assistant", "content": partial_assistant})
            for steer_text in steers:
                session.messages.append({"role": "user", "content": steer_text})
        for steer_text in steers:
            self._emit_steer_consumed(session, steer_text)
        return True

    @staticmethod
    def _active_tools_for_turn(session: Any) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        if session.native is not None:
            available = {spec.name for spec in session.native.specs()}
            visible = session.agent.allowed_native_names(available)
            tools.extend(session.native.openai_tools(visible))
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
                    *session.mcp.openai_tools(visible_loaded),
                ]
            )
        return tools

    @staticmethod
    def _append_tool_result(session: Any, call_id: str, result_text: str) -> None:
        with session.state_lock:
            session.messages.append(
                {"role": "tool", "tool_call_id": call_id, "content": result_text}
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
                result_text = "Permission denied by the Kiro Crew host."
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
            result_text = "Permission denied by the Kiro Crew host."
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
            result_text = "Permission denied by the Kiro Crew host."
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

    @staticmethod
    def _relay_teammate(decision: Any, provider: Any, model: Any) -> Any:
        """Pick the Glitter fast teammate: same provider, fast tier, eligible now.

        Same provider only, so tool-call ids and message shapes stay valid when
        the lead resumes. None when no such model exists (single-model setups,
        mixed providers): the lead then works alone.
        """
        for row in getattr(decision, "candidates", ()) or ():
            if row.provider.id != provider.id or row.model.model_id == model.model_id:
                continue
            if model_tier(row.model) != "fast":
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

        Glitter review never replays provider-private continuation blobs or raw
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

    @classmethod
    def _resolve_invocation_reasoning(
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
    ) -> None:
        # Resolve the primary invocation from two independent controls. Explicit
        # user choices are hard locks; AUTO delegates to the policy pack.
        if reasoning_profile is not None:
            turn_thinking, turn_effort = self._resolve_invocation_reasoning(
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
        # Glitter relay collaboration (``relay`` = (provider, model, effort) of a
        # fast same-provider teammate, or None). The lead model plans, makes
        # every change and writes the final answer; the teammate only continues
        # rounds whose previous tool calls were pure information gathering.
        relay_next_role = "lead"
        relay_rounds = 0
        first_visible_at: float | None = None
        last_visible_at: float | None = None
        previous_visible_at: float | None = None
        upstream_ms = 0.0
        tool_ms = 0.0
        mcp_wait_ms = 0.0
        tool_calls_total = 0
        provider_rounds_total = 0
        execution_guard_halted = False
        execution_guard_reason = ""
        pending_replan_notice = ""
        execution_guard = ExecutionGuard()
        native_tool_calls_total = 0
        tool_search_calls_total = 0
        tool_search_loaded_total = 0
        tool_schema_bytes = 0
        active_tools_count = 0
        stream_chunks = 0
        stream_chars = 0
        max_chunk_chars = 0
        max_gap_ms = 0.0
        # Text streamed during the CURRENT provider round. It stays internal
        # until we know whether this is a tool-calling draft or the final answer.
        round_text_parts: list[str] = []
        round_reasoning_emitted = False
        input_tokens = 0
        output_tokens = 0
        cached_tokens = 0
        cache_write_tokens = 0
        reasoning_tokens = 0
        cache_usage_reported = False
        cache_write_usage_reported = False
        reasoning_usage_reported = False
        served_model_id = model.model_id
        served_provider_id = provider.id
        mode_fallbacks = list(mode_fallbacks or [])
        telemetry_recorded = False
        final_text = ""
        observed_thinking_modes: set[str] = set()
        observed_efforts: set[str] = set()

        def _reasoning_summary(values: set[str]) -> str:
            if not values:
                return "default"
            if len(values) == 1:
                return next(iter(values))
            return "mixed"

        def _prompt_result(stop_reason: str) -> dict[str, Any]:
            # Crew 0.9 reads these exact PromptResponse fields as per-turn
            # billing/token usage. Cache read/write are separate dimensions:
            # Anthropic/MiniMax expose explicit cache creation while many other
            # providers only expose cache reads. Unknown write usage remains 0
            # on the ACP wire, while diagnostics retain an explicit reported flag.
            return {
                "stopReason": stop_reason,
                "inputTokens": int(input_tokens),
                "outputTokens": int(output_tokens),
                "cachedReadTokens": int(cached_tokens),
                "cachedWriteTokens": int(cache_write_tokens),
                # Crew ignores this extra field today, while diagnostics and
                # forward-compatible consumers can retain real reasoning usage.
                "reasoningTokens": int(reasoning_tokens),
            }

        def _record_turn_telemetry(outcome: str, error_kind: str = "") -> None:
            nonlocal telemetry_recorded
            if telemetry_recorded:
                return
            telemetry_recorded = True
            now = time.perf_counter()
            first_ms = (
                (first_visible_at - t_prompt) * 1000.0
                if first_visible_at is not None
                else -1.0
            )
            try:
                self.telemetry.record(
                    TurnTelemetry(
                        timestamp=now_timestamp(),
                        session_id=session.session_id,
                        outcome=outcome,
                        requested_model=session.model,
                        provider_id=served_provider_id,
                        served_model=served_model_id,
                        effort=_reasoning_summary(observed_efforts),
                        thinking_mode=_reasoning_summary(observed_thinking_modes),
                        total_ms=(now - t_prompt) * 1000.0,
                        first_visible_ms=first_ms,
                        upstream_ms=upstream_ms,
                        tool_ms=tool_ms,
                        mcp_wait_ms=mcp_wait_ms,
                        tool_calls=tool_calls_total,
                        native_calls=native_tool_calls_total,
                        tool_search_calls=tool_search_calls_total,
                        active_tools=active_tools_count,
                        tool_schema_bytes=tool_schema_bytes,
                        stream_chunks=stream_chunks,
                        stream_chars=stream_chars,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cached_tokens=cached_tokens,
                        cache_write_tokens=cache_write_tokens,
                        reasoning_tokens=reasoning_tokens,
                        cache_usage_reported=cache_usage_reported,
                        cache_write_usage_reported=cache_write_usage_reported,
                        reasoning_usage_reported=reasoning_usage_reported,
                        error_kind=error_kind,
                    )
                )
            except OSError as exc:
                self.trace(f"telemetry write skipped: {type(exc).__name__}")
            self._runtime_event(
                "turn.completed",
                session,
                data={
                    "outcome": outcome,
                    "error_kind": error_kind,
                    "provider_id": served_provider_id,
                    "requested_model": session.model,
                    "served_model": served_model_id,
                    "tool_calls": tool_calls_total,
                    "provider_rounds": provider_rounds_total,
                    "execution_guard_tool_calls": execution_guard.tool_calls_seen,
                    "execution_guard_halted": execution_guard_halted,
                    "execution_guard_reason": execution_guard_reason,
                    "total_ms": (now - t_prompt) * 1000.0,
                },
            )

        def _emit_timed(chunk: str) -> None:
            nonlocal stream_chunks, stream_chars, max_chunk_chars
            if not chunk:
                return
            # Provider content is not automatically "thinking". A tool-calling
            # round may later prove to be an internal draft, while a no-tool
            # round is the final answer. Buffer it until the round outcome is
            # known so Crew never renders the same text once as a quote/thinking
            # block and again as the final answer.
            stream_chunks += 1
            stream_chars += len(chunk)
            max_chunk_chars = max(max_chunk_chars, len(chunk))
            round_text_parts.append(chunk)
            self._runtime_event(
                "provider.stream.delta",
                session,
                data={"step": step, "chars": len(chunk)},
                content={"text": chunk},
            )

        def _mark_visible() -> None:
            nonlocal first_visible_at, last_visible_at, previous_visible_at, max_gap_ms
            now = time.perf_counter()
            if first_visible_at is None:
                first_visible_at = now
            if previous_visible_at is not None:
                max_gap_ms = max(max_gap_ms, (now - previous_visible_at) * 1000.0)
            previous_visible_at = now
            last_visible_at = now

        def _emit_reasoning_timed(chunk: str) -> None:
            nonlocal round_reasoning_emitted
            if not chunk:
                return
            round_reasoning_emitted = True
            _mark_visible()
            # Only provider-authentic reasoning/thinking enters Crew's folded
            # thinking channel. Normal answer content stays buffered until the
            # round is known to be the final no-tool answer.
            self.emit_text(session_id, chunk, is_thinking=True)
            self._runtime_event(
                "provider.stream.reasoning_delta",
                session,
                data={"step": step, "chars": len(chunk)},
                content={"text": chunk},
            )

        def _observe_execution_guard(
            round_role: str, call: Any, arguments: Any, result_text: str
        ) -> Any:
            nonlocal pending_replan_notice, execution_guard_reason
            decision = execution_guard.observe(
                f"{round_role}:{getattr(call, 'name', '')}", arguments, result_text
            )
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
            return decision

        try:
            # MCP process warm-up continues in the background, but ordinary
            # text turns never join the full 100+ tool catalogue. This is the
            # provider-neutral equivalent of OpenAI/Anthropic deferred tool
            # loading and keeps the initial prompt prefix small and stable.
            step = 0
            while True:
                step += 1
                # Reset the per-round content buffer. It is not exposed until
                # the round is known to be the final no-tool answer.
                round_text_parts.clear()
                round_reasoning_emitted = False
                if relay is not None and relay_next_role == "worker":
                    round_role = "worker"
                    round_provider, round_model, legacy_round_effort = relay
                    relay_rounds += 1
                    round_stage = "gathering"
                    if reasoning_profile is not None:
                        round_thinking, round_effort = self._resolve_invocation_reasoning(
                            round_model, session, reasoning_profile, routing_signals,
                            role="worker", stage=round_stage,
                        )
                    else:
                        round_effort = legacy_round_effort
                        round_thinking = self._policy_thinking_mode(round_model, session, "economy")
                else:
                    round_role = "lead"
                    round_provider, round_model = provider, model
                    round_stage = (
                        "planning" if step == 1
                        else "synthesis" if relay_rounds > 0
                        else "analysis"
                    )
                    if reasoning_profile is not None:
                        round_thinking, round_effort = self._resolve_invocation_reasoning(
                            round_model, session, reasoning_profile, routing_signals,
                            role="lead", stage=round_stage,
                        )
                    else:
                        round_effort = turn_effort
                        round_thinking = turn_thinking
                observed_thinking_modes.add(round_thinking or "default")
                observed_efforts.add(round_effort or "default")
                if session.cancel_event.is_set():
                    self._rollback_current_turn(session, history_start)
                    _record_turn_telemetry("cancelled")
                    self.result(req_id, _prompt_result("cancelled"))
                    return

                tools = self._active_tools_for_turn(session)
                if round_role == "worker":
                    # The teammate may only gather information: it never sees
                    # (and so can never call) a tool that changes anything.
                    tools = [
                        tool for tool in tools
                        if self._relay_gathering_tool(
                            session, str(((tool or {}).get("function") or {}).get("name") or "")
                        )
                    ]
                active_tools_count = len(tools)
                tool_schema_bytes = (
                    len(
                        json.dumps(
                            tools, ensure_ascii=False, separators=(",", ":")
                        ).encode("utf-8")
                    )
                    if tools
                    else 0
                )
                with session.state_lock:
                    provider_messages = [
                        *([{"role": "system", "content": session.agent.prompt}] if session.agent.prompt else []),
                        self._runtime_system_message(
                            round_provider,
                            round_model,
                            round_effort,
                            thinking_mode=round_thinking,
                            deferred_tool_search=bool(
                                session.mcp is not None and session.mcp.servers
                            ),
                            mcp_server_names=tuple(
                                session.mcp.servers.keys()
                                if session.mcp is not None
                                else ()
                            ),
                            native_tool_names=tuple(
                                spec.name for spec in session.native.specs()
                            ) if session.native is not None else (),
                            mode_context=runtime_mode_context,
                        ),
                        *self._messages_for_provider(list(session.messages), round_provider, round_model),
                    ]
                    if round_role == "worker":
                        provider_messages.insert(
                            len(provider_messages) - len(session.messages),
                            {
                                "role": "system",
                                "content": (
                                    "Kiro BYO Glitter relay: you are the fast teammate of the lead model. "
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
                            len(provider_messages) - len(session.messages),
                            {"role": "system", "content": pending_replan_notice},
                        )
                        pending_replan_notice = ""
                provider_rounds_total += 1
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
                    },
                    content={"messages": provider_messages, "tools": tools},
                )
                t_upstream = time.perf_counter()
                try:
                    turn = chat_completion_turn(
                        round_provider,
                        round_model.model_id,
                        provider_messages,
                        _emit_timed,
                        on_reasoning=_emit_reasoning_timed,
                        reasoning_effort=self._reasoning_wire_value(round_model, round_thinking, round_effort) or None,
                        thinking_mode=round_thinking,
                        model_spec=round_model,
                        cache_key=session.session_id if round_role == "lead" else f"{session.session_id}:relay",
                        tools=tools,
                        should_interrupt=lambda: self._interrupt_requested(session),
                    )
                except ProviderInterrupted as exc:
                    upstream_ms += (time.perf_counter() - t_upstream) * 1000.0
                    if session.cancel_event.is_set():
                        self._rollback_current_turn(session, history_start)
                        _record_turn_telemetry("cancelled")
                        self.result(req_id, _prompt_result("cancelled"))
                        return
                    if self._consume_queued_steers_for_turn(session, exc.partial_text):
                        continue
                    # Interrupted but nothing to consume (no cancel, no queued
                    # steer). Re-looping here would silently spin the agent and
                    # discard exc.partial_text. End the turn gracefully,
                    # keeping any text the provider already produced.
                    if exc.partial_text:
                        with session.state_lock:
                            session.messages.append(
                                {"role": "assistant", "content": exc.partial_text}
                            )
                    self._persist_session(session)
                    _record_turn_telemetry("interrupted")
                    self.result(req_id, _prompt_result("end_turn"))
                    return
                except ProviderError as exc:
                    upstream_ms += (time.perf_counter() - t_upstream) * 1000.0
                    if round_role == "worker":
                        # A teammate failure never fails the turn: the lead
                        # simply takes this round back. Relay stops for the turn.
                        self._runtime_event(
                            "model.relay.worker_failed", session,
                            data={"provider_id": round_provider.id, "model": round_model.model_id, "error": type(exc).__name__},
                        )
                        relay = None
                        relay_next_role = "lead"
                        step -= 1
                        continue
                    # Mode routing may fail over only before the first model has emitted
                    # anything. Switching provider after tool/model state exists
                    # risks replaying provider-specific context and is therefore
                    # deliberately fail-closed.
                    if mode_fallbacks and step == 1 and not round_text_parts and not round_reasoning_emitted and tool_calls_total == 0:
                        failed_provider, failed_model = provider, model
                        provider, model = mode_fallbacks.pop(0)
                        served_provider_id = provider.id
                        served_model_id = model.model_id
                        self._runtime_event(
                            "model.route.fallback", session,
                            data={
                                "from_provider": failed_provider.id,
                                "from_model": failed_model.model_id,
                                "to_provider": provider.id,
                                "to_model": model.model_id,
                                "error": type(exc).__name__,
                            },
                        )
                        step -= 1
                        continue
                    raise
                upstream_ms += (time.perf_counter() - t_upstream) * 1000.0
                if round_role == "lead":
                    served_model_id = turn.served_model or served_model_id
                input_tokens += turn.usage.input_tokens
                output_tokens += turn.usage.output_tokens
                cached_tokens += turn.usage.cached_tokens
                cache_write_tokens += turn.usage.cache_write_tokens
                reasoning_tokens += turn.usage.reasoning_tokens
                cache_usage_reported = cache_usage_reported or turn.usage.cached_tokens_reported
                cache_write_usage_reported = cache_write_usage_reported or turn.usage.cache_write_tokens_reported
                reasoning_usage_reported = reasoning_usage_reported or turn.usage.reasoning_tokens_reported
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
                            {"id": call.call_id, "name": call.name, "arguments": call.arguments_text}
                            for call in turn.tool_calls
                        ],
                        "provider_context": turn.provider_context,
                    },
                )
                # ``input_tokens`` on the latest provider request is the
                # closest provider-native measure of context occupancy. Add
                # this request's completion because it is immediately appended
                # to the transcript. Do not sum prior tool-loop requests here.
                if round_model.context_window:
                    self.emit_usage_update(
                        session.session_id,
                        used=turn.usage.input_tokens + turn.usage.output_tokens,
                        size=round_model.context_window,
                    )

                if not turn.tool_calls:
                    if round_role == "worker":
                        # The teammate finished gathering: its summary goes on
                        # the transcript and the lead takes over to decide and
                        # answer. A worker never writes the final answer.
                        with session.state_lock:
                            session.messages.append(
                                {
                                    "role": "assistant",
                                    "content": "[Glitter relay findings]\n" + (turn.text or "".join(round_text_parts)),
                                }
                            )
                        relay_next_role = "lead"
                        self._runtime_event(
                            "model.relay.handback", session,
                            data={"step": step, "worker_model": round_model.model_id, "relay_rounds": relay_rounds},
                        )
                        continue
                    with session.state_lock:
                        assistant_message: dict[str, Any] = {"role": "assistant", "content": turn.text}
                        if turn.provider_context:
                            assistant_message["_kiro_provider_context"] = self._scoped_provider_context(
                                turn.provider_context, round_provider, round_model
                            )
                        session.messages.append(assistant_message)
                    if self._consume_queued_steers_for_turn(session):
                        # A queued steer turns this into another round; its text
                        # was only streamed as thinking, so nothing leaks.
                        continue
                    # Final answer: this round had no tool calls. Provider text
                    # was buffered above, so Crew receives it exactly once as the
                    # real deliverable. Prefer the provider's authoritative
                    # turn.text; fall back to the streamed buffer.
                    final_text = turn.text or "".join(round_text_parts)
                    break

                tool_calls_total += len(turn.tool_calls)
                assistant_tool_calls: list[dict[str, Any]] = []
                for call in turn.tool_calls:
                    assistant_tool_calls.append(
                        {
                            "id": call.call_id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": call.arguments_text,
                            },
                        }
                    )
                with session.state_lock:
                    assistant_message = {
                        "role": "assistant",
                        "content": turn.text or None,
                        "tool_calls": assistant_tool_calls,
                    }
                    # Teammate rounds never store provider-private continuation
                    # data, so the lead never replays another model's blobs.
                    if turn.provider_context and round_role == "lead":
                        assistant_message["_kiro_provider_context"] = self._scoped_provider_context(
                            turn.provider_context, round_provider, round_model
                        )
                    session.messages.append(assistant_message)

                steered_during_tools = False
                guard_replan_requested = False
                guard_stop_requested = False
                for call in turn.tool_calls:
                    if session.cancel_event.is_set():
                        self._rollback_current_turn(session, history_start)
                        _record_turn_telemetry("cancelled")
                        self.result(req_id, _prompt_result("cancelled"))
                        return

                    # Once the execution guard has decided this batch is no
                    # longer safe/productive, keep the transcript protocol-valid
                    # by returning a result for every already-emitted call while
                    # refusing to start further side effects.
                    if guard_stop_requested:
                        self._append_tool_result(
                            session,
                            call.call_id,
                            "Skipped because the execution guard stopped repeated/unbounded tool work.",
                        )
                        continue
                    if guard_replan_requested:
                        self._append_tool_result(
                            session,
                            call.call_id,
                            "Skipped because the execution guard requested a re-plan before more tool work.",
                        )
                        continue

                    # A steer changes the user's intent for the remainder of
                    # this provider round. Keep the transcript protocol-valid
                    # by supplying tool results for calls the model already
                    # emitted, but do not start new side effects after the steer.
                    if steered_during_tools or session.steer_event.is_set():
                        steered_during_tools = True
                        result_text = (
                            "Skipped before execution because user steering changed the task."
                        )
                        self._append_tool_result(session, call.call_id, result_text)
                        continue
                    arguments = call.arguments()

                    # Client-executed deferred tool search. It only exposes
                    # already-trusted schemas from this session's Crew MCP
                    # projection, so it has no host side effect and does not
                    # need a permission prompt. Real tool execution below is
                    # still gated through session/request_permission.
                    if call.name == DEFERRED_TOOL_SEARCH_NAME:
                        tool_search_calls_total += 1
                        result_text, waited_ms, newly_loaded = self._deferred_tool_search_result(
                            session, arguments
                        )
                        mcp_wait_ms += waited_ms
                        tool_search_loaded_total += newly_loaded
                        self._append_tool_result(session, call.call_id, result_text)
                        decision = _observe_execution_guard(round_role, call, arguments, result_text)
                        if decision.action == "warn":
                            guard_replan_requested = True
                        elif decision.action == "halt":
                            guard_stop_requested = True
                        continue

                    if session.native is not None and session.native.has_tool(call.name):
                        native_tool_calls_total += 1
                        result_text, elapsed_ms, outcome = self._execute_native_tool(
                            session, call, arguments
                        )
                        tool_ms += elapsed_ms
                        if outcome == "cancelled":
                            self._rollback_current_turn(session, history_start)
                            _record_turn_telemetry("cancelled")
                            self.result(req_id, _prompt_result("cancelled"))
                            return
                        self._append_tool_result(session, call.call_id, result_text)
                        decision = _observe_execution_guard(round_role, call, arguments, result_text)
                        if decision.action == "warn":
                            guard_replan_requested = True
                        elif decision.action == "halt":
                            guard_stop_requested = True
                        if outcome == "steered":
                            steered_during_tools = True
                        continue

                    result_text, elapsed_ms, outcome = self._execute_mcp_tool(
                        session, call, arguments
                    )
                    tool_ms += elapsed_ms
                    if outcome == "cancelled":
                        self._rollback_current_turn(session, history_start)
                        _record_turn_telemetry("cancelled")
                        self.result(req_id, _prompt_result("cancelled"))
                        return
                    self._append_tool_result(session, call.call_id, result_text)
                    decision = _observe_execution_guard(round_role, call, arguments, result_text)
                    if decision.action == "warn":
                        guard_replan_requested = True
                    elif decision.action == "halt":
                        guard_stop_requested = True
                    if outcome == "steered":
                        steered_during_tools = True

                if guard_stop_requested:
                    # A looping Glitter teammate should not kill the whole turn:
                    # hand control back to the lead and disable relay. The very
                    # high emergency cap, or a stagnant lead, stops the turn.
                    if round_role == "worker" and execution_guard_reason != "emergency_tool_call_limit":
                        self._runtime_event(
                            "model.relay.guard_handback",
                            session,
                            data={
                                "reason": execution_guard_reason,
                                "worker_model": round_model.model_id,
                                "step": step,
                            },
                        )
                        relay = None
                        relay_next_role = "lead"
                        pending_replan_notice = (
                            "The fast relay teammate was stopped because its tool work became repetitive. "
                            "Re-plan from the lead role using the results already gathered; do not repeat "
                            "the same tool/action cycle."
                        )
                        execution_guard.reset_stagnation_window()
                        execution_guard_reason = ""
                        continue

                    execution_guard_halted = True
                    if execution_guard_reason == "emergency_tool_call_limit":
                        final_text = (
                            "本轮已触发异常执行保护：工具调用量达到灾难级安全上限。"
                            "已停止继续调用工具，并保留当前已经完成的工作记录。"
                        )
                    else:
                        final_text = (
                            "检测到工具调用在重新规划后仍持续重复、没有产生新进展，"
                            "本轮已停止继续调用工具。当前已完成的工作记录已保留。"
                        )
                    with session.state_lock:
                        session.messages.append({"role": "assistant", "content": final_text})
                    self._runtime_event(
                        "execution.guard.stopped",
                        session,
                        data={
                            "reason": execution_guard_reason,
                            "tool_calls_seen": execution_guard.tool_calls_seen,
                            "step": step,
                            "role": round_role,
                        },
                    )
                    break

                if guard_replan_requested:
                    # The warning applies to this same execution role. Give the
                    # model an actual chance to re-plan before evaluating
                    # continued repetition as stagnation.
                    continue

                if self._consume_queued_steers_for_turn(session):
                    relay_next_role = "lead"
                    continue
                if relay is not None:
                    # Hand the next round to the fast teammate only when every
                    # call this round was pure information gathering; any
                    # change, shell or third-party tool keeps the lead in charge.
                    gathering_only = all(
                        self._relay_gathering_tool(session, call.name) for call in turn.tool_calls
                    )
                    relay_next_role = "worker" if gathering_only else "lead"
        except ProviderError as exc:
            self._rollback_current_turn(session, history_start)
            self.emit_runtime_error(session_id, str(exc))
            _record_turn_telemetry("error", type(exc).__name__)
            self.result(req_id, _prompt_result("end_turn"))
            t_end = time.perf_counter()
            self.trace(
                f"session/prompt ERROR total={(t_end-t_prompt)*1000:.0f}ms "
                f"pre_upstream={(t_ready-t_prompt)*1000:.0f}ms "
                f"mcp_wait={mcp_wait_ms:.0f}ms upstream={upstream_ms:.0f}ms tool={tool_ms:.0f}ms "
                f"provider_rounds={provider_rounds_total} tool_calls={tool_calls_total} native_calls={native_tool_calls_total} "
                f"tool_search_calls={tool_search_calls_total} "
                f"loaded_tools={len(session.loaded_tool_names)} model={model.model_id} error={exc}"
            )
            return

        # Types are execution policies, not model-size presets. Any Type
        # may add a bounded, tool-free secondary stage when its optimization
        # policy says the expected benefit is worth the extra cost/latency.
        # Failure never discards the primary draft.
        substantive_work = (
            tool_calls_total > 0
            or output_tokens >= 300
            or len(final_text.strip()) >= 1000
        )
        should_run_post_stage = (
            post_stage is not None
            and bool(final_text)
            and not session.cancel_event.is_set()
            and not execution_guard_halted
            and (not post_stage.requires_substantive_work or substantive_work)
        )
        if post_stage is not None and final_text and not should_run_post_stage:
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
        if should_run_post_stage:
            review_provider = post_stage.provider
            review_model = post_stage.model
            review_reason = post_stage.reason
            review_role = post_stage.role
            review_pack = post_stage.pack
            review_pack_label = post_stage.pack_label
            review_policy = post_stage.policy
            review_policy_label = post_stage.policy_label
            transcript = self._review_transcript(session, history_start)
            if reasoning_profile is not None:
                review_thinking, review_effort = self._resolve_invocation_reasoning(
                    review_model, session, reasoning_profile, routing_signals,
                    role=post_stage.role, stage="review",
                )
            else:
                review_effort = self._review_effort(review_model, session, post_stage.reasoning_intent)
                review_thinking = self._policy_thinking_mode(review_model, session, post_stage.reasoning_intent)
            observed_thinking_modes.add(review_thinking or "default")
            observed_efforts.add(review_effort or "default")
            review_messages: list[dict[str, Any]] = []
            if session.agent.prompt:
                review_messages.append({"role": "system", "content": session.agent.prompt})
            if runtime_mode_context:
                review_messages.append({"role": "system", "content": runtime_mode_context})
            review_messages.append({
                "role": "system",
                "content": (
                    f"Kiro BYO {review_pack_label} / {review_policy_label} {review_role} pass. Check the draft against the "
                    "turn transcript. Return only the final answer for the user. "
                    + (
                        "For verification, preserve the draft unless a material correctness, omission, or reasoning issue needs fixing. "
                        if review_role == "verify" else
                        "For final review, actively improve correctness, completeness, clarity, and reasoning where useful. "
                    )
                    + "Do not claim tools were used in this secondary stage and do not mention this stage. "
                    + "Never add actions, file changes, command results, or facts that the turn transcript does not show; "
                    "for long agentic work, keep the draft's account of what was actually done and only fix errors, gaps, or unclear conclusions."
                ),
            })
            review_messages.append({
                "role": "user",
                "content": f"TURN TRANSCRIPT:\n{transcript}\n\nDRAFT ANSWER:\n{final_text}",
            })
            self._runtime_event(
                f"model.execution.{review_pack}.{review_policy}.{review_role}.started",
                session,
                data={
                    "primary_provider": provider.id,
                    "primary_model": model.model_id,
                    "review_provider": review_provider.id,
                    "review_model": review_model.model_id,
                    "review_effort": review_effort or "default",
                    "review_thinking": review_thinking or "default",
                    "reason": review_reason,
                },
                content={"draft": final_text, "review_transcript": transcript},
            )
            review_chunks: list[str] = []
            t_review = time.perf_counter()
            try:
                review_turn = chat_completion_turn(
                    review_provider,
                    review_model.model_id,
                    review_messages,
                    review_chunks.append,
                    on_reasoning=_emit_reasoning_timed,
                    reasoning_effort=self._reasoning_wire_value(
                        review_model, review_thinking, review_effort
                    ) or None,
                    thinking_mode=review_thinking,
                    model_spec=review_model,
                    cache_key=f"{session.session_id}:{review_pack}-{review_policy}-{review_role}",
                    tools=[],
                    should_interrupt=lambda: self._interrupt_requested(session),
                )
                upstream_ms += (time.perf_counter() - t_review) * 1000.0
                input_tokens += review_turn.usage.input_tokens
                output_tokens += review_turn.usage.output_tokens
                cached_tokens += review_turn.usage.cached_tokens
                cache_write_tokens += review_turn.usage.cache_write_tokens
                reasoning_tokens += review_turn.usage.reasoning_tokens
                cache_usage_reported = cache_usage_reported or review_turn.usage.cached_tokens_reported
                cache_write_usage_reported = cache_write_usage_reported or review_turn.usage.cache_write_tokens_reported
                reasoning_usage_reported = reasoning_usage_reported or review_turn.usage.reasoning_tokens_reported
                reviewed = (review_turn.text or "".join(review_chunks)).strip()
                if reviewed and not review_turn.tool_calls:
                    final_text = reviewed
                    served_provider_id = review_provider.id
                    served_model_id = review_turn.served_model or review_model.model_id
                    with session.state_lock:
                        if session.messages and session.messages[-1].get("role") == "assistant":
                            session.messages[-1]["content"] = final_text
                            # The reviewer runs on a detached prompt branch. Its
                            # response/interaction id must never become the main
                            # conversation continuation. The primary continuation
                            # is also discarded because its native response text
                            # may differ from the reviewed answer now persisted.
                            session.messages[-1].pop("_kiro_provider_context", None)
                    self._runtime_event(
                        f"model.execution.{review_pack}.{review_policy}.{review_role}.completed",
                        session,
                        data={
                            "review_provider": review_provider.id,
                            "review_model": review_model.model_id,
                            "elapsed_ms": (time.perf_counter() - t_review) * 1000.0,
                            "changed": final_text.strip() != (turn.text or "".join(round_text_parts)).strip(),
                        },
                        content={"final": final_text},
                    )
                else:
                    self._runtime_event(
                        f"model.execution.{review_pack}.{review_policy}.{review_role}.skipped",
                        session,
                        data={"reason": "empty review or unexpected tool call"},
                    )
            except (ProviderError, ProviderInterrupted) as exc:
                upstream_ms += (time.perf_counter() - t_review) * 1000.0
                self._runtime_event(
                    f"model.execution.{review_pack}.{review_policy}.{review_role}.failed",
                    session,
                    data={
                        "review_provider": review_provider.id,
                        "review_model": review_model.model_id,
                        "error": type(exc).__name__,
                    },
                )

        if final_text:
            _mark_visible()
            self.emit_text(session_id, final_text, is_thinking=False)

        self._persist_session(session)
        # Commit diagnostics before acknowledging the turn.  The management
        # panel may refresh immediately after session/prompt resolves; writing
        # telemetry afterwards created a visible Turns=0 / TTFT=0 race.
        _record_turn_telemetry("success")
        self.result(req_id, _prompt_result("end_turn"))
        t_end = time.perf_counter()
        first_ms = (
            (first_visible_at - t_prompt) * 1000.0 if first_visible_at is not None else -1.0
        )
        stream_window_ms = (
            (last_visible_at - first_visible_at) * 1000.0
            if first_visible_at is not None and last_visible_at is not None
            else 0.0
        )
        cache_hits = (
            len(session.mcp.catalog_cache_hits) if session.mcp is not None else 0
        )
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
            f"mcp_wait={mcp_wait_ms:.0f}ms upstream={upstream_ms:.0f}ms tool={tool_ms:.0f}ms "
            f"tool_calls={tool_calls_total} native_calls={native_tool_calls_total} "
            f"tool_search_calls={tool_search_calls_total} "
            f"tool_search_loaded={tool_search_loaded_total} active_tools={active_tools_count} "
            f"loaded_real_tools={len(session.loaded_tool_names)} tool_schema_bytes={tool_schema_bytes} "
            f"catalog_tools={len(session.mcp.bindings) if session.mcp is not None else 0} "
            f"catalog_cache_hits={cache_hits} chunks={stream_chunks} chars={stream_chars} "
            f"max_chunk_chars={max_chunk_chars} stream_window={stream_window_ms:.0f}ms "
            f"max_gap={max_gap_ms:.0f}ms mcp_server_ms=[{mcp_server_ms}] "
            f"mcp_failures=[{mcp_failures}] model={model.model_id} "
            f"served_model={served_model_id} effort={session.effort or 'default'} "
            f"input_tokens={input_tokens} output_tokens={output_tokens} "
            f"cached_tokens={cached_tokens} cache_write_tokens={cache_write_tokens} "
            f"reasoning_tokens={reasoning_tokens}"
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
            self.error(req_id, -32000, "session busy; use the advertised _session/steer extension or let the client queue the message")
            return

        with session.state_lock:
            session.cancel_event.clear()
            session.steer_event.clear()
            session.steer_queue.clear()
            session.active_turn = True

        history_start = len(session.messages)
        try:
            text = self.prompt_text(params)
            user_message, has_image = self.prompt_message(params)

            # Echo fallback remains useful for installation diagnostics.
            if session.model in {MODEL_ECHO, MODEL_UPPER}:
                if text.strip().startswith("/compact"):
                    with session.state_lock:
                        session.messages.clear()
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
            mode_fallbacks: list[tuple[Any, Any]] = []
            post_stage: ReviewTarget | None = None
            primary_effort = ""
            primary_thinking = ""
            reasoning_profile = None
            routing_signals = None
            runtime_mode_context = ""
            relay: Any = None
            selected_type = self._type_for_selection(config, session.model)
            if selected_type is not None:
                tools_expected = session.agent.unrestricted or bool(session.agent.tools) or bool(session.loaded_tool_names)
                decision = route_model(
                    config,
                    pack=config.mode_pack,
                    policy=selected_type,
                    prompt=text,
                    history=session.messages,
                    has_image=has_image,
                    tools_expected=tools_expected,
                    reasoning_effort=session.effort if getattr(session, "effort_user_set", False) else "",
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
                policy_def = decision.policy_definition
                runtime_mode_context = runtime_policy_context(
                    decision.pack, decision.policy, active=True
                )
                routing_signals = decision.signals
                pin_key = f"{decision.pack}:{decision.policy}"
                if getattr(policy_def, "sticky", False):
                    pins = getattr(session, "route_pins", None)
                    pinned = pins.get(pin_key) if isinstance(pins, dict) else None
                    if pinned:
                        # Keep a long task on the model it started with, as long
                        # as that model is still eligible for this turn.
                        for row in decision.candidates:
                            if (row.provider.id, row.model.model_id) == pinned:
                                provider, model = row.provider, row.model
                                break
                    if isinstance(pins, dict):
                        pins[pin_key] = (provider.id, model.model_id)
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
                reasoning_profile = getattr(policy_def, "auto_reasoning", None)
                primary_thinking, primary_effort = self._resolve_invocation_reasoning(
                    model, session, reasoning_profile, decision.signals,
                    role="lead", stage="planning",
                )
                if str(getattr(policy_def, "collaboration", "none") or "none") == "relay":
                    teammate = self._relay_teammate(decision, provider, model)
                    if teammate is not None:
                        # The third tuple slot is retained for direct callers and
                        # old tests. Policy-driven turns recompute it per call.
                        relay = (teammate[0], teammate[1], "")
                execution_plan = build_execution_plan(decision)
                review_target = execution_plan.post_stage
                post_stage = review_target
                self._runtime_event(
                    "model.route.selected", session,
                    data={
                        "requested_model": session.model,
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
                            "tools_expected": decision.signals.tools_expected,
                            "reasoning_effort": decision.signals.reasoning_effort,
                            "code_task": decision.signals.code_task,
                            "complexity": round(decision.signals.complexity, 4),
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
                runtime_mode_context = runtime_policy_context(
                    config.mode_pack, config.mode_type, active=False
                )
                # A concrete model selection still obeys that model's persistent
                # BYO Auto/On/Off preference. With no Type suggestion, Auto
                # preserves the verified model/provider default.
                primary_thinking = resolve_thinking_mode(
                    model, getattr(model, "thinking_preference", "auto"), ""
                )
            t_ready = time.perf_counter()

            if text.strip().startswith("/compact"):
                # Compatibility route for Crew 0.9.0-insider.1, which still sends
                # manual compaction as a prompt. Current Kiro V3 clients should
                # use the advertised ``_kiro/session/compact`` extension instead.
                self._emit_summarization_info(session, "summarization_started")
                try:
                    summary = self._compact_session(
                        session,
                        provider,
                        model,
                        self._compact_context_suffix(text),
                    )
                except ProviderInterrupted:
                    self._emit_summarization_info(
                        session, "summarization_failed", detail="cancelled"
                    )
                    self.result(req_id, {"stopReason": "cancelled"})
                    return
                except ProviderError as exc:
                    self._emit_summarization_info(
                        session, "summarization_failed", detail=str(exc)
                    )
                    self.error(req_id, -32603, f"compaction failed: {exc}")
                    return
                self._persist_session(session)
                self._emit_summarization_info(
                    session, "summarization_completed", summary=summary
                )
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
                session.messages.append(user_message)

            self._run_provider_tool_loop(
                req_id, session_id, session, provider, model, history_start, t_prompt, t_ready, mode_fallbacks, post_stage,
                primary_effort=primary_effort,
                primary_thinking=primary_thinking,
                relay=relay,
                reasoning_profile=reasoning_profile,
                routing_signals=routing_signals,
                runtime_mode_context=runtime_mode_context,
            )
            return
        finally:
            with session.state_lock:
                session.active_turn = False
                session.cancel_event.clear()
                session.steer_event.clear()
                session.steer_queue.clear()
            session.turn_lock.release()
