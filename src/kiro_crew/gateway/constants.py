"""Stable wire names shared by Gateway API producers and clients."""

TURN_ORIGIN_HEADER = "X-KiroCrew-Turn-Origin"
MCP_OWNER_HEADER = "X-KiroCrew-MCP-Owner"
GATEWAY_TURN_ORIGIN_META_KEY = "_gateway_turn_origin"
QUEUED_GATEWAY_MCP_META_KEY = "_gateway_request_mcp_servers"
PRESIGNED_TOKEN_HEADER = "X-Presigned-Token"
SESSION_EVENTS_HEADER = "X-KiroCrew-Event-Subscription"
SESSION_EVENTS_VALUE = "sessions"
SESSION_EVENTS_CAPABILITY = "session_events"
SESSION_MESSAGE_EVENT = "session_message"
MAX_SESSION_EVENT_KEYS = 64
MAX_SESSION_EVENT_KEY_CHARS = 256
SESSION_PLAN_EVENT = "session_plan"
SUBSCRIBE_SESSIONS_MESSAGE = "subscribe_sessions"
