"""ACP (Agent Client Protocol) types for kiro-cli JSON-RPC communication."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── ACP Event Kinds ──

EVENT_TEXT_CHUNK = "text_chunk"
EVENT_THINKING_CHUNK = "thinking_chunk"
EVENT_TOOL_CALL = "tool_call"
EVENT_TOOL_CALL_UPDATE = "tool_call_update"
EVENT_TOOL_RESULT = "tool_result"
EVENT_PERMISSION_REQUEST = "permission_request"
EVENT_COMPLETE = "complete"
EVENT_COMPACTION_STATUS = "compaction_status"
EVENT_CLEAR_STATUS = "clear_status"
EVENT_AGENT_SWITCHED = "agent_switched"
EVENT_MCP_OAUTH_REQUEST = "mcp_oauth_request"
EVENT_MCP_SERVER_INITIALIZED = "mcp_server_initialized"
EVENT_MCP_SERVER_INIT_FAILURE = "mcp_server_init_failure"
EVENT_SUBAGENT_LIST = "subagent_list"
EVENT_SUBAGENT_ACTIVITY = "subagent_activity"
EVENT_STEER_QUEUED = "steer_queued"
EVENT_STEER_CONSUMED = "steer_consumed"
EVENT_STEER_CLEARED = "steer_cleared"

# ── ACP Protocol Methods ──

METHOD_INITIALIZE = "initialize"
METHOD_SESSION_NEW = "session/new"
METHOD_SET_MODEL = "session/set_model"
METHOD_SET_MODE = "session/set_mode"
METHOD_PROMPT = "session/prompt"
METHOD_CANCEL = "session/cancel"
METHOD_REQUEST_PERMISSION = "session/request_permission"
METHOD_SESSION_UPDATE = "session/update"
METHOD_METADATA = "_kiro.dev/metadata"
METHOD_COMMANDS_EXECUTE = "_kiro.dev/commands/execute"
METHOD_SESSION_LOAD = "session/load"
# kiro-cli extension: evict a session from the multiplexed process, freeing its
# transcript/context + reaping its MCP children. Without this the shared
# kiro-cli process retains every session's state for its whole lifetime, so RSS
# grows without bound as sessions accumulate. Handler: acp_agent.rs -> Session
# ManagerRequestData::TerminateSession (self.sessions.remove + handle.shutdown).
METHOD_SESSION_TERMINATE = "_kiro.dev/session/terminate"
METHOD_COMPACTION_STATUS = "_kiro.dev/compaction/status"
METHOD_CLEAR_STATUS = "_kiro.dev/clear/status"
METHOD_AGENT_SWITCHED = "_kiro.dev/agent/switched"
METHOD_MCP_OAUTH_REQUEST = "_kiro.dev/mcp/oauth_request"
METHOD_MCP_SERVER_INITIALIZED = "_kiro.dev/mcp/server_initialized"
METHOD_MCP_SERVER_INIT_FAILURE = "_kiro.dev/mcp/server_init_failure"
METHOD_SUBAGENT_LIST_UPDATE = "_kiro.dev/subagent/list_update"
METHOD_KIRO_SESSION_UPDATE = "_kiro.dev/session/update"
METHOD_SET_CONFIG_OPTION = "session/set_config_option"

# ── ACP Backend Identifiers ──

ACP_BACKEND_CLAUDE = "claude"

# ── ACP Session Update Types ──

UPDATE_USER_MESSAGE_CHUNK = "user_message_chunk"
UPDATE_AGENT_MESSAGE_CHUNK = "agent_message_chunk"
UPDATE_AGENT_THOUGHT_CHUNK = "agent_thought_chunk"
UPDATE_TOOL_CALL = "tool_call"
UPDATE_TOOL_CALL_UPDATE = "tool_call_update"
UPDATE_PLAN = "plan"
UPDATE_AVAILABLE_COMMANDS = "available_commands_update"
UPDATE_CURRENT_MODE = "current_mode_update"
UPDATE_CONFIG_OPTION = "config_option_update"
UPDATE_SESSION_INFO = "session_info_update"
UPDATE_USAGE = "usage_update"

# Updates we recognise but don't yet surface (plumbing-only). Listed here so the
# "unhandled session update" log doesn't fire for them.
KNOWN_SESSION_UPDATES = frozenset({
    UPDATE_USER_MESSAGE_CHUNK,
    UPDATE_AGENT_MESSAGE_CHUNK,
    UPDATE_AGENT_THOUGHT_CHUNK,
    UPDATE_TOOL_CALL,
    UPDATE_TOOL_CALL_UPDATE,
    UPDATE_PLAN,
    UPDATE_AVAILABLE_COMMANDS,
    UPDATE_CURRENT_MODE,
    UPDATE_CONFIG_OPTION,
    UPDATE_SESSION_INFO,
    UPDATE_USAGE,
})

# ── ACP Permission Outcomes ──

OUTCOME_SELECTED = "selected"
OUTCOME_CANCELLED = "cancelled"
OPTION_ALLOW_ONCE = "allow_once"
OPTION_ALLOW_ALWAYS = "allow_always"

# ── Stop Reasons ──

STOP_REASON_CANCELLED = "cancelled"
STOP_REASON_END_TURN = "end_turn"
# Model-side content refusal ("response declined by the model"). Non-retryable:
# retrying the same prompt hits the same refusal, so chat_runner surfaces an
# actionable message instead of churning the retry ladder.
STOP_REASON_REFUSAL = "refusal"
# Signalled by the ACP layer when a genuinely-wedged (stale) turn was probed via
# session/cancel and got no ack within the grace window — a confirmed wedge, not
# a done-but-missing-frame turn (which acks and completes normally). The
# dashboard routes this to reset+resume+continue-nudge auto-recovery.
STOP_REASON_STALE_RECOVER = "stale_recover"
# Signalled by the per-session watchdog when an in-flight tool was judged dead
# / stuck / UNKNOWN-past-budget and the session was cancelled. Kept in the
# "error:" family so callers without a dedicated branch fall back to the
# generic error handling; chat_runner routes it to a dedicated recovery
# (continue-nudge, NOT a verbatim re-run of the original message).
STOP_REASON_TOOL_STALL = "error: tool stall"

# ── Approval Modes ──

APPROVAL_AUTO = "auto"
APPROVAL_INTERACTIVE = "interactive"


@dataclass
class JsonRpcRequest:
    """Outbound JSON-RPC 2.0 request."""

    method: str
    params: dict[str, Any]
    id: int
    jsonrpc: str = "2.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "jsonrpc": self.jsonrpc,
            "id": self.id,
            "method": self.method,
            "params": self.params,
        }


@dataclass
class JsonRpcMessage:
    """Inbound JSON-RPC 2.0 message (response or notification)."""

    id: Any = None
    method: str | None = None
    result: Any = None
    error: Any = None
    params: Any = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JsonRpcMessage":
        """Build a JsonRpcMessage from a parsed JSON-RPC frame."""
        return cls(
            id=data.get("id"),
            method=data.get("method"),
            result=data.get("result"),
            error=data.get("error"),
            params=data.get("params"),
        )

    def is_response_for(self, req_id: int) -> bool:
        # A JSON-RPC *response* carries an id + result/error and NO method.
        # The id space for our outbound requests (prompt, initialize, ...) is
        # independent of the agent's inbound *request* id space (server→client
        # session/request_permission), so the two can collide on the same
        # integer.  Requiring method is None ensures an inbound permission
        # request whose id happens to equal the in-flight prompt's req_id is
        # NOT misread as that prompt's completion (which would end the turn
        # early and leave the real tool permission unanswered → stuck turn).
        return self.id == req_id and self.method is None

    def is_method(self, name: str) -> bool:
        return self.method == name


@dataclass
class AcpEvent:
    """Structured event from kiro-cli ACP stream."""

    kind: str  # text_chunk, tool_call, permission_request, complete
    text: str = ""
    tool_call_id: str = ""
    title: str = ""
    tool_kind: str = ""
    tool_purpose: str = ""
    context_usage_pct: float = 0.0
    stop_reason: str = ""
    request_id: str | int = ""
    options: list[dict[str, str]] = field(default_factory=list)
    tool_input: str = ""
    tool_output: str = ""
    tool_final: bool = False  # True when this tool_result is the final (status=completed) update
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    credits: float = 0.0
    num_turns: int = 0
    duration_ms: int = 0
    raw_tool_params: dict | None = None  # original tool params before diff conversion (for file-chip snapshots)
    # MCP OAuth notification fields (EVENT_MCP_OAUTH_REQUEST):
    server_name: str = ""
    oauth_url: str = ""
    # Native subagent list (EVENT_SUBAGENT_LIST) — kiro-cli per-subagent state.
    subagents: list[dict[str, Any]] | None = None
    # Owning sub-agent session id (EVENT_SUBAGENT_ACTIVITY) — ties a tool call
    # to a specific native sub-agent card.
    sub_session_id: str = ""
    # Provider-set canonical signal: True when this tool call is a shell/exec
    # command. Each provider maps its own vocabulary (ACP kind=="execute", CC
    # tool name "Bash") onto this one flag, so the dashboard validation layer
    # exempts shell commands from the tool-name length cap without hardcoding
    # provider-specific tool_kind literals (which silently re-break on every
    # engine migration / tool rename).
    is_shell: bool = False


@dataclass
class AcpPromptStats:
    """Stats from the last ACP prompt."""

    event_count: int = 0
    text_chunks: int = 0
    tool_calls: list[tuple[str, str]] = field(default_factory=list)
    context_pct: float = 0.0
    # Raw token counts from the adapter's usage_update {used, size}. context_pct
    # is derived as used/size*100, but the dashboard token TEXT must use the
    # real served window (size) — re-deriving it on the frontend from the model
    # id (e.g. assuming 1M for "[1m]") can disagree with the window the adapter
    # actually divided by, inflating the displayed "X / Y tokens". 0 = unknown.
    context_used_tokens: int = 0
    context_window_tokens: int = 0
    # Per-turn billing credits summed from kiro's _kiro.dev/metadata
    # meteringUsage (unit="credit"). 0 for providers that bill in tokens.
    credits: float = 0.0
