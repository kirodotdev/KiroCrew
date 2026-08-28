"""AgentCore Gateway MCP inject — URL-only rebuild + live session/new.

Workload posture emits a URL-only spec at rebuild. The AWS extra rewrites
that URL to a localhost SigV4 proxy; kiro-cli never sees the unsigned
Gateway hostname. Rebuild may persist an ephemeral listen port; after a
gateway restart that listener is gone. ``session_gateway_servers``
therefore injects the live loopback URL so session/new outranks the
stale agent-file entry. It never injects the unsigned https Gateway
hostname.

Login attach, inbound sidecars, and consent live in a later PR. This
module does not write a sidecar and does not import the consent
allowlist.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping
from urllib.parse import urlparse

from kiro_crew.platform.context import current_context, safe_context_call
from kiro_crew.platform.governance import agentcore_posture
from kiro_crew.platform.governance_profiles import governance_permits

logger = logging.getLogger(__name__)

GATEWAY_SERVER_NAME = "agentcore-gateway"

# Spec keys that are bearer material or a place to hide it. Stripped before
# any write to ~/.kiro/agents/kirocrew.json.
_SECRET_SPEC_KEYS = frozenset({"headers", "authorization", "Authorization"})

# Remote-MCP keys safe to persist on the agent file (URL-only).
_URL_ONLY_KEYS = frozenset({"url", "type", "timeout", "disabledTools", "autoApprove"})

# Hosts the SigV4 proxy may advertise. https is never loopback-listen.
_LOOPBACK_LISTEN_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# kiro-cli 2.20.1 ``session/new`` deserializes an untagged ``McpServer``.
# HTTP requires ``type`` plus a ``headers`` array (empty is fine).
ACP_HTTP_TYPE = "http"


def strip_secret_spec_keys(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Copy *spec* without header / Authorization keys."""
    return {key: value for key, value in spec.items() if key not in _SECRET_SPEC_KEYS}


def sanitize_gateway_spec(spec: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return a URL-only remote MCP spec, or ``None`` when it is not one.

    Requires a non-empty string ``url``. Drops ``headers`` / ``Authorization``
    so a companion extra cannot persist a bearer into the agent file.
    """
    if not isinstance(spec, dict):
        return None
    url = spec.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    out: dict[str, Any] = {"url": url.strip()}
    for key in _URL_ONLY_KEYS:
        if key == "url" or key not in spec:
            continue
        out[key] = spec[key]
    return out


def acp_http_server(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    disabled: bool = False,
    name: str = GATEWAY_SERVER_NAME,
) -> dict[str, Any]:
    """ACP ``session/new`` HTTP element kiro-cli will deserialize.

    ``headers`` is always present (empty list when there is no bearer).
    """
    pairs = headers or {}
    shaped: dict[str, Any] = {
        "name": name,
        "type": ACP_HTTP_TYPE,
        "url": url,
        "headers": [{"name": str(key), "value": str(value)} for key, value in pairs.items()],
    }
    if disabled:
        shaped["disabled"] = True
    return shaped


def is_loopback_listen_url(url: str) -> bool:
    """True for the SigV4 proxy listen URL. Never the unsigned Gateway hostname."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "http":
        return False
    host = (parsed.hostname or "").lower()
    return host in _LOOPBACK_LISTEN_HOSTS


def _identity_on() -> bool:
    adapter_on = bool(
        safe_context_call(
            lambda: current_context().agent_identity.enabled(),
            fallback=False,
            log_message="agent_identity.enabled lookup failed; treating as off",
        )
    )
    if not adapter_on:
        return False
    permitted = bool(
        safe_context_call(
            lambda: getattr(
                governance_permits(
                    "capabilities.agentcore",
                    "",
                    fail_closed=True,
                    log_warning=False,
                ),
                "permitted",
                False,
            ),
            fallback=False,
            log_message="agentcore governance lookup failed; treating as disabled",
        )
    )
    if not permitted:
        return False
    return bool(
        safe_context_call(
            lambda: agentcore_posture(current_context().governance) is not None,
            fallback=False,
            log_message="agentcore posture lookup failed; treating as disabled",
        )
    )


def _current_posture() -> str | None:
    return safe_context_call(
        lambda: agentcore_posture(current_context().governance),
        fallback=None,
        log_message="agentcore posture lookup failed; no Gateway contribution",
    )


def _gateway_spec_from_adapter() -> dict[str, Any] | None:
    spec: dict[str, Any] | None = safe_context_call(
        lambda: current_context().agent_identity.gateway_mcp_spec(),
        fallback=None,
        log_message="gateway_mcp_spec lookup failed; no Gateway contribution",
    )
    if spec is not None:
        return sanitize_gateway_spec(spec)
    extras: dict[str, Any] = safe_context_call(
        lambda: current_context().mcp_tooling.extra_mcp_servers(),
        fallback={},
        log_message="extra_mcp_servers lookup failed; no Gateway fallback",
    )
    if not extras:
        return None
    return sanitize_gateway_spec(extras.get(GATEWAY_SERVER_NAME))


def rebuild_gateway_contribution() -> dict[str, dict[str, Any]]:
    """Gateway servers safe to write into the rebuilt agent file.

    Empty when identity is off, posture is not ``workload``, or the
    adapter spec is missing a URL. The URL is whatever
    ``gateway_mcp_spec()`` returned after sanitizer strip — the AWS extra
    substitutes a ``127.0.0.1`` SigV4 proxy.
    """
    if not _identity_on():
        return {}
    if _current_posture() != "workload":
        return {}
    sanitized = _gateway_spec_from_adapter()
    if sanitized is None:
        return {}
    return {GATEWAY_SERVER_NAME: sanitized}


def session_gateway_servers(session_key: str) -> list[dict[str, Any]]:
    """ACP ``mcpServers`` entries for this session's workload Gateway, or ``[]``.

    Injects the live loopback SigV4 listen URL so session/new outranks a
    stale agent-file port after a gateway restart. The unsigned Gateway
    hostname is never injected. Login sidecars are a later PR.
    """
    if not session_key:
        return []
    if not _identity_on():
        return []
    if _current_posture() != "workload":
        return []
    sanitized = _gateway_spec_from_adapter()
    url = str((sanitized or {}).get("url") or "")
    if not is_loopback_listen_url(url):
        return []
    return [acp_http_server(url)]
