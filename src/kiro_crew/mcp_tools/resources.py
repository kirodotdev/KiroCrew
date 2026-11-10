"""MCP resource-subscription tools: subscribe, list, remove (RFC Phase 4).

``rfc-mcp-resource-subscriptions.md`` §4.8 and §4.9.3. ``schemas()`` is the
advertisement, ``HANDLERS`` the behaviour, following ``ledger.py``.

The tools carry NO session argument: the caller is resolved strictly with
:func:`mcp_core.require_strict_session_key`, the verified key is sent on the
wire, and the gateway acts only on that key, so a session subscribes, lists and
removes its own subscriptions alone. The holder service lives in the gateway
process; these handlers only reach it over the loopback API.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from kiro_crew import mcp_core

#: Refusal code for a caller whose identity could not be verified strictly.
REFUSED_IDENTITY = "identity"

#: A subscribe waits for the holder to connect and for the server's answer. The
#: service answers within its own ``SUBSCRIBE_BUDGET_SECS``, the wait for its
#: lock included; this call waits longer than that budget, so a refusal here
#: means the gateway never answered, not that a change is still running.
_SUBSCRIBE_TIMEOUT_S = 150.0


def schemas() -> list[dict[str, Any]]:
    """Descriptors for the resource-subscription tools."""
    return [
        {
            "name": "resource_subscribe",
            "description": (
                "Subscribe THIS session to an MCP resource on one of its own MCP "
                "servers, and be woken when the server reports it changed -- even "
                "after this session's kiro-cli has stopped. Each wake is a "
                "[Webhook wake] turn that names the URI as untrusted data and "
                "repeats your `instruction`, which is the only part of the wake "
                "that instructs. Updates are coalesced (one wake per 60 s window) "
                "and budgeted (48 wakes a day per subscription, then paused until "
                "the next UTC day). Subscribing the same server and URI again keeps "
                "the subscription and replaces its instruction; it also resumes a "
                "subscription paused because its server could not be reached. "
                "Refused with a reason when resource subscriptions are off, the "
                "session has no dashboard tab, the server is not attached to this "
                "session or does not support subscriptions, or a limit is reached. "
                "Only for dashboard sessions; prefer it to a polling loop when the "
                "server supports it."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "server": {
                        "type": "string",
                        "maxLength": 128,
                        "description": "The MCP server's name as this session knows it.",
                    },
                    "uri": {
                        "type": "string",
                        "maxLength": 8192,
                        "description": "The resource URI to watch.",
                    },
                    "instruction": {
                        "type": "string",
                        "maxLength": 2000,
                        "description": "What to do when woken: carried by every wake.",
                    },
                },
                "required": ["server", "uri", "instruction"],
            },
        },
        {
            "name": "resource_subscriptions",
            "description": (
                "List THIS session's MCP resource subscriptions: id, server, full "
                "URI, instruction, state (active, or paused with its reason) and "
                "wakes today."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "resource_unsubscribe",
            "description": (
                "Remove one of THIS session's MCP resource subscriptions by the id "
                "resource_subscribe or resource_subscriptions returned. Answers "
                "not_found for an id this session does not hold."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "subscription_id": {"type": "string", "maxLength": 64},
                },
                "required": ["subscription_id"],
            },
        },
    ]


def _strict_session_key() -> tuple[str, str]:
    """The calling session, strictly, or a refusal; see ``ledger._strict_session_key``."""
    return mcp_core.require_strict_session_key(
        f"Refused ({REFUSED_IDENTITY}): this session's identity could not be verified "
        "strictly, so it cannot hold resource subscriptions. Subagents inherit no "
        "session identity of their own -- subscribe from the parent session instead."
    )


def _answer(d: dict[str, Any]) -> str | None:
    """The refusal or error text for *d*, or None when it succeeded."""
    if d.get("error"):
        return f"Error: {d['error']}"
    if d.get("ok") is False:
        line = f"Refused ({d.get('refused', 'unknown')}): {d.get('reason', '')}".rstrip()
        if "server_error" in d:
            line += "\nServer error: " + json.dumps(d["server_error"])
        return line
    return None


def resource_subscribe(name: str, args: dict[str, Any]) -> str:
    sk, err = _strict_session_key()
    if err:
        return err
    payload = {k: args.get(k) for k in ("server", "uri", "instruction")}
    d = mcp_core._post(
        "/api/resource-subscriptions", payload, session_key=sk, timeout=_SUBSCRIBE_TIMEOUT_S
    )
    refused = _answer(d)
    if refused is not None:
        return refused
    return json.dumps(
        {"subscription_id": d.get("id", ""), "server_result": d.get("server_result")}, indent=2
    )


def resource_subscriptions(name: str, args: dict[str, Any]) -> str:
    sk, err = _strict_session_key()
    if err:
        return err
    d = mcp_core._get("/api/resource-subscriptions", session_key=sk)
    refused = _answer(d)
    if refused is not None:
        return refused
    rows = d.get("subscriptions") or []
    if not rows:
        return "This session holds no resource subscriptions."
    return json.dumps(rows, indent=2)


def resource_unsubscribe(name: str, args: dict[str, Any]) -> str:
    subscription_id = args.get("subscription_id")
    if not isinstance(subscription_id, str) or not subscription_id:
        return "Error: subscription_id is required"
    sk, err = _strict_session_key()
    if err:
        return err
    d = mcp_core._post(
        "/api/resource-subscriptions/remove", {"subscription_id": subscription_id}, session_key=sk
    )
    refused = _answer(d)
    if refused is not None:
        return refused
    return f"Removed {subscription_id}."


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "resource_subscribe": resource_subscribe,
    "resource_subscriptions": resource_subscriptions,
    "resource_unsubscribe": resource_unsubscribe,
}
