"""A workflow step's tool identity, derived the way a chat turn derives it.

In ``agent_sdk`` because it reads the ACP layer's own frame parser and request
builder; ``kiro_crew.kas_workflows.step_identity`` re-exports it for the router.

A KAS step runs in a session KAS opened itself, so no ``AcpSessionHandle`` owns
it, but its frames still reach the runtime: a ``session/update`` ``tool_call``
frame names each call (kind, ``rawInput``, ``_meta.kiro`` identity) before the
step's ``session/request_permission`` for it, exactly as on a chat session
(recorded: ``test/fixtures/acp_frames/kas/workflow-run-live.jsonl``). The router
watches those frames for the sessions it knows are steps and feeds them through
the chat path's own parser (``parse_session_update``), into caches of the same
shape the session handle keeps, under the same admission bound. The step's
permission request is then built by the chat path's own builder
(``build_permission_event``) over those caches, so every decision on the step
reads the identity a chat turn would read for the same call: the Crew tool name,
``is_shell`` and the command, the trusted raw params, the MCP server and tool.

A request whose ``tool_call`` frame never arrived (or whose cache entry the bound
refused) has no trusted identity, and is refused rather than judged on its title.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Mapping

#: Step sessions whose frames are kept; the oldest is dropped past this, and a
#: request on a dropped session then has no identity and is refused.
MAX_STEP_SESSIONS = 256
#: Each session's caches are bounded by the chat path's own admission bound
#: (``admit_tool_call_cache_entry``, the ``cache_overflow`` set), and a settled
#: call's rows are released at its terminal frame, as on a chat session.

#: The reason a step with no trusted identity is refused.
NO_IDENTITY = "the step's tool call named no identity Kiro Crew can verify"


@dataclass
class _Caches:
    """One step session's tool-call caches, the session handle's set."""

    tool_input: dict[str, str] = field(default_factory=dict)
    tool_input_redacted: dict[str, bool] = field(default_factory=dict)
    shell: dict[str, bool] = field(default_factory=dict)
    raw_params: dict[str, dict] = field(default_factory=dict)
    mcp_server_name: dict[str, str] = field(default_factory=dict)
    tool_name: dict[str, str] = field(default_factory=dict)
    diff_path: dict[str, str] = field(default_factory=dict)
    harness_tool_name: dict[str, str] = field(default_factory=dict)
    tool_kind: dict[str, str] = field(default_factory=dict)
    identity_unreadable: dict[str, bool] = field(default_factory=dict)
    overflow: set[str] = field(default_factory=set)

    def kwargs(self) -> dict[str, Any]:
        return {
            "tool_input_cache": self.tool_input,
            "tool_input_redacted_cache": self.tool_input_redacted,
            "shell_cache": self.shell,
            "raw_params_cache": self.raw_params,
            "mcp_server_name_cache": self.mcp_server_name,
            "tool_name_cache": self.tool_name,
            "diff_path_cache": self.diff_path,
            "harness_tool_name_cache": self.harness_tool_name,
            "tool_kind_cache": self.tool_kind,
            "identity_unreadable_cache": self.identity_unreadable,
        }


def _is_turn_start(update: Mapping[str, Any]) -> bool:
    meta = update.get("_meta")
    kiro = meta.get("kiro") if isinstance(meta, Mapping) else None
    return isinstance(kiro, Mapping) and kiro.get("kind") == "turn_start"


class StepToolCalls:
    """The tool-call caches of every step session the router watches."""

    def __init__(self) -> None:
        self._sessions: OrderedDict[str, _Caches] = OrderedDict()

    def observe(self, session_id: str, update: Any) -> None:
        """Feed one step session's ``session/update`` through the chat path's parser.

        Synchronous and cheap: it runs on the runtime's reader loop.
        """
        from kiro_crew.acp._dispatch_parts.session_update_parser import (
            UPDATE_TOOL_CALL,
            UPDATE_TOOL_CALL_UPDATE,
            parse_session_update,
        )
        from kiro_crew.acp._dispatch_parts.tool_call_provenance import (
            release_tool_call_provenance,
            scoped_tool_cache_key,
        )
        from kiro_crew.acp.types import TERMINAL_TOOL_STATUSES
        from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS

        if not isinstance(update, dict):
            return
        kind = update.get("sessionUpdate")
        if _is_turn_start(update):
            # A new turn: the handle clears its per-turn caches here too.
            self._sessions.pop(session_id, None)
            return
        if kind not in (UPDATE_TOOL_CALL, UPDATE_TOOL_CALL_UPDATE):
            return
        caches = self._sessions.pop(session_id, None) or _Caches()
        self._sessions[session_id] = caches
        while len(self._sessions) > MAX_STEP_SESSIONS:
            self._sessions.popitem(last=False)
        parse_session_update(
            update,
            cache_scope=session_id,
            cache_overflow=caches.overflow,
            harness_backend=ACP_BACKEND_KAS,
            **caches.kwargs(),
        )
        # A settled call's rows go once its result frame reports a terminal
        # status, as the session handle releases them
        # (``session_handle_updates._release_settled_tool_call``): the admission
        # bound then counts the calls in flight, not the turn's length.
        call_id = update.get("toolCallId")
        status = update.get("status")
        if (
            kind == UPDATE_TOOL_CALL_UPDATE
            and isinstance(status, str)  # type-checked before the set hashes it
            and status in TERMINAL_TOOL_STATUSES
            and isinstance(call_id, str)
            and call_id
        ):
            release_tool_call_provenance(
                scoped_tool_cache_key(session_id, call_id), *caches.kwargs().values()
            )

    def forget(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def event_for(self, params: Mapping[str, Any], *, plan: bool = False) -> tuple[Any, str | None]:
        """The chat path's permission event for a step's request: ``(event, refusal)``.

        ``refusal`` is set when no trusted identity can be derived: the builder
        cannot read the request, the frame named an identity half the bound
        refused, or no ``tool_call`` frame classified the call. A plan handoff is
        no exception: its ``update_workflow`` identity comes from its frame too.
        A plan KAS asks again after a restart is named by the frame KAS replays
        on the step's ``session/load`` (KAS-written history from the step's own
        runtime); with no such frame it is refused. *plan* is accepted for the
        callers' symmetry and changes nothing.
        """
        from kiro_crew.acp._dispatch import build_permission_event
        from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION, JsonRpcMessage
        from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS

        session_id = params.get("sessionId")
        session_id = session_id if isinstance(session_id, str) else ""
        caches = self._sessions.get(session_id) or _Caches()
        event, _options = build_permission_event(
            JsonRpcMessage(id=0, method=METHOD_REQUEST_PERMISSION, params=dict(params)),
            cache_scope=session_id,
            kas_consent_meta=True,
            harness_backend=ACP_BACKEND_KAS,
            **caches.kwargs(),
        )
        if event is None:
            return None, "the step's permission request could not be read"
        if getattr(event, "mcp_identity_unreadable", False):
            return None, NO_IDENTITY
        if not getattr(event, "shell_classified", False):
            return None, NO_IDENTITY
        return event, None


def payload_bound() -> int:
    """The tool-call cache's own bound on one call's input or params (chars)."""
    from kiro_crew.acp._dispatch_parts.tool_call_provenance import (
        TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS,
    )

    return TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS
