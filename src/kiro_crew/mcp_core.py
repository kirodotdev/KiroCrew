"""MCP server exposing spawn, learn, and task tools to kiro-cli.

Runs as ``kirocrew mcp-core`` — kiro-cli spawns it as a child process
and calls tools via JSON-RPC over stdio (MCP protocol).

Tools:
    spawn_run       — spawn a background subagent
    spawn_list      — list running/completed subagents
    spawn_status    — retrieve full subagent output
    learn_add       — save a learned correction
    learn_list      — list all lessons
    learn_remove    — remove lessons by substring
    task_run        — start the autonomous task runner
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import mimetypes
import os
import platform
import re as _re
import socket
import subprocess
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse

from kiro_crew import platform_compat
from kiro_crew.agent_discovery import list_agents
from kiro_crew.artifacts import _infer_kind
from kiro_crew.autonudge import binding_key_for
from kiro_crew.config.loader import KiroCrewConfig, config_dir, outbox_dir
from kiro_crew.context_management import COMPLETION_KEEP_DEFAULT_CHARS, summarize_result
from kiro_crew.dashboard.origin import parse_dashboard_url
from kiro_crew.history import _SEARCH_SCAN_WINDOW as SEARCH_SCAN_WINDOW
from kiro_crew.history import INCOGNITO_MEMORY_MODES, ConversationLog
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes
from kiro_crew.knowledge.dedup import dedup_sweep
from kiro_crew.knowledge.embedder import create_embedder_from_config
from kiro_crew.knowledge.retrieval import HybridRetriever
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.mcp_shared import (
    ToolCancelled,
    call_tool_with_logging,
    is_tool_cancelled,
    run_mcp_stdio_loop,
)
from kiro_crew.messaging.link import is_legacy_slack_key, legacy_key
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.security import (
    BINARY_MIME_ALLOWLIST,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import sel
from kiro_crew.skills import SkillsLoader
from kiro_crew.subagent import resolve_max_subagents
from kiro_crew.subagent_persistence import _agent_dir
from kiro_crew.validation import (
    _SLACK_TS_RE,
    ARTIFACT_AGENT_MARKER,
    ARTIFACT_DELETE_COMMENT_SCHEMA,
    ARTIFACT_DELETE_SCHEMA,
    ARTIFACT_FOLDER_CREATE_SCHEMA,
    ARTIFACT_FOLDER_DELETE_SCHEMA,
    ARTIFACT_FOLDER_LIST_SCHEMA,
    ARTIFACT_FOLDER_MOVE_SCHEMA,
    ARTIFACT_FOLDER_RENAME_SCHEMA,
    ARTIFACT_GET_COMMENTS_SCHEMA,
    ARTIFACT_GET_SCHEMA,
    ARTIFACT_LIST_SCHEMA,
    ARTIFACT_MARK_REVIEW_SCHEMA,
    ARTIFACT_MOVE_SCHEMA,
    ARTIFACT_POST_COMMENT_SCHEMA,
    ARTIFACT_REPLY_COMMENT_SCHEMA,
    ARTIFACT_REVERT_SCHEMA,
    ARTIFACT_SAVE_SCHEMA,
    ARTIFACT_UPDATE_SCHEMA,
    ARTIFACT_VERSIONS_SCHEMA,
    AUTONUDGE_STOP_SCHEMA,
    CHANNEL_ID_RE,
    GET_CHAT_SESSION_SCHEMA,
    KNOWLEDGE_DEDUP_SCHEMA,
    LEARN_ADD_SCHEMA,
    LIST_SESSIONS_SCHEMA,
    LOCAL_KNOWLEDGE_SEARCH_SCHEMA,
    MAX_MEDIUM_STRING,
    MAX_SHORT_STRING,
    MCP_CORE_SCHEMAS,
    MONITOR_START_SCHEMA,
    REGISTER_HOOK_SCHEMA,
    SEARCH_CHAT_HISTORY_SCHEMA,
    SET_PROJECT_SCHEMA,
    SKILL_SEARCH_SCHEMA,
    SPAWN_RUN_SCHEMA,
    SPAWN_SUB_AGENTS_SCHEMA,
    TASK_RUN_SCHEMA,
    WAIT_SCHEMA,
    WORKFLOW_AUTHOR_SCHEMA,
    WORKFLOW_RERUN_SCHEMA,
    WORKFLOW_RUN_ID_SCHEMA,
    WORKFLOW_RUN_SCHEMA,
    validate_tool_args,
)


def _resolve_api_base() -> str:
    """Resolve the gateway API base URL from ``dashboard.url`` config."""
    cfg = KiroCrewConfig.load()
    _host, port = parse_dashboard_url(cfg.dashboard.url)
    return f"http://localhost:{port}"


_API = _resolve_api_base()


def _compress_snapshot_to_outline(snapshot: str, max_lines: int = 100) -> str:
    """Compress a full accessibility snapshot into a compact outline.

    Keeps: headings, links, buttons, inputs, images with alt text, and
    structural landmarks. Strips: empty containers, decorative elements,
    redundant whitespace. Returns element refs so agent can interact
    without re-reading the full snapshot.
    """
    if not snapshot:
        return "Empty snapshot — page may not have loaded."

    lines = snapshot.split("\n")
    keep_patterns = _re.compile(
        r"(heading|link|button|textbox|combobox|checkbox|radio|tab|menu"
        r"|img|image|navigation|main|banner|contentinfo|search|alert"
        r"|dialog|listitem|row|cell|ref=)"
    )
    outline: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped == "-":
            continue
        if keep_patterns.search(stripped.lower()):
            indent = len(line) - len(line.lstrip())
            compact_indent = "  " * min(indent // 2, 4)
            outline.append(f"{compact_indent}{stripped}")
            if len(outline) >= max_lines:
                outline.append(f"... (truncated at {max_lines} lines)")
                break

    if not outline:
        total = len([ln for ln in lines if ln.strip()])
        return f"No interactive elements found in snapshot ({total} total lines). Try browser_snapshot with a more specific target."

    return f"Page outline ({len(outline)} elements):\n" + "\n".join(outline)


def _search_snapshot(snapshot: str, query: str, max_results: int = 50) -> str:
    """Search a snapshot for lines matching a query pattern."""
    if not snapshot:
        return "Empty snapshot."
    if not query:
        return "Error: query is required"

    try:
        pattern = _re.compile(query, _re.IGNORECASE)
    except _re.error:
        pattern = _re.compile(_re.escape(query), _re.IGNORECASE)

    lines = snapshot.split("\n")
    matches: list[str] = []
    for i, line in enumerate(lines, 1):
        if pattern.search(line):
            matches.append(f"L{i}: {line.strip()}")
            if len(matches) >= max_results:
                break

    if not matches:
        return f"No matches for '{query}' in snapshot ({len(lines)} lines)."

    return f"Found {len(matches)} matches:\n" + "\n".join(matches)


def _list_tools() -> list[dict[str, Any]]:
    # Derive the learn_add rule/negative char limit from the schema field the
    # validator actually enforces (single source of truth) so the tool hint
    # tracks the enforced limit — including a future config-driven value —
    # instead of a parallel constant that can silently drift.
    _rule_max = next(
        (f.max_len for f in LEARN_ADD_SCHEMA.fields if f.name == "rule"),
        MAX_SHORT_STRING,
    )
    _neg_max = next(
        (f.max_len for f in LEARN_ADD_SCHEMA.fields if f.name == "negative"),
        MAX_SHORT_STRING,
    )
    # Advertise the live concurrent sub-agent cap so the model fans out with
    # confidence instead of self-limiting. resolve_max_subagents is the single
    # source of truth (auto-sizes from host mem/CPU + learned cost, or the
    # explicit agent.max_subagents). A snapshot at tool-list time is fine: this
    # is advisory guidance, not an enforced limit, and SubagentManager
    # auto-queues any overflow regardless.
    try:
        _max_sub = resolve_max_subagents(KiroCrewConfig.load())
    except Exception:
        _max_sub = 0
    _cap_hint = (
        f" You can run up to {_max_sub} sub-agents concurrently; if a task has "
        "more independent parts than that, still pass ALL of them in one call — "
        "any beyond the cap are queued and drained automatically as slots free, "
        "so you never need to split the work into multiple manual rounds."
        if _max_sub > 0
        else ""
    )
    return [
        {
            "name": "spawn_run",
            "description": (
                "Spawn subagent(s) to run tasks in the background. "
                "Returns immediately — results arrive as [Subagent completion event] "
                "messages in your conversation. For parallel work, use 'tasks' array. "
                "Tasks are automatically batched if they exceed the concurrency limit."
                + _cap_hint
                + " WAIT for all completion events before responding to the user."
                " If result batches from a previous spawn are still arriving,"
                " do not start a new spawn until all of them have been"
                " delivered and processed."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Single task description",
                    },
                    "tasks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Multiple tasks to run in parallel",
                    },
                    "agent": {
                        "type": "string",
                        "description": "Agent name for the subagent. Use spawn_list to see available agents.",
                    },
                    "agents": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Agent names corresponding to each task in 'tasks' array",
                    },
                    "max_turns": {
                        "type": "integer",
                        "description": "Override tool-call budget for this spawn (default: config or 100)",
                    },
                    "cwd": {
                        "type": "string",
                        "description": (
                            "Optional absolute path to launch the subagent subprocess in, "
                            "instead of the default sandbox. Enables cwd-relative resource globs "
                            "(.kiro/steering, AGENTS.md, CLAUDE.md) to resolve against this directory. "
                            "Must be under a configured subagent_cwd_allowed_roots entry "
                            "(default: [~/workspace, ~/workplace]). Applies to all tasks in a batch spawn."
                        ),
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            "Optional model override for the subagent (e.g. 'deepseek-3.2', "
                            "'claude-haiku-4.5'). When set, the subagent runs on this model "
                            "instead of the gateway default. To discover available models, "
                            "run: kiro-cli chat --list-models --format json"
                        ),
                    },
                },
            },
        },
        {
            "name": "spawn_list",
            "description": "List all running and completed subagents (read-only, no commands executed)",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "skill_search",
            "description": (
                "Search available skills by keyword (grep over skill names, "
                "descriptions, and — on a metadata miss — bodies). Only the most-"
                "used skills are pre-listed in the injected '## Available Skills' "
                "block; use this tool to discover the long tail that is NOT shown "
                "there. Returns matching skills with file paths — `cat` a path to "
                "load the full skill, or use the $<name> inline token."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keywords to search for across skills.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20, max 50).",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "spawn_status",
            "description": (
                "Retrieve a completed subagent's full transcript by agent ID (from a "
                "completion event). The completion event gives a summary plus this "
                "transcript on disk — use this tool (or the read/grep tools on the path) "
                "to read the rest instead of re-running the subagent. For large "
                "transcripts, page with offset/limit (line-based, like reading code) or "
                "filter with grep (regex) rather than pulling the whole thing into context."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Subagent ID from completion event",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "0-based start line for a paged read (default 0)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Max lines to return (1-2000). Omit for the full transcript; "
                            "use with offset to page through a large result."
                        ),
                    },
                    "grep": {
                        "type": "string",
                        "description": (
                            "Case-insensitive regex; return only transcript lines that "
                            "match (offset/limit then apply to the matches)."
                        ),
                    },
                },
                "required": ["agent_id"],
            },
        },
        {
            "name": "spawn_sub_agents",
            "description": (
                "Spawn one or more sub-agents to run tasks in parallel. Each sub-agent "
                "gets its own session with full tool access. BLOCKS until all sub-agents "
                "complete, then returns their collected results. Use for delegating "
                "independent subtasks to specialist agents. Preferred over spawn_run when "
                "you need results before continuing." + _cap_hint
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agents": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "agent_or_mode": {
                                    "type": "string",
                                    "description": "Agent name for the sub-agent",
                                },
                                "prompt": {
                                    "type": "string",
                                    "description": "Task/prompt for the sub-agent",
                                },
                            },
                            "required": ["prompt"],
                        },
                        "description": "Array of sub-agents to spawn in parallel",
                    },
                    "cwd": {
                        "type": "string",
                        "description": (
                            "Optional absolute path to launch sub-agents in. "
                            "Must be under a configured subagent_cwd_allowed_roots entry."
                        ),
                    },
                },
                "required": ["agents"],
            },
        },
        {
            "name": "learn_add",
            "description": (
                "Save a learned correction or preference that persists across all "
                "future sessions. MUST be called when the user corrects you, says "
                "'always do X', 'never do Y', or 'remember that'. Include both "
                "the rule (what to do) and negative (what not to do)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "rule": {
                        "type": "string",
                        "maxLength": _rule_max,
                        "description": (
                            f"The lesson to remember. HARD LIMIT {_rule_max} "
                            "characters — longer rules are REJECTED (not truncated), "
                            "so keep it concise. Put 'what not to do' in the separate "
                            "'negative' field rather than inlining a long '-- NOT: ...' "
                            "clause here, and split unrelated corrections into multiple "
                            "learn_add calls instead of one oversized rule."
                        ),
                    },
                    "category": {
                        "type": "string",
                        "enum": ["tool", "preference", "knowledge"],
                        "description": "Category: tool, preference, or knowledge",
                    },
                    "negative": {
                        "type": "string",
                        "maxLength": _neg_max,
                        "description": (
                            f"What NOT to do (optional). HARD LIMIT {_neg_max} "
                            "characters — rejected if exceeded."
                        ),
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["global", "workspace"],
                        "description": "Where to save: 'global' (default, all workspaces) or 'workspace' (active workspace only)",
                    },
                    "workspace": {
                        "type": "string",
                        "description": "Workspace name (required when scope='workspace'). Use the workspace name from your session context.",
                    },
                },
                "required": ["rule", "category"],
            },
        },
        {
            "name": "learn_list",
            "description": "List all saved lessons and corrections",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "learn_remove",
            "description": "Remove lessons whose rule contains the given substring",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Substring to match"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "task_run",
            "description": (
                "Start the autonomous task runner from a spec file or inline content. "
                "Use when the user provides a task spec or says 'run this task', "
                "'start a task', or 'run a task'. "
                "For inline specs, prefix content with __inline__:"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "spec": {
                        "type": "string",
                        "description": "Path to spec file, or inline content prefixed with __inline__:",
                    },
                    "name": {
                        "type": "string",
                        "description": "Human-readable task name (auto-derived from spec if omitted)",
                    },
                },
                "required": ["spec"],
            },
        },
        {
            "name": "wait",
            "description": (
                "Pause execution for a specified duration while preserving full session "
                "context. Use when waiting for external systems (code review, CI "
                "pipeline, deployment). Max 1800s (30 min)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "integer",
                        "description": "Duration to wait in seconds (60-1800)",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why we are waiting (shown to user)",
                    },
                },
                "required": ["seconds", "reason"],
            },
        },
        {
            "name": "register_hook",
            "description": (
                "Register a webhook listener so an external system can inject a message "
                "into a dedicated agent session later. Returns the webhook URL and session "
                "key. Use this when you need to hand off to an external process (e.g. "
                "submit a code review, then wait for the review bot to call back with results). "
                "The external system POSTs to the returned URL with the results."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "hook_id": {
                        "type": "string",
                        "description": "Unique identifier for this hook (e.g. 'review:pr-123')",
                    },
                    "context_summary": {
                        "type": "string",
                        "description": "Summary of current work context for session resume",
                    },
                },
                "required": ["hook_id", "context_summary"],
            },
        },
        {
            "name": "send_message",
            "description": (
                "Send a message to the user. By default delivers a dashboard "
                'notification only. Set session="slack" to also send a Slack DM. '
                "Set 'channel' to target a tracked channel, or 'user' to DM an "
                "allowed user — specify at most one, not both. "
                "Use this whenever you decide someone should be notified — most "
                "commonly in silent cron jobs, but applicable any time proactive "
                "notification is needed."
                "\n\nsession param (optional):"
                "\n  omitted  — dashboard notification only (default)."
                '\n  "slack"  — Slack DM + dashboard notification.'
                '\n  "origin" — inject into the dashboard session that spawned'
                " this cron. Falls through to notification-only if origin is"
                " unreachable (tab closed, history deleted, or cron has no origin)."
                "\n\nExplicit channel=... or user=... always sends to Slack."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Message text. Also used as fallback when blocks are provided.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Optional title for the notification",
                    },
                    "blocks": {
                        "type": "array",
                        "description": "Optional Slack Block Kit blocks array. When provided, the message is sent as a rich Block Kit message with text as fallback.",
                        "items": {"type": "object"},
                        "maxItems": 50,
                    },
                    "channel": {
                        "type": "string",
                        "description": "Target channel ID (e.g. C0123ABC456). Must be a tracked channel. Omit to send to owner DM.",
                    },
                    "user": {
                        "type": "string",
                        "description": "Target user ID (e.g. U0123ABC456) to DM. Must be an allowed user. Omit to send to owner DM.",
                    },
                    "unfurl_links": {
                        "type": "boolean",
                        "description": "Whether to unfurl URL link previews. Defaults to true.",
                    },
                    "unfurl_media": {
                        "type": "boolean",
                        "description": "Whether to unfurl media (images/video) previews. Defaults to true.",
                    },
                    "thread_ts": {
                        "type": "string",
                        "description": (
                            "Optional Slack thread timestamp (e.g. '1712793600.123456'). "
                            "When provided, the message is posted as a threaded reply under "
                            "that parent message. Works with 'channel' (thread in channel) "
                            "or 'user' (thread in DM)."
                        ),
                    },
                    "reply_broadcast": {
                        "type": "boolean",
                        "description": (
                            "When true and 'thread_ts' is set, also broadcast the threaded reply "
                            "to the channel's main message list. Requires 'thread_ts' — passing "
                            "reply_broadcast=true without thread_ts returns 400. Defaults to false."
                        ),
                    },
                    "session": {
                        "type": "string",
                        "enum": ["origin", "slack"],
                        "description": (
                            "Delivery routing. Omit for notification bell only (default). "
                            '"slack" adds Slack DM delivery. '
                            '"origin" injects into the dashboard session that spawned '
                            "this cron (falls back to notification if unreachable)."
                        ),
                    },
                },
                "required": ["text"],
            },
        },
        {
            "name": "delete_message",
            "description": (
                "Delete a message previously sent by this bot. Only works on "
                "messages authored by the KiroCrew bot itself (Slack API constraint). "
                "Use to clean up transient notifications after the user acknowledges them."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                        "description": "Channel ID where the message was posted.",
                    },
                    "ts": {
                        "type": "string",
                        "description": "Timestamp of the message to delete (from send_message response).",
                    },
                },
                "required": ["channel", "ts"],
            },
        },
        {
            "name": "read_slack_profile",
            "description": (
                "Read a Slack user's profile. Returns display name, title, "
                "status, timezone, and other profile fields. Rate limited to "
                "5 lookups per minute."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "user": {
                        "type": "string",
                        "description": "Slack user ID (e.g. U0123ABC456).",
                    },
                },
                "required": ["user"],
            },
        },
        {
            "name": "file_send",
            "description": (
                "Send a file to the user. Copies the file to the outbox and "
                "notifies the dashboard/Slack with a download link. Use when "
                "you've generated a report, export, artifact, or any file the "
                "user should receive."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the file to send"},
                    "description": {
                        "type": "string",
                        "description": "Brief description of what the file is",
                    },
                    "channel": {
                        "type": "string",
                        "description": (
                            "Optional Slack channel ID (e.g. C0123ABC456) to upload "
                            "the file to. Must be a tracked channel the bot is a "
                            "member of. Omit to send to the owner's DM."
                        ),
                    },
                },
                "required": ["path"],
            },
        },
        {
            "name": "artifact_save",
            "description": (
                "Save a chat-rendered artifact (typically the HTML body of an "
                "<mcwidget>) so the user can find, view, and iterate on it later. "
                "Returns the slug — a stable handle the user (and you) can "
                "reference in future sessions ('iterate on artifact <slug>'). "
                "Use this when the user asks to save a widget, when you create "
                "something worth keeping, or before iterating (use artifact_update "
                "for the iteration step itself)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Human-readable name (e.g. 'CR Queue Dashboard'). Used to derive the slug if omitted.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Artifact content. For widgets, the inner HTML of the <mcwidget> tag (NOT the surrounding tag itself).",
                    },
                    "slug": {
                        "type": "string",
                        "description": "Optional explicit slug (lowercase, digits, hyphens). Auto-derived from name when omitted.",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["widget", "html", "markdown", "svg", "json", "text", "webapp"],
                        "description": (
                            "Artifact kind. Optional — inferred from the content "
                            "when omitted (HTML-ish body -> widget, markdown text "
                            "-> markdown). Pass explicitly to override; markdown "
                            "documents should set kind='markdown'."
                        ),
                    },
                    "source": {
                        "type": "string",
                        "enum": ["chat", "cron", "subagent", "manual", "import"],
                        "description": "Provenance marker. Default: chat.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Short description of what the artifact shows or does.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tags for filtering in the library (max 16).",
                    },
                    "folder": {
                        "type": "string",
                        "description": (
                            "Optional folder to file the artifact in — a folder id "
                            "OR a '/'-separated human path (e.g. 'Reports/Q3'). "
                            "Missing path segments are auto-created (mkdir -p). "
                            "Omit or pass 'root' to leave it unfiled."
                        ),
                    },
                    "webapp_metadata": {
                        "type": "object",
                        "description": (
                            "For kind='webapp' only — metadata for the app-artifact "
                            "control card. Shape: {slug, origin_session, "
                            "deploy_target:{provider,account,region,public_url}, "
                            "architecture, lifecycle, cost, teardown}. "
                            "For draft apps: set lifecycle.status='draft'"
                        ),
                        "additionalProperties": True,
                    },
                },
                "required": ["name", "content"],
            },
        },
        {
            "name": "artifact_get",
            "description": (
                "Load an artifact by slug. Returns the metadata and content. "
                "Use this before artifact_update to read the current HTML when "
                "the user asks to iterate on an existing artifact."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug (lowercase, digits, hyphens).",
                    },
                    "version": {
                        "type": "integer",
                        "description": "Specific version to read. Omit for current.",
                    },
                },
                "required": ["slug"],
            },
        },
        {
            "name": "artifact_update",
            "description": (
                "Update an artifact's live state. Each agent edit "
                "automatically creates a new version (like a git commit) — "
                "the user can revert to any prior agent iteration via "
                "artifact_revert. Use after artifact_get when iterating "
                "on an existing artifact at the user's request."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug to update.",
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "New content. Each call records a new version "
                            "automatically when invoked via MCP."
                        ),
                    },
                    "name": {
                        "type": "string",
                        "description": "New name (optional rename).",
                    },
                    "description": {
                        "type": "string",
                        "description": "New description (optional).",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Replacement tag list (optional).",
                    },
                    "webapp_metadata": {
                        "type": "object",
                        "description": (
                            "Webapp deployment metadata (optional). Used to "
                            "transition an artifact between draft and live "
                            "deployment states."
                        ),
                    },
                },
                "required": ["slug"],
            },
        },
        {
            "name": "artifact_revert",
            "description": (
                "Revert an artifact's live state to a prior version. Reads "
                "version N's content and writes it as the new live state, "
                "creating a fresh snapshot tagged 'reverted' so the activity "
                "timeline shows the rollback. Use this instead of "
                "artifact_update when the user asks to undo recent changes "
                "or restore an earlier state — it avoids the agent having "
                "to manually fetch the old content first."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug to revert.",
                    },
                    "target_version": {
                        "type": "integer",
                        "description": (
                            "Version number to restore. Use artifact_versions "
                            "first to list available versions."
                        ),
                        "minimum": 1,
                    },
                },
                "required": ["slug", "target_version"],
            },
        },
        {
            "name": "artifact_list",
            "description": (
                "List saved artifacts. Optionally filter by tag, kind, or "
                "name substring. Use this to discover what artifacts exist "
                "before iterating, or when the user asks 'what have we saved?'"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "description": "Filter by tag."},
                    "kind": {
                        "type": "string",
                        "enum": ["widget", "html", "markdown", "svg", "json", "text", "webapp"],
                        "description": "Filter by kind.",
                    },
                    "q": {
                        "type": "string",
                        "description": "Case-insensitive substring filter on artifact name.",
                    },
                },
            },
        },
        {
            "name": "artifact_versions",
            "description": (
                "List the version numbers stored for an artifact. Use this "
                "before artifact_get with an explicit version to figure out "
                "what's available."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug.",
                    },
                },
                "required": ["slug"],
            },
        },
        {
            "name": "artifact_delete",
            "description": (
                "Permanently delete an artifact and all its versions. Use only "
                "when the user explicitly asks to remove an artifact."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug to delete.",
                    },
                },
                "required": ["slug"],
            },
        },
        {
            "name": "artifact_get_comments",
            "description": (
                "Get all comments on an artifact (local + provider-synced). "
                "Use to read feedback, review comments, or discussion threads "
                "on an artifact before addressing them."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug to get comments for.",
                    },
                },
                "required": ["slug"],
            },
        },
        {
            "name": "artifact_post_comment",
            "description": (
                "Post a comment on an artifact. Agent comments are flagged "
                "(is_agent) and SEL-audited. Use scope='shared' to sync to the "
                "provider."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Comment body text.",
                    },
                    "scope": {
                        "type": "string",
                        "description": "private (local only) or shared (syncs to provider).",
                    },
                },
                "required": ["slug", "text"],
            },
        },
        {
            "name": "artifact_reply_comment",
            "description": (
                "Reply to an existing comment thread on an artifact. "
                "If the parent is provider-origin, the reply posts back."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug.",
                    },
                    "parent_id": {
                        "type": "string",
                        "description": "ID of the comment to reply to.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Reply body text.",
                    },
                },
                "required": ["slug", "parent_id", "text"],
            },
        },
        {
            "name": "artifact_mark_review",
            "description": (
                "Advance a comment thread to REVIEW status, signaling "
                "the issue is addressed and awaiting human verification. "
                "Agent can mark_review but NEVER resolve."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug.",
                    },
                    "comment_id": {
                        "type": "string",
                        "description": "ID of the root comment to advance.",
                    },
                },
                "required": ["slug", "comment_id"],
            },
        },
        {
            "name": "artifact_delete_comment",
            "description": (
                "Delete a comment thread you have demonstrably applied — an "
                "unambiguous directive ('delete this', 'fix typo') that was "
                "fully executed. Root deletes cascade to replies. For "
                "judgment calls the human may want to verify, use "
                "artifact_mark_review instead. Provider-synced comments "
                "cannot be deleted by agents (the tool refuses) — mark those "
                "REVIEW. Deletion is SEL-audited and recorded in the "
                "artifact's activity feed with your reason."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug.",
                    },
                    "comment_id": {
                        "type": "string",
                        "description": "ID of the comment to delete (root deletes its replies too).",
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "One-line justification recorded in the audit log and "
                            "activity feed, e.g. 'applied in v12: deleted the "
                            "flagged paragraph'."
                        ),
                    },
                },
                "required": ["slug", "comment_id", "reason"],
            },
        },
        {
            "name": "artifact_folder_list",
            "description": (
                "List the artifact-library folder tree. Returns each folder's id, "
                "name, parent_id, human path, and direct item_count. Use to "
                "discover folder ids/paths before moving or organizing artifacts."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "artifact_folder_create",
            "description": (
                "Create an artifact-library folder. ``parent`` accepts a folder id "
                "OR a '/'-separated human path; missing segments are auto-created "
                "(mkdir -p). Omit ``parent`` (or pass 'root') to create at the top "
                "level. Returns the new folder id and canonical path."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Folder name (max 100 chars)."},
                    "parent": {
                        "type": "string",
                        "description": "Parent folder id or human path. Omit / 'root' for top level.",
                    },
                },
                "required": ["name"],
            },
        },
        {
            "name": "artifact_folder_rename",
            "description": "Rename an artifact-library folder. ``folder`` = folder id or human path.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "folder": {"type": "string", "description": "Folder id or human path."},
                    "name": {"type": "string", "description": "New name (max 100 chars)."},
                },
                "required": ["folder", "name"],
            },
        },
        {
            "name": "artifact_folder_move",
            "description": (
                "Reparent an artifact-library folder (nest it under another, or move "
                "to the top level). Cycle-guarded — a folder cannot become its own "
                "descendant. ``folder`` and ``new_parent`` are each a folder id or "
                "human path; omit ``new_parent`` (or pass 'root') to move to top level."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "folder": {"type": "string", "description": "Folder to move (id or path)."},
                    "new_parent": {
                        "type": "string",
                        "description": "Destination parent folder (id or path). Omit / 'root' for top level.",
                    },
                },
                "required": ["folder"],
            },
        },
        {
            "name": "artifact_folder_delete",
            "description": (
                "Delete an artifact-library folder. By default (delete_contents=false) "
                "this is SAFE: the folder's direct child folders and artifacts are "
                "re-parented up to the folder's parent, and only the folder itself is "
                "removed. Pass delete_contents=true to permanently delete the entire "
                "subtree, INCLUDING every descendant artifact — echo the affected "
                "count to the user before doing so. ``folder`` = folder id or human path."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "folder": {"type": "string", "description": "Folder id or human path."},
                    "delete_contents": {
                        "type": "boolean",
                        "description": (
                            "false (default) = keep artifacts, re-parent to the folder's "
                            "parent. true = permanently delete the whole subtree."
                        ),
                    },
                },
                "required": ["folder"],
            },
        },
        {
            "name": "artifact_move",
            "description": (
                "Move an existing artifact into a folder (or unfile it). ``folder`` = "
                "a folder id, a '/'-separated human path (missing segments auto-created), "
                "or ''/'root' to unfile. Metadata-only — does not change the artifact's "
                "content or version."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string", "description": "Artifact slug to move."},
                    "folder": {
                        "type": "string",
                        "description": "Destination folder id or human path; ''/'root' to unfile.",
                    },
                },
                "required": ["slug"],
            },
        },
        {
            "name": "deploy_artifact",
            "description": (
                "Preview a deploy of a webapp artifact or local directory to a "
                "public URL on the user's AWS account. This tool is PREVIEW-ONLY: "
                "it returns scan status and deploy details but never executes. "
                "Final confirmation happens in the dashboard Artifact Deploy page. "
                "Restricted-session guard and SEL audit apply identically to the "
                "HTTP endpoint."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "site_id": {
                        "type": "string",
                        "description": "Deploy slot name (e.g. 'my-app').",
                    },
                    "artifact_slug": {
                        "type": "string",
                        "description": (
                            "Slug of a static artifact (widget/html/markdown) "
                            "to deploy — its content is rendered as a page. "
                            "kind=webapp artifacts are rejected (their content "
                            "is an app summary, not deployable HTML — deploy "
                            "the app's built directory via local_dir instead). "
                            "Mutually exclusive with local_dir."
                        ),
                    },
                    "local_dir": {
                        "type": "string",
                        "description": (
                            "Validated absolute path to a static directory "
                            "(e.g. fullstack app's public/ root). Mutually "
                            "exclusive with artifact_slug."
                        ),
                    },
                    "profile": {
                        "type": "string",
                        "description": "AWS profile override (default: registry default).",
                    },
                    "ttl_hours": {
                        "type": "integer",
                        "description": "Hours until auto-cleanup, 0-8760 (default: 72; 0 = persistent).",
                    },
                },
                "required": ["site_id"],
            },
        },
        {
            "name": "autonudge_stop",
            "description": (
                "Stop the auto-nudge loop driving your current session. Call this "
                "when you determine the loop should halt (e.g. goal complete, "
                "blocked on user input, or a STOP sentinel file indicates shutdown). "
                "Removes the loop from the AutoNudgeService so no further nudges "
                "fire into this session. Safe to call even if no loop is active — "
                "returns a no-op message."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why the loop is being stopped (logged for audit)",
                    },
                },
            },
        },
        {
            "name": "monitor_start",
            "description": (
                "Start a monitoring loop on YOUR CURRENT session: after each of "
                "your turns completes and the session sits idle for "
                "interval_secs, the given message is re-injected into this same "
                "session as your next turn — same context, same tools, same "
                "conversation. Works from dashboard chat, Slack threads, and "
                "Discord DMs. Use when the user asks to babysit / monitor / "
                "keep checking something (a PR, CI run, ticket, deployment): "
                "put the check instructions and the exit condition in the "
                "message, then END YOUR TURN — the loop wakes you on the "
                "interval. When the exit condition is met (or the user says "
                "stop), call autonudge_stop. One loop per session; starting a "
                "new one replaces the old. Survives gateway restarts."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": (
                            "The recurring instruction to re-inject each cycle, "
                            "including what to check and when to stop (max 8000 chars)"
                        ),
                    },
                    "interval_secs": {
                        "type": "integer",
                        "description": (
                            "Idle seconds between cycles (15-86400, default 300)"
                        ),
                    },
                    "max_cycles": {
                        "type": "integer",
                        "description": (
                            "Safety cap on delivered cycles; 0 = unlimited (default 0)"
                        ),
                    },
                },
                "required": ["message"],
            },
        },
        {
            "name": "local_knowledge_search",
            "description": (
                "Search the user's knowledge library. Call ONLY when the user's "
                "message contains one of these explicit signals:\n"
                "- Asks 'what do we know about X' or 'check knowledge for X'\n"
                "- References a specific document, wiki, or stored content by name\n"
                "- Says 'in my docs', 'in my notes', 'according to our knowledge'\n"
                "- Asks a factual question AND mentions a topic you know is in "
                "their knowledge base\n\n"
                "Do NOT call for: general coding questions, file operations, "
                "debugging, or any task you can answer from context alone."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query to find relevant knowledge chunks",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 3, max 5)",
                        "default": 3,
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "knowledge_dedup",
            "description": (
                "Find and collapse cross-source duplicate documents in the Knowledge "
                "Base (e.g. the same file uploaded directly AND synced via a folder). "
                "Defaults to a DRY-RUN preview that lists which duplicate would be "
                "deleted and which copy is kept, changing nothing. Pass apply=true to "
                "perform the hard deletes. Use when the user asks to de-duplicate, "
                "clean up, or preview duplicates in their knowledge base."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "apply": {
                        "type": "boolean",
                        "description": (
                            "false (default) = dry-run preview, no changes. "
                            "true = perform the hard deletes."
                        ),
                        "default": False,
                    },
                },
            },
        },
        {
            "name": "search_chat_history",
            "description": (
                "Search your own past conversation transcripts (chat history) by "
                "keyword and get back ranked, snippet-level hits. Use this to "
                "recover context that is NOT in your injected memory — e.g. 'what "
                "did we decide about X three weeks ago', 'the error message from "
                "that debugging session', a name/number/path mentioned earlier. "
                "Search like a human: try a query, read the snippets, then re-search "
                "with different keywords if the first hit isn't right. Returns "
                "metadata + a short snippet per session (NOT full transcripts) — "
                "call get_chat_session with a returned session_key to read the full "
                "thread once a hit looks promising. Scoped to your current workspace "
                "by default. This is a READ — it never modifies memory or history."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keyword(s) to search for in past conversations.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 10, max 50).",
                        "default": 10,
                    },
                    "before": {
                        "type": "string",
                        "description": "Optional ISO date (YYYY-MM-DD); only sessions modified before this day.",
                    },
                    "after": {
                        "type": "string",
                        "description": "Optional ISO date (YYYY-MM-DD); only sessions modified on/after this day.",
                    },
                    "all_workspaces": {
                        "type": "boolean",
                        "description": "Search across all workspaces instead of just the current one (default false).",
                        "default": False,
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "get_chat_session",
            "description": (
                "Read the full message transcript of one past conversation, "
                "identified by a session_key returned from search_chat_history. "
                "Returns the messages as role/content pairs, tail-capped at "
                "max_messages. Use after search_chat_history when a snippet hit "
                "looks like the thread you need. Refuses incognito/temporary "
                "sessions. This is a READ — it never modifies memory or history."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_key": {
                        "type": "string",
                        "description": "The session_key from a search_chat_history result.",
                    },
                    "max_messages": {
                        "type": "integer",
                        "description": "Max (most recent) messages to return (default 50, max 200).",
                        "default": 50,
                    },
                    "all_workspaces": {
                        "type": "boolean",
                        "description": "Allow reading a session from a different workspace than the caller's (default false — deny cross-workspace).",
                        "default": False,
                    },
                },
                "required": ["session_key"],
            },
        },
        {
            "name": "list_sessions",
            "description": (
                "List your recent conversation sessions in this workspace so you "
                "can see the work in flight and what you've been doing — titles, "
                "owning agent, message volume, and last-activity time, newest "
                "first. Use this when the user asks 'what are you working on?', "
                "'what sessions are open?', 'what have we been doing?', or when you "
                "need a bird's-eye view of your own workspace before acting. This "
                "is a READ — it never modifies memory or history. It complements "
                "search_chat_history (which finds a specific past thread by "
                "keyword): list_sessions is the browse/overview, search is the "
                "lookup. Scoped to your current workspace by default; "
                "incognito/temporary sessions are never listed."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max sessions to return, newest first (default 20, max 100).",
                        "default": 20,
                    },
                    "all_workspaces": {
                        "type": "boolean",
                        "description": "List sessions across all workspaces instead of just the current one (default false).",
                        "default": False,
                    },
                    "summarize": {
                        "type": "boolean",
                        "description": (
                            "When true, generate a fresh one-line LLM summary for the top "
                            "sessions (bounded, best-effort — costs tokens + latency, so it's "
                            "opt-in). When false (default), the existing session title is used "
                            "with zero cost."
                        ),
                        "default": False,
                    },
                },
            },
        },
        {
            "name": "browse_outline",
            "description": (
                "Compress a browser snapshot into a compact outline with element refs. "
                "Use AFTER calling browser_snapshot to reduce a large accessibility tree "
                "(50-100K tokens) into a navigable outline (~2-5K tokens). "
                "Returns interactive elements with refs for clicking, plus page structure."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "snapshot": {
                        "type": "string",
                        "description": "The raw browser_snapshot output text to compress",
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "Max output lines (default 100)",
                        "default": 100,
                    },
                },
                "required": ["snapshot"],
            },
        },
        {
            "name": "browse_search",
            "description": (
                "Search a browser snapshot for specific text or patterns. "
                "Returns matching lines with element refs. Use instead of reading "
                "the full snapshot when looking for specific content on a page."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "snapshot": {
                        "type": "string",
                        "description": "The raw browser_snapshot output text to search",
                    },
                    "query": {
                        "type": "string",
                        "description": "Text or regex pattern to search for",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max matching lines to return (default 50)",
                        "default": 50,
                    },
                },
                "required": ["snapshot", "query"],
            },
        },
        {
            "name": "set_project",
            "description": (
                "Set the calling chat slot's project directory. The directory scopes "
                "file search, @-mention auto-complete, the [PROJECT] context line, "
                "and project-level .kiro/steering/**/*.md. "
                "\n\n"
                "Use after a skill scaffolds a new working tree (e.g. a new workspace) "
                "so the agent retargets to the new source instead of the old one. "
                'To clear the project, pass path="" with clear=true. '
                "\n\n"
                "Restrictions: only works in dashboard sessions with explicit identity "
                "(injected KIROCREW_SESSION_KEY or per-call caller context). Subagents, "
                "Slack, and cron contexts are rejected — those resolve via PID-walk and "
                "would silently mutate the wrong slot. Sensitive paths (~/.aws, ~/.ssh, "
                "etc.) are blocked by the underlying endpoint. "
                "\n\n"
                "The session is reset on the NEXT turn boundary (not inline) so this "
                "tool returns cleanly without killing its own caller."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Absolute path to the new project directory. "
                            "Must be non-empty unless clear=true."
                        ),
                    },
                    "clear": {
                        "type": "boolean",
                        "description": "Set true to clear the project scope (path must be empty).",
                    },
                },
                "required": ["path"],
            },
        },
        # --- Dynamic workflows (M6): author + run + monitor from chat ---
        {
            "name": "workflow_author",
            "description": (
                "Turn a natural-language goal into a runnable DYNAMIC WORKFLOW "
                "Python script (orchestrates agents via a sandboxed `ctx` DSL). "
                "Returns the validated script source — then call workflow_run to "
                "execute it. (Usually you can skip this and pass `intent` straight to "
                "workflow_run, which authors+runs in one step.)"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "description": "The goal in plain language, e.g. 'deep research on the origin of pizza'",
                    },
                },
                "required": ["intent"],
            },
        },
        {
            "name": "workflow_run",
            "description": (
                "★ THE tool for 'use a dynamic workflow to …' / 'run a workflow' / any "
                "multi-phase, monitorable, restartable agent orchestration. PREFER THIS "
                "over spawn_sub_agents for such requests. Just pass `intent` (the user's "
                "goal in plain words) and it authors + launches the workflow in one step "
                "— do NOT hand-roll the orchestration with spawn tools. Returns a run_id "
                "immediately; the run streams to the Workflows dashboard tab and its "
                "result is injected back into this chat on completion. Monitor with "
                "workflow_status / workflow_result; restart parts with "
                "workflow_rerun_subtree."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Workflow script source (Python)"},
                    "intent": {
                        "type": "string",
                        "description": "If no source: a NL goal to author then run",
                    },
                    "name": {"type": "string", "description": "Optional run name"},
                    "args": {
                        "type": "object",
                        "description": "Optional args passed to the workflow",
                    },
                    "budget_total": {
                        "type": "integer",
                        "description": "Optional token budget ceiling for the run",
                    },
                },
            },
        },
        {
            "name": "workflow_status",
            "description": (
                "Get the live status of a background workflow run by run_id "
                "(running/finished/failed/cancelled + agent/event counts). Use to "
                "monitor a run you started; for the full result use workflow_result."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"run_id": {"type": "string"}},
                "required": ["run_id"],
            },
        },
        {
            "name": "workflow_result",
            "description": (
                "Get a workflow run's full result + event stream by run_id "
                "(phases, per-agent outcomes, logs, final result)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"run_id": {"type": "string"}},
                "required": ["run_id"],
            },
        },
        {
            "name": "workflow_list",
            "description": "List recent background workflow runs (newest first) with their status.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "workflow_cancel",
            "description": "Cancel a running background workflow by run_id.",
            "inputSchema": {
                "type": "object",
                "properties": {"run_id": {"type": "string"}},
                "required": ["run_id"],
            },
        },
        {
            "name": "workflow_rerun_subtree",
            "description": (
                "Re-run a prior workflow, REPLAYING the unchanged prefix and "
                "re-executing from a chosen step ('restart parts' at runtime). "
                "Agent calls before `from_index` reuse the prior run's cached "
                "results; calls at/after re-call the model. from_index=0 re-runs "
                "everything fresh. Returns a new run_id."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "The prior run to restart from"},
                    "from_index": {
                        "type": "integer",
                        "description": "Agent call_index to restart at (0 = full re-run)",
                        "default": 0,
                    },
                },
                "required": ["run_id"],
            },
        },
    ]


def _internal_secret() -> str:
    """Read the per-session secret for IPC authentication."""
    try:
        return (config_dir() / ".local_secret").read_text().strip()
    except Exception:
        return ""


# Cached user-scoped token for routes that reject ``X-Internal-Secret`` and
# require a real session token (e.g. ``/api/autonudge*``). Bootstrapped on
# demand from ``/api/token/local`` using the same local secret we already hold,
# and refreshed once it nears expiry. ``(token, expires_at_monotonic)``.
_USER_TOKEN_CACHE: tuple[str, float] = ("", 0.0)


def _local_user_token() -> str:
    """Exchange the local secret for a short-lived user-scoped token.

    A few routes (notably ``/api/autonudge*``) deliberately reject the
    machine-to-machine ``X-Internal-Secret`` handshake and require a
    user-scoped token instead. ``GET /api/token/local`` mints one for any
    loopback caller that presents the local secret via the ``X-Local-Secret``
    header. We cache the token in-process and refresh it shortly before
    expiry so a self-halting loop doesn't pay the round-trip every call.

    Returns ``""`` if the exchange fails; callers surface that as the usual
    ``{"error": ...}`` path rather than crashing.
    """
    global _USER_TOKEN_CACHE
    cached, expires_at = _USER_TOKEN_CACHE
    # 30s safety margin so a token doesn't expire mid-request.
    if cached and time.monotonic() < expires_at - 30:
        return cached
    secret = _internal_secret()
    if not secret:
        return ""
    req = urllib.request.Request(
        f"{_API}/api/token/local?ttl=15m",
        headers={"X-Local-Secret": secret},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception:
        return ""
    token = str(data.get("token", ""))
    if not token:
        return ""
    ttl = float(data.get("expires_in", 900) or 900)
    _USER_TOKEN_CACHE = (token, time.monotonic() + ttl)
    return token


def _ppid_via_libproc(pid: int) -> int:
    """macOS parent-PID lookup via libproc's ``proc_pidinfo`` (stdlib ctypes).

    macOS has no ``/proc``, and the app sandbox denies spawning ``ps``
    (``Operation not permitted``). ``proc_pidinfo`` is an information syscall
    (no ``exec``), so the sandbox allows it — the same primitive psutil uses,
    but with zero third-party dependency. Returns 0 on any failure so the caller
    can fall back.
    """
    import ctypes
    import struct

    proc_pidtbsdinfo = 3
    # sizeof(struct proc_bsdinfo) is 232 on 64-bit Darwin; over-allocate.
    buf_size = 256
    try:
        libproc = ctypes.CDLL("libproc.dylib", use_errno=True)
        libproc.proc_pidinfo.restype = ctypes.c_int
        libproc.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        buf = ctypes.create_string_buffer(buf_size)
        n = libproc.proc_pidinfo(pid, proc_pidtbsdinfo, 0, buf, buf_size)
        # pbi_ppid is the 5th uint32 (offset 16); need at least that many bytes.
        if n <= 16:
            return 0
        # struct proc_bsdinfo starts: pbi_flags, pbi_status, pbi_xstatus,
        # pbi_pid, pbi_ppid (5 x uint32) — pbi_ppid is index 4.
        return int(struct.unpack_from("<5I", buf.raw, 0)[4])
    except Exception:
        return 0


def _get_ppid(pid: int) -> int:
    """Get parent PID cross-platform. Returns 0 on failure.

    Standard-library only — deliberately NO third-party dependency (e.g.
    psutil), so the shipped app needs nothing extra bundled or code-signed and
    works across OS versions out of the box.

    - Linux: read ``/proc/<pid>/status`` (plain file read).
    - macOS: ``proc_pidinfo`` via libproc (see ``_ppid_via_libproc``). The old
      code shelled out to ``ps`` here, which the macOS app sandbox denies
      (``Operation not permitted``) — that broke the ancestor PID-walk in
      ``_resolve_session_key``, leaving spawned sub-agents unable to resolve
      their parent session key (empty ``KIROCREW_SESSION_KEY``) and surfacing
      spurious tool-approval cards on trusted sessions. libproc needs no
      ``exec``, so it works under the sandbox.
    - Other/unknown platforms: fall back to ``ps`` (may be blocked, then 0).
    """
    system = platform.system()
    try:
        if system == "Linux":
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("PPid:"):
                    return int(line.split()[1])
        elif system == "Darwin":
            ppid = _ppid_via_libproc(pid)
            if ppid:
                return ppid
        # Last-resort fallback (unknown platform, or a libproc/proc miss): ``ps``.
        # May be sandbox-blocked, in which case this raises and we return 0.
        out = subprocess.check_output(["ps", "-o", "ppid=", "-p", str(pid)], text=True, timeout=2)
        return int(out.strip())
    except Exception:
        pass
    return 0


# ── Knowledge-search store/embedder cache ──
#
# local_knowledge_search runs per LLM tool call in a long-lived MCP server.
# Rebuilding KnowledgeStore every call re-runs the schema DDL, an orphan-cleanup
# DELETE transaction, and a full SELECT of all entities/relations into the
# in-memory graph; rebuilding the embedder re-runs the model availability probe
# (up to 3s when configured). We cache both, keyed on a signature of the DB
# files (main + -wal, since WAL commits land in -wal) and config.json, so
# out-of-band dashboard ingestion or config edits trigger a rebuild on the next
# call. The MCP stdio loop services calls serially, but a lock keeps this safe
# if that ever changes.
_KNOWLEDGE_CACHE_LOCK = threading.Lock()
# (signature_tuple, KnowledgeStore, embedder_or_None)
_KNOWLEDGE_CACHE: tuple[tuple, Any, Any] | None = None


def _knowledge_db_signature(db_path: Path, cfg_path: Path) -> tuple:
    """Cheap fingerprint of the knowledge DB (+WAL) and config files.

    Any ingestion (which writes the main DB or its -wal sidecar) or config edit
    changes this, busting the cache so a fresh search sees new data / embedder.
    """
    sig: list = []
    wal_path = db_path.with_name(db_path.name + "-wal")
    for p in (db_path, wal_path, cfg_path):
        try:
            st = p.stat()
            sig.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((str(p), None))
    return tuple(sig)


def _get_knowledge_search(db_path: Path, cfg_path: Path) -> tuple[Any, Any]:
    """Return a cached ``(KnowledgeStore, embedder)`` pair, rebuilding on change.

    Rebuilds (and closes the prior connection) only when the DB/WAL/config
    signature changes; otherwise reuses the live store + embedder, avoiding the
    per-call schema/migrate/graph-load and embedder availability probe.
    """
    global _KNOWLEDGE_CACHE
    sig = _knowledge_db_signature(db_path, cfg_path)
    with _KNOWLEDGE_CACHE_LOCK:
        if _KNOWLEDGE_CACHE is not None and _KNOWLEDGE_CACHE[0] == sig:
            return _KNOWLEDGE_CACHE[1], _KNOWLEDGE_CACHE[2]
        # Rebuild. Build the new store FIRST; only close the stale connection
        # after the build succeeds. If KnowledgeStore.__init__ raises (locked or
        # corrupt DB, disk-full during the migrate DELETE), we leave the existing
        # cache entry — and its still-open connection — intact rather than
        # stranding a closed connection in the cache for the next caller.
        prev = _KNOWLEDGE_CACHE
        store = KnowledgeStore(str(db_path))
        try:
            cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
        except Exception:
            cfg = {}
        embedder = create_embedder_from_config(cfg)
        # Close the stale connection only AFTER the full rebuild (store + cfg +
        # embedder) succeeds. If any step above raised, the existing cache entry
        # — and its open connection — is left intact and usable for the next call.
        if prev is not None:
            with contextlib.suppress(Exception):
                prev[1].db.close()
        # Re-fingerprint AFTER building: KnowledgeStore.__init__ creates/migrates
        # the DB (writing the file + -wal), so the pre-build signature no longer
        # matches the on-disk state. Caching under the post-build signature lets
        # the next idle call hit the cache instead of rebuilding every time.
        post_sig = _knowledge_db_signature(db_path, cfg_path)
        _KNOWLEDGE_CACHE = (post_sig, store, embedder)
        return store, embedder


def _resolve_session_key() -> str:
    """Return the real session key, falling back to PID file when env var is absent.

    Warm-pool kiro-cli processes have no KIROCREW_SESSION_KEY env var (the pool
    spawns with an empty key so rekey() + PID file provide the correct mapping).

    After rekey, the process tree may be: gateway -> kiro-cli (pool, has PID file)
    -> kiro-cli-chat (forked child) -> MCP server.  os.getppid() returns the
    immediate parent (kiro-cli-chat) which has no PID file.  Walk up ancestors
    until we find a matching file or hit init.
    """
    sk = os.environ.get("KIROCREW_SESSION_KEY", "")
    if sk:
        return sk
    try:
        from kiro_crew.session_pid_sig import read_session_pid_txt

        cfg_dir = config_dir()
        # Sandbox launcher exports its own HOST pid (the pid the gateway keys
        # session_pid files by) — direct lookup works even when this
        # process's pid view diverges from the host's (PID-namespace
        # sandboxing), where the ancestor walk below can never match.
        # Reads go through session_pid_sig's hardened reader (symlink
        # refusal, regular-file check, size bound) — same read discipline
        # as the strict verifier, minus the signature requirement.
        host_pid = os.environ.get("KIROCREW_HOST_PID", "")
        if host_pid.isdigit():
            key = read_session_pid_txt(host_pid, cfg_dir)
            if key:
                return key
        pid = os.getppid()
        seen: set[int] = set()
        while pid > 1 and pid not in seen:
            seen.add(pid)
            key = read_session_pid_txt(pid, cfg_dir)
            if key:
                return key
            pid = _get_ppid(pid)
    except Exception:
        pass
    return ""


def _resolve_session_key_strict() -> str:
    """Resolve the session key, refusing PID-walked and unsigned identities.

    Like ``_resolve_session_key`` but drops the ``/proc`` ancestor walk.
    Two identity sources are accepted:

    1. The gateway-injected ``KIROCREW_SESSION_KEY`` env var.
    2. The direct ``KIROCREW_HOST_PID`` -> ``session_pid_<pid>.txt``
       lookup, but ONLY when the HMAC sidecar written by the gateway
       verifies (:func:`kiro_crew.session_pid_sig.verify_session_pid`).
       PID-namespace sandboxing strips ``KIROCREW_SESSION_KEY`` from the
       sandboxed env, but the sandbox launcher exports its OWN host pid
       (``sandbox.py``) — exactly the pid the gateway keys
       ``session_pid_<pid>.txt`` by on session claim. The bare ``.txt``
       file is agent-writable and therefore forgeable; the sidecar is
       signed with the SEL trust root (``sel_hmac.key``), which agents
       cannot read, and binds the pid into the MAC so another pid's
       pair cannot be replayed. Without this branch,
       ``monitor_start``/``autonudge_stop``/``set_project`` fail closed
       in every sandboxed dashboard session even though the session is
       fully identified.

    Returns ``""`` when only the ``/proc`` ancestor WALK would have
    matched, or when the sidecar is missing/invalid. The walk stays
    excluded: a subagent spawned via ``spawn_run`` lives under the
    parent slot's process tree, so walking ancestors from its MCP-core
    child silently resolves to the parent — which would let the
    subagent mutate state on the wrong slot. Read-only callers (audit,
    telemetry) keep the lenient resolver where misattribution is
    harmless.
    """
    sk = os.environ.get("KIROCREW_SESSION_KEY", "")
    if sk:
        return sk
    try:
        host_pid = os.environ.get("KIROCREW_HOST_PID", "")
        if host_pid.isdigit():
            from kiro_crew.session_pid_sig import verify_session_pid

            return verify_session_pid(host_pid)
    except Exception:
        pass
    return ""


def _vet_messaging_governance(caller_session: str) -> str | None:
    """Return a denial reason if governance forbids outbound messaging, else None.

    Proactive/outbound messaging is a ``capabilities.messaging`` gate (an exfil
    surface a policy/profile may disable per surface/app).  Runs in the
    ``kirocrew-core`` stdio subprocess, which DOES boot the platform via
    ``cli.main`` — so ``current_context()`` carries the ceiling.  Best-effort:
    a ``PlatformCompositionError`` propagates; any other error returns None.
    Emits no stray stdout/stderr (either would corrupt the JSON-RPC stream); a
    fail-open degrade is audited via the file-backed ``governance_degraded`` SEL
    only (``log_warning=False`` suppresses the logger here).
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        decision = governance_permits(
            "capabilities.messaging",
            "",
            session_key=caller_session,
            app=_governance_app(),
            log_warning=False,
        )
        if not getattr(decision, "permitted", True):
            _audit_governance_deny(
                caller_session, "send_message", "capabilities.messaging", decision
            )
            return "outbound messaging blocked by governance policy"
        return None
    except PlatformCompositionError:
        raise
    except Exception:
        # No logger here: this runs inside the kirocrew-core stdio MCP server,
        # whose stray stdout/stderr would corrupt the JSON-RPC stream (same
        # constraint as redact_via_context). Still emit the file-backed
        # governance_degraded SEL (no stdout) so the fail-open is auditable.
        # Wrapped so a late-import failure cannot raise ImportError out of this
        # except-branch and hard-fail the stdio tool call.
        try:
            from kiro_crew.platform.governance_profiles import audit_governance_degraded

            audit_governance_degraded(
                "send_message",
                session_key=caller_session,
                scope="capabilities.messaging",
                app=_governance_app(),
                log_warning=False,
            )
        except Exception:
            pass
        return None


def _vet_channel_governance(caller_session: str, transport: str) -> str | None:
    """Return a denial reason if governance forbids messaging *via transport*.

    The ``channels`` scope (a ScopedMap) is the per-transport allowlist: which
    chat transports (``slack``, future ``discord``/``telegram``) outbound
    messaging may use.  It is finer-grained than the on/off
    ``capabilities.messaging`` gate above — a policy may permit messaging
    generally but restrict it to specific transports (e.g. Slack only).  We
    query the ScopedMap ``members`` allowlist for *transport*.  ``posture`` (the
    per-transport identity ceiling, policy-only) is enforced at the transport's
    own admission path, not here.  Same stdio-silent, fail-closed-CPP discipline
    as :func:`_vet_messaging_governance`.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        # A bare member id queries the ScopedMap ``members`` ruleset.
        decision = governance_permits(
            "channels",
            transport,
            session_key=caller_session,
            app=_governance_app(),
            log_warning=False,
        )
        if not getattr(decision, "permitted", True):
            _audit_governance_deny(
                caller_session, f"send_message:{transport}", "channels", decision
            )
            return f"messaging via transport {transport!r} blocked by governance policy"
        return None
    except PlatformCompositionError:
        raise
    except Exception:
        # Wrapped: a late-import failure must not hard-fail the stdio tool call.
        try:
            from kiro_crew.platform.governance_profiles import audit_governance_degraded

            audit_governance_degraded(
                f"send_message:{transport}",
                session_key=caller_session,
                scope="channels",
                app=_governance_app(),
                log_warning=False,
            )
        except Exception:
            pass
        return None


def _audit_governance_deny(session_key: str, tool_name: str, scope: str, decision: object) -> None:
    """Best-effort SEL audit of a governance denial (writes to the JSONL file,
    NOT stdout — safe in the stdio MCP server). Never raises."""
    try:
        from kiro_crew.sel import sel

        sel().log_governance_decision(
            session_key=session_key,
            tool_name=tool_name,
            scope=scope,
            outcome="denied",
            rule=getattr(decision, "rule", ""),
            layer=getattr(decision, "layer", ""),
            reason=getattr(decision, "reason", ""),
        )
    except Exception:
        # No stdout/stderr in the stdio server; SEL writes to a file so this is
        # safe, but a failure here must never wedge the deny path.
        pass


def _governance_app() -> str:
    """Best-effort active app slug for per-app profile binding, or "".

    An app backend process carries ``KIROCREW_APP_NAME`` (set in
    ``apps.backend.start_app_backend``); when an app's own tool call reaches a
    governance chokepoint in-process, this lets a per-app profile
    (``bind:{type:"app"}``) resolve.  NOTE: the managed ``kirocrew-core`` MCP
    server is spawned by kiro-cli, NOT by an app backend, so this env var is
    absent there — a per-app profile is therefore only reachable for in-app tool
    calls today, not for the agent's MCP-routed ``learn_add``/``send_message``
    (those still resolve the per-SURFACE profile + policy ceiling, which is the
    enforced path).  Returns "" when not in an app context.
    """
    return os.environ.get("KIROCREW_APP_NAME", "")


def _vet_memory_writes_governance(caller_session: str) -> str | None:
    """Return a denial reason if governance forbids durable memory writes, else None.

    A durable memory/lesson write (``learn_add`` → persisted lesson) is an
    instruction-injection surface: content written here is re-injected into
    every future session's context.  The ``capabilities.memory_writes`` gate
    (default ON in the catalog) lets a policy/profile forbid it for a surface/app
    (e.g. a sandboxed app must not be able to plant a durable instruction).  Same
    stdio-silent, fail-closed-CPP discipline as :func:`_vet_messaging_governance`.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        decision = governance_permits(
            "capabilities.memory_writes",
            "",
            session_key=caller_session,
            app=_governance_app(),
            log_warning=False,
        )
        if not getattr(decision, "permitted", True):
            _audit_governance_deny(
                caller_session, "learn_add", "capabilities.memory_writes", decision
            )
            return "durable memory writes blocked by governance policy"
        return None
    except PlatformCompositionError:
        raise
    except Exception:
        # Wrapped: a late-import failure must not hard-fail the stdio tool call.
        try:
            from kiro_crew.platform.governance_profiles import audit_governance_degraded

            audit_governance_degraded(
                "learn_add",
                session_key=caller_session,
                scope="capabilities.memory_writes",
                app=_governance_app(),
                log_warning=False,
            )
        except Exception:
            pass
        return None


def _session_key_header_error(sk: str) -> str | None:
    """Return an actionable error if the session key cannot go in an HTTP header.

    http.client encodes header values as latin-1, so a non-latin-1 char in the
    session key (e.g. an em-dash from a tab title) raises UnicodeEncodeError
    before the request is sent. Detect it up front and tell the user to rename
    the tab, rather than surfacing the raw codec error.
    """
    try:
        sk.encode("latin-1")
        return None
    except UnicodeEncodeError:
        return (
            "session key contains a character invalid in HTTP headers "
            "(non-latin-1, e.g. an em-dash or emoji in the tab title) — "
            "rename the chat tab to use ASCII characters and retry"
        )


def _post(path: str, body: dict | None = None, *, timeout: float = 30) -> dict:
    data = json.dumps(body or {}).encode()
    headers = {"Content-Type": "application/json", "X-Internal-Secret": _internal_secret()}
    sk = _resolve_session_key()
    _sk_err = _session_key_header_error(sk)
    if _sk_err:
        return {"error": _sk_err}
    if sk:
        headers["X-Session-Key"] = sk
    req = urllib.request.Request(
        f"{_API}{path}",
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- URL is the loopback gateway (_API from dashboard.url config) + a fixed internal path; never user-controlled  # noqa: E501
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # urlopen raises HTTPError on 4xx/5xx; str(e) is only "HTTP Error 400:
        # Bad Request" — the structured {"error": ...} body lives in e.read().
        # Surface it so callers can act on the backend's actual error (e.g.
        # the learn_add "unknown session" mapping) instead of an opaque code.
        return _http_error_body(e)
    except urllib.error.URLError as e:
        if isinstance(e.reason, (ConnectionRefusedError, socket.gaierror)):
            return {"error": str(e)}
        # ``transport_error`` is consumed only by spawn_run's batch reconcile:
        # it means acceptance is unknown, so that member must not be declared
        # lost. Other _post callers should treat the payload as a normal error.
        return {"error": str(e), "transport_error": True}
    except Exception as e:
        # The request may have reached the gateway before the response failed
        # (for example, a read timeout after spawn acceptance). Callers must
        # not present this as a definite rejection or retry automatically.
        return {"error": str(e), "transport_error": True}


def _http_error_body(exc: urllib.error.HTTPError) -> dict:
    """Decode the JSON body of an ``HTTPError`` into the standard error dict.

    Prefers the structured ``{"error": ...}`` JSON body (so callers can match
    on the backend's actual message), then the raw body text, then
    ``str(exc)`` — so a non-JSON or empty error response still yields a usable
    ``{"error": ...}`` payload instead of an opaque ``"HTTP Error 400"``.

    An HTTP response body is content originating outside KiroCrew, so the
    decoded message is redacted (``redact_exfiltration_urls`` +
    ``redact_credentials``) before it is handed back to a caller that may echo
    it to the LLM / dashboard / Slack. Redaction leaves plain markers like
    ``"unknown session"`` intact, so downstream matching is unaffected.
    """
    try:
        raw = exc.read().decode("utf-8", "replace").strip()
    except Exception:
        raw = ""
    message = raw or str(exc)
    counted = False
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and "error" in parsed:
                message = str(parsed["error"])
                # Preserve api_spawn's "this rejection was already counted"
                # marker (wave-liveness reconcile) — it must survive the
                # error-body flattening or spawn_run would double-reconcile
                # in-process rejections and close waves early.
                counted = bool(parsed.get("counted"))
        except Exception:
            pass
    message, _ = redact_exfiltration_urls(message)
    message, _ = redact_credentials(message)
    out: dict = {"error": message}
    if counted:
        out["counted"] = True
    return out


def _get(path: str) -> dict:
    headers = {"X-Internal-Secret": _internal_secret()}
    sk = _resolve_session_key()
    _sk_err = _session_key_header_error(sk)
    if _sk_err:
        return {"error": _sk_err}
    if sk:
        headers["X-Session-Key"] = sk
    req = urllib.request.Request(
        f"{_API}{path}",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return _http_error_body(e)
    except Exception as e:
        return {"error": str(e)}


def _patch(path: str, body: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode()
    headers = {"Content-Type": "application/json", "X-Internal-Secret": _internal_secret()}
    sk = _resolve_session_key()
    _sk_err = _session_key_header_error(sk)
    if _sk_err:
        return {"error": _sk_err}
    if sk:
        headers["X-Session-Key"] = sk
    req = urllib.request.Request(
        f"{_API}{path}",
        data=data,
        headers=headers,
        method="PATCH",
    )
    try:
        # _API is the hardcoded loopback dashboard base and `path` is a code
        # literal — never attacker-controlled, so no file:// scheme risk.
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosemgrep  # noqa: E501
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return _http_error_body(e)
    except Exception as e:
        return {"error": str(e)}


def _delete(path: str, body: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode() if body else None
    headers = {"X-Internal-Secret": _internal_secret()}
    sk = _resolve_session_key()
    _sk_err = _session_key_header_error(sk)
    if _sk_err:
        return {"error": _sk_err}
    if sk:
        headers["X-Session-Key"] = sk
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{_API}{path}",
        data=data,
        headers=headers,
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return _http_error_body(e)
    except Exception as e:
        return {"error": str(e)}


def _with_token(path: str, token: str) -> str:
    """Append ``?token=`` (or ``&token=``) to *path* for user-token routes."""
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}{urlencode({'token': token})}"


def _get_user(path: str) -> dict:
    """GET a user-token-gated route (e.g. ``/api/autonudge*``).

    These routes reject ``X-Internal-Secret``; authenticate with a
    bootstrapped user token passed as the ``?token=`` query param instead.
    """
    token = _local_user_token()
    if not token:
        return {"error": "could not obtain local user token"}
    req = urllib.request.Request(f"{_API}{_with_token(path, token)}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return _http_error_body(e)
    except Exception as e:
        return {"error": str(e)}


def _delete_user(path: str) -> dict:
    """DELETE a user-token-gated route (e.g. ``/api/autonudge/{id}``)."""
    token = _local_user_token()
    if not token:
        return {"error": "could not obtain local user token"}
    req = urllib.request.Request(
        f"{_API}{_with_token(path, token)}",
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return _http_error_body(e)
    except Exception as e:
        return {"error": str(e)}


def _post_user(path: str, body: dict) -> dict:
    """POST JSON to a user-token-gated route (e.g. ``POST /api/autonudge``)."""
    token = _local_user_token()
    if not token:
        return {"error": "could not obtain local user token"}
    req = urllib.request.Request(
        f"{_API}{_with_token(path, token)}",
        method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- _API is the loopback dashboard base resolved from local config and path is code-constructed; no user-controlled URL reaches urlopen (same trust profile as _get_user/_delete_user)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return _http_error_body(e)
    except Exception as e:
        return {"error": str(e)}


def _autonudge_binding_key(sk: str) -> str | None:
    """Map a session key to its AutoNudge binding key, or None if unsupported.

    ``dashboard:chat-N-TS`` → bare slot key ``chat-N-TS`` (the autonudge REST
    layer keys dashboard loops on the bare slot key); ``slack:``/``discord:``
    session keys pass through unchanged (channel-bound loops). Anything else
    (``cron:``, ``hook:``, ``subagent:`` ...) is not a nudge-able session.

    Delegates to ``autonudge.binding_key_for`` so the MCP tool and the workflow
    ``ctx.nudge`` port share one definition of "nudge-able".
    """
    return binding_key_for(sk)


def _artifact_ref_link(slug: str, name: str) -> str:
    """Render a clickable ``[<name>](/artifacts/<slug>)`` markdown link.

    The chat renderer turns this into an anchor the frontend intercepts to open
    the artifact in the side panel; ``/artifacts/<slug>`` is the canonical
    full-page route, so it also degrades to a normal navigation if interception
    is absent. Used for non-widget kinds, which (unlike widgets) don't
    round-trip via ``<mcwidget>`` and otherwise have no clickable form in chat.
    """
    # name/slug are LLM-influenced and rendered verbatim on the dashboard, so
    # scrub for credential / exfiltration patterns (same guard as other
    # tool-result paths).
    label = name or slug
    label, _ = redact_exfiltration_urls(label)
    label, _ = redact_credentials(label)
    # Unescaped ']' would break the markdown link syntax.
    label = label.replace("[", "(").replace("]", ")")
    # A literal newline in the label splits the link text across lines, breaking
    # the single-line markdown anchor — collapse CR/LF to spaces so a crafted
    # name can't fragment the rendered link.
    label = label.replace("\r", " ").replace("\n", " ")
    safe_slug, _ = redact_exfiltration_urls(slug or "")
    safe_slug, _ = redact_credentials(safe_slug)
    # Constrain to the slug charset so a crafted value can't inject ')'/markdown
    # out of the URL.
    safe_slug = _re.sub(r"[^a-z0-9-]", "", safe_slug.lower())
    # If sanitization leaves no slug (e.g. the '?' fallback or an all-redacted
    # value), a link would dangle at /artifacts/ with no target — degrade to
    # plain text so the name still surfaces without a broken anchor.
    if not safe_slug:
        return label
    return f"[{label}](/artifacts/{safe_slug})"


def _resolve_artifact_folder_id(ref: str) -> tuple[str, str | None]:
    """Resolve an artifact-folder reference (id or human path) to a folder id.

    Read-only: fetches ``/api/artifact-folders`` and matches by id, then walks
    ``/``-separated path segments against folder names (case-insensitive). Used
    by the rename/move/delete MCP tools, which must address an existing folder
    (no auto-create — that only happens on save/move to an artifact folder,
    handled server-side). Returns ``(folder_id, error)``; ``""`` = root.
    """
    ref = str(ref or "").strip()
    if not ref or ref.lower() == "root":
        return "", None
    d = _get("/api/artifact-folders")
    if d.get("error"):
        return "", d["error"]
    folders = d.get("folders", [])
    by_id = {f.get("id"): f for f in folders if isinstance(f, dict) and f.get("id")}
    if ref in by_id:
        return ref, None
    segments = [s.strip().lower() for s in ref.split("/") if s.strip()]
    if not segments:
        return "", None
    parent = ""
    cur = ""
    for seg in segments:
        match = next(
            (
                f
                for f in folders
                if str(f.get("parent_id") or "") == parent
                and str(f.get("name", "")).strip().lower() == seg
            ),
            None,
        )
        if match is None:
            safe_ref, _ = redact_exfiltration_urls(ref)
            safe_ref, _ = redact_credentials(safe_ref)
            return "", f"folder not found: {safe_ref}"
        cur = str(match.get("id") or "")
        parent = cur
    return cur, None


def _artifact_reemit_hint(slug: str, name: str, kind: str = "widget") -> str:
    """Render the canonical re-emit-this-artifact-in-chat instruction.

    Appended to artifact_save / artifact_get / artifact_update tool
    responses so the agent has the exact tag string in context at the
    moment it's about to render the artifact in chat. The artifacts
    skill says ``slug=`` is required on every re-emission of a saved
    artifact, but skill rules can be overlooked at emission time —
    session logs confirmed an LLM had the slug in front of
    it twice (artifact_get response + artifact_update response) and
    still emitted ``<mcwidget title="...">`` without the attribute,
    creating a duplicate artifact when the user clicked save.

    The hint reduces this to "copy the tag I just gave you."
    """
    if kind != "widget":
        # Non-widget artifacts (markdown, html, svg, json, text) don't
        # round-trip through `<mcwidget>` — they render via the artifact
        # detail page or MarkdownPanel. No re-emit hint needed.
        return ""
    safe_name = (name or "").replace('"', "'")
    return (
        "When you re-emit this widget in chat, use this exact opening tag\n"
        "(slug attribute is REQUIRED — without it, the user clicking save\n"
        "creates a duplicate artifact):\n\n"
        f'<mcwidget title="{safe_name}" slug="{slug}">'
    )


def _validate_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate tool arguments against schema. Returns cleaned args."""
    schema = MCP_CORE_SCHEMAS.get(name)
    if schema:
        return validate_tool_args(args, schema)
    return args  # tools without schemas (learn_list) pass through


def _current_session_thread_ts() -> str | None:
    """Return the CALLER's Slack thread_ts, or None.

    Thin ``thread_ts | None`` view over :func:`_classify_slack_identity` — see
    that function for the three-state discrimination (``thread`` /
    ``non_slack`` / ``unresolved``) that ``file_send`` uses to fail CLOSED for
    audience when the caller cannot be attributed. This wrapper returns the
    bare thread_ts only for the ``thread`` state and ``None`` otherwise; on its
    own it does NOT distinguish "not a Slack session" from "identity
    unresolved", so callers on the outward-facing send path MUST use
    :func:`_classify_slack_identity` directly to avoid the channel-root
    disclosure hazard (unresolved identity + explicit channel -> channel root).

    Resolution is via :func:`_resolve_session_key_strict`, which accepts ONLY
    the gateway-injected env var or an HMAC-sidecar-verified
    ``KIROCREW_HOST_PID`` lookup. It deliberately drops the ``/proc`` ancestor
    walk and the bare (agent-writable, forgeable) ``.txt`` fallback the lenient
    resolver allows — closing both the forged-pid-file and the subagent->parent
    misresolution paths, and the prior newest-mtime ``session_pid_*.txt`` glob
    that frequently resolved to a DIFFERENT session than the caller.
    """
    state, thread_ts = _classify_slack_identity()
    return thread_ts if state == "thread" else None


def _classify_slack_identity() -> tuple[str, str | None]:
    """Classify the caller's STRICT Slack identity for outward file delivery.

    ``_current_session_thread_ts`` collapses the result to a bare
    ``thread_ts | None``; this returns the underlying THREE-state discrimination
    that ``file_send`` needs to tell "this is not a Slack session" apart from
    "the caller's Slack identity could not be resolved". Collapsing those two
    into a bare ``None`` is a channel-root disclosure hazard: an *unresolved*
    caller that still supplies an explicit tracked channel would upload at the
    CHANNEL ROOT (``thread_ts=None`` + channel), exposing a file meant for one
    thread to the entire channel — a reachable cross-session disclosure that is
    fail-OPEN with respect to audience, not fail-closed. Warm-pool-claimed Slack
    sessions have no strict identity source (the gateway writes the env var /
    HMAC sidecar only at sandbox spawn, not at warm-pool claim), so every one of
    their ``file_send`` calls hits this seam.

    Returns one of:

    * ``("thread", "<bare_ts>")`` — caller is a RESOLVED Slack thread (a
      canonical ``slack:<thread_ts>`` key, converted via
      :func:`messaging.link.legacy_key`, or an already-bare legacy Slack key).
      Deliver threaded to ``thread_ts``.
    * ``("non_slack", None)``     — caller is a RESOLVED non-Slack session
      (``dashboard:``/``discord:``/app/channel/future namespace). It has no
      Slack thread, but its identity IS known, so the handler's authorized
      routing (owner DM, session-map-linked thread, or an explicitly-supplied
      tracked channel) is safe — never a channel-root broadcast for an
      unknown caller.
    * ``("unresolved", None)``    — strict resolution failed (no gateway env var
      and no HMAC-verified host-pid). The caller cannot be attributed, so an
      outward Slack send must fail CLOSED (refuse) rather than broadcast.
    """
    key = _resolve_session_key_strict()
    if not key:
        return ("unresolved", None)
    # Canonical ``slack:<thread_ts>`` -> bare thread_ts.
    bare = legacy_key(key)
    if bare is not None:
        return ("thread", bare)
    # Already-bare legacy Slack thread_ts -> pass through.
    if is_legacy_slack_key(key):
        return ("thread", key)
    # Resolved, but not a Slack thread (dashboard:, discord:, apps, channels,
    # future ns) — identity is known, so downstream routing is authorized.
    return ("non_slack", None)


def _call_tool(name: str, raw_args: dict[str, Any]) -> str:
    return call_tool_with_logging(
        name,
        raw_args,
        _validate_args,
        _call_tool_inner,
        session_key="mcp_core",
        downstream_service="kirocrew-core",
    )


# ── Chat-history search helpers (Phase 1: search_chat_history / get_chat_session) ──

_HISTORY_INCOGNITO_MODES = INCOGNITO_MEMORY_MODES  # canonical set (single source of truth in history.py)
_SNIPPET_RADIUS = 120  # chars of context kept on each side of a match
_SNIPPET_MAX_LEN = 320  # hard cap on a returned snippet
# Upper bound on ranked candidates pulled from the backend per search. Bound to
# the backend's own scan window (imported, not copied) so we consider every
# ranked match (bounded I/O) and post-filtering can't starve a caller whose hits
# rank past a small page — and the two can't silently drift apart.
_SEARCH_HISTORY_SCAN = SEARCH_SCAN_WINDOW


def _history_is_incognito(meta: dict) -> bool:
    """True if a session's memory_mode marks it private (never searchable)."""
    return str(meta.get("memory_mode", "")).lower() in _HISTORY_INCOGNITO_MODES


def _redact_history_output(text: str) -> str:
    """Apply the standard dual redaction to any chat-history tool output.

    Used on EVERY return path (including early-return error strings that echo an
    LLM-supplied session_key) so nothing reaches the dashboard unredacted.

    Routes through the context-aware :func:`redact` shim so the companion's extra
    credential patterns apply to verbatim chat-transcript egress; the Default
    ``CredentialPolicy`` delegates to ``security.redact`` (the same
    exfil-then-credential dual pass), so standalone is byte-for-byte unchanged.
    """
    return redact(text)


def _parse_iso_date_epoch(date_str: str) -> float | None:
    """Parse a YYYY-MM-DD string to a UTC midnight epoch. None on bad input."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


def _ws_bucket(meta_ws: object) -> str:
    """Normalize a session's workspace value to a comparable bucket.

    ``update_metadata`` accepts arbitrary JSON for ``workspace``; a non-string
    (or empty) value must bucket to "default" rather than compare unequal to a
    real workspace name and silently hide the session from its owner.
    """
    return meta_ws if isinstance(meta_ws, str) and meta_ws else "default"


def _caller_workspace(cl: "object", session_key: str) -> str:
    """Resolve the calling session's workspace bucket for scope filtering.

    Read from the caller's own session metadata (normalized via _ws_bucket).
    Known limitation: on a brand-new session whose metadata file has not been
    written yet, this returns "default". A multi-workspace caller in that narrow
    window is scoped to the default bucket (fail-CLOSED — they see fewer results,
    never another workspace's). Fully fixing it needs the gateway to carry the
    workspace in CallerContext (the register payload does not today), so it is
    tracked as a separate gateway change rather than papered over here.
    """
    if not session_key:
        return "default"
    return _ws_bucket(cl.get_metadata(session_key).get("workspace"))  # type: ignore[attr-defined]


_HISTORY_SNIPPET_ROLES = frozenset({"user", "assistant"})


def _casefold_match_span(text: str, needle_cf: str) -> tuple[int, int] | None:
    """Locate *needle_cf* (already casefolded) inside *text* using full casefolding.

    Returns ``(start, end)`` source indices into *text* for the first match, or
    ``None``. Unlike ``re.search(..., re.IGNORECASE)`` — which does only simple
    per-character case mapping — this mirrors ``str.casefold`` so multi-char
    folds (e.g. ``ß`` ↔ ``ss``, ``ﬃ`` ↔ ``ffi``) match, keeping the wrap matcher
    consistent with the ``str.casefold().find`` selection above. ``str.casefold``
    is a per-character homomorphism, so casefolded offsets map back to source
    character boundaries.
    """
    if not needle_cf:
        return None
    # bounds[k] = length of casefold(text[:k]); the running offset into cf_text
    # for each source char boundary, so a casefolded match offset maps back to
    # the source index whose bounds entry equals it.
    bounds = [0]
    for ch in text:
        bounds.append(bounds[-1] + len(ch.casefold()))
    cf_text = text.casefold()
    cf_start = cf_text.find(needle_cf)
    if cf_start < 0:
        return None
    cf_end = cf_start + len(needle_cf)
    # Map casefolded offsets to source char boundaries. A fold that expands
    # length can leave an offset mid-expansion (no exact boundary); fall back to
    # the enclosing boundary so the wrap never splits a source character.
    try:
        start = bounds.index(cf_start)
    except ValueError:
        start = next((k for k in range(len(bounds)) if bounds[k] > cf_start), 1) - 1
    try:
        end = bounds.index(cf_end)
    except ValueError:
        end = next((k for k in range(len(bounds)) if bounds[k] >= cf_end), len(bounds) - 1)
    return start, end


def _extract_history_snippet(messages: list[dict], needle: str) -> str:
    """Return a bounded snippet around the first user/assistant message matching *needle*.

    The matched substring is delimited with ``<<<...>>>``. Returns "" when no
    eligible message content contains the needle (e.g. it only matched the title).
    """
    # Defense-in-depth: an empty/whitespace needle makes str.find return 0 on
    # every message and would wrap meaningless text in <<<>>>. The query is
    # already validated non-empty upstream, but guard here too since this helper
    # is independently callable.
    if not needle.strip():
        return ""
    needle_cf = needle.casefold()
    for m in messages:
        # Only surface user/assistant content (mirror get_chat_session) so the
        # snippet is the human-facing context, not a tool/system trace blob.
        if str(m.get("role", "")).lower() not in _HISTORY_SNIPPET_ROLES:
            continue
        content = m.get("content")
        if not isinstance(content, str) or not content:
            continue
        idx = content.casefold().find(needle_cf)
        if idx < 0:
            continue
        start = max(0, idx - _SNIPPET_RADIUS)
        end = min(len(content), idx + len(needle) + _SNIPPET_RADIUS)
        seg = content[start:end]
        # Redact BEFORE inserting <<<...>>> markers: marker insertion would split
        # a credential/URL token and defeat the contiguous-match redactors, so a
        # query that is a substring of a secret in stored content could leak it.
        seg = _redact_history_output(seg)
        # Locate the match span in the (possibly redacted) original text using the
        # SAME full casefolding as the selection above — a case-insensitive regex
        # does only simple per-char mapping and would miss multi-char folds
        # (ß→ss), leaving a selected-but-unwrapped snippet with no <<<...>>>.
        span = _casefold_match_span(seg, needle_cf)
        if span:
            s, e = span
            seg = seg[:s] + "<<<" + seg[s:e] + ">>>" + seg[e:]
        seg = ("…" if start > 0 else "") + seg + ("…" if end < len(content) else "")
        result = seg[:_SNIPPET_MAX_LEN]
        # If the hard cap sliced through the match delimiters (possible with a
        # long query), re-close so the consumer never sees a dangling "<<<".
        if "<<<" in result and ">>>" not in result:
            result = result[: _SNIPPET_MAX_LEN - 3] + ">>>"
        return result
    return ""


def _format_anchor(anchor: dict) -> str:
    """Format an anchor quote for the artifact_get_comments output.

    Short quotes (≤300 chars) are shown in full. Longer quotes are bookended
    with the first and last 100 chars plus an explicit TRUNCATED marker
    (never ambiguous with literal user text). Offsets are always included
    when available so the agent can locate the range in the document.
    """
    quote = anchor.get("quote", "")
    start = anchor.get("start_offset")
    end = anchor.get("end_offset")
    offset_info = ""
    if start is not None and end is not None:
        offset_info = f", chars {start}:{end}"
    if len(quote) <= 300:
        return f' [on: "{quote}"{offset_info}]'
    head = quote[:100]
    tail = quote[-100:]
    omitted = len(quote) - 200
    return f' [on: "{head}" [TRUNCATED: {omitted} chars omitted' f'{offset_info}] "{tail}"]'


def _call_tool_inner(name: str, args: dict[str, Any]) -> str:
    if name == "spawn_run":
        # Re-validate to make schema enforcement visible at the extraction point.
        # _call_tool() already validates, but defense-in-depth ensures agent/agents
        # are schema-clean even if the call chain changes.
        args = validate_tool_args(args, SPAWN_RUN_SCHEMA)

        tasks = args.get("tasks")
        task = args.get("task")

        # Support both single task and batch tasks
        if tasks and isinstance(tasks, list):
            task_list = [t for t in tasks if isinstance(t, str) and t.strip()]
        elif task:
            task_list = [task]
        else:
            return "Error: task or tasks is required"

        # Read parent session key so completions inject back into this session.
        parent_session = _resolve_session_key()

        # Fire-and-forget — gateway's SubagentManager queues excess tasks
        # and auto-spawns them as slots free up.
        agent = args.get("agent") or ""
        agents_list = args.get("agents") or []
        max_turns = args.get("max_turns") or 0
        cwd = args.get("cwd") or ""
        model = args.get("model") or ""
        if agents_list and len(agents_list) != len(task_list):
            return f"Error: agents length ({len(agents_list)}) must match tasks length ({len(task_list)})"

        agent_ids: list[str] = []
        agent_names: list[str] = []
        agent_tasks: list[str] = []
        errors: list[str] = []
        transport_errors: list[str] = []
        # Forward this session's own approval_mode (set as an env var at
        # process spawn -- see gateway.py cron dispatch, mirroring
        # KIROCREW_SESSION_KEY/KIROCREW_CHANNEL_ID) so a cron running with
        # approval_mode="auto" deterministically auto-approves its own
        # spawn_run subagent launches. Without this, SubagentManager.spawn's
        # only route to auto-approve is its own parent_trusted lookup, which
        # requires parent_session to resolve back to the cron's session key
        # -- an identity-plumbing path that can fail silently and leave the
        # spawn stuck on the interactive approval path a cron has no
        # responder for.
        approval_mode = os.environ.get("KIROCREW_APPROVAL_MODE", "")
        # Batch/wave identity: one id per multi-task spawn_run call so the
        # gateway can digest completions (one injection turn per wave instead
        # of N) and emit batch lifecycle events at 60-100-agent scale.
        batch_id = uuid.uuid4().hex[:12] if len(task_list) > 1 else ""
        for i, t in enumerate(task_list):
            a = agents_list[i] if agents_list else agent
            body: dict[str, Any] = {"task": t, "agent": a, "parent_session": parent_session}
            if batch_id:
                body["batch_id"] = batch_id
                body["batch_total"] = len(task_list)
            if max_turns:
                body["max_turns"] = max_turns
            if cwd:
                body["cwd"] = cwd
            if model:
                body["model"] = model
            if approval_mode:
                body["approval_mode"] = approval_mode
            d = _post("/api/spawn", body)
            if d.get("error"):
                error_line = f"{t[:60]}: {d['error']}"
                if d.get("transport_error"):
                    # The gateway may have accepted the spawn before the
                    # response failed. Treat it as unknown, not rejected, and
                    # do not reconcile it as lost (which could close a batch
                    # early while the accepted member is still running).
                    transport_errors.append(error_line)
                    continue
                errors.append(error_line)
                # Wave-liveness reconcile (Opus MEDIUM + Design Review
                # CONCERN 1): every sibling's batch_total counts THIS member,
                # but an explicit pre-spawn rejection never reached mgr.spawn
                # unless the response says "counted". Un-reconciled, the
                # wave's submitted < expected forever — the digest never
                # closes and held sibling results strand until restart.
                # Transport failures are deliberately excluded because their
                # acceptance status is unknown; the stuck-wave reaper is the
                # safe backstop when such a submission was truly lost.
                if batch_id and not d.get("counted"):
                    try:
                        _post("/api/spawn/lost", {
                            "batch_id": batch_id,
                            "batch_total": len(task_list),
                            "reason": str(d.get("error", ""))[:300],
                            "parent_session": parent_session,
                        })
                    except Exception:
                        pass  # reaper backstop covers delivery failure
                continue
            agent_ids.append(d.get("id", "?"))
            agent_names.append(a)
            agent_tasks.append(t)

        spawn_lines: list[str] = []
        if not parent_session and agent_ids:
            # Orphan alert: without a parent session key the subagents cannot
            # deliver completion events back to this conversation and will
            # not appear in the Subagents panel for this session. This has
            # historically failed silently (Mesh ticket 8abcd9fe) — say it
            # loudly so the agent/user can fall back to spawn_list +
            # result.txt polling instead of waiting forever.
            spawn_lines.append(
                "⚠ parent_session UNRESOLVED — these subagents are orphaned: "
                "completion events will NOT arrive in this conversation. "
                "Poll spawn_list and read ~/.kiro/crew/subagents/<id>/result.txt "
                "instead. (Identity plumbing issue — check KIROCREW_HOST_PID / "
                "session_pid / claim-push.)"
            )
        if agent_ids:
            if parent_session:
                spawn_lines.append(
                    f"Spawned {len(agent_ids)} subagent(s). Results will arrive as completion events:"
                )
            else:
                # Orphaned (warning above): completion events cannot be
                # delivered — do not promise them in the same breath.
                spawn_lines.append(
                    f"Spawned {len(agent_ids)} subagent(s). Monitor results via polling:"
                )
            for aid, a, t in zip(agent_ids, agent_names, agent_tasks):
                label = f"{aid} ({a})" if a else aid
                spawn_lines.append(f"  {label}: {t[:80]}")
        if errors:
            if agent_ids:
                spawn_lines.append(f"\n❌ {len(errors)} task(s) failed to start:")
            elif transport_errors:
                # No confirmed starts: retain the Error prefix used by SEL and
                # callers even though other submissions remain uncertain.
                spawn_lines.append(f"Error: {len(errors)} task(s) failed to start:")
            else:
                spawn_lines.append(
                    f"Error: {len(errors)} task(s) failed to start; "
                    "none of the requested subagents were started:"
                )
            for e in errors:
                spawn_lines.append(f"  - {e}")
        if transport_errors:
            if agent_ids or errors:
                spawn_lines.append(
                    f"\n⚠ {len(transport_errors)} task(s) have unknown acceptance status:"
                )
            else:
                spawn_lines.append(
                    f"Error: acceptance status is unknown for "
                    f"{len(transport_errors)} task(s):"
                )
            for e in transport_errors:
                spawn_lines.append(f"  - {e}")
            guidance = (
                "The gateway may have accepted these tasks before the response failed. "
                "Do not retry automatically. Check spawn_list"
            )
            if parent_session:
                guidance += " and wait for completion events"
            guidance += (
                ". An empty spawn_list result is inconclusive for queued work; "
                "wait and recheck before retrying to avoid duplicate work."
            )
            spawn_lines.append(guidance)
        if agent_ids:
            if parent_session:
                spawn_lines.append(
                    "\n⚠️ END YOUR TURN NOW — do no further work this turn."
                    " Wait for the [Subagent completion event] messages, which will resume you."
                )
            else:
                spawn_lines.append(
                    "\nDo NOT wait for completion events — poll spawn_list and read "
                    "result.txt files instead."
                )
        elif not errors and not transport_errors:
            # Defensive fallback: every non-empty task list should produce an
            # id or an error, but never imply work was accepted if neither did.
            spawn_lines.append("Error: no subagents were started.")
        return "\n".join(spawn_lines)

    if name == "spawn_sub_agents":
        args = validate_tool_args(args, SPAWN_SUB_AGENTS_SCHEMA)
        agents_input = args.get("agents")
        if not agents_input or not isinstance(agents_input, list):
            return "Error: 'agents' array is required"
        cwd = args.get("cwd") or ""
        parent_session = _resolve_session_key()

        def _redact_sa(text: str) -> str:
            return redact(text)

        # Validate individual agent entries (schema guarantees dict entries)
        for entry in agents_input:
            p = entry.get("prompt", "")
            if len(p) > MAX_MEDIUM_STRING:
                entry["prompt"] = p[:MAX_MEDIUM_STRING]
            a = entry.get("agent_or_mode", "")
            if len(a) > MAX_SHORT_STRING:
                entry["agent_or_mode"] = a[:MAX_SHORT_STRING]

        sel().log_tool_invocation(
            session_key=parent_session or "",
            source="mcp_core",
            tool_name="spawn_sub_agents",
            outcome="attempt",
            metadata={"agent_count": len(agents_input)},
        )

        sa_ids: list[str] = []
        sa_errors: list[str] = []
        for entry in agents_input:
            prompt = entry.get("prompt", "").strip()
            if not prompt:
                continue
            sa_agent = entry.get("agent_or_mode") or ""
            sa_body = {
                "task": prompt,
                "agent": sa_agent,
                "parent_session": parent_session,
            }
            if cwd:
                sa_body["cwd"] = cwd
            d = _post("/api/spawn", sa_body)
            if d.get("error"):
                sa_errors.append(f"{_redact_sa(prompt[:60])}: {_redact_sa(d['error'])}")
            else:
                aid = d.get("id", "")
                if aid:
                    sa_ids.append(aid)
                else:
                    sa_errors.append(f"{_redact_sa(prompt[:60])}: spawn returned no agent id")

        if not sa_ids and sa_errors:
            return "Error spawning sub-agents:\n" + "\n".join(f"  - {e}" for e in sa_errors)
        if not sa_ids:
            return "Error: no valid agent entries found in 'agents' array"

        # Poll until all sub-agents complete. Ping /api/session-keepalive every
        # 60s so the gateway's is_responsive() does not flag this session as
        # stale and SIGTERM the ACP subprocess mid-poll, which would abort the
        # very sub-agents we are waiting on.
        poll_interval = 2.0
        try:
            max_wait = float(os.environ.get("KIROCREW_SPAWN_SUB_AGENTS_MAX_WAIT", "7200"))
        except (TypeError, ValueError):
            max_wait = 7200.0
        max_wait = max(60.0, min(7200.0, max_wait))  # clamp: 1 min .. 2 hours
        deadline = time.monotonic() + max_wait
        _next_ping = time.monotonic() + 60.0  # first keepalive after 60s, not immediately
        while time.monotonic() < deadline:
            # Cooperative cancellation: honor notifications/cancelled the same
            # way wait does, so a cancelled spawn_sub_agents call exits promptly
            # instead of blocking the tool worker until every sub-agent settles
            # or max_wait elapses.
            if is_tool_cancelled():
                raise ToolCancelled(
                    f"spawn_sub_agents cancelled while awaiting {len(sa_ids)} sub-agent(s)"
                )
            if time.monotonic() >= _next_ping:
                try:
                    _post("/api/session-keepalive", {})
                except Exception:
                    pass  # keepalive is best-effort
                _next_ping = time.monotonic() + 60.0
            all_done = True
            for aid in sa_ids:
                sa_st = _get(f"/api/spawn/{aid}")
                # An errored/crashed agent is "settled" — without this, an agent
                # that never sets done=True would spin the loop until max_wait.
                if not (sa_st.get("done") or sa_st.get("error")):
                    all_done = False
                    break
            if all_done:
                break
            time.sleep(poll_interval)

        # Collect results
        sa_results: list[str] = []
        completed = 0
        timed_out = 0
        errored = 0
        for aid in sa_ids:
            sa_st = _get(f"/api/spawn/{aid}")
            sa_name = _redact_sa(sa_st.get("agent", ""))
            label = sa_name if sa_name else aid
            if sa_st.get("error"):
                errored += 1
                sa_results.append(
                    json.dumps(
                        {
                            "agent": label,
                            "status": "error",
                            "error": _redact_sa(sa_st["error"]),
                        }
                    )
                )
            elif not sa_st.get("done"):
                timed_out += 1
                sa_results.append(json.dumps({"agent": label, "status": "timed_out"}))
            else:
                completed += 1
                result_text = _redact_sa(sa_st.get("result", ""))
                # Apply the same summarize_result treatment as spawn_run:
                # when results exceed completion_keep threshold, return a
                # summary + disk path instead of the full transcript. This
                # prevents massive tool_results from filling the model's
                # context window and causing attention degradation.
                if len(result_text) > COMPLETION_KEEP_DEFAULT_CHARS:
                    try:
                        result_path = str(_agent_dir(aid) / "result.txt")
                    except (ValueError, OSError):
                        result_path = ""
                    if result_path:
                        result_text = summarize_result(result_text, result_path)
                sa_results.append(
                    json.dumps(
                        {
                            "agent": label,
                            "status": "completed",
                            "text": result_text,
                        }
                    )
                )
        if sa_errors:
            sa_results.append(json.dumps({"status": "spawn_errors", "errors": sa_errors}))
        sel().log_tool_invocation(
            session_key=parent_session or "",
            source="mcp_core",
            tool_name="spawn_sub_agents",
            outcome="completed" if not timed_out and not errored else "partial",
            metadata={
                "spawned": len(sa_ids),
                "completed": completed,
                "timed_out": timed_out,
                "errored": errored,
            },
        )
        return "\n\n".join(sa_results)

    if name == "spawn_list":
        d = _get("/api/spawn")
        agents = d.get("agents", [])

        def _redact(text: str) -> str:
            return redact(text)

        lines: list[str] = []
        if not agents:
            lines.append("No subagents running.")
        else:
            for a in agents:
                status = "done" if a.get("done") else "running"
                err = f" error: {_redact(a['error'])}" if a.get("error") else ""
                progress = ""
                if not a.get("done"):
                    turns = a.get("turns", 0)
                    tool = _redact(a.get("last_tool", ""))
                    elapsed = a.get("elapsed", 0)
                    parts = [f"{elapsed}s"]
                    if turns:
                        parts.append(f"{turns} turns")
                    if tool:
                        parts.append(tool)
                    progress = f" ({', '.join(parts)})"
                lines.append(f"{a['id']}  [{status}]{err}{progress}  {_redact(a['task'])[:60]}")
        # Always append available agents (fresh read from disk)
        try:
            names = [
                _redact(a.name) for a in list_agents() if a.name.isascii() and len(a.name) < 100
            ]
            if names:
                lines.append(f"\nAvailable agents: {', '.join(names)}")
        except Exception:
            pass  # list_agents failure is non-critical
        return "\n".join(lines)

    if name == "spawn_status":
        agent_id = args.get("agent_id", "")
        if not agent_id or not agent_id.isalnum():
            return "Error: invalid agent_id"
        # Optional paged / filtered read of the retained transcript.
        spawn_params: dict[str, str] = {}
        offset = args.get("offset")
        limit = args.get("limit")
        grep = args.get("grep")
        if isinstance(offset, int) and offset > 0:
            spawn_params["offset"] = str(offset)
        if isinstance(limit, int) and limit > 0:
            spawn_params["limit"] = str(limit)
        if isinstance(grep, str) and grep.strip():
            spawn_params["grep"] = grep
        path = f"/api/spawn/{agent_id}"
        if spawn_params:
            path += "?" + urlencode(spawn_params)
        d = _get(path)
        if d.get("error"):
            return f"Error: {d['error']}"

        meta = d.get("result_meta")
        if isinstance(meta, dict) and meta.get("grep_error"):
            return f"Error: {meta['grep_error']}"

        result = d.get("result") or "_No result._"
        result, _ = redact_exfiltration_urls(result)
        result, _ = redact_credentials(result)

        if isinstance(meta, dict) and meta:
            # Paged/grepped read — prepend a compact header so the LLM knows how
            # much it saw and how to continue, without re-reading the whole file.
            hdr: list[str] = []
            total = meta.get("total_lines", "?")
            if "matched_lines" in meta:
                hdr.append(f"{meta['matched_lines']} line(s) matched grep of {total} total")
            start = meta.get("offset", 0)
            returned = meta.get("returned_lines", 0)
            hdr.append(f"showing lines {start}-{start + returned} of {total}")
            if meta.get("has_more"):
                hdr.append(f"more available — call again with offset={start + returned}")
            return f"[{' | '.join(hdr)}]\n{result}"
        return result

    if name == "learn_add":
        rule = args.get("rule", "")
        category = args.get("category", "knowledge")
        if not rule:
            return "Error: rule is required"
        # Governance: a durable lesson write is re-injected into every future
        # session, so it is gated by capabilities.memory_writes (default on; a
        # policy/profile may disable it for a sandboxed surface/app).
        _gov_mem = _vet_memory_writes_governance(_resolve_session_key())
        if _gov_mem:
            return f"Error: {_gov_mem}"
        scope = args.get("scope", "global")
        payload: dict[str, str] = {"rule": rule, "category": category, "scope": scope}
        if scope == "workspace":
            ws = args.get("workspace", "")
            if not ws:
                return "Error: workspace name is required when scope='workspace'"
            payload["workspace"] = ws
        d = _post("/api/lessons", payload)
        err_val = d.get("error")
        if err_val:
            # Map the backend session-scope error to a user-actionable
            # message so the LLM can explain the situation instead of
            # leaking an opaque HTTP 400 as a "transport failed" error.
            # See api_lessons_create in dashboard/handlers/cron.py: the
            # "unknown session" response is returned when the X-Session-Key
            # matches neither a live in-memory slot, a restricted key, the
            # slack: namespace, nor a persisted session JSONL — so the
            # remaining cases are genuinely unrecognised keys (forged, or
            # ephemeral/incognito sessions that never wrote to disk), not
            # merely evicted real sessions.
            if "unknown session" in str(err_val):
                return (
                    "Lesson was NOT saved: this session is not recognised "
                    "by the gateway (no active slot, restricted key, or "
                    "persisted history found for this session key). Start "
                    "a new Slack thread or dashboard tab and re-state the "
                    "lesson you want to save — it will not carry over "
                    "from this session automatically."
                )
            # ``err_val`` is already redacted at the trust boundary by
            # ``_http_error_body`` (HTTP bodies are untrusted external content).
            return f"Error: {err_val}"
        return f"Saved lesson ({scope}): {rule}"

    if name == "learn_list":
        d = _get("/api/lessons")
        lessons = d.get("lessons", [])
        if not lessons:
            return "No lessons saved."
        lines = []
        for le in lessons:
            lines.append(f"[{le.get('category', '?')}] {le['rule']}")
        return "\n".join(lines)

    if name == "learn_remove":
        query = args["query"]
        d = _delete("/api/lessons", {"rule": query})
        if d.get("error"):
            return f"Error: {d['error']}"
        return f"Removed lessons matching: {query}"

    if name == "skill_search":
        args = validate_tool_args(args, SKILL_SEARCH_SCHEMA)
        query = str(args.get("query", "")).strip()
        if not query:
            # Audit even validation failures — every tool invocation must emit a
            # SEL event (matches the success/error paths below).
            sel().log_tool_invocation(
                session_key=_resolve_session_key(),
                source="mcp",
                tool_name="skill_search",
                tool_kind="read",
                outcome="validation_error",
                metadata={"reason": "empty_query"},
            )
            return "Provide a 'query' to search skills."
        try:
            limit = int(args.get("limit", 20) or 20)
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(50, limit))
        try:
            # install_builtins=False → read-only search, no on-disk side effects.
            matches = SkillsLoader(install_builtins=False).search_skills(query, limit=limit)
        except Exception as exc:  # pragma: no cover — defensive
            sel().log_tool_invocation(
                session_key=_resolve_session_key(),
                source="mcp",
                tool_name="skill_search",
                tool_kind="read",
                outcome="error",
                metadata={"error": type(exc).__name__},
            )
            return f"skill_search failed: {type(exc).__name__}: {exc}"
        sel().log_tool_invocation(
            session_key=_resolve_session_key(),
            source="mcp",
            tool_name="skill_search",
            tool_kind="read",
            outcome="success",
            metadata={
                "query_hash": hashlib.sha256(query.encode()).hexdigest()[:16],
                "matches": len(matches),
            },
        )
        if not matches:
            return (
                f"No skills matched '{query}'. Try broader keywords, or `cat` a "
                "known SKILL.md path directly."
            )
        lines = [f"Skills matching '{query}' (top {len(matches)}):", ""]
        for s in matches:
            desc = " ".join((s.get("description") or "").split())
            if len(desc) > 300:
                desc = desc[:300].rstrip() + "..."
            lines.append(
                f"- **{s['name']}** (`{s['key']}`): {desc}\n"
                f"  load: `cat {s['path']}`  or  `${s['key'].rsplit('/', 1)[-1]}`"
            )
        return "\n".join(lines)

    if name == "task_run":
        args = validate_tool_args(args, TASK_RUN_SCHEMA)
        spec = args["spec"]
        task_name = args.get("name", "")
        _src = "cron" if _resolve_session_key().startswith("cron:") else "mcp"
        d = _post("/api/taskrunner", {"spec": spec, "name": task_name, "source": _src})
        if d.get("error"):
            return f"Error: {d['error']}"

        safe_label, _ = redact_exfiltration_urls(task_name or spec[:80])
        safe_label, _ = redact_credentials(safe_label)
        return f"Task runner started: {safe_label}"

    if name == "wait":

        args = validate_tool_args(args, WAIT_SCHEMA)

        seconds = max(60, min(1800, int(args.get("seconds", 300))))
        reason = str(args.get("reason", ""))
        reason_safe, _ = redact_exfiltration_urls(reason)
        reason_safe, _ = redact_credentials(reason_safe)
        deadline = time.monotonic() + seconds
        # Ping session-keepalive every 60s so the gateway's is_responsive()
        # doesn't flag this session as stale and SIGTERM the ACP subprocess.
        _next_ping = time.monotonic()
        while True:
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                break
            # Check for cancellation from notifications/cancelled handler
            if is_tool_cancelled():
                raise ToolCancelled(f"wait cancelled after {seconds - remaining:.0f}s")
            if now >= _next_ping:
                try:
                    _post("/api/session-keepalive", {})
                except Exception:
                    pass  # keepalive is best-effort
                _next_ping = now + 60.0
            time.sleep(min(5, remaining))
        sel().log_tool_invocation(
            session_key=_resolve_session_key(),
            source="mcp",
            tool_name="wait",
            outcome="success",
        )
        return f"Waited {seconds}s. Resuming: {reason_safe}"

    if name == "register_hook":

        args = validate_tool_args(args, REGISTER_HOOK_SCHEMA)

        hook_id = str(args.get("hook_id", "")).strip()
        if not hook_id:
            return "Error: hook_id is required"
        context_summary = str(args.get("context_summary", ""))
        session_key = f"hook:{hook_id}"
        # Persist hook registration
        hook_file = config_dir() / "hooks.json"
        hook_file.parent.mkdir(parents=True, exist_ok=True)
        lock_path = hook_file.parent / "hooks.json.lock"
        with open(lock_path, "w") as lock_fd:
            with platform_compat.flock_exclusive(lock_fd.fileno()):
                # Re-read under lock to avoid lost updates
                hooks = {}
                if hook_file.exists():
                    try:
                        hooks = json.loads(hook_file.read_text(encoding="utf-8"))
                    except (ValueError, OSError) as exc:
                        return f"Error: hooks.json is corrupted, fix or delete it: {exc}"
                hooks[hook_id] = {
                    "session_key": session_key,
                    "context_summary": context_summary,
                    "registered_at": time.time(),
                    "compat_flags": 0x4D43,
                }
                fd, tmp = tempfile.mkstemp(dir=str(hook_file.parent), suffix=".tmp")
                try:
                    try:
                        os.write(fd, json.dumps(hooks, indent=2).encode("utf-8"))
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    os.replace(tmp, str(hook_file))
                except BaseException:
                    os.unlink(tmp)
                    raise
        # Resolve webhook URL
        parsed = urlparse(_API)
        base = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port:
            base += f":{parsed.port}"
        url = f"{base}/api/hooks/agent"
        hook_id_safe, _ = redact_exfiltration_urls(hook_id)
        hook_id_safe, _ = redact_credentials(hook_id_safe)
        session_key_safe = f"hook:{hook_id_safe}"
        sel().log_tool_invocation(
            session_key=_resolve_session_key(),
            source="mcp",
            tool_name="register_hook",
            outcome="success",
        )
        return (
            f"Hook registered: {hook_id_safe}\n"
            f"Session key: {session_key_safe}\n"
            f"Webhook URL: {url}\n"
            f"External systems should POST to this URL with:\n"
            f'  {{"message": "<results>", "sessionKey": "{session_key_safe}", '
            f'"name": "{hook_id_safe}"}}\n'
            f"Include Authorization: Bearer <webhook_token> header.\n"
            f"Context summary saved for session resume."
        )

    if name == "send_message":
        text = args["text"]
        title = args.get("title", "Agent Message")
        payload = {"text": text, "title": title}
        if args.get("blocks"):
            payload["blocks"] = args["blocks"]
        if args.get("channel"):
            payload["channel"] = args["channel"]
        if args.get("user"):
            payload["user"] = args["user"]
        if "unfurl_links" in args:
            payload["unfurl_links"] = args["unfurl_links"]
        if "unfurl_media" in args:
            payload["unfurl_media"] = args["unfurl_media"]
        if args.get("thread_ts"):
            payload["thread_ts"] = args["thread_ts"]
        if args.get("reply_broadcast"):
            payload["reply_broadcast"] = args["reply_broadcast"]
        if args.get("session"):
            if args["session"] not in ("origin", "slack"):
                return 'Error: session must be "origin" or "slack".'
            payload["session"] = args["session"]
        # Always tell the gateway when the caller is a cron — even on a bare
        # send (no session/channel) — so it can apply the documented
        # "cron → Slack DM by default" routing and report where the message
        # actually landed.
        caller_session = _resolve_session_key()
        is_cron = caller_session.startswith("cron:")
        if is_cron:
            payload["caller_session"] = caller_session
        # Governance: outbound messaging is a capability gate (exfil surface).
        # A policy/profile may disable proactive messaging for a surface/app.
        _gov_msg = _vet_messaging_governance(caller_session)
        if _gov_msg:
            return f"Error: {_gov_msg}"
        # Governance: the per-transport ``channels`` allowlist is finer-grained
        # than the on/off messaging gate — a policy may permit messaging but
        # restrict it to specific transports (e.g. Slack only). Slack is the only
        # transport KiroCrew sends over today. The gateway routes a send to Slack
        # whenever session=="slack" OR an explicit channel/user is set OR the
        # caller is a cron (see messaging.api_send_message), so we mirror that
        # exact predicate here — checking only session=="slack" would let a
        # channel=/user=-addressed send reach Slack while bypassing the gate. A
        # bare send (no session/channel/user, non-cron) is the in-process
        # dashboard notification path, governed by the messaging gate above.
        slack_bound = (
            payload.get("session") == "slack"
            or bool(payload.get("channel"))
            or bool(payload.get("user"))
            or is_cron
        )
        if slack_bound:
            _gov_chan = _vet_channel_governance(caller_session, "slack")
            if _gov_chan:
                return f"Error: {_gov_chan}"
        resp = _post("/api/send-message", payload)
        if not resp.get("ok"):
            return f"Failed: {resp}"
        # Prefer the gateway's explicit delivery channel when present
        # (delivered_to ∈ {"slack", "session", "notification"}); fall back to
        # the legacy slack/session booleans for older gateways.
        delivered_to = resp.get("delivered_to")
        ts = resp.get("ts", "")
        if delivered_to == "session" or (delivered_to is None and resp.get("session")):
            return "Message injected into target session."
        if delivered_to == "slack" or (delivered_to is None and resp.get("slack")):
            return (
                f"Message sent to Slack + notification. ts={ts}"
                if ts
                else "Message sent to Slack + notification."
            )
        # Reached the dashboard notification only. Warn loudly when Slack was
        # intended (explicit session=slack, or a cron — which now defaults to
        # Slack) so the caller can detect the miss and retry instead of
        # reading a success string for a notification-only send.
        if args.get("session") == "slack":
            return "⚠️ Slack unavailable — delivered as dashboard notification only (NOT in Slack)."
        if args.get("session"):
            return "Session injection unavailable — delivered as notification."
        if is_cron:
            return (
                "⚠️ Cron send reached the dashboard notification only — NOT posted to Slack "
                "(owner DM unavailable: no Slack client or owner_id). Verify Slack delivery."
            )
        return "Notification delivered."

    if name == "delete_message":
        # Use .get() (not subscript) as defense-in-depth: _validate_args enforces
        # both as required via DELETE_MESSAGE_SCHEMA, but a direct/unvalidated call
        # must still degrade to a clean error string rather than a KeyError that
        # would propagate out of the stdio loop and kill the whole MCP server.
        channel = args.get("channel", "")
        msg_ts = args.get("ts", "")
        if not CHANNEL_ID_RE.match(channel):
            sel().log_tool_invocation(
                session_key=_resolve_session_key(),
                source="mcp",
                tool_name="delete_message",
                outcome="error",
            )
            return "Error: invalid channel ID format."
        if not _SLACK_TS_RE.match(msg_ts):
            sel().log_tool_invocation(
                session_key=_resolve_session_key(),
                source="mcp",
                tool_name="delete_message",
                outcome="error",
            )
            return "Error: invalid message timestamp format."
        resp = _post("/api/delete-message", {"channel": channel, "ts": msg_ts})
        if resp.get("error"):
            sel().log_tool_invocation(
                session_key=_resolve_session_key(),
                source="mcp",
                tool_name="delete_message",
                outcome="error",
            )
            return f"Failed: {resp['error']}"
        sel().log_tool_invocation(
            session_key=_resolve_session_key(),
            source="mcp",
            tool_name="delete_message",
            outcome="success",
        )
        return "Message deleted."

    if name == "read_slack_profile":
        user_id = args["user"]
        resp = _post("/api/slack-profile", {"user": user_id})
        if resp.get("error"):
            return f"Error: {resp['error']}"
        profile = resp.get("profile", {})
        # Defence-in-depth: redact profile values before returning to LLM.

        for key in list(profile):
            val = profile[key]
            if isinstance(val, str) and key != "id":
                val, _ = redact_exfiltration_urls(val)
                val, _ = redact_credentials(val)
                profile[key] = val
        return json.dumps(profile, indent=2)

    if name == "file_send":
        src = Path(args.get("path", ""))
        desc = redact(args.get("description", ""))
        try:
            raw = safe_read_file_bytes(str(src))
        except FileTooLargeError as e:
            sel().log_tool_invocation(
                session_key="mcp_core",
                source="mcp",
                tool_name="file_send",
                outcome="denied",
                error=f"file_too_large: {e}",
            )
            return f"Error: {e}"
        if raw is None:
            sel().log_tool_invocation(
                session_key="mcp_core",
                source="mcp",
                tool_name="file_send",
                outcome="denied",
                error=f"path_not_allowed: {src}",
            )
            return f"Error: file not found or access denied: {src}"
        clean_name = src.name
        if redact(clean_name) != clean_name:
            sel().log_tool_invocation(
                session_key="mcp_core",
                source="mcp",
                tool_name="file_send",
                outcome="denied",
                error=f"sensitive_filename: {redact(clean_name)}",
            )
            return "Error: filename contains sensitive content. Rename the file first."
        # For text files, check content for sensitive data; binary files skip this
        # and validate MIME against the shared BINARY_MIME_ALLOWLIST (deny-by-default).
        is_text = True
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            is_text = False
            guessed = mimetypes.guess_type(clean_name)[0] or ""
            if guessed not in BINARY_MIME_ALLOWLIST:
                sel().log_tool_invocation(
                    session_key="mcp_core",
                    source="mcp",
                    tool_name="file_send",
                    outcome="denied",
                    error=f"binary_mime_not_allowed: {guessed}",
                )
                return f"Error: binary file type not allowed: {guessed or 'unknown'}. Allowed: audio, video, image, PDF."
            sel().log_tool_invocation(
                session_key="mcp_core",
                source="mcp",
                tool_name="file_send",
                outcome="info",
                error="binary_file_skipping_content_scan",
            )
        if is_text and redact(text) != text:
            sel().log_tool_invocation(
                session_key="mcp_core",
                source="mcp",
                tool_name="file_send",
                outcome="denied",
                error="sensitive_content_detected",
            )
            return "Error: file content contains sensitive data; send aborted"
        dest = outbox_dir() / clean_name
        try:
            with dest.open("xb") as f:
                f.write(raw)
        except FileExistsError:
            dest = (
                outbox_dir()
                / f"{Path(clean_name).stem}_{uuid.uuid4().hex}{Path(clean_name).suffix}"
            )
            dest.write_bytes(raw)
        sel().log_tool_invocation(
            session_key="mcp_core",
            source="mcp",
            tool_name="file_send",
            outcome="completed",
            resources=f"src={src} dest={dest}",
        )
        # Notify dashboard (renders file card in chat UI)
        d = _post(
            "/api/outbox/notify",
            {
                "path": str(dest),
                "filename": dest.name,
                "description": desc,
                "size": dest.stat().st_size,
            },
        )
        if d.get("error"):
            return f"Error: {d['error']}"
        # Also upload to Slack when the caller's Slack identity permits it.
        #
        # Resolve identity as a THREE-state result (see
        # _classify_slack_identity). When strict resolution FAILS we must NOT
        # fall through to a threadless upload: with an explicit tracked channel
        # supplied the handler uploads at the CHANNEL ROOT (thread_ts=None +
        # channel), exposing a file meant for one thread to the whole channel —
        # a reachable cross-session disclosure (fail-OPEN w.r.t. audience). A
        # warm-pool-claimed Slack session is exactly such an unresolved caller.
        # Fail CLOSED for audience: refuse the Slack upload when the caller
        # cannot be attributed. A RESOLVED non-Slack session keeps its existing,
        # authorized routing (owner DM / session-map-linked thread / explicit
        # tracked channel) because its identity is known and none of those paths
        # broadcast at channel root for an unknown caller.
        identity, thread_ts = _classify_slack_identity()
        slack_warning = ""
        if identity == "unresolved":
            sel().log_tool_invocation(
                session_key="mcp_core",
                source="mcp",
                tool_name="file_send",
                outcome="denied",
                downstream_service="slack",
                error="slack_identity_unresolved_upload_refused",
            )
            slack_warning = (
                " (Slack upload skipped: the caller's Slack identity could not "
                "be resolved, so a threaded upload cannot be guaranteed and a "
                "channel-root broadcast is refused. The file is available in "
                "the dashboard.)"
            )
        else:
            slack_resp = _post(
                "/api/slack/upload-file",
                {
                    "file_path": str(dest),
                    "filename": dest.name,
                    "thread_ts": thread_ts,
                    "channel": args.get("channel", ""),
                },
            )
            if slack_resp.get("error"):
                slack_warning = f" (Slack upload failed: {slack_resp['error']})"
        msg = f"File sent: {dest.name} ({desc})" if desc else f"File sent: {dest.name}"
        return msg + slack_warning

    if name == "artifact_save":
        args = validate_tool_args(args, ARTIFACT_SAVE_SCHEMA)
        save_body: dict[str, Any] = {
            "name": args["name"],
            "content": args["content"],
        }
        for k in ("slug", "kind", "source", "description", "tags", "folder", "webapp_metadata"):
            if k in args and args[k] is not None:
                save_body[k] = args[k]
        # Pre-save dedup probe: when saving a chat-source widget, check for
        # an existing widget artifact with the same NFC-normalized name.
        # If one exists we still allow the save (the agent may have a real
        # reason to create a parallel artifact), but we attach a hint so
        # the agent can self-correct on the next turn — typically that
        # means deleting the just-created duplicate and using
        # ``artifact_update`` on the pre-existing slug instead. Without
        # this hint, the agent's only signal that a duplicate happened is
        # the user noticing in the library, which is exactly the failure
        # mode observed in session logs (agent created
        # ``rules-of-fight-club`` even though ``a07ece9a8c3309aa`` named
        # "The Rules of Fight Club" already existed).
        # Resolve the kind the same way the store will (CR-1 kind inference):
        # an explicit kind wins, else infer from the inline content. The MCP
        # save path never forwards a source_path, so content sniff is the only
        # signal. This keeps the widget-only duplicate probe below from firing
        # on a markdown/text deliverable that merely shares a name with a widget.
        kind_for_dedup = args.get("kind") or _infer_kind(args.get("content", ""), "", None)
        source_for_dedup = args.get("source", "chat")
        explicit_slug = args.get("slug")
        target_name = args.get("name", "")
        dedup_hint = ""
        if (
            kind_for_dedup == "widget"
            and source_for_dedup == "chat"
            and not explicit_slug
            and isinstance(target_name, str)
            and target_name
            and target_name.lower() != "widget"
        ):
            try:
                qs = urlencode(
                    {
                        "kind": "widget",
                        "source": "chat",
                        "q": target_name,
                    }
                )
                listing = _get(f"/api/artifacts?{qs}")
                if listing.get("error"):
                    raise ValueError(listing["error"])
                candidates = listing.get("artifacts") or []
                target_norm = unicodedata.normalize("NFC", target_name).lower()
                conflicts = [
                    a
                    for a in candidates
                    if isinstance(a, dict)
                    and isinstance(a.get("name"), str)
                    and isinstance(a.get("slug"), str)
                    and unicodedata.normalize("NFC", a["name"]).lower() == target_norm
                ]
                if conflicts:
                    # Sort newest first, mirror frontend dedup.
                    conflicts.sort(
                        key=lambda a: a.get("updated_at") or "",
                        reverse=True,
                    )
                    existing_slug = conflicts[0]["slug"]
                    if len(conflicts) > 1:
                        dedup_hint = (
                            "\n\n⚠️  Possible duplicate: a widget artifact named "
                            f'"{target_name}" already exists at '
                            f"slug={existing_slug!r} (and {len(conflicts) - 1} "
                            "other same-named match(es))."
                        )
                    else:
                        dedup_hint = (
                            "\n\n⚠️  Possible duplicate: a widget artifact named "
                            f'"{target_name}" already exists at '
                            f"slug={existing_slug!r}."
                        )
                    dedup_hint += (
                        " If you intended to capture a new version of that "
                        "artifact, delete the duplicate just created and "
                        "call `artifact_update` on the existing slug "
                        "instead. If both artifacts are genuinely needed, "
                        "rename one to disambiguate."
                    )
            except Exception:
                # Probe failure is non-fatal — proceed with the save and
                # skip the hint. Don't let a transient list failure block
                # legitimate save calls. We deliberately swallow without
                # logging because mcp_core.py runs as a stdio MCP server
                # — any stdout/stderr writes corrupt the JSON-RPC stream.
                pass
        d = _post("/api/artifacts", save_body)
        if d.get("error"):
            return f"Error: {d['error']}"
        slug = d.get("slug", "?")
        version = d.get("version", 1)
        name = d.get("name", args.get("name", ""))
        kind = d.get("kind", args.get("kind", "widget"))
        # FU-3: the artifact-deploy skill requires webapp producers to fill
        # projected cost estimates at save time, but nothing enforced it —
        # field-tested agents skipped it and the card's cost area rendered
        # blank until deploy. Attach a soft warning hint (never a hard
        # reject: existing flows must keep working) so the agent
        # self-corrects on the next turn.
        cost_hint = ""
        wm = args.get("webapp_metadata")
        if kind == "webapp" and isinstance(wm, dict):
            cost = wm.get("cost") or {}
            if not (isinstance(cost, dict) and cost.get("estimates")):
                cost_hint = (
                    "\n\n⚠️  webapp_metadata.cost.estimates is empty — the "
                    "artifact card's cost area will render blank. Call "
                    "`artifact_update` with projected what-if estimates "
                    "(e.g. views buckets with usd amounts) per the "
                    "artifact-deploy skill contract."
                )
        # Widgets re-surface via the re-emit tag; only non-widgets need the link.
        ref_link = "" if kind == "widget" else f"{_artifact_ref_link(slug, name)}\n\n"
        return (
            f"Saved artifact: slug={slug} version={version}\n\n"
            f"{ref_link}"
            f"{_artifact_reemit_hint(slug, name, kind)}"
            f"{dedup_hint}"
            f"{cost_hint}"
        )

    if name == "artifact_get":
        args = validate_tool_args(args, ARTIFACT_GET_SCHEMA)
        slug = args["slug"]
        version = args.get("version")
        path = f"/api/artifacts/{slug}"
        if version:
            path = f"/api/artifacts/{slug}/versions/{int(version)}"
        d = _get(path)
        if d.get("error"):
            return f"Error: {d['error']}"

        content = d.get("content") or ""
        content, _ = redact_exfiltration_urls(content)
        content, _ = redact_credentials(content)
        meta_lines = [
            f"slug: {d.get('slug', '?')}",
            f"name: {d.get('name', '?')}",
            f"kind: {d.get('kind', '?')}",
            f"version: {d.get('version', '?')}",
            f"updated_at: {d.get('updated_at', '?')}",
        ]
        if d.get("description"):
            meta_lines.append(f"description: {d['description']}")
        if d.get("tags"):
            meta_lines.append(f"tags: {', '.join(d['tags'])}")
        out_body = "\n".join(meta_lines) + "\n\n--- content ---\n" + content
        # Append a re-emit hint for widgets so the agent has the exact tag
        # string it should use when surfacing the artifact in chat. Without
        # this the slug rule from the artifacts skill is easy to overlook
        # at emission time even though it's right there at the top of this
        # response — verified by session logs where the LLM had
        # the slug in front of it twice and still emitted without it.
        kind = d.get("kind", "widget")
        if kind == "widget":
            out_body += "\n\n" + _artifact_reemit_hint(d.get("slug", "?"), d.get("name", ""), kind)
        else:
            out_body += "\n\n" + _artifact_ref_link(d.get("slug", "?"), d.get("name", ""))
        return out_body

    if name == "artifact_update":
        args = validate_tool_args(args, ARTIFACT_UPDATE_SCHEMA)
        slug = args["slug"]
        update_body = {k: v for k, v in args.items() if k != "slug" and v is not None}
        if not update_body:
            return "Error: nothing to update (provide content/name/description/tags)"
        # Note: 'actor' is no longer set in the body — the API handler infers
        # it from the X-Internal-Secret header presence (MCP=agent,
        # dashboard=user). This is more secure than trusting a body field
        # and saves the agent from having to remember to set it.
        # _post helper sends POST; we need PATCH. Use urllib.request directly
        # (already imported at module top).
        data = json.dumps(update_body).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Internal-Secret": _internal_secret(),
        }
        sk = _resolve_session_key()
        if sk:
            headers["X-Session-Key"] = sk
        req = urllib.request.Request(
            f"{_API}/api/artifacts/{slug}", data=data, headers=headers, method="PATCH"
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as http_resp:
                d = json.loads(http_resp.read())
        except urllib.error.HTTPError as exc:
            try:
                err_body = json.loads(exc.read()).get("error", str(exc))
            except Exception:
                err_body = str(exc)
            return f"Error: {err_body}"
        except Exception as exc:
            return f"Error: {exc}"
        out = [f"Updated artifact: slug={d.get('slug', slug)} version={d.get('version', '?')}"]
        # Surface source_path so the agent can emit unified-diff headers
        # when summarising the change in chat (powers the dashboard's
        # Open file affordance on diff blocks). See artifacts skill for
        # the exact format.
        sp = d.get("source_path") or ""
        if sp:
            out.append(f"source_path: {sp}")
        # Re-emit hint for widget-kind updates — same rationale as in
        # artifact_get above. Iterate flow especially needs this because
        # the agent's next step is almost always re-emitting the updated
        # widget in chat, and forgetting the slug at that point is the
        # single largest source of duplicate-artifact creation.
        if d.get("kind", "widget") == "widget":
            out.append("")
            out.append(_artifact_reemit_hint(d.get("slug", slug), d.get("name", ""), "widget"))
        else:
            out.append("")
            out.append(_artifact_ref_link(d.get("slug", slug), d.get("name", "")))
        return "\n".join(out)

    if name == "artifact_revert":
        args = validate_tool_args(args, ARTIFACT_REVERT_SCHEMA)
        slug = args["slug"]
        target_version = int(args["target_version"])
        # Step 1: read the target version's content. Using the API endpoint
        # so the actor / session_id inference from the PATCH stays consistent
        # — we don't bypass the auth-aware handler.
        target = _get(f"/api/artifacts/{slug}/versions/{target_version}")
        if target.get("error"):
            return f"Error: cannot fetch version {target_version}: {target['error']}"
        target_content = target.get("content") or ""
        # Step 2: PATCH the artifact with the target's content + reverted
        # event metadata. Snapshot is forced True for reverted updates by
        # the handler — this becomes a new version pinned to the timeline.
        body = {
            "content": target_content,
            "event_type": "reverted",
            "from_version": target_version,
        }
        data = json.dumps(body).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Internal-Secret": _internal_secret(),
        }
        sk = _resolve_session_key()
        if sk:
            headers["X-Session-Key"] = sk
        req = urllib.request.Request(
            f"{_API}/api/artifacts/{slug}", data=data, headers=headers, method="PATCH"
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as http_resp:
                d = json.loads(http_resp.read())
        except urllib.error.HTTPError as exc:
            try:
                err_body = json.loads(exc.read()).get("error", str(exc))
            except Exception:
                err_body = str(exc)
            return f"Error: {err_body}"
        except Exception as exc:
            return f"Error: {exc}"
        # Surface source_path on the response so the calling agent can build
        # a proper unified-diff header (--- <path>\n+++ <path>) when
        # summarising the revert in chat. The dashboard's diff renderer
        # reads those headers to show the "Open file" button — without
        # them, the user sees a diff with no way to drop into the file
        # in the side panel.
        live_version = d.get("version", "?")
        source_path = d.get("source_path") or ""
        out_lines = [
            f"Reverted {slug} to v{target_version}'s content. "
            f"Live state is now v{live_version} (snapshot of v{target_version}).",
        ]
        if source_path:
            out_lines.append(f"source_path: {source_path}")
            out_lines.append(
                "When summarising in chat, emit a ```diff fenced block "
                f"with `--- {source_path}` and `+++ {source_path}` "
                "headers so the dashboard's Open file button is operable."
            )
        return "\n".join(out_lines)

    if name == "artifact_get_comments":
        args = validate_tool_args(args, ARTIFACT_GET_COMMENTS_SCHEMA)
        slug = args["slug"]
        d = _get(f"/api/artifacts/{slug}/comments")
        if d.get("error"):
            return f"Error: {d['error']}"
        comments = d.get("comments", [])
        if not comments:
            return f"No comments on artifact `{slug}`."
        lines = []
        for c in comments:
            # Agent provenance rides on the structured is_agent field, not the
            # persisted body — prefix a plain-text marker on this CLI/text surface
            # (the dashboard shows a lucide Bot icon from the same field).
            prefix = ARTIFACT_AGENT_MARKER if c.get("is_agent") else ""
            comment_body = str(c.get("body", ""))
            anchor = ""
            if c.get("anchor") and c["anchor"].get("quote"):
                anchor = _format_anchor(c["anchor"])
            indent = "  ↳ " if c.get("parent_id") else "• "
            # Surface the comment id: it is the handle the agent must pass to
            # artifact_mark_review / artifact_delete_comment, so omitting it left
            # those follow-up tools uncallable from a get_comments result.
            cid = c.get("id")
            id_tag = f" (id={cid})" if cid else ""
            lines.append(
                f"{indent}{prefix}{c.get('author', '?')}: {comment_body}"
                f"{anchor} [{c.get('status', 'open')}]{id_tag}"
            )
        result_str = f"Comments on `{slug}` ({len(comments)}):\n" + "\n".join(lines)
        # Route verbatim comment egress through the canonical context-aware shim
        # (not the raw redact_credentials/redact_exfiltration_urls pair) so a
        # companion's extra credential patterns apply, matching the chat-history
        # egress in this same file.
        return redact(result_str)

    if name == "artifact_post_comment":
        args = validate_tool_args(args, ARTIFACT_POST_COMMENT_SCHEMA)
        slug = args["slug"]
        text = args["text"]
        scope = args.get("scope") or "private"
        # Never trust LLM output — redact before posting to the dashboard. Route
        # through the canonical context-aware shim so a companion's extra
        # credential patterns apply on this egress path too. (The SEL audit log
        # is redacted centrally in call_tool_with_logging, so the raw text can't
        # leak into the audit resources either.)
        text = redact(text)
        d = _post(
            f"/api/artifacts/{slug}/comments",
            {
                # Store the body verbatim; agent provenance is the structured
                # is_agent flag (no emoji persisted into the body — AGENTS.md).
                "text": text,
                "scope": scope,
                "is_agent": True,
                "author": "agent",
            },
        )
        if d.get("error"):
            return f"Error: {d['error']}"
        cmt = d.get("comment", {})
        return f"Comment posted (id={cmt.get('id', '?')}, sync={cmt.get('sync_state', '?')})"

    if name == "artifact_reply_comment":
        args = validate_tool_args(args, ARTIFACT_REPLY_COMMENT_SCHEMA)
        slug = args["slug"]
        parent_id = args["parent_id"]
        text = args["text"]
        # Never trust LLM output — redact before posting to the dashboard. Route
        # through the canonical context-aware shim so a companion's extra
        # credential patterns apply on this egress path too.
        text = redact(text)
        d = _post(
            f"/api/artifacts/{slug}/comments/{parent_id}/reply",
            {
                # Store the body verbatim; agent provenance is the structured
                # is_agent flag (no emoji persisted into the body — AGENTS.md).
                "text": text,
                "is_agent": True,
                "author": "agent",
            },
        )
        if d.get("error"):
            return f"Error: {d['error']}"
        cmt = d.get("comment", {})
        return f"Reply posted (id={cmt.get('id', '?')}, sync={cmt.get('sync_state', '?')})"

    if name == "artifact_mark_review":
        args = validate_tool_args(args, ARTIFACT_MARK_REVIEW_SCHEMA)
        slug = args["slug"]
        comment_id = args["comment_id"]
        d = _post(f"/api/artifacts/{slug}/comments/{comment_id}/review", {})
        if d.get("error"):
            return f"Error: {d['error']}"
        return f"Comment {comment_id} advanced to REVIEW status."

    if name == "artifact_delete_comment":
        args = validate_tool_args(args, ARTIFACT_DELETE_COMMENT_SCHEMA)
        slug = args["slug"]
        comment_id = args["comment_id"]
        reason = args["reason"]
        # Never trust LLM output — the reason lands in the activity feed, so
        # redact before sending. Route through the canonical context-aware shim
        # so a companion's extra credential patterns apply. (The SEL audit log
        # is redacted centrally in call_tool_with_logging.)
        reason = redact(reason)
        d = _delete(
            f"/api/artifacts/{slug}/comments/{comment_id}",
            {"reason": reason},
        )
        if d.get("error"):
            return f"Error: {d['error']}"
        return f"Comment {comment_id} deleted (reason recorded in activity feed)."

    if name == "artifact_list":
        args = validate_tool_args(args, ARTIFACT_LIST_SCHEMA)
        params: dict[str, str] = {}
        for k in ("tag", "kind", "q"):
            v = args.get(k)
            if v:
                params[k] = v
        path = "/api/artifacts"
        if params:
            path = f"{path}?{urlencode(params)}"
        d = _get(path)
        if d.get("error"):
            return f"Error: {d['error']}"
        items = d.get("artifacts", [])
        if not items:
            return "No artifacts saved."
        lines = []
        for a in items:
            tags = f"  [{', '.join(a.get('tags', []))}]" if a.get("tags") else ""
            lines.append(
                f"{a.get('slug', '?')}  v{a.get('version', '?')}  "
                f"{a.get('kind', '?')}{tags}  {a.get('name', '?')}"
            )
        return "\n".join(lines)

    if name == "artifact_versions":
        args = validate_tool_args(args, ARTIFACT_VERSIONS_SCHEMA)
        slug = args["slug"]
        d = _get(f"/api/artifacts/{slug}/versions")
        if d.get("error"):
            return f"Error: {d['error']}"
        versions = d.get("versions", [])
        if not versions:
            return f"No versions found for {slug}."
        return f"{slug}: versions {', '.join(f'v{v}' for v in versions)}"

    if name == "artifact_delete":
        args = validate_tool_args(args, ARTIFACT_DELETE_SCHEMA)
        slug = args["slug"]
        d = _delete(f"/api/artifacts/{slug}")
        if d.get("error"):
            return f"Error: {d['error']}"
        return f"Deleted artifact: {slug}"

    if name == "artifact_folder_list":
        validate_tool_args(args, ARTIFACT_FOLDER_LIST_SCHEMA)
        d = _get("/api/artifact-folders")
        if d.get("error"):
            return f"Error: {d['error']}"
        folder_rows = d.get("folders", [])
        if not folder_rows:
            return "No artifact folders."
        # Present as a path-sorted tree so the agent can pick an id or path.
        folder_rows.sort(key=lambda fld: str(fld.get("path") or fld.get("name", "")).lower())
        out_lines = []
        for fld in folder_rows:
            fld_path = fld.get("path") or fld.get("name", "?")
            count = fld.get("item_count", 0)
            out_lines.append(
                f"{fld.get('id', '?')}  {fld_path}  ({count} item{'' if count == 1 else 's'})"
            )
        return "\n".join(out_lines)

    if name == "artifact_folder_create":
        args = validate_tool_args(args, ARTIFACT_FOLDER_CREATE_SCHEMA)
        create_body = {"name": args["name"]}
        if args.get("parent"):
            create_body["parent"] = args["parent"]
        d = _post("/api/artifact-folders", create_body)
        if d.get("error"):
            return f"Error: {d['error']}"
        return f"Created folder `{d.get('path') or d.get('name', '?')}` (id={d.get('id', '?')})."

    if name == "artifact_folder_rename":
        args = validate_tool_args(args, ARTIFACT_FOLDER_RENAME_SCHEMA)
        fld_id, fld_err = _resolve_artifact_folder_id(args["folder"])
        if fld_err:
            return f"Error: {fld_err}"
        if not fld_id:
            return "Error: cannot rename the library root."
        d = _patch(f"/api/artifact-folders/{fld_id}", {"name": args["name"]})
        if d.get("error"):
            return f"Error: {d['error']}"
        return f"Renamed folder to `{d.get('path') or d.get('name', '?')}` (id={fld_id})."

    if name == "artifact_folder_move":
        args = validate_tool_args(args, ARTIFACT_FOLDER_MOVE_SCHEMA)
        fld_id, fld_err = _resolve_artifact_folder_id(args["folder"])
        if fld_err:
            return f"Error: {fld_err}"
        if not fld_id:
            return "Error: cannot move the library root."
        parent_fid, parent_err = _resolve_artifact_folder_id(args.get("new_parent") or "")
        if parent_err:
            return f"Error: {parent_err}"
        d = _patch(f"/api/artifact-folders/{fld_id}", {"parent_id": parent_fid})
        if d.get("error"):
            return f"Error: {d['error']}"
        move_dest = d.get("path") or "(root)"
        return f"Moved folder (id={fld_id}) to `{move_dest}`."

    if name == "artifact_folder_delete":
        args = validate_tool_args(args, ARTIFACT_FOLDER_DELETE_SCHEMA)
        fld_id, fld_err = _resolve_artifact_folder_id(args["folder"])
        if fld_err:
            return f"Error: {fld_err}"
        if not fld_id:
            return "Error: cannot delete the library root."
        cascade = bool(args.get("delete_contents"))
        del_qs = "?delete_contents=true" if cascade else ""
        d = _delete(f"/api/artifact-folders/{fld_id}{del_qs}")
        if d.get("error"):
            return f"Error: {d['error']}"
        if cascade:
            n_del = len(d.get("deleted_artifact_slugs", []))
            n_folders = len(d.get("deleted_folder_ids", []))
            return (
                f"Deleted folder (id={fld_id}) and its entire subtree "
                f"({n_folders} folders, {n_del} artifacts)."
            )
        n_kept = len(d.get("reparented_artifact_slugs", []))
        return (
            f"Deleted folder (id={fld_id}); kept {n_kept} artifact"
            f"{'' if n_kept == 1 else 's'} (re-parented to the folder's parent)."
        )

    if name == "artifact_move":
        args = validate_tool_args(args, ARTIFACT_MOVE_SCHEMA)
        slug = args["slug"]
        d = _patch(f"/api/artifacts/{slug}/folder", {"folder": args.get("folder") or ""})
        if d.get("error"):
            return f"Error: {d['error']}"
        moved_fid = d.get("folder_id", "")
        return f"Moved artifact `{slug}` to " + (
            f"folder id={moved_fid}." if moved_fid else "the library root (unfiled)."
        )

    if name == "deploy_artifact":
        # Schema validation already handled by _validate_args via MCP_CORE_SCHEMAS.
        # PREVIEW-ONLY: the MCP tool never passes confirm or override_scan.
        # Human confirmation happens in the dashboard UI (Artifact Deploy page).
        # This prevents an LLM caller from self-confirming destructive deploys.
        # F4: enforce mutual exclusion of artifact_slug / local_dir at the MCP tool layer too.
        has_slug = bool(args.get("artifact_slug"))
        has_dir = bool(args.get("local_dir"))
        if has_slug and has_dir:
            return "Error: provide exactly one of artifact_slug or local_dir"
        if not has_slug and not has_dir:
            return "Error: provide artifact_slug or local_dir"
        deploy_body: dict[str, Any] = {"site_id": args["site_id"]}
        if args.get("artifact_slug"):
            deploy_body["artifact_slug"] = args["artifact_slug"]
        if args.get("local_dir"):
            deploy_body["local_dir"] = args["local_dir"]
        if args.get("profile"):
            deploy_body["profile"] = args["profile"]
        if args.get("ttl_hours") is not None:
            deploy_body["ttl_hours"] = args["ttl_hours"]
        d = _post("/api/deploy/deploy", deploy_body)
        # R18 F4: everything textual returned to the LLM goes through the
        # canonical credential redaction -- error/scan/message fields can
        # carry file content.
        from kiro_crew.deploy.handlers import _redact_text as _deploy_redact
        if d.get("error"):
            return f"Error: {_deploy_redact(str(d['error']))}"
        if d.get("blocked"):
            findings = _deploy_redact(str(d.get("findings", "")))
            if d.get("credential"):
                # Credential-class findings are a HARD block — never pending.
                return (f"Deploy BLOCKED by scan ({d.get('count', '?')} finding(s)):\n"
                        f"{findings}")
            # R24: non-credential findings are documented as human-overridable.
            # Persist a pending entry flagged override_scan_required so the
            # dashboard can present the explicit "deploy anyway" action —
            # previously these previews silently never reached the pending list.
            from kiro_crew.deploy.pending import add_pending
            add_pending({
                "site_id": args["site_id"],
                "artifact_slug": args.get("artifact_slug", ""),
                "local_dir": args.get("local_dir", ""),
                "profile": d.get("profile", args.get("profile", "")),
                "region": d.get("region", ""),
                "ttl_hours": args.get("ttl_hours", 72),
                "scan_summary": findings,
                "content_digest": d.get("content_digest", ""),
                "override_scan_required": True,
            })
            return (
                f"Deploy blocked by scan ({d.get('count', '?')} non-credential "
                f"finding(s)):\n{findings}\n\n"
                f"These findings are overridable by a HUMAN: the deploy now "
                f"appears under \"Pending confirmations\" on the Artifact "
                f"Deploy page, where the user can review the findings and "
                f"explicitly deploy anyway (or dismiss)."
            )
        # Preview response (requires_confirm is always true for the tool path)
        # Persist as a pending confirmation so the dashboard UI can execute it.
        from kiro_crew.deploy.pending import add_pending
        pending_params = {
            "site_id": args["site_id"],
            "artifact_slug": args.get("artifact_slug", ""),
            "local_dir": args.get("local_dir", ""),
            "profile": d.get("profile", args.get("profile", "")),
            "region": d.get("region", deploy_body.get("region", "")),
            "ttl_hours": args.get("ttl_hours", 72),
            "scan_summary": d.get("scan", "clean"),
            "content_digest": d.get("content_digest", ""),
        }
        add_pending(pending_params)
        return (
            f"Deploy preview for site '{args['site_id']}':\n"
            f"  Public: {d.get('public', True)}\n"
            f"  Size: {d.get('bytes', '?')} bytes\n"
            f"  Scan: {_deploy_redact(str(d.get('scan', 'clean')))}\n"
            f"  TTL: {args.get('ttl_hours', 72)} hours\n"
            f"\nThis deploy now appears under \"Pending confirmations\" on the "
            f"Artifact Deploy page in the dashboard. Open it to confirm or dismiss."
        )

    if name == "autonudge_stop":
        # Defense-in-depth: _call_tool() already validates via _validate_args;
        # re-validate here so schema enforcement is visible at the extraction
        # point (matches spawn_run pattern above).
        args = validate_tool_args(args, AUTONUDGE_STOP_SCHEMA)

        # Resolve the current session's binding key and stop any loop on it.
        # STRICT resolution (env-var only, no PID walk): this tool mutates
        # another process's persistent loop state, and a subagent lives under
        # the parent slot's process tree — a PID-walk would let it silently
        # stop the PARENT session's loop (matches set_project's rule).
        sk = _resolve_session_key_strict()
        # AutoNudge binds to dashboard chat slots and slack:/discord: channel
        # sessions — never to "cron:<id>", "hook:<id>", "subagent:<id>", etc.
        slot_key = _autonudge_binding_key(sk)
        if slot_key is None:
            sel().log_tool_invocation(
                session_key=sk, source="mcp", tool_name="autonudge_stop", outcome="noop"
            )
            return (
                "No auto-nudge loop to stop: this tool only works from within "
                "a dashboard, Slack, or Discord session "
                f"(current session_key={sk!r})."
            )
        reason = args.get("reason", "").strip()
        # /api/autonudge* rejects X-Internal-Secret and requires a user-scoped
        # token, so use the token-aware helpers (bootstrapped via
        # /api/token/local) rather than the plain internal-secret _get/_delete.
        lookup = _get_user(f"/api/autonudge/slot/{quote(slot_key, safe='')}")
        if lookup.get("error"):
            sel().log_tool_invocation(
                session_key=sk, source="mcp", tool_name="autonudge_stop", outcome="error"
            )
            return f"Failed to look up loop: {lookup['error']}"
        loop = lookup.get("loop")
        if not loop:
            sel().log_tool_invocation(
                session_key=sk, source="mcp", tool_name="autonudge_stop", outcome="noop"
            )
            return "No active auto-nudge loop on this session — nothing to stop."
        loop_id = loop.get("id", "")
        resp = _delete_user(f"/api/autonudge/{loop_id}")
        if resp.get("error"):
            sel().log_tool_invocation(
                session_key=sk, source="mcp", tool_name="autonudge_stop", outcome="error"
            )
            return f"Failed to stop loop {loop_id}: {resp['error']}"
        sel().log_tool_invocation(
            session_key=sk,
            source="mcp",
            tool_name="autonudge_stop",
            outcome="success",
            metadata={"slot_key": slot_key, "loop_id": loop_id, "reason": reason},
        )
        return (
            f"Auto-nudge loop {loop_id} stopped on session {slot_key}"
            + (f" (reason: {reason})" if reason else "")
            + ". No further nudges will fire."
        )

    if name == "monitor_start":
        args = validate_tool_args(args, MONITOR_START_SCHEMA)
        # STRICT resolution (env-var only, no PID walk): monitor_start creates
        # a persistent unattended loop that repeatedly runs tools in the bound
        # session. A subagent under the parent's process tree must NOT be able
        # to PID-walk into the parent's identity and mint a loop the parent
        # user never asked for (crosses the session authorization boundary).
        sk = _resolve_session_key_strict()
        binding_key = _autonudge_binding_key(sk)
        if binding_key is None:
            sel().log_tool_invocation(
                session_key=sk, source="mcp", tool_name="monitor_start", outcome="noop"
            )
            return (
                "monitor_start only works from within a dashboard, Slack, or "
                f"Discord session (current session_key={sk!r}). For other "
                "contexts use cron_add or a HEARTBEAT.md task."
            )
        message = args["message"].strip()
        if not message:
            return "monitor_start: message must not be empty."
        interval_secs = int(args.get("interval_secs") or 300)
        max_cycles = int(args.get("max_cycles") or 0)
        resp = _post_user(
            "/api/autonudge",
            {
                "session_key": binding_key,
                "message": message,
                "idle_secs": interval_secs,
                "max_cycles": max_cycles,
            },
        )
        if resp.get("error"):
            sel().log_tool_invocation(
                session_key=sk, source="mcp", tool_name="monitor_start", outcome="error"
            )
            return f"Failed to start monitor loop: {resp['error']}"
        loop = resp.get("loop") or {}
        sel().log_tool_invocation(
            session_key=sk,
            source="mcp",
            tool_name="monitor_start",
            outcome="success",
            metadata={
                "binding_key": binding_key,
                "loop_id": loop.get("id", ""),
                "interval_secs": interval_secs,
                "max_cycles": max_cycles,
            },
        )
        return (
            f"Monitor loop {loop.get('id', '?')} started on session {binding_key}: "
            f"the message will be re-injected into this session after every "
            f"~{interval_secs}s of idle"
            + (f", stopping after {max_cycles} cycles" if max_cycles else "")
            + ". End your turn now — the loop wakes you. Call autonudge_stop "
            "when the exit condition is met."
        )

    if name == "search_chat_history":
        args = validate_tool_args(args, SEARCH_CHAT_HISTORY_SCHEMA)
        query = args["query"]
        limit = args.get("limit", 10)
        all_workspaces = args.get("all_workspaces", False)
        # A supplied-but-unparseable date (e.g. 2026-02-30 passes the regex but is
        # not a real calendar date) must ERROR, not be silently dropped — a silent
        # drop would return the UNFILTERED set and mislead the caller.
        after_epoch = before_epoch = None
        if args.get("after"):
            after_epoch = _parse_iso_date_epoch(args["after"])
            if after_epoch is None:
                return "Invalid 'after' date — use a real calendar date (YYYY-MM-DD)."
        if args.get("before"):
            before_epoch = _parse_iso_date_epoch(args["before"])
            if before_epoch is None:
                return "Invalid 'before' date — use a real calendar date (YYYY-MM-DD)."

        cl = ConversationLog()
        session_key = _resolve_session_key()
        # Default scoping: confine to the caller's workspace (fail-closed — unset
        # buckets to "default"). all_workspaces opts out.
        current_ws: str | None = None if all_workspaces else _caller_workspace(cl, session_key)

        # Fetch the FULL ranked match set (bounded by the backend's scan window),
        # not a fixed limit*3 over-fetch: heavy incognito/workspace/date drops on
        # the first page could otherwise starve a caller whose real matches rank
        # lower, returning "no results" while hits exist.
        ranked: list[dict] = cl.search_sessions(query, limit=_SEARCH_HISTORY_SCAN)

        results: list[dict] = []
        for meta in ranked:
            key = meta.get("key", "")
            if not key:
                continue
            # TOCTOU: the file may be unlinked (clear-sessions, rotation, concurrent
            # process) between the ranked snapshot and this read. has_log is the
            # existence gate so we never emit a ghost row for a session the read
            # tool can no longer retrieve. Do NOT additionally require non-empty
            # metadata: a legacy session whose file predates the metadata line
            # returns {} here yet get_chat_session serves it fine, so rejecting {}
            # would hide those sessions from search while they remain readable.
            if not cl.has_log(key):
                continue
            full_meta = cl.get_metadata(key)
            if _history_is_incognito(full_meta) or _history_is_incognito(meta):
                continue  # EB-5: incognito/temporary never surface
            if current_ws is not None and _ws_bucket(full_meta.get("workspace")) != current_ws:
                continue  # EB-cc3: workspace scoping (fail-closed; normalizes non-str)
            modified = meta.get("modified", 0) or 0
            if after_epoch is not None and modified < after_epoch:
                continue
            if before_epoch is not None and modified >= before_epoch:
                continue

            snippet = _extract_history_snippet(cl.read_messages(key), query)
            results.append(
                {
                    "session_key": key,
                    "title": meta.get("title") or key,
                    "date": meta.get("created") or "",
                    "snippet": snippet,
                }
            )
            if len(results) >= limit:
                break

        if not results:
            sel().log_tool_invocation(
                session_key=session_key,
                source="mcp",
                tool_name="search_chat_history",
                outcome="no_results",
                metadata={"query_len": len(query)},
            )
            return "No matching conversations found. Try different keywords."

        lines = [
            "\U0001f50e Chat history matches "
            "(snippets only — use get_chat_session to read a full thread):"
        ]
        for r in results:
            lines.append("\n---")
            lines.append(f"**{r['title']}**  ·  `{r['session_key']}`")
            if r["date"]:
                lines.append(f"_{r['date']}_")
            if r["snippet"]:
                lines.append(f"\n{r['snippet']}")

        output = "\n".join(lines)
        # EB-6: redact secrets/exfil URLs from snippets before returning.
        output = _redact_history_output(output)
        sel().log_tool_invocation(
            session_key=session_key,
            source="mcp",
            tool_name="search_chat_history",
            outcome="success",
            metadata={"query_len": len(query), "result_count": len(results)},
        )
        return output

    if name == "get_chat_session":
        args = validate_tool_args(args, GET_CHAT_SESSION_SCHEMA)
        key = args["session_key"]
        max_messages = args.get("max_messages", 50)
        all_workspaces = args.get("all_workspaces", False)

        # Defense-in-depth on a path-bearing identifier: ConversationLog._safe_key
        # already neutralizes separators. Reject path separators outright, and ".."
        # only as a STANDALONE component — not as a substring — so legitimate keys
        # like "dashboard_chat-2..3" round-trip between search and read. (A strict
        # allowlist regex is avoided: real keys legitimately contain ':' and '.')
        if "/" in key or "\\" in key or key in ("..", "."):
            sel().log_tool_invocation(
                session_key=_resolve_session_key(),
                source="mcp",
                tool_name="get_chat_session",
                outcome="rejected_bad_key",
            )
            return "Invalid session_key."

        cl = ConversationLog()
        if not cl.has_log(key):
            sel().log_tool_invocation(
                session_key=_resolve_session_key(),
                source="mcp",
                tool_name="get_chat_session",
                outcome="not_found",
            )
            # Do NOT echo the raw caller-supplied key: the dashboard renders it as
            # live markdown, so a crafted key (e.g. "[x](https://evil/)") would be a
            # reflected phishing/prompt-injection payload. Return a stable
            # fingerprint instead — enough to correlate, safe to render. (Not a
            # security signature — just a display-safe correlation id — but use
            # sha256 anyway so no weak-hash scanner flags this egress path.)
            fp = hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:12]
            return f"No conversation found for that session_key (fp:{fp})."

        meta = cl.get_metadata(key)
        if _history_is_incognito(meta):
            # EB-7b: no bypass of incognito exclusion via direct fetch.
            sel().log_tool_invocation(
                session_key=_resolve_session_key(),
                source="mcp",
                tool_name="get_chat_session",
                outcome="refused_incognito",
            )
            return "That conversation is private (incognito/temporary) and cannot be read."

        # Deny-by-default workspace isolation: mirror search_chat_history's
        # fail-closed scoping so a caller can't bypass it by fetching a session
        # from another workspace directly. Unset/non-string workspaces bucket as
        # "default" via _ws_bucket.
        if not all_workspaces:
            caller_ws = _caller_workspace(cl, _resolve_session_key())
            if _ws_bucket(meta.get("workspace")) != caller_ws:
                sel().log_tool_invocation(
                    session_key=_resolve_session_key(),
                    source="mcp",
                    tool_name="get_chat_session",
                    outcome="denied_cross_workspace",
                )
                return "Access denied: that conversation belongs to a different workspace."

        messages = cl.recent(key, max_messages=max_messages, roles={"user", "assistant"})
        if not messages:
            sel().log_tool_invocation(
                session_key=_resolve_session_key(),
                source="mcp",
                tool_name="get_chat_session",
                outcome="empty",
            )
            return _redact_history_output(f"Conversation `{key}` has no readable messages.")

        title = meta.get("title") or key
        lines = [f"\U0001f4dc Conversation: **{title}**  ·  `{key}`", ""]
        for m in messages:
            role = str(m.get("role", "?")).title()
            lines.append(f"**{role}:** {m.get('content', '')}")
            lines.append("")

        output = _redact_history_output("\n".join(lines))
        sel().log_tool_invocation(
            session_key=_resolve_session_key(),
            source="mcp",
            tool_name="get_chat_session",
            outcome="success",
            metadata={"message_count": len(messages)},
        )
        return output

    if name == "list_sessions":
        args = validate_tool_args(args, LIST_SESSIONS_SCHEMA)
        limit = args.get("limit", 20)
        all_workspaces = args.get("all_workspaces", False)
        summarize = args.get("summarize", False)

        cl = ConversationLog()
        session_key = _resolve_session_key()
        list_ws: str | None = None if all_workspaces else _caller_workspace(cl, session_key)

        rows: list[dict] = []
        for meta in cl.list_sessions():
            key = meta.get("key", "")
            if not key:
                continue
            if _history_is_incognito(meta):
                continue  # incognito/temporary never surface
            if list_ws is not None:
                # list_sessions() rows omit `workspace`, so scope off the full
                # metadata line (mirrors search_chat_history). Runs in the MCP
                # process, not the gateway loop, so the extra read is fine.
                if _ws_bucket(cl.get_metadata(key).get("workspace")) != list_ws:
                    continue  # fail-closed workspace scoping
            rows.append(meta)
            if len(rows) >= limit:
                break

        if not rows:
            sel().log_tool_invocation(
                session_key=session_key,
                source="mcp",
                tool_name="list_sessions",
                outcome="no_results",
            )
            return "No sessions found in this workspace yet."

        # Opt-in: ask the gateway (which owns the LLM background session) to
        # generate fresh one-line summaries for the returned keys. Best-effort —
        # any failure falls back to titles, so the list is always returned.
        summaries: dict[str, str] = {}
        if summarize:
            resp = _post(
                "/api/sessions/summarize",
                {"keys": [r["key"] for r in rows]},
                timeout=120,
            )
            if isinstance(resp, dict) and isinstance(resp.get("summaries"), dict):
                summaries = {str(k): str(v) for k, v in resp["summaries"].items() if v}

        scope_label = "across all workspaces" if all_workspaces else "in this workspace"
        lines = [f"\U0001f5c2\ufe0f Sessions {scope_label} ({len(rows)}, newest first):"]
        for r in rows:
            key = r["key"]
            title = r.get("title") or key
            agent = r.get("agent")
            msgs = r.get("messages", 0)
            created = r.get("created", "")
            meta_bits = []
            if agent:
                meta_bits.append(f"agent={agent}")
            meta_bits.append(f"~{msgs} msgs")
            if created:
                meta_bits.append(str(created)[:16])
            lines.append("\n---")
            lines.append(f"**{title}**  ·  `{key}`")
            lines.append(f"_{'  ·  '.join(meta_bits)}_")
            summary = summaries.get(key)
            if summary:
                lines.append(f"\n{summary}")

        output = _redact_history_output("\n".join(lines))
        sel().log_tool_invocation(
            session_key=session_key,
            source="mcp",
            tool_name="list_sessions",
            outcome="success",
            metadata={"result_count": len(rows), "summarized": len(summaries)},
        )
        return output

    if name == "local_knowledge_search":
        args = validate_tool_args(args, LOCAL_KNOWLEDGE_SEARCH_SCHEMA)
        query = args["query"]
        limit = args.get("limit", 3)

        db_path = Path(config_dir()) / "workspace" / "knowledge" / "knowledge.db"
        if not db_path.exists():
            sel().log_tool_invocation(
                session_key=_resolve_session_key(),
                source="mcp",
                tool_name="local_knowledge_search",
                outcome="not_configured",
            )
            return "Knowledge Library is not configured. Ingest documents via the dashboard first."

        # Reuse a cached store + embedder across calls; rebuilt only when the
        # knowledge DB (or its -wal) or config.json changes (see
        # _get_knowledge_search). Avoids the per-call schema/migrate/graph-load
        # and the embedder availability probe.
        cfg_path = Path(config_dir()) / "config.json"
        store, embedder = _get_knowledge_search(db_path, cfg_path)
        embed_fn = embedder.embed if embedder and embedder.is_available() else None
        retriever = HybridRetriever(store, embedder=embed_fn)

        results = retriever.search(query, limit=limit)

        # Filter by minimum confidence score
        min_score = 0.012
        results = [r for r in results if r.get("score", 0) >= min_score]

        if not results:
            sel().log_tool_invocation(
                session_key=_resolve_session_key(),
                source="mcp",
                tool_name="local_knowledge_search",
                outcome="no_results",
                metadata={"query": query},
            )
            return "No relevant knowledge found."

        # Format output. Source identity (source_type/source_name/source_uri)
        # and the per-document locator (file_path for folders, artifact_slug +
        # artifact_name for artifacts) are attached by HybridRetriever
        # (_attach_citation_sources).
        lines = [
            "\U0001f4da Knowledge Library "
            "(supplementary reference \u2014 extract only what's relevant to the question):"
        ]
        for r in results:
            title = r.get("title") or "(untitled)"
            source_type = r.get("source_type") or ""
            artifact_slug = r.get("artifact_slug")
            artifact_name = r.get("artifact_name")
            # Document identity shown before the section. For artifacts this is
            # the artifact's own name -- the aggregate "Artifacts" source name
            # carries no information; for every other type it's the source name.
            if source_type == "artifact":
                source = artifact_name or r.get("source_name") or artifact_slug or ""
            else:
                source = r.get("source_name") or ""
            content = r.get("content", "")
            lines.append("\n---")
            lines.append(f"## {title}")
            if source:
                # Citation: [type] name, then section + line range when present.
                cite = "**Source:**"
                if source_type:
                    cite += f" [{source_type}]"
                cite += f" {source}"
                section = r.get("section_title")
                if section:
                    cite += f" \u2014 {section}"
                chunk_range = r.get("chunk_range")
                if chunk_range:
                    cite += f" (lines {chunk_range})"
                lines.append(cite)
                # The most specific locator the source type affords, mirroring
                # the folder File: line.
                file_path = r.get("file_path")
                uri = r.get("source_uri") or ""
                if file_path:
                    lines.append(f"**File:** {file_path}")
                elif artifact_slug:
                    lines.append(f"**Artifact:** {artifact_slug}")
                elif uri:
                    lines.append(f"**Link:** {uri}")
            lines.append(f"\n{content}")

        output = "\n".join(lines)
        output, _ = redact_exfiltration_urls(output)
        output, _ = redact_credentials(output)
        sel().log_tool_invocation(
            session_key=_resolve_session_key(),
            source="mcp",
            tool_name="local_knowledge_search",
            outcome="success",
            metadata={"query": query, "result_count": len(results)},
        )
        return output

    if name == "knowledge_dedup":
        args = validate_tool_args(args, KNOWLEDGE_DEDUP_SCHEMA)
        apply = bool(args.get("apply", False))
        db_path = Path(config_dir()) / "workspace" / "knowledge" / "knowledge.db"
        if not db_path.exists():
            sel().log_tool_invocation(
                session_key=_resolve_session_key(),
                source="mcp",
                tool_name="knowledge_dedup",
                outcome="not_configured",
            )
            return "Knowledge Library is not configured. Ingest documents via the dashboard first."
        store = KnowledgeStore(str(db_path))
        try:
            results = dedup_sweep(store, apply=apply)
        finally:
            store.db.close()
        sel().log_tool_invocation(
            session_key=_resolve_session_key(),
            source="mcp",
            tool_name="knowledge_dedup",
            outcome="applied" if apply else "preview",
            metadata={"duplicate_count": len(results), "apply": apply},
        )
        if not results:
            return "No cross-source duplicate documents found."
        mode = "Deleted" if apply else "Would delete (dry run; set apply=true to delete)"
        lines = [f"{mode} — {len(results)} duplicate document(s):"]
        for r in results:
            lines.append(
                f"- {r['loser']} ({r['items_deleted']} chunks) -> kept "
                f"{r['winner']} [{r['reason']}]"
            )
        output = "\n".join(lines)
        output, _ = redact_exfiltration_urls(output)
        output, _ = redact_credentials(output)
        return output

    if name == "browse_outline":
        snapshot = args.get("snapshot", "")
        max_lines = args.get("max_lines", 100)
        result = _compress_snapshot_to_outline(snapshot, max_lines)
        result, _ = redact_exfiltration_urls(result)
        result, _ = redact_credentials(result)
        sel().log_tool_invocation(
            session_key=_resolve_session_key(),
            source="mcp",
            tool_name="browse_outline",
            outcome="success",
        )
        return result

    if name == "browse_search":
        snapshot = args.get("snapshot", "")
        query = args.get("query", "")
        max_results = args.get("max_results", 50)
        result = _search_snapshot(snapshot, query, max_results)
        result, _ = redact_exfiltration_urls(result)
        result, _ = redact_credentials(result)
        sel().log_tool_invocation(
            session_key=_resolve_session_key(),
            source="mcp",
            tool_name="browse_search",
            outcome="success",
        )
        return result

    if name == "set_project":
        # Defense-in-depth: _call_tool() already validates, but the explicit
        # call here keeps the schema gate visible at the extraction site.
        args = validate_tool_args(args, SET_PROJECT_SCHEMA)
        path = args.get("path", "")
        sk = _resolve_session_key_strict()
        if not sk.startswith("dashboard:"):
            sel().log_tool_invocation(
                session_key=sk or "<unresolved>",
                source="mcp",
                tool_name="set_project",
                outcome="rejected",
                error="non-dashboard or unresolved session",
            )
            return (
                "Error: set_project only works in dashboard sessions with explicit "
                "identity. Slack, cron, and subagent contexts are rejected to avoid "
                "cross-context state mutation."
            )
        slot_name = sk[len("dashboard:") :]
        d = _post(f"/api/chat/slots/{slot_name}/project", {"project": path})
        err_val = d.get("error")
        if err_val:
            sel().log_tool_invocation(
                session_key=sk,
                source="mcp",
                tool_name="set_project",
                outcome="error",
                error=str(err_val),
            )
            return f"Error: {err_val}"
        sel().log_tool_invocation(
            session_key=sk,
            source="mcp",
            tool_name="set_project",
            outcome="success",
        )
        new_project = d.get("project") or ""
        if not new_project:
            return "Project cleared. The next message will cold-start with no project scope."
        return (
            f"Project set to {new_project}. The session will cold-start with the new "
            "CWD and project-level .kiro/steering on the next message."
        )

    def _redact_obj(obj: Any) -> Any:
        """Recursively redact credentials + exfiltration URLs from a response."""
        if isinstance(obj, str):
            s, _ = redact_exfiltration_urls(obj)
            s, _ = redact_credentials(s)
            return s
        if isinstance(obj, list):
            return [_redact_obj(x) for x in obj]
        if isinstance(obj, dict):
            return {k: _redact_obj(v) for k, v in obj.items()}
        return obj

    # --- Dynamic workflows (M6): author / run / monitor from chat ---
    # All workflow tools share this exit path: redact LLM-derived strings (run
    # names, authored source, results, errors can all be LLM output) AND emit a SEL
    # audit event, before any value reaches the dashboard/LLM surface — consistent
    # with browse_*/spawn_* tools above (security-controls guideline).
    def _wf_return(tool: str, text: str, *, outcome: str = "success") -> str:
        safe, _ = redact_exfiltration_urls(text)
        safe, _ = redact_credentials(safe)
        sel().log_tool_invocation(
            session_key=_resolve_session_key(),
            source="mcp",
            tool_name=tool,
            outcome=outcome,
        )
        return safe

    if name == "workflow_author":
        args = validate_tool_args(args, WORKFLOW_AUTHOR_SCHEMA)
        intent = (args.get("intent") or "").strip()
        if not intent:
            return _wf_return("workflow_author", "Error: intent is required", outcome="error")
        d = _post("/api/workflows/author", {"intent": intent})
        if d.get("error"):
            return _wf_return(
                "workflow_author", f"workflow_author failed: {d['error']}", outcome="error"
            )
        if not d.get("ok"):
            return _wf_return(
                "workflow_author",
                "Could not author a valid workflow: " + "; ".join(d.get("errors", [])),
                outcome="error",
            )
        return _wf_return(
            "workflow_author",
            "Authored workflow. Review then run it with workflow_run(source=…):\n\n"
            f"{d.get('source', '')}",
        )

    if name == "workflow_run":
        args = validate_tool_args(args, WORKFLOW_RUN_SCHEMA)
        source = args.get("source") or ""
        intent = (args.get("intent") or "").strip()
        wf_body: dict[str, Any] = {}
        if args.get("name"):
            wf_body["name"] = args["name"]
        if isinstance(args.get("args"), dict):
            wf_body["args"] = args["args"]
        if isinstance(args.get("budget_total"), int):
            wf_body["budget_total"] = args["budget_total"]
        if not source and intent:
            # Author-in-run (M6.7): returns a run_id INSTANTLY — the script is
            # authored inside the background run as a visible "Authoring" phase, so
            # the slow model call never blocks this tool (no 30s author timeout).
            wf_body["intent"] = intent
            d = _post("/api/workflows/run_intent", wf_body)
            if d.get("error"):
                return _wf_return(
                    "workflow_run", f"workflow_run failed: {d['error']}", outcome="error"
                )
            return _wf_return(
                "workflow_run",
                f"Started workflow run `{d.get('run_id')}`. It is authoring the workflow "
                "from your request now (watch the Authoring phase in the Workflows tab / "
                "chat activity), then runs in the background. Its result will be injected "
                f"here on completion — or check progress with workflow_status('{d.get('run_id')}').",
            )
        if not source:
            return _wf_return(
                "workflow_run", "Error: provide either 'source' or 'intent'", outcome="error"
            )
        wf_body["source"] = source
        d = _post("/api/workflows/run", wf_body)
        if d.get("error"):
            return _wf_return("workflow_run", f"workflow_run failed: {d['error']}", outcome="error")
        return _wf_return(
            "workflow_run",
            f"Started workflow run `{d.get('run_id')}` (name: {d.get('name') or '—'}). "
            "It runs in the background — monitor with workflow_status, and its result "
            "will be injected here on completion. You can keep working; check back with "
            f"workflow_status('{d.get('run_id')}').",
        )

    if name == "workflow_status":
        args = validate_tool_args(args, WORKFLOW_RUN_ID_SCHEMA)
        run_id = args.get("run_id", "")
        d = _get(f"/api/workflows/runs/{run_id}")
        # A *failed* run's snapshot legitimately carries its own ``error`` field
        # (its failure message) alongside ``run_id`` — that is NOT a transport
        # error. Only bail early when the response is a bare transport/404 error
        # (``{"error": ...}`` with no ``run_id``); otherwise report the run,
        # including its failure message.
        if d.get("error") and "run_id" not in d:
            return _wf_return("workflow_status", f"workflow_status: {d['error']}", outcome="error")
        # ``error`` (and ``name``) are LLM-derived — redact before surfacing them
        # to the dashboard/chat (credentials + exfiltration URLs).
        safe_err = _redact_obj(d["error"]) if d.get("error") else ""
        safe_name = _redact_obj(d.get("name") or "—")
        return _wf_return(
            "workflow_status",
            f"Run `{d.get('run_id')}` ({safe_name}): **{d.get('status')}** "
            f"— {d.get('event_count', 0)} events" + (f"; error: {safe_err}" if safe_err else ""),
        )

    if name == "workflow_result":
        args = validate_tool_args(args, WORKFLOW_RUN_ID_SCHEMA)
        run_id = args.get("run_id", "")
        d = _get(f"/api/workflows/runs/{run_id}")
        # As in workflow_status: a failed run carries its own ``error`` in the
        # snapshot. Distinguish a real transport error (no ``run_id``) from a
        # failed-but-readable run so a failed run still returns its full event
        # stream instead of masquerading as a transport failure.
        if d.get("error") and "run_id" not in d:
            return _wf_return("workflow_result", f"workflow_result: {d['error']}", outcome="error")
        # ``result`` / ``error`` / ``events`` are LLM-derived (agent outputs, log
        # lines) — recursively redact credentials + exfiltration URLs before
        # returning them through this MCP tool to the dashboard/chat surface.
        return _wf_return(
            "workflow_result",
            json.dumps(
                {
                    "run_id": d.get("run_id"),
                    "status": d.get("status"),
                    "result": _redact_obj(d.get("result")),
                    "error": _redact_obj(d.get("error")),
                    "events": _redact_obj(d.get("events", [])),
                },
                indent=2,
                default=str,
            ),
        )

    if name == "workflow_list":
        d = _get("/api/workflows/runs")
        if d.get("error"):
            return _wf_return("workflow_list", f"workflow_list: {d['error']}", outcome="error")
        runs = d.get("runs", [])
        if not runs:
            return _wf_return("workflow_list", "No workflow runs yet.")
        lines = [
            f"- `{r.get('run_id')}` {r.get('name') or '—'} → {r.get('status')} "
            f"({r.get('event_count', 0)} events)"
            for r in runs
        ]
        return _wf_return("workflow_list", "Workflow runs (newest first):\n" + "\n".join(lines))

    if name == "workflow_cancel":
        args = validate_tool_args(args, WORKFLOW_RUN_ID_SCHEMA)
        run_id = args.get("run_id", "")
        d = _post(f"/api/workflows/runs/{run_id}/cancel", {})
        if d.get("error"):
            return _wf_return("workflow_cancel", f"workflow_cancel: {d['error']}", outcome="error")
        return _wf_return(
            "workflow_cancel",
            f"Run `{run_id}`: {'cancelled' if d.get('cancelled') else 'not cancellable (already done?)'}",
        )

    if name == "workflow_rerun_subtree":
        args = validate_tool_args(args, WORKFLOW_RERUN_SCHEMA)
        run_id = args.get("run_id", "")
        from_index = args.get("from_index", 0)
        d = _post(
            f"/api/workflows/runs/{run_id}/rerun",
            {"from_index": from_index if isinstance(from_index, int) else 0},
        )
        if d.get("error"):
            return _wf_return(
                "workflow_rerun_subtree", f"workflow_rerun_subtree: {d['error']}", outcome="error"
            )
        return _wf_return(
            "workflow_rerun_subtree",
            f"Re-running `{run_id}` as `{d.get('run_id')}` "
            f"(replaying calls before index {d.get('replayed_before')}). "
            f"Monitor with workflow_status('{d.get('run_id')}').",
        )

    return f"Unknown tool: {name}"


def run_mcp_core_server() -> None:
    """Run MCP stdio server for core agent tools."""
    run_mcp_stdio_loop("kirocrew-core", "1.0.0", _list_tools, _call_tool)
