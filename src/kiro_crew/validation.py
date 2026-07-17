"""Centralized input/output validation for MCP tools and API endpoints.

All tool inputs from untrusted sources (LLM, end user, other MCP tools)
are validated here before execution.  Responses are sanitized and
truncated before returning to callers.

Implements: SDO-183 (Tool Input and Response Validation)
- Schema validation with type enforcement
- Length and size limits
- Unicode normalization and hidden character stripping
- Allow-list approach for enums and key patterns
- Semantic/business logic checks (positive numbers, valid timestamps, etc.)
- Response truncation to prevent resource exhaustion
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

# ── Constants ──

# Max lengths for string inputs
MAX_TOOL_NAME_LEN = 256
MAX_SHORT_STRING = 500  # names, IDs, categories
MAX_MEDIUM_STRING = 5_000  # messages, rules
MAX_LONG_STRING = 50_000  # task specs, inline content
MAX_RESPONSE_LEN = 100_000  # truncate tool responses

# Allowed categories for lessons
ALLOWED_LESSON_CATEGORIES = frozenset({"tool", "preference", "knowledge"})

# Allowed scopes for lessons (mirrors the learn_add MCP inputSchema enum).
ALLOWED_LESSON_SCOPES = frozenset({"global", "workspace"})

# Allowed cron schedule kinds
ALLOWED_SCHEDULE_KINDS = frozenset({"every", "cron", "at"})

# Allowed hook events
ALLOWED_HOOK_EVENTS = frozenset(
    {"AgentSpawn", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"}
)

# Valid agent name pattern (alphanumeric, hyphens, underscores)
_AGENT_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}[a-zA-Z0-9]$|^[a-zA-Z0-9]$")

# Artifact slug grammar — mirrors kiro_crew.artifacts._SLUG_RE (kept here so
# consumers outside the store module share one public definition). Used to
# validate the companion-chat `artifact` slot binding (Mesh-2772) at EVERY
# boundary it crosses: slot create (chat_handlers) and history-metadata
# restore (chat_persistence) — a tampered history JSONL must not be able to
# inject an arbitrary string that flows into to_dict()/WS broadcasts.
# \Z (not $): Python's $ also matches just before a trailing newline, so
# "valid-slug\n" would pass a $-anchored .match() — \Z anchors at the true
# end of the string.
ARTIFACT_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?\Z")

# Valid model name pattern — alphanumerics, hyphens, dots (e.g. "claude-opus-4.8", "deepseek-3.2")
_MODEL_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")

# Valid workspace name pattern (same rules as agent names)
WORKSPACE_NAME_RE = _AGENT_NAME_RE

# Valid Slack channel ID pattern (exported for reuse in handlers/CLI)
# C = standard channels, D = DM channels, G = legacy private channels (pre-2022),
# W = Slack Connect shared channels (cross-org)
CHANNEL_ID_RE = re.compile(r"^[CDGW][A-Z0-9]+$")
CHANNEL_MAX_LEN = 20
# Valid Slack user ID pattern (U or W prefix, max 20 chars total)
USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{1,19}$")
USER_MAX_LEN = 20

# Slack thread/message timestamp (e.g. "1781215864.487849"): a 10+ digit epoch
# seconds component, a dot, then 6+ digits of sub-second precision. Slack
# threads now key their session off the canonical namespaced form
# ``slack:<thread_ts>`` (see messaging/link.py and slack/handler.py), but the
# legacy bare thread_ts form persists in older session maps, conversation logs,
# and callers — distinct from the "slack:<chan>:<ts>" delivery-target form —
# so callers that authorize by session key must accept both the bare shape and
# the ``slack:`` prefix (see ``is_slack_ns`` in ``api_lessons_create``).
#
# Use the explicit ASCII class ``[0-9]`` (not ``\d``): in Python 3 ``\d`` also
# matches non-ASCII Unicode decimal digits (Arabic-Indic ٠-٩, Devanagari ०-९,
# etc.). Because this pattern gates an authorization decision (``is_slack_ns``
# in ``api_lessons_create``), ``\d`` would let a crafted key built from Unicode
# digits pass as a Slack thread_ts, matching the ASCII-only intent of the other
# patterns in this file (e.g. ``CHANNEL_ID_RE``).
SLACK_THREAD_TS_RE = re.compile(r"^[0-9]{10,}\.[0-9]{6,}$")


def infer_use_case(session_key: str) -> str:
    """Map a KiroCrew session_key to a categorical useCase label.

    Returns ``"unknown"`` for unrecognized shapes — never raises. Pure string
    matching on the session key; lives here next to ``SLACK_THREAD_TS_RE`` so
    authorization (learn_add) and classification stay in lockstep.

    Phase 1 limitations: ``cli_chat`` and ``_bg`` collapse multiple
    invocations into one session each. Both can be fixed in a follow-up
    by appending a ``:<uuid>`` suffix at the entry point.
    """
    if not session_key:
        return "unknown"
    if session_key.startswith("cron:") or session_key.startswith("cron_"):
        return "cron"
    if session_key.startswith("subagent:") or session_key.startswith("subagent_"):
        return "subagent"
    if session_key == "_bg":
        return "subagent"
    if session_key.startswith("taskrunner_") or session_key.startswith("taskrunner:"):
        return "task-runner"
    if session_key.startswith("dashboard:") or session_key.startswith("chat-"):
        return "dashboard"
    if session_key == "cli_chat" or session_key.startswith("cli_chat:"):
        return "cli"
    if SLACK_THREAD_TS_RE.match(session_key):
        return "slack"
    return "unknown"


# Valid Jira project key pattern (e.g. PROJ, TEAM_X)
JIRA_PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
# Valid Jira site ID pattern (UUID or alphanumeric with hyphens)
JIRA_SITE_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,128}$")
# Valid Jira issue key pattern (e.g. PROJ-123)
JIRA_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
# Valid Jira site URL pattern (Atlassian Cloud only)
JIRA_SITE_URL_RE = re.compile(r"^https://[a-zA-Z0-9][a-zA-Z0-9._-]{0,253}\.atlassian\.net/?$")

# Valid cron job ID pattern (hex)
_JOB_ID_RE = re.compile(r"^[a-f0-9]{1,16}$")

# A cron caller_session is "cron:<job_id>" or "cron:<job_id>:<run_id>".
# Used to validate the field before it escalates send_message routing from
# notification-only to owner Slack DM delivery (a malformed/injected value
# must not abuse that upgrade).
CRON_SESSION_RE = re.compile(r"^cron:[a-zA-Z0-9]+(?::[a-zA-Z0-9]+)?$")

# Hidden Unicode categories to strip (control chars, format chars, etc.)
# Keeps: letters, numbers, punctuation, symbols, separators (space/newline)
_HIDDEN_CATEGORIES = frozenset(
    {
        "Cc",  # control (except \n \r \t)
        "Cf",  # format (zero-width, BOM, directional overrides)
        "Co",  # private use
        "Cs",  # surrogate
    }
)

# Specific chars to always allow even if in a hidden category
_ALLOWED_CONTROL = frozenset({"\n", "\r", "\t"})


# ── Exceptions ──


class ValidationError(Exception):
    """Raised when input validation fails."""

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")


# ── Field Validators ──


@dataclass
class FieldSpec:
    """Declarative field specification for validation."""

    name: str
    type: type | tuple[type, ...]  # expected Python type(s)
    required: bool = False
    max_len: int = 0  # 0 = no limit
    min_val: float | None = None  # for numeric fields
    max_val: float | None = None
    allowed: frozenset[str] | None = None  # enum allow-list
    pattern: re.Pattern[str] | None = None  # regex pattern
    default: Any = None
    item_type: type | None = None  # type: ignore[valid-type]  # for list fields: expected type of each element
    item_max_len: int = 0  # for list fields: max length of each string element
    item_pattern: re.Pattern[str] | None = None  # for list fields: regex for each string element
    max_items: int = 0  # for list fields: max number of items (0 = no limit)


@dataclass
class ToolSchema:
    """Schema for a tool's input arguments."""

    tool_name: str
    fields: list[FieldSpec] = field(default_factory=list)
    custom_validator: Any = None  # Optional callable(cleaned_args) -> None; raises ValidationError


def validate_field(value: Any, spec: FieldSpec) -> Any:
    """Validate and normalize a single field value. Returns cleaned value."""
    if value is None:
        if spec.required:
            raise ValidationError(spec.name, "required")
        return spec.default

    # Type check
    if not isinstance(value, spec.type):
        raise ValidationError(
            spec.name,
            f"expected {spec.type.__name__ if isinstance(spec.type, type) else spec.type}, "
            f"got {type(value).__name__}",
        )

    # bool is a subclass of int, so isinstance(True, int) is True — a bool
    # would otherwise slip through an int field (and pass min/max range checks
    # since True == 1). Reject bool unless it is an explicitly allowed type.
    allowed_types = spec.type if isinstance(spec.type, tuple) else (spec.type,)
    if isinstance(value, bool) and bool not in allowed_types:
        raise ValidationError(
            spec.name,
            f"expected {spec.type.__name__ if isinstance(spec.type, type) else spec.type}, "
            f"got {type(value).__name__}",
        )

    # String validation
    if isinstance(value, str):
        value = sanitize_string(value)
        if not value and spec.required:
            raise ValidationError(spec.name, "required (empty after sanitization)")
        if spec.max_len and len(value) > spec.max_len:
            # Report the actual length + overshoot so a caller (e.g. the LLM
            # composing a learn_add rule) can trim by the exact amount in one
            # pass instead of guessing and re-submitting repeatedly.
            raise ValidationError(
                spec.name,
                f"exceeds max length {spec.max_len} "
                f"(got {len(value)}, trim {len(value) - spec.max_len} chars)",
            )
        if spec.allowed and value not in spec.allowed:
            raise ValidationError(spec.name, f"must be one of: {', '.join(sorted(spec.allowed))}")
        if spec.pattern and value and not spec.pattern.match(value):
            raise ValidationError(spec.name, "invalid format")

    # Numeric validation
    if isinstance(value, (int, float)):
        if spec.min_val is not None and value < spec.min_val:
            raise ValidationError(spec.name, f"must be >= {spec.min_val}")
        if spec.max_val is not None and value > spec.max_val:
            raise ValidationError(spec.name, f"must be <= {spec.max_val}")

    # List item validation
    if isinstance(value, list):
        if spec.max_items and len(value) > spec.max_items:
            raise ValidationError(spec.name, f"exceeds max items {spec.max_items}")
        if spec.item_type:
            for i, item in enumerate(value):
                if not isinstance(item, spec.item_type):
                    raise ValidationError(
                        spec.name,
                        f"item[{i}]: expected {spec.item_type.__name__}, got {type(item).__name__}",
                    )
                if isinstance(item, str):
                    item = sanitize_string(item)
                    value[i] = item
                    if spec.item_max_len and len(item) > spec.item_max_len:
                        raise ValidationError(
                            spec.name, f"item[{i}]: exceeds max length {spec.item_max_len}"
                        )
                    if spec.item_pattern and item and not spec.item_pattern.fullmatch(item):
                        raise ValidationError(spec.name, f"item[{i}]: invalid format")

    return value


def validate_tool_args(args: dict[str, Any], schema: ToolSchema) -> dict[str, Any]:
    """Validate all tool arguments against a schema. Returns cleaned args dict."""
    if not isinstance(args, dict):
        raise ValidationError("args", "must be a JSON object")

    cleaned: dict[str, Any] = {}
    known_fields = {s.name for s in schema.fields}

    # Reject unknown fields
    for key in args:
        if key not in known_fields:
            raise ValidationError(key, f"unknown field for tool '{schema.tool_name}'")

    for spec in schema.fields:
        # Only process fields that are explicitly in args OR are required
        if spec.name in args:
            raw = args[spec.name]
            cleaned[spec.name] = validate_field(raw, spec)
        elif spec.required:
            # Required field missing - validate_field will raise error
            cleaned[spec.name] = validate_field(None, spec)
        elif spec.default is not None:
            # Field not in args, but has a default - include it
            cleaned[spec.name] = spec.default

    if schema.custom_validator:
        schema.custom_validator(cleaned)

    return cleaned


# ── String Sanitization ──


def strip_hidden_unicode(text: str) -> str:
    """Remove hidden Unicode characters (zero-width, directional overrides, etc.).

    Preserves normal whitespace (\\n, \\r, \\t) and all visible characters.
    """
    return "".join(
        ch
        for ch in text
        if ch in _ALLOWED_CONTROL or unicodedata.category(ch) not in _HIDDEN_CATEGORIES
    )


def normalize_unicode(text: str) -> str:
    """NFC-normalize Unicode text to canonical form."""
    return unicodedata.normalize("NFC", text)


def sanitize_string(text: str) -> str:
    """Full sanitization pipeline: normalize → strip hidden chars → strip edges."""
    text = normalize_unicode(text)
    text = strip_hidden_unicode(text)
    return text.strip()


# ── Response Sanitization ──


def sanitize_response(text: str, max_len: int = MAX_RESPONSE_LEN) -> str:
    """Sanitize and truncate a tool response before returning to caller."""
    text = sanitize_string(text)
    if len(text) > max_len:
        text = text[:max_len] + "\n…[response truncated]"
    return text


# ── JSON-RPC Envelope Validation ──


def validate_jsonrpc_request(req: dict[str, Any]) -> tuple[str, Any, dict[str, Any]]:
    """Validate a JSON-RPC 2.0 request envelope.

    Returns (method, id, params). Raises ValidationError on invalid structure.
    """
    if not isinstance(req, dict):
        raise ValidationError("request", "must be a JSON object")
    if req.get("jsonrpc") not in ("2.0", None):
        raise ValidationError("jsonrpc", "must be '2.0'")

    method = req.get("method")
    if method is not None and not isinstance(method, str):
        raise ValidationError("method", "must be a string")

    req_id = req.get("id")
    params = req.get("params", {})
    if not isinstance(params, dict):
        params = {}

    return method or "", req_id, params


# ── Tool Schemas (MCP Core) ──

SPAWN_RUN_SCHEMA = ToolSchema(
    tool_name="spawn_run",
    fields=[
        FieldSpec("task", str, max_len=MAX_MEDIUM_STRING),
        FieldSpec("tasks", list, item_type=str, item_max_len=MAX_MEDIUM_STRING),
        FieldSpec("agent", str, max_len=MAX_SHORT_STRING, pattern=_AGENT_NAME_RE),
        FieldSpec(
            "agents",
            list,
            item_type=str,
            item_max_len=MAX_SHORT_STRING,
            item_pattern=_AGENT_NAME_RE,
        ),
        # 0 = "not set" → falls through to config default via `0 or config_value`
        FieldSpec("max_turns", int, min_val=0, max_val=200),
        # Optional working directory for the subagent subprocess. Must be
        # absolute, exist, and be under subagent_cwd_allowed_roots. Validated
        # in SubagentManager.spawn.
        FieldSpec("cwd", str, max_len=MAX_MEDIUM_STRING),
        # Optional model override for the subagent (e.g. "deepseek-3.2").
        # When set, the subagent runs on this model instead of the gateway default.
        FieldSpec("model", str, max_len=MAX_SHORT_STRING, pattern=_MODEL_NAME_RE),
    ],
)

SPAWN_SUB_AGENTS_SCHEMA = ToolSchema(
    tool_name="spawn_sub_agents",
    fields=[
        # Each item is a dict with prompt (required, max MAX_MEDIUM_STRING) and
        # agent_or_mode (optional, max MAX_SHORT_STRING). Per-field validation
        # enforced in handler (no item_schema support in FieldSpec).
        FieldSpec("agents", list, required=True, item_type=dict),
        FieldSpec("cwd", str, max_len=MAX_MEDIUM_STRING),
    ],
)

LEARN_ADD_SCHEMA = ToolSchema(
    tool_name="learn_add",
    fields=[
        FieldSpec("rule", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("category", str, allowed=ALLOWED_LESSON_CATEGORIES, default="knowledge"),
        FieldSpec("negative", str, max_len=MAX_SHORT_STRING),
        # scope/workspace: the learn_add MCP tool (mcp_core.py) and the
        # /api/lessons handler already support workspace-scoped lessons; this
        # schema was created before that feature landed and never listed them,
        # so unknown-field rejection blocked the params. The "workspace
        # required when scope='workspace'" rule is enforced in the handler.
        FieldSpec("scope", str, allowed=ALLOWED_LESSON_SCOPES, default="global"),
        FieldSpec("workspace", str, max_len=MAX_SHORT_STRING, pattern=WORKSPACE_NAME_RE),
    ],
)

LEARN_REMOVE_SCHEMA = ToolSchema(
    tool_name="learn_remove",
    fields=[
        FieldSpec("query", str, required=True, max_len=MAX_SHORT_STRING),
    ],
)

SPAWN_STATUS_SCHEMA = ToolSchema(
    tool_name="spawn_status",
    fields=[
        FieldSpec("agent_id", str, required=True, max_len=64),
        # Paged / filtered reads of the retained transcript (line-oriented).
        FieldSpec("offset", int, min_val=0, max_val=100_000_000),
        FieldSpec("limit", int, min_val=0, max_val=2000),
        FieldSpec("grep", str, max_len=500),
    ],
)

SPAWN_LIST_SCHEMA = ToolSchema(tool_name="spawn_list")

TASK_RUN_SCHEMA = ToolSchema(
    tool_name="task_run",
    fields=[
        FieldSpec("spec", str, required=True, max_len=MAX_LONG_STRING),
        FieldSpec("name", str, max_len=200),
    ],
)

FILE_SEND_SCHEMA = ToolSchema(
    tool_name="file_send",
    fields=[
        FieldSpec("path", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("description", str, max_len=MAX_SHORT_STRING),
        FieldSpec("channel", str, max_len=MAX_SHORT_STRING),
    ],
)

AUTONUDGE_STOP_SCHEMA = ToolSchema(
    tool_name="autonudge_stop",
    fields=[
        FieldSpec("reason", str, max_len=MAX_SHORT_STRING),
    ],
)

SKILL_SEARCH_SCHEMA = ToolSchema(
    tool_name="skill_search",
    fields=[
        FieldSpec("query", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("limit", int),
    ],
)

# Absolute filesystem path. Empty string is allowed (clears the project) —
# the validator skips the pattern check on empty values, so the regex only
# needs to cover the non-empty case.
_ABSOLUTE_PATH_RE = re.compile(r"^/")

# 4096 = Linux PATH_MAX. The gateway endpoint enforces realpath and
# sensitive-path checks; this schema is the MCP-layer shape gate.


def _validate_set_project(args: dict[str, Any]) -> None:
    clear = args.get("clear", False)
    path = args.get("path", "")
    if clear and path:
        raise ValidationError("path", "path must be empty when clear=true")
    if not clear and not path:
        raise ValidationError("path", "required (use clear=true to unset project)")


SET_PROJECT_SCHEMA = ToolSchema(
    tool_name="set_project",
    fields=[
        FieldSpec("path", str, max_len=4096, pattern=_ABSOLUTE_PATH_RE),
        FieldSpec("clear", bool),
    ],
    custom_validator=_validate_set_project,
)

# --- Dynamic Workflows (M6) ---
_WF_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

WORKFLOW_AUTHOR_SCHEMA = ToolSchema(
    tool_name="workflow_author",
    fields=[
        FieldSpec("intent", str, required=True, max_len=MAX_MEDIUM_STRING),
    ],
)

WORKFLOW_RUN_SCHEMA = ToolSchema(
    tool_name="workflow_run",
    fields=[
        # Either an authored Python script (source) or a NL intent to author one.
        FieldSpec("source", str, max_len=MAX_LONG_STRING),
        FieldSpec("intent", str, max_len=MAX_MEDIUM_STRING),
        FieldSpec("name", str, max_len=MAX_SHORT_STRING),
        FieldSpec("args", dict),
        FieldSpec("budget_total", int, min_val=0, max_val=100_000_000),
    ],
)

WORKFLOW_RUN_ID_SCHEMA = ToolSchema(
    tool_name="workflow_status",
    fields=[
        FieldSpec("run_id", str, required=True, max_len=64, pattern=_WF_RUN_ID_RE),
    ],
)

WORKFLOW_RERUN_SCHEMA = ToolSchema(
    tool_name="workflow_rerun_subtree",
    fields=[
        FieldSpec("run_id", str, required=True, max_len=64, pattern=_WF_RUN_ID_RE),
        FieldSpec("from_index", int, min_val=0, max_val=1_000_000),
    ],
)

# Artifact tools — slug pattern matches kiro_crew.artifacts._SLUG_RE.
_ARTIFACT_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$")
_ARTIFACT_TAG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_:.-]{0,63}$")
_ARTIFACT_KIND_RE = re.compile(r"^(widget|html|markdown|svg|json|text)$")
_ARTIFACT_SOURCE_RE = re.compile(r"^(chat|cron|subagent|manual|import)$")
ARTIFACT_CONTENT_MAX = 1_048_576  # 1 MiB — matches MAX_CONTENT_BYTES in artifacts.py

ARTIFACT_SAVE_SCHEMA = ToolSchema(
    tool_name="artifact_save",
    fields=[
        FieldSpec("name", str, required=True, max_len=200),
        FieldSpec("content", str, required=True, max_len=ARTIFACT_CONTENT_MAX),
        FieldSpec("slug", str, max_len=80, pattern=_ARTIFACT_SLUG_RE),
        FieldSpec("kind", str, max_len=20, pattern=_ARTIFACT_KIND_RE),
        FieldSpec("source", str, max_len=20, pattern=_ARTIFACT_SOURCE_RE),
        FieldSpec("description", str, max_len=2_000),
        FieldSpec(
            "tags",
            list,
            item_type=str,
            item_max_len=64,
            item_pattern=_ARTIFACT_TAG_RE,
            max_items=16,
        ),
        FieldSpec("folder", str, max_len=4096),
    ],
)

ARTIFACT_GET_SCHEMA = ToolSchema(
    tool_name="artifact_get",
    fields=[
        FieldSpec("slug", str, required=True, max_len=80, pattern=_ARTIFACT_SLUG_RE),
        FieldSpec("version", int, min_val=1, max_val=10_000),
    ],
)

ARTIFACT_UPDATE_SCHEMA = ToolSchema(
    tool_name="artifact_update",
    fields=[
        FieldSpec("slug", str, required=True, max_len=80, pattern=_ARTIFACT_SLUG_RE),
        FieldSpec("content", str, max_len=ARTIFACT_CONTENT_MAX),
        FieldSpec("name", str, max_len=200),
        FieldSpec("description", str, max_len=2_000),
        FieldSpec(
            "tags",
            list,
            item_type=str,
            item_max_len=64,
            item_pattern=_ARTIFACT_TAG_RE,
            max_items=16,
        ),
    ],
)

ARTIFACT_DELETE_SCHEMA = ToolSchema(
    tool_name="artifact_delete",
    fields=[
        FieldSpec("slug", str, required=True, max_len=80, pattern=_ARTIFACT_SLUG_RE),
    ],
)

ARTIFACT_LIST_SCHEMA = ToolSchema(
    tool_name="artifact_list",
    fields=[
        FieldSpec("tag", str, max_len=64, pattern=_ARTIFACT_TAG_RE),
        FieldSpec("kind", str, max_len=20, pattern=_ARTIFACT_KIND_RE),
        FieldSpec("q", str, max_len=200),
    ],
)

ARTIFACT_VERSIONS_SCHEMA = ToolSchema(
    tool_name="artifact_versions",
    fields=[
        FieldSpec("slug", str, required=True, max_len=80, pattern=_ARTIFACT_SLUG_RE),
    ],
)

ARTIFACT_REVERT_SCHEMA = ToolSchema(
    tool_name="artifact_revert",
    fields=[
        FieldSpec("slug", str, required=True, max_len=80, pattern=_ARTIFACT_SLUG_RE),
        FieldSpec("target_version", int, required=True, min_val=1, max_val=10_000),
    ],
)

# Artifact folders (Mesh-2720). A folder reference is a folder id OR a
# ``/``-separated human path, so it can't share the slug regex — only bound
# the length. Folder names cap at 100 chars (matches ArtifactFolderStore).
_ARTIFACT_FOLDER_NAME_MAX = 100
_ARTIFACT_FOLDER_REF_MAX = 4096

ARTIFACT_FOLDER_LIST_SCHEMA = ToolSchema(
    tool_name="artifact_folder_list",
    fields=[],
)

ARTIFACT_FOLDER_CREATE_SCHEMA = ToolSchema(
    tool_name="artifact_folder_create",
    fields=[
        FieldSpec("name", str, required=True, max_len=_ARTIFACT_FOLDER_NAME_MAX),
        FieldSpec("parent", str, max_len=_ARTIFACT_FOLDER_REF_MAX),
    ],
)

ARTIFACT_FOLDER_RENAME_SCHEMA = ToolSchema(
    tool_name="artifact_folder_rename",
    fields=[
        FieldSpec("folder", str, required=True, max_len=_ARTIFACT_FOLDER_REF_MAX),
        FieldSpec("name", str, required=True, max_len=_ARTIFACT_FOLDER_NAME_MAX),
    ],
)

ARTIFACT_FOLDER_MOVE_SCHEMA = ToolSchema(
    tool_name="artifact_folder_move",
    fields=[
        FieldSpec("folder", str, required=True, max_len=_ARTIFACT_FOLDER_REF_MAX),
        FieldSpec("new_parent", str, max_len=_ARTIFACT_FOLDER_REF_MAX),
    ],
)

ARTIFACT_FOLDER_DELETE_SCHEMA = ToolSchema(
    tool_name="artifact_folder_delete",
    fields=[
        FieldSpec("folder", str, required=True, max_len=_ARTIFACT_FOLDER_REF_MAX),
        FieldSpec("delete_contents", bool),
    ],
)

ARTIFACT_MOVE_SCHEMA = ToolSchema(
    tool_name="artifact_move",
    fields=[
        FieldSpec("slug", str, required=True, max_len=80, pattern=_ARTIFACT_SLUG_RE),
        FieldSpec("folder", str, max_len=_ARTIFACT_FOLDER_REF_MAX),
    ],
)

# ── Tool Schemas (MCP Cron) ──


def _validate_cron_add_requires_message_or_script(args: dict[str, Any]) -> None:
    if not args.get("message") and not args.get("script") and not args.get("command"):
        raise ValidationError("message", "either 'message', 'script', or 'command' is required")
    if args.get("script") and args.get("command"):
        raise ValidationError("command", "'script' and 'command' are mutually exclusive")


CRON_ADD_SCHEMA = ToolSchema(
    tool_name="cron_add",
    fields=[
        FieldSpec("name", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("message", str, max_len=MAX_MEDIUM_STRING),
        FieldSpec("every", int, min_val=60, max_val=86400 * 30),
        FieldSpec("cron_expr", str, max_len=100),
        FieldSpec("at", (int, float), min_val=0, max_val=4102444800),  # up to 2100
        FieldSpec("delay", (int, float), min_val=1, max_val=86400 * 30),  # 1s to 30 days
        FieldSpec("at_time", str, max_len=100),
        FieldSpec("agent", str, max_len=MAX_SHORT_STRING, pattern=_AGENT_NAME_RE),
        FieldSpec("silent", bool),
        FieldSpec("channel", str, max_len=CHANNEL_MAX_LEN, pattern=CHANNEL_ID_RE),
        FieldSpec("thread_ts", str, max_len=30, pattern=re.compile(r"^\d+\.\d+$")),
        FieldSpec("approval_mode", str, max_len=10, pattern=re.compile(r"^(auto)?$")),
        FieldSpec("skip_dates", list, item_type=str, item_max_len=10, max_items=366, item_pattern=re.compile(r"^\d{4}-\d{2}-\d{2}$")),
        FieldSpec("timezone", str, max_len=50, pattern=re.compile(r"^[A-Za-z0-9_/+-]+$")),
        FieldSpec("persistent_session", bool),
        FieldSpec("minimal_context", bool),
        FieldSpec("hide_in_chat", bool),
        FieldSpec("strict_schedule", bool),
        # SECURITY NOTE: the patterns below are input-SHAPE checks, NOT security
        # sanitizers. The "command" regex only rejects control bytes and the
        # "script" regex only enforces a path:func shape — neither makes the
        # value safe to execute. The enforced security boundary for the
        # model-supplied cron command/script lives elsewhere (finding P454794507):
        #   1. storage-time deny-list  -> mcp_cron._vet_shell_command / _vet_script_file
        #   2. exec-time OS sandbox     -> cron_script.run_command_sandboxed (mode="cc")
        #                                  + _clean_cron_env() env scrubbing
        # Do not treat these regexes as the guard, and do not relax them assuming
        # downstream code re-validates the value as safe.
        FieldSpec("script", str, max_len=200, pattern=re.compile(r"^[a-zA-Z0-9_.~/-]+:[a-zA-Z_][a-zA-Z0-9_]*$")),
        FieldSpec("command", str, max_len=5000, pattern=re.compile(r"^[^\x00-\x1f\x7f]*$")),
        FieldSpec("timeout", int, min_val=0, max_val=3600),
    ],
    custom_validator=_validate_cron_add_requires_message_or_script,
)

CRON_LIST_SCHEMA = ToolSchema(
    tool_name="cron_list",
    fields=[
        FieldSpec("verbose", bool),
        FieldSpec(
            "ids",
            list,
            item_type=str,
            item_max_len=16,
            max_items=200,
            item_pattern=_JOB_ID_RE,
        ),
    ],
)

CRON_REMOVE_SCHEMA = ToolSchema(
    tool_name="cron_remove",
    fields=[
        FieldSpec("job_id", str, required=True, max_len=16, pattern=_JOB_ID_RE),
    ],
)

CRON_PAUSE_SCHEMA = ToolSchema(
    tool_name="cron_pause",
    fields=[
        FieldSpec("job_id", str, required=True, max_len=16, pattern=_JOB_ID_RE),
    ],
)

CRON_RESUME_SCHEMA = ToolSchema(
    tool_name="cron_resume",
    fields=[
        FieldSpec("job_id", str, required=True, max_len=16, pattern=_JOB_ID_RE),
    ],
)

# ── Tool Schemas (Hooks) ──

HOOK_CREATE_SCHEMA = ToolSchema(
    tool_name="hook_create",
    fields=[
        FieldSpec("name", str, required=True, max_len=200),
        FieldSpec("command", str, required=True, max_len=2000),
        FieldSpec("event", str, required=True, allowed=ALLOWED_HOOK_EVENTS),
        FieldSpec("matcher", str, max_len=500, default=""),  # optional: empty = match all
        FieldSpec("timeout", int, min_val=1, max_val=300, default=30),
        FieldSpec("enabled", bool, default=True),
    ],
)

HOOK_UPDATE_SCHEMA = ToolSchema(
    tool_name="hook_update",
    fields=[
        FieldSpec("name", str, max_len=200),  # optional on update
        FieldSpec("command", str, max_len=2000),  # optional on update
        FieldSpec("event", str, allowed=ALLOWED_HOOK_EVENTS),
        FieldSpec("matcher", str, max_len=500),  # optional: empty = match all
        FieldSpec("timeout", int, min_val=1, max_val=300),
        FieldSpec("enabled", bool),
    ],
)

# ── Tool Schemas (File I/O) ──

FILE_READ_SCHEMA = ToolSchema(
    tool_name="file_read",
    fields=[
        FieldSpec(
            "path", str, required=True, max_len=4096, pattern=re.compile(r"^[~/][-\w.@~/ ]+$")
        ),
    ],
)

FILE_WRITE_SCHEMA = ToolSchema(
    tool_name="file_write",
    fields=[
        FieldSpec(
            "path", str, required=True, max_len=4096, pattern=re.compile(r"^[~/][-\w.@~/ ]+$")
        ),
        FieldSpec("content", str, required=True, max_len=512000),
    ],
)

SEND_MESSAGE_SCHEMA = ToolSchema(
    tool_name="send_message",
    fields=[
        FieldSpec("text", str, required=True, max_len=MAX_MEDIUM_STRING),
        FieldSpec("title", str, max_len=MAX_SHORT_STRING),
        FieldSpec("blocks", list, item_type=dict, max_items=50),
        FieldSpec("channel", str, max_len=CHANNEL_MAX_LEN, pattern=CHANNEL_ID_RE),
        FieldSpec("user", str, max_len=USER_MAX_LEN, pattern=USER_ID_RE),
        FieldSpec("unfurl_links", bool),
        FieldSpec("unfurl_media", bool),
        FieldSpec("thread_ts", str, max_len=30, pattern=re.compile(r"^\d+\.\d+$")),
        FieldSpec("reply_broadcast", bool),
        FieldSpec("session", str, max_len=MAX_SHORT_STRING, pattern=re.compile(r"^(origin|slack)$")),
        FieldSpec("caller_session", str, max_len=MAX_SHORT_STRING, pattern=CRON_SESSION_RE),
    ],
)

READ_SLACK_PROFILE_SCHEMA = ToolSchema(
    tool_name="read_slack_profile",
    fields=[
        FieldSpec("user", str, required=True, max_len=USER_MAX_LEN, pattern=USER_ID_RE),
    ],
)

WAIT_SCHEMA = ToolSchema(
    tool_name="wait",
    fields=[
        FieldSpec("seconds", int, required=True, min_val=60, max_val=1800),
        FieldSpec("reason", str, required=True, max_len=MAX_SHORT_STRING),
    ],
)

REGISTER_HOOK_SCHEMA = ToolSchema(
    tool_name="register_hook",
    fields=[
        FieldSpec("hook_id", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("context_summary", str, required=True, max_len=MAX_MEDIUM_STRING),
    ],
)

# ── Tool Schemas (Slack Reactions) ──

# Slack emoji names: alphanumeric, underscores, hyphens, and plus signs
_EMOJI_NAME_RE = re.compile(r"^[a-zA-Z0-9+\-][a-zA-Z0-9_+\-]{0,98}[a-zA-Z0-9]$|^[a-zA-Z0-9+]$")
# Slack message timestamp: digits.digits
_SLACK_TS_RE = re.compile(r"^\d+\.\d+$")

ADD_REACTION_SCHEMA = ToolSchema(
    tool_name="add_reaction",
    fields=[
        FieldSpec("channel", str, required=True, max_len=CHANNEL_MAX_LEN, pattern=CHANNEL_ID_RE),
        FieldSpec("timestamp", str, required=True, max_len=30, pattern=_SLACK_TS_RE),
        FieldSpec("reaction", str, required=True, max_len=100, pattern=_EMOJI_NAME_RE),
    ],
)

LOCAL_KNOWLEDGE_SEARCH_SCHEMA = ToolSchema(
    tool_name="local_knowledge_search",
    fields=[
        FieldSpec("query", str, required=True, max_len=500),
        FieldSpec("limit", int, required=False, min_val=1, max_val=5, default=3),
    ],
)

KNOWLEDGE_DEDUP_SCHEMA = ToolSchema(
    tool_name="knowledge_dedup",
    fields=[
        FieldSpec("apply", bool, required=False, default=False),
    ],
)

# ISO calendar date (YYYY-MM-DD) for the chat-history date filters.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

SEARCH_CHAT_HISTORY_SCHEMA = ToolSchema(
    tool_name="search_chat_history",
    fields=[
        FieldSpec("query", str, required=True, max_len=500),
        FieldSpec("limit", int, required=False, min_val=1, max_val=50, default=10),
        FieldSpec("before", str, required=False, max_len=10, pattern=_ISO_DATE_RE),
        FieldSpec("after", str, required=False, max_len=10, pattern=_ISO_DATE_RE),
        FieldSpec("all_workspaces", bool, required=False, default=False),
    ],
)

GET_CHAT_SESSION_SCHEMA = ToolSchema(
    tool_name="get_chat_session",
    fields=[
        FieldSpec("session_key", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("max_messages", int, required=False, min_val=1, max_val=200, default=50),
        FieldSpec("all_workspaces", bool, required=False, default=False),
    ],
)

# ── Schema Registry ──

MCP_CORE_SCHEMAS: dict[str, ToolSchema] = {
    "spawn_run": SPAWN_RUN_SCHEMA,
    "spawn_sub_agents": SPAWN_SUB_AGENTS_SCHEMA,
    "spawn_list": SPAWN_LIST_SCHEMA,
    "spawn_status": SPAWN_STATUS_SCHEMA,
    "learn_add": LEARN_ADD_SCHEMA,
    "learn_remove": LEARN_REMOVE_SCHEMA,
    "skill_search": SKILL_SEARCH_SCHEMA,
    "task_run": TASK_RUN_SCHEMA,
    "send_message": SEND_MESSAGE_SCHEMA,
    "read_slack_profile": READ_SLACK_PROFILE_SCHEMA,
    "wait": WAIT_SCHEMA,
    "register_hook": REGISTER_HOOK_SCHEMA,
    "file_send": FILE_SEND_SCHEMA,
    "autonudge_stop": AUTONUDGE_STOP_SCHEMA,
    "local_knowledge_search": LOCAL_KNOWLEDGE_SEARCH_SCHEMA,
    "knowledge_dedup": KNOWLEDGE_DEDUP_SCHEMA,
    "search_chat_history": SEARCH_CHAT_HISTORY_SCHEMA,
    "get_chat_session": GET_CHAT_SESSION_SCHEMA,
    "set_project": SET_PROJECT_SCHEMA,
    "artifact_save": ARTIFACT_SAVE_SCHEMA,
    "artifact_get": ARTIFACT_GET_SCHEMA,
    "artifact_update": ARTIFACT_UPDATE_SCHEMA,
    "artifact_delete": ARTIFACT_DELETE_SCHEMA,
    "artifact_list": ARTIFACT_LIST_SCHEMA,
    "artifact_versions": ARTIFACT_VERSIONS_SCHEMA,
    "artifact_revert": ARTIFACT_REVERT_SCHEMA,
    "artifact_folder_list": ARTIFACT_FOLDER_LIST_SCHEMA,
    "artifact_folder_create": ARTIFACT_FOLDER_CREATE_SCHEMA,
    "artifact_folder_rename": ARTIFACT_FOLDER_RENAME_SCHEMA,
    "artifact_folder_move": ARTIFACT_FOLDER_MOVE_SCHEMA,
    "artifact_folder_delete": ARTIFACT_FOLDER_DELETE_SCHEMA,
    "artifact_move": ARTIFACT_MOVE_SCHEMA,
}

MCP_CRON_SCHEMAS: dict[str, ToolSchema] = {
    "cron_list": CRON_LIST_SCHEMA,
    "cron_add": CRON_ADD_SCHEMA,
    "cron_update": ToolSchema(
        tool_name="cron_update",
        fields=[
            FieldSpec("job_id", str, required=True, max_len=16, pattern=_JOB_ID_RE),
            FieldSpec("name", str, max_len=MAX_SHORT_STRING),
            FieldSpec("message", str, max_len=MAX_MEDIUM_STRING),
            FieldSpec("cron_expr", str, max_len=100),
            FieldSpec("every", int, min_val=60, max_val=86400 * 30),
            FieldSpec("agent", str, max_len=MAX_SHORT_STRING, pattern=_AGENT_NAME_RE),
            FieldSpec("channel", str, max_len=CHANNEL_MAX_LEN, pattern=CHANNEL_ID_RE),
            FieldSpec("thread_ts", str, max_len=30, pattern=re.compile(r"^\d+\.\d+$")),
            FieldSpec("approval_mode", str, max_len=10, pattern=re.compile(r"^(auto)?$")),
            FieldSpec("silent", bool),
            FieldSpec("strict_schedule", bool),
            FieldSpec("skip_dates", list, item_type=str, item_max_len=10, max_items=366, item_pattern=re.compile(r"^\d{4}-\d{2}-\d{2}$")),
            FieldSpec("timezone", str, max_len=50, pattern=re.compile(r"^[A-Za-z0-9_/+-]+$")),
            FieldSpec("persistent_session", bool),
            FieldSpec("minimal_context", bool),
            FieldSpec("hide_in_chat", bool),
        ],
    ),
    "cron_remove": CRON_REMOVE_SCHEMA,
    "cron_pause": CRON_PAUSE_SCHEMA,
    "cron_resume": CRON_RESUME_SCHEMA,
    "cron_trigger": ToolSchema(
        tool_name="cron_trigger",
        fields=[
            FieldSpec("job_id", str, required=True, max_len=16, pattern=_JOB_ID_RE),
        ],
    ),
}


# ── Response Schemas ──


@dataclass
class McpTextContent:
    """Type-safe MCP TextContent response — the only content type our tools return."""

    type: str  # always "text"
    text: str

    def to_dict(self) -> dict[str, str]:
        return {"type": self.type, "text": self.text}


def build_tool_response(text: str, max_len: int = MAX_RESPONSE_LEN) -> dict[str, Any]:
    """Build a validated, sanitized MCP tools/call response.

    Returns the ``result`` payload for a JSON-RPC response:
    ``{"content": [{"type": "text", "text": "..."}]}``

    This is the single exit point for all tool responses — ensures every
    response conforms to the MCP TextContent schema and is sanitized.
    """
    text = sanitize_response(text, max_len)
    content = McpTextContent(type="text", text=text)
    return {"content": [content.to_dict()]}


def validate_jsonrpc_response(resp: dict[str, Any]) -> dict[str, Any]:
    """Validate a JSON-RPC 2.0 response envelope before writing to stdout.

    Ensures: has ``jsonrpc``, ``id``, and either ``result`` or ``error``.
    """
    if not isinstance(resp, dict):
        raise ValidationError("response", "must be a JSON object")
    if "id" not in resp:
        raise ValidationError("response", "missing id")
    if "result" not in resp and "error" not in resp:
        raise ValidationError("response", "must have result or error")
    resp["jsonrpc"] = "2.0"
    return resp


# ── Dashboard API Validation Helpers ──


def validate_api_body(body: Any, max_size: int = 100_000) -> dict[str, Any]:
    """Validate a parsed JSON request body from aiohttp."""
    if not isinstance(body, dict):
        raise ValidationError("body", "must be a JSON object")
    raw = str(body)
    if len(raw) > max_size:
        raise ValidationError("body", f"exceeds max size {max_size}")
    return body


def validate_string_field(
    body: dict[str, Any],
    field_name: str,
    *,
    required: bool = False,
    max_len: int = MAX_MEDIUM_STRING,
    allowed: frozenset[str] | None = None,
) -> str:
    """Extract and validate a string field from a request body."""
    val = body.get(field_name)
    if val is None:
        if required:
            raise ValidationError(field_name, "required")
        return ""
    if not isinstance(val, str):
        raise ValidationError(field_name, "must be a string")
    val = sanitize_string(val)
    if not val and required:
        raise ValidationError(field_name, "required (empty after sanitization)")
    if max_len and len(val) > max_len:
        raise ValidationError(field_name, f"exceeds max length {max_len}")
    if allowed and val not in allowed:
        raise ValidationError(field_name, f"must be one of: {', '.join(sorted(allowed))}")
    return val


# ── AskUserQuestion Schema Validation ──

_ASK_MAX_QUESTIONS = 4
_ASK_MAX_OPTIONS = 6
_ASK_MAX_QUESTION_LEN = 500
_ASK_MAX_HEADER_LEN = 50
_ASK_MAX_LABEL_LEN = 200
_ASK_MAX_DESC_LEN = 500


def validate_ask_user_question(raw: object) -> list[dict]:
    """Validate and normalize AskUserQuestion tool input.

    Returns a list of validated question dicts ready for broadcast.
    Raises ValidationError if the top-level structure is invalid.
    Skips individual malformed questions/options defensively.
    """
    if not isinstance(raw, dict):
        raise ValidationError("tool_input", "must be a JSON object")
    questions = raw.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValidationError("questions", "must be a non-empty list")

    result: list[dict] = []
    for q in questions[:_ASK_MAX_QUESTIONS]:
        if not isinstance(q, dict):
            continue
        qt = str(q.get("question") or "")[:_ASK_MAX_QUESTION_LEN]
        if not qt:
            continue
        qh = str(q.get("header") or "")[:_ASK_MAX_HEADER_LEN]
        raw_opts = q.get("options")
        if not isinstance(raw_opts, list):
            continue
        opts: list[dict] = []
        for o in raw_opts[:_ASK_MAX_OPTIONS]:
            if not isinstance(o, dict):
                continue
            label = str(o.get("label") or "")[:_ASK_MAX_LABEL_LEN]
            if not label:
                continue
            desc = str(o.get("description") or "")[:_ASK_MAX_DESC_LEN]
            opts.append({"label": label, "description": desc})
        if not opts:
            continue
        result.append({
            "question": qt,
            "header": qh,
            "options": opts,
            "multiSelect": bool(q.get("multiSelect")),
        })
    if not result:
        raise ValidationError("questions", "no valid questions after validation")
    return result
