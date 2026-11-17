"""Tool declarations of the ``code_channels`` domain of ``kirocrew-core``.

The handlers are in :mod:`kiro_crew.mcp_tools.code_channels`, which reads every name here as
its own. This module imports none of them (see the package docstring).
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.mcp_tools.catalog import loaded_binding

logger = logging.getLogger("kiro_crew.mcp_tools.code_channels")


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
        return bool(features.code_channels_toggle(cfg))
    except Exception:
        logger.debug("slack.code_channels unreadable; code-channel tools not listed", exc_info=True)
        return False


def advertised() -> list[dict[str, Any]]:
    """``schemas()`` while ``slack.code_channels`` is on, else nothing."""
    toggle_on = loaded_binding("kiro_crew.mcp_tools.code_channels", "_toggle_on", _toggle_on)
    return schemas() if toggle_on() else []
