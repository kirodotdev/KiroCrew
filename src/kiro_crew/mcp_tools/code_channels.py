"""The ``code_channel_*`` tools: a bound session's own Slack code channel.

``schemas()`` is the ADVERTISEMENT half (name, model-facing description, the
JSON Schema a call is built against) and ``HANDLERS`` maps each name to the
function that runs it; ``test_mcp_tool_registry`` holds the two together.

Every handler is the same thin forwarder: resolve the caller STRICTLY
(``mcp_core.require_strict_session_key`` -- never the ``/proc`` walk, under which
a sub-agent would resolve to its parent's slot) and POST the call to the
gateway's ``/api/slack/code-channel/tool`` with that key. The gateway attests the
key, derives the channel from it and decides everything else
(:mod:`kiro_crew.slack.code_channels.tools`); no argument here names a channel.

Listing follows the ``slack.code_channels`` setting (:func:`advertised`): with it
off ``tools/list`` is exactly what it is without code channels. Whether THIS
session is bound to a channel cannot be known when kiro-cli lists tools once per
session (a pooled server answers many sessions, and a warm-pool process may list
before it has one), so a call from an unbound session is refused at the gateway
with a message saying so.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from kiro_crew import mcp_core

logger = logging.getLogger(__name__)

_ROUTE = "/api/slack/code-channel/tool"
_PIN = (
    " It always acts on THIS session's own Slack code channel (the session must be one "
    "a code channel is bound to); it takes no channel and cannot reach any other channel."
)


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def schemas() -> list[dict[str, Any]]:
    """Descriptors for the code-channel tools."""
    return [
        {
            "name": "code_channel_set_view",
            "description": (
                "Publish or update a view tab in your Slack code channel: an html "
                "page (a full, self-contained document; no external resources load) "
                "or a display-only block_kit surface. Calls with the same view_key "
                "update the same tab; a channel holds at most five tabs. The Code "
                "tab's diff comes from your worktree and the Plan tab from "
                "code_channel_publish_plan_canvas, so neither is a type here." + _PIN
            ),
            "inputSchema": _obj(
                {
                    "type": {
                        "type": "string",
                        "enum": ["html", "block_kit"],
                        "description": "View kind (default html).",
                    },
                    "view_key": {
                        "type": "string",
                        "description": "Stable id of the tab (html/block_kit), e.g. 'reports/coverage.html'. 'plan' is reserved.",
                    },
                    "name": {"type": "string", "description": "Tab label (<= 80 chars)."},
                    "content": {
                        "type": "string",
                        "description": "html: the HTML document (<= 1 MB).",
                    },
                    "blocks": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "block_kit: up to 50 display-only blocks (no buttons, inputs or remote images).",
                    },
                }
            ),
        },
        {
            "name": "code_channel_publish_plan_canvas",
            "description": (
                "Create your code channel's plan canvas, or replace its content, from "
                "markdown, and show it as the Plan tab (members can comment, you edit). "
                "Use it for the plan of the work and keep it current as the plan changes." + _PIN
            ),
            "inputSchema": _obj(
                {
                    "markdown": {
                        "type": "string",
                        "description": "The plan, as markdown (<= 1 MB).",
                    },
                    "title": {"type": "string", "description": "Canvas title when it is created."},
                },
                ["markdown"],
            ),
        },
        {
            "name": "code_channel_set_context_bar",
            "description": (
                "Set your own items on your code channel's context bar (the strip at the "
                "top of the channel). Your items follow the repo / branch / PR / CI items "
                "Kiro Crew keeps there itself, five items in all; the list you send "
                "replaces your previous items (an empty list clears them). Keys repo, "
                "folder, branch, pr and ci are taken. Items are informational, or links "
                "with an https url." + _PIN
            ),
            "inputSchema": _obj(
                {
                    "items": {
                        "type": "array",
                        "maxItems": 5,
                        "items": _obj(
                            {
                                "key": {"type": "string", "description": "Unique id, lowercase."},
                                "label": {"type": "string", "description": "Text (<= 128 chars)."},
                                "icon": {
                                    "type": "string",
                                    "enum": [
                                        "branch",
                                        "folder",
                                        "hierarchy",
                                        "life-ring",
                                        "link",
                                        "globe",
                                        "terminal",
                                        "code",
                                        "search",
                                        "lock",
                                    ],
                                },
                                "url": {"type": "string", "description": "https link (optional)."},
                            },
                            ["key", "label"],
                        ),
                    }
                },
                ["items"],
            ),
        },
        {
            "name": "code_channel_rename",
            "description": (
                "Rename your code channel and its session when the focus of the work "
                "changes (1-200 characters)." + _PIN
            ),
            "inputSchema": _obj({"title": {"type": "string"}}, ["title"]),
        },
        {
            "name": "code_channel_set_status",
            "description": (
                "Set your code channel's session status. Kiro Crew already shows "
                "processing while you work and active when your turn ends; set "
                "suspended when you have asked the people in the channel something and "
                "cannot continue without their answer (it stays after your turn ends)." + _PIN
            ),
            "inputSchema": _obj(
                {"status": {"type": "string", "enum": ["processing", "suspended", "active"]}},
                ["status"],
            ),
        },
        {
            "name": "code_channel_wrap_up",
            "description": (
                "Post the wrap-up of your code channel's work: your summary (what changed, "
                "branch, pull request and CI, anything left open), followed by the facts "
                "Kiro Crew knows and an Archive button. Use it when the work is done or "
                "when someone runs /wrap-up. You cannot archive the channel: a person "
                "presses the button." + _PIN
            ),
            "inputSchema": _obj(
                {
                    "summary": {
                        "type": "string",
                        "description": "Short markdown summary (<= 2800 chars).",
                    }
                },
                ["summary"],
            ),
        },
    ]


def _toggle_on() -> bool:
    """Whether ``slack.code_channels`` is on (fails to off; never raises)."""
    try:
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.slack import features

        cfg = KiroCrewConfig.load()
        toggle = getattr(features, "code_channels_toggle", None) or features.code_channels_enabled
        return bool(toggle(cfg))
    except Exception:
        logger.debug("slack.code_channels unreadable; code-channel tools not listed", exc_info=True)
        return False


def advertised() -> list[dict[str, Any]]:
    """``schemas()`` while ``slack.code_channels`` is on, else nothing."""
    return schemas() if _toggle_on() else []


def _forward(name: str, args: dict[str, Any]) -> str:
    session_key, refusal = mcp_core.require_strict_session_key(
        f"Error: {name} acts on the calling session's own Slack code channel, and this "
        "session could not be identified."
    )
    if refusal:
        return refusal
    reply = mcp_core._post(
        _ROUTE,
        {"tool": name, "args": args if isinstance(args, dict) else {}},
        session_key=session_key,
        timeout=60,
    )
    if not isinstance(reply, dict):
        return f"Error: {name} got no answer from the gateway."
    text = reply.get("text")
    if isinstance(text, str) and text:
        return text if reply.get("ok") or text.startswith("Error") else f"Error: {text}"
    error = reply.get("error") or "the gateway did not complete the call"
    return f"Error: {error}"


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    tool["name"]: _forward for tool in schemas()
}
