"""Crew-governed MCP tool dispatch for the BYO turn runtime.

The host retains control over tool visibility and approval. A granted tool is
revalidated immediately before dispatch; after dispatch, interrupted or failed
results are treated as potentially side-effecting.  Mode/Type and model routing
are intentionally outside this module.
"""
from __future__ import annotations

import time
from typing import Any

from .errors import ClientRequestError


class McpToolDispatchMixin:
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
        live = bool(binding is not None and (
            not hasattr(session.mcp, "is_live_binding")
            or session.mcp.is_live_binding(call.name)
        ))
        if not live and hasattr(session.mcp, "revalidate_requested_crew_binding"):
            # A model may request a Crew tool while the live MCP catalogue is
            # still warming or has changed. A targeted authenticated refresh
            # can recover a real binding; it cannot grant a missing tool.
            recovered = session.mcp.revalidate_requested_crew_binding(call.name)
            if recovered:
                self._sync_host_capabilities(session)  # update diagnostics after real discovery
            binding = session.mcp.bindings.get(call.name)
            live = bool(binding is not None and session.mcp.is_live_binding(call.name))
        visible = bool(binding is not None and self._mcp_visible_to_agent(session, binding))
        if not (live and visible):
            reason = "agent_scope_denied" if live and not visible else (
                "crew_binding_unverified" if binding is not None else "crew_binding_absent"
            )
            result_text = ("Error: MCP tool is not currently granted or verified by KiroCrew. "
                           f"Diagnostic: {reason}.")
            self._emit_tool_status(session, call.call_id, status="refused", text=result_text)
            self._runtime_event(
                "tool.dispatch.blocked", session,
                data={"kind": "mcp", "call_id": call.call_id,
                      "tool": call.name, "reason": reason},
            )
            return result_text, 0.0, ""
        auto_approved = bool(
            binding is not None
            and not (hasattr(session.mcp, "is_crew_member_mount")
                     and session.mcp.is_crew_member_mount(binding.server_name))
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

        # A permission dialog can remain open while Crew revokes a tool or a
        # refreshed catalogue changes which server owns a public tool name.
        # Re-check both the grant and the exact binding identity at dispatch.
        current_binding = session.mcp.bindings.get(call.name)
        if (current_binding is None
                or (current_binding.server_name, current_binding.tool_name)
                    != (binding.server_name, binding.tool_name)
                or not self._mcp_visible_to_agent(session, current_binding)
                or (hasattr(session.mcp, "is_live_binding")
                    and not session.mcp.is_live_binding(call.name))):
            result_text = "Error: Crew revoked or changed this MCP tool during approval; it was not dispatched."
            self._emit_tool_status(session, call.call_id, status="refused", text=result_text)
            self._runtime_event(
                "tool.dispatch.blocked", session,
                data={"kind": "mcp", "call_id": call.call_id, "tool": call.name, "reason": "grant_changed"},
            )
            return result_text, 0.0, ""

        pre_dispatch_message, interrupted = self._pre_dispatch_tool_interrupt(
            session, call.call_id, call.name, kind="mcp"
        )
        if interrupted:
            return pre_dispatch_message, 0.0, interrupted

        self._emit_tool_status(session, call.call_id, status="in_progress")
        started = time.perf_counter()
        # The production MCP manager exposes protocol-level error status. Keep
        # the string-only fallback for external/mock clients whose legacy API
        # cannot distinguish an error from a successful "Error:" text payload.
        failed = False
        effect_uncertain = False
        try:
            call_with_status = getattr(session.mcp, "call_with_status", None)
            # A session-specific legacy override (including test/adapter
            # patching of ``call``) must retain its existing interception
            # contract. Only the unmodified production manager uses the
            # structured status API; arbitrary string-only clients cannot
            # supply protocol-level status.
            legacy_override = "call" in getattr(session.mcp, "__dict__", {})
            if callable(call_with_status) and not legacy_override:
                outcome = call_with_status(call.name, arguments)
                result_text = outcome.text
                failed = bool(outcome.is_error)
                effect_uncertain = failed and bool(outcome.dispatched)
            else:
                result_text = session.mcp.call(call.name, arguments)
                failed = result_text.startswith("Error:")
                effect_uncertain = failed
        except Exception as exc:
            # Any unexpected transport/protocol/decoder error may arrive after
            # a mutation was performed. Never replay it; never expose exception
            # messages which may contain private URLs or authentication data.
            result_text = (
                f"Error: MCP tool transport failed ({type(exc).__name__})."
                if isinstance(exc, OSError) else
                f"Error: MCP tool call failed ({type(exc).__name__})."
            )
            failed = True
            effect_uncertain = True
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        status = "failed" if failed else "completed"
        if effect_uncertain:
            result_text += (
                "\nExecution outcome may be unknown. Verify external state before "
                "retrying this tool; do not automatically repeat a mutation."
            )
            self._runtime_event(
                "tool.effect.uncertain", session,
                data={"kind": "mcp", "call_id": call.call_id, "tool": call.name,
                      "effect_class": "unknown_write", "reason": "tool_error"},
            )
        interrupted_after = self._tool_interrupt_reason(session)
        if interrupted_after:
            # The call was already dispatched. A cancellation or steering event
            # does not undo a possibly committed remote action.
            self._runtime_event(
                "tool.effect.uncertain", session,
                data={"kind": "mcp", "call_id": call.call_id, "tool": call.name,
                      "effect_class": "unknown_write", "reason": interrupted_after},
            )
        self._emit_tool_status(session, call.call_id, status=status, text=result_text)
        self._runtime_event(
            "tool.call.completed",
            session,
            data={"kind": "mcp", "call_id": call.call_id, "tool": call.name, "status": status,
                  "elapsed_ms": elapsed_ms, "interruption": interrupted_after},
            content={"arguments": arguments, "result": result_text},
        )
        return result_text, elapsed_ms, interrupted_after
