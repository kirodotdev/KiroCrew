"""A consent URL shaped like the one kiro-cli mints for PostHog's MCP server.

PostHog registers its MCP clients dynamically (RFC 7591) and hands out 40-char
alphanumeric ``client_id`` values, asks for about 190 scopes, and serves its
authorization endpoint with a trailing slash. The values here are fake; the
shapes are the real ones (read from PostHog's RFC 9728 / RFC 8414 metadata).
"""

from __future__ import annotations

from urllib.parse import urlencode

# Shaped like PostHog's dynamically registered ids: 40 alphanumeric characters.
POSTHOG_CLIENT_ID = "Kq7RmX2vNb9TzL4pWc8YhD3sFj6GaE1uQo5tVn0B"
# kiro-cli mints state as token_urlsafe(32) and an S256 challenge, both 43 chars.
STATE = "Zr8pW2kQ9xL4mN7vB1cT6yH3sD0fG5jK8aE2uRwXq9P"
CHALLENGE = "Qm9xVf3kLr8sT1uWz0aYcD6eHgJ2nPq5RtXv7BnMk4E"
POSTHOG_MCP = "https://mcp.posthog.com/mcp"
POSTHOG_ISSUER = "https://oauth.posthog.com"
POSTHOG_HOST = "oauth.posthog.com"
POSTHOG_PATH = "/oauth/authorize/"
POSTHOG_AUTHORIZE = f"https://{POSTHOG_HOST}{POSTHOG_PATH}"

_RESOURCES = [
    "action", "access_control", "account", "activity_log", "alert", "annotation",
    "approvals", "autoresearch", "batch_export", "batch_import", "billing",
    "business_knowledge", "canvas", "cohort", "comment", "conversation",
    "cross_project_dashboard", "customer_analytics", "customer_task",
    "customer_journey", "customer_profile_config", "data_catalog",
    "data_catalog_approval", "dashboard", "dashboard_template", "dataset",
    "early_access_feature", "endpoint", "engineering_analytics", "error_tracking",
    "evaluation", "element", "event_definition", "experiment", "experiment_holdout",
    "experiment_saved_metric", "external_data_source", "feature_flag", "group",
    "health_issue", "heatmap", "hog_function", "insight", "insight_variable",
    "integration", "live_debugger", "llm_analytics", "llm_gateway", "llm_prompt",
    "llm_provider_key", "logs", "max_tools", "notebook", "organization",
    "organization_integration", "organization_member", "person", "persisted_folder",
    "plugin", "product_tour", "project", "property_definition", "query",
    "revenue_analytics", "session_recording", "session_recording_playlist",
    "sharing_configuration", "subscription", "survey", "task", "uploaded_media",
    "user", "user_interview", "warehouse_objects", "warehouse_table",
    "warehouse_view", "web_analytics", "webhook", "workflow", "file_system",
    "file_system_shortcut", "desktop_recording", "conversation_ticket",
    "signal", "visual_review", "replay_summary", "issue_cluster", "tracing",
    "metrics", "deep_research",
]  # fmt: skip
SCOPE = " ".join(
    ["openid", "profile", "email", "introspection"]
    + [f"{name}:{verb}" for name in _RESOURCES for verb in ("read", "write")]
)


def posthog_consent_url(
    endpoint: str = POSTHOG_AUTHORIZE, client_id: str = POSTHOG_CLIENT_ID
) -> str:
    """A consent URL with every parameter kiro-cli puts on PostHog's."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "state": STATE,
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "redirect_uri": "http://127.0.0.1:49152/oauth/callback",
        "scope": SCOPE,
        "resource": POSTHOG_MCP,
    }
    return f"{endpoint}?{urlencode(params)}"
