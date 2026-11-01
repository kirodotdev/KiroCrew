"""Native tool dispatch boundary for the ACP Turn Engine.

Only orchestrates existing policy, permission, scope, interruption and audit
contracts. NativeToolRuntime owns sandbox execution. No routing, Provider,
Durable Run admission, or Crew integration policy is defined here.
"""
from __future__ import annotations

import time
from typing import Any

from .errors import ClientRequestError
from .native_tools import NativeInvocation, NativeToolError, PermissionDecision, ToolValidationError
from .sandbox import SandboxError


class NativeToolDispatchMixin:
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

        # The model can emit a stale or invented function call even when that
        # function was NOT in the current Saved Agent's advertised tool list.
        # Visibility is an execution constraint, not merely UI/model guidance.
        # This check must precede permission prompts and filesystem scope grants.
        available_native = {spec.name for spec in session.native.specs()}
        if invocation.spec.name not in self._native_visible_to_agent(session, available_native):
            result_text = "Permission denied: native tool is not visible to this Crew Saved Agent."
            self._emit_tool_call(session, call.call_id, invocation.spec.name, invocation.arguments)
            self._emit_tool_status(session, call.call_id, status="refused", text=result_text)
            session.native.audit.record(
                session_id=session.session_id,
                call_id=call.call_id,
                tool=invocation.spec.name,
                capability=invocation.spec.capability,
                permission="agent_tool_visibility",
                outcome="refused",
                exit_code=None,
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

        permission_choice, added_scope_roots, permission_already_requested, early = (
            self._approve_native_scope(session, call, invocation, scope_roots)
        )
        if early is not None:
            return early

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

        pre_dispatch_message, interrupted = self._pre_dispatch_tool_interrupt(
            session, call.call_id, invocation.spec.name, kind="native"
        )
        if interrupted:
            if permission_choice == "allow_once" and added_scope_roots:
                session.native.revoke_scope_roots(added_scope_roots)
            return pre_dispatch_message, 0.0, interrupted

        return self._run_authorized_native_invocation(
            session, call, invocation,
            permission_choice=permission_choice, added_scope_roots=added_scope_roots,
        )


    def _approve_native_scope(
        self, session: Any, call: Any, invocation: NativeInvocation,
        scope_roots: tuple[Any, ...],
    ) -> tuple[str, tuple[Any, ...], bool, tuple[str, float, str] | None]:
        """Request only required directory roots; return a terminal result on refusal.

        Approval is rechecked for cancellation/steering *before* the grant.
        The caller owns revoking an allow_once grant once acquired.
        """
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
                    return "", (), False, ("", 0.0, "cancelled")
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
                    return "", (), False, (result_text, 0.0, "steered")
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
                return "", (), False, (result_text, 0.0, "")

            pre_dispatch_message, interrupted = self._pre_dispatch_tool_interrupt(
                session, call.call_id, invocation.spec.name, kind="native"
            )
            if interrupted:
                return "", (), False, (pre_dispatch_message, 0.0, interrupted)
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
                return "", (), False, (result_text, 0.0, "")

        return permission_choice, added_scope_roots, permission_already_requested, None


    def _run_authorized_native_invocation(
        self, session: Any, call: Any, invocation: NativeInvocation,
        *, permission_choice: str, added_scope_roots: tuple[Any, ...],
    ) -> tuple[str, float, str]:
        """Execute after the last interruption fence and retain audit semantics.

        A one-time scope is revoked in a finally block, including tool errors.
        """
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
