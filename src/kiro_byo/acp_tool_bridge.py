"""ACP tool events and permission-request presentation boundary.

These payloads describe a tool to the host; metadata (including MCP identity)
is never itself authority to execute. The runtime policy and host consent
remain in their existing execution paths. Keeping this away from ACP dispatch
avoids coupling host-specific permission envelopes to protocol lifecycle.
"""
from __future__ import annotations

from typing import Any

from .acp_session_state import Session
from .tool_display import session_display_locale, tool_display_title


class AcpToolBridgeMixin:
    @staticmethod
    def _tool_identity(session: Session, public_name: str) -> tuple[str, str]:
        if session.mcp is None:
            return "", ""
        binding = session.mcp.bindings.get(public_name)
        if binding is None:
            return "", ""
        return binding.server_name, binding.tool_name

    def _tool_call_payload(
        self,
        session: Session,
        call_id: str,
        public_name: str,
        arguments: dict[str, Any],
        *,
        status: str | None = None,
    ) -> dict[str, Any]:
        server_name, tool_name = self._tool_identity(session, public_name)
        is_native = bool(session.native is not None and session.native.has_tool(public_name))
        payload: dict[str, Any] = {
            "toolCallId": call_id,
            "title": tool_display_title(
                public_name,
                ui_language=session_display_locale(session),
                native_name=session.native.canonical_name(public_name) if is_native and session.native else "",
                mcp_tool_name=tool_name if server_name else "",
                description=arguments.get("description") if is_native else None,
            ),
            # Native tools use canonical ACP kinds so Crew PreToolUse / governance
            # sees the real operation. MCP tools keep trusted identity metadata.
            "kind": session.native.kind_for(public_name) if is_native and session.native else "other",
            "rawInput": arguments,
        }
        if status:
            payload["status"] = status
        kiro_meta: dict[str, Any] = {}
        if is_native and session.native is not None:
            kiro_meta["toolId"] = session.native.canonical_name(public_name)
        if server_name and tool_name:
            # Keep the legacy flat identity for KiroCrew 0.9.0-insider.1 while
            # also emitting Kiro V3's versioned MCP identity envelope. Hints are
            # display metadata only; neither Kiro BYO nor Crew may authorize from
            # them alone.
            kiro_meta.update(
                {
                    "mcpServerName": server_name,
                    "toolName": tool_name,
                    "isMcpTool": True,
                }
            )
            binding = session.mcp.bindings.get(public_name) if session.mcp is not None else None
            mcp_tool: dict[str, Any] = {
                "version": 1,
                "identity": {"serverName": server_name, "toolName": tool_name},
            }
            if binding is not None and binding.annotations:
                mcp_tool["annotations"] = dict(binding.annotations)
            kiro_meta["mcpTool"] = mcp_tool
        if kiro_meta:
            payload["_meta"] = {"kiro": kiro_meta}
        return payload

    def _emit_tool_call(
        self,
        session: Session,
        call_id: str,
        public_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        tool_call = self._tool_call_payload(
            session, call_id, public_name, arguments, status="pending"
        )
        self.notify(
            "session/update",
            {
                "sessionId": session.session_id,
                "update": {"sessionUpdate": "tool_call", **tool_call},
            },
        )
        return tool_call

    def _emit_tool_status(
        self,
        session: Session,
        call_id: str,
        *,
        status: str,
        text: str = "",
    ) -> None:
        update: dict[str, Any] = {
            "sessionUpdate": "tool_call_update",
            "toolCallId": call_id,
            "status": status,
        }
        if text:
            update["content"] = [{"type": "text", "text": text}]
        self.notify("session/update", {"sessionId": session.session_id, "update": update})

    def _permission_consent_meta(
        self,
        session: Session,
        public_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Build Kiro V3 consent context without making it an auth decision."""
        server_name, tool_name = self._tool_identity(session, public_name)
        if server_name and tool_name:
            capability = "mcp"
            resource = f"{server_name}/{tool_name}"
        elif session.native is not None and session.native.has_tool(public_name):
            capability = session.native.capability_for(public_name)
            canonical = session.native.canonical_name(public_name)
            if canonical == "shell":
                resource = str(arguments.get("command") or canonical)
            elif canonical == "web_fetch":
                resource = str(arguments.get("url") or canonical)
            elif canonical == "web_search":
                resource = str(arguments.get("query") or canonical)
            elif canonical == "read_files":
                resource = ", ".join(str(x) for x in arguments.get("paths", [])[:5]) or canonical
            else:
                resource = str(arguments.get("path") or canonical)
        else:
            capability = "all"
            resource = public_name
        return {
            "capability": capability,
            "resource": resource,
            "triggeringResource": resource,
            "workspaceRoot": session.cwd,
            "persistableConsent": True,
        }

    def _request_tool_permission_choice(
        self,
        session: Session,
        call_id: str,
        public_name: str,
        arguments: dict[str, Any],
    ) -> str:
        tool_call = self._emit_tool_call(session, call_id, public_name, arguments)
        kiro_meta: dict[str, Any] = {
            "consent": self._permission_consent_meta(session, public_name, arguments)
        }
        server_name, tool_name = self._tool_identity(session, public_name)
        if server_name and tool_name:
            binding = session.mcp.bindings.get(public_name) if session.mcp is not None else None
            mcp_tool: dict[str, Any] = {
                "version": 1,
                "identity": {"serverName": server_name, "toolName": tool_name},
            }
            if binding is not None and binding.annotations:
                mcp_tool["annotations"] = dict(binding.annotations)
            # Kiro V3 places MCP identity on the permission request metadata.
            # Keep the copy in toolCall._meta too for Crew 0.9.0-insider.1.
            kiro_meta["mcpTool"] = mcp_tool
        result = self._request_client(
            "session/request_permission",
            {
                "sessionId": session.session_id,
                "toolCall": tool_call,
                "options": [
                    {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "allow_always", "name": "Always allow", "kind": "allow_always"},
                    {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
                    {"optionId": "reject_always", "name": "Always reject", "kind": "reject_always"},
                ],
                "_meta": {"kiro": kiro_meta},
            },
            cancel_event=session.cancel_event,
            should_interrupt=lambda: self._interrupt_requested(session),
        )
        outcome = result.get("outcome") if isinstance(result, dict) else None
        if not isinstance(outcome, dict):
            return ""
        if outcome.get("outcome") != "selected":
            return ""
        selected = str(outcome.get("optionId") or "")
        if selected in {"allow_once", "allow_always"}:
            return selected
        return ""

    def _request_tool_permission(
        self,
        session: Session,
        call_id: str,
        public_name: str,
        arguments: dict[str, Any],
    ) -> bool:
        return bool(
            self._request_tool_permission_choice(
                session, call_id, public_name, arguments
            )
        )
